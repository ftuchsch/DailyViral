from __future__ import annotations

from dataclasses import dataclass

from .queue import PostRecord, QueueStore


@dataclass(slots=True)
class AnalyticsReport:
    status_totals: dict[str, int]
    account_status_totals: list[dict[str, int | str]]
    recent_successes: list[PostRecord]
    recent_failures: list[PostRecord]


def build_report(
    queue_store: QueueStore,
    *,
    account_name: str | None = None,
) -> AnalyticsReport:
    status_totals = queue_store.status_counts()

    if account_name:
        account_rows = [
            row
            for row in queue_store.account_status_counts()
            if row["account_name"] == account_name
        ]
    else:
        account_rows = queue_store.account_status_counts()

    return AnalyticsReport(
        status_totals=status_totals,
        account_status_totals=account_rows,
        recent_successes=queue_store.recent_successes(account_name=account_name, limit=5),
        recent_failures=queue_store.recent_failures(account_name=account_name, limit=5),
    )
