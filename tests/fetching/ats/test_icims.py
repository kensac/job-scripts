"""core.fetching.ats: the iCIMS resolver, over a posting frame copied from
careers-gdms.icims.com/jobs/75243/job?in_iframe=1 on 2026-10-05 and trimmed
to the JSON-LD block and the page around it."""

from __future__ import annotations

import datetime

from core.fetching import ats

# Verbatim JSON-LD from the frame, description cut short. The address carries
# iCIMS's UNAVAILABLE for an empty part, as Peraton's remote postings do for
# the city.
FRAME = """<!doctype html><html lang="en-US" ><head>
<title>Find a Job - General Dynamics Mission Systems</title>
<script type="application/ld+json">{"hiringOrganization":{"@type":"Organization","name":"General Dynamics Mission Systems, Inc","sameAs":"https://gdmissionsystems.com/"},"validThrough":"2027-10-04T04:00:00.000Z","jobLocation":[{"address":{"addressCountry":"US","streetAddress":"430 National Business Parkway","@type":"PostalAddress","postalCode":"20701","addressLocality":"Annapolis Junction","addressRegion":"MD","postOfficeBoxNumber":"UNAVAILABLE"},"@type":"Place"},{"address":{"addressCountry":"US","@type":"PostalAddress","addressLocality":"UNAVAILABLE","addressRegion":"VA"},"@type":"Place"}],"employmentType":"OTHER","@type":"JobPosting","description":"<h2>Basic Qualifications <\\/h2>\\n<p>Bachelor's degree in a related specialized area.<\\/p>\\n<p><strong>CLEARANCE REQUIREMENTS:<\\/strong>Department of Defense Top Secret/SCI security clearance is required.<\\/p>","directApply":true,"datePosted":"2026-10-04T04:00:00.000Z","title":"Sr. Advanced Field Support Specialist (TS/SCI Clearance Required)","occupationalCategory":"Technicians/Technical Support","@context":"http://schema.org","url":"https://careers-gdms.icims.com/jobs/75243/sr.-advanced-field-support-specialist-%28ts-sci-clearance-required%29/job"}</script>
</head><body><div class="iCIMS_JobContent">Please Enable Cookies to Continue</div></body></html>"""


class _Resp:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


def _serve(monkeypatch, resp: _Resp) -> list[str]:
    asked: list[str] = []
    monkeypatch.setattr(ats._session, "get", lambda url, **kw: asked.append(url) or resp)
    return asked


def test_icims_reads_the_posting_from_the_frame_the_public_page_embeds(monkeypatch):
    asked = _serve(monkeypatch, _Resp(FRAME))
    res = ats.resolve(
        "https://careers-gdms.icims.com/jobs/75243/sr.-advanced-field-support-specialist/job?mobile=true"
    )
    # The public page holds the posting in an iframe no fetch tier reads; the
    # frame is the same path with in_iframe=1.
    assert asked == ["https://careers-gdms.icims.com/jobs/75243/job?in_iframe=1"]
    assert res.ok and res.source == "icims"
    assert res.text == (
        "Sr. Advanced Field Support Specialist (TS/SCI Clearance Required)\n\n"
        "Annapolis Junction, MD, US; VA, US\n\n"
        "Basic Qualifications \n\n"
        "Bachelor's degree in a related specialized area.\n\n"
        "CLEARANCE REQUIREMENTS:\n"
        "Department of Defense Top Secret/SCI security clearance is required."
    )
    assert res.posted == datetime.date(2026, 10, 4)


def test_an_icims_posting_that_is_not_public_answers_410_and_is_gone(monkeypatch):
    # Measured: 40 of 40 ids missing from the GDMS and Joby sitemaps answered
    # 410 on 2026-10-05, with the portal's chrome as the body.
    _serve(monkeypatch, _Resp("<html>Careers - General Dynamics Mission Systems</html>", 410))
    assert ats.resolve("https://careers-gdms.icims.com/jobs/75326/job").status is ats.Status.GONE


def test_an_icims_frame_without_a_job_posting_is_an_error_not_text(monkeypatch):
    """The page chrome is long enough to pass for a posting; returning it as
    text would hand the checks the site navigation."""
    chrome = "<html><body>" + "Land Products, Programs & Services " * 200 + "</body></html>"
    _serve(monkeypatch, _Resp(chrome))
    res = ats.resolve("https://careers-gdms.icims.com/jobs/75243/job")
    assert res.status is ats.Status.ERROR and res.text is None


def test_icims_leaves_urls_that_are_not_postings_alone(monkeypatch):
    asked = _serve(monkeypatch, _Resp(FRAME))
    assert ats.resolve("https://careers-gdms.icims.com/jobs/search?pr=2").status is (
        ats.Status.UNSUPPORTED
    )
    assert asked == []
