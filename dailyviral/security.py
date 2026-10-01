"""Keep credentials and bearer URLs out of diagnostic output."""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import quote

_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_SECRET_NAMES = ("TOKEN", "SECRET", "ACCESS_KEY", "PASSWORD", "VIDEO_URL")


def redact_sensitive(value: object) -> str:
    text = str(value)
    for name, secret in os.environ.items():
        if any(part in name.upper() for part in _SECRET_NAMES) and secret:
            for representation in (secret, quote(secret, safe="")):
                text = text.replace(representation, "[redacted]")
    # URLs may carry signed query parameters, userinfo, or sensitive object paths.
    return _URL.sub("[redacted URL]", text)


def mask_github_secret(value: str) -> None:
    """Register derived secrets before any later step can log them."""
    if os.getenv("GITHUB_ACTIONS") == "true":
        escaped = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::add-mask::{escaped}")


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_sensitive(record.getMessage())
        record.args = ()
        return True
