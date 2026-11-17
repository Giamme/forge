"""The one rule for what state a finding is in, shared by the CLI, the sweep and forge-ui."""
from __future__ import annotations

# Only these two count as open. `swept`, `shadow`, `unlabelled` and `superseded` are shown, not worked on.
OPEN = ("open", "to-file")

_RECORD_WINS = ("duplicate", "dismissed", "fixed", "filed", "to-file")


def effective(record_entry, issues_entry, review_row) -> str:
    """The status of one finding, from the first of these that answers.

    1. The record: duplicate, dismissed, fixed, filed, to-file. `open` means reopened: go to 3
       (a filed issue in step 2 still shows).
    2. The issues entry (a `forge issues --json` finding): filed and CLOSED is fixed, filed is filed,
       swept is swept, dismissed (the old store) is dismissed.
    3. The review row (forge-ui only, may be None): a shadow review, an unlabelled one, a superseded one.
    4. open.

    An entry that holds only tags, a note, an edit or a draft has no status: it says nothing about the state.
    """
    recorded = record_entry.get("status") if isinstance(record_entry, dict) else None
    if recorded in _RECORD_WINS:
        return recorded
    reopened = recorded == "open"
    if isinstance(issues_entry, dict):
        filed = issues_entry.get("filed")
        if filed:
            state = filed.get("state") if isinstance(filed, dict) else None
            return "fixed" if isinstance(state, str) and state.upper() == "CLOSED" else "filed"
        if not reopened:
            if issues_entry.get("swept"):
                return "swept"
            if issues_entry.get("dismissed"):
                return "dismissed"
    if isinstance(review_row, dict):
        if review_row.get("kind") == "shadow":
            return "shadow"
        if review_row.get("confidence") == "UNSTATED":
            return "unlabelled"
        if review_row.get("superseded"):
            return "superseded"
    return "open"
