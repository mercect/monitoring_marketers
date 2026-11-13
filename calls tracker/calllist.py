# calllist.py — call-list order: which list a pid is on, and its place in it.
# -----------------------------------------------------------------------------
# Two columns on the data_entry tab define the call lists: `assigned_day`
# (4 or 5, written "4" or "03Nov-D4") and `ordering` (position in that list,
# restarting for each assigned day). Used by the queues in app.py and by the
# assignment checks in assign.py.
# -----------------------------------------------------------------------------
import pandas as pd

from data_io import parse_dates


def assigned_day_number(s):
    """The call-list day (4 or 5) from `assigned_day`, whichever way the sheet
    writes it: a plain "4" / "5", "Day 4", or a batch-stamped "03Nov-D4". The
    number AFTER the D is the day; taking the first number instead read
    "03Nov-D4" as day 3 and merged that batch's two lists. Blank when there is
    no recognisable day. Used for SORTING only: the tables show assigned_day
    exactly as the sheet writes it."""
    s = pd.Series(s).fillna("").astype(str).str.strip()
    after_d = s.str.extract(r"(?i)(?:d|day)\s*(\d+)\s*$")[0]
    plain = s.str.extract(r"^(\d+)$")[0]
    return after_d.fillna(plain).fillna("")


def with_list_order(df):
    """df plus the keys that put pids in CALL-LIST ORDER, from two columns on
    the data_entry tab: `assigned_day` (4 or 5, which call list the pid is on)
    and `ordering` (its position in that list; the numbering restarts for each
    assigned day). Adds _rec (recruitment date), _day and _ord, so sorting on
    (_rec, _day, _ord) gives each recruitment day's day-4 list, then its day-5
    list, each in order. Guarantees both columns exist to show, blank until the
    sheet has them."""
    out = df.copy()
    for c in ("assigned_day", "ordering"):
        if c not in out.columns:
            out[c] = ""
    out["_day"] = pd.to_numeric(assigned_day_number(out["assigned_day"]), errors="coerce")
    out["_ord"] = pd.to_numeric(out["ordering"], errors="coerce")
    if "date_recruited" in out.columns:
        rec = parse_dates(out["date_recruited"])
        rec.index = out.index
        out["_rec"] = rec
    else:
        out["_rec"] = pd.NaT
    return out
