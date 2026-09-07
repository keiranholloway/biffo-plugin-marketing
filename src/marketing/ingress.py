"""The one place this plugin names the platform group its user surface gates on.

Issue #46: ``founder`` is **biffo-platform's** vocabulary. This plugin is meant
to install on any Biffo platform, and a platform that has no ``founder`` group
(tabsii-platform has ``admin``/``platform_admin``/``editor``/``viewer`` plus a
franchise RBAC model of its own) gets a Surface B that 403s for everybody. The
*concept* — "a unit operator marketing its own services" — is portable; the
*group name* is an instance decision the plugin cannot know.

The real fix is keiranholloway/biffo-template#1517: a plugin declares a need,
the instance supplies the value at install. **That has landed** (PR#1946) — but
host-side, not as anything this module reads: an instance overrides the group
via the env var ``BIFFO_PLUGIN_MARKETING_USER_INGRESS_REQUIRED_GROUP``, which
``discover.py`` resolves before the shared plugin host's own ``group_gate``
ever authorizes a caller (``plugin_host/mount.py``) — entirely upstream of
this plugin's own code. So the *other* half of #46 turned out to matter more:
this plugin's user surface (``user_app.py``) used to ALSO run its own,
plugin-owned ``require_group`` check on top of the host's, built once at
import time from the bare literal below. Once an instance overrode the
host-side group, the host correctly admitted the caller and this plugin's own
stale check 403'd them anyway. That second gate is now gone — ``user_app.py``
relies solely on the host's ``group_gate`` and no longer calls
``user_ingress_group()`` at all. What remains here is the manifest's
*declared default* (``biffo.plugin.json``'s ``user_ingress.required_group``),
kept in exactly one place in this plugin's Python source so a future
hardcoded copy has one home to be caught against
(``tests/test_marketing_ingress_group_guard.py``) and so the manifest's
literal is reconciled against something rather than drifting on its own
(``tests/test_marketing_manifest.py``).

## Why ``admin`` is a bare literal and this is not

``admin_ingress.required_group`` stays the literal ``"admin"``, here and in
``biffo.plugin.json``, and it is deliberately **not** routed through this
module. The two are not the same kind of name:

- ``admin`` is a **universal Biffo role**. Every Biffo platform has one by
  construction — it is what ADR-0004's per-table ``required_role: ["admin"]``
  already assumes throughout this manifest, on every table, on every write.
  A platform without it could not run any plugin's generated CRUD at all.
- ``founder`` is **one platform's product vocabulary**. It exists in
  biffo-platform and nowhere else, which is the entire defect in #46.

Parameterising ``admin`` too would look symmetrical and be a straight loss: it
would mint an instance setting that every instance must set to the same value,
and an unset required setting fails the install (#1517 §4). Instance
configuration nobody varies is configuration nobody sets correctly. So the
asymmetry is the point, and it is written down here — and beside
``require_admin`` in ``admin_app.py`` and ``image_routes.py`` — rather than
left to look like an oversight somebody helpfully "fixes" later.

## Nothing in this plugin's source reads the group at all, deliberately

There is no accessor here to call — ``USER_INGRESS_GROUP`` below is data for
the two tests that reconcile it, not a value any request path consults.
Before #46's fix this module also exported a ``user_ingress_group()``
function and a reserved ``USER_INGRESS_GROUP_SETTING`` name, on the theory
that #1517 would give a plugin a settings API to call at request time. #1517
landed differently (PR#1946): the override is resolved **host-side**, in
``discover.py``, before this plugin's ASGI app is ever invoked — so there was
never going to be a call site here, and carrying an unused "reserved for
later" accessor was worse than removing it once that became clear. Nothing
else in this plugin's source names the group at all —
``tests/test_marketing_ingress_group_guard.py`` enumerates every violation and
fails on the first new one.

**The manifest deliberately declares no ``config`` block, and that is a
correction rather than an omission.** An earlier revision of this work declared
one, reasoning that ``PluginManifest`` was not ``extra="forbid"`` at the top
level so an unrecognised key would simply be ignored until #1517 gave it a
meaning. That was true when written and stopped being true a few hours later:
biffo-template#1561 made the manifest strict, and the shared plugin host does
not treat ``config`` as salvageable — an unknown top-level key means
``_load_manifest_tolerant`` returns ``None`` and **the whole plugin is skipped**,
taking the admin surface down with the user one.

Worse, nothing in this repo would have said so: ``uv.lock`` pins an older SDK,
so the authoritative ``load_manifest`` check here passes while the instance
refuses the same file. So the need is declared in prose, here and on #46, until
there is a schema that actually reads it.
"""

from __future__ import annotations

#: The group name this plugin's user surface gates on today, and the ONLY
#: occurrence of it in this plugin's Python source. ``biffo.plugin.json``'s
#: ``user_ingress.required_group`` carries the same string because the shared
#: plugin host reads the gate from the manifest, not from this module — that
#: copy is reconciled against this one by
#: ``tests/test_marketing_manifest.py::test_the_user_surface_gates_on_the_one_declared_group``,
#: so the two cannot drift the way #119's class does.
USER_INGRESS_GROUP = "founder"
