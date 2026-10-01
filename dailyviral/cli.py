from __future__ import annotations

import argparse
import json
import logging
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as datetime_time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import __version__
from .analytics import build_report
from .captions import CaptionResolution, prepare_new_post, resolve_caption
from .config import (
    DEFAULT_ACCOUNTS_PATH,
    AccountConfig,
    AppConfig,
    AppSettings,
    ConfigurationError,
    ensure_default_files,
    ensure_project_directories,
    getenv_stripped,
    is_placeholder_ig_user_id,
    load_config,
    setup_logging,
)
from .gha import handle_gha_post_daily, handle_gha_schedule_readiness
from .instagram import (
    InstagramApiError,
    InstagramClient,
)
from .queue import PostRecord, QueueStore
from .runtime import (
    assert_account_publish_config,
    current_local_date,
    parse_date,
    require_env_value,
    today_in_timezone,
    validate_date_string,
)
from .security import redact_sensitive
from .url_validation import UrlValidationResult, validate_public_video_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQUIRED_CHECK_ENV_VARS = (
    "AUTO_POST_ENABLED",
    "DRY_RUN",
    "INSTAGRAM_USERNAME",
    "INSTAGRAM_ACCESS_TOKEN",
    "DAILYVIRAL_VIDEO_URL",
)
ACCOUNT_REQUIRED_COMMANDS = {
    "add",
    "add-from-file",
    "add-real-post",
    "check-config",
    "dry-run",
    "make-cron",
    "make-launchd",
    "posted",
    "post-next",
    "run-due",
    "schedule-days",
    "upcoming",
}


@dataclass(slots=True)
class PostPreview:
    post: PostRecord
    caption_resolution: CaptionResolution
    validation: UrlValidationResult


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    if args.command == "init":
        return handle_init()

    logger: logging.Logger | None = None
    try:
        config = load_config(require_accounts=args.command in ACCOUNT_REQUIRED_COMMANDS)
        logger = setup_logging(config.settings.log_path)

        if args.command == "gha-post-daily":
            return handle_gha_post_daily(args, config.settings, logger)
        if args.command == "gha-schedule-readiness":
            return handle_gha_schedule_readiness(args, config.settings)

        queue_store = QueueStore(config.settings.database_path)
        queue_store.initialize()

        if args.command == "check-config":
            return handle_check_config(args, config)
        if args.command == "add":
            return handle_add(args, config, queue_store)
        if args.command == "add-real-post":
            return handle_add_real_post(args, config, queue_store)
        if args.command == "add-from-file":
            return handle_add_from_file(args, config, queue_store)
        if args.command == "schedule-days":
            return handle_schedule_days(args, config, queue_store)
        if args.command == "dry-run":
            return handle_dry_run(args, config, queue_store)
        if args.command == "run-due":
            return handle_run_due(args, config, queue_store, logger)
        if args.command == "post-next":
            return handle_post_next(args, config, queue_store, logger)
        if args.command == "status":
            return handle_status(args, queue_store)
        if args.command == "upcoming":
            return handle_upcoming(args, config, queue_store)
        if args.command == "posted":
            return handle_posted(args, queue_store)
        if args.command == "analytics":
            return handle_analytics(args, queue_store)
        if args.command == "make-launchd":
            return handle_make_launchd(args, config)
        if args.command == "make-cron":
            return handle_make_cron(args, config)
        parser.print_help()
        return 1
    except ConfigurationError as exc:
        if logger:
            logger.error(str(exc))
        print(f"Configuration error: {redact_sensitive(exc)}")
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dailyviral",
        description="Schedule and publish Instagram Reels through the official Meta API.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser(
        "init", help="Create local config/data/log paths and initialize the project."
    )

    check_parser = subparsers.add_parser(
        "check-config",
        help="Verify local .env values, account config, and public video URL reachability.",
    )
    check_parser.add_argument("account_name")

    add_parser = subparsers.add_parser("add", help="Interactively add a queued post.")
    add_parser.add_argument("--account-name")
    add_parser.add_argument("--video-url")
    add_parser.add_argument("--caption")
    add_parser.add_argument("--hashtags")
    add_parser.add_argument("--scheduled-date")

    add_real_parser = subparsers.add_parser(
        "add-real-post",
        help="Queue a post using DAILYVIRAL_VIDEO_URL from .env and the selected account config.",
    )
    add_real_parser.add_argument("account_name")
    add_real_parser.add_argument("--scheduled-date")

    add_file_parser = subparsers.add_parser(
        "add-from-file",
        help="Add a queued post from a JSON file.",
    )
    add_file_parser.add_argument("path", help="Path to a JSON file describing one post.")

    schedule_parser = subparsers.add_parser(
        "schedule-days",
        help="Queue one post per day for the next N days using DAILYVIRAL_VIDEO_URL.",
    )
    schedule_parser.add_argument("account_name")
    schedule_parser.add_argument("--days", type=int, required=True)
    schedule_parser.add_argument("--start-date")

    dry_run_parser = subparsers.add_parser(
        "dry-run",
        help="Validate and preview the next due post without making Instagram API calls.",
    )
    dry_run_parser.add_argument("account_name")

    run_due_parser = subparsers.add_parser(
        "run-due",
        help="Post the oldest due queued post, or dry-run when posting is disabled.",
    )
    run_due_parser.add_argument("account_name")
    run_due_parser.add_argument("--yes", action="store_true", help="Skip interactive confirmation.")
    run_due_parser.add_argument(
        "--force",
        action="store_true",
        help="Allow another post even if one already succeeded today.",
    )

    post_parser = subparsers.add_parser(
        "post-next",
        help="Validate and post the next due Reel for an account.",
    )
    post_parser.add_argument("account_name")
    post_parser.add_argument("--dry-run", action="store_true")
    post_parser.add_argument("--yes", action="store_true", help="Skip interactive confirmation.")
    post_parser.add_argument(
        "--force",
        action="store_true",
        help="Allow another post even if one already succeeded today.",
    )

    status_parser = subparsers.add_parser("status", help="Show queue status and recent posts.")
    status_parser.add_argument("--account-name")
    status_parser.add_argument("--limit", type=int, default=20)

    upcoming_parser = subparsers.add_parser(
        "upcoming",
        help="Show the next queued posts for an account.",
    )
    upcoming_parser.add_argument("account_name")
    upcoming_parser.add_argument("--limit", type=int, default=10)

    posted_parser = subparsers.add_parser(
        "posted",
        help="Show recently posted posts for an account.",
    )
    posted_parser.add_argument("account_name")
    posted_parser.add_argument("--limit", type=int, default=10)

    analytics_parser = subparsers.add_parser(
        "analytics",
        help="Show local queue analytics for posted and failed posts.",
    )
    analytics_parser.add_argument("--account-name")

    launchd_parser = subparsers.add_parser(
        "make-launchd",
        help="Generate a macOS launchd job for the daily runner.",
    )
    launchd_parser.add_argument("account_name")
    launchd_parser.add_argument("--time", required=True)
    launchd_parser.add_argument("--install", action="store_true")

    cron_parser = subparsers.add_parser(
        "make-cron",
        help="Print a cron line for the daily runner.",
    )
    cron_parser.add_argument("account_name")
    cron_parser.add_argument("--time", required=True)

    gha_parser = subparsers.add_parser(
        "gha-post-daily",
        help="Stateless GitHub Actions daily posting mode using env vars only.",
    )
    gha_parser.add_argument("account_name")
    gha_parser.add_argument(
        "--force", action="store_true", help="Bypass recent-caption duplicate checks."
    )

    readiness_parser = subparsers.add_parser(
        "gha-schedule-readiness",
        help="Non-posting preflight report for the next GitHub Actions scheduled overlay post.",
    )
    readiness_parser.add_argument("account_name")

    return parser


def handle_init() -> int:
    ensure_project_directories()
    config = load_config(require_accounts=False)
    created = ensure_default_files()
    queue_store = QueueStore(config.settings.database_path)
    queue_store.initialize()

    print("Initialized DailyViral.")
    if created:
        print("Created:")
        for path in created:
            try:
                display_path = path.relative_to(Path.cwd())
            except ValueError:
                display_path = path
            print(f"  - {display_path}")
    print(f"SQLite queue: {config.settings.database_path}")
    print(f"Accounts config: {DEFAULT_ACCOUNTS_PATH}")
    return 0


def handle_check_config(args: argparse.Namespace, config: AppConfig) -> int:
    account = config.get_account(args.account_name)
    env_username = getenv_stripped("INSTAGRAM_USERNAME")
    access_token_value = getenv_stripped(account.access_token_env_var)
    dailyviral_video_url = getenv_stripped("DAILYVIRAL_VIDEO_URL")

    checks: list[tuple[bool, str, str]] = []
    for name in REQUIRED_CHECK_ENV_VARS:
        value = getenv_stripped(name)
        detail = "set" if value else "missing"
        checks.append((bool(value), f"{name} is set", detail))

    checks.append((True, "Selected account exists", account.account_name))
    checks.append(
        (
            bool(account.instagram_username),
            "Account config username is present",
            account.instagram_username or "missing",
        )
    )
    checks.append(
        (
            bool(
                account.instagram_username
                and env_username
                and account.instagram_username == env_username
            ),
            "Account username matches INSTAGRAM_USERNAME",
            f"config={account.instagram_username or '<missing>'}, env={env_username or '<missing>'}",
        )
    )
    checks.append(
        (
            bool(account.ig_user_id)
            and account.ig_user_id.isdigit()
            and not is_placeholder_ig_user_id(account.ig_user_id),
            "IG User ID is present and not a placeholder",
            account.ig_user_id or "missing",
        )
    )
    checks.append(
        (
            bool(account.access_token_env_var),
            "Access token env var is configured",
            account.access_token_env_var or "missing",
        )
    )
    checks.append(
        (
            bool(access_token_value),
            "Access token env var resolves to a real value",
            account.access_token_env_var,
        )
    )

    validation: UrlValidationResult | None = None
    if dailyviral_video_url:
        validation = validate_public_video_url(
            dailyviral_video_url,
            timeout_seconds=config.settings.http_timeout_seconds,
        )
        checks.append(
            (validation.ok, "DAILYVIRAL_VIDEO_URL is publicly reachable", validation.message)
        )
    else:
        checks.append((False, "DAILYVIRAL_VIDEO_URL is publicly reachable", "missing"))

    overall_ok = all(result for result, _, _ in checks)

    print(f"Check Config: {account.account_name}")
    print()
    print("Environment")
    print(f"  INSTAGRAM_ACCESS_TOKEN is set: {'yes' if bool(access_token_value) else 'no'}")
    print(f"  AUTO_POST_ENABLED: {getenv_stripped('AUTO_POST_ENABLED') or '<missing>'}")
    print(f"  DRY_RUN: {getenv_stripped('DRY_RUN') or '<missing>'}")
    print()
    print("Checks")
    for passed, label, detail in checks:
        _print_report_line(passed, label, detail)

    if validation and validation.final_url:
        print()
        print("Video URL")
        print("  Final URL: [redacted]")
        print(f"  HTTP status: {validation.http_status}")
        print(f"  Content-Type: {validation.content_type or '<unknown>'}")
        if validation.warnings:
            print(f"  Warnings: {'; '.join(validation.warnings)}")

    print()
    print(f"Overall: {'PASS' if overall_ok else 'FAIL'}")
    return 0 if overall_ok else 1


def handle_add(args: argparse.Namespace, config: AppConfig, queue_store: QueueStore) -> int:
    interactive = sys.stdin.isatty()
    account_name = args.account_name or _required_value(
        "Account name",
        interactive=interactive,
    )
    account = config.get_account(account_name)
    video_url = args.video_url or _required_value(
        "Public video URL",
        interactive=interactive,
    )
    caption = (
        args.caption
        if args.caption is not None
        else (prompt("Caption (blank to use template)", allow_blank=True) if interactive else "")
    )
    hashtags = (
        args.hashtags
        if args.hashtags is not None
        else (
            prompt("Hashtags (blank to use account defaults)", allow_blank=True)
            if interactive
            else ""
        )
    )
    scheduled_date = (
        args.scheduled_date
        if args.scheduled_date is not None
        else (
            prompt("Scheduled date (YYYY-MM-DD, blank for today)", allow_blank=True)
            if interactive
            else ""
        )
    )

    if not scheduled_date:
        scheduled_date = today_in_timezone(account.timezone)

    prepared = prepare_new_post(
        account=account,
        queue_store=queue_store,
        caption=caption,
        hashtags=hashtags,
    )
    post = queue_store.add_post(
        account_name=account_name,
        video_url=video_url,
        caption=prepared.caption,
        hashtags=prepared.hashtags,
        scheduled_date=validate_date_string(scheduled_date),
    )
    print(f"Queued post #{post.id} for {post.account_name} on {post.scheduled_date}.")
    return 0


def handle_add_real_post(
    args: argparse.Namespace, config: AppConfig, queue_store: QueueStore
) -> int:
    account = config.get_account(args.account_name)
    assert_account_publish_config(account)

    video_url = require_env_value("DAILYVIRAL_VIDEO_URL")
    require_env_value(account.access_token_env_var)
    scheduled_date = (
        validate_date_string(args.scheduled_date)
        if args.scheduled_date
        else today_in_timezone(account.timezone)
    )
    prepared = prepare_new_post(
        account=account,
        queue_store=queue_store,
        caption="",
        hashtags="",
    )
    post = queue_store.add_post(
        account_name=account.account_name,
        video_url=video_url,
        caption=prepared.caption,
        hashtags=prepared.hashtags,
        scheduled_date=scheduled_date,
        status="queued",
    )

    print("Queued real post")
    _print_post_details(post=post, account=account, final_caption=prepared.final_caption)
    return 0


def handle_add_from_file(
    args: argparse.Namespace, config: AppConfig, queue_store: QueueStore
) -> int:
    input_path = Path(args.path)
    with input_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    account_name = str(payload["account_name"]).strip()
    account = config.get_account(account_name)
    prepared = prepare_new_post(
        account=account,
        queue_store=queue_store,
        caption=str(payload.get("caption", "")).strip(),
        hashtags=str(payload.get("hashtags", "")).strip(),
    )
    post = queue_store.add_post(
        account_name=account_name,
        video_url=str(payload["video_url"]).strip(),
        caption=prepared.caption,
        hashtags=prepared.hashtags,
        scheduled_date=validate_date_string(str(payload["scheduled_date"]).strip()),
    )
    print(f"Queued post #{post.id} from {input_path}.")
    return 0


def handle_schedule_days(
    args: argparse.Namespace, config: AppConfig, queue_store: QueueStore
) -> int:
    account = config.get_account(args.account_name)
    assert_account_publish_config(account)

    created = schedule_days_for_account(
        account=account,
        queue_store=queue_store,
        days=args.days,
        start_date=args.start_date,
        video_url=require_env_value("DAILYVIRAL_VIDEO_URL"),
    )

    print(f"Created {len(created)} queued posts for {account.account_name}.")
    for post in created:
        print(f"  {post.scheduled_date} | {post.caption or '<empty>'} | {post.status}")
    return 0


def handle_dry_run(args: argparse.Namespace, config: AppConfig, queue_store: QueueStore) -> int:
    account = config.get_account(args.account_name)
    return _run_dry_run(account=account, config=config, queue_store=queue_store)


def handle_run_due(
    args: argparse.Namespace,
    config: AppConfig,
    queue_store: QueueStore,
    logger: logging.Logger,
) -> int:
    account = config.get_account(args.account_name)
    return _execute_due_post(
        account=account,
        config=config,
        queue_store=queue_store,
        logger=logger,
        yes=args.yes,
        force=args.force,
        explicit_dry_run=False,
    )


def handle_post_next(
    args: argparse.Namespace,
    config: AppConfig,
    queue_store: QueueStore,
    logger: logging.Logger,
) -> int:
    account = config.get_account(args.account_name)
    return _execute_due_post(
        account=account,
        config=config,
        queue_store=queue_store,
        logger=logger,
        yes=args.yes,
        force=args.force,
        explicit_dry_run=args.dry_run,
    )


def handle_status(args: argparse.Namespace, queue_store: QueueStore) -> int:
    counts = queue_store.status_counts(account_name=args.account_name)
    posts = queue_store.list_posts(limit=args.limit, account_name=args.account_name)

    print("Queue status")
    if args.account_name:
        print(f"Account: {args.account_name}")
    if counts:
        for status, total in sorted(counts.items()):
            print(f"  {status}: {total}")
    else:
        print("  No posts yet.")

    if posts:
        print("\nRecent posts")
        for post in posts:
            summary = (
                f"#{post.id} | {post.account_name} | {post.scheduled_date} | "
                f"{post.status} | [video URL redacted]"
            )
            print(summary)
            if post.error_message:
                print(f"    error: {post.error_message}")
            if post.instagram_permalink:
                print(f"    permalink: {post.instagram_permalink}")
    return 0


def handle_upcoming(args: argparse.Namespace, config: AppConfig, queue_store: QueueStore) -> int:
    account = config.get_account(args.account_name)
    posts = queue_store.upcoming_posts(
        account.account_name,
        limit=args.limit,
        statuses=("queued", "ready"),
    )

    print(f"Upcoming posts: {account.account_name}")
    if not posts:
        print("  No queued posts.")
        return 0

    for post in posts:
        print(f"  {post.scheduled_date} | {post.status} | {post.caption or '<empty>'}")
    return 0


def handle_posted(args: argparse.Namespace, queue_store: QueueStore) -> int:
    posts = queue_store.recent_posted_posts(args.account_name, limit=args.limit)

    print(f"Posted posts: {args.account_name}")
    if not posts:
        print("  No posted posts.")
        return 0

    for post in posts:
        print(
            f"  {post.posted_at or '<unknown>'} | {post.instagram_media_id or '<no media id>'} | "
            f"{post.instagram_permalink or '<no permalink>'}"
        )
    return 0


def handle_analytics(args: argparse.Namespace, queue_store: QueueStore) -> int:
    report = build_report(queue_store, account_name=args.account_name)

    print("Analytics")
    if args.account_name:
        print(f"Account: {args.account_name}")

    print("Status totals")
    if report.status_totals:
        for status, total in sorted(report.status_totals.items()):
            print(f"  {status}: {total}")
    else:
        print("  No posts yet.")

    print("\nPer-account totals")
    if report.account_status_totals:
        for row in report.account_status_totals:
            print(f"  {row['account_name']} | {row['status']} | {row['total']}")
    else:
        print("  No per-account data yet.")

    print("\nRecent successes")
    if report.recent_successes:
        for post in report.recent_successes:
            print(
                f"  #{post.id} | {post.account_name} | {post.posted_at} | "
                f"{post.instagram_permalink or '<no permalink>'}"
            )
    else:
        print("  No successful posts yet.")

    print("\nRecent failures")
    if report.recent_failures:
        for post in report.recent_failures:
            print(f"  #{post.id} | {post.account_name} | {post.created_at} | {post.error_message}")
    else:
        print("  No failed posts.")
    return 0


def handle_make_launchd(args: argparse.Namespace, config: AppConfig) -> int:
    config.get_account(args.account_name)
    run_time = parse_clock_time(args.time)
    generated_path = write_launchd_plist(
        project_root=PROJECT_ROOT,
        account_name=args.account_name,
        run_time=run_time,
        python_executable=Path(sys.executable),
    )

    print(f"Generated launchd plist: {generated_path}")
    if args.install:
        install_path = install_launchd_plist(
            account_name=args.account_name,
            generated_path=generated_path,
        )
        print(f"Installed launchd plist: {install_path}")
        print("Logs:")
        print(f"  {PROJECT_ROOT / 'logs' / 'launchd.out.log'}")
        print(f"  {PROJECT_ROOT / 'logs' / 'launchd.err.log'}")
    return 0


def handle_make_cron(args: argparse.Namespace, config: AppConfig) -> int:
    config.get_account(args.account_name)
    run_time = parse_clock_time(args.time)
    cron_line = build_cron_line(
        project_root=PROJECT_ROOT,
        account_name=args.account_name,
        run_time=run_time,
        python_executable=Path(sys.executable),
    )
    print(cron_line)
    return 0


def schedule_days_for_account(
    *,
    account: AccountConfig,
    queue_store: QueueStore,
    days: int,
    start_date: str | None,
    video_url: str,
) -> list[PostRecord]:
    if days <= 0:
        raise ConfigurationError("--days must be greater than zero.")

    start = determine_schedule_start_date(
        account=account,
        queue_store=queue_store,
        start_date=start_date,
    )
    current_date = start
    created: list[PostRecord] = []

    while len(created) < days:
        scheduled_date = current_date.isoformat()
        if queue_store.scheduled_date_exists(account.account_name, scheduled_date):
            current_date += timedelta(days=1)
            continue

        prepared = prepare_new_post(
            account=account,
            queue_store=queue_store,
            caption="",
            hashtags="",
        )
        post = queue_store.add_post(
            account_name=account.account_name,
            video_url=video_url,
            caption=prepared.caption,
            hashtags=prepared.hashtags,
            scheduled_date=scheduled_date,
            status="queued",
        )
        created.append(post)
        current_date += timedelta(days=1)

    return created


def determine_schedule_start_date(
    *,
    account: AccountConfig,
    queue_store: QueueStore,
    start_date: str | None,
) -> date:
    if start_date:
        return parse_date(start_date)

    local_today = current_local_date(account.timezone)
    tomorrow = local_today + timedelta(days=1)
    latest_future = queue_store.latest_scheduled_date(
        account.account_name,
        statuses=("queued", "ready"),
        scheduled_after=local_today.isoformat(),
    )
    if latest_future:
        return parse_date(latest_future) + timedelta(days=1)
    return tomorrow


def parse_clock_time(value: str) -> datetime_time:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise ConfigurationError(f"Invalid time '{value}'. Expected format HH:MM.") from exc


def write_launchd_plist(
    *,
    project_root: Path,
    account_name: str,
    run_time: datetime_time,
    python_executable: Path,
) -> Path:
    output_path = launchd_generated_path(project_root=project_root, account_name=account_name)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = build_launchd_payload(
        project_root=project_root,
        account_name=account_name,
        run_time=run_time,
        python_executable=python_executable,
    )
    with output_path.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=False)
    return output_path


def install_launchd_plist(*, account_name: str, generated_path: Path) -> Path:
    if sys.platform != "darwin":
        raise ConfigurationError("--install is only supported on macOS.")

    install_path = launchd_install_path(account_name=account_name)
    install_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(generated_path, install_path)

    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["launchctl", "bootout", domain, str(install_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(install_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ConfigurationError(
            "launchctl bootstrap failed: "
            f"{result.stderr.strip() or result.stdout.strip() or 'unknown error'}"
        )
    return install_path


def build_launchd_payload(
    *,
    project_root: Path,
    account_name: str,
    run_time: datetime_time,
    python_executable: Path,
) -> dict[str, object]:
    label = launchd_label(account_name)
    stdout_log = project_root / "logs" / "launchd.out.log"
    stderr_log = project_root / "logs" / "launchd.err.log"

    return {
        "Label": label,
        "ProgramArguments": [
            str(python_executable),
            "-m",
            "dailyviral",
            "run-due",
            account_name,
            "--yes",
        ],
        "WorkingDirectory": str(project_root),
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
            "PYTHONUNBUFFERED": "1",
        },
        "StartCalendarInterval": {
            "Hour": run_time.hour,
            "Minute": run_time.minute,
        },
        "StandardOutPath": str(stdout_log),
        "StandardErrorPath": str(stderr_log),
        "RunAtLoad": False,
    }


def build_cron_line(
    *,
    project_root: Path,
    account_name: str,
    run_time: datetime_time,
    python_executable: Path,
) -> str:
    out_log = project_root / "logs" / "launchd.out.log"
    err_log = project_root / "logs" / "launchd.err.log"
    repo = shlex.quote(str(project_root))
    python_cmd = shlex.quote(str(python_executable))
    out_path = shlex.quote(str(out_log))
    err_path = shlex.quote(str(err_log))
    account = shlex.quote(account_name)
    return (
        f"{run_time.minute} {run_time.hour} * * * "
        f"cd {repo} && {python_cmd} -m dailyviral run-due {account} --yes "
        f">> {out_path} 2>> {err_path}"
    )


def launchd_label(account_name: str) -> str:
    return f"com.dailyviral.{account_name}"


def launchd_generated_path(*, project_root: Path, account_name: str) -> Path:
    return project_root / "data" / "launchd" / f"{launchd_label(account_name)}.plist"


def launchd_install_path(*, account_name: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{launchd_label(account_name)}.plist"


def _run_dry_run(account: AccountConfig, config: AppConfig, queue_store: QueueStore) -> int:
    post = _next_due_post(account=account, queue_store=queue_store)
    if post is None:
        print("No due posts")
        return 0

    preview = _build_post_preview(
        account=account, config=config, queue_store=queue_store, post=post
    )
    print_dry_run(
        post=post,
        account=account,
        settings=config.settings,
        validation=preview.validation,
        final_caption=preview.caption_resolution.final_caption,
    )
    return 0 if preview.validation.ok else 1


def _execute_due_post(
    *,
    account: AccountConfig,
    config: AppConfig,
    queue_store: QueueStore,
    logger: logging.Logger,
    yes: bool,
    force: bool,
    explicit_dry_run: bool,
) -> int:
    assert_account_publish_config(account)

    post = _next_due_post(account=account, queue_store=queue_store)
    if post is None:
        print("No due posts")
        return 0

    preview = _build_post_preview(
        account=account, config=config, queue_store=queue_store, post=post
    )
    effective_dry_run = bool(
        explicit_dry_run or config.settings.dry_run or not config.settings.auto_post_enabled
    )
    if effective_dry_run:
        reason = []
        if explicit_dry_run:
            reason.append("--dry-run was requested")
        if config.settings.dry_run:
            reason.append("DRY_RUN=true")
        if not config.settings.auto_post_enabled:
            reason.append("AUTO_POST_ENABLED=false")
        print(f"Dry-run mode active: {', '.join(reason)}")
        if _account_already_posted_today(account, queue_store) and not force:
            print(
                "A post already succeeded today for this account. Live posting would stop without --force."
            )
        print_dry_run(
            post=post,
            account=account,
            settings=config.settings,
            validation=preview.validation,
            final_caption=preview.caption_resolution.final_caption,
        )
        return 0 if preview.validation.ok else 1

    unsafe_reason = _posting_safety_reason(
        account=account, queue_store=queue_store, post=post, force=force
    )
    if unsafe_reason:
        print(unsafe_reason)
        return 0

    if not preview.validation.ok:
        queue_store.mark_failed(post.id, preview.validation.message)
        logger.error(
            "Validation failed for post %s account=%s url=%s message=%s",
            post.id,
            account.account_name,
            post.video_url,
            preview.validation.message,
        )
        print(f"Validation failed for post #{post.id}: {preview.validation.message}")
        return 1

    post = queue_store.update_post(
        post.id,
        caption=preview.caption_resolution.caption,
        hashtags=preview.caption_resolution.hashtags,
        status="ready",
        error_message=None,
    )

    if config.settings.confirm_before_posting and not yes:
        print_dry_run(
            post=post,
            account=account,
            settings=config.settings,
            validation=preview.validation,
            final_caption=preview.caption_resolution.final_caption,
        )
        if input("Proceed with publishing this Reel? [y/N]: ").strip().lower() not in {"y", "yes"}:
            print("Aborted.")
            return 0

    try:
        queue_store.set_status(post.id, status="posting", error_message=None)
        logger.info(
            "Posting Reel account=%s post_id=%s video_url=%s caption=%s",
            account.account_name,
            post.id,
            post.video_url,
            preview.caption_resolution.final_caption,
        )
        client = InstagramClient(config.settings, logger=logger)
        result = client.publish_reel(
            account=account,
            video_url=post.video_url,
            caption=preview.caption_resolution.final_caption,
        )
    except (ConfigurationError, InstagramApiError) as exc:
        queue_store.mark_failed(post.id, str(exc))
        logger.error(
            "Posting failed account=%s post_id=%s error=%s",
            account.account_name,
            post.id,
            exc,
        )
        print(f"Posting failed for post #{post.id}: {exc}")
        return 1

    updated = queue_store.mark_posted(
        post.id,
        instagram_creation_id=result.creation_id,
        instagram_media_id=result.media_id,
        instagram_permalink=result.permalink,
    )
    logger.info(
        "Posting succeeded account=%s post_id=%s media_id=%s permalink=%s",
        updated.account_name,
        updated.id,
        updated.instagram_media_id,
        updated.instagram_permalink,
    )
    print(f"Posted post #{updated.id} to {updated.account_name}.")
    print(f"Instagram creation id: {updated.instagram_creation_id}")
    print(f"Instagram media id: {updated.instagram_media_id}")
    if updated.instagram_permalink:
        print(f"Permalink: {updated.instagram_permalink}")
    return 0


def _posting_safety_reason(
    *,
    account: AccountConfig,
    queue_store: QueueStore,
    post: PostRecord,
    force: bool,
) -> str | None:
    if post.status == "posted":
        return f"Post #{post.id} is already marked posted."
    if _account_already_posted_today(account, queue_store) and not force:
        return (
            f"A post already succeeded today for {account.account_name}. "
            "Use --force if you intentionally want to post another Reel today."
        )
    return None


def _next_due_post(account: AccountConfig, queue_store: QueueStore) -> PostRecord | None:
    due_date = today_in_timezone(account.timezone)
    return queue_store.get_next_post(
        account.account_name,
        on_or_before=due_date,
        statuses=("queued", "ready"),
    )


def _account_already_posted_today(account: AccountConfig, queue_store: QueueStore) -> bool:
    start_utc, end_utc = local_day_bounds_utc(account.timezone)
    return queue_store.has_posted_between(
        account.account_name,
        start_posted_at=start_utc,
        end_posted_at=end_utc,
    )


def local_day_bounds_utc(timezone_name: str, target_date: date | None = None) -> tuple[str, str]:
    zone = ZoneInfo(timezone_name)
    local_date = target_date or datetime.now(zone).date()
    start_local = datetime.combine(local_date, datetime_time.min, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc).isoformat(timespec="seconds")
    end_utc = end_local.astimezone(timezone.utc).isoformat(timespec="seconds")
    return start_utc, end_utc


def _build_post_preview(
    *,
    account: AccountConfig,
    config: AppConfig,
    queue_store: QueueStore,
    post: PostRecord,
) -> PostPreview:
    caption_resolution = resolve_caption(post=post, account=account, queue_store=queue_store)
    validation = validate_public_video_url(
        post.video_url,
        timeout_seconds=config.settings.http_timeout_seconds,
    )
    return PostPreview(
        post=post,
        caption_resolution=caption_resolution,
        validation=validation,
    )


def _print_post_details(*, post: PostRecord, account: AccountConfig, final_caption: str) -> None:
    print(f"  Post ID: {post.id}")
    print(f"  Account: {account.account_name}")
    print(f"  Instagram username: {account.instagram_username}")
    print(f"  IG User ID: {account.ig_user_id}")
    print("  Video URL: [redacted]")
    print(f"  Caption: {post.caption or '<empty>'}")
    print(f"  Hashtags: {post.hashtags or '<empty>'}")
    print(f"  Final caption: {final_caption or '<empty>'}")
    print(f"  Scheduled date: {post.scheduled_date}")
    print(f"  Status: {post.status}")


def _print_report_line(passed: bool, label: str, detail: str) -> None:
    marker = "PASS" if passed else "FAIL"
    print(f"  [{marker}] {label}: {detail}")


def print_dry_run(
    *,
    post: PostRecord,
    account: AccountConfig,
    settings: AppSettings,
    validation: UrlValidationResult,
    final_caption: str,
) -> None:
    payload = {
        "post_id": post.id,
        "status": post.status,
        "account_name": post.account_name,
        "instagram_username": account.instagram_username,
        "ig_user_id": account.ig_user_id,
        "video_url": "[redacted]",
        "caption": post.caption,
        "hashtags": post.hashtags,
        "final_caption": final_caption,
        "scheduled_date": post.scheduled_date,
        "account_timezone": account.timezone,
        "AUTO_POST_ENABLED": settings.auto_post_enabled,
        "DRY_RUN": settings.dry_run,
        "validation": {
            **asdict(validation),
            "final_url": "[redacted]" if validation.final_url else None,
        },
    }
    print("Dry run")
    print(json.dumps(payload, indent=2))


def prompt(label: str, *, allow_blank: bool = False) -> str:
    while True:
        value = input(f"{label}: ").strip()
        if value or allow_blank:
            return value
        print("This field is required.")


def _required_value(label: str, *, interactive: bool) -> str:
    if interactive:
        return prompt(label)
    raise ConfigurationError(f"{label} is required when running non-interactively.")
