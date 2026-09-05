"""core.ats: the Oracle Recruiting and Workable resolvers, over bodies copied
from the live APIs on 2026-09-05 and trimmed to the keys read."""

from __future__ import annotations

import datetime

from core import ats


class _Resp:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


def test_oracle_canonical_drops_locale_and_facets():
    url = (
        "https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/"
        "CX_1/job/40082?lastSelectedFacet=TITLES&location=Canada"
    )
    assert ats.canonicalize(url) == (
        "https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/"
        "CX_1/job/40082"
    )
    assert ats.canonicalize("https://hdhe.fa.em3.oraclecloud.com/hcmUI/CandidateExperience") is None


def test_oracle_fetches_the_requisition_detail_and_reads_its_date(monkeypatch):
    asked = []

    def get(url):
        asked.append(url)
        return _Resp(
            {
                "items": [
                    {
                        "Title": "Firmware Development Engineer",
                        "PrimaryLocation": "India",
                        "ExternalPostedStartDate": "2026-09-05T07:31:46+00:00",
                        "ExternalDescriptionStr": "<p>Nokia builds networks.</p>",
                        "ExternalResponsibilitiesStr": "<ul><li>Write firmware.</li></ul>",
                        "ExternalQualificationsStr": "<p>C and a degree.</p>",
                    }
                ]
            }
        )

    res = ats.Oracle()
    monkeypatch.setattr(res, "get", get)
    out = res.fetch(
        "https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/"
        "CX_1/job/40082"
    )
    assert out.ok and out.posted == datetime.date(2026, 9, 5)
    assert out.text == (
        "Firmware Development Engineer\n\nIndia\n\nNokia builds networks.\n\n"
        "Write firmware.\n\nC and a degree."
    )
    assert asked == [
        "https://fa-evmr-saasfaprod1.fa.ocs.oraclecloud.com/hcmRestApi/resources/latest/"
        "recruitingCEJobRequisitionDetails?onlyData=true&expand=all"
        "&finder=ById;siteNumber=CX_1,Id=%2240082%22"
    ]


def test_oracle_reads_an_empty_detail_as_gone(monkeypatch):
    res = ats.Oracle()
    monkeypatch.setattr(res, "get", lambda url: _Resp({"items": []}))
    out = res.fetch("https://x.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/9923")
    assert out.status is ats.Status.GONE and out.source == "oracle"


def test_workable_canonical_is_the_apply_page_without_its_tail():
    assert (
        ats.canonicalize("https://apply.workable.com/zego/j/637F3B9521/apply")
        == "https://apply.workable.com/zego/j/637F3B9521/"
    )
    # A feed once wrote the host with no account; the path still names one.
    assert (
        ats.canonicalize("https:///.workable.com/trexquant/j/A634E0E3F4")
        == "https://apply.workable.com/trexquant/j/A634E0E3F4/"
    )


def test_workable_fetches_the_v2_job_and_reads_its_date(monkeypatch):
    asked = []

    def get(url):
        asked.append(url)
        return _Resp(
            {
                "title": "Tech Talent Sourcer",
                "published": "2026-09-01T00:00:00.000Z",
                "location": {"country": "United Kingdom", "city": "London", "region": "England"},
                "description": "<p>Find people.</p>",
                "requirements": "<ul><li>Sourcing.</li></ul>",
                "benefits": "",
            }
        )

    res = ats.Workable()
    monkeypatch.setattr(res, "get", get)
    out = res.fetch("https://apply.workable.com/zego/j/BCEAC9B2D1/")
    assert out.ok and out.posted == datetime.date(2026, 9, 1)
    assert (
        out.text
        == "Tech Talent Sourcer\n\nLondon, England, United Kingdom\n\nFind people.\n\nSourcing."
    )
    assert asked == ["https://apply.workable.com/api/v2/accounts/zego/jobs/BCEAC9B2D1"]


def test_workable_and_oracle_answer_resolve(monkeypatch):
    monkeypatch.setattr(
        ats.Oracle, "fetch", lambda self, url: ats.AtsResult(ats.Status.OK, "t", "oracle")
    )
    monkeypatch.setattr(
        ats.Workable, "fetch", lambda self, url: ats.AtsResult(ats.Status.OK, "t", "workable")
    )
    assert (
        ats.resolve(
            "https://x.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1"
        ).source
        == "oracle"
    )
    assert ats.resolve("https://apply.workable.com/zego/j/BCEAC9B2D1/").source == "workable"
