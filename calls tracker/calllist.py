# calllist.py — call-list order: which list a pid is on, and its place in it.
# -----------------------------------------------------------------------------
# Two columns on the data_entry tab define the call lists: `assigned_list`
# (4 or 5, written "4" or "03Nov-D4") and `call_order` (position in that list,
# restarting for each assigned day). Used by the queues in app.py and by the
# assignment checks in assign.py.
# -----------------------------------------------------------------------------
import pandas as pd

from data_io import parse_dates


def assigned_list_number(s):
    """The call-list day (4 or 5) from `assigned_list`, whichever way the sheet
    writes it: a plain "4" / "5", "Day 4", or a batch-stamped "03Nov-D4". The
    number AFTER the D is the day; taking the first number instead read
    "03Nov-D4" as day 3 and merged that batch's two lists. Blank when there is
    no recognisable day. Used for SORTING only: the tables show assigned_list
    exactly as the sheet writes it."""
    s = pd.Series(s).fillna("").astype(str).str.strip()
    after_d = s.str.extract(r"(?i)(?:d|day)\s*(\d+)\s*$")[0]
    plain = s.str.extract(r"^(\d+)$")[0]
    return after_d.fillna(plain).fillna("")


def with_list_order(df):
    """df plus the keys that put pids in CALL-LIST ORDER, from two columns on
    the data_entry tab: `assigned_list` (4 or 5, which call list the pid is on)
    and `call_order` (its position in that list; the numbering restarts for each
    assigned day). Adds _rec (recruitment date), _day and _ord, so sorting on
    (_rec, _day, _ord) gives each recruitment day's day-4 list, then its day-5
    list, each in order. Guarantees both columns exist to show, blank until the
    sheet has them."""
    out = df.copy()
    for c in ("assigned_list", "call_order"):
        if c not in out.columns:
            out[c] = ""
    out["_day"] = pd.to_numeric(assigned_list_number(out["assigned_list"]), errors="coerce")
    out["_ord"] = pd.to_numeric(out["call_order"], errors="coerce")
    if "date_recruited" in out.columns:
        rec = parse_dates(out["date_recruited"])
        rec.index = out.index
        out["_rec"] = rec
    else:
        out["_rec"] = pd.NaT
    return out


def first_call_date(df):
    """The day each pid's list is first called: recruitment date + the list's
    day number (a "12Nov-D4" pid recruited on 12 Nov is first called on 16 Nov).
    NaT when either is missing."""
    o = with_list_order(df)
    return (o["_rec"].dt.normalize() + pd.to_timedelta(o["_day"], unit="D")).dt.date


def lists_due(never_called, today, lookback=3):
    """Never-called pids left on each list due by `today`, one row per list:
    assigned_list, due (first-call date), left. Lists due today first, then
    lists due in the previous `lookback` days that still have pids left
    (overdue). Older lists are left out: they are not today's work."""
    cols = ["assigned_list", "due", "left"]
    if never_called is None or never_called.empty:
        return pd.DataFrame(columns=cols)
    d = never_called.assign(due=first_call_date(never_called).values)
    d = d[d["due"].notna()]
    d = d[(d["due"] <= today) & (d["due"] >= today - pd.Timedelta(days=lookback))]
    if d.empty:
        return pd.DataFrame(columns=cols)
    out = (d.groupby(["assigned_list", "due"]).size().rename("left").reset_index())
    return out.sort_values(["due", "assigned_list"], ascending=[False, True]).reset_index(drop=True)[cols]


def _sheet_day(s):
    """'16Nov2026' (the data_entry format), '16/11/2026' or ISO -> date."""
    s = pd.Series(s).fillna("").astype(str).str.strip()
    d = pd.to_datetime(s, format="%d%b%Y", errors="coerce")
    rest = d.isna() & s.ne("")
    if rest.any():
        d = d.where(~rest, parse_dates(s[rest]).reindex(s.index))
    return d.dt.date


def active_lists(roster, today, days=3):
    """The call lists being worked on `today`: first call day <= today <= last
    call day. Uses the roster's own first_call_date / last_call_day when it has
    them; otherwise recruitment date + the list's day number, for `days` days."""
    if roster is None or roster.empty or "assigned_list" not in roster.columns:
        return []
    r = roster[roster["assigned_list"].fillna("").astype(str).str.strip().ne("")]
    if r.empty:
        return []
    first = (_sheet_day(r["first_call_date"]) if "first_call_date" in r.columns
             else pd.Series(pd.NaT, index=r.index))
    first = pd.Series(first.values, index=r.index)
    first = first.where(first.notna(), pd.Series(first_call_date(r).values, index=r.index))
    last = (pd.Series(_sheet_day(r["last_call_day"]).values, index=r.index)
            if "last_call_day" in r.columns else pd.Series(pd.NaT, index=r.index))
    fallback = pd.Series([f + pd.Timedelta(days=days - 1) if pd.notna(f) else pd.NaT for f in first],
                         index=r.index)
    last = last.where(last.notna(), fallback)
    first, last = pd.to_datetime(first, errors="coerce"), pd.to_datetime(last, errors="coerce")
    t = pd.Timestamp(today)
    on = ((first <= t) & (t <= last)).fillna(False).values
    return sorted(set(r.loc[on, "assigned_list"].astype(str).str.strip()))
