"""A classified location string and the rules for writing one.

The classification sweep and the admin correction route are two callers of
the same write. Keeping its input shape and normalisation here means neither
caller owns location semantics on behalf of the other.
"""

from __future__ import annotations

from pydantic import BaseModel

from api import db


class Place(BaseModel):
    country: str = ""
    region: str = ""
    city: str = ""


class LocationExtract(BaseModel):
    """One place through country/region/city, or several through places;
    the first is what the display columns carry."""

    country: str = ""
    region: str = ""
    city: str = ""
    remote: bool = False
    places: list[Place] = []

    def all_places(self) -> list[Place]:
        if self.places:
            return self.places
        if self.country:
            return [Place(country=self.country, region=self.region, city=self.city)]
        return []


# A bare two-letter code is a US state or a Canadian province on these boards,
# and the model got a third of them wrong on the first pass over production:
# CA became Canada (1,604 postings say CA and mean California), IN India, DE
# Germany, AR Argentina, ME Britain, ON the US, and a dozen states grew an
# invented city. Sixty-three codes is a table, not a judgment, so it is one.
_US_STATES = [
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "DC",
]
_CA_PROVINCES = ["AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"]
_CODES = {code: "US" for code in _US_STATES} | {code: "CA" for code in _CA_PROVINCES}


def _clean(value: str, length: int) -> str | None:
    v = value.strip()
    return v.upper() if v and len(v) == length and v.isalpha() else None


def _normalised(place: Place) -> dict[str, str | None] | None:
    """A place with a valid country, or nothing: a region or city without a
    country is a half-answer that could match the wrong country's CA."""
    country = _clean(place.country, 2)
    if not country:
        return None
    return {
        "country": country,
        "region": _clean(place.region, 2),
        "city": place.city.strip() or None,
    }


def store(
    text: str, parsed: LocationExtract, model: str | None, *, preserve_manual: bool = False
) -> bool:
    code = text.strip().upper()
    if code in _CODES:
        parsed = LocationExtract(country=_CODES[code], region=code)
    places = [p for p in (_normalised(pl) for pl in parsed.all_places()) if p is not None]
    first = places[0] if places else {"country": None, "region": None, "city": None}
    row = db.query_one(
        """
        INSERT INTO locations (text, country, region, city, remote, places, model)
        VALUES (%(text)s, %(country)s, %(region)s, %(city)s, %(remote)s, %(places)s, %(model)s)
        ON CONFLICT (text) DO UPDATE SET
            country = EXCLUDED.country, region = EXCLUDED.region, city = EXCLUDED.city,
            remote = EXCLUDED.remote, places = EXCLUDED.places, model = EXCLUDED.model,
            classified_at = now()
        WHERE NOT %(preserve_manual)s OR locations.model IS DISTINCT FROM 'admin'
        RETURNING text
        """,
        {
            "text": text,
            "preserve_manual": preserve_manual,
            "country": first["country"],
            "region": first["region"],
            "city": first["city"],
            "remote": bool(parsed.remote),
            "places": db.jsonb(places),
            "model": model,
        },
    )

    return row is not None
