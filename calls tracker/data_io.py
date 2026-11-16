# data_io.py — shared data loading for BOTH dashboards.
# -----------------------------------------------------------------------------
# app.py     = the call-tracking monitor (field / supervisor team)
# pi_app.py  = the Principal Investigators dashboard (indicators)
#
# Both read the same two Google Sheet tabs (links in .streamlit/secrets.toml),
# so the loading lives here once instead of being copy-pasted per dashboard.
# -----------------------------------------------------------------------------
import pathlib
import re
from datetime import datetime

import pandas as pd
import streamlit as st

from rollup import (rollup, summarize, frame_from_attempts, sheet_csv,
                    hide_columns, drop_test_attempts, _to_dt)

REFRESH_SECONDS = 300

# gid -> human name, so the dashboards can say WHICH tab they are showing.
# A gid that isn't listed still prints as `gid=<n>`, so an unknown tab is
# visible rather than silently anonymous.
# The tab the roster is SUPPOSED to come from. Anything else is almost certainly
# a stale secrets entry — most likely data_entry_archived, the previous roster,
# which still loads fine and quietly reports the wrong sample.
EXPECTED_SAMPLE_TAB = "data_entry"

TAB_NAMES = {
    # current round: Launch 0 (phone_survey_launch0_nov2026, November 2026)
    "948483771": "phone_survey",
    "342765187": "data_entry",
    # training round (October 2026 spreadsheet) - labelled as old so a stale
    # secrets link trips the wrong-roster banner instead of passing silently
    "1615108913": "phone_survey (training round)",
    "970246647": "data_entry (training round)",
    # pilot round (August 2026 spreadsheet) - labelled as old so a stale
    # secrets link trips the wrong-roster banner instead of passing silently
    "249772860": "phone_survey (pilot round)",
    "517204918": "data_entry (pilot round)",
    "716196793": "data_entry_archived",
}


def code_stamp():
    """Newest edit time across the modules this dashboard is built from.

    Streamlit re-runs app.py on save, but rollup.py / data_io.py / indicators.py
    are IMPORTED modules — Python caches them in sys.modules at startup and a
    rerun does not re-import them. So the app can show updated labels while still
    computing with stale logic.

    Covers every module, not just rollup: a change to data_io or indicators is
    exactly as stale-able, and reporting only rollup gave a stamp that looked
    current while another module was out of date."""
    newest, names = None, []
    for mod in ("rollup", "data_io", "indicators"):
        try:
            m = __import__(mod)
            ts = pathlib.Path(m.__file__).stat().st_mtime
            if newest is None or ts > newest:
                newest, names = ts, [mod]
            elif ts == newest:
                names.append(mod)
        except Exception:
            continue
    if newest is None:
        return "unknown"
    return f"{datetime.fromtimestamp(newest):%H:%M:%S} ({'/'.join(names)})"


def _read_sheet(url, key, tab, gid):
    """Read a sheet CSV, or raise an error that says WHICH link is broken.

    pandas surfaces a bare "HTTP Error 400: Bad Request" here, which gives no
    clue whether the attempts link or the roster link is at fault - and the two
    look nearly identical in the secrets box. Google returns 400 for a gid that
    does not exist on the sheet, and 401/403/404 when the sheet is not shared."""
    try:
        return pd.read_csv(url, dtype=str, keep_default_na=False)
    except Exception as e:
        hint = ""
        code = "".join(ch for ch in str(e) if ch.isdigit())[:3]
        if code == "400":
            hint = (f" Google rejected `gid={gid or '(missing)'}` - that tab id does "
                    "not exist on this sheet. Open the tab in your browser and copy "
                    "the gid from the address bar.")
        elif code in ("401", "403", "404"):
            hint = (" The sheet is not readable by the link - set Share -> General "
                    "access -> Anyone with the link -> Viewer.")
        raise RuntimeError(
            f"Could not read `{key}` (tab {tab}, gid={gid or 'missing'}).{hint}"
            f"{chr(10)}{chr(10)}Original error: {e}") from e


def _tab_of(url):
    """(tab name, gid) from a sheet URL. An unlisted gid is reported as such
    rather than guessed, and a link with no gid is flagged loudly — Google
    silently serves the FIRST tab in that case, which is a real trap."""
    m = re.search(r"gid=(\d+)", url or "")
    if not m:
        return "⚠️ no gid in link — Google serves the FIRST tab", ""
    gid = m.group(1)
    return TAB_NAMES.get(gid, "⚠️ unrecognised tab"), gid


def _secret(key, default=""):
    """Read a secret without crashing when no secrets.toml exists yet."""
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default



# The data_entry tab renamed its call-list columns: assigned_day is now
# assigned_list and ordering is now call_order. A sheet still using the old
# names is read under the new ones, so the dashboard works with either.
LIST_COLUMN_RENAMES = {"assigned_day": "assigned_list", "ordering": "call_order"}


def rename_list_columns(df):
    ren = {old: new for old, new in LIST_COLUMN_RENAMES.items()
           if old in df.columns and new not in df.columns}
    return df.rename(columns=ren) if ren else df


# The form's tablet list (choice value -> label). The form stores the number;
# the dashboard shows the name. -55 "Other specify" takes the typed name.
TABLETS = {
    1: "Moto G13 002", 2: "Moto G13 005", 3: "Moto G13 004", 4: "Moto G13 008",
    5: "Moto G13 006", 6: "Moto G13 007", 7: "Moto G14 001", 8: "Moto G14 002",
    9: "Moto G14 004", 10: "Moto G22 Phone 2", 11: "Moto G22 Phone 3",
    12: "Moto G22 Phone 4", 13: "Moto G22 Phone 5", 14: "Moto G22 Phone 6",
    15: "Moto G22 Phone 11", 16: "Moto G22 Phone 12", 17: "Moto G22 Phone 15",
    18: "Moto G22 Phone 17", 19: "Moto G22 Phone 18", 20: "Moto G22 Phone 20",
    21: "Doogee Tab 11", 22: "Doogee Tab 12", 23: "Doogee Tab 13", 24: "Doogee Tab 14",
    25: "Doogee Tab 15", 26: "Doogee Tab 17", 27: "Doogee Tab 18", 28: "Doogee Tab 19",
    29: "Doogee Tab 20", 30: "Doogee Tab 21", 31: "Doogee Tab 22", 32: "Doogee Tab 23",
    33: "Doogee Tab 24", 34: "Doogee Tab 26", 35: "Doogee Tab 27", 36: "Doogee Tab 28",
    37: "Doogee Tab 29", 38: "Doogee Tab 30", 39: "Doogee Tab 31", 40: "Doogee Tab 32",
    41: "Doogee Tab 33", 42: "Doogee Tab 34", 43: "Doogee Tab 35", 44: "Doogee Tab 36",
    45: "Doogee Tab 37", 46: "Doogee Tab 38", 47: "Doogee Tab 39",
    48: "Samsung Tab 20", 49: "Samsung Tab 21", 50: "Samsung Tab 22", 51: "Samsung Tab 23",
    52: "Samsung Tab 27", 53: "Samsung Tab 28", 54: "Samsung Tab 29", 55: "Samsung Tab 30",
    56: "Samsung Tab 31", 57: "Samsung Tab 32", 58: "Samsung Tab 33", 59: "Samsung Tab 34",
    60: "Samsung Tab 25",
}
TABLET_OTHER = -55


def tablet_name(code, other=""):
    """'12' / '12.0' -> 'Moto G22 Phone 4'; -55 -> the typed name (or 'Other');
    anything that is not a code on the list (a name typed in an older round)
    is kept as it is."""
    s = str(code if code is not None else "").strip()
    if s.lower() in ("", "nan", "none"):
        return ""
    try:
        n = int(float(s))
    except ValueError:
        return s
    if n == TABLET_OTHER:
        o = str(other or "").strip()
        return o if o.lower() not in ("", "nan", "none") else "Other (not specified)"
    return TABLETS.get(n, s)


def tablet_column(df):
    """The tablet holding a partial save. The form now asks it as rs_tab (the
    tablet chosen from the list, stored as its number) and rs_tab_oth (typed
    when it is not on the list); older submissions used rs_tab_id. All three
    meet in rs_tab_id, the column the rollup reads, as the tablet's NAME:
    rs_tab_oth when filled, else rs_tab, else the old rs_tab_id. tab_id (the
    long survey's tablet) is turned from a number into the name too."""
    out = df.copy()
    blank = pd.Series("", index=out.index)
    if "tab_id" in out.columns:
        t_oth = out.get("tab_id_oth", blank).fillna("").astype(str)
        out["tab_id"] = [tablet_name(c, o) for c, o in zip(out["tab_id"], t_oth)]
    if "rs_tab" not in out.columns and "rs_tab_oth" not in out.columns:
        return out
    oth = out.get("rs_tab_oth", blank).fillna("").astype(str).str.strip()
    pick = pd.Series([tablet_name(c, o) for c, o in
                      zip(out.get("rs_tab", blank).fillna(""), oth)], index=out.index)
    # The old rs_tab_id was typed by hand: kept as typed, never read as a code.
    old = out.get("rs_tab_id", blank).fillna("").astype(str).str.strip()
    out["rs_tab_id"] = oth.where(oth != "", pick.where(pick != "", old))
    return out

@st.cache_data(ttl=REFRESH_SECONDS)
def load_submissions():
    """Read the raw submission export (Kobo) from the sheet link, or the sample."""
    raw = _secret("sheet_csv_url", "")
    url = sheet_csv(raw)
    tab, gid = _tab_of(raw)
    origin = "Google Sheet"
    if not url:
        url, tab, gid, origin = "../sample_database.csv", "sample_database.csv", "", "demo file"
    df = _read_sheet(url, "sheet_csv_url", tab, gid)
    df = tablet_column(df)
    # test submissions are not observations - drop before anything counts them
    df, n_test = drop_test_attempts(df)
    return df, {"origin": origin, "tab": tab, "gid": gid, "rows": len(df),
                "dropped_test": n_test, "read": f"{datetime.now():%H:%M:%S}"}


@st.cache_data(ttl=REFRESH_SECONDS)
def load_sample(_subs):
    """Read the full sample / roster tab (one row per pid). If no sample tab is
    set yet, derive a frame from the attempts so the summary still runs — but
    that fallback can't include never-called respondents."""
    raw = _secret("sample_csv_url", "")
    url = sheet_csv(raw)
    if url:
        tab, gid = _tab_of(raw)
        df = _read_sheet(url, "sample_csv_url", tab, gid)
        df = rename_list_columns(df)
        return df, {"origin": "Google Sheet", "tab": tab, "gid": gid,
                    "rows": len(df), "dropped_test": 0,
                    "read": f"{datetime.now():%H:%M:%S}"}
    return frame_from_attempts(_subs), {
        "origin": "⚠️ derived from attempts", "tab": "no sample tab set", "gid": "",
        "rows": 0, "dropped_test": 0, "read": f"{datetime.now():%H:%M:%S}"}


def load_all(decisions=None, name_map=None):
    """subs + sample as loaded, plus the rolled-up cases and per-pid summary.

    The rollup runs on the FULL sample tab, then hide_columns() drops the
    covariates that must not be published (rollup.HIDE_FROM_DASHBOARD — the
    `pitch` arm, `signup_scan`, `age`, `education`, `pre_symptom_*`). Doing it
    here means no dashboard view, drill-down table or CSV download can leak
    them, while every derived column is still computed from the whole sheet."""
    subs, a_meta = load_submissions()
    sample, s_meta = load_sample(subs)
    # One name per enumerator (enumerators.py): every spelling a person typed
    # becomes their official name, so each is counted once on every tab.
    if name_map:
        from enumerators import apply_names
        subs = apply_names(subs, name_map)
    # Supervisor review: each decision that changes a status becomes a
    # supervisor row BEFORE the rollup, so every tab sees the reviewed status.
    # Only the call-tracking monitor passes `decisions`; the PI dashboard does not.
    if decisions is not None and len(decisions):
        from review import apply_decisions
        subs = apply_decisions(subs, decisions)
    cases = rollup(subs)
    summary = hide_columns(summarize(sample, subs))
    meta = {"attempts": a_meta, "sample": s_meta, "code_stamp": code_stamp()}
    return subs, hide_columns(sample), cases, summary, meta


def route_column(summary):
    """Route lives on the sample tab (roster) -> it's on the summary, not the
    attempts. Returns the column name, or None when the sheet has no route."""
    return next((c for c in summary.columns
                 if c == "route_recruited" or "route" in c.lower()), None)


def parse_dates(s):
    """Parse a roster date column, tz-naive. Shared by the sidebar filter and
    newest_batch() so they cannot disagree about what a date means.

    The survey timestamps are ISO, but the roster's date_recruited arrives from
    the sheet as dd/mm/yyyy - which pandas reads month-first by default, so
    05/06/2026 would land in May. Day-first only when the column looks like it."""
    s = pd.Series(s).astype(str).str.strip()
    if s.str.match(r"^\d{1,2}/\d{1,2}/\d{4}").any():
        return pd.to_datetime(s, errors="coerce", dayfirst=True)
    return _to_dt(s)


def newest_batch(summary, route_col=None):
    """(date, routes) of the MOST RECENT recruitment batch on the roster.

    Both dashboards OPEN ON this batch: the recruitment-date filter starts at
    that date and the route filter starts on the route(s) recruited then. Every
    filter is still a widget the user can widen back to everything, so this is a
    starting view and not a restriction - nothing is hidden, it just is not
    selected on first load.

    DERIVED, not hardcoded, for two reasons. It keeps working when the next
    batch is recruited, with no edit. And it keeps the two filters CONSISTENT:
    on this study each recruitment date is one route (28/08 Bawbaw, 01/09 Jui,
    04/09 Regent), so a route pinned by name while the date default moved on
    would intersect to nothing and render a blank dashboard.

    Returns (None, []) when the roster carries no usable date, and the callers
    then fall back to selecting everything."""
    if "date_recruited" not in summary.columns or not len(summary):
        return None, []
    d = parse_dates(summary["date_recruited"])
    if d.dropna().empty:
        return None, []
    last = d.max().date()
    on_last = summary.loc[d.dt.date == last]
    routes = (sorted(x for x in on_last[route_col].dropna().unique() if str(x).strip())
              if route_col and route_col in on_last.columns else [])
    return last, routes
