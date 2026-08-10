"""MarketingPlugin — this plugin's BiffoPluginBase implementation.

Reacts to `biffo.core/UserCreated` by logging the new user, and carries an
idempotent seed for its baseline row. Replace both with real logic for your
own plugin — this exists to show a working, correctly-wired skeleton:
constructor -> manifest loading -> `self.api` -> `@self.subscribe`, exercised
end to end by the tests in `tests/test_marketing.py`.

**`on_install()` / `on_uninstall()` are not invoked.** They are required by
`BiffoPluginBase` and nothing calls them, so they are no-ops here and should
be no-ops in your plugin (biffo-template#709). An earlier version of this
skeleton seeded through `on_install()` — copying that gives you a seed that
silently never runs. `seed_default_widget()` below shows where seeding
actually goes.

Modelled directly on the RBAC reference plugin's
`services/rbac/src/rbac/plugin.py` (PR #76) in the biffo-template monorepo —
see that file (and its README) for a more elaborate example with multiple
tables, an event-driven side effect, and an in-process (non-CRUD) helper.
"""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Logger
from biffo_plugin_sdk import BiffoAPIClient, BiffoEvent, BiffoPluginBase, load_manifest

from .manifest import MANIFEST_PATH

logger = Logger()

_PLUGIN_BASE_PATH = "/api/v1/plugins/marketing"

# Seeded by seed_default_widget(). Real plugins would seed whatever baseline
# data their feature needs.
_DEFAULT_WIDGET: dict[str, Any] = {
    "name": "starter-widget",
    "description": "Created by this plugin's own seed. Safe to delete.",
    "is_active": True,
}


class MarketingPlugin(BiffoPluginBase):
    """The example plugin shipped with the plugin repository template."""

    def __init__(self, api: BiffoAPIClient | None = None) -> None:
        manifest = load_manifest(MANIFEST_PATH)
        super().__init__(manifest, api=api)

        @self.subscribe("UserCreated")
        async def _on_user_created(event: BiffoEvent) -> None:
            await self._log_user_created(event)

    def on_install(self) -> None:
        """No-op. **Not invoked** — nothing calls it (biffo-template#709).

        `BiffoPluginBase` requires it, so it is defined; `biffo plugin
        install` does not run it, and neither does anything else. Put no
        logic here. Seeding goes in `seed_default_widget()` below.
        """
        return None

    async def seed_default_widget(self) -> None:
        """Create this plugin's baseline row, idempotently.

        Nothing in this skeleton calls this — deliberately. **Where you call
        it from depends on what kind of plugin you are building**, and
        neither answer is `on_install()`:

        - **A plugin with an `api_ingress` ASGI app** (ADR-0021) seeds from
          its app's *lifespan*, which the shared plugin host drives on the
          first invocation of each process::

              @asynccontextmanager
              async def lifespan(app: FastAPI) -> AsyncIterator[None]:
                  await plugin.seed_default_widget()
                  yield

              app = FastAPI(lifespan=lifespan)

          The host has to run that handshake itself, because Starlette's
          `Mount` never delivers the lifespan scope to a mounted app — until
          biffo-template#948 a plugin's own `@app.on_event("startup")` was as
          dead as `on_install()`. It runs on every cold start, so the seed
          must be idempotent (below), and Core's own seed endpoint
          `POST /api/v1/internal/plugins/me/config/seed` was itself not
          idempotent until #1000. Verify your seed; do not assume it.

        - **An event-only plugin like this one** declares no `api_ingress`,
          so it has no startup at all. Its baseline rows belong in a SQL
          module in the *instance's* `db/imports/<name>/`, applied by
          `biffo data apply` on every deploy — the mechanism the first-party
          plugins use.

        Idempotency: generic CRUD (issue #19) has no upsert — every POST
        creates a new row — so this lists existing widgets first and only
        creates the default one if it's missing by name, making a repeated
        cold start a no-op instead of accumulating duplicates.
        """
        existing = await self.api.get(f"{_PLUGIN_BASE_PATH}/widgets")
        existing_names = {row.get("name") for row in existing}
        if _DEFAULT_WIDGET["name"] not in existing_names:
            await self.api.post(f"{_PLUGIN_BASE_PATH}/widgets", json=dict(_DEFAULT_WIDGET))

    def on_uninstall(self) -> None:
        """No-op. **Not invoked** — nothing calls it (biffo-template#709).

        There is no teardown moment for it to belong to either: `biffo plugin
        uninstall` removes the plugin's code and Terraform and deliberately
        leaves its tables in place (ADR-0003 section 9). Cleanup beyond that
        is a hand-written Alembic migration, not a hook.
        """
        return None

    async def _log_user_created(self, event: BiffoEvent) -> None:
        """Example event handler. Replace with real logic, or remove the
        `@self.subscribe` registration in `__init__` if your plugin doesn't
        need to react to this event."""
        logger.info(
            "New user created",
            extra={"tenant_id": event.tenant_id, "payload": event.payload},
        )
