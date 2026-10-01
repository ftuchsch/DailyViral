from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from dailyviral.config import AccountConfig, AppSettings
from dailyviral.instagram import InstagramClient, InstagramTokenError


class FakeResponse:
    def __init__(self, payload: dict, *, ok: bool, status_code: int):
        self._payload = payload
        self.ok = ok
        self.status_code = status_code
        self.text = str(payload)

    def close(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class RecordingSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def build_settings() -> AppSettings:
    return AppSettings(
        auto_post_enabled=True,
        confirm_before_posting=False,
        dry_run=False,
        graph_api_base_url="https://graph.instagram.com",
        graph_api_version="v23.0",
        publishing_limit_buffer=1,
        http_timeout_seconds=20,
        database_path=Path("unused.sqlite"),
        log_path=Path("unused.log"),
        dotenv_path=Path("unused.env"),
    )


def build_account() -> AccountConfig:
    return AccountConfig(
        account_name="main_meme_account",
        instagram_username="account",
        ig_user_id="123456789",
        access_token_env_var="INSTAGRAM_ACCESS_TOKEN",
    )


class InstagramTokenRefreshTests(unittest.TestCase):
    def test_refresh_uses_unversioned_meta_endpoint(self) -> None:
        session = RecordingSession(
            FakeResponse(
                {
                    "access_token": "refreshed-token",
                    "token_type": "bearer",
                    "expires_in": 5_183_944,
                },
                ok=True,
                status_code=200,
            )
        )
        client = InstagramClient(build_settings(), session=session)

        with patch.dict(os.environ, {"INSTAGRAM_ACCESS_TOKEN": "current-token"}):
            result = client.refresh_long_lived_access_token(build_account())

        self.assertEqual(result.access_token, "refreshed-token")
        self.assertEqual(result.expires_in_seconds, 5_183_944)
        self.assertEqual(len(session.calls), 1)
        request = session.calls[0]
        self.assertEqual(request["url"], "https://graph.instagram.com/refresh_access_token")
        self.assertEqual(
            request["params"],
            {"grant_type": "ig_refresh_token", "access_token": "current-token"},
        )

    def test_meta_error_190_becomes_token_error(self) -> None:
        session = RecordingSession(
            FakeResponse(
                {
                    "error": {
                        "message": "Error validating access token: Session has expired.",
                        "type": "OAuthException",
                        "code": 190,
                        "error_subcode": 463,
                        "fbtrace_id": "trace-id",
                    }
                },
                ok=False,
                status_code=400,
            )
        )
        client = InstagramClient(build_settings(), session=session)

        with patch.dict(os.environ, {"INSTAGRAM_ACCESS_TOKEN": "expired-token"}):
            with self.assertRaises(InstagramTokenError) as raised:
                client.refresh_long_lived_access_token(build_account())

        self.assertEqual(raised.exception.code, 190)
        self.assertEqual(raised.exception.subcode, 463)
        self.assertIn("invalid or expired", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
