"""WSGI entry point for hosts that import an application object directly
(PythonAnywhere, mod_wsgi, uWSGI) rather than running gunicorn.

On PythonAnywhere, point the Web tab's WSGI configuration file at this module:

    import sys
    path = '/home/<username>/twilio'
    if path not in sys.path:
        sys.path.insert(0, path)
    from wsgi import application          # noqa

Environment variables come from .env, which is read by app.py via python-dotenv.
Keep .env on the server only — it is gitignored and must never be committed.
"""

import os
import sys

# Make sure the project directory is importable no matter where the host
# starts the process from.
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from app import app as application  # noqa: E402

if __name__ == "__main__":
    application.run()
