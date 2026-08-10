"""Tests for MarketingPlugin: manifest loading, the seed, and the UserCreated
event subscription.

`on_install()` / `on_uninstall()` are asserted to be no-ops, not to seed:
they are **not invoked** by anything (biffo-template#709), so a test that
proved seeding through them would be proving something that never happens.

Modelled on the RBAC reference plugin's
`services/rbac/tests/test_rbac_plugin.py` (PR #76) in the biffo-template
monorepo.
"""

from __future__ import annotations

from biffo_plugin_sdk import BiffoEvent
from marketing_fakes import FakeCoreApi

from marketing.plugin import _DEFAULT_WIDGET, MarketingPlugin


def _make_plugin() -> tuple[MarketingPlugin, FakeCoreApi]:
    fake = FakeCoreApi()
    plugin = MarketingPlugin(api=fake.client())
    return plugin, fake


class TestManifestLoading:
    def test_manifest_name_and_version(self) -> None:
        plugin, _ = _make_plugin()
        assert plugin.manifest.name == "marketing"
        assert plugin.manifest.version == "0.1.0"

    def test_manifest_declares_the_widgets_table(self) -> None:
        plugin, _ = _make_plugin()
        table_names = {t.name for t in plugin.manifest.tables}
        assert table_names == {"marketing_widgets"}

    def test_register_returns_manifest_registration(self) -> None:
        plugin, _ = _make_plugin()
        registration = plugin.register()
        assert registration["name"] == "marketing"
        assert {t["name"] for t in registration["tables"]} == {"marketing_widgets"}
        assert {r["path"] for r in registration["api_routes"]} == {
            "/widgets",
            "/widgets/{id}",
        }


class TestSubscription:
    def test_subscribes_to_user_created(self) -> None:
        plugin, _ = _make_plugin()
        assert plugin.events.has_subscription("UserCreated")


class TestSeeding:
    async def test_seeds_default_widget(self) -> None:
        plugin, fake = _make_plugin()

        await plugin.seed_default_widget()

        names = {r["name"] for r in fake.tables["widgets"]}
        assert names == {_DEFAULT_WIDGET["name"]}

    async def test_is_idempotent(self) -> None:
        plugin, fake = _make_plugin()

        await plugin.seed_default_widget()
        await plugin.seed_default_widget()

        assert len(fake.tables["widgets"]) == 1

    async def test_default_widget_marked_active(self) -> None:
        plugin, fake = _make_plugin()

        await plugin.seed_default_widget()

        widget = fake.tables["widgets"][0]
        assert widget["is_active"] is True


class TestLifecycleHooksAreNoOps:
    """The ABC's hooks are **not invoked** by anything (biffo-template#709).

    So the contract worth asserting is that they do nothing — in particular
    that neither of them seeds. A plugin whose baseline data depends on one
    of these ships a seed that never runs.
    """

    def test_on_install_is_a_noop_and_seeds_nothing(self) -> None:
        plugin, fake = _make_plugin()

        assert plugin.on_install() is None
        assert fake.tables["widgets"] == []

    def test_on_uninstall_is_a_noop(self) -> None:
        plugin, _ = _make_plugin()
        assert plugin.on_uninstall() is None

    def test_on_upgrade_is_a_noop(self) -> None:
        plugin, fake = _make_plugin()
        assert plugin.on_upgrade("0.0.1") is None
        assert fake.tables["widgets"] == []


class TestUserCreatedLogsEvent:
    async def test_dispatch_does_not_raise(self) -> None:
        """The example handler only logs — this proves the subscription is
        wired end to end (event -> EventSubscriber -> handler) without
        raising, the same shape a real handler with side effects would be
        tested in (see RBAC's test_rbac_plugin.py for a handler that
        actually mutates state via self.api)."""
        plugin, _ = _make_plugin()

        event = BiffoEvent(
            detail_type="UserCreated",
            tenant_id="default",
            payload={"cognito_sub": "user-123", "email": "new@example.com"},
        )
        await plugin.events.dispatch(event)
