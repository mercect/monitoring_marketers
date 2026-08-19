# data_io.py — shared data loading for BOTH dashboards.
# -----------------------------------------------------------------------------
# app.py     = the call-tracking monitor (field / supervisor team)
# pi_app.py  = the Principal Investigators dashboard (indicators)
#
# Both read the same two Google Sheet tabs (links in .streamlit/secrets.toml),
# so the loading lives here once instead of being copy-pasted per dashboard.
# -----------------------------------------------------------------------------
import pandas as pd
import streamlit as st

from rollup import rollup, summarize, frame_from_attempts, sheet_csv

REFRESH_SECONDS = 300


def _secret(key, default=""):
    """Read a secret without crashing when no secrets.toml exists yet."""
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


@st.cache_data(ttl=REFRESH_SECONDS)
def load_submissions():
    """Read the raw submission export (Kobo) from the sheet link, or the sample."""
    url = sheet_csv(_secret("sheet_csv_url", ""))
    source = "Google Sheet (live Kobo export)"
    if not url:
        url = "../sample_database.csv"
        source = "sample_database.csv (demo — no sheet link set yet)"
    df = pd.read_csv(url, dtype=str, keep_default_na=False)
    return df, source


@st.cache_data(ttl=REFRESH_SECONDS)
def load_sample(_subs):
    """Read the full sample / roster tab (one row per pid). If no sample tab is
    set yet, derive a frame from the attempts so the summary still runs — but
    that fallback can't include never-called respondents."""
    url = sheet_csv(_secret("sample_csv_url", ""))
    if url:
        df = pd.read_csv(url, dtype=str, keep_default_na=False)
        return df, "Google Sheet (sample tab)"
    return frame_from_attempts(_subs), "derived from attempts (no sample tab set)"


def load_all():
    """subs + sample as loaded, plus the rolled-up cases and per-pid summary."""
    subs, source = load_submissions()
    sample, sample_source = load_sample(subs)
    cases = rollup(subs)
    summary = summarize(sample, subs)
    return subs, sample, cases, summary, source, sample_source


def route_column(summary):
    """Route lives on the sample tab (roster) -> it's on the summary, not the
    attempts. Returns the column name, or None when the sheet has no route."""
    return next((c for c in summary.columns
                 if c == "route_recruited" or "route" in c.lower()), None)
