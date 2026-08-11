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


def test_a_genuinely_missing_parameter_is_empty_and_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmed-absent parameter (`ssm.read_parameter` returning `""`) is
    the expected state of a deployment nobody has configured yet — `""`, and
    safe to cache (see the transient-failure test below for the contrast)."""
    monkeypatch.delenv("BIFFO_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("BIFFO_PUBLIC_BASE_URL_PARAMETER", "/tabsii/dev/marketing/public-base-url")
    monkeypatch.setattr(config.ssm, "read_parameter", lambda parameter: "")

    assert config._from_ssm("/tabsii/dev/marketing/public-base-url") == ""
    assert config.public_base_url() == ""
    assert config._cached == ""


def test_a_transient_ssm_failure_does_not_poison_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #25. `ssm.read_parameter` returning `None` means "could not ask"
    — a `ThrottlingException`, a network blip, a missing region — and must
    NOT be cached as "asked and got nothing", or one bad cold start disables
    link minting for that container's entire warm life.

    Fails before the fix: the old `_from_ssm` caught every exception the same
    way and always cached `""`, so the second call below never re-consulted
    SSM and stayed wrong for a value that was there all along.
    """
    monkeypatch.delenv("BIFFO_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("BIFFO_PUBLIC_BASE_URL_PARAMETER", "/tabsii/dev/marketing/public-base-url")

    responses: list[str | None] = [None, "https://dev.tabsii.com"]
    calls: list[str] = []

    def _flaky_then_fine(parameter: str) -> str | None:
        calls.append(parameter)
        return responses.pop(0)

    monkeypatch.setattr(config.ssm, "read_parameter", _flaky_then_fine)

    # First call: SSM could not be reached. This call reports "not
    # configured"...
    assert config.public_base_url() == ""
    # ...but that must not have been cached as the answer.
    assert config._cached is None

    # A later call — the same warm container, no redeploy — retries and
    # gets the real value, because nothing poisoned the cache.
    assert config.public_base_url() == "https://dev.tabsii.com"
    assert len(calls) == 2


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


def test_the_request_origin_is_preferred_over_configuration() -> None:
    """The origin an operator is looking at IS the public base URL.

    A plugin's Terraform configures the plugin's OWN Lambda, but an
    `admin_ingress` app runs on the shared plugin host — a different function
    with a different environment. So the configured value is absent exactly
    where this code runs (biffo-template#1456), and the request is the only
    reliable source.
    """
    assert config.public_base_url_for("https://dev.tabsii.com", None) == "https://dev.tabsii.com"


def test_the_referer_is_the_fallback_when_there_is_no_origin() -> None:
    """A same-origin GET may carry Referer but no Origin."""
    assert (
        config.public_base_url_for(None, "https://dev.tabsii.com/api/v1/plugins/marketing/admin")
        == "https://dev.tabsii.com"
    )


def test_a_non_http_scheme_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only http(s) origins are usable; anything else falls through."""
    monkeypatch.setenv("BIFFO_PUBLIC_BASE_URL", "https://configured.example")
    assert config.public_base_url_for("file:///etc/passwd", None) == "https://configured.example"


def test_it_falls_back_to_configuration_when_neither_header_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIFFO_PUBLIC_BASE_URL", "https://configured.example")
    assert config.public_base_url_for(None, None) == "https://configured.example"
