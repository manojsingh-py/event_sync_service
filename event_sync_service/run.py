"""Single-command launcher: `python run.py`

* creates ./.venv on first run and installs requirements.txt (only when it changes),
* then starts the API + frontend on http://127.0.0.1:8000.

Uses only the standard library so it works on a clean Python 3.10+ install.
Set EVENT_SYNC_LOG_LEVEL=DEBUG for detailed processing logs.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
MARKER = VENV_DIR / ".requirements.sha256"


def say(message: str) -> None:
    print(f"[run] {message}", flush=True)


def venv_python() -> Path:
    return VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def running_in_project_venv() -> bool:
    return Path(sys.prefix).resolve() == VENV_DIR.resolve()


def bootstrap() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit(f"Python 3.10+ is required (found {sys.version.split()[0]}).")
    if not REQUIREMENTS.exists():
        raise SystemExit(f"Missing {REQUIREMENTS}")

    if not venv_python().exists():
        say(f"creating virtual environment in {VENV_DIR}")
        try:
            venv.create(VENV_DIR, with_pip=True)
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"Could not create the virtual environment: {exc}\n"
                             f"Try deleting {VENV_DIR} and running again.") from exc

    digest = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    if MARKER.exists() and MARKER.read_text().strip() == digest:
        return
    say("installing dependencies")
    try:
        subprocess.check_call([str(venv_python()), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", str(REQUIREMENTS)])
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Dependency installation failed (exit {exc.returncode}). "
                         "Check your network/proxy settings and run again.") from exc
    MARKER.write_text(digest)


def serve() -> None:
    sys.path.insert(0, str(ROOT))
    try:
        import uvicorn

        from event_sync import config
    except ImportError as exc:
        raise SystemExit(f"Missing dependency ({exc}). Delete {MARKER} and run `python run.py` again.") from exc

    say(f"Event Sync Service on http://{config.HOST}:{config.PORT}  (API docs at /docs)")
    try:
        uvicorn.run("event_sync.api:app", host=config.HOST, port=config.PORT, log_level="info")
    except OSError as exc:  # typically "address already in use"
        raise SystemExit(f"Could not start the server on {config.HOST}:{config.PORT}: {exc}. "
                         "Set PORT to use a different port.") from exc


if __name__ == "__main__":
    try:
        if running_in_project_venv() or os.getenv("EVENT_SYNC_NO_BOOTSTRAP") == "1":
            serve()
        else:
            bootstrap()
            # Re-launch inside the venv; forward the exit code.
            sys.exit(subprocess.call([str(venv_python()), str(Path(__file__).resolve()), *sys.argv[1:]], cwd=ROOT))
    except KeyboardInterrupt:
        say("stopped")
