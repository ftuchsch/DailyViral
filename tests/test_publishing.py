from __future__ import annotations

import argparse
import io
import logging
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from dailyviral.config import ConfigurationError
from dailyviral.gha import handle_gha_post_daily
from dailyviral.instagram import InstagramApiError
from tests.test_instagram import build_settings


class PublishingLifecycleTests(unittest.TestCase):
    def run_post(self, *, publish_error=False, cleanup_error=False, dry_run=False):
        environment = {
            "INSTAGRAM_ACCESS_TOKEN": "test-instagram-token",
            "IG_USER_ID": "123456789",
            "INSTAGRAM_USERNAME": "test-account",
            "DAILYVIRAL_START_DATE": "2026-01-01",
            "ENABLE_VIDEO_OVERLAY": "true",
            "R2_BASE_VIDEO_KEY": "base/video.mp4",
            "R2_ACCESS_KEY_ID": "test-access",
            "R2_SECRET_ACCESS_KEY": "test-secret",
            "R2_SECURITY_API_TOKEN": "test-security",
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_BUCKET_NAME": "dailyviral-test",
            "R2_PUBLIC_BASE_URL": "",
        }
        settings = build_settings()
        settings.dry_run = dry_run
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, environment),
            patch("dailyviral.gha.PROJECT_ROOT", Path(directory)),
            patch("dailyviral.gha.check_overlay_runtime", return_value=[]),
            patch("dailyviral.gha.select_overlay_sentence", return_value="Hello"),
            patch("dailyviral.gha.refresh_gha_access_token", return_value=True),
            patch("dailyviral.gha.InstagramClient") as instagram,
            patch("dailyviral.gha.R2Storage") as storage,
            patch(
                "dailyviral.gha.render_overlay_video", return_value=Path(directory) / "render.mp4"
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            instagram.return_value.fetch_recent_media.return_value = []
            instagram.return_value.publish_reel.return_value.media_id = "123"
            instagram.return_value.publish_reel.return_value.permalink = None
            storage.return_value.upload_video.return_value = (
                "https://private.example/video.mp4?signature=do-not-log"
            )
            if publish_error:
                instagram.return_value.publish_reel.side_effect = InstagramApiError(
                    "publish failed"
                )
            if cleanup_error:
                storage.return_value.delete_generated_video.side_effect = ConfigurationError(
                    "cleanup failed"
                )
            result = handle_gha_post_daily(
                argparse.Namespace(account_name="main_meme_account", force=False),
                settings,
                logging.getLogger("test"),
            )
            return result, instagram, storage, output.getvalue()

    def test_success_deletes_temporary_object(self):
        result, _, storage, output = self.run_post()
        self.assertEqual(result, 0)
        storage.return_value.delete_generated_video.assert_called_once()
        self.assertNotIn("do-not-log", output)

    def test_publish_failure_still_deletes_temporary_object(self):
        result, _, storage, _ = self.run_post(publish_error=True)
        self.assertEqual(result, 1)
        storage.return_value.delete_generated_video.assert_called_once()

    def test_cleanup_failure_reports_published_media_without_forcing_retry(self):
        result, _, _, output = self.run_post(cleanup_error=True)
        self.assertEqual(result, 1)
        self.assertIn("Do not force a duplicate", output)

    def test_preview_never_uploads_or_publishes(self):
        result, instagram, storage, _ = self.run_post(dry_run=True)
        self.assertEqual(result, 0)
        instagram.return_value.publish_reel.assert_not_called()
        storage.assert_not_called()
