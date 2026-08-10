"""Load an operator script by file path, without putting it on sys.path.

Copied from ``biffo-plugin-idea-scout``'s ``tests/_scripts.py`` verbatim
(module-name prefix aside) — both plugins are vendored into `biffo-platform` as
`services/<name>/`, and a `scripts/__init__.py` + root `conftest.py` approach
does not survive a second plugin sharing the literal package name `scripts`
(keiranholloway/biffo-template#686). Loading by path sidesteps the package
namespace entirely: nothing named `scripts` is imported, so there is nothing to
collide. The scripts stay where an operator expects them
(`python scripts/seed_x.py`).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def load_script(name: str) -> ModuleType:
    """Import ``scripts/<name>.py`` under a plugin-scoped module name."""
    path = _SCRIPTS / f"{name}.py"
    # Namespaced so it cannot collide with another plugin's script of the same
    # name in the aggregate run either.
    module_name = f"marketing_scripts.{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover — a missing file
        raise ImportError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
