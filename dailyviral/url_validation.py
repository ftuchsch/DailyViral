from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import urlparse

import requests

DIRECT_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
PREVIEW_HOST_HINTS = {
    "drive.google.com",
    "docs.google.com",
    "dropbox.com",
    "www.dropbox.com",
}


@dataclass(slots=True)
class UrlValidationResult:
    ok: bool
    message: str
    final_url: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    warnings: list[str] = field(default_factory=list)


def validate_public_video_url(url: str, *, timeout_seconds: int = 20) -> UrlValidationResult:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return UrlValidationResult(
            ok=False,
            message="Video URL must be a fully-qualified public http(s) URL.",
        )

    host = parsed.netloc.lower()
    if _is_known_preview_url(host=host, url=url):
        return UrlValidationResult(
            ok=False,
            message=(
                "URL looks like a Google Drive or Dropbox preview page. "
                "Instagram requires a direct public video file URL that Meta can fetch."
            ),
        )

    session = requests.Session()
    try:
        response = _probe_with_head_then_get(session, url, timeout_seconds=timeout_seconds)
    except requests.RequestException as exc:
        return UrlValidationResult(
            ok=False,
            message=(
                "Could not reach the video URL from a server context. "
                f"Meta needs a direct public URL. Error: {exc}"
            ),
        )

    final_url = response.url
    status_code = response.status_code
    content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    warnings: list[str] = []

    try:
        if status_code < 200 or status_code >= 300:
            return UrlValidationResult(
                ok=False,
                message=(
                    f"URL responded with HTTP {status_code}. "
                    "Meta must be able to fetch the video publicly without authentication."
                ),
                final_url=final_url,
                http_status=status_code,
                content_type=content_type or None,
            )

        if _is_known_preview_url(host=urlparse(final_url).netloc.lower(), url=final_url):
            return UrlValidationResult(
                ok=False,
                message=(
                    "URL resolves to a preview or landing page instead of a direct video file. "
                    "Meta needs a direct public MP4-style URL."
                ),
                final_url=final_url,
                http_status=status_code,
                content_type=content_type or None,
            )

        suffix = PurePosixPath(urlparse(final_url).path).suffix.lower()
        is_video_content_type = content_type.startswith("video/")
        is_direct_file = suffix in DIRECT_VIDEO_EXTENSIONS

        if content_type == "text/html":
            return UrlValidationResult(
                ok=False,
                message=(
                    "URL returned an HTML page, not a video file. "
                    "Instagram requires a direct public video URL that Meta can fetch."
                ),
                final_url=final_url,
                http_status=status_code,
                content_type=content_type,
            )

        if not is_video_content_type and not is_direct_file:
            return UrlValidationResult(
                ok=False,
                message=(
                    "URL is reachable but does not look like a direct video file. "
                    "Use a public MP4 URL when possible."
                ),
                final_url=final_url,
                http_status=status_code,
                content_type=content_type or None,
            )

        if is_video_content_type and content_type != "video/mp4":
            warnings.append(
                f"Content-Type is {content_type}. MP4 is usually the safest format for Meta fetch."
            )
        elif not is_video_content_type and is_direct_file:
            warnings.append(
                "Server did not return a video Content-Type header, but the URL looks like a direct file."
            )

        return UrlValidationResult(
            ok=True,
            message="Video URL is publicly reachable and looks like a direct video file.",
            final_url=final_url,
            http_status=status_code,
            content_type=content_type or None,
            warnings=warnings,
        )
    finally:
        response.close()
        session.close()


def _probe_with_head_then_get(
    session: requests.Session,
    url: str,
    *,
    timeout_seconds: int,
) -> requests.Response:
    headers = {"User-Agent": "DailyViral/0.1 (+https://example.invalid/dailyviral)"}

    head_response = session.head(
        url,
        allow_redirects=True,
        timeout=timeout_seconds,
        headers=headers,
    )
    if head_response.status_code in {400, 403, 405, 429, 500, 501, 502, 503, 504}:
        head_response.close()
        return session.get(
            url,
            allow_redirects=True,
            timeout=timeout_seconds,
            headers=headers,
            stream=True,
        )
    return head_response


def _is_known_preview_url(*, host: str, url: str) -> bool:
    normalized_host = host.lower()
    if normalized_host in {"dropbox.com", "www.dropbox.com"}:
        return "raw=1" not in url and "dl=1" not in url
    if normalized_host in {"drive.google.com", "docs.google.com"}:
        return True
    return normalized_host in PREVIEW_HOST_HINTS
