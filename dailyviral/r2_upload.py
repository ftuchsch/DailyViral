"""Private R2 storage with verified public-access settings and bounded sharing."""

from __future__ import annotations

import re
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from uuid import uuid4

import boto3
import requests
from botocore.config import Config

from .config import ConfigurationError, getenv_stripped
from .http import MAX_VIDEO_BYTES, write_video_chunks
from .security import mask_github_secret

R2_REQUIRED_ENV_VARS = (
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_ACCOUNT_ID",
    "R2_BUCKET_NAME",
    "R2_SECURITY_API_TOKEN",
    "R2_BASE_VIDEO_KEY",
)


@dataclass(slots=True)
class R2Config:
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    account_id: str = field(repr=False)
    bucket_name: str
    security_api_token: str = field(repr=False)
    base_video_key: str
    signed_url_ttl_seconds: int = 900

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-fA-F0-9]{32}", self.account_id):
            raise ConfigurationError("R2_ACCOUNT_ID must contain 32 hexadecimal characters.")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", self.bucket_name):
            raise ConfigurationError("R2_BUCKET_NAME is invalid.")
        validate_object_key(self.base_video_key, prefix="base/")
        if not 300 <= self.signed_url_ttl_seconds <= 1800:
            raise ConfigurationError("R2_SIGNED_URL_TTL_SECONDS must be between 300 and 1800.")

    @property
    def endpoint_url(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"


def validate_object_key(key: str, *, prefix: str) -> None:
    if (
        not key.startswith(prefix)
        or not key.endswith(".mp4")
        or not re.fullmatch(r"[A-Za-z0-9/_\-.]+", key)
        or any(part in {"", ".", ".."} for part in key.split("/"))
    ):
        raise ConfigurationError(f"R2 object key must be an MP4 under {prefix} with no traversal.")


def load_r2_config_from_env() -> R2Config:
    if getenv_stripped("R2_PUBLIC_BASE_URL"):
        raise ConfigurationError("Remove R2_PUBLIC_BASE_URL and disable all public bucket access.")
    values = {name: getenv_stripped(name) for name in R2_REQUIRED_ENV_VARS}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigurationError(f"Missing private R2 configuration: {', '.join(missing)}.")
    try:
        ttl = int(getenv_stripped("R2_SIGNED_URL_TTL_SECONDS") or "900")
    except ValueError:
        raise ConfigurationError("R2_SIGNED_URL_TTL_SECONDS must be an integer.") from None
    return R2Config(
        access_key_id=values["R2_ACCESS_KEY_ID"],
        secret_access_key=values["R2_SECRET_ACCESS_KEY"],
        account_id=values["R2_ACCOUNT_ID"],
        bucket_name=values["R2_BUCKET_NAME"],
        security_api_token=values["R2_SECURITY_API_TOKEN"],
        base_video_key=values["R2_BASE_VIDEO_KEY"],
        signed_url_ttl_seconds=ttl,
    )


def build_generated_object_key(*, day_number: int, local_date: date) -> str:
    if day_number < 1:
        raise ConfigurationError("Day number must be positive.")
    return f"generated/day-{day_number}-{local_date.isoformat()}-{uuid4().hex}.mp4"


class R2Storage:
    """Use a dedicated bucket; never enable public access or issue write links."""

    def __init__(self, config: R2Config):
        self.config = config
        self.client = boto3.client(
            "s3",
            endpoint_url=config.endpoint_url,
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
            region_name="auto",
            config=Config(
                signature_version="s3v4",
                proxies={},
                connect_timeout=10,
                read_timeout=30,
                retries={"max_attempts": 3},
                s3={"addressing_style": "path"},
            ),
        )

    def assert_bucket_private(self) -> None:
        """Fail closed if r2.dev/custom domains are enabled or unverifiable.

        Workers, Pages, other credentials, and account administrators require a
        separate account review; these two endpoints cannot audit those paths.
        """
        base = (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{self.config.account_id}/r2/buckets/{self.config.bucket_name}/domains"
        )
        results = {}
        with requests.Session() as session:
            session.trust_env = False
            for kind in ("managed", "custom"):
                try:
                    with session.get(
                        f"{base}/{kind}",
                        timeout=20,
                        allow_redirects=False,
                        headers={"Authorization": f"Bearer {self.config.security_api_token}"},
                    ) as response:
                        if response.status_code != 200:
                            raise ConfigurationError("Could not verify R2 public-access settings.")
                        payload = response.json()
                    if not isinstance(payload, dict) or payload.get("success") is not True:
                        raise ConfigurationError("Could not verify R2 public-access settings.")
                    results[kind] = payload["result"]
                except (requests.RequestException, ValueError, KeyError):
                    raise ConfigurationError(
                        "Could not verify R2 public-access settings."
                    ) from None
        managed, custom = results["managed"], results["custom"]
        if not isinstance(managed, dict) or managed.get("enabled") is not False:
            raise ConfigurationError("R2 r2.dev public access is enabled or unverifiable.")
        domains = custom.get("domains") if isinstance(custom, dict) else None
        if not isinstance(domains, list) or any(
            not isinstance(domain, dict) or domain.get("enabled") is not False for domain in domains
        ):
            raise ConfigurationError("R2 custom domain public access is enabled or unverifiable.")

    def verify_source(self) -> None:
        self.assert_bucket_private()
        try:
            metadata = self.client.head_object(
                Bucket=self.config.bucket_name,
                Key=self.config.base_video_key,
            )
            if not 0 < metadata["ContentLength"] <= MAX_VIDEO_BYTES:
                raise ValueError
        except Exception:
            raise ConfigurationError(
                "Private source is missing, unreadable, empty, or exceeds 256 MiB."
            ) from None

    def download_base_video(self, destination: Path) -> Path:
        self.verify_source()
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = self.client.get_object(
                Bucket=self.config.bucket_name,
                Key=self.config.base_video_key,
            )
            with closing(result["Body"]) as body:
                if not 0 < result["ContentLength"] <= MAX_VIDEO_BYTES:
                    raise ConfigurationError("Private source video exceeds the size limit.")
                write_video_chunks(body.iter_chunks(chunk_size=512 * 1024), destination)
        except Exception:
            destination.unlink(missing_ok=True)
            raise ConfigurationError("Private R2 base-video download failed.") from None
        return destination

    def upload_video(self, *, file_path: Path, object_key: str) -> str:
        validate_object_key(object_key, prefix="generated/")
        if not file_path.is_file() or not 0 < file_path.stat().st_size <= MAX_VIDEO_BYTES:
            raise ConfigurationError("Rendered video must be nonempty and no larger than 256 MiB.")
        self.assert_bucket_private()
        try:
            self.client.upload_file(
                str(file_path),
                self.config.bucket_name,
                object_key,
                ExtraArgs={"ContentType": "video/mp4", "CacheControl": "private, no-store"},
            )
            url = self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.config.bucket_name, "Key": object_key},
                ExpiresIn=self.config.signed_url_ttl_seconds,
                HttpMethod="GET",
            )
            mask_github_secret(url)
            return url
        except Exception:
            self.delete_generated_video(object_key)
            raise ConfigurationError("Private R2 upload or signing failed.") from None

    def delete_generated_video(self, object_key: str) -> None:
        validate_object_key(object_key, prefix="generated/")
        try:
            self.client.delete_object(Bucket=self.config.bucket_name, Key=object_key)
        except Exception:
            raise ConfigurationError(
                "R2 temporary-video cleanup failed; remove it in Cloudflare. "
                "The signed link will expire; configure a generated/ lifecycle rule as a backup."
            ) from None
