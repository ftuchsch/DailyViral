from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import ConfigurationError, getenv_stripped


@dataclass(slots=True)
class R2Config:
    access_key_id: str
    secret_access_key: str
    account_id: str
    bucket_name: str
    public_base_url: str

    @property
    def endpoint_url(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"


def load_r2_config_from_env() -> R2Config:
    values: dict[str, str] = {}
    for name in (
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_ACCOUNT_ID",
        "R2_BUCKET_NAME",
        "R2_PUBLIC_BASE_URL",
    ):
        value = getenv_stripped(name)
        if not value:
            raise ConfigurationError(
                f"Environment variable '{name}' is required when ENABLE_VIDEO_OVERLAY=true."
            )
        values[name] = value

    return R2Config(
        access_key_id=values["R2_ACCESS_KEY_ID"],
        secret_access_key=values["R2_SECRET_ACCESS_KEY"],
        account_id=values["R2_ACCOUNT_ID"],
        bucket_name=values["R2_BUCKET_NAME"],
        public_base_url=values["R2_PUBLIC_BASE_URL"].rstrip("/"),
    )


def build_generated_object_key(*, day_number: int, local_date: date) -> str:
    return f"generated/day-{day_number}-{local_date.isoformat()}.mp4"


def upload_video_to_r2(
    *,
    file_path: Path,
    object_key: str,
    config: R2Config,
) -> str:
    try:
        import boto3

        client = boto3.client(
            "s3",
            endpoint_url=config.endpoint_url,
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
            region_name="auto",
        )
        client.upload_file(
            str(file_path),
            config.bucket_name,
            object_key,
            ExtraArgs={"ContentType": "video/mp4"},
        )
    except Exception as exc:
        raise ConfigurationError(
            f"Cloudflare R2 upload failed for object '{object_key}': {exc}"
        ) from exc
    return f"{config.public_base_url}/{object_key}"
