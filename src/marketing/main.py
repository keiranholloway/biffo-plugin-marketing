"""Example plugin Lambda entrypoint.

Mirrors the RBAC reference plugin's `services/rbac/src/rbac/main.py` and
`services/_template/src/service/main.py`'s documented event shape and rules
(ADR-0002): never import a DB client, talk to the Core API only via
`BiffoAPIClient`, react to state changes via EventBridge.

This handler's only responsibility is dispatching EventBridge events the
plugin subscribes to (see `plugin.py`) through `MarketingPlugin`'s
`EventSubscriber`. It is not wired to a real Lambda in this template — that
wiring (an EventBridge rule targeting this function) belongs to your
plugin's own `terraform/` module, added once ADR-0003's conditional plugin
Terraform inclusion (issue #25) lands in a generated project.
"""

from __future__ import annotations

import asyncio

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.utilities.typing import LambdaContext
from biffo_plugin_sdk import create_event_handler

from .plugin import MarketingPlugin

logger = Logger()
tracer = Tracer()

# Reuse one event loop across warm invocations. asyncio.run() closes the loop
# each call, but the plugin's reused httpx client pools connections bound to
# the first loop -> "RuntimeError: Event loop is closed" on the next
# invocation. Mirrors services/api/src/api/main.py.
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)

_plugin: MarketingPlugin | None = None


def _get_plugin() -> MarketingPlugin:
    """Lazily construct the plugin singleton, reused across warm invocations
    — a misconfigured environment (e.g. missing BIFFO_CORE_API_URL) surfaces
    at first use rather than being buried inside every event."""
    global _plugin
    if _plugin is None:
        _plugin = MarketingPlugin()
    return _plugin


@logger.inject_lambda_context
@tracer.capture_lambda_handler
def handler(event: dict, context: LambdaContext) -> dict:
    logger.info("Received event", extra={"event": event})

    global _loop
    if _loop.is_closed():
        _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

    biffo_event = create_event_handler(event)
    plugin = _get_plugin()
    _loop.run_until_complete(plugin.events.dispatch(biffo_event))

    return {"statusCode": 200}
