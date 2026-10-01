"""Shared account validation and account-local scheduling primitives."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import AccountConfig, ConfigurationError, getenv_stripped, is_placeholder_ig_user_id


def assert_account_publish_config(account: AccountConfig) -> None:
    if not account.instagram_username.strip():
        raise ConfigurationError(
            f"Account '{account.account_name}' is missing instagram_username in config/accounts.yaml."
        )
    if not account.ig_user_id.strip():
        raise ConfigurationError(
            f"Account '{account.account_name}' is missing ig_user_id in config/accounts.yaml."
        )
    if is_placeholder_ig_user_id(account.ig_user_id) or not account.ig_user_id.isdigit():
        raise ConfigurationError(
            f"Account '{account.account_name}' has a placeholder or invalid ig_user_id: {account.ig_user_id}"
        )
    if not account.access_token_env_var.strip():
        raise ConfigurationError(
            f"Account '{account.account_name}' is missing access_token_env_var in config/accounts.yaml."
        )


def require_env_value(name: str) -> str:
    value = getenv_stripped(name)
    if value:
        return value
    raise ConfigurationError(f"Environment variable '{name}' is required but not set.")


def env_flag(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def validate_date_string(value: str) -> str:
    return parse_date(value).isoformat()


def parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ConfigurationError(f"Invalid date '{value}'. Expected format YYYY-MM-DD.") from exc


def now_in_timezone(timezone_name: str) -> datetime:
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigurationError(f"Unknown timezone '{timezone_name}'.") from exc
    return datetime.now(zone)


def current_local_date(timezone_name: str) -> date:
    return now_in_timezone(timezone_name).date()


def today_in_timezone(timezone_name: str) -> str:
    return current_local_date(timezone_name).isoformat()
