"""Persisted boards settings and their admin metadata."""

from __future__ import annotations

from pydantic import PositiveInt

from api.config_types import ColumnState, ConfigKey, SortKey
from core.screening import Recipe
from core.volume_gate import VolumeGate

CONFIG_KEYS: dict[str, ConfigKey] = {
    "managed_board_cache_writes_enabled": ConfigKey(
        section="Boards",
        default=True,
        value_type=bool,
        help="Allow the provider's default prompt caching for new managed-board reviews. "
        "Turning this off disables both cache reads and writes on supported models only, "
        "avoiding cache-write premiums for one-off posting text. Unsupported models and "
        "personal filters keep their existing policy. Prompts and decision criteria are unchanged; "
        "submitted requests retain their original policy. Re-enable to restore default caching.",
    ),
    "verify_answers_board_questions": ConfigKey(
        section="Boards",
        default=True,
        value_type=bool,
        help="Ask each published managed board's question inside the new-posting verification "
        "request, so the posting text is paid for once instead of once per board. Only boards "
        "on the same model and effort as verification, whose sources, criteria and enforced "
        "gates admit the posting and that have no verdict for it yet. Board runs then reuse "
        "those verdicts. Turning this off restores separate board requests from the next sweep.",
    ),
    "verify_near_copy_reuse": ConfigKey(
        section="Boards",
        default=True,
        value_type=bool,
        help="Give a posting its verified twin's verdicts (closed, clearance and every live board "
        "and filter prompt) instead of reading the same text again. A twin is the same source, "
        "title and text once location lines and numbers are removed. The posting's own "
        "location, date and gates still apply. Turning this off asks for every posting again.",
    ),
    "verification_volume_gate": ConfigKey(
        section="Boards",
        default=VolumeGate().model_dump(mode="json"),
        value_type=VolumeGate,
        help="Postings verification skips for the listed boards and filters (exact prompt "
        "hashes in scopes; empty means off). Skips a source with source_min_judged postings "
        "judged in window_days and a keep rate at or below source_max_keep_rate (0 = never "
        "kept), and a source's title judged title_min_judged "
        "times in window_days with no keep by any board or filter, except audit_percent of "
        "their postings, so a source or title that starts producing keeps returns by itself. Also "
        "skips titles naming a listed occupation with no technical word. A posting someone "
        "tracks is always read. A new prompt hash is not listed until added here.",
    ),
    # Seeded from filter_review_gate's enforced title scopes by acbc00862b2b.
    "title_screens": ConfigKey(
        section="Boards",
        default={},
        value_type=dict[str, Recipe],
        help="Exact board or filter prompt hash to the title screen (core/screening.py) "
        "its candidates leave out. A posting the screen skips is never chunked, read or "
        "judged for that prompt, and writes nothing; removing a hash returns its postings "
        "on the next run. A prose edit changes the hash and so drops the screen.",
    ),
    # Groups whose filter saves re-judge the board at once. Seeded closed:
    # three edits on one new account cost 10.27 dollars in a day
    # (2026-09-08), so for everyone else an edit waits for the hourly
    # sweep at batch price. "*" opens it to everyone.
    "filter_rejudge_on_change_groups": ConfigKey(
        section="Boards",
        default=[],
        value_type=list[str],
        kind="groups",
        help="Authentik groups whose filter saves re-judge the whole board at once; "
        'everyone else waits for the hourly sweep. "*" means everyone.',
    ),
    # Groups whose members may start a filter run by hand. Seeded closed, so
    # only admins can: a run re-judges every posting in the catalog against a
    # prompt, and it is the most expensive thing a button can do. The hourly
    # sweep still re-judges everyone's board at batch price, so a person who
    # cannot press Run is not stuck, only slower. "*" opens it to everyone.
    "filter_run_groups": ConfigKey(
        section="Boards",
        default=[],
        value_type=list[str],
        kind="groups",
        help="Authentik groups whose members may start a filter run by hand. "
        "Admins always may. Everyone else waits for the hourly sweep. "
        '"*" means everyone.',
    ),
    # How the board is ordered before anybody touches a header. Two keys,
    # because one is not enough: date_posted is what a person actually wants
    # (newest postings first), but 5,814 of 117,460 postings carry no
    # date_posted and 339 of the 1,976 on Kanishk's board do, measured
    # 2026-09-11. Every sort here is NULLS LAST, so those 17 percent would
    # sink together into one arbitrary block. added_at breaks that tie with
    # the next best thing we know, when we first saw it.
    #
    # added_at alone was the old default and is worse than it sounds: it is a
    # catalog-load timestamp, so the 58,000 postings the reseed of 09-04 and
    # 09-05 brought in ordered the top of the board by a database rebuild for
    # days. A lens still overrides this; it is only the starting order.
    "board_default_sort": ConfigKey(
        section="Boards",
        default=[{"key": "date_posted", "dir": "desc"}, {"key": "added_at", "dir": "desc"}],
        value_type=list[SortKey],
        help="How the board is ordered before a person sorts it or picks a lens. "
        "Keys come from the board's sortable columns; later entries break ties.",
    ),
    # The board a person sees before they touch a column: Kanishk's own
    # layout on 2026-09-09 (order, hidden columns, pins, widths; no sort,
    # the lenses own that). Served by GET /user/settings when the row
    # holds no layout, so a new account and a reset both start here; the
    # column chooser still shows any hidden column.
    "board_default_column_layout": ConfigKey(
        section="Boards",
        default=[
            {"colId": "company", "hide": False, "pinned": "left", "width": 160},
            {"colId": "size", "hide": True, "width": 120},
            {"colId": "location", "hide": False, "width": 150},
            {"colId": "comp", "hide": False, "width": 130},
            {"colId": "source", "hide": True, "width": 120},
            {"colId": "ats", "hide": True, "width": 130},
            {"colId": "url", "hide": False, "width": 110},
            {"colId": "title", "hide": False, "width": 210},
            {"colId": "terms", "hide": True, "width": 170},
            {"colId": "recruiter", "hide": True, "width": 140},
            {"colId": "connection1", "hide": True, "width": 150},
            {"colId": "connection2", "hide": True, "width": 150},
            {"colId": "documents", "hide": True, "width": 140},
            {"colId": "added_at", "hide": False, "width": 130},
            {"colId": "date_posted", "hide": False, "width": 130},
            {"colId": "date_applied", "hide": False, "width": 140},
            {"colId": "waiting", "hide": True, "width": 110},
            {"colId": "status", "hide": False, "width": 220},
            {"colId": "notes", "hide": True, "width": 200},
            {"colId": "row_actions", "hide": False, "pinned": "right", "width": 44},
        ],
        value_type=list[ColumnState],
        kind="columns",
        help="Column state a board starts with before the person changes a column: "
        "AG Grid column state entries (colId, hide, pinned, width).",
    ),
    # Minutes between recomputes of every person's board membership. A
    # preference write recomputes within a minute regardless; this is how
    # long a new verdict waits to reach a board. Kanishk: minutes, never a day.
    "board_refresh_minutes": ConfigKey(
        section="Boards",
        default=3,
        value_type=PositiveInt,
        help="Minutes between recomputes of every person's board; a preference change recomputes sooner.",
    ),
    # At 100 inputs per provider request this is 20 requests, and the
    # original corpus drained in 11 cycles.
    "embed_postings_per_cycle": ConfigKey(
        section="Boards",
        default=2000,
        value_type=PositiveInt,
        help="Postings the hourly similarity embedding sweep embeds per cycle, in one "
        "provider batch. A cycle waits for the previous batch to return.",
    ),
}
