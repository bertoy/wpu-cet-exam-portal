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
* **Leaving the exam ends it, and the server is what ends it.** The moment
  the exam leaves a student's screen the client says so, and from then on the
  server keeps the clock. Once the absence passes the exam's allowance the
  attempt is submitted — whether or not that device is ever heard from again,
  so a phone switched off mid-exam does not escape. The sweep runs on the
  pages already being polled: the instructor's monitor, and every other
  student's clock check. (If literally everyone leaves at once there is
  nobody left to poll, and those attempts are submitted when the exam's own
  time runs out.)
* **The allowance goes down to 5 seconds**, and the trade-off is real and
  stated in the editor: a browser cannot tell a locked screen from a switch to
  another app. Below about 60 seconds a screen that times out on its own, an
  incoming call or a notification will end the exam too. 90 seconds is the
  default and is safe for unsupervised sittings; a short allowance belongs in
  a room you are watching. The student is told which regime applies before
  they start.
* **A watermark naming the student**, on by default. No browser offers any
  way for a page to block a screenshot, a screen recording, or an on-screen
  assistant reading the page — so the portal does not pretend to. It prints
  the student's own name, number and start time faintly across the questions
  instead, so a capture that circulates identifies whoever made it. The marks
  sit over the paper rather than behind it, deliberately: behind, a capture
  cropped to one card would carry no mark at all. They are faint enough to
  read straight through, hidden from screen readers, and ignore taps. Removing
  them with developer tools is possible — this is a deterrent, not a lock —
  but a casual removal is put back and recorded for the instructor.
* **The screen is kept awake** for the length of the exam, through the
  browser's wake lock. There is no way for a page to be told that a screen is
  dimming — by the time anything is observable the screen has gone — so
  rather than react to it the exam asks the system not to dim at all. This
  matters most where the absence allowance is short, since a phone that
  sleeps on its own is otherwise submitted. Chrome on Android has had it
  since 2020 and Safari on iOS since 18.4; anywhere else the screen simply
  behaves normally and nothing else changes.
* **Hiding the questions when the exam is not in front**, off by default.
  The paper is blanked the moment the window loses focus, so an assistant
  opened over the top has nothing to read. It cannot stop a capture taken in
  the same instant, and on some phones an overlay never takes the focus at
  all, so it is a narrowing rather than a barrier.
* PrintScreen is recorded where a browser exposes it, which is desktops only
  and only after the capture has already happened. **An on-screen assistant
  overlay cannot be detected at all** — no browser reports one — so the only
  defence against it is the blanking above, which catches the overlays that
  take the focus and misses the ones that do not.
* **An examination can be restricted to the room's own network.** List the
  networks it may be sat from (`192.168.1.0/24`, or just `192.168.1.`) and a
  student reaching it from anywhere else is turned away *before* an attempt
  is created, so switching to mobile data does not get round the room's wifi.
  Leaving the network mid-exam hides the questions and is recorded. Saving
  and submitting are never blocked by this: losing somebody's work to a
  network rule would be a worse outcome than the lookup it prevents. There is
  a page per exam showing the address students connect to, the networks this
  machine is on, and how to set the room up — and it says plainly that the
  rule is useless behind a proxy, where every student appears to arrive from
  the same address.
* **An inactivity limit**, off by default. When it is set, an exam with no
  typing, tapping or scrolling for that long is submitted. Reading is not
  activity, so a student thinking about a long problem can be caught by it —
  which is why it is off unless you turn it on.
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
* **A paper copy in the college's own format, with the answer key on the last
  page.** US Letter, Georgia 11pt, the Name / Program / Year / Section / Score
  line across the top, the centred title block, items grouped into parts by
  topic ("Part I — Stresses in Beams"), the four options across the page,
  "Good Luck!", and the Prepared by / Checked by signature blocks. The
  university seal is at the head and the form's control number at the foot;
  both the chair's name and that control number are settings, and either can
  be typed in for a single printing without changing the standing value.
  Leaving the chair's name blank gives a ruled line to sign over.
  The answer key follows a page break as a grid of letters read down each
  column, with the scoring line beneath it. It can be switched off to print
  the students' copies alone. Several shuffled versions can be printed, each
  seeded so reprinting Set B gives the same Set B and its key still matches
  the paper in a student's hand. Power and signal are not guaranteed in
  Palawan, and an examination sat on paper beats one cancelled.
* **Item analysis**: difficulty and discrimination per question, so you can
  see which items are not working and why.
* **The marked paper, printed.** One student's exam exactly as it was
  answered — what they chose, what the key was, the marks for each question,
  the sub-items of a problem one by one, and for a written answer the rubric
  criterion by criterion with the marker's comment. Print one student, or the
  whole class in one go with a sheet each. The correct answers and the
  explanations can each be withheld, so the same page serves for handing back
  and for a paper you intend to reuse.

### The assistant pane

An add-in in the sense Word means it: a panel that slides in beside whatever
page you are on, sees it, and drafts, checks or explains against it. Open it
with the **Assistant** button at the bottom-right of any staff page, or with
**Alt+A**.

What it can see depends on where you opened it, and the quick actions change
to match:

| On | It can see | It offers |
|---|---|---|
| An examination | The whole paper, with your answer key and notes | Check this paper · Is the spread right? · Draft a few more items · Write the instructions |
| One question | The item, its key and its rubric | Check this item · Make it harder · Better distractors · Write a worked solution |
| A student's paper, being marked | The answers that need judgement, with the key and rubric | Suggest marks and feedback · Where did they go wrong? · Is my rubric covering this? |
| A gradebook or class record | The figures, in aggregate | How is this class doing? · Who needs help, and with what? · Draft a remark |

Three rules are built into it rather than left to good behaviour:

* **It cannot save anything.** There is no code path from the pane to a
  record — no session, no insert, no update. Everything it writes has a
  **Copy** button, and copying is how a suggestion becomes a question or a
  mark. `test_rules.py` reads the syntax tree of the pane and fails if the
  machinery to write ever appears.
* **It cannot cross into another instructor's subject.** The identifier the
  browser sends is re-checked against your own subjects on every request, so
  editing a number in a request gets you nothing.
* **Students never see it**, and above all not during an examination. It is
  not rendered on their pages, and every route behind it is staff-only.

A student's paper goes to the model **without the student's name or number**.
The model does not need it to judge an answer against a rubric, and a name is
what turns a transcript into a record about a person.

**It needs internet**, so it does nothing during an examination on the room's
own network — which is the point of that network. The pane says so plainly
rather than failing silently, and works again when you are back online. On
PythonAnywhere's free tier it cannot work at all, because `api.anthropic.com`
is not on the outbound allow-list; the $10 Developer plan lifts that.

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
* **INC for work left undone, where finishing it could still pass.** A
  component with no marks in it is rescaled out of the weighting, so without
  this a student who never sat a major examination was graded on their
  quizzes alone — and could come out passing. At finalisation a student is
  recorded **INC** if they have more than the allowed number of unmarked
  performance tasks (**two by default, set per subject**, counted across
  lecture and laboratory together) **or sat no major examination at all**.
  * **Only where it could still pass.** Full marks on everything outstanding
    are assumed and the grade recomputed; if that still fails, the student is
    recorded as failed, not INC. An incomplete grade that cannot become a
    pass only postpones the news by a term.
  * An activity that does not exist is nobody's fault: a subject with no
    major examination entered yet does not put the whole class on INC.
  * The computed standing is kept alongside the INC, so completing the work
    after a reopen does not start from nothing.
  * **INC shows live**, as the grade, from the moment the condition is met —
    in the gradebook, on the student's own page once grades are released, and
    on the class record. The gradebook prints the reason beside it, so a
    student can be chased about the right thing.
  * The student is told **why** and **what finishing it could still reach**,
    rather than being shown a bare INC.
  * Underneath, the computed grade is kept intact, because finalising and the
    removal examination both need the real figure — a student recorded INC
    still has their true pre-removal grade stored. The finalise page previews
    every case with its reason and the best mark still reachable.
* **Marks can be entered two ways, and they obey the same rules.** The
  activity page marks one activity across the whole class, which is what you
  want after a quiz. **A student's own page marks every one of that student's
  activities in one pass**, which is what you want when someone comes to make
  up four things they missed. Open a student from the gradebook, type into
  the Score column, and save.
  * A **blank box clears the mark**; it does not record a zero. "Not yet" has
    to stay different from "nothing", or an in-progress gradebook lies.
  * The refusals are identical on both screens — nothing above the maximum,
    nothing negative, nothing that is not a number — because they are written
    once and shared. A gradebook where a rule depends on which screen the
    mark was typed into is worse than one with no shortcut at all.
  * **An overridden mark is shown but not editable there.** An override
    carries a recorded reason, so it is changed on the activity page or not
    at all. The server enforces this, not just the form.
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
  instructor, the chair and the dean. **A failing row is tinted red and a
  removal candidate yellow**, with the grade and remark themselves more
  strongly coloured, so a class of sixty can be scanned without reading every
  remark — and the tints survive printing rather than coming out white.
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

## Running an examination on the room's own network

The strongest thing you can do about a student looking an answer up is to
take the internet out of the room. Nothing in the portal comes close to it.
This runs the whole portal from your laptop, on a wireless network with no
way out, so the examination also keeps working whatever the campus
connection does that morning.

Do steps 1 to 3 once, days before. Steps 4 to 9 on the day.

### 1. Put the portal on the laptop (once)

Install [Python](https://www.python.org/downloads/) — on Windows, tick
**Add Python to PATH** on the first screen of the installer. Then download
this repository (**Code ▸ Download ZIP** on GitHub) and unzip it somewhere
you will find again, such as `Documents\mechexam`.

### 2. Start it once at your desk (once)

Open the `mechexam` folder and double-click **`start-exam-lan.bat`**
(on a Mac or Linux, run `./start-exam-lan.sh`).

The first run takes a few minutes and needs internet, because it installs
what it depends on. After that it starts in seconds and needs no internet
ever again. When it finishes it prints something like:

```
  Students open ONE of these on their phone:

      http://192.168.1.14:8000/
```

Open that address in the laptop's own browser and sign in as the
administrator. If this is a brand-new copy, the account is `admin` with the
password `admin123` and it will make you change it immediately.

### 3. Carry your subjects across (once)

If your marks are on PythonAnywhere, take a backup there from
**Administration ▸ System check ▸ Backup**, download the file, and restore
it on the laptop from the same page. Otherwise set the subjects up here and
import the class lists as usual.

**Decide now which machine is the real one.** Marks entered on the laptop do
not appear on PythonAnywhere by themselves, and the reverse. The rule that
does not go wrong: the laptop is the real one for the examination itself, and
the website stays the real one for the term's records. **Administration ▸
Results transfer** then carries the examination back without touching
anything else — see *Carrying the results back*, below. Set that page up on
both machines now, while you have time, rather than on the day.

### 4. Set the room's wifi up

Pick whichever you can manage. The first is the hardest to get wrong.

| | What to do | Internet in the room |
|---|---|---|
| **A router with its internet cable unplugged** | Plug nothing into the socket marked WAN or Internet. Everyone joins its wifi. | None for anybody, including you — there is nothing to misconfigure |
| **A router with the internet blocked for students** | Reserve an address for your laptop, then apply parental controls or an access schedule to every *other* device | You keep it, the room does not |
| **Your phone as the hotspot** | Turn the hotspot on, turn **mobile data off** | None. But a hotspot usually caps at 8–10 devices, so not a class of sixty |

### 5. Start the portal and find its address

Join the examination wifi on the laptop, then double-click
**`start-exam-lan.bat`**. Write the address it prints on the board.

Windows will ask whether to allow it through the firewall the first time —
**tick Private networks and allow it**. If no prompt appears and phones
cannot connect, open PowerShell as administrator and run:

```
netsh advfirewall firewall add rule name="WPU Exam Portal" dir=in action=allow protocol=TCP localport=8000 profile=private
```

### 6. Lock the examination to that network

In the exam, open **Set this up** beside *Networks this exam may be taken
on*. The page shows the networks this laptop is on — press the one that
matches and save. A student who leaves the room's wifi for mobile data is
then refused before an attempt is created.

This is the half the router cannot do. The router stops a phone reaching the
internet; only the portal can stop a phone leaving the room's wifi
altogether.

### 7. Test it before the students arrive

On your own phone, **with mobile data off**: open the address, sign in as a
student, start the exam, answer one question. Then turn mobile data on and
try again — you should be refused. Two minutes here saves an examination.

### 8. Run the examination

Press **Open the exam** when the class is seated; the clock starts for
everyone at once. Keep the **Monitor** page open — it drives the checks that
end an abandoned attempt, and shows you who has finished.

### 9. Afterwards — back it up before anything else

**Administration ▸ System check ▸ Backup**, download the file, and copy it
somewhere that is not the laptop. The marks exist in one place until you do.
Then carry them to the website as described next — but back up first, because
a backup is the only thing that survives the laptop being dropped.

## Carrying the results back

The examination leaves the papers, the marking and anything you typed into
the gradebook on the laptop. **Administration ▸ Results transfer** moves them
into the website.

It is not a restore. A restore replaces the whole database and destroys
whatever was entered on the website in the meantime; this moves only what the
examination produced, and merges it.

### Setting it up (once, on both machines)

1. On the **website**, open the page, press **Generate a token**, copy it, and
   leave the address blank. A copy with no address is the one that receives.
2. On the **laptop**, open the same page, paste the website's address
   (`https://bertoy.pythonanywhere.com`) and the **same token**, and save.
3. Tick **Send automatically when the laptop is online** if you want it to go
   by itself.

The token can write marks into your gradebook, so treat it as a password.

### On the day

If the laptop has no internet in the room — which is the point of the
room's own network — nothing is sent during the examination. Afterwards,
either rejoin a wifi with internet and the laptop sends within a few minutes,
or press **Send to the main portal** yourself. If the laptop will never have
internet, press **Download as a file**, carry it on a USB stick, and use
**Merge a results file** on the website.

Sending twice does the same as sending once, so retrying after a dropped
connection is safe.

### What it will and will not do

The laptop is treated as authoritative for what happened during the
examination, and for nothing else. **Nothing is ever deleted.**

* A student already on the website **keeps their name and password**. Only a
  student who is not there at all is created — the one who enrolled late in
  the room.
* An attempt **already submitted on the website is left alone** and reported,
  because you may have marked an essay or adjusted a score there since. An
  attempt still in progress there is replaced by the finished one.
* A gradebook mark is written **only if it is newer** than the one on the
  website, or if there is none.
* **Final grades are never sent.** Finalising a subject is deliberate and
  belongs where the records are kept.
* The subject and the examination **must already exist on both sides** with
  the same code, term and title, and the paper must have the same number of
  questions. Anything that cannot be matched is reported rather than guessed
  at — read the notes on the page after a transfer.

The two databases number their rows independently, so nothing is matched on
an id: a student by student number, a subject by code and term, an exam by
its title, an answer by the question's position on the paper.

### What can still go wrong

* **A phone cannot open the address.** Nearly always the firewall (step 5) or
  the phone being on a different wifi. Check the laptop can open the address
  itself first — if it can and a phone cannot, it is the firewall.
* **The laptop sleeps.** Set it never to sleep on mains power for the day.
* **One student hotspots for the row.** The network rule cannot see this,
  because those phones still arrive on a wifi address. Only invigilation
  catches it.
* **The laptop's address changes** between setting it up and the examination,
  if the router hands out a different one. Check the address on the board
  matches what the script printed that morning.

## If one part of the portal breaks after an upgrade

An upgrade adds columns to the database as the app starts. That can fail
without stopping the app — most often because the disk is full or another
worker held the database open at that moment — and the result is confusing:
every page that reads the affected table fails with "Something went wrong"
while the rest of the portal carries on as though nothing is wrong.

The portal now checks for this itself. Administration ▸ System check names
any column the app expects and the database does not have, and offers
**Repair the database now**, which runs the migration again without a
restart. The error page says the same thing rather than sending you looking.
If the repair cannot finish either, the disk is the first thing to check: a
full disk makes every write fail, including the one that adds the column.

The same page keeps the last fifty unhandled errors with their tracebacks,
under **Recent errors** — which is what the error page means when it says an
administrator can see the details.

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
