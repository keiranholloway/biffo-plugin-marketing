"""Resolving the public base URL (config.py).

The interesting behaviour is not "does it read an env var" — it is what happens
when nothing is configured, which is the state every deployment starts in.
"""

from __future__ import annotations

import pytest

from marketing import config


@pytest.fixture(autouse=True)
def _clear_cache():
    """The cache is module-level and survives between tests otherwise."""
    config.reset_cache()
    yield
    config.reset_cache()


def test_a_directly_set_url_wins_and_never_reaches_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local runs and tests must not need a region, a role or a network."""
    monkeypatch.setenv("BIFFO_PUBLIC_BASE_URL", "https://dev.tabsii.com")
    monkeypatch.setenv("BIFFO_PUBLIC_BASE_URL_PARAMETER", "/should/not/be/read")

    def _explode(_parameter: str) -> str:
        raise AssertionError("SSM must not be consulted when the value is set directly")

    monkeypatch.setattr(config, "_from_ssm", _explode)

    assert config.public_base_url() == "https://dev.tabsii.com"


def test_a_trailing_slash_is_normalised_away(monkeypatch: pytest.MonkeyPatch) -> None:
    """`tracked_url` strips one too, but a value stored with a slash should not
    depend on the caller remembering that."""
    monkeypatch.setenv("BIFFO_PUBLIC_BASE_URL", "https://dev.tabsii.com/")
    assert config.public_base_url() == "https://dev.tabsii.com"


def test_it_falls_back_to_the_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BIFFO_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("BIFFO_PUBLIC_BASE_URL_PARAMETER", "/tabsii/dev/marketing/public-base-url")
    monkeypatch.setattr(config, "_from_ssm", lambda p: "https://from-ssm.example")

    assert config.public_base_url() == "https://from-ssm.example"


def test_nothing_configured_is_empty_rather_than_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller turns this into a 503 that says what is missing.

    Raising here would surface as a 500 at the route, which says only that
    something broke — and this is the *expected* state of a deployment nobody
    has configured yet, not a fault.
    """
    monkeypatch.delenv("BIFFO_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("BIFFO_PUBLIC_BASE_URL_PARAMETER", raising=False)

    assert config.public_base_url() == ""


def test_an_unreadable_parameter_is_empty_rather_than_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing parameter, a denied grant and an SSM outage all mean the same
    thing to the caller: no link can be minted right now. All are logged."""
    monkeypatch.delenv("BIFFO_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("BIFFO_PUBLIC_BASE_URL_PARAMETER", "/tabsii/dev/marketing/public-base-url")

    # _from_ssm swallows every failure and returns "" — exercised here through
    # the real function, with boto3 absent/unreachable in the test environment.
    assert config._from_ssm("/tabsii/dev/marketing/public-base-url") == ""
    assert config.public_base_url() == ""


def test_an_unconfigured_deployment_does_not_re_query_on_every_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`None` (not looked up) and `""` (looked up, nothing there) are distinct.

    Without that distinction an unconfigured deployment pays an SSM call — and
    its latency — on every single request, failing slowly rather than quickly.
    """
    monkeypatch.delenv("BIFFO_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("BIFFO_PUBLIC_BASE_URL_PARAMETER", "/tabsii/dev/marketing/public-base-url")

    calls: list[str] = []

    def _count(parameter: str) -> str:
        calls.append(parameter)
        return ""

    monkeypatch.setattr(config, "_from_ssm", _count)

    for _ in range(5):
        assert config.public_base_url() == ""

    assert len(calls) == 1, f"SSM was consulted {len(calls)} times, not once"


def test_a_resolved_value_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BIFFO_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("BIFFO_PUBLIC_BASE_URL_PARAMETER", "/tabsii/dev/marketing/public-base-url")

    calls: list[str] = []

    def _count(parameter: str) -> str:
        calls.append(parameter)
        return "https://dev.tabsii.com"

    monkeypatch.setattr(config, "_from_ssm", _count)

    for _ in range(5):
        assert config.public_base_url() == "https://dev.tabsii.com"

    assert len(calls) == 1
