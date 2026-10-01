from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from dailyviral.config import ConfigurationError
from dailyviral.video_overlay import build_overlay_render_plan, render_overlay_video


class OverlayIsolationTests(unittest.TestCase):
    def test_ffmpeg_does_not_inherit_credentials_and_text_is_literal(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = build_overlay_render_plan(
                day_number=1,
                local_date=date(2026, 10, 1),
                output_root=Path(directory),
                overlay_sentence="%{env:INSTAGRAM_ACCESS_TOKEN}",
            )

            def render(*args, **kwargs):
                plan.output_video_path.write_bytes(b"dummy-video")
                return subprocess.CompletedProcess(args[0], 0, "", "")

            with (
                patch.dict(os.environ, {"INSTAGRAM_ACCESS_TOKEN": "private-test-value"}),
                patch("dailyviral.video_overlay.shutil.which", return_value="/usr/bin/ffmpeg"),
                patch("dailyviral.video_overlay.subprocess.run", side_effect=render) as process,
            ):
                render_overlay_video(plan=plan)
            command = process.call_args.args[0]
            self.assertIn("file", command)
            self.assertEqual(command[command.index("-f") + 1], "mov")
            self.assertIn("expansion=none", command[command.index("-vf") + 1])
            self.assertNotIn("INSTAGRAM_ACCESS_TOKEN", process.call_args.kwargs["env"])
            self.assertEqual(process.call_args.kwargs["timeout"], 300)
            self.assertEqual(plan.overlay_text_path.read_text(), "%{env:INSTAGRAM_ACCESS_TOKEN}")

    def test_render_timeout_becomes_safe_application_error(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = build_overlay_render_plan(
                day_number=1,
                local_date=date(2026, 10, 1),
                output_root=Path(directory),
                overlay_sentence="Hello",
            )
            with (
                patch("dailyviral.video_overlay.shutil.which", return_value="/usr/bin/ffmpeg"),
                patch(
                    "dailyviral.video_overlay.subprocess.run",
                    side_effect=subprocess.TimeoutExpired("ffmpeg", 300),
                ),
            ):
                with self.assertRaises(ConfigurationError) as error:
                    render_overlay_video(plan=plan)
            self.assertIn("render limit", str(error.exception))
