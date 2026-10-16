"""Run the portal on this computer.

    python run.py

Then open http://127.0.0.1:5000 and sign in as admin / admin123 (you will be
asked to set a real password straight away).

Use a different port with:   python run.py 8000
Reach it from a phone on the same Wi-Fi with:   HOST=0.0.0.0 python run.py
"""
from __future__ import annotations

import os
import sys

from app import create_app

app = create_app()

if __name__ == "__main__":
    port = 5000
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"'{sys.argv[1]}' is not a port number; using 5000.")

    host = os.environ.get("HOST", "127.0.0.1")
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"

    print()
    print(f"  {app.config['APP_NAME']}")
    print(f"  Data folder: {app.config['DATA_DIR']}")
    print(f"  Open:        http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}")
    if host == "0.0.0.0":
        print("  On the same Wi-Fi, use this computer's address and the port above.")
    print("  Stop with Ctrl+C")
    print()

    app.run(host=host, port=port, debug=debug, use_reloader=debug)
