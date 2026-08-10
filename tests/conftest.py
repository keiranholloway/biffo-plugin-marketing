"""Make `marketing` and its test-local fakes module importable.

This template is a standalone repo (not part of the biffo-template
monorepo's uv workspace — see README's "Standalone repo" note), so
`marketing` is installed normally via the project's own
[build-system]/hatchling config when `uv sync` runs. This conftest only
needs to put `tests/` itself on sys.path so `tests/test_marketing.py`
can import its fakes as a plain sibling module (pytest's rootdir insertion
already covers this in most configurations, but the explicit path insert
keeps this working from any cwd, matching the RBAC reference plugin's
`services/rbac/tests/conftest.py` pattern in the biffo-template monorepo).

The fakes module is named after the plugin (`marketing_fakes.py`, which
scaffolding rewrites per plugin) rather than a bare `fakes.py`. Without
`__init__.py` here, pytest's prepend import mode imports test modules by bare
basename, so two vendored plugins both shipping `fakes.py` would fight over the
top-level module name and one would silently import the other's — which is
exactly what happened when a second plugin was installed (issue #688). Guarded
by `cli/src/lib/plugin-skeleton-second-occupant.test.ts`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
