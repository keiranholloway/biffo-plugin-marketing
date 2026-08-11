"""`ssm.read_parameter` (issue #25) — the one place that classifies an SSM
failure as either "genuinely absent" or "could not ask".

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
    assert ssm.read_parameter("/some/parameter") == "sk-live-abc123"


def test_a_genuinely_absent_parameter_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ParameterNotFound` is SSM affirmatively answering "nothing is
    there" — the one case safe for a caller to cache."""
    _patch_client(monkeypatch, _FakeSSMClient(error_code="ParameterNotFound"))
    assert ssm.read_parameter("/some/parameter") == ""


def test_a_throttling_error_is_none_not_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact shape issue #25 reports: a momentary throttle must not read
    the same as a confirmed-absent parameter."""
    _patch_client(monkeypatch, _FakeSSMClient(error_code="ThrottlingException"))
    assert ssm.read_parameter("/some/parameter") is None


def test_an_access_denied_error_is_none_not_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A permissions problem is something to fix, but it is not "this
    parameter does not exist" — collapsing the two would send an operator
    hunting for a missing `put-parameter` that was never the issue."""
    _patch_client(monkeypatch, _FakeSSMClient(error_code="AccessDeniedException"))
    assert ssm.read_parameter("/some/parameter") is None


def test_a_non_client_transport_error_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raw connection failure (no region, network unreachable, ...) never
    even reaches a `ClientError` — it must still classify as "could not
    ask", not "asked and got nothing"."""

    class _Exploding:
        def get_parameter(self, Name: str, WithDecryption: bool) -> dict[str, Any]:  # noqa: N803
            raise OSError("network is unreachable")

    _patch_client(monkeypatch, _Exploding())
    assert ssm.read_parameter("/some/parameter") is None
