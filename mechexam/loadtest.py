"""Load test: how many students can sit an exam at once?

Simulates a class taking an exam against a running server, reproducing the
real traffic pattern the exam page generates per student:

  * one page load to open the exam
  * an autosave POST roughly every time they stop typing
  * a status poll every 20 s (the shared-clock re-sync)

Reports throughput, latency percentiles and — the figure that actually
matters — how many requests failed.

    python loadtest.py --url http://127.0.0.1:8000 --students 40
"""
from __future__ import annotations

import argparse
import random
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

RESULTS_LOCK = threading.Lock()
RESULTS: list[dict] = []


class Student:
    """One simulated student with their own cookie jar."""

    def __init__(self, base: str, number: str, password: str):
        self.base = base.rstrip("/")
        self.number = number
        self.password = password
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.attempt_id: int | None = None
        self.final_url = ""
        self.question_ids: list[int] = []

    def _call(self, method: str, path: str, data: dict | None = None,
              label: str = "") -> tuple[int, str, float]:
        url = self.base + path
        body = urllib.parse.urlencode(data).encode() if data else None
        request = urllib.request.Request(url, data=body, method=method)
        if body:
            request.add_header("Content-Type",
                               "application/x-www-form-urlencoded")

        started = time.perf_counter()
        status, text = 0, ""
        self.final_url = url
        try:
            with self.opener.open(request, timeout=60) as response:
                text = response.read().decode("utf-8", "replace")
                status = response.status
                # urllib follows the redirect, so this is where we landed --
                # which is how we learn the attempt id.
                self.final_url = response.geturl()
        except urllib.error.HTTPError as exc:
            status = exc.code
            text = exc.read().decode("utf-8", "replace")[:400]
        except Exception as exc:                       # timeouts, resets
            status = -1
            text = f"{type(exc).__name__}: {exc}"
        elapsed = time.perf_counter() - started

        if label:
            with RESULTS_LOCK:
                RESULTS.append({"label": label, "status": status,
                                "seconds": elapsed, "detail": text[:200]})
        return status, text, elapsed

    # ---- the exam journey ------------------------------------------------ #

    def sign_in(self) -> bool:
        status, _, _ = self._call(
            "POST", "/login",
            {"identifier": self.number, "password": self.password},
            label="login",
        )
        return status in (200, 302)

    def start_exam(self, exam_id: int, pin: str) -> bool:
        self._call("GET", f"/take/{exam_id}/start", label="start_page")
        status, body, _ = self._call(
            "POST", f"/take/{exam_id}/start", {"access_code": pin},
            label="start_post",
        )
        if status not in (200, 302):
            return False

        import re

        # Starting an exam redirects to /take/attempt/<id>; that landing URL
        # carries the attempt id.
        match = re.search(r"/take/attempt/(\d+)", self.final_url or "")
        if not match:
            match = re.search(r"/take/attempt/(\d+)", body or "")
        if not match:
            return False
        self.attempt_id = int(match.group(1))

        status, html, _ = self._call(
            "GET", f"/take/attempt/{self.attempt_id}", label="sit_page"
        )
        self.question_ids = [int(q) for q in set(re.findall(r'name="q(\d+)"', html))]
        return status == 200

    def autosave(self) -> None:
        if not self.attempt_id or not self.question_ids:
            return
        payload = {
            f"q{qid}": f"{random.uniform(1, 999):.2f}"
            for qid in random.sample(
                self.question_ids, min(3, len(self.question_ids))
            )
        }
        self._call("POST", f"/take/attempt/{self.attempt_id}/autosave",
                   payload, label="autosave")

    def poll_status(self) -> None:
        if not self.attempt_id:
            return
        self._call("GET", f"/take/attempt/{self.attempt_id}/status.json",
                   label="status")

    def submit(self) -> None:
        if not self.attempt_id:
            return
        self._call("POST", f"/take/attempt/{self.attempt_id}/submit",
                   {"reason": "loadtest"}, label="submit")


def run_student(base: str, number: str, password: str, exam_id: int,
                pin: str, duration: float, barrier: threading.Barrier) -> None:
    student = Student(base, number, password)
    if not student.sign_in():
        return

    # Everyone piles in at once, the way a class does when the bell goes.
    try:
        barrier.wait(timeout=120)
    except threading.BrokenBarrierError:
        pass

    if not student.start_exam(exam_id, pin):
        return

    import os

    # Mirror the real client: a save lands shortly after typing stops, but
    # never more often than the "save at most every N seconds" throttle, and
    # the clock is polled on its own, longer interval.
    min_gap    = float(os.environ.get("LT_MIN_GAP", "0"))
    poll_every = float(os.environ.get("LT_POLL", "20"))
    lo, hi     = [float(x) for x in os.environ.get("LT_INTERVAL", "4,12").split(",")]

    deadline    = time.time() + duration
    next_status = time.time() + poll_every
    last_save   = 0.0

    while time.time() < deadline:
        time.sleep(random.uniform(lo, hi))
        now = time.time()
        if now - last_save >= min_gap:
            student.autosave()
            last_save = now
        if now >= next_status:
            student.poll_status()
            next_status = now + poll_every

    student.submit()


def summarise(students: int, wall: float) -> int:
    with RESULTS_LOCK:
        rows = list(RESULTS)

    if not rows:
        print("no requests recorded")
        return 1

    print(f"\n{'=' * 70}")
    print(f"{students} simultaneous students, {wall:.0f} s of exam")
    print(f"{'=' * 70}")
    print(f"{'request':<14}{'count':>7}{'ok':>7}{'failed':>8}"
          f"{'median':>9}{'p95':>9}{'worst':>9}")
    print("-" * 70)

    failures: dict[str, int] = {}
    total_ok = total_bad = 0

    for label in ["login", "start_post", "sit_page", "autosave", "status", "submit"]:
        subset = [r for r in rows if r["label"] == label]
        if not subset:
            continue
        times = sorted(r["seconds"] for r in subset)
        ok = [r for r in subset if 200 <= r["status"] < 400]
        bad = [r for r in subset if not (200 <= r["status"] < 400)]
        total_ok += len(ok)
        total_bad += len(bad)

        for r in bad:
            key = f"{label} -> {r['status']}"
            failures[key] = failures.get(key, 0) + 1

        p95 = times[min(len(times) - 1, int(len(times) * 0.95))]
        print(f"{label:<14}{len(subset):>7}{len(ok):>7}{len(bad):>8}"
              f"{statistics.median(times):>8.3f}s{p95:>8.3f}s{times[-1]:>8.3f}s")

    print("-" * 70)
    requests = total_ok + total_bad
    print(f"{'TOTAL':<14}{requests:>7}{total_ok:>7}{total_bad:>8}"
          f"{'':>9}{'':>9}{requests / wall:>7.1f}/s")

    if failures:
        print("\nFAILURES:")
        for key, count in sorted(failures.items(), key=lambda kv: -kv[1]):
            print(f"  {count:>5}  {key}")
        sample = next((r for r in rows if not (200 <= r["status"] < 400)), None)
        if sample:
            print(f"\n  example: {sample['detail'][:180]}")
    else:
        print("\nNo failures.")

    return 0 if total_bad == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--students", type=int, default=40)
    parser.add_argument("--exam", type=int, required=True)
    parser.add_argument("--pin", required=True)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--prefix", default="LT-")
    args = parser.parse_args()

    barrier = threading.Barrier(args.students + 1)
    threads = []
    for n in range(1, args.students + 1):
        number = f"{args.prefix}{n:04d}"
        thread = threading.Thread(
            target=run_student,
            args=(args.url, number, number, args.exam, args.pin,
                  args.duration, barrier),
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    time.sleep(2)
    print(f"releasing {args.students} students at once...")
    started = time.perf_counter()
    try:
        barrier.wait(timeout=120)
    except threading.BrokenBarrierError:
        print("not every student reached the start line")

    for thread in threads:
        thread.join(timeout=args.duration + 120)
    wall = time.perf_counter() - started

    return summarise(args.students, wall)


if __name__ == "__main__":
    sys.exit(main())
