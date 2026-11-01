"""Run the portal on the room's own network, for an examination.

    python run_lan.py

Everything an examination in a room needs and nothing it does not: it binds
to the wireless network rather than to the laptop alone, prints the address
to write on the board, and says what to do if a phone cannot reach it.

Three things it deals with that catch people out:

* **Binding.** A development server listens on 127.0.0.1 by default, which
  only the laptop itself can reach. This binds to 0.0.0.0, which is every
  network the machine is on, so the phones can get in.

* **The server.** gunicorn does not run on Windows at all. This uses waitress
  where it is installed, which does, and falls back to Flask's own server with
  a warning rather than refusing to start — an examination about to begin is
  the wrong moment to be told to install something.

* **A secret key.** Sessions are signed with it. Generated and kept in the
  data directory on first run, so signing in does not stop working every time
  the laptop restarts, which is what happens when the key is random per boot.
"""
from __future__ import annotations

import argparse
import os
import secrets
import socket
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_PORT = 8000


def local_addresses() -> list[str]:
    """Every address of this machine a student's phone might reach it on."""
    found: list[str] = []
    for probe in ("10.255.255.255", "192.168.255.255", "172.31.255.255",
                  "8.8.8.8"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((probe, 1))
            address = sock.getsockname()[0]
            if address and not address.startswith("127.") and address not in found:
                found.append(address)
        except OSError:
            pass
        finally:
            sock.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address and not address.startswith("127.") and address not in found:
                found.append(address)
    except OSError:
        pass
    return found


def secret_key(data_dir: Path) -> str:
    """A key that survives a restart, so nobody is signed out by a reboot."""
    keyfile = data_dir / "secret_key.txt"
    if keyfile.exists():
        key = keyfile.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = secrets.token_hex(32)
    data_dir.mkdir(parents=True, exist_ok=True)
    keyfile.write_text(key, encoding="utf-8")
    try:                                    # best effort; Windows ignores it
        os.chmod(keyfile, 0o600)
    except OSError:
        pass
    return key


def banner(addresses: list[str], port: int, data_dir: Path) -> None:
    line = "=" * 66
    print("\n" + line)
    print("  WPU CET Examination Portal — running on this network")
    print(line)

    if addresses:
        print("\n  Students open ONE of these on their phone:\n")
        for address in addresses:
            print(f"      http://{address}:{port}/")
        print("\n  Write it on the board. Nothing else on the phone matters.")
    else:
        print("\n  !! No network address found. This laptop does not appear to")
        print("     be on a wifi or cable network, so nothing can reach it.")
        print("     Join the examination wifi and start this again.")

    print(f"\n  Marks are saved in: {data_dir}")
    print("  Back that folder up straight after the examination.")
    print("\n  If a phone cannot open the address:")
    print("    1. Check the phone is on the SAME wifi, with mobile data off.")
    print("    2. Allow this program through the laptop's firewall. On")
    print("       Windows the prompt appears the first time — tick Private")
    print("       networks. If no prompt appeared, see the README.")
    print("\n  Stop the examination server with Ctrl+C.")
    print(line + "\n")


def start_auto_sync(application, every_seconds: int = 300) -> None:
    """Try to push results to the main portal whenever there is a connection.

    Runs in the background while the examination server is up. It does
    nothing when nothing is configured, nothing when there is no connection,
    and says so on the Results transfer page either way. Deliberately quiet:
    an examination in progress is not the moment for a dialogue about the
    network.
    """
    import threading

    def loop() -> None:
        import time

        from app import sync
        from app.models import Course, Setting

        while True:
            time.sleep(every_seconds)
            try:
                with application.app_context():
                    if not Setting.get_bool(sync.SETTING_ENABLED):
                        continue
                    if not sync.is_configured():
                        continue

                    year, semester = Setting.term()
                    courses = Course.query.filter_by(
                        school_year=year, semester=semester
                    ).all()
                    for course in courses:
                        package = sync.build_package(course)
                        if not package.get("exams") and not package.get("marks"):
                            continue
                        result = sync.send(package, timeout=90)
                        sync.remember_result(result)
                        if result.get("ok"):
                            print(f"  [sync] {course.code}: sent to the main portal")
                        elif not result.get("offline"):
                            print(f"  [sync] {course.code}: {result.get('error')}")
                        else:
                            break      # no connection; try the whole lot later
            except Exception as error:                  # noqa: BLE001
                print(f"  [sync] skipped this round: {error}")

    threading.Thread(target=loop, daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="0.0.0.0",
                        help="0.0.0.0 means every network this machine is on")
    parser.add_argument("--data-dir", default="",
                        help="where the marks are kept (default: ./data)")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).expanduser().resolve() if args.data_dir \
        else HERE / "data"
    os.environ.setdefault("DATA_DIR", str(data_dir))
    os.environ.setdefault("SECRET_KEY", secret_key(data_dir))

    sys.path.insert(0, str(HERE))
    from app import create_app

    application = create_app()

    report = application.config.get("SCHEMA_REPORT") or {}
    if report.get("missing"):
        print("\n  !! The database is missing: "
              + ", ".join(report["missing"][:5]))
        print("     Sign in as the administrator and use System check ▸ "
              "Repair the database now before the examination.\n")

    with application.app_context():
        from app import sync
        from app.models import Setting

        if Setting.get_bool(sync.SETTING_ENABLED) and sync.is_configured():
            print("  Results will be sent to "
                  f"{Setting.get(sync.SETTING_URL)} when there is a connection.")
            start_auto_sync(application)

    banner(local_addresses(), args.port, data_dir)

    try:
        from waitress import serve
    except ImportError:
        print("  (waitress is not installed, using the built-in server —")
        print("   fine for one class, but install waitress for a big one:")
        print("   pip install waitress)\n")
        application.run(host=args.host, port=args.port, debug=False,
                        threaded=True)
        return 0

    # Threads, not processes: SQLite behaves badly with several writers, and
    # one examination does not need more than this.
    serve(application, host=args.host, port=args.port, threads=16,
          channel_timeout=120, ident="mechexam")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
