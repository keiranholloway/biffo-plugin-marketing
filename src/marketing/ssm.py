"""Shared, tested classification of an SSM ``get_parameter`` failure (issue #25).

`config.public_base_url` and `image_provider._api_key` each resolve one SSM
parameter once per warm Lambda container: ``None`` means "not yet looked up",
distinct from ``""`` meaning "looked up, and there is nothing there" — without
that distinction an unconfigured deployment would re-query SSM (and pay its
latency) on every single request. Both modules say, in their own words, that
they are "written to the same pattern deliberately".

Both had the same gap. Neither could tell a **genuinely absent** parameter
(``ParameterNotFound`` — SSM answered, and the answer is no) from an SSM call
that simply **could not be completed** (a `ThrottlingException`, a network
blip, a missing region — SSM never answered at all). Both caught every
exception the same way and cached ``""`` regardless, so a single transient
error on a cold start read as "not configured" for that container's entire
warm life, and the resulting error told an operator to fix a deployment that
was not broken.

There is a third state, and collapsing it into either of the first two is
also wrong: a **denied** grant (``AccessDeniedException``) is not "SSM
answered no" — the parameter may well exist — but it is also not the kind of
transient failure a retry fixes. An IAM grant does not repair itself between
one request and the next the way a throttle clears, so treating it as
"could not ask, try again" would mean paying a fresh `boto3.client` and a
full SSM round trip on **every request, forever**, for a condition that only
a human (fixing the grant) can end. It is therefore grouped with
`ParameterNotFound` below: both are final answers for this container's
life, and both are safe to cache, precisely because neither will change
without external action this process cannot detect anyway.

This module is the one place that draws these lines, so a future caller that
wants to cache an SSM value does not have to relearn the distinction a third
time. `config._from_ssm` and `image_provider._api_key` each keep their own
`_cached` global and their own precedence over a direct env var — only the
"what does this failure mean" classification lives here.
"""

from __future__ import annotations

from aws_lambda_powertools import Logger

logger = Logger(child=True)

#: `ClientError` codes that are a FINAL answer for this container's life —
#: not "we could not ask", but "we asked, and this will not change without a
#: human acting outside this process" (a `put-parameter`, or an IAM grant).
#: Safe to cache, unlike everything else `read_parameter` catches.
_PARAMETER_ABSENT = "ParameterNotFound"
_ACCESS_DENIED = "AccessDeniedException"


def read_parameter(parameter: str, *, purpose: str) -> str | None:
    """Read `parameter` from SSM, with decryption.

    `purpose` is a short, human-readable description of what this parameter
    is for (e.g. ``"public base URL"``, ``"image provider API key"``) —
    included in every log line so an operator triaging a throttle from a
    shared module can tell which of a deployment's several cached values
    failed, without that context living in the module that no longer knows
    it (see the module docstring on why the classification itself is
    shared while the caching stays per-caller).

    Returns:

    - the value SSM returned, verbatim (not stripped — callers normalise as
      they each already did before this module existed).
    - ``""`` when SSM gives a final answer that is not the value: the
      parameter genuinely does not exist (`ParameterNotFound`), or this
      caller is not authorised to read it (`AccessDeniedException`). Both
      need a human to change and neither self-heals mid-request, so both
      are safe for a caller to cache.
    - ``None`` when the call itself could not be completed: a transport or
      throttling error, a missing region, or anything else that means "we
      could not ask" rather than "we got a final answer". **A caller must
      never cache this** — the next call should try again, not repeat a
      non-answer forever.
    """
    try:
        import boto3
        from botocore.exceptions import ClientError
    except Exception:
        logger.warning(
            "%s: boto3 unavailable; cannot read %s from SSM", purpose, parameter, exc_info=True
        )
        return None

    try:
        client = boto3.client("ssm")
        return str(client.get_parameter(Name=parameter, WithDecryption=True)["Parameter"]["Value"])
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == _PARAMETER_ABSENT:
            logger.warning("%s: no SSM parameter at %s", purpose, parameter)
            return ""
        if code == _ACCESS_DENIED:
            logger.warning(
                "%s: SSM denied reading %s — this needs an IAM grant, not a retry; "
                "caching as unavailable for this container",
                purpose,
                parameter,
            )
            return ""
        logger.warning(
            "%s: transient SSM error reading %s (%s); will retry rather than caching as absent",
            purpose,
            parameter,
            code,
            exc_info=True,
        )
        return None
    except Exception:
        logger.warning(
            "%s: could not read %s from SSM; will retry rather than caching as absent",
            purpose,
            parameter,
            exc_info=True,
        )
        return None
