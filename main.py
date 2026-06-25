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
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
REQUIRED_API_ROUTES = {
    "/copilot/create",
    "/copilot/modify",
    "/copilot/fix",
    "/copilot/explain",
    "/copilot/modify/operations",
    "/copilot/conversations",
    "/copilot/conversations/{conversation_id}/messages",
    "/copilot/plans",
    "/copilot/plans/{session_id}/answers",
    "/copilot/plans/stream",
    "/copilot/plans/{session_id}/answers/stream",
    "/workflows",
    "/workflows/{workflow_id}",
    "/workflows/diff",
    "/workflows/validate",
}


def add_project_paths() -> None:
    for path in (PACKAGE_DIR, SRC_DIR):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)


def relaunch_in_project_venv() -> None:
    """Make `python main.py` use the project's installed dependencies."""
    if not VENV_PYTHON.exists():
        return
    if Path(sys.executable).resolve() == VENV_PYTHON.resolve():
        return
    raise SystemExit(
        subprocess.call(
            [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
            cwd=str(ROOT),
        )
    )


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


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def verify_backend_features() -> tuple[str, int]:
    """Fail fast when the launcher resolves an incomplete or stale API module."""
    from copilot_api.main import app

    available_routes = {
        route.path
        for route in app.routes
        if getattr(route, "path", None)
    }
    missing_routes = sorted(REQUIRED_API_ROUTES - available_routes)
    if missing_routes:
        raise RuntimeError(
            "The loaded backend is missing required routes: "
            + ", ".join(missing_routes)
        )
    return app.version, len(available_routes)


def main() -> None:
    relaunch_in_project_venv()
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

    version, route_count = verify_backend_features()
    if "--check" in sys.argv:
        print(
            f"Backend verification passed: API v{version}, "
            f"{route_count} routes, all required Copilot features loaded."
        )
        return

    import uvicorn

    host = os.getenv("BACKEND_HOST", "127.0.0.1")
    port = int(os.getenv("BACKEND_PORT", "8000"))
    reload_enabled = env_flag("BACKEND_RELOAD", default=True)
    if "--reload" in sys.argv:
        reload_enabled = True
    if "--no-reload" in sys.argv:
        reload_enabled = False

    print(f"App: http://{host}:{port}")
    print(f"Backend: http://{host}:{port}")
    print(f"API docs: http://{host}:{port}/docs")
    print(f"Loaded: API v{version}, {route_count} routes")
    print(f"Auto-reload: {'enabled' if reload_enabled else 'disabled'}")
    uvicorn.run(
        "copilot_api.main:app",
        host=host,
        port=port,
        app_dir=str(SRC_DIR),
        reload=reload_enabled,
        reload_dirs=[str(SRC_DIR)] if reload_enabled else None,
    )


if __name__ == "__main__":
    main()
