from __future__ import annotations

import io
import logging
import os
import socket
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from dailyviral.config import ConfigurationError, mask_secret
from dailyviral.http import copy_video_body, open_public_url, resolve_public_url
from dailyviral.instagram import InstagramApiError, InstagramClient
from dailyviral.queue import QueueStore
from dailyviral.security import RedactingFilter, mask_github_secret, redact_sensitive
from tests.test_instagram import FakeResponse, RecordingSession, build_account, build_settings


def dns(ip: str):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]


class PublicNetworkTests(unittest.TestCase):
    def test_rejects_internal_addresses(self):
        for ip in (
            "127.0.0.1",
            "10.0.0.1",
            "169.254.169.254",
            "192.168.1.1",
            "::1",
            "fc00::1",
            "0.0.0.0",
            "224.0.0.1",
            "ff00::1",
            "100.64.0.1",
        ):
            with self.subTest(ip=ip), patch("socket.getaddrinfo", return_value=dns(ip)):
                with self.assertRaises(ConfigurationError):
                    resolve_public_url("https://video.example/a.mp4")

    def test_rejects_unsafe_url_shapes_before_dns(self):
        for url in (
            "http://video.example/a.mp4",
            "file:///tmp/a.mp4",
            "https://user:secret@video.example/a.mp4",
            "https://video.example:8443/a.mp4",
            "https://video.example/a.mp4#fragment",
        ):
            with self.subTest(url=url), patch("socket.getaddrinfo") as resolver:
                with self.assertRaises(ConfigurationError):
                    resolve_public_url(url)
                resolver.assert_not_called()

    def test_rejects_mixed_public_and_private_dns(self):
        with patch("socket.getaddrinfo", return_value=dns("1.1.1.1") + dns("10.0.0.1")):
            with self.assertRaises(ConfigurationError):
                resolve_public_url("https://video.example/a.mp4")

    def test_connection_pins_address_and_preserves_tls_hostname(self):
        with (
            patch("socket.getaddrinfo", return_value=dns("1.1.1.1")) as resolver,
            patch("urllib3.HTTPSConnectionPool") as factory,
        ):
            pool = factory.return_value
            pool.urlopen.return_value.status = 200
            with open_public_url("https://video.example/a.mp4"):
                pass
            self.assertEqual(resolver.call_count, 1)
            self.assertEqual(factory.call_args.args, ("1.1.1.1",))
            self.assertEqual(factory.call_args.kwargs["assert_hostname"], "video.example")
            self.assertEqual(factory.call_args.kwargs["server_hostname"], "video.example")
            self.assertEqual(pool.urlopen.call_args.kwargs["headers"]["Host"], "video.example")
            pool.close.assert_called()

    def test_redirect_to_metadata_is_blocked_before_connecting(self):
        with (
            patch("socket.getaddrinfo", side_effect=[dns("1.1.1.1"), dns("169.254.169.254")]),
            patch("urllib3.HTTPSConnectionPool") as factory,
        ):
            factory.return_value.urlopen.return_value.status = 302
            factory.return_value.urlopen.return_value.headers = {
                "Location": "https://169.254.169.254/latest"
            }
            with (
                self.assertRaises(ConfigurationError),
                open_public_url("https://video.example/a.mp4"),
            ):
                pass
            self.assertEqual(factory.call_count, 1)

    def test_bounded_download_removes_partial_file(self):
        response = MagicMock()
        response.stream.return_value = [b"1234", b"5678"]
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("dailyviral.http.MAX_VIDEO_BYTES", 5),
        ):
            destination = Path(directory) / "a.mp4"
            with self.assertRaises(ConfigurationError):
                copy_video_body(response, destination)
            self.assertFalse(destination.exists())


class SecretHandlingTests(unittest.TestCase):
    def test_no_partial_token_preview(self):
        self.assertEqual(mask_secret("super-secret-token"), "[redacted]")
        self.assertEqual(mask_secret("abc"), "[redacted]")

    def test_redacts_environment_values_urls_and_logging_arguments(self):
        with patch.dict(os.environ, {"R2_SECRET_ACCESS_KEY": "private-test-value"}):
            value = redact_sensitive(
                "private-test-value https://example.com/video.mp4?X-Amz-Signature=signature"
            )
            self.assertNotIn("private-test-value", value)
            self.assertNotIn("signature", value)
            record = logging.LogRecord(
                "test", logging.ERROR, "", 0, "Error: %s", ("private-test-value",), None
            )
            RedactingFilter().filter(record)
            self.assertNotIn("private-test-value", record.getMessage())

    def test_derived_secret_mask_escapes_workflow_commands(self):
        with (
            patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}),
            redirect_stdout(io.StringIO()) as output,
        ):
            mask_github_secret("token%\n::error::injected")
        self.assertEqual(output.getvalue(), "::add-mask::token%25%0A::error::injected\n")

    def test_api_rejects_attacker_origin(self):
        settings = build_settings()
        settings.graph_api_base_url = "https://attacker.example"
        with self.assertRaises(ConfigurationError):
            InstagramClient(settings)

    def test_api_does_not_follow_redirects_and_uses_header_token(self):
        session = RecordingSession(FakeResponse({"data": []}, ok=True, status_code=200))
        client = InstagramClient(build_settings(), session=session)
        with patch.dict(os.environ, {"INSTAGRAM_ACCESS_TOKEN": "private-test-value"}):
            client.fetch_recent_media(account=build_account())
        request = session.calls[0]
        self.assertFalse(request["allow_redirects"])
        self.assertNotIn("access_token", request["params"])
        self.assertEqual(request["headers"], {"Authorization": "Bearer private-test-value"})

    def test_api_network_error_never_contains_request_url(self):
        session = MagicMock()
        session.request.side_effect = requests.ConnectionError(
            "https://example.com?access_token=private-test-value"
        )
        with self.assertRaises(InstagramApiError) as error:
            InstagramClient(build_settings(), session=session)._request_json(
                "GET", "/123", token="private-test-value"
            )
        self.assertNotIn("private-test-value", str(error.exception))

    def test_api_error_body_is_not_echoed(self):
        session = RecordingSession(
            FakeResponse(
                {
                    "error": {
                        "message": "private-test-value https://a.example/?signature=s",
                        "code": 100,
                    }
                },
                ok=False,
                status_code=400,
            )
        )
        with self.assertRaises(InstagramApiError) as error:
            InstagramClient(build_settings(), session=session)._request_json(
                "GET", "/123", token="private-test-value"
            )
        self.assertNotIn("private-test-value", str(error.exception))
        self.assertNotIn("signature", str(error.exception))

    def test_api_rejects_malformed_payload(self):
        session = RecordingSession(FakeResponse([], ok=True, status_code=200))
        with self.assertRaises(InstagramApiError):
            InstagramClient(build_settings(), session=session)._request_json(
                "GET", "/123", token="private-test-value"
            )

    def test_queue_rejects_column_injection(self):
        with tempfile.TemporaryDirectory() as directory:
            store = QueueStore(Path(directory) / "queue.sqlite")
            store.initialize()
            with self.assertRaises(ValueError):
                store.update_post(1, **{"status = 'posted' --": "queued"})


class PreviewRedactionTests(unittest.TestCase):
    def test_nested_validation_does_not_expose_signed_link(self):
        from dailyviral.cli import print_dry_run
        from dailyviral.queue import PostRecord
        from dailyviral.url_validation import UrlValidationResult

        post = PostRecord(
            1,
            "test",
            "https://private.example/a.mp4?signature=do-not-log",
            "Caption",
            "",
            "2026-10-01",
            "queued",
            "2026-10-01",
            None,
            None,
            None,
            None,
            None,
        )
        with redirect_stdout(io.StringIO()) as output:
            print_dry_run(
                post=post,
                account=build_account(),
                settings=build_settings(),
                validation=UrlValidationResult(True, "OK", post.video_url),
                final_caption="Caption",
            )
        self.assertNotIn("do-not-log", output.getvalue())
