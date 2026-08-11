"""`ssm.read_parameter` (issue #25) — the one place that classifies an SSM
failure as one of three things: a final "no" (safe to cache), a final
"denied" (also safe to cache — see below), or "could not ask at all" (must
never be cached).

Both callers (`config._from_ssm`, `image_provider._api_key`) cache this
module's `""` forever and never cache its `None`. This file proves the
classification itself; `test_marketing_config.py` and
`test_marketing_image_provider*.py` prove each caller respects it.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from marketing import ssm

_PURPOSE = "test parameter"


class _FakeSSMClient:
    """Stands in for `boto3.client("ssm")` — either returns a parameter value
    or raises exactly the `ClientError` a real `get_parameter` call would."""

    def __init__(self, *, value: str | None = None, error_code: str | None = None) -> None:
        self._value = value
        self._error_code = error_code

    def get_parameter(self, Name: str, WithDecryption: bool) -> dict[str, Any]:  # noqa: N803
        if self._error_code is not None:
            raise ClientError(
                {"Error": {"Code": self._error_code, "Message": "synthetic"}}, "GetParameter"
            )
        return {"Parameter": {"Value": self._value}}


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    monkeypatch.setattr("boto3.client", lambda service: client)


def test_returns_the_value_when_ssm_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, _FakeSSMClient(value="sk-live-abc123"))
    assert ssm.read_parameter("/some/parameter", purpose=_PURPOSE) == "sk-live-abc123"


def test_a_genuinely_absent_parameter_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ParameterNotFound` is SSM affirmatively answering "nothing is
    there" — safe for a caller to cache."""
    _patch_client(monkeypatch, _FakeSSMClient(error_code="ParameterNotFound"))
    assert ssm.read_parameter("/some/parameter", purpose=_PURPOSE) == ""


def test_a_throttling_error_is_none_not_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact shape issue #25 reports: a momentary throttle must not read
    the same as a confirmed-absent parameter — it self-heals, so it must
    not be cached."""
    _patch_client(monkeypatch, _FakeSSMClient(error_code="ThrottlingException"))
    assert ssm.read_parameter("/some/parameter", purpose=_PURPOSE) is None


def test_an_access_denied_error_is_cached_as_a_final_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AccessDeniedException` is a THIRD state, distinct from both a
    confirmed-absent parameter and a transient failure — the parameter may
    well exist, but this caller is not, and will not become, authorised to
    read it before this container recycles. An IAM grant does not repair
    itself between one request and the next the way a throttle clears, so
    treating this as "could not ask, retry" would pay a fresh
    `boto3.client()` and a full SSM round trip on every request forever for
    a condition only a human can fix. It is grouped with `ParameterNotFound`
    instead: both are final for this container's life, and both are safe to
    cache."""
    _patch_client(monkeypatch, _FakeSSMClient(error_code="AccessDeniedException"))
    assert ssm.read_parameter("/some/parameter", purpose=_PURPOSE) == ""


def test_a_non_client_transport_error_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raw connection failure (no region, network unreachable, ...) never
    even reaches a `ClientError` — it must still classify as "could not
    ask", not a final answer."""

    class _Exploding:
        def get_parameter(self, Name: str, WithDecryption: bool) -> dict[str, Any]:  # noqa: N803
            raise OSError("network is unreachable")

    _patch_client(monkeypatch, _Exploding())
    assert ssm.read_parameter("/some/parameter", purpose=_PURPOSE) is None


def test_the_purpose_reaches_the_log_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Both callers share this module's classification but not its context —
    `purpose` is what lets an operator triaging a throttle tell which of a
    deployment's cached values failed, from a log line the shared module
    itself has no other way to make specific."""
    _patch_client(monkeypatch, _FakeSSMClient(error_code="ThrottlingException"))

    with caplog.at_level("WARNING"):
        ssm.read_parameter("/some/parameter", purpose="a very specific purpose")

    assert any("a very specific purpose" in record.message for record in caplog.records)
