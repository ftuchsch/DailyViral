from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from dailyviral.config import ConfigurationError
from dailyviral.r2_upload import (
    R2Config,
    R2Storage,
    build_generated_object_key,
    load_r2_config_from_env,
)


def config(**changes):
    values = dict(
        access_key_id="test-access",
        secret_access_key="test-secret",
        account_id="a" * 32,
        bucket_name="dailyviral-test",
        security_api_token="test-security-token",
        base_video_key="base/video.mp4",
    )
    values.update(changes)
    return R2Config(**values)


class PrivateStorageTests(unittest.TestCase):
    def setUp(self):
        self.boto = patch("dailyviral.r2_upload.boto3.client")
        self.factory = self.boto.start()
        self.addCleanup(self.boto.stop)
        self.storage = R2Storage(config())
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.video = Path(directory.name) / "test.mp4"
        self.video.write_bytes(b"dummy-video")

    def privacy_responses(self, managed, custom):
        session = MagicMock()
        responses = []
        for result in (managed, custom):
            response = MagicMock()
            response.status_code = 200
            response.json.return_value = {"success": True, "result": result}
            response.__enter__.return_value = response
            responses.append(response)
        session.get.side_effect = responses
        session.__enter__.return_value = session
        return patch("dailyviral.r2_upload.requests.Session", return_value=session)

    def test_private_bucket_is_accepted(self):
        with self.privacy_responses({"enabled": False}, {"domains": []}):
            self.storage.assert_bucket_private()

    def test_enabled_or_malformed_public_access_fails_closed(self):
        for managed, custom in [
            ({"enabled": True}, {"domains": []}),
            ({}, {"domains": []}),
            ({"enabled": False}, {}),
            ({"enabled": False}, {"domains": [{"enabled": True}]}),
            ({"enabled": False}, {"domains": [{}]}),
        ]:
            with (
                self.subTest(managed=managed, custom=custom),
                self.privacy_responses(managed, custom),
            ):
                with self.assertRaises(ConfigurationError):
                    self.storage.assert_bucket_private()

    def test_verification_failure_prevents_upload(self):
        with patch.object(
            self.storage, "assert_bucket_private", side_effect=ConfigurationError("unverified")
        ):
            with self.assertRaises(ConfigurationError):
                self.storage.upload_video(file_path=self.video, object_key="generated/test.mp4")
        self.storage.client.upload_file.assert_not_called()

    def test_get_only_link_is_bounded_and_upload_has_no_public_acl(self):
        self.storage.client.generate_presigned_url.return_value = (
            "https://signed.example/video.mp4?signature=test"
        )
        with (
            patch.object(self.storage, "assert_bucket_private"),
            patch("dailyviral.r2_upload.mask_github_secret") as mask,
        ):
            url = self.storage.upload_video(file_path=self.video, object_key="generated/test.mp4")
        call = self.storage.client.generate_presigned_url.call_args
        self.assertEqual(call.args, ("get_object",))
        self.assertEqual(call.kwargs["ExpiresIn"], 900)
        self.assertEqual(call.kwargs["HttpMethod"], "GET")
        self.assertNotIn("ACL", self.storage.client.upload_file.call_args.kwargs["ExtraArgs"])
        self.assertEqual(
            self.storage.client.upload_file.call_args.kwargs["ExtraArgs"]["CacheControl"],
            "private, no-store",
        )
        mask.assert_called_once_with(url)

    def test_signing_failure_deletes_uploaded_object_and_hides_error(self):
        self.storage.client.generate_presigned_url.side_effect = RuntimeError(
            "secret-should-not-leak"
        )
        with patch.object(self.storage, "assert_bucket_private"):
            with self.assertRaises(ConfigurationError) as error:
                self.storage.upload_video(file_path=self.video, object_key="generated/test.mp4")
        self.storage.client.delete_object.assert_called_once_with(
            Bucket="dailyviral-test", Key="generated/test.mp4"
        )
        self.assertNotIn("secret-should-not-leak", str(error.exception))

    def test_cleanup_cannot_delete_source_video(self):
        with self.assertRaises(ConfigurationError):
            self.storage.delete_generated_video("base/video.mp4")
        self.storage.client.delete_object.assert_not_called()

    def test_large_source_is_rejected_before_downloading(self):
        self.storage.client.head_object.return_value = {"ContentLength": 257 * 1024 * 1024}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(self.storage, "assert_bucket_private"),
        ):
            with self.assertRaises(ConfigurationError):
                self.storage.download_base_video(Path(directory) / "base.mp4")
        self.storage.client.download_file.assert_not_called()

    def test_source_stream_is_bounded_even_if_metadata_changes(self):
        self.storage.client.head_object.return_value = {"ContentLength": 4}
        body = MagicMock()
        body.iter_chunks.return_value = [b"1234", b"5678"]
        self.storage.client.get_object.return_value = {"ContentLength": 4, "Body": body}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(self.storage, "assert_bucket_private"),
            patch("dailyviral.http.MAX_VIDEO_BYTES", 5),
        ):
            destination = Path(directory) / "base.mp4"
            with self.assertRaises(ConfigurationError):
                self.storage.download_base_video(destination)
            self.assertFalse(destination.exists())
        body.close.assert_called_once()

    def test_keys_are_unique_per_attempt(self):
        keys = [
            build_generated_object_key(day_number=1, local_date=date(2026, 10, 1)) for _ in range(2)
        ]
        self.assertNotEqual(*keys)

    def test_rejects_public_mode_even_if_private_settings_are_present(self):
        with patch.dict(os.environ, {"R2_PUBLIC_BASE_URL": "https://public.example"}):
            with self.assertRaises(ConfigurationError):
                load_r2_config_from_env()

    def test_bounds_ttl_and_source_paths(self):
        for changes in (
            {"signed_url_ttl_seconds": 86400},
            {"base_video_key": "base/../secret.mp4"},
            {"account_id": "attacker.example/path"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ConfigurationError):
                config(**changes)

    def test_config_repr_does_not_contain_credentials(self):
        representation = repr(config())
        for value in ("test-access", "test-secret", "test-security-token"):
            self.assertNotIn(value, representation)


class SigningIntegrationTests(unittest.TestCase):
    def test_real_sdk_issues_private_get_signature_without_network(self):
        from urllib.parse import parse_qs, urlsplit

        storage = R2Storage(config())
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "video.mp4"
            video.write_bytes(b"dummy-video")
            with (
                patch.object(storage, "assert_bucket_private"),
                patch.object(storage.client, "upload_file"),
            ):
                url = storage.upload_video(file_path=video, object_key="generated/test.mp4")
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.hostname, "a" * 32 + ".r2.cloudflarestorage.com")
        self.assertEqual(parsed.path, "/dailyviral-test/generated/test.mp4")
        self.assertEqual(query["X-Amz-Expires"], ["900"])
        self.assertEqual(query["X-Amz-Algorithm"], ["AWS4-HMAC-SHA256"])
        self.assertIn("X-Amz-Signature", query)
        self.assertNotIn("test-secret", url)
