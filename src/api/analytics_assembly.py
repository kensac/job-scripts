"""Assemble source analytics from independent aggregate query results."""

import decimal
from urllib.parse import urlencode

from api import db
from api.analytics_models import (
    BoardAnalytics,
    BoardDecay,
    BoardDrill,
    BoardFreshness,
    BoardIngest,
    BoardInventory,
    BoardOverlap,
    BoardSpend,
    BoardYield,
    CheckStage,
    CustomCheckStage,
    ModelSpend,
    _ConfigRow,
    _CustomRow,
    _FirstClosedRow,
    _FunnelRow,
    _IngestRow,
    _InventoryRow,
    _OverlapRow,
    _PopulationsRow,
    _SourceKeyed,
    _SpendRow,
    _YieldRow,
)
from api.analytics_queries import (
    _CUSTOM_SQL,
    _FIRST_CLOSED_SQL,
    _FUNNEL_SQL,
    _INGEST_SQL,
    _INVENTORY_SQL,
    _OVERLAP_SQL,
    _POPULATIONS_SQL,
    _SPEND_SQL,
    _VERDICT_CHECKS,
    _YIELD_SQL,
)
from api.rates import rate as _rate


def index_rows_by_source[R: _SourceKeyed](rows: list[R]) -> dict[str, R]:
    return {r.source: r for r in rows if r.source}


def source_drill_links(source: str) -> BoardDrill:
    sources = urlencode({"sources": source})
    queries = f"/v1/admin/queries?{sources}"
    return BoardDrill(
        # Deliberately NOT a link to the postings count. This drills the rows
        # behind the FUNNEL denominators - urls this board has had checked -
        # because /v1/admin/jobs aggregates ai_queries and only knows postings
        # that have a verdict. Nothing serves "every posting from this source"
        # to an admin, and pointing inventory.total at an endpoint counting a
        # subset is the drift these links exist to prevent.
        checked_jobs=f"/v1/admin/jobs?{sources}",
        queries=queries,
        closed_rejected=f"{queries}&check_type=closed&status=rejected",
        clearance_rejected=f"{queries}&check_type=clearance&status=rejected",
        custom_rejected=f"{queries}&check_type=custom&status=rejected",
    )


def assemble_check_stage(row: _FunnelRow | None, total: int, min_sample: int) -> CheckStage:
    checked = row.checked if row else 0
    passed = row.passed if row else 0
    return CheckStage(
        checked=checked,
        passed=passed,
        rejected=checked - passed,
        pass_rate=_rate(passed, checked, min_sample),
        # How much of the board the stage has actually seen. A pass rate over
        # 2% of a board says almost nothing about the other 98%.
        coverage=_rate(checked, total, min_sample),
    )


def assemble_source_analytics(
    source: str,
    *,
    min_sample: int,
    config: _ConfigRow | None,
    inventory: _InventoryRow | None,
    funnel: dict[str, _FunnelRow],
    custom: _CustomRow | None,
    first_closed: _FirstClosedRow | None,
    spend: list[_SpendRow],
    ingest: _IngestRow | None,
    board: _YieldRow | None,
    populations: _PopulationsRow | None,
    overlap: _OverlapRow | None,
) -> BoardAnalytics:
    total = inventory.total if inventory else 0
    active = inventory.active if inventory else 0
    inactive = inventory.inactive if inventory else 0

    stages: dict[str, CustomCheckStage | CheckStage] = {
        check_name: assemble_check_stage(funnel.get(check_name), total, min_sample)
        for check_name in _VERDICT_CHECKS
    }
    custom_evaluated = custom.jobs_evaluated if custom else 0
    custom_passed = custom.jobs_passed_all if custom else 0
    stages["custom"] = CustomCheckStage(
        checked=custom_evaluated,
        passed=custom_passed,
        rejected=custom_evaluated - custom_passed,
        pass_rate=_rate(custom_passed, custom_evaluated, min_sample),
        coverage=_rate(custom_evaluated, total, min_sample),
        # One job judged by three filters is three evaluations but one job;
        # both denominators are real and they answer different questions.
        evaluations=custom.evaluations if custom else 0,
        evaluations_passed=custom.evaluations_passed if custom else 0,
    )

    first_checked = first_closed.first_checked if first_closed else 0
    dead_on_arrival = first_closed.dead_on_arrival if first_closed else 0

    keyed_jobs = overlap.keyed_jobs if overlap else 0
    shared_with_other_source = overlap.shared_with_other_source if overlap else 0

    board_rows = board.board_rows if board else 0
    applied = board.applied if board else 0

    return BoardAnalytics(
        source=source,
        configured=config is not None,
        listings_url=config.listings_url if config else None,
        source_active=config.active if config else None,
        inventory=BoardInventory(
            total=total,
            active=active,
            inactive=inactive,
            active_share=_rate(active, total, min_sample),
            # False means every row this board has ever supplied is still
            # marked active, which means the feed has no way to say otherwise -
            # so active_share is a fact about the feed, not about the board.
            reports_inactive=inactive > 0,
        ),
        freshness=BoardFreshness(
            with_date_posted=_rate(
                inventory.with_date_posted if inventory else 0, total, min_sample
            ),
            median_age_days=rounded_days(inventory.median_age_days if inventory else None),
            p90_age_days=rounded_days(inventory.p90_age_days if inventory else None),
            posted_7d=inventory.posted_7d if inventory else 0,
            posted_30d=inventory.posted_30d if inventory else 0,
            first_loaded_at=inventory.first_loaded_at if inventory else None,
            last_loaded_at=inventory.last_loaded_at if inventory else None,
        ),
        ingest=BoardIngest(
            runs=ingest.runs if ingest else 0,
            succeeded=ingest.succeeded if ingest else 0,
            failed=ingest.failed if ingest else 0,
            last_success_at=ingest.last_success_at if ingest else None,
            last_failure_at=ingest.last_failure_at if ingest else None,
            success_rate=_rate(
                ingest.succeeded if ingest else 0,
                ingest.runs if ingest else 0,
                min_sample,
            ),
        ),
        funnel=stages,
        decay=BoardDecay(
            dead_on_arrival=_rate(dead_on_arrival, first_checked, min_sample),
            still_open=stages["closed"].pass_rate,
        ),
        spend=summarize_source_spend(spend, min_sample),
        overlap=BoardOverlap(
            keyed_jobs=keyed_jobs,
            shared_with_other_source=shared_with_other_source,
            duplicated_share=_rate(shared_with_other_source, keyed_jobs, min_sample),
            exclusive=keyed_jobs - shared_with_other_source,
        ),
        board_yield=BoardYield(
            with_status=board.with_status if board else 0,
            applied=applied,
            users=board.users if board else 0,
            apply_rate=_rate(applied, board_rows, min_sample),
            acted_on=populations.acted_on if populations else 0,
            working_set=populations.working_set if populations else 0,
            visible=populations.visible if populations else 0,
        ),
        drill=source_drill_links(source),
    )


def rounded_days(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def summarize_source_spend(rows: list[_SpendRow], min_sample: int) -> BoardSpend:
    """What this board's postings cost to check, as billed at the time.

    Deliberately not broken down per check_type: verify_new books one batched
    call against the `closed` row and writes the clearance verdict from the
    same response, so a per-check split reads as though clearance were free on
    some paths and not others. The per-board total is unambiguous; the split
    is not.
    """
    calls = sum(r.calls for r in rows)
    priced_calls = sum(r.priced_calls for r in rows)
    cost = sum((r.cost_usd for r in rows if r.cost_usd is not None), decimal.Decimal(0))
    return BoardSpend(
        # Said out loud, because /admin/spend defaults to 30 days and this does
        # not. A reader comparing the two was comparing a lifetime against a
        # month with nothing on either page saying so.
        window="all_time",
        calls=calls,
        total_tokens=sum(r.total_tokens for r in rows),
        cost_usd=float(cost),
        # A call with no price is a model missing from the price table.
        # Reporting coverage keeps "cheap" distinguishable from "partly
        # unpriced".
        priced_coverage=_rate(priced_calls, calls, min_sample),
        by_model=[
            ModelSpend(
                model=r.model,
                calls=r.calls,
                total_tokens=r.total_tokens,
                cost_usd=float(r.cost_usd) if r.cost_usd is not None else None,
            )
            for r in sorted(rows, key=lambda r: -r.calls)
        ],
    )


def collect_source_analytics(min_sample: int) -> list[BoardAnalytics]:
    configs = index_rows_by_source(
        db.query_as(_ConfigRow, "SELECT name AS source, listings_url, active FROM sources")
    )
    inventory = index_rows_by_source(db.query_as(_InventoryRow, _INVENTORY_SQL))
    custom = index_rows_by_source(db.query_as(_CustomRow, _CUSTOM_SQL))
    first_closed = index_rows_by_source(db.query_as(_FirstClosedRow, _FIRST_CLOSED_SQL))
    ingest = index_rows_by_source(db.query_as(_IngestRow, _INGEST_SQL))
    board = index_rows_by_source(db.query_as(_YieldRow, _YIELD_SQL))
    populations = index_rows_by_source(db.query_as(_PopulationsRow, _POPULATIONS_SQL))
    overlap = index_rows_by_source(db.query_as(_OverlapRow, _OVERLAP_SQL))

    funnel: dict[str, dict[str, _FunnelRow]] = {}
    for row in db.query_as(_FunnelRow, _FUNNEL_SQL, {"checks": list(_VERDICT_CHECKS)}):
        funnel.setdefault(row.source, {})[row.check_type] = row

    spend: dict[str, list[_SpendRow]] = {}
    for row in db.query_as(_SpendRow, _SPEND_SQL):
        spend.setdefault(row.source, []).append(row)

    # A configured source with no postings is not an empty row to skip - it is
    # a board that is being ingested and returning nothing, which is the
    # loudest thing this endpoint can say about it.
    names = set(configs) | set(inventory) | set(ingest)
    return [
        assemble_source_analytics(
            name,
            min_sample=min_sample,
            config=configs.get(name),
            inventory=inventory.get(name),
            funnel=funnel.get(name, {}),
            custom=custom.get(name),
            first_closed=first_closed.get(name),
            spend=spend.get(name, []),
            ingest=ingest.get(name),
            board=board.get(name),
            populations=populations.get(name),
            overlap=overlap.get(name),
        )
        for name in sorted(names)
    ]
