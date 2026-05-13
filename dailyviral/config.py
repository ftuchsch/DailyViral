from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
DEFAULT_ACCOUNTS_PATH = CONFIG_DIR / "accounts.yaml"
DEFAULT_ACCOUNTS_EXAMPLE_PATH = CONFIG_DIR / "accounts.example.yaml"
DEFAULT_DATABASE_PATH = DATA_DIR / "posts.sqlite"
DEFAULT_LOG_PATH = LOG_DIR / "dailyviral.log"
DEFAULT_DOTENV_PATH = PROJECT_ROOT / ".env"


class ConfigurationError(RuntimeError):
    """Raised when project or account configuration is missing or invalid."""


@dataclass(slots=True)
class AccountConfig:
    account_name: str
    ig_user_id: str
    access_token_env_var: str
    default_caption_template: str = ""
    default_hashtags: str = ""
    timezone: str = "UTC"


@dataclass(slots=True)
class AppSettings:
    auto_post_enabled: bool
    confirm_before_posting: bool
    dry_run: bool
    graph_api_version: str
    publishing_limit_buffer: int
    http_timeout_seconds: int
    database_path: Path
    log_path: Path
    dotenv_path: Path


@dataclass(slots=True)
class AppConfig:
    settings: AppSettings
    accounts: dict[str, AccountConfig]

    def get_account(self, account_name: str) -> AccountConfig:
        try:
            return self.accounts[account_name]
        except KeyError as exc:
            known = ", ".join(sorted(self.accounts)) or "<none configured>"
            raise ConfigurationError(
                f"Unknown account '{account_name}'. Configured accounts: {known}"
            ) from exc


def load_config(
    *,
    accounts_path: Path | None = None,
    dotenv_path: Path | None = None,
    require_accounts: bool = True,
) -> AppConfig:
    """Load runtime settings and account configuration."""
    dotenv_path = dotenv_path or DEFAULT_DOTENV_PATH
    accounts_path = accounts_path or DEFAULT_ACCOUNTS_PATH

    load_dotenv(dotenv_path)

    settings = AppSettings(
        auto_post_enabled=_env_bool("AUTO_POST_ENABLED", default=False),
        confirm_before_posting=_env_bool("CONFIRM_BEFORE_POSTING", default=True),
        dry_run=_env_bool("DRY_RUN", default=False),
        graph_api_version=os.getenv("GRAPH_API_VERSION", "v23.0"),
        publishing_limit_buffer=_env_int("PUBLISHING_LIMIT_BUFFER", default=1),
        http_timeout_seconds=_env_int("HTTP_TIMEOUT_SECONDS", default=20),
        database_path=DEFAULT_DATABASE_PATH,
        log_path=DEFAULT_LOG_PATH,
        dotenv_path=dotenv_path,
    )

    accounts: dict[str, AccountConfig] = {}
    if accounts_path.exists():
        payload = _load_yaml(accounts_path)
        accounts = _parse_accounts(payload)
    elif require_accounts:
        raise ConfigurationError(
            "Missing config/accounts.yaml. Run `python -m dailyviral init` first."
        )

    if require_accounts and not accounts:
        raise ConfigurationError(
            "No Instagram accounts configured in config/accounts.yaml."
        )

    return AppConfig(settings=settings, accounts=accounts)


def ensure_project_directories() -> list[Path]:
    created: list[Path] = []
    for path in (CONFIG_DIR, DATA_DIR, LOG_DIR):
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created.append(path)
    return created


def ensure_default_files(*, force: bool = False) -> list[Path]:
    """Create missing local files that should exist after init."""
    ensure_project_directories()

    created: list[Path] = []
    if DEFAULT_ACCOUNTS_EXAMPLE_PATH.exists():
        target = DEFAULT_ACCOUNTS_PATH
        if force or not target.exists():
            shutil.copyfile(DEFAULT_ACCOUNTS_EXAMPLE_PATH, target)
            created.append(target)

    if not DEFAULT_LOG_PATH.exists():
        DEFAULT_LOG_PATH.touch()
        created.append(DEFAULT_LOG_PATH)

    return created


def setup_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("dailyviral")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.propagate = False
    return logger


def require_access_token(account: AccountConfig) -> str:
    token = os.getenv(account.access_token_env_var, "").strip()
    if token:
        return token
    raise ConfigurationError(
        f"Environment variable '{account.access_token_env_var}' is not set."
    )


def _load_yaml(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Invalid YAML in {path}: {exc}") from exc


def _parse_accounts(payload: Any) -> dict[str, AccountConfig]:
    if not isinstance(payload, dict):
        raise ConfigurationError("config/accounts.yaml must contain a top-level mapping.")

    raw_accounts = payload.get("accounts", [])
    if raw_accounts is None:
        raw_accounts = []
    if not isinstance(raw_accounts, list):
        raise ConfigurationError("config/accounts.yaml must define an 'accounts' list.")

    accounts: dict[str, AccountConfig] = {}
    for index, raw_account in enumerate(raw_accounts, start=1):
        if not isinstance(raw_account, dict):
            raise ConfigurationError(f"Account entry #{index} must be a mapping.")

        required = ("account_name", "ig_user_id", "access_token_env_var")
        missing = [field for field in required if not str(raw_account.get(field, "")).strip()]
        if missing:
            raise ConfigurationError(
                f"Account entry #{index} is missing required fields: {', '.join(missing)}"
            )

        account = AccountConfig(
            account_name=str(raw_account["account_name"]).strip(),
            ig_user_id=str(raw_account["ig_user_id"]).strip(),
            access_token_env_var=str(raw_account["access_token_env_var"]).strip(),
            default_caption_template=str(raw_account.get("default_caption_template", "")).strip(),
            default_hashtags=str(raw_account.get("default_hashtags", "")).strip(),
            timezone=str(raw_account.get("timezone", "UTC")).strip() or "UTC",
        )
        accounts[account.account_name] = account

    return accounts


def _env_bool(name: str, *, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(
        f"Environment variable {name} must be true/false, got '{raw_value}'."
    )


def _env_int(name: str, *, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(
            f"Environment variable {name} must be an integer, got '{raw_value}'."
        ) from exc
