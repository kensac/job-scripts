"""Getting a posting page, and knowing what the page is.

`boards` selects listing formats and groups their provider fetchers;
`ats` selects posting detail resolvers grouped by provider; `scrape`
is the browser of last resort; `forms` and `urls` say which url a posting is
stored under and which page is its application form; `listings` and `posting`
are the shapes a pull returns.

Grouped because "how did we get this text" was seven modules at one level, and
the order they are tried in is the thing worth reading in one place.
"""
