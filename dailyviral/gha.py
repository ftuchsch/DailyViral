"""Stateless daily publishing from private R2 storage on trusted runners."""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from string import Formatter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .captions import compose_caption
from .config import (
    PROJECT_ROOT,
    AccountConfig,
    AppSettings,
    ConfigurationError,
    getenv_stripped,
    is_placeholder_ig_user_id,
)
from .instagram import InstagramApiError, InstagramClient, InstagramMediaRecord, InstagramTokenError
from .r2_upload import (
    R2_REQUIRED_ENV_VARS,
    R2Storage,
    build_generated_object_key,
    load_r2_config_from_env,
)
from .runtime import (
    assert_account_publish_config,
    current_local_date,
    env_flag,
    now_in_timezone,
    parse_date,
    require_env_value,
    validate_date_string,
)
from .security import mask_github_secret, redact_sensitive
from .video_overlay import (
    build_overlay_render_plan,
    load_overlay_messages,
    render_overlay_video,
    select_overlay_sentence,
)

DEFAULT_GHA_CAPTION_TEMPLATE = "Day {day_number} of posting this meme"
DEFAULT_GHA_HASHTAGS = "#meme #reels #daily"
DEFAULT_GHA_TIMEZONE = "America/New_York"
DEFAULT_GHA_ENABLE_VIDEO_OVERLAY = "false"
DEFAULT_GHA_SCHEDULE_CRON = "17 16 * * *"
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "dailyviral.yml"
GHA_DAILY_REQUIRED_ENV_VARS = (
    "INSTAGRAM_ACCESS_TOKEN",
    "R2_BASE_VIDEO_KEY",
    "IG_USER_ID",
    "INSTAGRAM_USERNAME",
    "DAILYVIRAL_START_DATE",
)


@dataclass(slots=True)
class GhaDayResolution:
    day_number: int
    source: str
    already_posted_today: bool


def handle_gha_post_daily(
    args: argparse.Namespace,
    settings: AppSettings,
    logger: logging.Logger,
) -> int:
    env_values = load_gha_env_values()
    account = build_gha_account(
        account_name=args.account_name,
        env_values=env_values,
    )

    local_today = current_local_date(env_values["timezone"])
    calendar_day_number = calculate_gha_day_number(
        start_date=env_values["start_date"],
        local_today=local_today,
    )
    day_resolution = GhaDayResolution(
        day_number=calendar_day_number,
        source="DAILYVIRAL_START_DATE fallback (Instagram history was not checked)",
        already_posted_today=False,
    )
    overlay_enabled = env_flag(env_values["enable_video_overlay"])
    event_mode = getenv_stripped("GITHUB_EVENT_NAME") or "local-shell"

    r2_config = load_r2_config_from_env()

    instagram_history_requested = settings.auto_post_enabled and overlay_enabled
    real_post_requested = instagram_history_requested and not settings.dry_run
    preflight_reasons: list[str] = []
    client: InstagramClient | None = None
    recent_media: list[InstagramMediaRecord] | None = None
    if real_post_requested:
        preflight_reasons = check_overlay_runtime()
    if instagram_history_requested and not preflight_reasons:
        client = InstagramClient(settings, logger=logger)
        if real_post_requested:
            if not refresh_gha_access_token(client=client, account=account):
                return 1
        try:
            recent_media = client.fetch_recent_media(account=account, limit=50)
        except InstagramTokenError as exc:
            print_gha_token_recovery(exc)
            return 1
        except InstagramApiError as exc:
            print(
                "Warning: could not read recent Instagram media to determine the next "
                f"successful day number. Error: {exc}"
            )
            print("Refusing to guess because that could create a duplicate or skip a day.")
            return 1

        day_resolution = resolve_gha_day_number_from_recent_media(
            caption_template=env_values["caption_template"],
            local_today=local_today,
            timezone_name=env_values["timezone"],
            recent_media=recent_media,
            fallback_day_number=calendar_day_number,
        )

    day_number = day_resolution.day_number
    day_caption = env_values["caption_template"].format(day_number=day_number)
    final_caption = compose_caption(
        caption=day_caption,
        hashtags=env_values["hashtags"],
    )
    overlay_sentence = None
    if overlay_enabled:
        overlay_sentence = select_overlay_sentence(
            day_number=day_number,
            csv_path=PROJECT_ROOT / "config" / "overlay_messages.csv",
        )

    print("GitHub Actions daily post")
    print(f"  Event mode: {event_mode}")
    print(f"  Account: {account.account_name}")
    print(f"  Date: {local_today.isoformat()}")
    print(f"  Day number: {day_number}")
    print(f"  Day number source: {day_resolution.source}")
    print(f"  Instagram username: {account.instagram_username}")
    print(f"  IG User ID: {account.ig_user_id}")
    print("  Base video: private R2 object")
    print(f"  ENABLE_VIDEO_OVERLAY: {str(overlay_enabled).lower()}")
    if overlay_sentence:
        print(f"  Overlay sentence: {overlay_sentence}")
    if overlay_enabled:
        print("  Generated video: private R2 object with temporary GET access")
    print(f"  Final caption: {final_caption}")
    print("  INSTAGRAM_ACCESS_TOKEN set: yes")
    print(f"  AUTO_POST_ENABLED: {str(settings.auto_post_enabled).lower()}")
    print(f"  DRY_RUN: {str(settings.dry_run).lower()}")
    if not settings.auto_post_enabled:
        print("  Action: skip publish because AUTO_POST_ENABLED is not true")
    elif settings.dry_run:
        print("  Action: dry run only because DRY_RUN is true")
    elif not overlay_enabled:
        print("  Action: skip publish because ENABLE_VIDEO_OVERLAY is not true")
    else:
        print("  Action: preflight for a real overlay post")

    if not settings.auto_post_enabled:
        print("AUTO_POST_ENABLED is not true. Dry-run only; skipping publish.")
        if overlay_enabled:
            print(
                "Overlay mode dry run: would render a generated MP4, skip the R2 upload, and skip the Instagram publish."
            )
        return 0
    if settings.dry_run:
        print("DRY_RUN is true. Dry-run only; skipping publish.")
        if overlay_enabled:
            print(
                "Overlay mode dry run: would render a generated MP4, skip the R2 upload, and skip the Instagram publish."
            )
        return 0
    if not overlay_enabled:
        print(
            "ENABLE_VIDEO_OVERLAY is not true. Refusing to run a real GitHub Actions post without overlay mode."
        )
        return 1

    if preflight_reasons:
        print("Overlay posting is not ready:")
        for reason in preflight_reasons:
            print(f"  - {reason}")
        return 1

    if client is None or recent_media is None:
        print("Internal error: Instagram preflight did not complete before a real post.")
        return 1
    recent_media_result = check_recent_caption_duplicate(
        client=client,
        account=account,
        expected_caption_fragment=day_caption,
        force=args.force,
        recent_media=recent_media,
    )
    if recent_media_result == "already-posted":
        print("Already posted today")
        return 0
    if recent_media_result == "cannot-check":
        return 1

    storage = R2Storage(r2_config)
    object_key = build_generated_object_key(day_number=day_number, local_date=local_today)
    render_plan = build_overlay_render_plan(
        day_number=day_number,
        local_date=local_today,
        output_root=PROJECT_ROOT / "generated",
        overlay_sentence=overlay_sentence or "",
    )
    uploaded = False
    cleanup_failed = False
    result = None
    try:
        storage.download_base_video(render_plan.downloaded_video_path)
        rendered_path = render_overlay_video(plan=render_plan)
        post_video_url = storage.upload_video(file_path=rendered_path, object_key=object_key)
        uploaded = True
        logger.info(
            "Publishing private R2 video account=%s day_number=%s", account.account_name, day_number
        )
        result = client.publish_reel(
            account=account, video_url=post_video_url, caption=final_caption
        )
    except (ConfigurationError, InstagramApiError) as exc:
        logger.error("GitHub Actions posting failed: %s", exc)
        print(f"Posting failed: {redact_sensitive(exc)}")
    finally:
        if uploaded:
            try:
                storage.delete_generated_video(object_key)
            except ConfigurationError as exc:
                cleanup_failed = True
                logger.error("%s", exc)
                print(str(exc))
        shutil.rmtree(render_plan.work_dir, ignore_errors=True)
    if result is None:
        return 1
    if cleanup_failed:
        print(
            f"Instagram published media {result.media_id}; cleanup failed. Do not force a duplicate post."
        )
        return 1

    print(f"Posted media id: {result.media_id}")
    if result.permalink:
        print(f"Permalink: {result.permalink}")
    return 0


def handle_gha_schedule_readiness(
    args: argparse.Namespace,
    settings: AppSettings,
) -> int:
    timezone_name = getenv_stripped("DAILYVIRAL_TIMEZONE") or DEFAULT_GHA_TIMEZONE
    utc_now = datetime.now(timezone.utc)
    local_now = now_in_timezone(timezone_name)
    cron_expression = read_gha_workflow_cron_expression() or DEFAULT_GHA_SCHEDULE_CRON

    env_presence = {name: bool(getenv_stripped(name)) for name in GHA_DAILY_REQUIRED_ENV_VARS}
    r2_presence = {name: bool(getenv_stripped(name)) for name in R2_REQUIRED_ENV_VARS}
    auto_post_enabled = settings.auto_post_enabled
    dry_run = settings.dry_run
    overlay_enabled = env_flag(
        getenv_stripped("ENABLE_VIDEO_OVERLAY") or DEFAULT_GHA_ENABLE_VIDEO_OVERLAY
    )
    base_video_key = getenv_stripped("R2_BASE_VIDEO_KEY")
    start_date_value = getenv_stripped("DAILYVIRAL_START_DATE")
    caption_template = (
        getenv_stripped("DAILYVIRAL_CAPTION_TEMPLATE") or DEFAULT_GHA_CAPTION_TEMPLATE
    )
    hashtags = getenv_stripped("DAILYVIRAL_HASHTAGS") or DEFAULT_GHA_HASHTAGS

    reasons: list[str] = []
    try:
        validate_caption_template(caption_template)
    except ConfigurationError as exc:
        reasons.append(str(exc))
        caption_template = DEFAULT_GHA_CAPTION_TEMPLATE
    start_date = ""
    today_day_number: int | None = None
    tomorrow_day_number: int | None = None
    tomorrow_caption = ""
    tomorrow_overlay = ""

    if not start_date_value:
        reasons.append("DAILYVIRAL_START_DATE is missing.")
    else:
        try:
            start_date = validate_date_string(start_date_value)
            today_day_number = calculate_gha_day_number(
                start_date=start_date,
                local_today=local_now.date(),
            )
            tomorrow_day_number = calculate_gha_day_number(
                start_date=start_date,
                local_today=(local_now + timedelta(days=1)).date(),
            )
            tomorrow_caption = compose_caption(
                caption=caption_template.format(day_number=tomorrow_day_number),
                hashtags=hashtags,
            )
        except (ConfigurationError, KeyError, ValueError) as exc:
            reasons.append(f"Could not compute day numbers from DAILYVIRAL_START_DATE: {exc}")

    overlay_csv_path = PROJECT_ROOT / "config" / "overlay_messages.csv"
    overlay_csv_ok = False
    try:
        load_overlay_messages(overlay_csv_path)
        overlay_csv_ok = True
        if tomorrow_day_number is not None:
            tomorrow_overlay = select_overlay_sentence(
                day_number=tomorrow_day_number,
                csv_path=overlay_csv_path,
            )
    except ConfigurationError as exc:
        reasons.append(str(exc))

    private_storage_verified = False
    if base_video_key:
        try:
            storage = R2Storage(load_r2_config_from_env())
            storage.verify_source()
            private_storage_verified = True
        except Exception:
            reasons.append("Private R2 settings or source object could not be verified.")
    else:
        reasons.append("R2_BASE_VIDEO_KEY is missing.")

    ffmpeg_available = shutil.which("ffmpeg") is not None

    if not auto_post_enabled:
        reasons.append("AUTO_POST_ENABLED is not true.")
    if dry_run:
        reasons.append("DRY_RUN is true.")
    if not overlay_enabled:
        reasons.append("ENABLE_VIDEO_OVERLAY is not true.")

    for name, present in env_presence.items():
        if not present:
            reasons.append(f"{name} is missing.")
    for name, present in r2_presence.items():
        if not present:
            reasons.append(f"{name} is missing.")

    ig_user_id = getenv_stripped("IG_USER_ID")
    if ig_user_id and (is_placeholder_ig_user_id(ig_user_id) or not ig_user_id.isdigit()):
        reasons.append("IG_USER_ID is invalid or still looks like a placeholder.")

    instagram_token_valid: bool | None = None
    token_check_detail = "not checked"
    if (
        env_presence["INSTAGRAM_ACCESS_TOKEN"]
        and env_presence["INSTAGRAM_USERNAME"]
        and ig_user_id
        and not is_placeholder_ig_user_id(ig_user_id)
        and ig_user_id.isdigit()
    ):
        readiness_account = AccountConfig(
            account_name=args.account_name,
            instagram_username=getenv_stripped("INSTAGRAM_USERNAME"),
            ig_user_id=ig_user_id,
            access_token_env_var="INSTAGRAM_ACCESS_TOKEN",
        )
        try:
            InstagramClient(settings).fetch_recent_media(
                account=readiness_account,
                limit=1,
            )
        except InstagramTokenError as exc:
            instagram_token_valid = False
            token_check_detail = f"Meta rejected it (code={exc.code or '<unknown>'})"
            reasons.append(
                "INSTAGRAM_ACCESS_TOKEN is expired, revoked, or invalid; replace the "
                "GitHub Actions secret before the next run."
            )
        except InstagramApiError as exc:
            instagram_token_valid = False
            token_check_detail = str(exc)
            reasons.append(f"Could not validate INSTAGRAM_ACCESS_TOKEN: {exc}")
        else:
            instagram_token_valid = True
            token_check_detail = "Meta accepted it"

    if not ffmpeg_available:
        reasons.append("ffmpeg is not available in PATH.")
    if not overlay_csv_ok:
        reasons.append("Overlay message CSV is not readable.")

    ready = not reasons

    print("GitHub Actions schedule readiness")
    print(f"  Account: {args.account_name}")
    print(f"  Current UTC time: {utc_now.isoformat()}")
    print(f"  Current {timezone_name} time: {local_now.isoformat()}")
    print(f"  Workflow cron: {cron_expression}")
    print(f"  DAILYVIRAL_START_DATE: {start_date_value or '<missing>'}")
    print(
        f"  Today's day number: {today_day_number if today_day_number is not None else '<unavailable>'}"
    )
    print(
        f"  Tomorrow's day number: {tomorrow_day_number if tomorrow_day_number is not None else '<unavailable>'}"
    )
    print(f"  Expected caption for tomorrow: {tomorrow_caption or '<unavailable>'}")
    print(f"  Expected overlay sentence for tomorrow: {tomorrow_overlay or '<unavailable>'}")
    print(f"  AUTO_POST_ENABLED: {str(auto_post_enabled).lower()}")
    print(f"  DRY_RUN: {str(dry_run).lower()}")
    print(f"  ENABLE_VIDEO_OVERLAY: {str(overlay_enabled).lower()}")
    print("  Instagram env vars:")
    for name, present in env_presence.items():
        print(f"    {name}: {'yes' if present else 'no'}")
    print("  R2 env vars:")
    for name, present in r2_presence.items():
        print(f"    {name}: {'yes' if present else 'no'}")
    print(
        f"  Private R2 storage and source verified: {'yes' if private_storage_verified else 'no'}"
    )
    print(
        "  Instagram access token valid: "
        f"{'yes' if instagram_token_valid else 'no' if instagram_token_valid is False else 'not checked'} "
        f"({token_check_detail})"
    )
    print(f"  ffmpeg available: {'yes' if ffmpeg_available else 'no'}")

    if ready:
        print()
        print("READY FOR NEXT SCHEDULED POST")
        return 0

    print()
    print("NOT READY")
    for reason in sorted(dict.fromkeys(reasons)):
        print(f"  - {reason}")
    return 1


def load_gha_env_values() -> dict[str, str]:
    values = {name: require_env_value(name) for name in GHA_DAILY_REQUIRED_ENV_VARS}
    values["caption_template"] = (
        getenv_stripped("DAILYVIRAL_CAPTION_TEMPLATE") or DEFAULT_GHA_CAPTION_TEMPLATE
    )
    values["hashtags"] = getenv_stripped("DAILYVIRAL_HASHTAGS") or DEFAULT_GHA_HASHTAGS
    values["timezone"] = getenv_stripped("DAILYVIRAL_TIMEZONE") or DEFAULT_GHA_TIMEZONE
    values["enable_video_overlay"] = (
        getenv_stripped("ENABLE_VIDEO_OVERLAY") or DEFAULT_GHA_ENABLE_VIDEO_OVERLAY
    )
    validate_caption_template(values["caption_template"])
    values["start_date"] = validate_date_string(values["DAILYVIRAL_START_DATE"])
    return values


def check_overlay_runtime() -> list[str]:
    """Check rendering prerequisites before requesting a real publication."""
    reasons: list[str] = []
    if shutil.which("ffmpeg") is None:
        reasons.append("ffmpeg is not available in PATH.")
    try:
        load_overlay_messages(PROJECT_ROOT / "config" / "overlay_messages.csv")
    except ConfigurationError as exc:
        reasons.append(str(exc))
    return reasons


def read_gha_workflow_cron_expression() -> str | None:
    if not WORKFLOW_PATH.exists():
        return None

    content = WORKFLOW_PATH.read_text(encoding="utf-8")
    match = re.search(r'cron:\s*"([^"]+)"', content)
    if match:
        return match.group(1).strip()
    return None


def build_gha_account(*, account_name: str, env_values: dict[str, str]) -> AccountConfig:
    account = AccountConfig(
        account_name=account_name,
        instagram_username=env_values["INSTAGRAM_USERNAME"],
        ig_user_id=env_values["IG_USER_ID"],
        access_token_env_var="INSTAGRAM_ACCESS_TOKEN",
        default_caption_template=env_values["caption_template"],
        default_hashtags=env_values["hashtags"],
        required_hashtags="",
        timezone=env_values["timezone"],
    )
    assert_account_publish_config(account)
    return account


def calculate_gha_day_number(*, start_date: str, local_today: date) -> int:
    first_day = parse_date(start_date)
    delta_days = (local_today - first_day).days
    if delta_days < 0:
        raise ConfigurationError(
            f"DAILYVIRAL_START_DATE {start_date} is in the future relative to {local_today.isoformat()}."
        )
    return delta_days + 1


def refresh_gha_access_token(
    *,
    client: InstagramClient,
    account: AccountConfig,
) -> bool:
    """Refresh a valid long-lived token before doing any expensive post work."""
    try:
        result = client.refresh_long_lived_access_token(account)
    except InstagramTokenError as exc:
        print_gha_token_recovery(exc)
        return False
    except InstagramApiError as exc:
        # A newly issued token may not be refreshable until it is 24 hours old.
        # The normal recent-media request below still validates whether it can post.
        print(f"Warning: Meta did not refresh the Instagram access token: {exc}")
        print("Continuing with the current token and validating it through a normal API request.")
        return True

    mask_github_secret(result.access_token)
    os.environ[account.access_token_env_var] = result.access_token
    if result.expires_in_seconds is None:
        print("Instagram long-lived access token refreshed successfully.")
    else:
        expires_in_days = result.expires_in_seconds / 86_400
        print(
            "Instagram long-lived access token refreshed successfully "
            f"(expires in approximately {expires_in_days:.1f} days)."
        )
    return True


def print_gha_token_recovery(exc: InstagramTokenError) -> None:
    print("Instagram authentication failed: the access token is expired, revoked, or invalid.")
    print(f"Meta API error: {exc}")
    print("An expired token cannot be refreshed. To recover:")
    print("  1. Generate a new long-lived Instagram access token in the Meta App Dashboard.")
    print("  2. Replace the publishing environment secret named INSTAGRAM_ACCESS_TOKEN.")
    print("  3. Re-run this workflow normally; --force does not fix authentication.")


def resolve_gha_day_number_from_recent_media(
    *,
    caption_template: str,
    local_today: date,
    timezone_name: str,
    recent_media: list[InstagramMediaRecord],
    fallback_day_number: int,
) -> GhaDayResolution:
    """Continue the published series without advancing on failed scheduled runs."""
    validate_caption_template(caption_template)

    escaped_template = re.escape(caption_template.strip())
    escaped_placeholder = re.escape("{day_number}")
    caption_expression = escaped_template.replace(
        escaped_placeholder,
        r"(?P<day_number>\d+)",
    )
    caption_pattern = re.compile(
        rf"^{caption_expression}(?=\s|$)",
        flags=re.IGNORECASE,
    )

    matches: list[tuple[datetime, int]] = []
    for record in recent_media:
        match = caption_pattern.search(record.caption.strip())
        if not match:
            continue
        if not record.timestamp:
            raise ConfigurationError(
                "Instagram returned a matching series post without a timestamp; "
                "refusing to guess the next day number."
            )
        try:
            published_at = datetime.fromisoformat(record.timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ConfigurationError(
                f"Instagram returned an invalid media timestamp: {record.timestamp}"
            ) from exc
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        matches.append((published_at, int(match.group("day_number"))))

    if not matches:
        return GhaDayResolution(
            day_number=fallback_day_number,
            source="DAILYVIRAL_START_DATE fallback (no matching published series caption found)",
            already_posted_today=False,
        )

    published_at, published_day_number = max(matches, key=lambda item: item[0])
    try:
        account_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigurationError(f"Unknown timezone '{timezone_name}'.") from exc

    published_local_date = published_at.astimezone(account_timezone).date()
    if published_local_date == local_today:
        return GhaDayResolution(
            day_number=published_day_number,
            source=f"published Instagram history (day {published_day_number} already exists today)",
            already_posted_today=True,
        )

    return GhaDayResolution(
        day_number=published_day_number + 1,
        source=f"published Instagram history (latest successful day is {published_day_number})",
        already_posted_today=False,
    )


def check_recent_caption_duplicate(
    *,
    client: InstagramClient,
    account: AccountConfig,
    expected_caption_fragment: str,
    force: bool,
    recent_media: list[InstagramMediaRecord] | None = None,
) -> str:
    if force:
        print("Force flag enabled. Skipping recent-caption duplicate check.")
        return "force"

    try:
        media_records = (
            recent_media
            if recent_media is not None
            else client.fetch_recent_media(account=account, limit=50)
        )
    except InstagramTokenError as exc:
        print_gha_token_recovery(exc)
        return "cannot-check"
    except InstagramApiError as exc:
        print(
            f"Warning: could not check recent media captions through the official API. Error: {exc}"
        )
        print("Retry with --force only if you are certain this run has not already posted today.")
        return "cannot-check"

    for record in media_records:
        if re.match(
            re.escape(expected_caption_fragment) + r"(?=\s|$)", record.caption, re.IGNORECASE
        ):
            print(
                "Recent media caption match found: "
                f"{record.media_id or '<unknown id>'} {record.permalink or '<no permalink>'}"
            )
            return "already-posted"
    return "not-found"


def validate_caption_template(template: str) -> None:
    try:
        fields = [
            (name, spec, conversion)
            for _, name, spec, conversion in Formatter().parse(template)
            if name is not None
        ]
    except ValueError:
        raise ConfigurationError("DAILYVIRAL_CAPTION_TEMPLATE has invalid braces.") from None
    if fields != [("day_number", "", None)]:
        raise ConfigurationError(
            "DAILYVIRAL_CAPTION_TEMPLATE must contain exactly one plain {day_number} placeholder."
        )
