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
from indicators import render_indicators, render_attrition
from progress import render_progress

st.set_page_config(page_title="PI Dashboard — Indicators", page_icon="📊", layout="wide")

# Password gate — see auth.py. Must come straight after set_page_config, before
# anything is drawn, so no data leaks onto the page for a signed-out visitor.
require_password("🔒 PI Dashboard")

st.title("📊 PI Dashboard — Survey Indicators")

try:
    # keep=("pitch",) — the PI view breaks recruitment down by trial arm. The
    # call-tracking monitor still calls load_all() with no keep, so the arm never
    # reaches the field team's screen. See rollup.HIDE_FROM_DASHBOARD.
    subs, sample, cases, summary, meta = load_all(keep=("pitch",))
except Exception as e:
    st.error(f"Could not load / roll up the data.\n\n{e}")
    st.stop()

left, right = st.columns([4, 1])
left.markdown(
    "**📊 Recruitment** — the rate at both eligibility stages, split by trial arm. "
    "**📉 Attrition** — both definitions, by arm, with a day-of-submission filter "
    "that moves the outcomes without moving the eligible base. Both tabs filter "
    "by route."
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

# Flip to False to hide the In Progress tab while it is being reworked; the
# module stays imported either way, so nothing has to be reverted.
SHOW_IN_PROGRESS = False

_titles = ["📊 Recruitment", "📉 Attrition"] + (
    ["🔧 In Progress"] if SHOW_IN_PROGRESS else [])
_tabs = st.tabs(_titles)
tab_rec, tab_att = _tabs[0], _tabs[1]
tab_prog = _tabs[2] if SHOW_IN_PROGRESS else None

# ============================================================================
# TAB — RECRUITMENT (the rate at both eligibility stages, by trial arm)
# ============================================================================
with tab_rec:
    render_indicators(summary, route_column(summary))

# ============================================================================
# TAB — ATTRITION (both definitions, and the disposition behind each base)
# ============================================================================
with tab_att:
    render_attrition(summary, subs, route_column(summary))

# ============================================================================
# TAB — IN PROGRESS (day-to-day monitoring: where to put effort next)
# ============================================================================
if SHOW_IN_PROGRESS:
    with tab_prog:
        render_progress(summary, subs, route_column(summary))
