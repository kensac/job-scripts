from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    NonNegativeInt,
    PositiveInt,
    TypeAdapter,
)

from api.apply.policy import ExtensionPolicy
from core.filter_policy import RoutingPolicy
from core.review_gate import ReviewGatePolicy, VolumeGate
from core.shapes import (
    CLASSIFY_LOCATIONS_PER_CYCLE,
    CLASSIFY_PER_CYCLE,
    EXTRACT_COMP_PER_CYCLE,
    EXTRACT_REQUIREMENTS_PER_CYCLE,
    JOB_PROFILE_TASK,
    LOCATIONS_TASK,
)

logger = logging.getLogger(__name__)
ALL_GROUPS = "*"


class ColumnState(BaseModel):
    # Additional grid state survives writes, so new frontend columns and state
    # properties do not require a synchronized backend release.
    model_config = ConfigDict(extra="allow", strict=True)

    colId: str = Field(min_length=1)
    hide: bool | None = None
    pinned: Literal["left", "right"] | None = None
    width: PositiveInt | None = None


class SortKey(BaseModel):
    """One term of the board's ordering. `key` must be a column the board
    knows how to sort by; the board refuses an unknown one rather than
    ordering by something else, which is why this is validated here and not
    at the point it is read."""

    model_config = ConfigDict(strict=True)

    key: str = Field(min_length=1)
    dir: Literal["asc", "desc"] = "desc"


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
    "embedding_visible_only": ConfigKey(
        section="Boards",
        default=True,
        value_type=bool,
        help="Buy new similarity embeddings only for postings visible on a personal board, "
        "including uploads and postings a person acted on. Existing vectors and paid batches "
        "are retained. Newly visible postings become eligible on the next embedding sweep; "
        "similar jobs may wait for its batch to complete. Turning this off restores broad "
        "collection. This does not change filter decisions or public-board membership.",
    ),
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
    "verification_volume_gate": ConfigKey(
        section="Boards",
        default=VolumeGate().model_dump(mode="json"),
        value_type=VolumeGate,
        help="Postings verification skips for the listed boards and filters (exact prompt "
        "hashes in scopes; empty means off). Skips sources with at least min_judged postings "
        "judged in window_days and no keep by any board or filter, except audit_percent of "
        "their postings, so a source that starts producing keeps returns by itself. Also "
        "skips titles naming a listed occupation with no technical word. A posting someone "
        "tracks is always read. A new prompt hash is not listed until added here.",
    ),
    "filter_review_gate": ConfigKey(
        section="Boards",
        default=ReviewGatePolicy().model_dump(mode="json"),
        value_type=ReviewGatePolicy,
        help="Independent off/shadow/enforce controls for conservative nontechnical title "
        "exclusions and shared-profile reuse. Scopes explicitly opt in exact filter prompt "
        "hashes. Unknown evidence receives detailed review. Never creates cached verdicts "
        "or cancels submitted batches. Rollback affects new submissions; skipped jobs "
        "become eligible again on the next run. Task payloads retain the funnel.",
    ),
    "filter_routing_policy": ConfigKey(
        section="Boards",
        default=RoutingPolicy().model_dump(mode="json"),
        value_type=RoutingPolicy,
        help="Independent off/shadow controls for shared profiles, title screening and "
        "ambiguity-only review. Profiles are keyed by exact filter prompt hash. "
        "Shadow mode preserves every detailed review and records comparisons on tasks. "
        "No live skipping is supported until decision quality and rollback are validated.",
    ),
    "compensation_demand_gate_enabled": ConfigKey(
        section="Catalog",
        default=False,
        value_type=bool,
        help="Extract compensation only after a posting passes an enabled personal filter, "
        "appears on a published managed board, or is personally tracked or uploaded. "
        "Existing verification and content freshness checks still apply. "
        "Already submitted batches finish normally.",
    ),
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
    # How long a posting whose page fetch came back empty waits before any
    # automatic path tries it again, after its first failure. The hourly
    # cycle used to be the retry: 24 attempts a day at the same dead URL from
    # every worker. Each further consecutive failure doubles the wait
    # (verdicts.fetch_parked_sql).
    "fetch_retry_after_hours": ConfigKey(
        section="Fetching",
        default=24,
        value_type=PositiveInt,
        help="Hours a posting whose page fetch came back empty waits before any "
        "automatic path tries it again. Each further consecutive failure doubles the wait.",
    ),
    # The longest the doubling wait grows to. A week: with the 24 h base the
    # retries land on days 1, 3, 7, 14 and 21, so a page that is down for a
    # weekend or blocked for a few days still comes back inside a month.
    "fetch_retry_max_hours": ConfigKey(
        section="Fetching",
        default=168,
        value_type=PositiveInt,
        help="Longest wait, in hours, between automatic retries of a posting whose "
        "page keeps coming back empty.",
    ),
    # Consecutive empty fetches after which no automatic path tries the page
    # again; an admin re-check still does, and a success resets the count.
    # Six spans about three weeks under the two defaults above. Before this
    # the 3,511 postings that had never fetched averaged 9 attempts and
    # reached 50 (production, 2026-10-04), on a flat day each.
    "fetch_give_up_after_failures": ConfigKey(
        section="Fetching",
        default=6,
        value_type=PositiveInt,
        help="Consecutive empty fetches after which a posting is unfetchable: no "
        "automatic path retries it until a manual re-check succeeds.",
    ),
    # The longest a board that keeps failing waits between pulls. After the
    # k-th failure in a row the wait is the board's own interval times
    # 2^(k-1), never less than the interval. Every failure run that ended on
    # its own in the 30 days to 2026-10-04 (502s, 403s) ended within 53 hours,
    # so a three-day cap still pulls a board that came back within three days
    # of its return.
    "ingest_retry_max_hours": ConfigKey(
        section="Fetching",
        default=72,
        value_type=PositiveInt,
        help="Longest wait, in hours, between pulls of a board whose pulls keep failing. "
        "Each failure in a row doubles the board's interval up to this.",
    ),
    # Failed pulls in a row after which the board is switched off, which
    # retires its postings. Eight spans about five days on an hourly board and
    # eighteen on a daily one under the cap above. Before this a failed pull
    # did not count toward the interval: twelve boards, most answering 404,
    # were pulled every hour for up to eleven days, and every board together
    # failed 1,509 pulls in the week to 2026-10-04.
    "ingest_give_up_after_failures": ConfigKey(
        section="Fetching",
        default=8,
        value_type=PositiveInt,
        help="Failed pulls in a row after which a board is switched off and its postings "
        "retired. Switch it back on once its listings URL is fixed; one more failure "
        "switches it off again, and one success ends the run.",
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
    # A live handler that has not changed its progress for this long is wedged,
    # even when its timer heartbeat remains fresh. Measured 2026-09-11: the
    # completed seven-day sample's p95 finish-after-progress gap was under 0.22
    # minutes and its maximum was 15.4 minutes across sampled kinds. The live
    # match_mail task that looked stopped for 166.5 minutes was not: it wrote
    # progress once per user, and production has one. 128 of the 235 stall
    # alerts in the 30 days to 2026-10-04 were that, and it now writes progress
    # every 100 messages.
    "task_progress_stall_minutes": ConfigKey(
        section="Health",
        default=30,
        value_type=PositiveInt,
        help="Minutes a running task may go without changing its reported progress, counted from its last progress or its current claim, whichever is later, before it counts as stalled.",
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
    # How stale a listing's last_seen_at may get before a pull that still
    # lists it rewrites the row only to move it. Retention is the one thing
    # the timestamp decides, and it is counted in whole days, so a day is the
    # finest distinction it can draw. Refreshed every pull instead, the
    # timestamp alone rewrote 1.17M rows a day, 99.2% of them otherwise
    # unchanged (pg_stat_statements, 2026-10-03).
    "listings_seen_refresh_hours": ConfigKey(
        section="Catalog",
        default=24,
        value_type=PositiveInt,
        help="Hours a listed posting's last-seen time may lag before a pull rewrites the row "
        "just to update it. A posting is kept between screened_retention_days and that plus "
        "this many hours after its board last listed it.",
    ),
    "source_title_patterns_enabled": ConfigKey(
        section="Catalog",
        default=True,
        value_type=bool,
        help="Whether source title patterns restrict which fetched postings enter the catalog. "
        "When disabled, every fetched posting is admitted while listings.kept still records "
        "whether the stored pattern matched, so the bypass remains measurable and reversible.",
    ),
    "verification_reachability_gate_enabled": ConfigKey(
        section="Catalog",
        default=False,
        value_type=bool,
        help="Restrict new posting verification to jobs that can reach an entitled personal "
        "filter or a published managed board after free date, location and enforced title gates. "
        "Tracked and directly imported jobs remain eligible.",
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
    # "eightfold.ai" paces every Eightfold tenant's pages together, whatever
    # domain it serves from, because one WAF fronts them: on 2026-10-05 about
    # 110 requests a minute from one address held for ten minutes, and
    # thirteen tenants pulled at once with no pace put that address behind a
    # captcha on every tenant tried but Microsoft. The limit itself was not
    # measured. One second keeps a worker near 60 a minute.
    "ingest_host_pace_seconds": ConfigKey(
        section="Fetching",
        default={"apply.workable.com": 20, "eightfold.ai": 1},
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
    # scans of ai_queries per call when measured (one scan since 2026-10-04),
    # a quarter of all server time on the box for lifetime totals that move
    # once an hour. 1 is as good as off.
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
    "health_warning_digest_hour_utc": ConfigKey(
        section="Health",
        default=13,
        value_type=Annotated[int, Field(ge=0, le=23)],
        help="UTC hour for the daily warning digest. Critical incidents are sent immediately; "
        "a missed digest is delivered on the next health run. All alerts remain in the UI.",
    ),
    "classify_locations_max_output_tokens": ConfigKey(
        section="Catalog",
        default=LOCATIONS_TASK.max_output_tokens,
        value_type=Annotated[int, Field(ge=256, le=64000)],
        help="Output-token ceiling for new location classification batches. Multi-city strings "
        "need room for every place. Incomplete answers remain rejected; existing paid batches "
        "keep their original limit. Actual spend uses tokens emitted, not this ceiling.",
    ),
    "health_notification_repeat_hours": ConfigKey(
        section="Health",
        default=24,
        value_type=PositiveInt,
        help="Minimum hours between emails for reopened instances of the same incident. "
        "Task stalls are grouped by task kind. A warning escalating to critical bypasses this delay.",
    ),
    # Distinct location strings classified per hourly cycle. The backlog is
    # 8,735 strings; set low for a first look at GET /admin/locations, then
    # raised to clear it in one cycle.
    "classify_locations_per_cycle": ConfigKey(
        section="Catalog",
        default=CLASSIFY_LOCATIONS_PER_CYCLE,
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
        "writing style. Empty means the built-in text in api.apply.drafting; a change "
        "here takes effect on the next draft, with no roll. Read by application_draft, "
        "application_sweep and the refine endpoint. Shared evidence and company-motivation "
        "guidance always applies after these rules and the writing style.",
    ),
    "application_suggest_instructions": ConfigKey(
        section="Applications",
        kind="text",
        default="",
        value_type=str,
        help="The rules the model fills the rest of an application form under (the fields the "
        "profile and drafts did not). Empty means the built-in text in api.routers.apply. "
        "Read by POST /user/apply/suggest. The person's writing style and shared evidence "
        "and company-motivation guidance are appended to these rules.",
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
    "job_profile_collection_enabled": ConfigKey(
        section="Catalog",
        default=True,
        value_type=bool,
        help="Collect new shared job profiles. Turning this off pauses scheduled, manual, "
        "and queued work before submission. Existing profiles stay available and submitted "
        "batches still finish. Turning it back on resumes collection of eligible postings.",
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
    "mail_classification_enabled": ConfigKey(
        section="Mail",
        default=True,
        value_type=bool,
        help="Whether mailbox syncs, archive imports, and the hourly scheduler enqueue new "
        "mail classification work. Turning it off does not discard already submitted batches.",
    ),
    # Each per-cycle size below was an environment variable or a literal
    # until 2026-10-04. None was set on any host in homelab-config, so every
    # default is the value production ran. A size derived in core.shapes
    # keeps its derivation there, beside the number.
    "comp_extract_per_cycle": ConfigKey(
        section="Catalog",
        default=EXTRACT_COMP_PER_CYCLE,
        value_type=PositiveInt,
        help="Postings the hourly compensation extraction sends to the model per cycle. "
        "The fleet budget prices a cycle at this many.",
    ),
    "requirements_extract_per_cycle": ConfigKey(
        section="Catalog",
        default=EXTRACT_REQUIREMENTS_PER_CYCLE,
        value_type=PositiveInt,
        help="Postings the hourly requirements extraction sends to the model per cycle, "
        "when requirements_extraction_enabled is on. The fleet budget prices a cycle at "
        "this many.",
    ),
    "mail_classify_per_cycle": ConfigKey(
        section="Mail",
        default=CLASSIFY_PER_CYCLE,
        value_type=PositiveInt,
        help="Messages one mail classification task sends to the model, for the hourly "
        "sweep and an archive backfill alike. The default fills every batch wave that runs "
        "at once; more only waits in the provider's queue. The fleet budget prices a cycle "
        "at this many.",
    ),
    "job_profiles_per_cycle": ConfigKey(
        section="Catalog",
        default=JOB_PROFILE_TASK.per_cycle,
        value_type=PositiveInt,
        help="Postings the shadow job-profile classification sends to the model per cycle. "
        "The fleet budget prices a cycle at this many.",
    ),
    # The newest first, so a backlog cannot starve the day's postings: on
    # 2026-09-15 214,306 active postings held no closed verdict and 3,440 of
    # them were posted within three days, so one cycle covers every fresh
    # posting and the remainder drains behind it.
    "verify_new_per_cycle": ConfigKey(
        section="Catalog",
        default=4000,
        value_type=PositiveInt,
        help="Postings without a closed or clearance verdict that the hourly verification "
        "sends to the model per cycle, newest first.",
    ),
    "reverify_days": ConfigKey(
        section="Catalog",
        default=7,
        value_type=PositiveInt,
        help="Days a posting on someone's board keeps its closed verdict before the hourly "
        "re-verification fetches the page and asks again.",
    ),
    # An emergency brake, not a size: the re-check population is whatever
    # went stale, and the shadow report compares that whole population and
    # shows this cap beside it.
    "reverify_per_cycle": ConfigKey(
        section="Catalog",
        default=0,
        value_type=NonNegativeInt,
        help="Most postings one re-verification cycle re-checks. 0 means no limit.",
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
    "content_backfill_per_cycle": ConfigKey(
        section="Fetching",
        default=100,
        value_type=PositiveInt,
        help="Never-fetched postings the hourly content backfill fetches per cycle, newest "
        "first. A manual run may pass its own limit.",
    ),
    # Gmail's own page size is 500 and a per-message get is one quota unit,
    # so this bounds one task rather than the provider: a sync that cannot
    # finish resumes next cycle from the messages not yet stored.
    "mail_sync_per_cycle": ConfigKey(
        section="Mail",
        default=500,
        value_type=PositiveInt,
        help="New messages one Gmail sync stores per mailbox. The rest wait for the next cycle.",
    ),
    # The scheduler buckets the hour by this, so it must divide 60 for every
    # bucket to be the same length.
    "ingest_interval_minutes": ConfigKey(
        section="Fleet",
        default=60,
        value_type=Literal[1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60],
        help="Minutes between scheduler cycles: board pulls, mail sync and every hourly sweep. "
        "Divides 60. The queue backlog alert counts in these cycles.",
    ),
    # A week's fleet ceiling as a multiple of one sweep of every task at its
    # current model, so it moves with the design instead of needing a new
    # dollar figure each time a task or model changes. One sweep was $11.71
    # and the busiest week spent $29.19, about 2.5 sweeps, because sweeps
    # mostly find nothing to do. 24 leaves about ten times that headroom; a
    # single task switched to the dearest model it can reach costs $30 an
    # hour and breaches it inside a day.
    "fleet_weekly_cycles": ConfigKey(
        section="Fleet",
        default=24,
        value_type=NonNegativeInt,
        help="Weekly fleet spend ceiling, in full sweeps of every task at its current model. "
        "Batched submissions stop once the week passes it. 0 disables the check, which is "
        "how a deliberate backfill runs.",
    ),
    # The shared queue load-balances by availability, so fast workers claim
    # more chunks; the size bounds how much work one lost worker takes with it.
    "filter_chunk_size": ConfigKey(
        section="Fleet",
        default=100,
        value_type=PositiveInt,
        help="Checks per chunk when a live filter run or a re-verification sweep is split "
        "across the fleet.",
    ),
    "filter_batch_chunk_size": ConfigKey(
        section="Fleet",
        default=500,
        value_type=PositiveInt,
        help="Postings per chunk when a scheduled filter run is split across the fleet. "
        "Each chunk submits its own half-price provider batch.",
    ),
    "task_max_attempts": ConfigKey(
        section="Fleet",
        default=3,
        value_type=PositiveInt,
        help="Claims a task gets before a lost worker or a transient error fails it for good.",
    ),
    # A worker heartbeats every api.worker.HEARTBEAT_SECONDS (60) from its own
    # thread whether or not the event loop is free, so a silent quarter hour is
    # a dead worker, not a slow task. Under two beats a single late beat would
    # requeue a live task under its worker.
    "task_heartbeat_timeout_minutes": ConfigKey(
        section="Fleet",
        default=15,
        value_type=Annotated[int, Field(ge=2)],
        help="Minutes without a heartbeat before the reaper requeues a running task.",
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
