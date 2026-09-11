"""The posting-report vocabulary shared by user and admin surfaces."""

from pydantic import BaseModel


class ReportKind(BaseModel):
    kind: str
    label: str


REPORT_KINDS = ("stale", "wrong_data", "closed", "other")
_REPORT_LABELS = {
    "stale": "Posting is stale",
    "wrong_data": "Details are wrong",
    "closed": "Posting is closed",
    "other": "Something else",
}


def report_kinds() -> list[ReportKind]:
    """The kinds a report can carry, with the label the form shows; one copy,
    served to the board's report modal and the admin reports page."""
    return [ReportKind(kind=kind, label=_REPORT_LABELS[kind]) for kind in REPORT_KINDS]
