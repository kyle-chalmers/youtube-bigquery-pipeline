"""Strip credentials out of text before it reaches a log line.

Google API client errors embed the request URL in their message, and the Data API puts the
API key in that URL as a query parameter. `log.exception(f"... {e}")` therefore wrote the
key into Cloud Logging on every hard failure (observed four times between 2026-06 and
2026-09). Every log line built from an exception message or a traceback goes through
`redact` first.
"""

import re

_QUERY_SECRET = re.compile(r"([?&](?:key|access_token|refresh_token|client_secret|token)=)[^&\s\"'<>]+")
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+")


def redact(text: str) -> str:
    """Replace secret-bearing URL query values and bearer tokens with <redacted>."""
    text = _QUERY_SECRET.sub(r"\1<redacted>", text)
    return _BEARER.sub(r"\1<redacted>", text)
