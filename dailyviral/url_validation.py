"""Check direct video links without permitting access to private networks."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from .config import ConfigurationError
from .http import open_public_url

DIRECT_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}


@dataclass(slots=True)
class UrlValidationResult:
    ok: bool
    message: str
    final_url: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    warnings: list[str] = field(default_factory=list)


def validate_public_video_url(url: str, *, timeout_seconds: int = 20) -> UrlValidationResult:
    try:
        with open_public_url(url.strip(), method="HEAD", timeout_seconds=timeout_seconds) as (
            response,
            final,
        ):
            status = response.status
            content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if status in {400, 403, 405, 501}:
            with open_public_url(url.strip(), timeout_seconds=timeout_seconds) as (response, final):
                status = response.status
                content_type = (
                    response.headers.get("Content-Type", "").split(";")[0].strip().lower()
                )
        if not 200 <= status < 300:
            return UrlValidationResult(
                False, f"Video endpoint returned HTTP {status}.", http_status=status
            )
        direct_file = PurePosixPath(urlsplit(final).path).suffix.lower() in DIRECT_VIDEO_EXTENSIONS
        if content_type == "text/html" or not (content_type.startswith("video/") or direct_file):
            return UrlValidationResult(
                False, "URL must return a direct video file, not a preview page."
            )
        warnings = []
        if content_type != "video/mp4":
            warnings.append("MP4 with a video/mp4 Content-Type is recommended for Meta fetch.")
        return UrlValidationResult(
            True, "Direct HTTPS video is reachable.", final, status, content_type or None, warnings
        )
    except ConfigurationError as exc:
        return UrlValidationResult(False, str(exc))
