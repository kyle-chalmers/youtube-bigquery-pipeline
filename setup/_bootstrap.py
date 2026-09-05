"""Make cloud_function/ importable from setup/ scripts.

Cloud Functions deploys only cloud_function/ as source, so that directory is the single
home for shared code. Scripts in setup/ import this module first, then import from
cloud_function/ by bare module name exactly as main.py does.

    import _bootstrap  # noqa: F401
    # isort: split   (the bootstrap must run before any cloud_function import)
    from oauth_credentials import load_oauth_credentials

Also home to resolve_project(), so the setup scripts share one way of finding the GCP
project instead of three slightly different copies.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLOUD_FUNCTION_DIR = REPO_ROOT / "cloud_function"

if str(CLOUD_FUNCTION_DIR) not in sys.path:
    sys.path.insert(0, str(CLOUD_FUNCTION_DIR))


def resolve_project() -> str:
    """GCP_PROJECT, else the active gcloud project, else a clear SystemExit."""
    project = os.environ.get("GCP_PROJECT")
    if project:
        return project
    try:
        out = subprocess.run(
            ["gcloud", "config", "get-value", "project"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        project = out.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        project = ""
    if not project or project == "(unset)":
        raise SystemExit(
            "GCP_PROJECT is not set and gcloud has no active project. "
            "Run `gcloud config set project <id>` or export GCP_PROJECT."
        )
    return project
