"""The manifest says what Core will actually build, and nothing validates it.

`core_capabilities` is read by NO validator anywhere in the estate — not the
SDK's `load_manifest`, not the CLI's zod schema, despite ADR-0017 saying a
user-facing plugin "must" declare it. The CLI schema is also not `.strict()`, so
it silently drops keys it does not know: `admin_ingress`, `core_capabilities`,
`dependencies`. Both existing plugins police their own manifest for exactly this
reason, and this is that test.

**Asserts against the RAW JSON, not the parsed model.** The SDK model ignores
fields it does not know about, so validating through it would pass on a
manifest missing every one of these keys.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from marketing import ingress

_MANIFEST = Path(__file__).resolve().parents[1] / "biffo.plugin.json"

#: Column types Core actually resolves into SQLAlchemy. There is NO JSON type —
#: and declaring one is worse than unsupported: `_TYPE_MAP.get(base) or String`
#: silently gives the in-process model a String while the migration generator
#: splices the declared type into Alembic, so model and DDL diverge with nothing
#: red. CI would not catch it; only `biffo plugin install`'s zod regex would.
_ALLOWED_TYPES = ("String", "Integer", "Text", "Boolean", "Float", "DateTime")

#: Injected by Core on every plugin table. Redeclaring any of them raises.
_AUTO_COLUMNS = {"id", "tenant_id", "created_at", "updated_at"}


def _raw() -> dict:
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


def test_the_manifest_is_where_this_test_thinks_it_is() -> None:
    """Guard the guard: a moved manifest makes every assertion below vacuous."""
    assert _MANIFEST.is_file(), f"No manifest at {_MANIFEST}"


def test_core_capabilities_are_declared() -> None:
    """Nothing else checks this. ADR-0017 requires it; no validator enforces it."""
    caps = _raw().get("core_capabilities")
    assert caps, "core_capabilities is absent — no validator will tell you"
    for needed in ("agent-run-request", "owner-scoped-tables", "object-storage"):
        assert needed in caps, f"{needed} is used by this plugin but not declared"


def test_the_admin_surface_is_declared_and_points_at_a_real_app() -> None:
    """The CLI zod schema drops `admin_ingress` silently, so nothing else looks."""
    ingress = _raw().get("admin_ingress")
    assert ingress, "admin_ingress is absent — the admin surface would never mount"
    assert ingress["required_group"] == "admin"
    assert ingress["app"] == "marketing.admin_app:app"


def test_the_user_surface_is_declared_and_points_at_a_real_app() -> None:
    """Surface B (franchise units) — mirrors
    `test_the_admin_surface_is_declared_and_points_at_a_real_app` exactly."""
    declared = _raw().get("user_ingress")
    assert declared, "user_ingress is absent — the unit surface would never mount"
    assert declared["app"] == "marketing.user_app:app"


def test_the_user_surface_gates_on_the_one_declared_group() -> None:
    """The manifest's copy and the code's copy must agree.

    The shared plugin host reads the gate from the manifest; `user_app.py`
    builds its FastAPI dependency from `ingress.user_ingress_group()`. Those
    are two different readers of one decision, and #46 is what happens when
    they are two independent literals instead — the same "adopted at some call
    sites, not all" shape as this repo's #119.

    So `ingress.py` holds the name and this reconciles the manifest against it.
    Once keiranholloway/biffo-template#1517 lands, the instance supplies the
    value for the `user_ingress_group` setting declared below and this
    assertion is what tells whoever makes that change that the manifest key has
    to move too.
    """
    assert _raw()["user_ingress"]["required_group"] == ingress.USER_INGRESS_GROUP


def test_user_surface_readable_tables_stay_read_only() -> None:
    """`marketing_campaign`/`marketing_artefact`/`marketing_asset`/
    `marketing_link` open `list`/`read` to any authenticated caller (`[]`) so
    `user_app.py` can read them — but `create`/`update`/`delete` must stay
    `admin`-only on every one of them, and `marketing_click` must stay
    admin-only on every operation (see `user_app.py`'s module docstring for
    why). A regression here would let a user-ingress-group caller write through
    the generic CRUD path directly, which no route in `user_app.py` needs or
    should have."""
    tables = {t["name"]: t for t in _raw()["tables"]}
    for name in ("marketing_campaign", "marketing_artefact", "marketing_asset", "marketing_link"):
        perms = tables[name]["permissions"]
        assert perms["list"]["required_role"] == [], f"{name}.list should be open"
        assert perms["read"]["required_role"] == [], f"{name}.read should be open"
        for op in ("create", "update", "delete"):
            assert perms[op]["required_role"] == ["admin"], f"{name}.{op} must stay admin-only"

    click_perms = tables["marketing_click"]["permissions"]
    for op in ("list", "read", "create", "update", "delete"):
        assert click_perms[op]["required_role"] == ["admin"], (
            f"marketing_click.{op} must stay admin-only"
        )


@pytest.mark.parametrize("table", _raw()["tables"], ids=lambda t: t["name"])
def test_every_column_uses_a_type_core_resolves(table: dict) -> None:
    for column in table["columns"]:
        base = column["type"].split("(")[0]
        assert base in _ALLOWED_TYPES, (
            f"{table['name']}.{column['name']} declares {column['type']!r}. "
            f"Core resolves only {_ALLOWED_TYPES}; anything else silently becomes "
            f"String in the model while the migration gets the declared type."
        )


@pytest.mark.parametrize("table", _raw()["tables"], ids=lambda t: t["name"])
def test_no_table_redeclares_an_auto_column(table: dict) -> None:
    names = {c["name"] for c in table["columns"]}
    assert not (names & _AUTO_COLUMNS), (
        f"{table['name']} redeclares {names & _AUTO_COLUMNS}, which Core injects. "
        f"Redeclaring raises at table build."
    )


@pytest.mark.parametrize("table", _raw()["tables"], ids=lambda t: t["name"])
def test_no_table_declares_a_foreign_key(table: dict) -> None:
    """Plugin tables cannot have FKs. Relations are plain keys, validated in the
    service layer — asserted so nobody adds one and wonders why install fails."""
    for column in table["columns"]:
        assert "foreign_key" not in column, f"{table['name']}.{column['name']} declares a FK"


@pytest.mark.parametrize("route", _raw()["api_routes"], ids=lambda r: f"{r['method']} {r['path']}")
def test_every_route_names_a_table_this_manifest_declares(route: dict) -> None:
    tables = {t["name"] for t in _raw()["tables"]}
    assert route["table"] in tables, f"{route['path']} names unknown table {route['table']!r}"


@pytest.mark.parametrize("route", _raw()["api_routes"], ids=lambda r: f"{r['method']} {r['path']}")
def test_every_route_path_is_derived_from_its_table_name(route: dict) -> None:
    """The collection segment must be the table's own name, pluralised.

    This exists because ``marketing_asset`` shipped as ``/asets`` — a typo in a
    hand-written manifest, in a file with a test suite already asserting types,
    auto-columns, foreign keys and verb/operation pairing. **Nothing asserted
    the path**, so a wrong one was invisible: the route still named a real
    table, still paired its verb correctly, and still passed every check here.

    A path is the one part of a manifest a human types twice and a machine never
    reconciles — Core mounts whatever is written, so a typo is not an error, it
    is just a differently-named endpoint that the UI then has to match. Deriving
    it removes the second copy.
    """
    collection = route["path"].split("/")[1]
    expected = route["table"].removeprefix("marketing_") + "s"
    assert collection == expected, (
        f"{route['method']} {route['path']} serves {route['table']!r}, "
        f"so its collection segment should be /{expected}, not /{collection}"
    )


@pytest.mark.parametrize("route", _raw()["api_routes"], ids=lambda r: f"{r['method']} {r['path']}")
def test_route_verbs_match_their_operation(route: dict) -> None:
    """Core pairs these strictly; a mismatch is a config error at install."""
    expected = {"list": "GET", "read": "GET", "create": "POST", "delete": "DELETE"}
    op = route["operation"]
    if op == "update":
        assert route["method"] in ("PUT", "PATCH")
    else:
        assert route["method"] == expected[op]
    # read/update/delete address one row and must carry {id}; list/create must not.
    if op in ("read", "update", "delete"):
        assert "{id}" in route["path"], f"{op} route {route['path']} has no {{id}}"
    else:
        assert "{id}" not in route["path"], f"{op} route {route['path']} should not take an id"


def test_clicks_are_append_only_from_the_admin_surface() -> None:
    """No create/update/delete routes on marketing_click.

    Clicks are written by Core's public redirect, never by an admin. A writable
    click route would let the number this whole feature exists to measure be
    edited by hand.
    """
    click_ops = {r["operation"] for r in _raw()["api_routes"] if r["table"] == "marketing_click"}
    assert click_ops <= {"list", "read"}, f"marketing_click exposes {click_ops}"
