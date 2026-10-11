"""Board format identity and the rules attached to each listing format."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse

from core.fetching.boards.amazon import _amazon
from core.fetching.boards.apple import _apple
from core.fetching.boards.ashby import _ashby
from core.fetching.boards.avature import _avature
from core.fetching.boards.bytedance import _BYTEDANCE_HOSTS, _BYTEDANCE_SEARCH, _bytedance
from core.fetching.boards.eightfold import _eightfold
from core.fetching.boards.goldman import _goldman
from core.fetching.boards.greenhouse import _greenhouse
from core.fetching.boards.ibm import _ibm
from core.fetching.boards.icims import _icims
from core.fetching.boards.jibe import _jibe
from core.fetching.boards.lever import _lever
from core.fetching.boards.markdown import _markdown
from core.fetching.boards.oracle import _oracle
from core.fetching.boards.smartrecruiters import _smartrecruiters
from core.fetching.boards.successfactors import _successfactors
from core.fetching.boards.taleo import _TALEO_SECTION, _taleo
from core.fetching.boards.workable import _workable
from core.fetching.boards.workday import _workday
from core.fetching.hosts import EIGHTFOLD_PCSX, EIGHTFOLD_V2
from core.fetching.posting import JobPosting


@dataclass(frozen=True, slots=True)
class ListingAddress:
    host: str
    path: str

    @classmethod
    def from_url(cls, url: str) -> ListingAddress:
        parsed = urlparse(url)
        return cls(parsed.netloc.lower(), parsed.path)


@dataclass(frozen=True, slots=True)
class BoardFormat:
    """One listing format's identity, fetcher, and source/closure rules."""

    name: str
    matches: Callable[[ListingAddress], bool]
    fetcher: Callable[[str, str], list[JobPosting]] | None
    needs_company: bool = field(default=False, kw_only=True)
    authoritative: bool = field(default=False, kw_only=True)

    def __post_init__(self) -> None:
        if self.needs_company and not self.authoritative:
            raise ValueError(f"{self.name}: a company board must prove a complete pull")


# A company board lists every open posting, so a complete pull can mark absent
# postings unlisted. Aggregator feeds do not prove closure. Taleo, iCIMS,
# Jibe, Eightfold and Avature report a partial pull when completeness fails.
# A board needs its source company when its rows omit the employer or name a
# legal entity or department instead of the employer people search for.
FORMATS = (
    BoardFormat(
        "greenhouse",
        lambda a: a.host == "boards-api.greenhouse.io",
        _greenhouse,
        authoritative=True,
    ),
    BoardFormat(
        "lever",
        lambda a: a.host in ("api.lever.co", "api.eu.lever.co"),
        _lever,
        needs_company=True,
        authoritative=True,
    ),
    BoardFormat(
        "ashby",
        lambda a: a.host == "api.ashbyhq.com",
        _ashby,
        needs_company=True,
        authoritative=True,
    ),
    BoardFormat(
        "workday",
        lambda a: a.host.endswith("myworkdayjobs.com") and "/wday/cxs/" in a.path,
        _workday,
        needs_company=True,
        authoritative=True,
    ),
    BoardFormat(
        "smartrecruiters",
        lambda a: a.host == "api.smartrecruiters.com",
        _smartrecruiters,
        authoritative=True,
    ),
    BoardFormat(
        "oracle",
        lambda a: a.host.endswith("oraclecloud.com") and "/hcmRestApi/" in a.path,
        _oracle,
        needs_company=True,
        authoritative=True,
    ),
    BoardFormat(
        "workable",
        lambda a: a.host == "apply.workable.com" and a.path.startswith("/api/"),
        _workable,
        needs_company=True,
        authoritative=True,
    ),
    BoardFormat(
        "avature",
        lambda a: a.host.endswith(".avature.net"),
        _avature,
        needs_company=True,
        authoritative=True,
    ),
    BoardFormat(
        "taleo",
        lambda a: a.host.endswith(".taleo.net") and bool(_TALEO_SECTION.match(a.path)),
        _taleo,
        needs_company=True,
        authoritative=True,
    ),
    BoardFormat(
        "apple",
        lambda a: a.host == "jobs.apple.com" and a.path.startswith("/api/"),
        _apple,
        needs_company=True,
        authoritative=True,
    ),
    BoardFormat(
        "bytedance",
        lambda a: a.host in _BYTEDANCE_HOSTS and a.path == _BYTEDANCE_SEARCH,
        _bytedance,
        needs_company=True,
        authoritative=True,
    ),
    # An iCIMS portal can use any tenant subdomain. Its search path identifies it.
    BoardFormat(
        "icims",
        lambda a: a.host.endswith(".icims.com") and a.path.rstrip("/") == "/jobs/search",
        _icims,
        needs_company=True,
        authoritative=True,
    ),
    # Jibe runs on employer domains. Only its API path identifies the format.
    BoardFormat("jibe", lambda a: a.path.rstrip("/") == "/api/jobs", _jibe, authoritative=True),
    BoardFormat(
        "ibm",
        lambda a: a.host == "www-api.ibm.com" and a.path == "/search/api/v2",
        _ibm,
        needs_company=True,
        authoritative=True,
    ),
    BoardFormat(
        "goldman",
        lambda a: a.host == "api-higher.gs.com" and a.path == "/gateway/api/v1/graphql",
        _goldman,
        needs_company=True,
        authoritative=True,
    ),
    BoardFormat(
        "amazon",
        lambda a: a.host == "www.amazon.jobs" and a.path.endswith("/search.json"),
        _amazon,
        needs_company=True,
        authoritative=True,
    ),
    # SuccessFactors Career Site Builder also runs on employer domains.
    BoardFormat(
        "successfactors",
        lambda a: a.path == "/services/rss/job/",
        _successfactors,
        needs_company=True,
        authoritative=True,
    ),
    # Eightfold's listing path is the marker across its tenant domains.
    BoardFormat(
        "eightfold",
        lambda a: a.path.rstrip("/") in (EIGHTFOLD_PCSX, EIGHTFOLD_V2),
        _eightfold,
        needs_company=True,
        authoritative=True,
    ),
    BoardFormat("markdown", lambda a: a.path.endswith(".md"), _markdown),
)
SHEET_ERA = BoardFormat("sheet_era", lambda a: True, None)


def format_for_url(url: str) -> BoardFormat:
    address = ListingAddress.from_url(url)
    return next(
        (board_format for board_format in FORMATS if board_format.matches(address)), SHEET_ERA
    )
