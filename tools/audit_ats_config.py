"""Coverage audit: an ATS selector table against what the extension carries.

Usage: python tools/audit_ats_config.py <remoteConfig.json> [out.md]

For every ATS in the table: the fields the converter would drop (a name
with no fact and no step), the variants it drops (a skipped method), the
fill methods the engine has no branch for, the action, variant and
question-reader keys the engine ignores, the top-level behaviour keys it
ignores, and whether the ATS loads at all (a url glob a content script can
match). The lists of what the engine implements live here and must move
with extension/engine.js; the audit is only as honest as they are. Run it
after any change to the table, the converter or the engine, and before
calling an ATS covered.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from convert_ats_config import FACTS, SKIP_METHODS, STEPS, glob_to_match

HAND = {"AshbyHQ", "Greenhouse", "Lever"}

# What extension/engine.js implements.
ENGINE_METHODS = {
    "default",
    "defaultWithoutBlur",
    "react",
    "jQuery",
    "setValue",
    "setValueOnly",
    "vanillaWithBlur",
    "ui5",
    "tinyMCE",
    "click",
    "reactClick",
    "selectCheckboxOrRadio",
    "uploadResume",
    "clearValue",
    "blur",
    "scrollListboxToOption",
    "reactDatePickerMonth",
    "writeCoverLetter",
}
ENGINE_ACTION_KEYS = {
    "method",
    "path",
    "time",
    "allowFailure",
    "eventOptions",
    "event",
    "delay",
    "condition",
    "valueRequired",
    "removed",
    "removedTime",
    "skipOnEmptyValue",
    "value",
    "values",
    "valueKey",
    "valuePath",
    "valueElementTime",
    "hidden",
}
ENGINE_VARIANT_KEYS = {
    "path",
    "method",
    "actions",
    "values",
    "value",
    "valuePath",
    "valueKey",
    "valuePathMap",
    "everyValue",
    "hidden",
    "visible",
    "allowReuse",
    "valueRequired",
    "inputSelectors",
    "containerPath",
    "addButtonPath",
    "confirmAddedPath",
    "removeExtraButtonPath",
    "limit",
    "reverse",
    "time",
    "refindPerEntry",
    "array",
    "optionsSource",
    "manual",
    "valueElementTime",
    "name",
}
ENGINE_Q_KEYS = {
    "fieldPath",
    "labelPath",
    "inputPath",
    "optionsPath",
    "optionsTextPath",
    "optionsSelectedPath",
    "sectionPath",
    "inputTextPath",
    "valueRequired",
    "optionsSource",
    "fillActions",
}
ENGINE_TOP_KEYS = {
    "urls",
    "inputSelectors",
    "trackedInputSelectors",
    "submitButtonPaths",
    "continueButtonPaths",
    "submittedSuccessPaths",
    "proxySubmitButtons",
    "containerPath",
    "defaultMethod",
    "defaultEventOptions",
    "applyButtonPaths",
    "applyOptionPaths",
    "urlsExcluded",
    "pathsExcluded",
    "embeddedPaths",
    "containerRequired",
    "fillInputInterval",
    "fillInputGroupInterval",
    "orderByDomPosition",
    "deferSubmissionModalPaths",
    "validationScopePaths",
}
# Table keys that are not fill behaviour (job info extraction, analytics).
NOT_FILL_BEHAVIOUR = {
    "trackedObjExtractors",
    "jobInfoExtractors",
    "sourceKeys",
    "sourceCookies",
    "helpMessageUrls",
    "analyticsEventSelectors",
    "warningMessage",
    "defaultTrackMethod",
}


def walk(variants: list, acc: dict) -> None:
    for v in variants:
        if not isinstance(v, dict):
            continue
        acc["variant_keys"].update(k for k in v if k not in ENGINE_VARIANT_KEYS)
        m = v.get("method") or "default"
        if m in SKIP_METHODS:
            acc["skipped"][m] += 1
        elif m not in ENGINE_METHODS:
            acc["methods"][m] += 1
        for a in v.get("actions") or []:
            if not isinstance(a, dict):
                continue
            am = a.get("method") or ("event" if a.get("event") else "click")
            if am in SKIP_METHODS:
                acc["skipped"]["action:" + am] += 1
            elif am not in ENGINE_METHODS and am != "event":
                acc["methods"]["action:" + am] += 1
            acc["action_keys"].update(k for k in a if k not in ENGINE_ACTION_KEYS)
        for _sub_name, sub_variants in v.get("inputSelectors") or []:
            walk(sub_variants, acc)


def audit(table: dict) -> tuple[list[str], Counter]:
    rows = []
    totals: Counter = Counter()
    detail = defaultdict(dict)
    for name, cfg in sorted(table.items()):
        if not isinstance(cfg, dict) or not (
            cfg.get("inputSelectors") or cfg.get("trackedInputSelectors")
        ):
            continue
        acc = {
            "variant_keys": Counter(),
            "skipped": Counter(),
            "methods": Counter(),
            "action_keys": Counter(),
        }
        dropped = []
        kept = 0
        for fname, variants in cfg.get("inputSelectors") or []:
            if fname in FACTS or fname in STEPS:
                kept += 1
            else:
                dropped.append(fname)
            walk(variants, acc)
        q_missing: Counter = Counter()
        for q in cfg.get("trackedInputSelectors") or []:
            q_missing.update(k for k in q if k not in ENGINE_Q_KEYS)
            for a in q.get("fillActions") or []:
                if isinstance(a, dict):
                    am = a.get("method") or ("event" if a.get("event") else "click")
                    if am not in ENGINE_METHODS and am != "event" and am not in SKIP_METHODS:
                        acc["methods"]["fill:" + am] += 1
                    acc["action_keys"].update(k for k in a if k not in ENGINE_ACTION_KEYS)
        top_missing = sorted(
            k
            for k in cfg
            if k not in ENGINE_TOP_KEYS and k not in NOT_FILL_BEHAVIOUR and not k.isdigit()
        )
        matches = [m for m in (glob_to_match(g) for g in cfg.get("urls") or []) if m]
        status = (
            "hand reader" if name in HAND else ("engine" if matches else "NOT LOADED (no url glob)")
        )
        ignored = sum(acc["action_keys"].values()) + sum(acc["variant_keys"].values())
        rows.append(
            (
                name,
                status,
                kept,
                len(dropped),
                len(cfg.get("trackedInputSelectors") or []),
                sum(q_missing.values()),
                sum(acc["methods"].values()),
                ignored,
                len(top_missing),
            )
        )
        totals["fields"] += kept + len(dropped)
        totals["dropped"] += len(dropped)
        totals["gaps"] += (
            sum(q_missing.values()) + sum(acc["methods"].values()) + ignored + len(top_missing)
        )
        totals["not_loaded"] += status.startswith("NOT")
        detail[name] = {
            "status": status,
            "urls": cfg.get("urls") or [],
            "dropped_fields": dropped,
            "skipped_variants": dict(acc["skipped"]),
            "methods_without_branch": dict(acc["methods"]),
            "action_keys_ignored": dict(acc["action_keys"]),
            "variant_keys_ignored": dict(acc["variant_keys"]),
            "question_keys_ignored": dict(q_missing),
            "top_keys_ignored": top_missing,
        }
    lines = [
        "# Selector table coverage audit",
        "",
        "| ATS | how we run it | fields kept | fields dropped | question readers | reader keys ignored | methods without a branch | action/variant keys ignored | top-level keys ignored |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    lines += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    lines += [
        "",
        f"Fields in the table: {totals['fields']}; dropped: {totals['dropped']}; "
        f"engine gaps: {totals['gaps']}; ATSs not loaded: {totals['not_loaded']}.",
        "",
        "## Per ATS",
    ]
    for name, d in detail.items():
        lines.append(f"\n### {name} ({d['status']})")
        if d["status"].startswith("NOT"):
            lines.append(f"- url globs: {d['urls']}")
        if d["dropped_fields"]:
            lines.append(f"- dropped fields: {', '.join(d['dropped_fields'])}")
        for k in (
            "skipped_variants",
            "methods_without_branch",
            "action_keys_ignored",
            "variant_keys_ignored",
            "question_keys_ignored",
        ):
            if d[k]:
                lines.append(f"- {k.replace('_', ' ')}: {d[k]}")
        if d["top_keys_ignored"]:
            lines.append(f"- top-level keys ignored: {d['top_keys_ignored']}")
    return lines, totals


def main(argv: list[str]) -> int:
    table = json.loads(Path(argv[1]).read_text())["ATS"]
    lines, _totals = audit(table)
    out = Path(argv[2]) if len(argv) > 2 else None
    if out:
        out.write_text("\n".join(lines) + "\n")
    print("\n".join(lines[: lines.index("## Per ATS")]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
