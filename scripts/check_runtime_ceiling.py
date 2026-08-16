#!/usr/bin/env python3
"""Read the ceilings this plugin's run budgets are enforced against, from the
deployment that enforces them — instead of asserting them from a copy.

Issue #132. Every agent definition in `marketing.definitions` declares a
`timeout_seconds` and a `max_turns`. Neither is enforced here: the
`agent_runtime` Lambda clamps both into the deployment's own ceilings
(`AGENT_RUNTIME_MAX_SECONDS`, `AGENT_RUNTIME_MAX_TURNS`), and `agent_runtime`
is not a dependency of this plugin, so nothing in this repo can import either
number. `RUNTIME_TIMEOUT_CEILING_SECONDS` is therefore a hand-copied belief
about another repo's Terraform, and every test in this repo asserts against
that copy rather than against the deployment.

**That copy went stale for two days and nothing failed.** On 2026-08-16 the
deployed `AGENT_RUNTIME_MAX_SECONDS` read `"300"` while the constant — and
every test here — still said 240. `AGENT_TIMEOUT_SECONDS` stayed pinned at 240
by a comment arguing that 240 *was* the ceiling, so 60 seconds of granted
headroom sat unspent on the two stages the estate had already measured as
marginal (`agent_runtime` logged "Agent run finished close to its wall-clock
limit" on 2026-08-13 04:53). That is instance 5 of #132, and it was found by a
human reading a deployed Lambda for an unrelated reason.

**Why no existing detector could have found it, in either repo.** The runtime
was taught to say when it *clamps* (biffo-template#1586: `RunLimits.
from_snapshot` records a `LimitClamp` naming requested, granted, ceiling and
the env var that raises it). But `clamp_report()` is deliberately empty when
nothing was reduced — "a line on every run is noise, noise gets filtered, and a
filtered line reports nothing". So the runtime tells you when the deployment
grants **less** than you asked for, and never when it grants **more**. Instance
5 was the widening direction: no clamp, no failed run, no log line, nothing to
investigate.

This script is the widening direction's detector, and it is the one form of it
this repo can build. It cannot be a pytest — reaching AWS is not a hermetic CI
gate, and the answer differs per environment — so it is an operator command in
the same shape as `seed_fan_in_workflow.py --check`, which already exists here
for the same reason (a snapshot in this checkout, a deployment that can move
under it, and no way to compare the two without asking the deployment).

    uv run python scripts/check_runtime_ceiling.py \
        --function-name tabsii-platform-dev-plugin-agent-runtime

    0  in step   · the deployment grants exactly what this checkout believes
    1  drifted   · a number here disagrees with the deployment, in either
                   direction — see the report for which and what it costs
    2  cannot tell · the deployment could not be read, or it does not set the
                   ceiling at all (see `_ceiling_from` for why that is a
                   refusal to answer rather than a pass)

**The function name is an argument and has no default, deliberately.** It is
`<project>-<environment>-plugin-agent-runtime` on the deployments this plugin
runs on, but a default would be a *fourth* copy of another repo's fact — the
exact defect this script exists to remove — and it would be a copy that reads
as authoritative while being wrong on every environment but one. `--profile`
and `--region` are passed to boto3 unchanged so an operator can point this at
whichever account holds the deployment.

What this still does not do: prove that the *plugin* deployed alongside that
runtime is the code in this checkout. This compares this checkout's declared
budgets against the deployed ceilings. If the vendored plugin is behind, its
budgets are not these ones — `seed_fan_in_workflow.py --check` is the tool for
that half, and neither substitutes for the upstream contract test that would
read both halves in CI (biffo-template#1364).
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

# Resolve `marketing.*` from the checkout rather than an installed package.
# Run with `uv run python scripts/check_runtime_ceiling.py`: importing
# `marketing.definitions` pulls in `marketing/__init__.py` -> `plugin.py` ->
# `aws_lambda_powertools`, which a bare `python3` will not have. Same
# bootstrap, and same caveat, as `seed_fan_in_workflow.py`.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from marketing.definitions import (  # noqa: E402
    AGENT_TIMEOUT_SECONDS,
    RUNTIME_TIMEOUT_CEILING_SECONDS,
    discover_definition_factories,
)

IN_STEP = 0
DRIFTED = 1
CANNOT_TELL = 2

#: The two environment variables `agent_runtime` resolves its ceilings from,
#: named here because the report has to tell an operator which lever to move
#: and they live in another repo's Terraform
#: (`services/_plugins/agent-runtime/terraform/main.tf` sets
#: `AGENT_RUNTIME_MAX_SECONDS = tostring(var.run_timeout_seconds)`).
TIMEOUT_CEILING_ENV = "AGENT_RUNTIME_MAX_SECONDS"
TURNS_CEILING_ENV = "AGENT_RUNTIME_MAX_TURNS"


class CannotReadError(Exception):
    """The deployment could not be asked — never "the deployment is fine".

    Its own exception type rather than a `None` return so a caller cannot
    accidentally treat "could not ask" as "asked, and there was nothing
    there". That collapse is what `marketing.ssm` exists to prevent on the
    SSM path, for the same reason (issue #25).
    """


def read_deployed_configuration(
    function_name: str, *, profile: str | None = None, region: str | None = None
) -> dict[str, Any]:
    """The deployed Lambda's configuration, or `CannotReadError`.

    boto3 is imported inside the function, matching `marketing.ssm`: neither
    boto3 nor credentials nor a region need to exist for this module to be
    imported, so the tests below can exercise every comparison without AWS
    being reachable at all.
    """
    try:
        import boto3
    except Exception as exc:  # pragma: no cover — boto3 is a declared dependency
        raise CannotReadError(f"boto3 is not importable: {exc}") from exc

    try:
        session = boto3.Session(profile_name=profile, region_name=region)
        client = session.client("lambda")
        return dict(client.get_function_configuration(FunctionName=function_name))
    except Exception as exc:
        # Every failure here is "cannot tell", never "in step": a missing
        # function, a denied `lambda:GetFunctionConfiguration`, an expired
        # SSO session and a typo'd name are indistinguishable to this script
        # and none of them is evidence that the ceilings agree.
        raise CannotReadError(
            f"could not read {function_name}: {type(exc).__name__}: {exc}"
        ) from exc


def _ceiling_from(environment: dict[str, str], name: str) -> float | None:
    """One ceiling as the deployment sets it, or `None` for "cannot tell".

    `None` covers both "the function does not set this variable" and "it sets
    something unparseable", and both are deliberately *not* resolved to
    `agent_runtime`'s own code default (240s / 10 turns).

    Substituting that default is the tempting move and it would recreate this
    script's own bug class: the default is a Python constant in a repo that is
    not a dependency here, so writing it down would be a fresh hand-copy,
    presented — by a tool whose whole purpose is to stop trusting copies — as a
    reading of the deployment. An unset ceiling is a question this repo cannot
    answer, and saying so is the honest output.
    """
    raw = environment.get(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def compare(configuration: dict[str, Any]) -> tuple[int, list[str]]:
    """The whole comparison, as a pure function of a Lambda configuration.

    Returns `(exit_code, report_lines)`. Kept separate from the AWS call so
    every case below — including the ones that need a *deployment* in a
    particular state — is a plain unit test rather than something only a live
    environment can reach.

    Verdict precedence is `DRIFTED` > `CANNOT_TELL` > `IN_STEP`: a definite
    disagreement outranks an unanswerable half, and anything unanswerable
    outranks a pass. Exit 2 is never a clean bill of health (AGENTS.md §7).
    """
    environment = dict(configuration.get("Environment", {}).get("Variables", {}))
    findings: list[str] = []
    unknowns: list[str] = []

    deployed_timeout = _ceiling_from(environment, TIMEOUT_CEILING_ENV)
    if deployed_timeout is None:
        unknowns.append(
            f"{TIMEOUT_CEILING_ENV} is not set (or is unparseable) on this function, so the "
            f"wall-clock ceiling is `agent_runtime`'s own code default — a constant in a repo "
            f"this plugin does not depend on. Nothing here can read it, and this script will "
            f"not guess it. Set `var.run_timeout_seconds` in the agent-runtime module and "
            f"re-run, or read the default in `agent_runtime/loop.py` by hand."
        )
    else:
        findings += _timeout_findings(deployed_timeout)
        findings += _lambda_timeout_findings(configuration, deployed_timeout)

    deployed_turns = _ceiling_from(environment, TURNS_CEILING_ENV)
    if deployed_turns is None:
        unknowns.append(
            f"{TURNS_CEILING_ENV} is not set (or is unparseable) on this function, so no turn "
            f"budget declared here can be checked against what the deployment will grant."
        )
    else:
        findings += _turn_findings(deployed_turns)

    report = findings + unknowns
    if findings:
        return DRIFTED, report
    if unknowns:
        return CANNOT_TELL, report
    return IN_STEP, [
        f"In step — {TIMEOUT_CEILING_ENV}={deployed_timeout:g}s matches "
        f"RUNTIME_TIMEOUT_CEILING_SECONDS, AGENT_TIMEOUT_SECONDS "
        f"({AGENT_TIMEOUT_SECONDS:g}s) is exactly what the deployment grants, and every "
        f"declared max_turns fits under {TURNS_CEILING_ENV}={deployed_turns:g}."
    ]


def _timeout_findings(deployed_timeout: float) -> list[str]:
    """The hand-copied ceiling, and the budget built on it, against the real one.

    Two separate assertions on purpose. The copy being wrong and the budget
    being wrong are different defects with different costs, and instance 5 was
    the first — a *correct* budget for the ceiling this repo believed in, and
    60 unspent seconds against the ceiling that was actually deployed.
    """
    findings: list[str] = []

    if RUNTIME_TIMEOUT_CEILING_SECONDS != deployed_timeout:
        direction = (
            "WIDENED — the deployment grants MORE than this repo believes"
            if deployed_timeout > RUNTIME_TIMEOUT_CEILING_SECONDS
            else "NARROWED — the deployment grants LESS than this repo believes"
        )
        findings.append(
            f"{direction}: {TIMEOUT_CEILING_ENV} is {deployed_timeout:g}s on the deployment "
            f"but RUNTIME_TIMEOUT_CEILING_SECONDS in definitions.py says "
            f"{RUNTIME_TIMEOUT_CEILING_SECONDS:g}s. Update the constant AND the deployed value "
            f"quoted in its docstring (a test pins those two to each other), then reconsider "
            f"AGENT_TIMEOUT_SECONDS, which is deliberately equal to the ceiling."
        )

    if AGENT_TIMEOUT_SECONDS > deployed_timeout:
        findings.append(
            f"CLAMPED: every agent asks for {AGENT_TIMEOUT_SECONDS:g}s but the deployment "
            f"grants at most {deployed_timeout:g}s, so `RunLimits.from_snapshot` reduces it. "
            f"This is the direction the runtime now logs (biffo-template#1586) — search the "
            f"agent-runtime log group for `budget_clamped` to see it happening. Either raise "
            f"`var.run_timeout_seconds` (and `var.timeout` above it) or lower "
            f"AGENT_TIMEOUT_SECONDS so the source stops claiming a budget no run has."
        )
    elif AGENT_TIMEOUT_SECONDS < deployed_timeout:
        findings.append(
            f"UNSPENT: the deployment grants {deployed_timeout:g}s and every agent asks for "
            f"{AGENT_TIMEOUT_SECONDS:g}s — {deployed_timeout - AGENT_TIMEOUT_SECONDS:g}s is "
            f"available and unused. This is exactly #132 instance 5, and it is the direction "
            f"nothing else reports: no run is clamped, no run fails, and the runtime says "
            f"nothing about a ceiling it did not have to enforce. Raise AGENT_TIMEOUT_SECONDS "
            f"to the granted value, or record in its docstring why the headroom is refused."
        )

    return findings


def _lambda_timeout_findings(configuration: dict[str, Any], deployed_timeout: float) -> list[str]:
    """The ceiling against the Lambda's own timeout.

    `AGENT_RUNTIME_MAX_SECONDS` is what the runtime will *grant*; the function
    `Timeout` is what AWS will *allow* before killing the invocation outright.
    A grant at or above the function timeout is a budget that cannot be spent —
    the run dies as a Lambda timeout, which surfaces as neither a clamp nor a
    run error, so it looks like an infrastructure fault rather than a
    misconfiguration. Both docstrings in `definitions.py` say `var.timeout`
    must stay above `var.run_timeout_seconds`; this is that sentence, checked.
    """
    lambda_timeout = configuration.get("Timeout")
    if not isinstance(lambda_timeout, (int, float)):
        return []
    if lambda_timeout > deployed_timeout:
        return []
    return [
        f"NO ROOM: the function's own Timeout is {lambda_timeout:g}s while it grants runs up "
        f"to {deployed_timeout:g}s, so a run spending its full budget is killed by AWS before "
        f"the runtime can end it. Raise `var.timeout` above `var.run_timeout_seconds`."
    ]


def _turn_findings(deployed_turns: float) -> list[str]:
    """Every stage's declared turn budget against the deployed turns ceiling.

    Swept from `discover_definition_factories()` rather than a list written
    here — #132 hole 1 was a hand-maintained denominator in a test, and a
    hand-maintained one in a script would be the same defect wearing a
    different hat. A stage added tomorrow is checked by this without anyone
    remembering to add it.

    Unlike the wall clock, no constant in this repo claims to know the turns
    ceiling, so there is nothing to compare it *to* — only the declared budgets
    to compare it *against*. That asymmetry is why a clamp here would be found
    by this script and not by any test.
    """
    findings: list[str] = []
    for factory in discover_definition_factories():
        definition = factory(model="some/model", instructions="do the thing")
        declared = definition.get("max_turns")
        if isinstance(declared, (int, float)) and declared > deployed_turns:
            findings.append(
                f"CLAMPED: {factory.__name__} declares max_turns={declared:g} but "
                f"{TURNS_CEILING_ENV} on the deployment is {deployed_turns:g}, so the run gets "
                f"{deployed_turns:g} turns and this repo's source says otherwise."
            )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--function-name",
        required=True,
        help=(
            "the deployed agent-runtime Lambda, e.g. "
            "tabsii-platform-dev-plugin-agent-runtime. No default: see this "
            "script's docstring for why one would be a fourth hand-copy."
        ),
    )
    parser.add_argument("--profile", default=None, help="AWS profile to read it with")
    parser.add_argument("--region", default=None, help="AWS region the function is in")
    args = parser.parse_args(argv)

    try:
        configuration = read_deployed_configuration(
            args.function_name, profile=args.profile, region=args.region
        )
    except CannotReadError as exc:
        print(f"CANNOT TELL: {exc}", file=sys.stderr)
        return CANNOT_TELL

    code, report = compare(configuration)
    stream = sys.stdout if code == IN_STEP else sys.stderr
    for line in report:
        print(f"  - {line}" if code != IN_STEP else line, file=stream)
    if code == DRIFTED:
        print(
            "\nThe deployment and this checkout disagree about what a run may spend.",
            file=sys.stderr,
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
