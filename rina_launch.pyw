import atexit
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

project_dir = Path(__file__).resolve().parent
app_file = project_dir / "rina_web.py"
lock_file = project_dir / ".rina_launch.lock"
python_exe = sys.executable.replace("pythonw.exe", "python.exe")
port = "8501"

def remove_lock():
    if lock_file.exists():
        try:
            lock_file.unlink()
        except:
            pass

if lock_file.exists():
    webbrowser.open(f"http://127.0.0.1:{port}")
    sys.exit()

lock_file.write_text(str(os.getpid()), encoding="utf-8")
atexit.register(remove_lock)

subprocess.Popen(
    [python_exe, "-m", "streamlit", "run", str(app_file), "--server.port", port],
    cwd=str(project_dir),
    creationflags=subprocess.CREATE_NO_WINDOW
)

time.sleep(2)
webbrowser.open(f"http://127.0.0.1:{port}")