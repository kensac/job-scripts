from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, PositiveInt, TypeAdapter

from api.apply.policy import ExtensionPolicy

logger = logging.getLogger("jobtracker_api")
ALL_GROUPS = "*"


class ColumnState(BaseModel):
    # Additional grid state survives writes, so new frontend columns and state
    # properties do not require a synchronized backend release.
    model_config = ConfigDict(extra="allow", strict=True)

    colId: str = Field(min_length=1)
    hide: bool | None = None
    pinned: Literal["left", "right"] | None = None
    width: PositiveInt | None = None


@dataclass(frozen=True)
class ConfigKey:
    default: JsonValue
    value_type: Any
    help: str
    # Which group of settings the admin page files this key under. The
    # registry is the only place that knows; a page grouping twenty keys
    # by guessing at their names guesses wrong the first time one is
    # renamed.
    section: str = "General"
    choices: tuple[str, ...] = ()
    kind: Literal["value", "text", "groups", "hosts", "columns"] = "value"
    _adapter: TypeAdapter = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_adapter", TypeAdapter(self.value_type))
        self.validate(self.default)

    @property
    def type(self) -> type:
        return type(self.default)

    def validate(self, value: JsonValue) -> JsonValue:
        parsed = self._adapter.validate_python(value, strict=True)
        if self.choices and parsed not in self.choices:
            raise ValueError(f"takes one of {', '.join(self.choices)}")
        if self.kind == "groups" and any(not group.strip() for group in parsed):
            raise ValueError("takes nonempty group names")
        if self.kind == "hosts":
            normalized = {}
            for host, rate in parsed.items():
                name = host.strip().lower()
                if not name or name in normalized:
                    raise ValueError("takes distinct nonempty host names")
                normalized[name] = rate
            return normalized
        if self.kind == "columns":
            if len({column.colId for column in parsed}) != len(parsed):
                raise ValueError("takes distinct column identifiers")
            return [column.model_dump(exclude_unset=True) for column in parsed]
        return self._adapter.dump_python(parsed, mode="json")


CONFIG_KEYS: dict[str, ConfigKey] = {
    "extension_policy": ConfigKey(
        section="Extension",
        default=ExtensionPolicy().model_dump(mode="json"),
        value_type=ExtensionPolicy,
        help="Controls bundled extension features and disables adapters by reader ID. "
        "One atomic policy; no scripts, selectors or navigation commands. "
        "Requires an extension supporting configuration schema 1; older releases ignore it.",
    ),
    "signups_enabled": ConfigKey(
        section="Access",
        default=True,
        value_type=bool,
        help="Whether brand-new people can create tracker accounts. Existing users, and the internal and admin groups, get in either way.",
    ),
    # Testing-mode OAuth is limited to 100 test users; keep the default
    # closed to other groups until the client is ready for them.
    "gmail_connect_groups": ConfigKey(
        section="Access",
        default=["infra-admins"],
        value_type=list[str],
        kind="groups",
        help="Authentik groups whose members may connect a mailbox.",
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
    # How long a posting whose page fetch came back empty waits before any
    # ingest or backfill tries it again. The hourly cycle used to be the
    # retry: 24 attempts a day at the same dead URL from every worker.
    "fetch_retry_after_hours": ConfigKey(
        section="Fetching",
        default=24,
        value_type=PositiveInt,
        help="Hours a posting whose page fetch came back empty waits before any "
        "ingest or backfill tries it again.",
    ),
    # An idle worker beside pending work for this long is a stall: a claim
    # takes one poll (JOBTRACKER_WORKER_POLL, 5s), so ten minutes is not
    # latency. Kinds allowlists (JOBTRACKER_WORKER_KINDS) are the one
    # legitimate reason, and the alert names them.
    "queue_stall_minutes": ConfigKey(
        section="Health",
        default=10,
        value_type=PositiveInt,
        help="Minutes an idle worker may sit beside pending work before the queue counts as stalled.",
    ),
    # Pending ingests older than this many ingest cycles mean the fleet is
    # behind the hour; one cycle is the normal wait.
    "ingest_backlog_cycles": ConfigKey(
        section="Health",
        default=2,
        value_type=PositiveInt,
        help="Pending ingests older than this many hourly cycles mean the fleet is behind.",
    ),
    # How long a posting a title pattern screened out stays on record after
    # its board stops listing it. Long enough to evaluate a new pattern
    # against a month of what the boards actually posted.
    "screened_retention_days": ConfigKey(
        section="Catalog",
        default=30,
        value_type=PositiveInt,
        help="Days a posting a title pattern screened out stays on record after its "
        "board stops listing it.",
    ),
    # A parked task resumes once every batch is terminal, or once the ones
    # still running are older than this while others have finished: the
    # finished ones are collected and the task parks again on the rest.
    # Provider batches normally land within the hour; the stragglers seen
    # on 2026-09-04 sat 14 hours at a few requests short.
    "batch_straggler_hours": ConfigKey(
        section="Health",
        default=4,
        value_type=PositiveInt,
        help="Hours a still-running provider batch may lag its finished siblings "
        "before the task collects those and parks again on it.",
    ),
    # Host -> page fetches per hour, fleet-wide. Empty means no host is paced;
    # a host that blocks bursts (www.tesla.com blocked 19 of 32 in a day
    # after 12 in an hour) is added here, from the alert, at the rate it
    # tolerates. Hosts are data, so none is written into code.
    "fetch_host_limits": ConfigKey(
        section="Fetching",
        default={},
        value_type=dict[str, PositiveInt],
        kind="hosts",
        help="Host to page fetches per hour, fleet-wide. A host that blocks bursts is "
        "drip-fed at this rate instead of being pulled off; the fetch-failure "
        "alert names the host.",
    ),
    # Host -> seconds between LISTING requests per worker process. Workable
    # limits by address and two workers share hetzner's; six seconds was not
    # enough, twenty holds. Read by core.fetching.boards through the ingest task.
    "ingest_host_pace_seconds": ConfigKey(
        section="Fetching",
        default={"apply.workable.com": 20},
        value_type=dict[str, PositiveInt],
        kind="hosts",
        help="Host to the smallest gap in seconds between pulls from one address, "
        "and between pages inside a pull. The fleet learns the actual gap per "
        "host and address from refusals (GET /admin/host-budgets); this is the "
        "floor it never goes under, not a ceiling.",
    ),
    # Which engine fetches a posting page after the ATS resolvers decline.
    # static_first tries a browserless fetch with a real Chrome fingerprint
    # and falls through to the browser unless the page plainly came back
    # whole; chromium goes straight to the browser as before. Switchable
    # without a roll, so the share each engine serves can be read off the
    # content rows (reason 'static' vs 'scraped') and the choice revisited.
    "fetch_engine": ConfigKey(
        section="Fetching",
        default="static_first",
        value_type=str,
        help="Which engine fetches a posting page after the ATS resolvers decline. "
        "static_first tries a browserless fetch and falls back to the browser "
        "unless the page came back whole; chromium goes straight to the browser.",
        choices=("chromium", "static_first"),
    ),
    # The text-length gate under static_first; see api.fetching.fetch_static
    # for the measurement behind 1,500.
    "static_fetch_min_chars": ConfigKey(
        section="Fetching",
        default=1500,
        value_type=PositiveInt,
        help="Characters of text a browserless fetch must return to be served instead of the browser.",
    ),
    # How long GET /admin/stats serves the same answer. The dashboard refetches
    # it on every worker event: 642 calls a day at 583 ms each, five full
    # scans of ai_queries per call, a quarter of all server time on the box
    # for lifetime totals that move once an hour. 1 is as good as off.
    "admin_stats_cache_seconds": ConfigKey(
        section="Health",
        default=60,
        value_type=PositiveInt,
        help="Seconds GET /admin/stats serves the same answer.",
    ),
    # Minutes a worker may heartbeat on a release other than the api's before
    # that is a host that did not deploy. A lockstep roll finishes inside ten,
    # but gcp-vps is deployed by hand until its runner exists, so the window
    # covers a person noticing a roll request; tighten it when every host
    # self-deploys. gcp-vps ran an hour behind on 2026-09-04 unnoticed.
    "fleet_roll_minutes": ConfigKey(
        section="Health",
        default=120,
        value_type=PositiveInt,
        help="Minutes a worker may run a different release from the api before it counts as a "
        "host that did not deploy.",
    ),
    # Distinct location strings classified per hourly cycle. The backlog is
    # 8,735 strings; set low for a first look at GET /admin/locations, then
    # raised to clear it in one cycle.
    "classify_locations_per_cycle": ConfigKey(
        section="Catalog",
        default=10000,
        value_type=PositiveInt,
        help="Distinct location strings the hourly classification cycle sends to the model.",
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
    # The hourly application sweep, per person: how many unread forms it
    # reads (one request each to the ATS, under the host budget; 1,307
    # readable postings were on the board on 2026-09-06, so the first pass
    # takes a working day at this rate and the ATSs see a trickle) and how
    # many missing drafts it batches (about $0.0005 each on luna).
    "application_form_reads_per_cycle": ConfigKey(
        section="Applications",
        default=150,
        value_type=PositiveInt,
        help="Application forms the hourly sweep reads per person per cycle, newest postings "
        "first, one request each to the ATS under the host budget.",
    ),
    "resumes_per_user": ConfigKey(
        section="Applications",
        default=10,
        value_type=PositiveInt,
        help="Resumes one person may keep. Each holds its PDF (5 MB at most) in the database, "
        "and the database is dumped and archived daily, so this bounds four copies of "
        "every upload.",
    ),
    # Empty means the built-in text in the code; see the admin registry.
    "application_draft_instructions": ConfigKey(
        section="Applications",
        kind="text",
        default="",
        value_type=str,
        help="The rules the model drafts application answers under, before the person's own "
        "writing style. Empty means the built-in text in tasks.application; a change "
        "here takes effect on the next draft, with no roll. Read by application_draft, "
        "application_sweep and the refine endpoint.",
    ),
    "application_suggest_instructions": ConfigKey(
        section="Applications",
        kind="text",
        default="",
        value_type=str,
        help="The rules the model fills the rest of an application form under (the fields the "
        "profile and drafts did not). Empty means the built-in text in api.routers.apply. "
        "Read by POST /user/apply/suggest.",
    ),
    # The model never fills these on a form; the person does. One label a
    # line or comma-separated, whole words in the field's label or key.
    "application_ai_never_fills": ConfigKey(
        section="Applications",
        default="location",
        value_type=str,
        help="Fields the model never fills on an application form, left to the person: one "
        "label a line or comma-separated, matched as whole words in the field's label or "
        "key. Read by POST /user/apply/suggest, which drops them before the call and names "
        "them in its reply so the extension lists them as the person's. Empty is not off: "
        "an empty list lets the model fill every field, the location box included.",
    ),
    "application_drafts_per_cycle": ConfigKey(
        section="Applications",
        default=500,
        value_type=PositiveInt,
        help="Missing application answers the hourly sweep drafts per person per cycle, in one "
        "half-price batch; about $0.0005 each on the sanctioned model.",
    ),
    # Whether the hourly requirements extraction runs. Off since 2026-09-07:
    # its one consumer is the market table, and the deployed arm was measured
    # at a third of the reference's skills with seniority mostly blank. Turn
    # on from the admin config page after choosing a model worth paying for.
    "requirements_extraction_enabled": ConfigKey(
        section="Catalog",
        default=False,
        value_type=bool,
        help="Whether the hourly requirements extraction runs. Off since 2026-09-07: its one "
        "consumer is the market table and the deployed arm measured poorly; pick an arm "
        "with an experiment before turning it on.",
    ),
}


def group_access_allowed(key: str, groups: list[str]) -> bool:
    from api import db

    spec = CONFIG_KEYS[key]
    if spec.kind != "groups":
        raise ValueError(f"{key} is not a group policy")
    try:
        allowed = spec.validate(db.get_config(key))
    except ValueError:
        logger.warning("%s has invalid group configuration, refusing access", key)
        return False
    return isinstance(allowed, list) and (
        ALL_GROUPS in allowed or any(group in allowed for group in groups)
    )
