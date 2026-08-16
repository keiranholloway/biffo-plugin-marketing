"""The check that reads the deployed ceiling instead of asserting it (#132).

Every other assertion about run budgets in this repo compares one of this
repo's beliefs against another of this repo's beliefs — `AGENT_TIMEOUT_SECONDS`
against `RUNTIME_TIMEOUT_CEILING_SECONDS`, the constant against the deployment
value quoted in its own docstring. Those are worth having and they are what
`test_marketing_agent_run_limits.py` does, but none of them can fail when the
*deployment* moves, because none of them has ever seen it.

`scripts/check_runtime_ceiling.py` is the one thing in this repo that reads the
real number. It cannot run in CI (it needs AWS credentials and the answer
differs per environment), so what CI can prove is that its comparison is right:
every case below hands `compare()` a Lambda configuration in a known state and
asserts the verdict, with no AWS involved.

**The case that matters most is `test_an_unset_ceiling_is_cannot_tell`.** A
drift check that reports "fine" when it could not actually find the number is a
fail-open, and this class's history is entirely made of green checks over
questions nobody asked (biffo-template#1363).
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from _scripts import load_script

from marketing.definitions import (
    AGENT_TIMEOUT_SECONDS,
    RUNTIME_TIMEOUT_CEILING_SECONDS,
    discover_definition_factories,
)

_check = load_script("check_runtime_ceiling")

IN_STEP = _check.IN_STEP
DRIFTED = _check.DRIFTED
CANNOT_TELL = _check.CANNOT_TELL

#: Bigger than any turn budget this plugin declares, so a test aimed at the
#: wall clock cannot be failed by an unrelated turns finding. Derived rather
#: than typed: a stage added tomorrow with a larger budget must not quietly
#: turn these into turns tests.
_GENEROUS_TURNS = (
    max(
        factory(model="some/model", instructions="do the thing")["max_turns"]
        for factory in discover_definition_factories()
    )
    + 1
)


def _configuration(
    *,
    seconds: str | None = str(int(RUNTIME_TIMEOUT_CEILING_SECONDS)),
    turns: str | None = str(_GENEROUS_TURNS),
    lambda_timeout: float | None = None,
) -> dict[str, Any]:
    """A `get_function_configuration` response in whatever state a test needs.

    Shaped exactly as boto3 returns it — the ceilings live under
    `Environment.Variables` as **strings**, which is the detail a test written
    against a tidied-up dict would miss.
    """
    variables: dict[str, str] = {}
    if seconds is not None:
        variables[_check.TIMEOUT_CEILING_ENV] = seconds
    if turns is not None:
        variables[_check.TURNS_CEILING_ENV] = turns
    timeout = lambda_timeout if lambda_timeout is not None else RUNTIME_TIMEOUT_CEILING_SECONDS + 60
    return {
        "FunctionName": "tabsii-platform-dev-plugin-agent-runtime",
        "Timeout": timeout,
        "Environment": {"Variables": variables},
    }


def test_a_deployment_matching_this_checkout_is_in_step() -> None:
    """The pass case, stated first so the failures below mean something."""
    code, report = _check.compare(_configuration())

    assert code == IN_STEP, report


def test_a_widened_ceiling_is_reported_as_unspent_headroom() -> None:
    """#132 instance 5, exactly: the deployment moved 240 -> 300, this repo did
    not notice for two days, nothing failed, and 60 seconds of granted budget
    went unspent on two stages already measured as marginal.

    This is the direction with no other detector anywhere. `RunLimits.
    from_snapshot` reports a clamp (biffo-template#1586) and deliberately says
    nothing when it did not have to clamp, so a deployment granting MORE than
    the plugin asks for produces no log line, no failure and no symptom.
    """
    widened = RUNTIME_TIMEOUT_CEILING_SECONDS + 60
    code, report = _check.compare(_configuration(seconds=str(int(widened))))

    assert code == DRIFTED
    blob = " ".join(report)
    assert "WIDENED" in blob
    assert "UNSPENT" in blob
    # Both numbers named, because a report that says "drifted" without saying
    # from what sends the reader back to the deployment to find out.
    assert f"{widened:g}" in blob
    assert f"{RUNTIME_TIMEOUT_CEILING_SECONDS:g}" in blob


def test_a_narrowed_ceiling_is_reported_as_a_clamp() -> None:
    """The direction that killed #126 and #130. Every agent asks for
    `AGENT_TIMEOUT_SECONDS`; a deployment below that grants less, silently as
    far as this repo's source is concerned, and the run dies partway through
    with the pipeline continuing on whatever it got."""
    narrowed = AGENT_TIMEOUT_SECONDS - 60
    code, report = _check.compare(_configuration(seconds=str(int(narrowed))))

    assert code == DRIFTED
    blob = " ".join(report)
    assert "NARROWED" in blob
    assert "CLAMPED" in blob
    assert f"{AGENT_TIMEOUT_SECONDS:g}" in blob


def test_an_unset_ceiling_is_cannot_tell_and_never_a_pass() -> None:
    """The fail-open this check must not be.

    A function with no `AGENT_RUNTIME_MAX_SECONDS` falls back to
    `agent_runtime`'s own code default — a constant in a repo that is not a
    dependency here. The tempting move is to write that default down and carry
    on comparing; that would make a tool built to stop trusting hand-copies
    depend on a hand-copy, and it would report a confident verdict derived from
    a number nobody read. Refusing to answer is the honest output, and exit 2
    is never a pass (AGENTS.md §7).
    """
    code, report = _check.compare(_configuration(seconds=None))

    assert code == CANNOT_TELL, report
    assert _check.TIMEOUT_CEILING_ENV in " ".join(report)


def test_an_unparseable_ceiling_is_cannot_tell() -> None:
    """Same refusal, different cause: a variable set to something that is not a
    number is not a deployment granting zero seconds."""
    code, _ = _check.compare(_configuration(seconds="none"))

    assert code == CANNOT_TELL


def test_a_definite_drift_outranks_an_unanswerable_half() -> None:
    """Precedence, asserted rather than assumed: an unreadable turns ceiling
    must not downgrade a wall clock that provably disagrees. Reporting
    "cannot tell" over a finding that IS tellable would lose the finding."""
    code, report = _check.compare(
        _configuration(seconds=str(int(RUNTIME_TIMEOUT_CEILING_SECONDS + 60)), turns=None)
    )

    assert code == DRIFTED
    # The unanswerable half is still printed — it is a second thing to fix,
    # not something the first finding excuses.
    assert _check.TURNS_CEILING_ENV in " ".join(report)


def test_a_turn_budget_above_the_deployed_ceiling_is_reported() -> None:
    """The other half of the same seam. No constant in this repo claims to know
    the turns ceiling, so nothing here can even hold a stale copy of it — which
    means a turn budget the deployment will clamp is invisible to every test in
    this repo, and visible only to something that asks the deployment."""
    code, report = _check.compare(_configuration(turns="1"))

    assert code == DRIFTED
    assert "max_turns" in " ".join(report)


def test_every_definition_factory_is_swept_for_turn_budgets() -> None:
    """#132 hole 1 was a hand-maintained denominator in a test; the same list
    written out in a script would be the same defect. With the turns ceiling at
    1, every factory must appear by name in the report — so a stage added
    tomorrow is checked without anyone remembering this file exists."""
    _, report = _check.compare(_configuration(turns="1"))

    blob = " ".join(report)
    for factory in discover_definition_factories():
        assert factory.__name__ in blob


def test_a_lambda_timeout_below_the_granted_budget_is_reported() -> None:
    """A grant the invocation cannot survive. The runtime would allow a run
    `AGENT_RUNTIME_MAX_SECONDS` seconds, but AWS kills the invocation at
    `Timeout` — so the run dies as a Lambda timeout, which is neither a clamp
    nor a run error and reads as an infrastructure fault. Both docstrings in
    `definitions.py` require `var.timeout` above `var.run_timeout_seconds`;
    this is that requirement checked against the deployment."""
    code, report = _check.compare(_configuration(lambda_timeout=RUNTIME_TIMEOUT_CEILING_SECONDS))

    assert code == DRIFTED
    assert "NO ROOM" in " ".join(report)


def test_an_unreadable_deployment_exits_cannot_tell(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of `CannotReadError` being an exception rather than an
    empty dict: a denied `lambda:GetFunctionConfiguration`, an expired session
    and a typo'd function name must not arrive at `compare()` looking like a
    function that sets no ceilings, and must never exit 0."""

    def _boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise _check.CannotReadError("could not read it")

    monkeypatch.setattr(_check, "read_deployed_configuration", _boom)

    assert _check.main(["--function-name", "whatever"]) == CANNOT_TELL


def test_main_exits_in_step_when_the_deployment_agrees(monkeypatch: pytest.MonkeyPatch) -> None:
    """The success path end to end, so the exit codes the runbook quotes are
    the ones the script actually returns."""

    def _fake(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return _configuration()

    monkeypatch.setattr(_check, "read_deployed_configuration", _fake)

    assert _check.main(["--function-name", "whatever"]) == IN_STEP


def test_a_refused_lambda_read_becomes_cannot_read_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`read_deployed_configuration` itself, with the AWS call failing.

    `test_an_unreadable_deployment_exits_cannot_tell` above replaces this whole
    function, so it proves `main` handles a `CannotReadError` — and never
    executes the `except` that raises one. The two are different claims, and
    only this one covers the line that turns a botocore exception into the
    tri-state's "cannot tell".

    It is not a hypothetical path: run the script against a name that does not
    exist and this is what answers, via `ResourceNotFoundException`.

    boto3 is injected through `sys.modules` rather than monkeypatched on the
    script, because the import is inside the function precisely so that neither
    boto3 nor credentials need to exist for the rest of the module to be
    testable — patching an attribute that does not exist at module scope would
    pass while testing nothing.
    """

    class _Client:
        def get_function_configuration(self, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("AccessDeniedException: not authorized")

    class _Session:
        def __init__(self, **_kwargs: Any) -> None: ...

        def client(self, _name: str) -> _Client:
            return _Client()

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(Session=_Session))

    with pytest.raises(_check.CannotReadError) as excinfo:
        _check.read_deployed_configuration("some-function")

    # The cause survives into the message: "cannot tell" is only actionable if
    # it says which of denied/expired/typo'd it was.
    assert "AccessDeniedException" in str(excinfo.value)
