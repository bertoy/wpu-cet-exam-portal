"""WSGI entry point.

Used by gunicorn on Railway (`gunicorn wsgi:application`) and by
PythonAnywhere's WSGI configuration file.
"""
from app import create_app

application = create_app()
app = application          # gunicorn and some hosts look for one or the other

if __name__ == "__main__":
    application.run()
