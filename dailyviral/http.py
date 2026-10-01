"""HTTPS downloads pinned to validated public IPs, including every redirect."""

from __future__ import annotations

import ipaddress
import socket
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import urljoin, urlsplit

import urllib3

from .config import ConfigurationError

MAX_VIDEO_BYTES = 256 * 1024 * 1024


def resolve_public_url(url: str) -> tuple[str, str, str]:
    """Validate before connecting, then return an IP to avoid DNS rebinding."""
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or parsed.fragment
            or any(ord(char) < 32 for char in url)
        ):
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii")
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = {answer[4][0] for answer in answers}
        if not addresses or any(
            not ipaddress.ip_address(address).is_global
            or ipaddress.ip_address(address).is_multicast
            for address in addresses
        ):
            raise ValueError
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
        return host, sorted(addresses)[0], path
    except (ValueError, UnicodeError, OSError):
        raise ConfigurationError(
            "Video URL must use HTTPS on port 443 and resolve only to public IPs."
        ) from None


@contextmanager
def open_public_url(
    url: str,
    *,
    method: str = "GET",
    timeout_seconds: int = 20,
) -> Iterator[tuple[urllib3.HTTPResponse, str]]:
    current_url = url
    pool = None
    response = None
    try:
        for _ in range(6):
            host, address, path = resolve_public_url(current_url)
            # Connect to the vetted address while retaining hostname TLS checks/SNI.
            pool = urllib3.HTTPSConnectionPool(
                address,
                port=443,
                server_hostname=host,
                assert_hostname=host,
                timeout=urllib3.Timeout(connect=timeout_seconds, read=timeout_seconds),
                retries=False,
            )
            response = pool.urlopen(
                method,
                path,
                redirect=False,
                preload_content=False,
                headers={"Host": host, "User-Agent": "DailyViral/1.0"},
            )
            if response.status not in {301, 302, 303, 307, 308}:
                yield response, current_url
                return
            location = response.headers.get("Location")
            response.close()
            pool.close()
            if not location:
                raise ConfigurationError("Video redirect has no destination.")
            current_url = urljoin(current_url, location)
        raise ConfigurationError("Video URL exceeded the redirect limit.")
    except (urllib3.exceptions.HTTPError, OSError):
        raise ConfigurationError("Could not reach the video URL over HTTPS.") from None
    finally:
        if response is not None:
            response.close()
        if pool is not None:
            pool.close()


def copy_video_body(
    response: urllib3.HTTPResponse, destination: Path, *, deadline_seconds: int = 120
) -> None:
    write_video_chunks(response.stream(512 * 1024), destination, deadline_seconds=deadline_seconds)


def write_video_chunks(
    chunks: Iterable[bytes], destination: Path, *, deadline_seconds: int = 120
) -> None:
    size = 0
    start = time.monotonic()
    try:
        with destination.open("wb") as handle:
            for chunk in chunks:
                size += len(chunk)
                if size > MAX_VIDEO_BYTES or time.monotonic() - start > deadline_seconds:
                    raise ConfigurationError("Video exceeds the download size or duration limit.")
                handle.write(chunk)
        if size == 0:
            raise ConfigurationError("Video download was empty.")
    except Exception:
        destination.unlink(missing_ok=True)
        raise ConfigurationError("Video download failed or exceeded its limits.") from None
