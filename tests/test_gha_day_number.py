from __future__ import annotations

import unittest
from datetime import date

from dailyviral.gha import resolve_gha_day_number_from_recent_media
from dailyviral.instagram import InstagramMediaRecord

CAPTION_TEMPLATE = "Day {day_number} of posting this meme"


def media(*, day_number: int, timestamp: str) -> InstagramMediaRecord:
    return InstagramMediaRecord(
        media_id=str(day_number),
        caption=f"Day {day_number} of posting this meme\n\n#meme #daily",
        timestamp=timestamp,
        permalink=f"https://instagram.example/{day_number}",
        media_type="VIDEO",
        media_product_type="REELS",
    )


class GitHubActionsDayNumberTests(unittest.TestCase):
    def test_continues_after_latest_success_instead_of_calendar_day(self) -> None:
        result = resolve_gha_day_number_from_recent_media(
            caption_template=CAPTION_TEMPLATE,
            local_today=date(2026, 8, 15),
            timezone_name="America/Los_Angeles",
            recent_media=[
                media(day_number=61, timestamp="2026-07-11T16:17:00+00:00"),
                media(day_number=60, timestamp="2026-07-10T16:17:00+00:00"),
            ],
            fallback_day_number=96,
        )

        self.assertEqual(result.day_number, 62)
        self.assertFalse(result.already_posted_today)
        self.assertIn("latest successful day is 61", result.source)

    def test_same_day_success_keeps_number_for_duplicate_detection(self) -> None:
        result = resolve_gha_day_number_from_recent_media(
            caption_template=CAPTION_TEMPLATE,
            local_today=date(2026, 8, 15),
            timezone_name="America/Los_Angeles",
            recent_media=[
                media(day_number=62, timestamp="2026-08-15T16:17:00+00:00"),
            ],
            fallback_day_number=96,
        )

        self.assertEqual(result.day_number, 62)
        self.assertTrue(result.already_posted_today)

    def test_uses_start_date_only_when_no_series_post_exists(self) -> None:
        result = resolve_gha_day_number_from_recent_media(
            caption_template=CAPTION_TEMPLATE,
            local_today=date(2026, 5, 12),
            timezone_name="America/Los_Angeles",
            recent_media=[],
            fallback_day_number=1,
        )

        self.assertEqual(result.day_number, 1)
        self.assertFalse(result.already_posted_today)
        self.assertIn("fallback", result.source)


if __name__ == "__main__":
    unittest.main()


class CaptionTemplateTests(unittest.TestCase):
    def test_only_plain_day_number_placeholder_is_allowed(self):
        from dailyviral.config import ConfigurationError
        from dailyviral.gha import validate_caption_template

        for template in (
            "Day {day_number} {secret}",
            "Day {day_number!r}",
            "Day {day_number:999999999}",
            "Day {day_number",
            "No placeholder",
        ):
            with self.subTest(template=template), self.assertRaises(ConfigurationError):
                validate_caption_template(template)


class DuplicateCaptionTests(unittest.TestCase):
    def test_day_one_does_not_match_day_ten(self):
        from unittest.mock import MagicMock

        from dailyviral.gha import check_recent_caption_duplicate
        from tests.test_instagram import build_account

        result = check_recent_caption_duplicate(
            client=MagicMock(),
            account=build_account(),
            expected_caption_fragment="Day 1",
            force=False,
            recent_media=[media(day_number=10, timestamp="2026-10-01T16:17:00+00:00")],
        )
        self.assertEqual(result, "not-found")
