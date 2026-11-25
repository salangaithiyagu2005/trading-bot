from __future__ import annotations

import getpass
import os
import subprocess
import sys
import sysconfig
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PROCESS_NAMES = ("nsebot-api", "nsebot-engine")


def _entrypoint(name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    executable = Path(sysconfig.get_path("scripts")) / f"{name}{suffix}"
    if not executable.is_file():
        raise RuntimeError(
            f"Could not find {executable}. Install the project with "
            "`python -m pip install -e .` and run nsebot from that environment."
        )
    return executable


def _configure_environment() -> None:
    os.environ.setdefault("LIVE_TRADING", "false")
    os.environ.setdefault("DATA_DIR", str(ROOT / "var"))
    os.environ.setdefault("REPO_DATA_DIR", str(ROOT / "data"))

    if not os.environ.get("UI_PASSWORD"):
        if not sys.stdin.isatty():
            raise RuntimeError(
                "Set UI_PASSWORD or run nsebot in an interactive terminal to enter it."
            )
        os.environ["UI_PASSWORD"] = getpass.getpass("Local UI password: ")
        if not os.environ["UI_PASSWORD"]:
            raise RuntimeError("UI_PASSWORD must not be empty.")


def _stop_processes(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()

    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main() -> None:
    _configure_environment()
    processes: list[subprocess.Popen] = []
    try:
        for name in PROCESS_NAMES:
            executable = _entrypoint(name)
            processes.append(subprocess.Popen([str(executable)], cwd=ROOT))

        print("API and engine are running. Press Ctrl+C to stop both.")
        while True:
            for name, process in zip(PROCESS_NAMES, processes):
                return_code = process.poll()
                if return_code is not None:
                    raise SystemExit(return_code)
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("\nStopping API and engine...")
    finally:
        _stop_processes(processes)
