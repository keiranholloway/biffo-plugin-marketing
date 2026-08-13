"""The one place this plugin names the platform group its user surface gates on.

Issue #46: ``founder`` is **biffo-platform's** vocabulary. This plugin is meant
to install on any Biffo platform, and a platform that has no ``founder`` group
(tabsii-platform has ``admin``/``platform_admin``/``editor``/``viewer`` plus a
franchise RBAC model of its own) gets a Surface B that 403s for everybody. The
*concept* — "a unit operator marketing its own services" — is portable; the
*group name* is an instance decision the plugin cannot know.

The real fix is keiranholloway/biffo-template#1517: a plugin declares a need,
the instance supplies the value at install. That mechanism **does not exist
yet**, so this module does not pretend to read it. What it does is remove the
second copy, so that landing #1517 is one edit rather than a hunt.

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

## What lands when #1517 does

``user_ingress_group()`` is the seam. Its body is the single line that changes:
today it returns the declared literal; then it returns the value the instance
supplied. Nothing else in this plugin's source names the group at all —
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

#: The setting name this plugin will read once keiranholloway/biffo-template#1517
#: gives instances a way to supply one. **Reserved, not declared** — see the
#: module docstring for why the manifest carries no ``config`` block today. It
#: lives here so that whoever wires #1517 up has one string to move rather than
#: inventing a second, and so the name is reviewable now rather than chosen in
#: a hurry later.
USER_INGRESS_GROUP_SETTING = "user_ingress_group"


def user_ingress_group() -> str:
    """Which group may reach the user-facing surface on this installation.

    **This is the one line keiranholloway/biffo-template#1517 changes.** Today
    it returns the group this plugin declares in its manifest. Once an instance
    can supply a value for the ``user_ingress_group`` setting, this returns
    that instead, and ``USER_INGRESS_GROUP`` above stops being a value the
    plugin decides.

    A function rather than a bare constant *deliberately*: #1517 has not
    specified how a plugin reads a resolved setting, and whatever that turns
    out to be will be a call, not a module-level string. Call sites depending
    on a callable now means the accessor lands inside this body and nowhere
    else. See this module's docstring for what that assumption does and does
    not cover — if #1517's resolution turns out to be **per-request** rather
    than per-process, the change is larger than this body, because
    ``user_app.require_user_ingress`` is built once at import time.
    """
    return USER_INGRESS_GROUP
