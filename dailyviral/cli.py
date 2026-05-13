from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__
from .analytics import build_report
from .captions import prepare_new_post, resolve_caption
from .config import (
    DEFAULT_ACCOUNTS_PATH,
    ConfigurationError,
    ensure_default_files,
    ensure_project_directories,
    load_config,
    setup_logging,
)
from .instagram import InstagramApiError, InstagramClient
from .queue import PostRecord, QueueStore
from .url_validation import UrlValidationResult, validate_public_video_url


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        return handle_init()

    config = load_config(require_accounts=args.command in {"add", "add-from-file", "post-next"})
    logger = setup_logging(config.settings.log_path)
    queue_store = QueueStore(config.settings.database_path)
    queue_store.initialize()

    try:
        if args.command == "add":
            return handle_add(args, config, queue_store)
        if args.command == "add-from-file":
            return handle_add_from_file(args, config, queue_store)
        if args.command == "post-next":
            return handle_post_next(args, config, queue_store, logger)
        if args.command == "status":
            return handle_status(args, queue_store)
        if args.command == "analytics":
            return handle_analytics(args, queue_store)
        parser.print_help()
        return 1
    except ConfigurationError as exc:
        logger.error(str(exc))
        print(f"Configuration error: {exc}")
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dailyviral",
        description="DailyViral MVP for queueing and posting Instagram Reels via the official Meta Graph API.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("init", help="Create local config/data/log paths and initialize the project.")

    add_parser = subparsers.add_parser("add", help="Interactively add a queued post.")
    add_parser.add_argument("--account-name")
    add_parser.add_argument("--video-url")
    add_parser.add_argument("--caption")
    add_parser.add_argument("--hashtags")
    add_parser.add_argument("--scheduled-date")

    add_file_parser = subparsers.add_parser(
        "add-from-file",
        help="Add a queued post from a JSON file.",
    )
    add_file_parser.add_argument("path", help="Path to a JSON file describing one post.")

    post_parser = subparsers.add_parser(
        "post-next",
        help="Validate and post the next due Reel for an account.",
    )
    post_parser.add_argument("account_name")
    post_parser.add_argument("--dry-run", action="store_true")
    post_parser.add_argument("--yes", action="store_true", help="Skip interactive confirmation.")

    status_parser = subparsers.add_parser("status", help="Show queue status and recent posts.")
    status_parser.add_argument("--account-name")
    status_parser.add_argument("--limit", type=int, default=20)

    analytics_parser = subparsers.add_parser(
        "analytics",
        help="Show local queue analytics for posted and failed posts.",
    )
    analytics_parser.add_argument("--account-name")

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


def handle_add(args: argparse.Namespace, config: Any, queue_store: QueueStore) -> int:
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
        scheduled_date = datetime.now().date().isoformat()

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
        scheduled_date=_validate_date_string(scheduled_date),
    )
    print(f"Queued post #{post.id} for {post.account_name} on {post.scheduled_date}.")
    return 0


def handle_add_from_file(args: argparse.Namespace, config: Any, queue_store: QueueStore) -> int:
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
        scheduled_date=_validate_date_string(str(payload["scheduled_date"]).strip()),
    )
    print(f"Queued post #{post.id} from {input_path}.")
    return 0


def handle_post_next(
    args: argparse.Namespace,
    config: Any,
    queue_store: QueueStore,
    logger: logging.Logger,
) -> int:
    account = config.get_account(args.account_name)
    due_date = _today_in_timezone(account.timezone)
    post = queue_store.get_next_post(account.account_name, on_or_before=due_date)
    if post is None:
        print(f"No queued post is due for {account.account_name} on or before {due_date}.")
        return 0

    queue_store.set_status(post.id, status="validating", error_message=None)
    validation = validate_public_video_url(
        post.video_url,
        timeout_seconds=config.settings.http_timeout_seconds,
    )
    if not validation.ok:
        queue_store.mark_failed(post.id, validation.message)
        logger.error(
            "Validation failed for post %s account=%s url=%s message=%s",
            post.id,
            account.account_name,
            post.video_url,
            validation.message,
        )
        print(f"Validation failed for post #{post.id}: {validation.message}")
        return 1

    post = queue_store.get_post(post.id)
    caption_resolution = resolve_caption(post=post, account=account, queue_store=queue_store)
    post = queue_store.update_post(
        post.id,
        caption=caption_resolution.caption,
        hashtags=caption_resolution.hashtags,
        status="ready",
        error_message=None,
    )

    effective_dry_run = bool(args.dry_run or config.settings.dry_run or not config.settings.auto_post_enabled)
    final_caption = resolve_caption(
        post=queue_store.get_post(post.id),
        account=account,
        queue_store=queue_store,
    ).final_caption
    if effective_dry_run:
        print_dry_run(
            post=queue_store.get_post(post.id),
            validation=validation,
            account_timezone=account.timezone,
            final_caption=final_caption,
        )
        return 0

    if config.settings.confirm_before_posting and not args.yes:
        print_dry_run(
            post=queue_store.get_post(post.id),
            validation=validation,
            account_timezone=account.timezone,
            final_caption=final_caption,
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
            final_caption,
        )
        client = InstagramClient(config.settings, logger=logger)
        result = client.publish_reel(
            account=account,
            video_url=post.video_url,
            caption=final_caption,
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
    print(f"Instagram media id: {updated.instagram_media_id}")
    if updated.instagram_permalink:
        print(f"Permalink: {updated.instagram_permalink}")
    return 0


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
                f"{post.status} | {post.video_url}"
            )
            print(summary)
            if post.error_message:
                print(f"    error: {post.error_message}")
            if post.instagram_permalink:
                print(f"    permalink: {post.instagram_permalink}")
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
            print(f"  #{post.id} | {post.account_name} | {post.posted_at} | {post.instagram_permalink or '<no permalink>'}")
    else:
        print("  No successful posts yet.")

    print("\nRecent failures")
    if report.recent_failures:
        for post in report.recent_failures:
            print(f"  #{post.id} | {post.account_name} | {post.created_at} | {post.error_message}")
    else:
        print("  No failed posts.")
    return 0


def print_dry_run(
    *,
    post: PostRecord,
    validation: UrlValidationResult,
    account_timezone: str,
    final_caption: str,
) -> None:
    payload = {
        "post_id": post.id,
        "account_name": post.account_name,
        "video_url": post.video_url,
        "caption": post.caption,
        "hashtags": post.hashtags,
        "final_caption": final_caption,
        "scheduled_date": post.scheduled_date,
        "account_timezone": account_timezone,
        "validation": asdict(validation),
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
    raise ConfigurationError(
        f"{label} is required when running non-interactively."
    )


def _validate_date_string(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ConfigurationError(
            f"Invalid date '{value}'. Expected format YYYY-MM-DD."
        ) from exc


def _today_in_timezone(timezone_name: str) -> str:
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigurationError(f"Unknown timezone '{timezone_name}'.") from exc
    return datetime.now(zone).date().isoformat()
