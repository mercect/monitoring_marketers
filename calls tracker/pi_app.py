# pi_app.py — Principal Investigators dashboard (indicators only)
# -----------------------------------------------------------------------------
# You do NOT need to know Python to use this. From the "dashboard" folder run:
#     streamlit run pi_app.py
# or just double-click  START_PI_DASHBOARD.bat
#
# What this is:
#   The PI team's view of the survey — headline recruitment and attrition rates
#   over the whole sample. It reads the same two Google Sheet tabs as the
#   call-tracking monitor (app.py), but shows only the Indicators tab: no case
#   lists, no work queues, no enumerator detail.
#
# The Indicators view itself lives in indicators.py, shared with nothing else —
# app.py (the call-tracking monitor) no longer carries it.
# -----------------------------------------------------------------------------
import streamlit as st

from auth import require_password
from data_io import load_all, route_column, EXPECTED_SAMPLE_TAB
from indicators import render_indicators

st.set_page_config(page_title="PI Dashboard — Indicators", page_icon="📊", layout="wide")

# Password gate — see auth.py. Must come straight after set_page_config, before
# anything is drawn, so no data leaks onto the page for a signed-out visitor.
require_password("🔒 PI Dashboard")

st.title("📊 PI Dashboard — Survey Indicators")

try:
    subs, sample, cases, summary, meta = load_all()
except Exception as e:
    st.error(f"Could not load / roll up the data.\n\n{e}")
    st.stop()

left, right = st.columns([4, 1])
left.markdown(
    "**📊 Indicators** — the recruitment rate and attrition on both definitions, over "
    "the whole sample. Day-to-day case management lives in the separate "
    "**Call-Tracking Monitor** dashboard."
)
if right.button("🔄 Refresh now"):
    st.cache_data.clear()
    st.rerun()

_a, _s = meta["attempts"], meta["sample"]
if _s["tab"] != EXPECTED_SAMPLE_TAB:
    st.error(f"🛑 **Reading the wrong roster tab: `{_s['tab']}`** — expected "
             f"`{EXPECTED_SAMPLE_TAB}`. Every figure below is about the wrong "
             "respondents. Fix `sample_csv_url` in ⚙ Settings → Secrets.")
st.caption(f"Attempts: {_a['tab']} ({_a['rows']} rows)  ·  "
           f"Sample: {_s['tab']} ({_s['rows']} rows)  ·  "
           f"{len(summary)} pids  ·  {len(subs)} call attempts  ·  "
           f"read {_a['read']}, rollup logic {meta['code_stamp']}.")

tab_ind, = st.tabs(["📊 Indicators"])

# ============================================================================
# TAB — INDICATORS (headline aggregates over the sample)
# ============================================================================
with tab_ind:
    render_indicators(summary, route_column(summary))
