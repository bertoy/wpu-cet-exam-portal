"""PythonAnywhere WSGI configuration — a sample to copy, not to deploy.

On PythonAnywhere, open the Web tab and click the link to your WSGI
configuration file (something like
/var/www/yourname_pythonanywhere_com_wsgi.py).  Delete everything in it and
paste the contents of this file, changing YOURNAME to your own username.

Two things matter here, and both were the cause of real trouble before:

1.  DATA_DIR points outside the code folder.  Uploading new code then cannot
    touch the database, the figures or the submissions.

2.  There is NO static-files mapping for the app's own CSS and JavaScript, and
    none should be added.  The portal serves those itself, at a URL containing
    a hash of the file's contents, which is what makes a stale stylesheet
    impossible.  A /static/ mapping on the Web tab would put PythonAnywhere's
    own cache in front of them and bring the old problem back -- so if one is
    already configured for this app, remove it.
"""
import os
import sys

# ---- 1. where the code is -------------------------------------------------
PROJECT = "/home/YOURNAME/mechexam"
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

# ---- 2. settings ----------------------------------------------------------
# Keep the data outside the code folder so redeploying cannot erase it.
os.environ.setdefault("DATA_DIR", "/home/YOURNAME/mechexam-data")

# Change this to a long random string of your own, and keep it the same --
# changing it signs everybody out.
os.environ.setdefault("SECRET_KEY", "put-a-long-random-string-here")

os.environ.setdefault("SCHOOL_NAME", "Western Philippines University")
os.environ.setdefault("COLLEGE_NAME", "College of Engineering and Technology")
os.environ.setdefault("DEPARTMENT_NAME", "Department of Mechanical Engineering")
os.environ.setdefault("CAMPUS_NAME", "Puerto Princesa Campus, Palawan")

# Only used the very first time, when no administrator exists yet.
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "change-me-on-first-sign-in")

# Google Drive is optional. Submissions are always stored on the server, so
# leaving this out costs nothing but the automatic copy into Drive.
# os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", '{"type":"service_account", ...}')

# ---- 3. the application ---------------------------------------------------
from app import create_app          # noqa: E402

application = create_app()
