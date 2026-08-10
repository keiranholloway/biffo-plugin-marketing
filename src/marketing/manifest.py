"""Path to this plugin's manifest file, shared by plugin.py and tests.

Mirrors the RBAC reference plugin's `services/rbac/src/rbac/manifest.py`
(PR #76) — same rationale: `plugin.py` and the tests both need a reliable
absolute path to `biffo.plugin.json` regardless of the current working
directory the process was started from.
"""

from __future__ import annotations

from pathlib import Path


# src/marketing/manifest.py -> src/marketing -> src -> repo root
# (plugin root, where biffo.plugin.json lives alongside pyproject.toml).
def _resolve_manifest_path() -> Path:
    # Walk up from this module to find biffo.plugin.json, so it resolves in both
    # the repo layout (src/<pkg>/manifest.py, manifest at the plugin root) and the
    # deployed Lambda (handler <pkg>.main.handler unzips <pkg>/ at the task root,
    # manifest bundled at /var/task/biffo.plugin.json). A fixed parents[2]
    # resolved to /var in the Lambda and raised FileNotFoundError on first invoke.
    here = Path(__file__).resolve()
    for base in here.parents:
        candidate = base / "biffo.plugin.json"
        if candidate.is_file():
            return candidate
    return here.parent.parent / "biffo.plugin.json"


MANIFEST_PATH = _resolve_manifest_path()
