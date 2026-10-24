# WPU-CET Examination & Gradebook Portal

Online examinations and a gradebook that follows the College of Engineering
and Technology grading system, for the Department of Mechanical Engineering,
Western Philippines University.

Rebuilt from scratch in Python (Flask). It runs on a laptop, on
PythonAnywhere, or on Railway, from the same code, against either SQLite or
PostgreSQL.

---

## Run it on this computer

```bash
pip install -r requirements.txt
python run.py
```

Open <http://127.0.0.1:5000> and sign in as **admin / admin123**. You are asked
to set a real password immediately.

To reach it from a phone on the same Wi-Fi:

```bash
HOST=0.0.0.0 python run.py
```

Then browse to this computer's address, for example `http://192.168.1.5:5000`.

---

## Before anything else: revoke the old Google key

A `key.json` service-account key was shared in an earlier conversation, which
means **that key must be treated as compromised**. In the Google Cloud
Console:

1. **IAM & Admin → Service Accounts** → open
   `exam-portal-uploader@…` → **Keys**
2. Delete the key whose id begins `0b035d96`
3. **Add key → Create new key → JSON**

Put the new key only in your host's environment variables, as
`GOOGLE_SERVICE_ACCOUNT_JSON`. Never commit it: `.gitignore` already blocks
`key.json`, and the portal only ever reads credentials from the environment.

---

## Deploy on Railway

Railway is the better home for this: it gives you PostgreSQL and a persistent
volume, so nothing is lost on redeployment.

1. **Push the code to GitHub.** `data/` is git-ignored on purpose — it holds
   student records and must never be in the repository.

2. **New Project → Deploy from GitHub repo**, and pick this repository.

3. **Add PostgreSQL**: in the project, *New → Database → Add PostgreSQL*.

4. **Add a volume** so uploads and backups survive: select the web service,
   *Settings → Volumes → New Volume*, mount path `/data`.

5. **Set the variables** (service → *Variables*):

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
   | `DATA_DIR` | `/data` |
   | `SECRET_KEY` | a long random string of your own |
   | `ADMIN_PASSWORD` | your first administrator password |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | the new service-account JSON, on one line |
   | `GOOGLE_DRIVE_IMPERSONATE` | your own address, if using domain-wide delegation |

   `SCHOOL_NAME`, `COLLEGE_NAME`, `DEPARTMENT_NAME` and `CAMPUS_NAME` already
   default to WPU-CET; set them only to change the wording on printed sheets.

6. **Generate a domain**: *Settings → Networking → Generate Domain*.

7. Open the site, sign in, and go to **Administration → System check** to
   confirm the database, the stylesheet and Drive all report healthy.

Railway reads `railway.json` and `Procfile`, so the start command and the
`/healthz` health check are already configured.

### Why your data went missing before

`postgres://` is the URL Railway hands out, and SQLAlchemy 2 no longer accepts
that spelling — so the app quietly fell back to a SQLite file inside the
container, which a redeployment then replaced. This build rewrites the URL to
`postgresql+psycopg2://` on startup, and the System check page shows which
database is actually in use, so the mistake cannot go unnoticed.

---

## Deploy on PythonAnywhere

1. **Bash console**:

   ```bash
   git clone https://github.com/YOURNAME/YOURREPO.git mechexam
   cd mechexam
   pip install --user -r requirements.txt
   mkdir -p ~/mechexam-data
   ```

   If `psycopg2-binary` will not install, delete that line from
   `requirements.txt` — PythonAnywhere uses SQLite and does not need it.

2. **Web tab → Add a new web app → Manual configuration → Python 3.10+**

3. Click the **WSGI configuration file** link, delete what is there, and paste
   the contents of `pythonanywhere_wsgi.py`, changing `YOURNAME` to your
   username and setting your own `SECRET_KEY`.

4. **Do not add a static files mapping.** See below.

5. **Reload** the web app.

### Why the administration pages looked broken before

PythonAnywhere was serving an **old copy of `app.css`** from its own static
cache. The page's markup was new, the stylesheet was old, so the sidebar and
the administration panels rendered unstyled while the rest looked fine — and
redeploying could not fix it, because the stale copy lived in a layer the
application could not reach.

This build removes that failure mode by construction:

* The stylesheet and scripts are served **by the application**, from a URL
  carrying a hash of the file's own bytes —
  `/asset/9f2a1c…/css/app.css`. A cache can only ever hold a URL matching
  bytes it actually received, so "stale" is not a state it can be in.
* Those URLs are marked immutable, so caching stays aggressive; it is the URL
  that changes when the file does.
* Pages themselves are sent `no-store`, so a fresh shell is never held back.
* **Nothing references `/static/` for the app's CSS or JavaScript**, which is
  why a static-files mapping must not be added. If one is already configured
  for this app on the Web tab, remove it.

**Administration → System check** shows the stylesheet's current hash, so you
can confirm at a glance that the right file is being served.

---

## Upgrading an existing installation

1. **Take a backup first**: Administration → System check → *Download a backup
   now*. One file holds every record.
2. Replace the code (`git pull`, or upload the files).
3. Reload the web app, or let Railway redeploy.

The database is **not** in the code folder, so replacing the code cannot touch
it. On startup the app compares the live database with what the models expect
and adds any missing columns by itself, so an older database is brought up to
date without a migration step. Administration → System check lists exactly
what was added.

### Moving between hosts

A backup taken on SQLite restores into PostgreSQL and the other way round, so
you can move the whole portal from PythonAnywhere to Railway without losing
anything: download a backup on the old host, then on the new one use
Administration → System check → *Restore*, type `REPLACE`, and upload it.

---

## What it does

### Examinations

* **Question types**: multiple choice, true or false, numerical answer with a
  percentage tolerance, short answer, matching type, essay, and multi-part
  problem solving — one problem statement with as many lettered sub-items as
  you like, each with its own answer, type and points.
* **Equation editor** on the question text *and* on every option, with a
  symbol palette (Basic, Calculus, Greek, Engineering) and a live preview.
  The renderer is bundled with the app, so **no internet connection is needed
  during an exam**.
* **Figures** can be attached to any question; they are stored outside the
  code folder and survive upgrades.
* **One clock for the class.** The timer starts when *you* open the exam and
  runs for everyone at once; a student who signs in late inherits the time
  that is left. When it expires the exam closes for everybody and whatever is
  on screen is submitted.
* **Answers are saved as they are typed.** A flat battery or a dropped
  connection costs a student nothing.
* **A locked phone does not end an exam.** A browser cannot tell a locked
  screen from a student opening another app — both hide the page and both take
  the focus away — so the exam does not guess. It measures how long the
  student was gone and decides when they come back, never while they are away.
  An absence shorter than the exam's allowance (90 seconds by default) is
  recorded as an interruption and never penalised; longer ones are forgiven a
  set number of times with a warning on screen; only after that does strict
  mode submit, and it says why. An earlier build submitted on the browser's
  `blur` event, which is exactly what locking a phone produces.
* **Exam traffic is tunable.** Administration → Year & semester sets how
  often answers are saved (at most every N seconds — answers still land
  shortly after a student stops typing, so nobody can lose more than N
  seconds of work) and how often the clock is re-checked. On a metered host
  these two settings are the difference between an affordable exam and an
  expensive one: 45 s and 90 s cut the running cost of a two-hour sitting by
  about five times. The autosave cannot be set beyond 120 seconds — that is a
  safety floor, not a preference.
* **Reopen or reset** an attempt: reopening restores the time and keeps every
  answer, which is the fix for an exam submitted by accident.
* **Score override** by the instructor, which survives any later re-marking.
* **Question banks**: keep items by topic and difficulty, pull a set into any
  exam, or have the exam draw a different random selection for each student.
* **A whole paper from a spreadsheet.** Type the questions in Excel, save as
  CSV UTF-8, and upload it into a bank or straight onto an exam. One sheet
  carries all seven question types, including matching pairs and multi-part
  problems; there is a download of a worked template so no column has to be
  guessed. Nothing is written until you have seen a preview of exactly what
  was read, and every row it could not read is named with its line number and
  the reason, so one typo in row 40 does not cost you the other 39. Banks and
  exams export back out in the same format — the quickest way to rewrite a
  long paper is export, edit in Excel, import.
* **Preview as a student sees it.** The preview is rendered by the very same
  template the exam itself uses, so there is nothing to keep in step: if the
  preview looks right, the exam looks right. It creates no attempt and writes
  nothing, so it is safe to open while an exam is running, and it will show
  you another student's shuffle.
* **A paper copy, with an answer key.** Print the same questions in the same
  order, optionally as several shuffled versions with space for working and a
  separate answer sheet. Each version is seeded, so reprinting Set B gives the
  same Set B and the key you printed still matches the paper in a student's
  hand. Power and signal are not guaranteed in Palawan, and an examination
  sat on paper beats one cancelled.
* **Item analysis**: difficulty and discrimination per question, so you can
  see which items are not working and why.
* **The marked paper, printed.** One student's exam exactly as it was
  answered — what they chose, what the key was, the marks for each question,
  the sub-items of a problem one by one, and for a written answer the rubric
  criterion by criterion with the marker's comment. Print one student, or the
  whole class in one go with a sheet each. The correct answers and the
  explanations can each be withheld, so the same page serves for handing back
  and for a paper you intend to reuse.

### The authoring assistant

* **A dedicated tab** where you describe the paper you want — "ten numerical
  problems on the first law, moderate, with tolerances" — and get a draft
  back. It knows the seven question types, writes mathematics in the same
  LaTeX the portal renders, and works out numerical answers rather than
  leaving placeholders.
* **It cannot save anything.** What it writes is a question sheet in exactly
  the CSV format a person would upload, and it is handed to the same importer
  and the same review screen. You see every question before one of them
  reaches a bank, and a malformed row is reported rather than stored. The
  assistant therefore has the authority of somebody typing into Excel, and no
  more: it cannot read the gradebook, cannot mark, cannot release, and cannot
  write to a bank without you pressing Import.
* **One key for the portal**, set by an administrator in Administration ▸
  Assistant, so every instructor can use it and the billing is in one place.
  The key is never shown back on the page once saved. Better still, set
  `ANTHROPIC_API_KEY` in the host's environment — it takes precedence and
  never touches the database. The model is chosen from a list fetched from the
  API itself, so it stays right as models change.
* **It will not work on a free PythonAnywhere account.** Outbound traffic from
  a free account is limited to an allowlist that does not include the API. On
  a paid account set `HTTPS_PROXY=http://proxy.server:3128`; on Railway or an
  ordinary host nothing is needed.
* Student names and marks are never sent anywhere.

### Gradebook

* The CET structure: Written Works 20%, Performance Task 40%, Major
  Examinations 40% for lecture; 20%, 60% and 20% for laboratory; final grade
  67% lecture plus 33% laboratory. Laboratory can be switched on per subject
  and every weight is editable.
* Transmutation from 95.60 = 1.00 down to 60.00 = 3.00, below that 5.00.
* **The lecture and laboratory split belongs to each subject**, not to the
  portal, because no two subjects divide the same way. Set it on the subject
  with the common splits as presets; the figures are read as a ratio, so 70
  and 30 means the same as 7 and 3. Changing one subject re-computes that
  subject and touches no other.
* Two averaging methods — total points over total possible (the college
  sheet), or the average of each activity's own percentage — and a per-activity
  multiplier so one quiz can count twice.
* **An activity with no mark recorded is ignored, not counted as zero**, so a
  gradebook in progress reads honestly.
* **Performance tasks are added from the subject page**, not only through the
  gradebook.
* **Rubrics** for essays and performance tasks — video presentation, technical
  report, laboratory report, oral reporting, essay, design project — at 10
  points per criterion, with a marking page that totals as you go and a
  breakdown the student can see.
* **Removal examination**: a standing of 55.00–59.99% is a candidate rather
  than a failure. Passing gives exactly 3.00; holding the examination moves
  everyone who already passed one grade higher (2.50 becomes 2.25). None of it
  takes effect until you finalise the subject, and finalising can be undone.
* **The registrar's Report of Grades**, WPU-OSF-OSAS-OAR-09 Rev.00,
  reproduced rather than paraphrased: the university seal and heading, the
  Instructor / Course Code / Section and College / Course Title / Programme
  blocks, the #, Student #, Name, Final Grade, Completion Grade and Remarks
  columns, the signature block and the form number. One programme per sheet,
  so it prints per section; sections carry their programme written out in
  full. This is the sheet that is signed and filed — the working grade sheet
  below is a separate page.
* **The gradebook grid on paper** — the whole class, students down and every
  activity across, with each component's weight, the component percentages,
  the scope standing, the final percentage, the grade and the remark. A term's
  gradebook is wider than any sheet, so the columns are split across sheets
  rather than shrunk until the figures cannot be read: a component is kept
  whole wherever it fits, the standing columns repeat on every part so any one
  sheet can be read alone, and you can set how many activity columns go on a
  sheet. Long class lists flow onto further sheets with the headings repeated.
  Lecture and laboratory print separately, with signature blocks for the
  instructor, the chair and the dean.
* **A full record of marks per student**, showing every activity behind the
  grade: written works, performance tasks and major examinations, lecture and
  laboratory separately with each component's weight, every mark with its date
  and percentage, and where a mark came from a rubric the criteria and what
  was given for each. Attendance is summarised at the foot. Print one student
  or the whole section, a sheet each. This is the sheet to hand a student who
  asks where a mark came from.
* **Printable grade sheet** in A4 landscape with the university heading,
  signature blocks, remarks and a pass / removal / fail tally; also a class
  list, a per-student grade slip and an attendance record.
* Marks can be pasted in from a list rather than clicked one by one.

### People and terms

* Three roles: administrator, instructor, student. **An instructor sees only
  the subjects assigned to them**, and cannot add or delete subjects.
* One school year and semester for the whole portal, with an end-of-semester
  rollover that carries subjects forward and keeps every past term's records.
* Sections per subject, with section tabs on the roster, the gradebook and the
  grade sheet.
* Per-subject drop, which keeps the marks and can be undone; and a whole-year
  standing — shifted course, transferred out, stopped — which removes a
  student from every class list for that year while keeping their records.
* Two lists: the term students roll, and the class list per section.
* Students get their own password and may change it; instructors issue a reset
  code when someone forgets, so no email server is needed.
* **The forced password change can be lifted in bulk.** A student created from
  a class list starts with their student number as the password and is asked
  to choose a new one at first sign-in — right for accounts that will outlive
  the term, wrong on the morning of an examination when it puts a form between
  sixty students and their paper. Administration ▸ Password prompts lists the
  students, ticks everyone currently being asked, and lifts it for whoever you
  leave ticked; filter by subject and section first. Nobody's password is
  changed, only the prompt. There is also a standing setting for whether newly
  created accounts are asked at all, so an import made ten minutes later does
  not put it back.
* Class lists can be imported by pasting them in, creating accounts as it goes.
* **One student can be added straight from the subject page** — for the late
  enrolee who turns up in the second week. If the student number is already on
  record the existing account is used and only the enrolment is added, so a
  student you share with another subject keeps one account and one password;
  if the number is new, the account is created and enrolled in one step. A
  student who was dropped is readmitted rather than duplicated.
* **Attendance** per meeting, with a rate per student.

### Everything else

* **Dashboard of subject tiles**: every subject you teach as a coloured card
  showing its headcount, exam count and grade split, clickable straight
  through to the subject. Each subject carries one of twelve colours you pick
  from the tile itself; a subject you have not coloured derives a stable one
  from its code, and tiles on screen are nudged apart so no two ever look
  alike. You choose the background (plain, tinted, or the department seal as a
  watermark), the tile size, and light or dark, and the choice is yours alone
  — it does not change what anyone else sees.
* **Announcements** to a class or a section, and private messages, with an
  unread badge.
* **File submissions** that go into the instructor's Google Drive folder
  automatically — no dragging — with inline video and image preview. Every
  submission is stored on the server first, so a Drive problem never costs a
  student their work.
* **Installable on Android** and usable from the home screen.
* **Backup and restore** in one click, and an activity log recording every
  grade change, override and finalisation with who did it and when.
* Light and dark appearance; built for a phone first.
* **System check** page: database, schema state, stylesheet hash, Drive status
  and row counts, so a problem can be diagnosed without a developer.

---

## How it is put together

```
app/
  __init__.py      application factory, filters, error handlers
  config.py        every setting, read from the environment
  models.py        the database tables
  migrate.py       brings an older database up to date on startup
  grading.py       the CET arithmetic, transmutation, removal examination
  marking.py       automatic marking of exam answers
  security.py      roles, access checks, subject visibility
  assets.py        content-hashed CSS and JavaScript  <- the stale-cache fix
  backup.py        export and restore, portable between databases
  drive.py         Google Drive uploads, credentials from the environment
  seed.py          first-run administrator, settings, rubric templates
  blueprints/      one module per area of the app
  templates/       pages
  static/          stylesheet, scripts, images, bundled equation renderer
data/              database, uploads, backups — never in git
```

Two tests come with it:

```bash
python smoke.py        # walks every page as each role, and the write paths
python test_rules.py   # checks the rules: grading, removal, access, timing
```

A third tool measures what a real class costs the server, so capacity is a
measurement rather than a guess:

```bash
# against a running server, with a seeded class and an open exam
python loadtest.py --url http://127.0.0.1:8000 --exam 1 --pin ABC123 --students 63
```

Both run against SQLite by default and against PostgreSQL if `DATABASE_URL`
is set, which is how the dual-database support is verified.

---

## If something goes wrong

**A page looks unstyled.** Reload it once. If it persists, open
Administration → System check and look at the stylesheet hash: if it says
*missing*, the `app/static/css/app.css` file did not upload. On
PythonAnywhere, make sure there is no `/static/` mapping on the Web tab.

**Equations show as `$x^{2}$`.** The bundled renderer did not upload. Check
`app/static/vendor/katex/` has `katex.min.css`, `katex.min.js` and a `fonts`
folder; System check reports this too.

**A Drive upload failed.** System check explains which arrangement is in
force. A service account has no Drive storage of its own, so uploads must go
either into a Shared drive it belongs to, or be performed as the instructor
through domain-wide delegation with `GOOGLE_DRIVE_IMPERSONATE`. Submissions
are safe on the server either way.

**Grades look wrong.** Open the student's page in the gradebook: it shows each
activity, each component's percentage, what it contributes, and the final
arithmetic in full.

**Something was deleted by mistake.** Restore the most recent backup from
System check. Taking one at the end of every term is worth the ten seconds.
