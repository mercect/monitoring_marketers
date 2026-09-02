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
                    hide_columns, drop_test_attempts)

REFRESH_SECONDS = 300

# gid -> human name, so the dashboards can say WHICH tab they are showing.
# A gid that isn't listed still prints as `gid=<n>`, so an unknown tab is
# visible rather than silently anonymous.
# The tab the roster is SUPPOSED to come from. Anything else is almost certainly
# a stale secrets entry — most likely data_entry_archived, the previous roster,
# which still loads fine and quietly reports the wrong sample.
EXPECTED_SAMPLE_TAB = "data_entry"

TAB_NAMES = {
    "249772860": "phone_survey",
    "517204918": "data_entry",
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
    for mod in ("rollup", "data_io", "indicators", "progress"):
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


MISSING_SECRET = (
    "No `{key}` is set, so there is no data to read.\n\n"
    "On Streamlit Community Cloud: open the app menu → **Settings → Secrets** "
    "and paste the line\n\n"
    "    {key} = \"<your Google Sheet tab URL>\"\n\n"
    "Running locally: put the same line in `dashboard/.streamlit/secrets.toml` "
    "(see `secrets.toml.example`).")


@st.cache_data(ttl=REFRESH_SECONDS)
def load_submissions():
    """Read the raw submission export (Kobo) from the sheet link.

    There is deliberately NO local-file fallback. The demo fixture it used to
    fall back to held real-looking respondent names and phone numbers, so it is
    not shipped; without it a silent fallback would just fail later with a
    confusing missing-file error. Missing config now says so directly."""
    raw = _secret("sheet_csv_url", "")
    url = sheet_csv(raw)
    if not url:
        raise RuntimeError(MISSING_SECRET.format(key="sheet_csv_url"))
    tab, gid = _tab_of(raw)
    origin = "Google Sheet"
    df = pd.read_csv(url, dtype=str, keep_default_na=False)
    # test submissions are not observations - drop before anything counts them
    df, n_test = drop_test_attempts(df)
    return df, {"origin": origin, "tab": tab, "gid": gid, "rows": len(df),
                "dropped_test": n_test, "read": f"{datetime.now():%H:%M:%S}"}


@st.cache_data(ttl=REFRESH_SECONDS)
def load_sample(_subs):
    """Read the full sample / roster tab (one row per pid).

    If no sample tab is set, derive a frame from the attempts so the app still
    renders — that fallback holds no respondent data of its own, but it cannot
    include never-called pids, so the banner says so loudly."""
    raw = _secret("sample_csv_url", "")
    url = sheet_csv(raw)
    if url:
        df = pd.read_csv(url, dtype=str, keep_default_na=False)
        tab, gid = _tab_of(raw)
        return df, {"origin": "Google Sheet", "tab": tab, "gid": gid,
                    "rows": len(df), "dropped_test": 0,
                    "read": f"{datetime.now():%H:%M:%S}"}
    return frame_from_attempts(_subs), {
        "origin": "⚠️ derived from attempts", "tab": "no sample tab set", "gid": "",
        "rows": 0, "dropped_test": 0, "read": f"{datetime.now():%H:%M:%S}"}


def load_all(keep=()):
    """subs + sample as loaded, plus the rolled-up cases and per-pid summary.

    The rollup runs on the FULL sample tab, then hide_columns() drops the
    covariates that must not be published (rollup.HIDE_FROM_DASHBOARD — the
    `pitch` arm, `signup_scan`, `age`, `education`, `pre_symptom_*`). Doing it
    here means no dashboard view, drill-down table or CSV download can leak
    them, while every derived column is still computed from the whole sheet.

    `keep` names columns to publish anyway. Only the PI dashboard uses it, and
    only for `pitch` — the PIs need the trial arm to break recruitment down by
    it. The call-tracking monitor calls load_all() with no `keep`, so the arm
    stays off the field team's screen."""
    subs, a_meta = load_submissions()
    sample, s_meta = load_sample(subs)
    cases = rollup(subs)
    summary = hide_columns(summarize(sample, subs), keep)
    meta = {"attempts": a_meta, "sample": s_meta, "code_stamp": code_stamp()}
    return subs, hide_columns(sample, keep), cases, summary, meta


def route_column(summary):
    """Route lives on the sample tab (roster) -> it's on the summary, not the
    attempts. Returns the column name, or None when the sheet has no route."""
    return next((c for c in summary.columns
                 if c == "route_recruited" or "route" in c.lower()), None)
