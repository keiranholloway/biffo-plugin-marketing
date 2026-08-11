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
blip, a missing region, a denied grant — SSM never answered at all). Both
caught every exception the same way and cached ``""`` regardless, so a single
transient error on a cold start read as "not configured" for that container's
entire warm life, and the resulting error told an operator to fix a deployment
that was not broken.

This module is the one place that draws that line, so a future caller that
wants to cache an SSM value does not have to relearn the distinction a third
time. `config._from_ssm` and `image_provider._api_key` each keep their own
`_cached` global and their own precedence over a direct env var — only the
"what does this failure mean" classification lives here.
"""

from __future__ import annotations

from aws_lambda_powertools import Logger

logger = Logger(child=True)


def read_parameter(parameter: str) -> str | None:
    """Read `parameter` from SSM, with decryption.

    Returns:

    - the value SSM returned, verbatim (not stripped — callers normalise as
      they each already did before this module existed).
    - ``""`` when SSM affirmatively answers "nothing is there"
      (`ParameterNotFound`) — a genuinely unconfigured deployment. Safe for a
      caller to cache: asking again would get the same answer.
    - ``None`` when the call itself could not be completed: a transport or
      throttling error, a missing region, denied credentials, or anything
      else that means "we could not ask" rather than "we asked and the
      answer is no". **A caller must never cache this** — the next call
      should try again, not repeat a non-answer forever.
    """
    try:
        import boto3
        from botocore.exceptions import ClientError
    except Exception:
        logger.warning("boto3 unavailable; cannot read %s from SSM", parameter, exc_info=True)
        return None

    try:
        client = boto3.client("ssm")
        return str(client.get_parameter(Name=parameter, WithDecryption=True)["Parameter"]["Value"])
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ParameterNotFound":
            logger.warning("No SSM parameter at %s", parameter)
            return ""
        logger.warning(
            "Transient SSM error reading %s (%s); will retry rather than caching as absent",
            parameter,
            code,
            exc_info=True,
        )
        return None
    except Exception:
        logger.warning(
            "Could not read %s from SSM; will retry rather than caching as absent",
            parameter,
            exc_info=True,
        )
        return None
