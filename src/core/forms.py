"""The questions an application form asks, read from the ATS that hosts it.

Four hosts publish the form without a sign-in: Greenhouse and Workable as
JSON, Ashby through the GraphQL call its own page makes, Lever as the apply
page's HTML. Measured 2026-09-06 over 24 live postings: every one of those
forms was readable, and about one in three carried a free-response question
beyond a cover letter. Workday and Oracle keep the form behind an account and
SmartRecruiters publishes no questions endpoint; for those `fetch` returns
None and the person pastes the question in.

Only the fields autofill cannot do are kept: a long-text box, and a short
text box that is the employer's own question rather than a name or an email.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from html import unescape
from urllib.parse import parse_qsl, urlparse, urlsplit, urlunsplit

import requests

TIMEOUT = 20
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/html"}


@dataclass
class Question:
    key: str
    label: str
    required: bool
    # long: a paragraph box. short: a one-line box the employer wrote.
    kind: str

    def as_dict(self) -> dict:
        return asdict(self)


def _get(url: str) -> str:
    r = requests.get(url, headers=_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def _post_json(url: str, body: dict) -> str:
    r = requests.post(
        url, json=body, headers={**_HEADERS, "Content-Type": "application/json"}, timeout=TIMEOUT
    )
    r.raise_for_status()
    return r.text


# Uploads and standard identity fields also render as text boxes; a person
# does not want a draft for them.
_GREENHOUSE_SKIP = {"Resume/CV", "Cover Letter"}


def _greenhouse(url: str) -> list[Question] | None:
    m = re.search(r"greenhouse\.io/([^/]+)/jobs/(\d+)", url)
    if not m:
        # The embed url carries a token, not the board slug the API needs.
        return None
    board, job_id = m.groups()
    data = json.loads(
        _get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?questions=true")
    )
    out = []
    for q in data.get("questions") or []:
        label = (q.get("label") or "").strip()
        fields = q.get("fields") or []
        types = {f.get("type") for f in fields}
        if label in _GREENHOUSE_SKIP or not fields:
            continue
        name = fields[0].get("name") or label
        # Custom questions are named question_<id>; everything else is the
        # standard identity block autofill already handles.
        if "textarea" in types:
            out.append(Question(name, label, bool(q.get("required")), "long"))
        elif "input_text" in types and str(name).startswith("question_"):
            out.append(Question(name, label, bool(q.get("required")), "short"))
    return out


_ASHBY_QUERY = """query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) {
    applicationForm { sections { title fieldEntries { field } } }
  }
}"""


def _ashby(url: str) -> list[Question] | None:
    m = re.search(r"ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", url)
    if not m:
        return None
    org, job_id = m.groups()
    data = json.loads(
        _post_json(
            "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting",
            {
                "operationName": "ApiJobPosting",
                "variables": {"organizationHostedJobsPageName": org, "jobPostingId": job_id},
                "query": _ASHBY_QUERY,
            },
        )
    )
    if data.get("errors"):
        raise RuntimeError(str(data["errors"])[:200])
    posting = (data.get("data") or {}).get("jobPosting") or {}
    out = []
    for section in (posting.get("applicationForm") or {}).get("sections") or []:
        for entry in section.get("fieldEntries") or []:
            f = entry.get("field") or {}
            # The field is a JSON scalar on Ashby's side; its type names the
            # widget. A String is a name or a company, never a question.
            if f.get("type") == "LongText":
                key = str(f.get("path") or f.get("id") or f.get("title") or "")
                out.append(
                    Question(key, f.get("title") or "", not f.get("isNullable", True), "long")
                )
    return out


_LEVER_BLOCK = re.compile(r'<li class="application-question[^"]*">(.*?)</li>', re.S)
_LEVER_LABEL = re.compile(r'<div class="application-label[^"]*">(.*?)</div></div>', re.S)
_LEVER_NAME = re.compile(r'<textarea[^>]*name="([^"]+)"')


def _lever(url: str) -> list[Question] | None:
    m = re.search(r"jobs\.lever\.co/([^/]+)/([0-9a-f-]{36})", url)
    if not m:
        return None
    page = _get(f"https://jobs.lever.co/{m.group(1)}/{m.group(2)}/apply")
    out = []
    for block in _LEVER_BLOCK.findall(page):
        name = _LEVER_NAME.search(block)
        if not name:
            continue
        label = _LEVER_LABEL.search(block)
        words = unescape(re.sub(r"<[^>]+>", " ", label.group(1) if label else "")).split()
        required = "✱" in words
        text = " ".join(w for w in words if w != "✱")
        if text in _GREENHOUSE_SKIP or text.lower().startswith("additional information"):
            # Lever's own "Additional information" box is a cover-letter slot.
            continue
        out.append(Question(name.group(1), text, required, "long"))
    return out


_WORKABLE_STANDARD = {"Personal information", "Profile"}


def _workable(url: str) -> list[Question] | None:
    m = re.search(r"workable\.com/[^/]+/j/([A-Z0-9]+)", url)
    if not m:
        return None
    sections = json.loads(_get(f"https://apply.workable.com/api/v1/jobs/{m.group(1)}/form"))
    out = []
    for section in sections:
        standard = section.get("name") in _WORKABLE_STANDARD
        for f in section.get("fields") or []:
            kind = f.get("type")
            label = f.get("label") or ""
            # Workable's own profile boxes, wherever the employer placed them.
            if f.get("id") in ("cover_letter", "summary"):
                continue
            if kind == "paragraph":
                out.append(Question(str(f.get("id")), label, bool(f.get("required")), "long"))
            elif kind == "text" and not standard:
                out.append(Question(str(f.get("id")), label, bool(f.get("required")), "short"))
    return out


_READERS = {
    "greenhouse.io": _greenhouse,
    "ashbyhq.com": _ashby,
    "lever.co": _lever,
    "workable.com": _workable,
}


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def posting_urls(url: str) -> list[str]:
    """The urls the board may store the posting under, for the page the
    form is on. Beside budget_host on purpose: the two are the whole of
    what this module knows about which hosts are one Greenhouse, and a
    second copy of that knowledge is how the first one drifted (#405). Ashby's form lives under /application and Lever's under
    /apply; a Greenhouse form embedded on an employer's site is
    boards.greenhouse.io/embed/job_app?for=<board>&token=<id>, and a
    Greenhouse posting is stored under whichever of its two hosts the
    board listed it from. The query string is otherwise tracking."""
    parts = urlsplit(url)
    if parts.netloc.endswith("greenhouse.io"):
        q = dict(parse_qsl(parts.query))
        m = re.match(r"^/([^/]+)/jobs/(\d+)", parts.path)
        board, job = (
            (q["for"], q["token"])
            if parts.path.startswith("/embed/job_app") and "for" in q and "token" in q
            else (m.group(1), m.group(2))
            if m
            else (None, None)
        )
        if board and job:
            eu = ".eu" if ".eu." in parts.netloc else ""
            return [
                f"https://{h}{eu}.greenhouse.io/{board}/jobs/{job}"
                for h in ("job-boards", "boards")
            ]
    # Workday's form is /apply/applyManually (or /apply/autofillWithResume)
    # under the posting; Ashby's is /application, Lever's /apply.
    path = re.sub(r"/(application|apply)(/[A-Za-z]+)?/?$", "", parts.path)
    base = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    # The board keeps the url the listing gave; Workable's carry a trailing
    # slash (2,005 of 2,462 rows) and the form page's /apply/ hid it.
    return [base, base + "/"]


def budget_host(url: str) -> str:
    """The host a form read actually speaks to, which is the row the host
    budget must key on. Greenhouse's form lives on boards-api.greenhouse.io
    while the posting lives on job-boards.greenhouse.io; keyed by the posting
    host, the form reads would sit under their own row with no shared
    refusal history, so a listing pull paced out to the cap would leave the
    form reads hammering the same operator at full speed. The listing pulls
    already key on boards-api.greenhouse.io, so this makes the two one row."""
    host = host_of(url)
    if host.endswith("greenhouse.io"):
        return "boards-api.greenhouse.io"
    return host


def reader_for(url: str):
    host = host_of(url)
    return next((fn for suffix, fn in _READERS.items() if host.endswith(suffix)), None)


def supported(url: str) -> bool:
    return reader_for(url) is not None


def fetch(url: str) -> list[Question] | None:
    """The form's questions, or None when this host's form cannot be read.
    Raises on a failed read so the caller can tell "no form" from "no luck"."""
    reader = reader_for(url)
    return reader(url) if reader else None
