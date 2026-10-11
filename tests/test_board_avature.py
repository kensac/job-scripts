"""Avature board behavior over producer-shaped feed excerpts."""

from __future__ import annotations

import pytest

from core.fetching import board_avature, boards

# Search-result markup copied from each tenant's live page on 2026-10-05, the
# share popups trimmed and the job's slug, id and title made parameters. Each
# tenant designs its own template, so the fetcher is tested against every one
# it was measured on.

AV_BLOOMBERG = (
    '<article class="article article--result" id="article--1"><div class="article__header">'
    '<div class="article__header__text"><h3 class="article__header__text__title title title--04">'
    '<a class="link" href="https://bloomberg.avature.net/careers/JobDetail/{slug}/{id}"> {title} </a>'
    '</h3><div class="article__header__text__subtitle"><span class="list-item-location">'
    'Hong Kong, Hong Kong</span></div></div></div><div class="article__footer">'
    '<a class="button button--primary" href="https://bloomberg.avature.net/careers/JobDetail/'
    '{slug}/{id}" tabindex="0"> Apply </a><a class="button button--secondary" '
    'href="https://bloomberg.avature.net/careers/SaveJob?jobId={id}" tabindex="0"> Save </a>'
    "</div></article>"
)
AV_TWO_SIGMA = (
    '<article class="article article--result" id="article--1"><div class="article__header">'
    '<div class="article__header__text"><h3 class="article__header__text__title title title--065">'
    '<a class="link" href="https://careers.twosigma.com/careers/JobDetail/{slug}/{id}"> {title} </a>'
    '</h3><div class="article__header__content"><div class="article__header__content__text">'
    '<span class="paragraph_inner-span">United States - NY New York</span>'
    '<div class="article__header__content__sub-text"><span class="paragraph_inner-span">'
    'Engineering</span><span class="paragraph_inner-span">Experienced</span></div></div>'
    '<div class="article__footer"><a class="button button--primary button--list-results--mobile" '
    'href="https://careers.twosigma.com/careers/JobDetail/{slug}/{id}" tabindex="0"> View role </a>'
    "</div></div></div></div></article>"
)
AV_SIEMENS = (
    '<article class="article article--result 1" id="article--1"><div class="article__header">'
    '<div class="article__header__text"><h3 class="article__header__text__title title title--h3 '
    'title--white" data-au="ag-h3-6"><a class="link" data-au="ag-a-10" '
    'href="https://jobs.siemens.com/en_US/externaljobs/JobDetail/{id}"> {title} </a></h3>'
    '<div class="article__header__text__subtitle"><span class="list-item-location">'
    '<span class="list-item-jobCity">Bangalore</span><span aria-hidden="true" class="separator">'
    ', </span><span class="list-item-jobState">Karnataka</span><span aria-hidden="true" '
    'class="separator">, </span><span class="list-item-jobCountry">India</span></span>'
    '<span aria-hidden="true" class="separator"> • </span><span class="list-item-jobId">'
    'Job ID: {id}</span></div></div></div><div class="article__footer">'
    '<a aria-label="Learn more" class="button button--primary" data-au="ag-a-12" '
    'href="https://jobs.siemens.com/en_US/externaljobs/JobDetail/{id}" tabindex="0"> Learn more </a>'
    "</div></article>"
)
AV_KOCH = (
    '<article class="article article--result"><div class="article__header">'
    '<div class="article__header__text"><h5 class="article__header__text__subtitle"> Molex </h5>'
    '<div class="article__header__actions"></div><h3 class="article__header__text__title '
    'article__header__text__title--7"><a href="https://koch.avature.net/en_US/careers/JobDetail/'
    '{slug}/{id}"> {title} </a></h3></div></div><div class="article__content">'
    '<div class="article__content__field m--t--s"><div class="article__content__field__label"> '
    'Location: </div><div class="article__content__field__value"> Hanoi, Hanoi </div></div>'
    '<div class="article__content__field m--t--s"><div class="article__content__field__label"> '
    'Job Number: </div><div class="article__content__field__value"> {id} </div></div></div>'
    "</article>"
)
AV_HARMAN = (
    '<article class="article article--result" id="article--1"><div class="article__header">'
    '<div class="article__header__text"><h3 class="article__header__text__title title title--04">'
    '<a class="link" href="https://jobsearch.harman.com/en_US/careers/JobDetail/{slug}/{id}"> '
    '{title} </a></h3><div class="article__header__text__subtitle"><span class="list-item-location">'
    '<strong> Location:</strong> Aarhus - Central Jutland, Denmark</span><span aria-hidden="true" '
    'class="separator"> • </span><span class="list-item-ref"><strong> Ref #</strong> '
    "R-55686-2026</span></div></div></div></article>"
)
AV_CDCN = (
    '<article class="article article--result"><div class="article__header">'
    '<div class="article__header__text"><h3 class="article__header__text__title '
    'article__header__text__title--4"><a href="https://cdcn.avature.net/careers/JobDetail/{slug}/'
    '{id}"> {title} </a></h3><div class="article__header__text__subtitle"><span> MT - Missoula '
    '</span> · <span> Posted 14-Jul-2026 </span></div></div><div class="article__header__actions">'
    '<a class="button button--secondary" href="https://cdcn.avature.net/careers/ApplicationMethods?'
    'jobId={id}" tabindex="0"> Apply </a></div></div></article>'
)
AV_POMERLEAU = (
    '<article class="article article--result"><div class="article__header">'
    '<div class="article__header__text"><h3 class="article__header__text__title '
    'article__header__text__title--4"><a href="https://pomerleau.avature.net/en_US/careers/'
    'JobDetail/{slug}/{id}"> {title} </a></h3><div class="article__header__text__subtitle">'
    '<span class="list-item-ref">Ref #7144</span><span aria-hidden="true" class="separator"> • '
    '</span><span class="list-item-posted">Posted 02-Oct-2026</span></div></div></div></article>'
)


def _av_article(template: str, n: int) -> str:
    return template.format(slug=f"Role-{n}", id=n, title=f"Role {n}")


def _av_page(articles: str, next_link: str = "", legend: str = "") -> str:
    """A search page around its results, laid out as the live pages are."""
    return (
        f'<html><body><div class="list-controls list-controls--top clearfix">{legend}'
        f'<nav aria-label="Pagination Navigation">{next_link}</nav></div>'
        f'<div class="results results--listed">{articles}</div></body></html>'
    )


class _AvResp:
    def __init__(self, url: str, text: str, status: int = 200):
        self.url, self.text, self.status_code = url, text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise boards.requests.HTTPError(str(self.status_code))


def _av_serve(monkeypatch, pages):
    """GET answered by `pages(url)`, every url asked recorded."""
    asked: list[str] = []

    def get(url, **kw):
        asked.append(url)
        return pages(url)

    monkeypatch.setattr(boards._session, "get", get)
    return asked


def test_avature_follows_the_next_link_the_page_gives(monkeypatch):
    """Bloomberg on 2026-10-05: 12 rows a page whatever jobRecordsPerPage
    asks, "1-12 of 348 results", and a next link to jobOffset=12. Here the
    tenant pages by 2, so a loop that steps by the size it asked for skips
    rows, and one that stops on a short page stops after the first."""
    search = "https://bloomberg.avature.net/careers/SearchJobs"

    def pages(url):
        offset = int(url.split("jobOffset=")[1]) if "jobOffset=" in url else 0
        rows = [n for n in (1, 2, 3) if offset < n <= offset + 2]
        following = (
            '<a class="list-controls__pagination__item link paginationNextLink" '
            f'href="{search}/?jobRecordsPerPage=2&amp;jobOffset={offset + 2}"> Next &gt;&gt; </a>'
            if offset + 2 < 3
            else ""
        )
        legend = (
            '<div class="list-controls__text__legend" aria-label="3 results"> '
            f"{offset + 1}-{offset + len(rows)} of 3 results </div>"
        )
        body = "".join(_av_article(AV_BLOOMBERG, n) for n in rows)
        return _AvResp(url, _av_page(body, following, legend))

    asked = _av_serve(monkeypatch, pages)
    out = boards.fetch_listings(search, "Bloomberg")
    assert [p.title for p in out] == ["Role 1", "Role 2", "Role 3"]
    assert out[0].url == "https://bloomberg.avature.net/careers/JobDetail/Role-1/1"
    assert out[0].locations == ["Hong Kong, Hong Kong"]
    assert all(p.company == "Bloomberg" for p in out)
    assert asked == [search, f"{search}/?jobRecordsPerPage=2&jobOffset=2"]


def test_avature_pages_by_the_offset_the_template_names(monkeypatch):
    """Siemens pages by folderOffset, puts the next link's class on its <li>,
    and answers its first page to every jobOffset (2026-10-05), so a fetcher
    that builds jobOffset itself reads page one forever."""
    search = "https://siemens.avature.net/en_US/externaljobs/SearchJobs"
    final = "https://jobs.siemens.com/en_US/externaljobs/SearchJobs"

    def pages(url):
        second = "folderOffset=2" in url
        rows = (3,) if second else (1, 2)
        following = (
            ""
            if second
            else '<li class="list-controls__pagination__item paginationNextLink"> '
            f'<a href="{final}/?folderRecordsPerPage=2&amp;folderOffset=2"> Next </a> </li>'
        )
        legend = (
            '<div aria-label="999+ results" class="list-controls__text__legend"> '
            "1 - 2 of 999+ results </div>"
        )
        body = "".join(_av_article(AV_SIEMENS, n) for n in rows)
        return _AvResp(final if url == search else url, _av_page(body, following, legend))

    _av_serve(monkeypatch, pages)
    out = boards.fetch_listings(search, "Siemens")
    assert [p.title for p in out] == ["Role 1", "Role 2", "Role 3"]
    assert out[2].url == "https://jobs.siemens.com/en_US/externaljobs/JobDetail/3"
    assert out[0].locations == ["Bangalore, Karnataka, India"]


def test_an_avature_walk_short_of_its_stated_count_is_partial(monkeypatch):
    """The legend says four and the walk ends at three: one posting was not
    seen, which is not evidence it closed."""
    legend = '<div class="list-controls__text__legend"> 1-3 of 4 results </div>'
    body = "".join(_av_article(AV_BLOOMBERG, n) for n in (1, 2, 3))
    _av_serve(monkeypatch, lambda url: _AvResp(url, _av_page(body, "", legend)))
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings("https://bloomberg.avature.net/careers/SearchJobs", "Bloomberg")
    assert len(raised.value.postings) == 3


def test_an_avature_next_link_back_to_a_page_already_read_is_partial(monkeypatch):
    """Two Sigma states no count. A next link that leads to rows already seen
    means the list moved or wrapped, so the walk proves nothing."""
    search = "https://twosigma.avature.net/careers/OpenRoles"
    following = f'<a class="paginationNextLink" href="{search}/?jobOffset=2"> Next </a>'
    body = "".join(_av_article(AV_TWO_SIGMA, n) for n in (1, 2))
    _av_serve(monkeypatch, lambda url: _AvResp(url, _av_page(body, following)))
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings(search, "Two Sigma")
    assert [p.title for p in raised.value.postings] == ["Role 1", "Role 2"]


def test_an_avature_walk_into_the_window_is_partial(monkeypatch):
    """Koch states no count and answers 406 from offset 2,000; Siemens answers
    an empty page there. Either way the rows past it are unseen."""
    monkeypatch.setattr(board_avature, "_AVATURE_WINDOW", 2)
    search = "https://koch.avature.net/en_US/careers/SearchJobs"

    def koch(url):
        if "jobOffset=2" in url:
            return _AvResp(url, "<html>406 Not Acceptable</html>", 406)
        following = f'<a class="paginationNextLink" href="{search}/?jobOffset=2"> Next </a>'
        body = "".join(_av_article(AV_KOCH, n) for n in (1, 2))
        return _AvResp(url, _av_page(body, following))

    _av_serve(monkeypatch, koch)
    with pytest.raises(boards.PartialPull) as raised:
        boards.fetch_listings(search, "Koch")
    assert [p.locations for p in raised.value.postings] == [["Hanoi, Hanoi"]] * 2

    # An empty page at the window ends the walk as the end of the list would.
    body = "".join(_av_article(AV_TWO_SIGMA, n) for n in (1, 2))
    _av_serve(monkeypatch, lambda url: _AvResp(url, _av_page(body)))
    with pytest.raises(boards.PartialPull):
        boards.fetch_listings("https://twosigma.avature.net/careers/OpenRoles", "Two Sigma")


def test_an_uncounted_avature_walk_under_the_window_is_complete(monkeypatch):
    body = "".join(_av_article(AV_TWO_SIGMA, n) for n in (1, 2))
    _av_serve(monkeypatch, lambda url: _AvResp(url, _av_page(body)))
    out = boards.fetch_listings("https://twosigma.avature.net/careers/OpenRoles", "Two Sigma")
    assert [p.locations for p in out] == [["United States - NY New York"]] * 2


@pytest.mark.parametrize(
    "template, expected",
    [
        (AV_BLOOMBERG, ["Hong Kong, Hong Kong"]),
        (AV_TWO_SIGMA, ["United States - NY New York"]),
        (AV_SIEMENS, ["Bangalore, Karnataka, India"]),
        (AV_KOCH, ["Hanoi, Hanoi"]),
        # The label is a <strong> inside the location span.
        (AV_HARMAN, ["Aarhus - Central Jutland, Denmark"]),
        (AV_CDCN, ["MT - Missoula"]),
        # No place on the list page: nothing, not the reference number.
        (AV_POMERLEAU, []),
    ],
)
def test_avature_reads_the_place_from_each_template(monkeypatch, template, expected):
    body = _av_article(template, 1)
    _av_serve(monkeypatch, lambda url: _AvResp(url, _av_page(body)))
    [posting] = boards.fetch_listings("https://x.avature.net/careers/SearchJobs", "X")
    assert posting.title == "Role 1"
    assert posting.locations == expected
