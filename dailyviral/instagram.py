from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from .config import AccountConfig, AppSettings, ConfigurationError, require_access_token
from .security import mask_github_secret


class InstagramApiError(RuntimeError):
    """Raised when the Instagram Graph API returns an error."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        subcode: int | None = None,
        fbtrace_id: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.fbtrace_id = fbtrace_id


class InstagramTokenError(InstagramApiError):
    """Raised when Meta rejects an expired, revoked, or otherwise invalid token."""


class PublishingLimitError(InstagramApiError):
    """Raised when the content publishing limit would be exceeded."""


@dataclass(slots=True)
class PublishingLimit:
    quota_usage: int
    quota_total: int
    quota_duration_seconds: int | None

    @property
    def remaining(self) -> int:
        return max(self.quota_total - self.quota_usage, 0)


@dataclass(slots=True)
class InstagramPublishResult:
    creation_id: str
    media_id: str
    permalink: str | None
    limit: PublishingLimit | None


@dataclass(slots=True)
class InstagramMediaRecord:
    media_id: str
    caption: str
    timestamp: str | None
    permalink: str | None
    media_type: str | None
    media_product_type: str | None


@dataclass(slots=True)
class InstagramTokenRefreshResult:
    access_token: str = field(repr=False)
    expires_in_seconds: int | None


class InstagramClient:
    def __init__(
        self,
        settings: AppSettings,
        *,
        logger: logging.Logger | None = None,
        session: requests.Session | None = None,
    ):
        if settings.graph_api_base_url not in {
            "https://graph.instagram.com",
            "https://graph.facebook.com",
        }:
            raise ConfigurationError("GRAPH_API_BASE_URL must be an official HTTPS Meta origin.")
        if not re.fullmatch(r"v[0-9]+\.[0-9]+", settings.graph_api_version):
            raise ConfigurationError("GRAPH_API_VERSION must use vNN.N format.")
        self.settings = settings
        self.logger = logger or logging.getLogger("dailyviral")
        self.session = session or requests.Session()
        self.session.trust_env = False

    def refresh_long_lived_access_token(
        self,
        account: AccountConfig,
    ) -> InstagramTokenRefreshResult:
        """Extend an unexpired Instagram long-lived token for another 60 days.

        Meta's refresh endpoint is intentionally not Graph-API-versioned. The token
        must already be long-lived and must not have expired.
        """
        current_token = require_access_token(account)
        payload = self._request_url_json(
            "GET",
            f"{self.settings.graph_api_base_url}/refresh_access_token",
            token=current_token,
            params={"grant_type": "ig_refresh_token"},
        )
        refreshed_token = str(payload.get("access_token", "")).strip()
        if not refreshed_token:
            raise InstagramApiError(
                "Instagram token refresh succeeded without returning an access token."
            )

        raw_expires_in = payload.get("expires_in")
        try:
            expires_in_seconds = int(raw_expires_in) if raw_expires_in is not None else None
        except (TypeError, ValueError):
            expires_in_seconds = None

        self.logger.info(
            "Refreshed Instagram access token for %s; expires_in_seconds=%s",
            account.account_name,
            expires_in_seconds if expires_in_seconds is not None else "<unknown>",
        )
        mask_github_secret(refreshed_token)
        return InstagramTokenRefreshResult(
            access_token=refreshed_token,
            expires_in_seconds=expires_in_seconds,
        )

    def get_content_publishing_limit(self, account: AccountConfig) -> PublishingLimit | None:
        token = require_access_token(account)
        payload = self._request_json(
            "GET",
            f"/{account.ig_user_id}/content_publishing_limit",
            token=token,
            params={"fields": "config,quota_usage"},
        )

        node = payload
        if isinstance(payload.get("data"), list) and payload["data"]:
            node = payload["data"][0]

        quota_usage = node.get("quota_usage")
        config = node.get("config", {})
        quota_total = config.get("quota_total")
        quota_duration = config.get("quota_duration")

        if quota_usage is None or quota_total is None:
            self.logger.warning(
                "Instagram content publishing limit response did not include quota fields",
            )
            return None

        return PublishingLimit(
            quota_usage=int(quota_usage),
            quota_total=int(quota_total),
            quota_duration_seconds=int(quota_duration) if quota_duration is not None else None,
        )

    def assert_can_publish(self, account: AccountConfig) -> PublishingLimit | None:
        try:
            limit = self.get_content_publishing_limit(account)
        except InstagramApiError as exc:
            if "Unsupported get request" in str(exc) or "Unknown path components" in str(exc):
                self.logger.warning(
                    "Could not verify content publishing limit for %s: %s",
                    account.account_name,
                    exc,
                )
                return None
            raise

        if limit and limit.remaining <= self.settings.publishing_limit_buffer:
            raise PublishingLimitError(
                "Instagram publishing limit is too close to exhaustion: "
                f"{limit.quota_usage}/{limit.quota_total} used in the active window."
            )
        return limit

    def publish_reel(
        self,
        *,
        account: AccountConfig,
        video_url: str,
        caption: str,
    ) -> InstagramPublishResult:
        token = require_access_token(account)
        limit = self.assert_can_publish(account)

        creation_id = self.create_reel_container(
            account=account,
            token=token,
            video_url=video_url,
            caption=caption,
        )
        self.wait_until_container_ready(creation_id=creation_id, token=token)
        media_id = self.publish_container(
            account=account,
            token=token,
            creation_id=creation_id,
        )
        permalink = self.fetch_permalink(media_id=media_id, token=token)
        return InstagramPublishResult(
            creation_id=creation_id,
            media_id=media_id,
            permalink=permalink,
            limit=limit,
        )

    def create_reel_container(
        self,
        *,
        account: AccountConfig,
        token: str,
        video_url: str,
        caption: str,
    ) -> str:
        payload = self._request_json(
            "POST",
            f"/{account.ig_user_id}/media",
            token=token,
            data={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption,
                "share_to_feed": "true",
            },
        )
        creation_id = str(payload.get("id", "")).strip()
        if not creation_id:
            raise InstagramApiError("Instagram did not return a creation id.")
        self.logger.info(
            "Created Instagram media container for %s: %s",
            account.account_name,
            creation_id,
        )
        return creation_id

    def wait_until_container_ready(self, *, creation_id: str, token: str) -> None:
        for attempt in range(1, 16):
            payload = self._request_json(
                "GET",
                f"/{creation_id}",
                token=token,
                params={"fields": "status_code,status,error_message"},
            )
            status_code = str(payload.get("status_code", "")).upper()
            status = str(payload.get("status", "")).upper()

            self.logger.info(
                "Polled container %s attempt %s: status_code=%s status=%s",
                creation_id,
                attempt,
                status_code or "<none>",
                status or "<none>",
            )

            if status_code in {"FINISHED", "PUBLISHED"} or status in {"FINISHED", "PUBLISHED"}:
                return
            if status_code in {"ERROR", "EXPIRED", "FAILED"} or status in {
                "ERROR",
                "EXPIRED",
                "FAILED",
            }:
                raise InstagramApiError(
                    f"Instagram media processing failed for container {creation_id}."
                )

            time.sleep(4)

        raise InstagramApiError(
            f"Instagram media container {creation_id} was not ready before timeout."
        )

    def publish_container(
        self,
        *,
        account: AccountConfig,
        token: str,
        creation_id: str,
    ) -> str:
        payload = self._request_json(
            "POST",
            f"/{account.ig_user_id}/media_publish",
            token=token,
            data={"creation_id": creation_id},
        )
        media_id = str(payload.get("id", "")).strip()
        if not media_id:
            raise InstagramApiError("Instagram did not return a media id.")
        self.logger.info(
            "Published Instagram media for %s: media_id=%s",
            account.account_name,
            media_id,
        )
        return media_id

    def fetch_permalink(self, *, media_id: str, token: str) -> str | None:
        payload = self._request_json(
            "GET",
            f"/{media_id}",
            token=token,
            params={"fields": "id,permalink,media_type,media_product_type"},
        )
        permalink = str(payload.get("permalink", "")).strip()
        self.logger.info("Fetched media details for %s", media_id)
        return permalink or None

    def fetch_recent_media(
        self,
        *,
        account: AccountConfig,
        limit: int = 10,
    ) -> list[InstagramMediaRecord]:
        token = require_access_token(account)
        payload = self._request_json(
            "GET",
            f"/{account.ig_user_id}/media",
            token=token,
            params={
                "fields": "id,caption,timestamp,permalink,media_type,media_product_type",
                "limit": str(limit),
            },
        )
        rows = payload.get("data", [])
        if not isinstance(rows, list):
            raise InstagramApiError("Instagram recent media response was not a list.")

        records: list[InstagramMediaRecord] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            records.append(
                InstagramMediaRecord(
                    media_id=str(row.get("id", "")).strip(),
                    caption=str(row.get("caption", "") or "").strip(),
                    timestamp=str(row.get("timestamp", "")).strip() or None,
                    permalink=str(row.get("permalink", "")).strip() or None,
                    media_type=str(row.get("media_type", "")).strip() or None,
                    media_product_type=str(row.get("media_product_type", "")).strip() or None,
                )
            )
        return records

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        token: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"/[0-9]+(?:/[a-z_]+)?", path):
            raise ConfigurationError("Instagram resource paths must contain numeric IDs.")
        url = f"{self.settings.graph_api_base_url}/{self.settings.graph_api_version}{path}"
        return self._request_url_json(
            method,
            url,
            token=token,
            params=params,
            data=data,
        )

    def _request_url_json(
        self,
        method: str,
        url: str,
        *,
        token: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_params = dict(params or {})
        request_data = dict(data or {})
        # The refresh endpoint requires a query token; other API calls use a header.
        headers = {"Authorization": f"Bearer {token}"}
        if url.endswith("/refresh_access_token"):
            request_params["access_token"] = token
            headers = {}

        try:
            response = self.session.request(
                method=method.upper(),
                url=url,
                params=request_params if method.upper() != "POST" else None,
                data=request_data if method.upper() == "POST" else None,
                timeout=self.settings.http_timeout_seconds,
                headers=headers,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise InstagramApiError("Network error calling Instagram Graph API.") from None

        try:
            payload = response.json()
        except ValueError:
            raise InstagramApiError(
                f"Instagram Graph API returned a non-JSON response: HTTP {response.status_code}."
            ) from None
        finally:
            response.close()

        if not isinstance(payload, dict):
            raise InstagramApiError("Instagram Graph API returned an invalid response shape.")
        if 200 <= response.status_code < 300 and "error" not in payload:
            return payload

        error = payload.get("error", {})
        if not isinstance(error, dict):
            error = {}
        message = error.get("message") or payload
        code = error.get("code")
        subcode = error.get("error_subcode")
        try:
            numeric_code = int(code) if code is not None else None
        except (TypeError, ValueError):
            numeric_code = None
        try:
            numeric_subcode = int(subcode) if subcode is not None else None
        except (TypeError, ValueError):
            numeric_subcode = None

        error_type = (
            InstagramTokenError
            if numeric_code == 190 or is_token_error(str(message))
            else InstagramApiError
        )
        raise error_type(
            f"Instagram authentication failed: token invalid or expired (code={numeric_code})."
            if error_type is InstagramTokenError
            else f"Instagram Graph API request failed (HTTP {response.status_code}, "
            f"code={numeric_code}, subcode={numeric_subcode}).",
            code=numeric_code,
            subcode=numeric_subcode,
            fbtrace_id=None,
        )


def is_token_error(message: str) -> bool:
    normalized = message.lower()
    return (
        "access token" in normalized
        or "oauth" in normalized
        or "expired" in normalized
        or "permissions error" in normalized
    )
