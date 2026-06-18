"""One-command backend launcher.

Run this file from the project root:

    python main.py

On the first run, missing Python packages are installed into a project-local
folder. No virtual environment activation is required.
"""

from __future__ import annotations

import importlib.util
import importlib
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
SRC_DIR = ROOT / "src"
PACKAGE_DIR = ROOT / ".python_packages"
REQUIREMENTS_FILE = ROOT / "requirements.txt"


def add_project_paths() -> None:
    for path in (PACKAGE_DIR, SRC_DIR):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)


def install_dependencies_if_needed() -> None:
    required_modules = ("fastapi", "httpx", "pytest", "uvicorn")
    if all(importlib.util.find_spec(module) is not None for module in required_modules):
        return

    print("First run: installing backend packages locally...")
    PACKAGE_DIR.mkdir(exist_ok=True)
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--target",
            str(PACKAGE_DIR),
            "-r",
            str(REQUIREMENTS_FILE),
        ]
    )
    importlib.invalidate_caches()


def load_env_file() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return

    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, value)


def main() -> None:
    add_project_paths()
    install_dependencies_if_needed()
    load_env_file()

    if "--test" in sys.argv:
        import pytest

        raise SystemExit(
            pytest.main(
                [
                    str(ROOT / "tests"),
                    "--basetemp",
                    str(ROOT / ".test_tmp"),
                    "-o",
                    f"cache_dir={ROOT / '.test_cache'}",
                ]
            )
        )

    import uvicorn

    host = os.getenv("BACKEND_HOST", "127.0.0.1")
    port = int(os.getenv("BACKEND_PORT", "8000"))
    print(f"Backend: http://{host}:{port}")
    print(f"API docs: http://{host}:{port}/docs")
    uvicorn.run("copilot_api.main:app", host=host, port=port, app_dir=str(SRC_DIR))


if __name__ == "__main__":
    main()
