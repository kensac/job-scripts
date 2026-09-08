"""Turn an ATS selector table into the extension's configs.

Usage: python tools/convert_ats_config.py <remoteConfig.json> [ATS ...] [--exclude ATS ...]

Writes one file per ATS under extension/ats/ (the url match patterns, the
fields with their selectors and fill methods, the continue, submit and
success paths) and one content_scripts entry per ATS in
extension/manifest.json, so a page loads only its own table. Names after
--exclude are skipped (the ATSs with hand-written readers).

The table is field-first: each selector says which fact it fills. Field
names are mapped onto the profile's facts. Nothing is dropped: a name with
no fact of its own is kept with fact None and resolved by its label on the
page (the model drafts a cover letter when the person's switch is on); a
name that is a flow step with no value (begin, save, expand a section,
wait for the location box) is kept with fact "step" and run in table
order. A whole ATS is dropped only when it has no url globs at all
(Homerun, PhenomPeople, Teamtailor live on employers' own domains and need
a page detector, which is a separate decision); an all-host glob with a
distinctive path (BrassRing's /TGnewUI/) becomes an all-host match pattern.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The table's field name -> the profile fact the engine fills it from. A
# value of None keeps the field but leaves the fact to the resolver by its
# label (custom questions); a missing key drops the field.
FACTS: dict[str, str | None] = {
    "first_name": "first_name",
    "first_name_2": "first_name",
    "last_name": "last_name",
    "last_name_2": "last_name",
    "full_name": "full_name",
    "preferred_name": "preferred_name",
    "preferred_first_name": "preferred_name",
    "first_name_preferred": "preferred_name",
    "email": "email",
    "email_2": "email",
    "email_confirm": "email",
    "phone": "phone",
    "phone_2": "phone",
    "phone_stripped": "phone_digits",
    "mobile_phone": "phone",
    "linkedin": "linkedin",
    "github": "github",
    "portfolio": "website",
    "website": "website",
    "websites": "website",
    "additional_url": "website",
    "twitter": "twitter",
    "country": "country",
    "country_2": "country",
    "country_location": "country",
    "state": "state",
    "state_2": "state",
    "city": "city",
    "city_2": "city",
    "postal_code": "postal_code",
    "address": "address",
    "city_state": "location",
    "city_state_full": "location",
    "location": "location",
    "work_auth": "work_authorized",
    "sponsorship": "needs_sponsorship",
    "sponsorship_2": "needs_sponsorship",
    "gender": "gender",
    "gender_2": "gender",
    "gender_checkable": "gender",
    "ethnicity": "ethnicity",
    "ethnicity_2": "ethnicity",
    "ethnicity_3": "ethnicity",
    "ethnicity_checkable": "ethnicity",
    "hispanic": "hispanic",
    "hispanic_2": "hispanic",
    "hispanic_3": "hispanic",
    "veteran_v2": "veteran",
    "veteran_v2_2": "veteran",
    "veteran_v2_3": "veteran",
    "disability_v2": "disability",
    "disability_v2_2": "disability",
    "disability_v2_3": "disability",
    "disability_name": "full_name",
    "disability_date": "today",
    "current_date": "today",
    "source": "referral_source",
    "current_company_name": "current_company",
    "highestDegree": "degree",
    "over18": "yes",
    "over21": "yes",
    "hasExperience": "yes",
    "current_employee": "no",
    "in_country": "yes",
    "armed_forces": "veteran",
    "visible_minority": "decline",
    "salary_requirements": "desired_salary",
    "phone_extension": None,
    "resume": "resume",
    # Repeated groups, filled one entry per profile row.
    "education": "education",
    "experience": "experience",
    # Second and third copies of a field, other names for one fact.
    "work_auth_2": "work_authorized",
    "work_auth_3": "work_authorized",
    "work_auth_us": "work_authorized",
    "sponsorship_3": "needs_sponsorship",
    "gender_3": "gender",
    "multiple_ethnicities": "ethnicity",
    "linkedin_2": "linkedin",
    "linkedin_3": "linkedin",
    "additional_url_2": "website",
    "additional_url_3": "website",
    "home_phone": "phone",
    "phone_stripped_2": "phone_digits",
    "preferred_first_name_2": "preferred_name",
    "preferred_last_name": "last_name",
    "legal_name": "full_name",
    "source_2": "referral_source",
    "source_other": "referral_source",
    "source_description": "referral_source",
    "current_job_title": "current_title",
    "title": "current_title",
    "currently_working": "yes",
    # Profile fields added for the table (2026-09-08).
    "middle_name": "middle_name",
    "address_2": "address_2",
    "address_3": "address_3",
    "behance": "behance",
    "dribbble": "dribbble",
    "pronouns": "pronouns",
    "phone_type": "phone_type",
    "phone_country": "phone_country",
    "phone_country_2": "phone_country",
    "phone_country_3": "phone_country",
    "phone_country_4": "phone_country",
    "birthday_M": "birthday",
    "birthday_MM": "birthday",
    "birthday_D": "birthday",
    "birthday_DD": "birthday",
    "birthday_YYYY": "birthday",
    "birthday_slashes_MDYYYY": "birthday",
    # Dates of today in the form's format; the engine formats by the name.
    "current_date_MM": "today",
    "current_date_YYYY": "today",
    "current_date_D": "today",
    "current_date_DD": "today",
    "current_date_slashes_MMDDYYYY": "today",
    "current_date_slashes_MDDYY": "today",
    # Self-identification the profile does not hold: declined.
    "transgender": "decline",
    "lgbt_v2": "decline",
    "lgbt_v2_2": "decline",
    "lgbt_v2_3": "decline",
    # Resolved by label, drafted by the model when the switch is on.
    "coverLetter": None,
    "cover_letter": None,
    "education_summary": None,
    "experience_summary": None,
    "language": None,
    "languages": None,
    "languages_text": None,
    "language_preferred": None,
    "skill": None,
    "skills": None,
    "referred_by": None,
    "preferred_contact_method": None,
    "has_drivers_license": None,
    "username": None,
}

# Flow steps: a click or a wait with no value, run in table order.
STEPS = {
    "begin",
    "resume_begin",
    "begin_education",
    "begin_experience",
    "save",
    "save_education",
    "save_experience",
    "save_contact_section",
    "save_personal_section",
    "save_preferences_section",
    "save_websites",
    "expand_contact_section",
    "expand_personal_info_section",
    "expand_preferences_section",
    "wait_for_location_loaded",
    "confirm_resume",
    "mark_relevant",
    "mark_resume",
    "done",
    "delay",
    "delete_extra",
    "clear_profile",
    "cover_letter_method_fallback",
}

# Behaviour the engine reads under the table's own names.
TOP_KEYS = (
    "applyButtonPaths",
    "urlsExcluded",
    "pathsExcluded",
    "embeddedPaths",
    "containerRequired",
    "fillInputInterval",
    "fillInputGroupInterval",
    "orderByDomPosition",
    "deferSubmissionModalPaths",
    "validationScopePaths",
    "applyOptionPaths",
)

GROUP_KEYS = (
    "containerPath",
    "addButtonPath",
    "confirmAddedPath",
    "removeExtraButtonPath",
    "limit",
    "reverse",
    "time",
    "refindPerEntry",
    "allowReuse",
)

SKIP_METHODS = {"uploadCoverLetter", "tptEnableResume", "dijit"}


def glob_to_match(glob: str) -> str | None:
    """A url glob from the table as a Chrome match pattern, or None when it cannot
    be one (a query string, or every host)."""
    if "?" in glob:
        return None
    g = re.sub(r"\*{2,}", "*", glob.replace("*://", "https://"))
    if g.startswith("https://*/"):
        # Every host, so only with a path that names the ATS (BrassRing's
        # /TGnewUI/); a bare "https://*/*" would run on the whole web.
        path = g[len("https://*") :]
        return f"https://*{path}" if len(path.strip("/*")) >= 6 else None
    m = re.match(r"^https://([^/]+)(/.*)?$", g)
    if not m:
        return None
    host, path = m.group(1), m.group(2) or "/*"
    # Chrome allows a wildcard only as a leading "*." on the host.
    if not re.match(r"^(\*\.)?[a-z0-9.-]+$", host):
        return None
    if not path.endswith("*"):
        path = path.rstrip("/") + "/*"
    return f"https://{host}{path}"


# Fields whose constant answers in the table belong to whoever wrote it: a
# "how did you hear about us" filled with a fixed source. The profile
# answers those here, so the constants are dropped at any depth.
PROFILE_ANSWERED = {"source"}


def _without_constant_answers(node):
    if isinstance(node, dict):
        return {
            k: _without_constant_answers(val)
            for k, val in node.items()
            if k not in ("value", "values")
        }
    if isinstance(node, list):
        return [_without_constant_answers(x) for x in node]
    return node


def convert_variants(name: str, variants: list) -> list[dict]:
    """A field's variants, each a selector list with its method and actions,
    or a group: a container per entry, an add button, and nested fields
    converted the same way. A nested field keeps its table name; the engine
    reads the entry's value and date format off it."""
    out_variants = []
    for raw in variants:
        if isinstance(raw, str):
            out_variants.append({"paths": [raw]})
            continue
        if not isinstance(raw, dict):
            continue
        if (raw.get("method") or "default") in SKIP_METHODS:
            continue
        if "inputSelectors" in raw:
            group: dict = {"group": True}
            for k in GROUP_KEYS:
                if k in raw:
                    group[k] = raw[k]
            group["fields"] = [
                {"name": sub_name, "variants": convert_variants(sub_name, sub_variants)}
                for sub_name, sub_variants in raw["inputSelectors"]
            ]
            group["fields"] = [f for f in group["fields"] if f["variants"]]
            if group["fields"]:
                out_variants.append(group)
            continue
        paths = raw.get("path")
        if paths is None:
            continue
        entry: dict = {"paths": paths if isinstance(paths, list) else [paths]}
        v = _without_constant_answers(raw) if name in PROFILE_ANSWERED else raw
        for k in (
            "method",
            "actions",
            "values",
            "value",
            "valuePath",
            "valueKey",
            "everyValue",
            "hidden",
            "visible",
            "allowReuse",
            "valueRequired",
            "array",
            "optionsSource",
            "valuePathMap",
            "manual",
            "valueElementTime",
            "time",
        ):
            if k in v:
                entry[k] = v[k]
        out_variants.append(entry)
    return out_variants


def convert_field(name: str, variants: list) -> dict | None:
    fact = "step" if name in STEPS else FACTS.get(name)
    out_variants = convert_variants(name, variants)
    if not out_variants:
        return None
    return {"name": name, "fact": fact, "variants": out_variants}


def convert(ats: dict, name: str) -> dict | None:
    matches = [m for m in (glob_to_match(g) for g in ats.get("urls") or []) if m]
    if not matches:
        return None
    fields = [f for f in (convert_field(n, vs) for n, vs in ats.get("inputSelectors") or []) if f]
    out = {
        "name": name,
        "matches": matches,
        "fields": fields,
        "continue": ats.get("continueButtonPaths") or [],
        "submit": ats.get("submitButtonPaths") or [],
        "success": ats.get("submittedSuccessPaths") or [],
        "proxySubmit": bool(ats.get("proxySubmitButtons")),
        "container": ats.get("containerPath") or [],
        "questions": ats.get("trackedInputSelectors") or [],
        "defaultMethod": ats.get("defaultMethod") or "default",
        "defaultEventOptions": ats.get("defaultEventOptions") or {},
    }
    for k in TOP_KEYS:
        if k in ats:
            out[k] = ats[k]
    return out


# A variant's `values` may name one of the table's shared maps instead of
# carrying its own; the engine resolves the name through these.
VALUE_MAPS = ("countryAbbreviationsToNames", "stateAbbreviationsToNames")


def main(argv: list[str]) -> int:
    source = Path(argv[1])
    rest = argv[2:]
    excluded = set(rest[rest.index("--exclude") + 1 :]) if "--exclude" in rest else set()
    wanted = set(rest[: rest.index("--exclude")] if "--exclude" in rest else rest)
    whole = json.loads(source.read_text())
    table = whole["ATS"]
    value_maps = {k: whole[k] for k in VALUE_MAPS if k in whole}
    configs = []
    for name, ats in table.items():
        if not isinstance(ats, dict) or (wanted and name not in wanted) or name in excluded:
            continue
        converted = convert(ats, name)
        if converted:
            converted["valueMaps"] = value_maps
            configs.append(converted)
    configs.sort(key=lambda c: c["name"])
    out_dir = ROOT / "extension" / "ats"
    out_dir.mkdir(exist_ok=True)
    for stale in out_dir.glob("*.js"):
        stale.unlink()
    # One file per ATS, loaded only on that ATS's hosts: a page carries its
    # own table, not everyone's.
    for c in configs:
        body = json.dumps(c, ensure_ascii=False, separators=(",", ":"))
        (out_dir / f"{c['name']}.js").write_text(
            "// Generated by tools/convert_ats_config.py; do not edit by hand.\n"
            f"window.__jtATS = [{body}];\n"
        )
    manifest_path = ROOT / "extension" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    scripts = [s for s in manifest["content_scripts"] if "engine.js" not in s["js"]]
    for c in configs:
        scripts.append(
            {
                "matches": c["matches"],
                "js": [f"ats/{c['name']}.js", "engine.js", "content.js"],
                "css": ["panel.css"],
                "all_frames": True,
                "run_at": "document_idle",
            }
        )
    manifest["content_scripts"] = scripts
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    matches = sorted({m for c in configs for m in c["matches"]})
    print(
        f"{len(configs)} ATS, {sum(len(c['fields']) for c in configs)} fields, {len(matches)} match patterns"
    )
    for c in configs:
        print(
            f"  {c['name']:18s} fields={len(c['fields']):2d} steps={'yes' if c['continue'] else 'no'} matches={c['matches']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
