from __future__ import annotations

from dataclasses import dataclass

from .config import AccountConfig
from .queue import PostRecord, QueueStore


@dataclass(slots=True)
class CaptionResolution:
    caption: str
    hashtags: str
    final_caption: str
    day_number: int | None


def prepare_new_post(
    *,
    account: AccountConfig,
    queue_store: QueueStore,
    caption: str,
    hashtags: str,
) -> CaptionResolution:
    clean_caption = caption.strip()
    clean_hashtags = _merge_required_hashtags(
        hashtags.strip() or account.default_hashtags.strip(),
        account.required_hashtags,
    )
    day_number: int | None = None

    if not clean_caption and account.default_caption_template.strip():
        day_number = infer_next_day_number(queue_store, account.account_name)
        clean_caption = account.default_caption_template.format(day_number=day_number)

    final_caption = compose_caption(caption=clean_caption, hashtags=clean_hashtags)
    return CaptionResolution(
        caption=clean_caption,
        hashtags=clean_hashtags,
        final_caption=final_caption,
        day_number=day_number,
    )


def resolve_caption(
    *,
    post: PostRecord,
    account: AccountConfig,
    queue_store: QueueStore,
) -> CaptionResolution:
    caption = post.caption.strip()
    hashtags = _merge_required_hashtags(
        post.hashtags.strip() or account.default_hashtags.strip(),
        account.required_hashtags,
    )
    day_number: int | None = None

    if not caption and account.default_caption_template.strip():
        day_number = max(queue_store.count_posts_for_day_number(account.account_name), 1)
        caption = account.default_caption_template.format(day_number=day_number)

    final_caption = compose_caption(caption=caption, hashtags=hashtags)
    return CaptionResolution(
        caption=caption,
        hashtags=hashtags,
        final_caption=final_caption,
        day_number=day_number,
    )


def compose_caption(*, caption: str, hashtags: str) -> str:
    clean_caption = caption.strip()
    clean_hashtags = hashtags.strip()

    if clean_caption and clean_hashtags:
        return f"{clean_caption}\n\n{clean_hashtags}"
    return clean_caption or clean_hashtags


def infer_next_day_number(queue_store: QueueStore, account_name: str) -> int:
    return queue_store.count_posts_for_day_number(account_name) + 1


def _merge_required_hashtags(current_hashtags: str, required_hashtags: str) -> str:
    ordered_tags: list[str] = []
    seen: set[str] = set()

    for raw_group in (current_hashtags, required_hashtags):
        for token in raw_group.split():
            if not token:
                continue
            normalized = token.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            ordered_tags.append(token)

    return " ".join(ordered_tags)
