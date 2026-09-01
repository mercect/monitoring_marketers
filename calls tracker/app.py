# app.py — Attrition & call-tracking monitor (revised 2026-08-02)
# -----------------------------------------------------------------------------
# You do NOT need to know Python to use this. To run it, follow GET_STARTED.md.
# In short, from the "dashboard" folder run:   streamlit run app.py
#
# What changed in this revision:
#   - Reads the RAW survey submission export (one row per call attempt) and rolls
#     it up to one row per case here (see rollup.py) — no separate pipeline needed.
#   - New status model: 0_/1=INACTIVE, 2_=PENDING, 3_=ACTIVE, 4_=NOTIFICATION.
#   - Work queues are driven by open cases (ACTIVE + PENDING), not ACTIVE only.
#   - Adds completion rate and both attrition definitions.
#
# Where the data comes from:
#   - A Google Sheet CSV link in .streamlit/secrets.toml (the published Kobo
#     export), else the local sample_database.csv so it works today. The loading
#     itself lives in data_io.py, shared with the PI dashboard.
#
# Not here any more: the Indicators tab (headline recruitment / attrition rates).
# It moved to the Principal Investigators dashboard — pi_app.py, rendered from
# indicators.py. This dashboard is the call-tracking / case-management view.
# -----------------------------------------------------------------------------
import pandas as pd
import streamlit as st

from auth import require_password
from data_io import load_all, route_column, REFRESH_SECONDS, code_stamp
from rollup import (OPEN_STATUSES, _to_dt, effort_by_enumerator, summary_kpis,
                    eligible_roster, recruit_exclusions,
                    CC_CALLBACK, CC_KEEP_CALLING, CC_RESUME)

st.set_page_config(page_title="Call-Tracking Monitor Dashboard", page_icon="📞", layout="wide")

# Password gate — see auth.py. Must come straight after set_page_config, before
# anything is drawn, so no data leaks onto the page for a signed-out visitor.
require_password("🔒 Call-Tracking Monitor")

# 0/1 flag columns shown as No/Yes on screen (the CSV download stays numeric 0/1).
FLAG_COLS = {
    "ever_attempted", "ever_picked_up", "ever_rescheduled", "ever_dropped",
    "is_complete", "last_is_answered", "last_is_incorrect", "ineligible_type1",
    "ineligible_type2", "is_open", "attrited_a", "attrited_b", "all_shifts_covered",
    "callback_overdue", "no_phone", "whatsapp_any", "is_supervisor",
    "is_incorrect", "is_othercontact", "eligible_to_call",
    # recruitment determinants carried through from the 2026-08 sample tab
    "ineligible_underage", "ineligible_outside_western_area", "ineligible_no_owned_phone",
    "ineligible_row_x", "ineligible_language_barrier", "ineligible_deaf_mute",
    "ineligible_nonpassenger_card", "recontacted_after_complete", "was_partialsaved",
}


def yesno(df):
    """Display copy: map known 0/1 flag columns to No/Yes (leaves everything else)."""
    out = df.copy()
    for c in set(out.columns) & FLAG_COLS:
        s = out[c].astype(str).str.strip()
        out[c] = s.map({"1": "Yes", "1.0": "Yes", "0": "No", "0.0": "No"}).fillna(s)
    return out


def count_pct(series, label):
    """Value counts as a small table with a % column and a Total row."""
    vc = series[series.astype(str).str.strip() != ""].value_counts()
    total = int(vc.sum())
    d = vc.rename_axis(label).reset_index(name="pids")
    d["%"] = (100 * d["pids"] / total).round().astype(int).astype(str) + "%" if total else "0%"
    d.loc[len(d)] = ["Total", total, "100%"]
    return d


SHIFTS_MOST, ATTEMPTS_MAX = 4, 8   # keep-calling escalation thresholds

# WhatsApp outreach is hidden for now (not in use). The `whatsapp` overlay is
# still computed in open_with_buckets, so flipping this back to True restores
# both the queue section and the monitoring-summary row with no other change.
SHOW_WHATSAPP = False


def open_with_buckets(cases, summary):
    """Open pids with effort columns merged and the Review escalation applied.
    Single source of truth for the action queues AND the monitoring summary."""
    su = cases["status"].astype(str).str.upper()
    oc = cases[su.isin({s.upper() for s in OPEN_STATUSES})].copy()
    oc["_active_first"] = (oc["status"].str.upper() == "ACTIVE").astype(int)
    # The times_* history counts live on the SUMMARY only, so they have to be
    # merged in here or the queues cannot show them. ever_rescheduled /
    # ever_dropped were merged in for the Callback queue alone and are no longer
    # used, so they are gone.
    eff = summary[[c for c in
                   ["pid", "total_attempts", "shifts_covered_n", "shifts_to_try",
                    "numbers_tried_of_available", "times_rescheduled", "times_dropped",
                    "times_noanswer", "times_off", "was_partialsaved",
                    "last_section", "tab_id", "last_comment"]
                   if c in summary.columns]]
    oc = oc.merge(eff, on="pid", how="left")
    oc["shifts_tried"] = (oc["shifts_covered_n"].fillna(0).astype(int).astype(str) + "/5")
    ta = oc["total_attempts"].fillna(0)
    sc = oc["shifts_covered_n"].fillna(0)
    # Escalation = overdue callbacks, OR a pid worked hard and still not finished.
    # Shifts and attempts are an OR, not an AND: either one on its own is grounds
    # for a supervisor to look. Not scoped to one queue — a pid this worked
    # deserves review whichever bucket it would otherwise sit in. `is_complete`
    # is always 0 here (open cases only), but the test is kept so the rule reads
    # as written: "and still no completion".
    esc_overdue = (oc["action_bucket"] == "Callback") & (oc["callback_when"] == "overdue")
    many_shifts = sc >= SHIFTS_MOST
    many_att = ta > ATTEMPTS_MAX
    esc_worked = (many_shifts | many_att) & (oc["is_complete"] != 1)
    # say which condition fired, so the queue is actionable rather than a bare label
    _why = pd.DataFrame({
        "overdue callback": esc_overdue,
        f"{SHIFTS_MOST}+/5 shifts": many_shifts & esc_worked,
        f">{ATTEMPTS_MAX} attempts": many_att & esc_worked,
    })
    # A list comprehension, NOT _why.apply(..., axis=1): on an EMPTY frame apply
    # returns a DataFrame rather than a Series, and assigning that to a single
    # column raises. Zero open cases is normal (every queue filtered out, or a
    # roster with nothing open), so this path has to survive it.
    oc["review_reason"] = [" + ".join(_why.columns[row]) for row in _why.values]
    oc.loc[esc_overdue | esc_worked, "action_bucket"] = "Review"
    # non-exclusive overlay: an open 2_ pid attempted >= 3 times should also be tried
    # on WhatsApp (it still shows in its primary queue too).
    oc["whatsapp"] = (oc["callcode"].astype(str).str.startswith("2_") & (ta >= 3)).astype(int)
    return oc


# Curated columns for the on-screen Respondent summary (the CSV download keeps all).
SUMMARY_COLS = [
    # identity
    "batch_code", "pid", "route_recruited", "date_recruited",
    # last attempt (current state)
    "enumerator", "current_status", "case_state", "last_submission_time", "current_callcode",
    "last_comment", "rsd_reason", "refuse_why", "is_supervisor", "call_length_min",
    "last_is_answered", "last_is_incorrect",
    # history (across all attempts)
    "total_attempts", "times_pickedup", "times_rescheduled", "times_dropped",
    "times_refused", "times_wrongnumber", "times_noanswer", "times_off",
    "times_incorrect", "times_ineligible", "times_othercontact", "times_dk_contact",
    "numbers_wrong", "numbers_tried_of_available", "shifts_to_try",
]


st.title("📞 Call-Tracking Monitor Dashboard")

try:
    subs, sample, cases, summary, meta = load_all()
except Exception as e:
    st.error(f"Could not load / roll up the data.\n\n{e}")
    st.stop()

# ---- ROSTER: the eligible sign-ups, and nothing else ------------------------
# The data_entry tab carries the WHOLE recruited roster: eligible sign-ups,
# refusals, and everyone screened out by an ineligibility determinant. Only
# `phone_sample_status = "Eligible - signed up"` may be called, so that pool is
# THE STARTING POINT for every figure this dashboard reports — KPIs, queues,
# per-enumerator effort, completion and attrition rates all denominate on it.
# Filtering here, immediately after the load, means nothing downstream can widen
# it back out. The PI dashboard is unaffected: it keeps the full roster.
_roster = eligible_roster(summary)
n_full = len(summary)
# A row the sheet calls signed-up while an ineligibility determinant also fires
# is a data-entry contradiction. Zero on the 2026-08 sheet; surfaced, not hidden.
n_conflict = int((_roster & (summary["eligible_to_call"] == 0)).sum())
summary_full = summary          # pre-filter frame, for the diagnostics panel
summary = summary[_roster].copy()
_roster_pids = set(summary["pid"])
n_offroster_cases = int((~cases["pid"].isin(_roster_pids)).sum())
cases = cases[cases["pid"].isin(_roster_pids)].copy()
subs = subs[subs["pid"].isin(_roster_pids)].copy() if "pid" in subs.columns else subs

# What each tab is for. Lives at the top of the Calls summary tab (below), not
# here — the header stays to the title, the data-source line and Refresh.
TAB_LEGEND = """
- **🧾 Calls summary** — indicators + what's active / inactive
- **✅ Active pids** — open pids to work, grouped by what to do
- **🗄️ Inactive pids** — closed pids
- **💾 Partial saves** — which tablet each held partial is sitting on
- **👤 By enumerator** — what's on each plate now, + calls made
- **📋 Tracking sheet** — one row per pid, full detail
"""

_, right = st.columns([4, 1])
if right.button("🔄 Refresh now"):
    st.cache_data.clear()
    st.rerun()

# Which tabs this view is actually built from, and when they were read. Without
# this there is no way to tell a stale cache from fresh data on screen.
# (the data-source and roster detail now live in the diagnostics panel at the
#  very bottom of the page — see "What this page is built from")

# --- Sidebar filters (apply to both tabs) -----------------------------------
# Both filters are keyed by pid: route and date_recruited live on the sample tab
# (the roster), not on the call attempts, so they narrow `cases` through the pid
# list rather than through a column of their own.
st.sidebar.header("Filters")


def parse_dates(s):
    """Parse a date column for filtering, tz-naive.

    The survey timestamps are ISO, but the roster's date_recruited arrives from
    the sheet as dd/mm/yyyy — which pandas reads month-first by default, so
    05/06/2026 would land in May. Day-first only when the column looks like it."""
    s = pd.Series(s).astype(str).str.strip()
    if s.str.match(r"^\d{1,2}/\d{1,2}/\d{4}").any():
        return pd.to_datetime(s, errors="coerce", dayfirst=True)
    return _to_dt(s)


def date_range_filter(label, dates, pids, help=None):
    """Sidebar date-range picker -> the set of pids inside the picked range.

    Returns None when the picker still spans the whole data, which means "don't
    filter" — so pids with no date at all (never recruited on a dated row, never
    assigned) stay visible until the range is actually narrowed."""
    dates = parse_dates(dates)
    valid = dates.dropna()
    if valid.empty:
        return None
    lo, hi = valid.min().date(), valid.max().date()
    if lo == hi:                                # a single day in the data: nothing to pick
        st.sidebar.caption(f"**{label}** — all on {lo}")
        return None
    got = st.sidebar.date_input(label, value=(lo, hi), min_value=lo, max_value=hi, help=help)
    if not isinstance(got, (tuple, list)) or len(got) != 2:
        return None                             # mid-pick: only the start date chosen yet
    a, b = got
    if a == lo and b == hi:
        return None                             # full span = no filter
    d = dates.dt.date
    return set(pids[(d >= a) & (d <= b)])


# route lives on the sample tab (roster) -> it's on the summary, not the attempts
route_col = route_column(summary)
route_options = (sorted(x for x in summary[route_col].dropna().unique() if str(x).strip())
                 if route_col else [])
picked_route = (st.sidebar.multiselect("Route", route_options, default=route_options)
                if route_options else [])

# when the respondent was recruited (roster date)
rec_pids = date_range_filter(
    "Recruitment date", summary.get("date_recruited", pd.Series(dtype=str)), summary["pid"],
    help="When the respondent was recruited (sample tab).")

summary_view = summary
# combine the pid-keyed filters (None = that filter is untouched). keep_pids stays
# available afterwards so the raw submissions can be narrowed the same way.
route_pids = None
if route_col and picked_route and len(picked_route) != len(route_options):
    route_pids = set(summary.loc[summary[route_col].isin(picked_route), "pid"])
keep_pids = None
for _sel in (route_pids, rec_pids):
    if _sel is not None:
        keep_pids = _sel if keep_pids is None else (keep_pids & _sel)
if keep_pids is not None:
    cases = cases[cases["pid"].isin(keep_pids)]
    summary_view = summary_view[summary_view["pid"].isin(keep_pids)]

# Tab ORDER is set here; the `with tab_*:` bodies further down can stay in any
# order, Streamlit renders each into its own tab regardless.
tab_summary, tab_action, tab_archive, tab_partial, tab_enum, tab_track = st.tabs(
    ["🧾 Calls summary", "✅ Active pids", "🗄️ Inactive pids",
     "💾 Partial saves", "👤 By enumerator", "📋 Tracking sheet"])

# ============================================================================
# TAB 1 — RESPONDENT SUMMARY (supervisor view: one row per respondent, whole sample)
# ============================================================================
with tab_summary:
    # same weight as "Indicators at a glance" below it
    st.subheader("Dashboard content")
    st.markdown(TAB_LEGEND)
    st.divider()

    sk = summary_kpis(summary_view)
    resp = sk["respondents"] or 1
    ea_pct = round(100 * sk["ever_attempted"] / resp)
    total_att = int(pd.to_numeric(summary_view["total_attempts"], errors="coerce").fillna(0).sum())
    avg_att = round(total_att / sk["ever_attempted"], 1) if sk["ever_attempted"] else 0
    # "contacted" = ever picked up. The Contact rate tile is removed for now, but
    # this count still denominates the second completion tile below.
    n_picked = int((summary_view["ever_picked_up"] == 1).sum())
    # TWO completion rates, shown as separate tiles because they answer different
    # questions: how far through the sample we are, vs. how well the interview
    # converts once someone actually answers the phone. Safe as a percentage —
    # completing requires answering, so every completed pid is also a contacted
    # one and the second rate cannot exceed 100%.
    done_of_contacted = round(100 * sk["completed"] / n_picked) if n_picked else 0

    # top indicators: title (bold) / description (metric label) / number (metric value)
    def _ind(col, title, desc, value, green=False):
        with col:
            st.markdown(f"<div style='margin-bottom:-0.8rem'><strong>{title}</strong></div>",
                        unsafe_allow_html=True)
            st.metric(desc, f":green[{value}]" if green else value)

    st.subheader("Indicators at a glance")
    m = st.columns(5)
    _ind(m[0], "Respondents", "in the sample", f"{sk['respondents']}")
    _ind(m[1], "Ever attempted", f"{sk['never_attempted']} not yet assigned",
         f"{ea_pct}%  ({sk['ever_attempted']})")
    _ind(m[2], "Completed — of sample",
         f"of all {sk['respondents']} respondents",
         f"{sk['completion_rate']}%  ({sk['completed']})", green=True)
    _ind(m[3], "Completed — of contacted",
         f"of the {n_picked} who picked up",
         f"{done_of_contacted}%  ({sk['completed']})", green=True)
    # Names its N like the other tiles do: the average is per ATTEMPTED pid, so
    # the denominator on show is the attempted count, not the whole sample.
    _ind(m[4], "Total attempts", f"across the {sk['ever_attempted']} attempted",
         f"{total_att}  ({avg_att}/pid)")

    # --- Monitoring status summary: what needs doing, at a glance ---------------
    st.divider()
    st.subheader("Monitoring status summary")
    ocb = open_with_buckets(cases, summary)
    cnt = ocb["action_bucket"].value_counts() if len(ocb) else pd.Series(dtype=int)
    n_followup, n_resume = int(cnt.get("Callback", 0)), int(cnt.get("Resume", 0))
    n_keep, n_review = int(cnt.get("Keep calling", 0)), int(cnt.get("Review", 0))
    n_assign = int(((summary_view["case_state"] == "Not assigned")
                    & (summary_view["eligible_to_call"] == 1)).sum())
    n_whatsapp = int(ocb["whatsapp"].sum()) if len(ocb) else 0   # hidden unless SHOW_WHATSAPP
    total = len(summary_view)
    actionable = n_followup + n_resume + n_keep + n_review + n_assign
    pct = round(100 * actionable / total) if total else 0
    st.markdown(f"### Active pids: {pct}%  ({actionable} of {total})")
    _mon_rows = [
        ("To be followed up", n_followup,
         "reschedules or drops — a callback time was set or the call dropped"),
        ("To be resumed", n_resume, "the survey was held mid-way to resume"),
        ("To be assigned", n_assign, "not attempted yet"),
        ("To keep calling", n_keep,
         "no answer / phone off, or wrong number / incorrect respondent with other numbers to try"),
        ("To be reviewed", n_review,
         "overdue callback, or ≥4/5 shifts tried OR >8 attempts and still no completion"),
    ]
    if SHOW_WHATSAPP:
        _mon_rows.append(("To be reached out on WhatsApp", n_whatsapp,
                          "PIDs attempted ≥3 times need to be messaged on WhatsApp "
                          "(overlaps the rows above)"))
    mon = pd.DataFrame(_mon_rows, columns=["action", "pids", "what it is"])
    st.dataframe(mon, width="stretch", hide_index=True)

    # Completed, then rung again. The rollup closes these (completion is
    # absorbing) so they no longer sit in the queues above — but the re-contact
    # already happened, and that is a coordination failure supervisors should see.
    _rc = summary_view[summary_view.get("recontacted_after_complete", 0) == 1]
    if len(_rc):
        st.warning(
            f"⚠️ **{len(_rc)} respondent(s) were called again AFTER completing the "
            "interview.** They are closed here and excluded from the queues above, "
            "but the calls were already made — check how a finished pid got back "
            "onto someone's list.")
        _rc_cols = [c for c in ["pid", "route_recruited", "enumerator",
                                "current_callcode", "total_attempts",
                                "last_submission_time"] if c in _rc.columns]
        st.dataframe(_rc[_rc_cols], width="stretch", hide_index=True)
        st.caption("`enumerator` and `current_callcode` are from the LATEST call — "
                   "i.e. whoever rang them after the interview was already done.")

    # --- Inactive pids: closed / completed, by reason (mirrors the Active table)-
    _inact_rows = summary_view[
        summary_view["current_status"].astype(str).str.upper() == "INACTIVE"]
    inactive_cc = _inact_rows["current_callcode"].astype(str).str.strip()
    # Completion is absorbing, so a pid that finished and was then rung again is
    # closed on is_complete even though its LAST callcode is a call outcome.
    # Keying the completed row on the callcode would lose exactly those rows.
    _is_done = _inact_rows["is_complete"] == 1
    _n_inact, _n_tot = len(inactive_cc), len(summary_view)
    _inact_pct = round(100 * _n_inact / _n_tot) if _n_tot else 0
    st.divider()
    st.markdown(f"### Inactive pids: {_inact_pct}%  ({_n_inact} of {_n_tot})")
    _inact = [
        ("1", "Completed the interview"),
        ("0_R", "Refusal (0_R)"),
        ("0_UN", "Ineligible (0_UN) — under 18 / outside area / no phone"),
        ("0_IN", "Incorrect respondent (0_IN) — wrong person, numbers exhausted"),
        ("0_WN", "Wrong number (0_WN) — numbers exhausted"),
    ]
    # completed first (on is_complete), then the closure codes among the rest.
    # _other is the REMAINDER, so the rows always add up to the inactive total.
    _pids = [int(_is_done.sum())]
    _pids += [int(((inactive_cc == code) & ~_is_done).sum()) for code, _ in _inact[1:]]
    _pids.append(max(_n_inact - sum(_pids), 0))
    _what = [d for _, d in _inact] + ["Other closure"]
    inactive_tbl = pd.DataFrame({
        "action": ["No action"] + [""] * (len(_what) - 1),
        "pids": _pids,
        "what it is": _what,
    })
    st.dataframe(inactive_tbl, width="stretch", hide_index=True)


# ============================================================================
# TAB — TRACKING SHEET (one row per respondent, full detail)
# ============================================================================
with tab_track:
    st.caption("One row per respondent (see context/tracking_sheet_codebook.md). "
               "**Current state** = most recent submission; **history** columns "
               "aggregate all submissions. Never-called respondents show empty current state.")
    tracking = summary_view.sort_values("last_submission_time", ascending=False)
    disp_cols = [c for c in SUMMARY_COLS if c in tracking.columns]
    disp = tracking[disp_cols].copy()
    # not-yet-assigned cases have no attempts — blank everything but identity so the
    # table isn't a wall of zeros.
    keep_unassigned = {"batch_code", "pid", "route_recruited", "date_recruited", "case_state"}
    blank_cols = [c for c in disp.columns if c not in keep_unassigned]
    disp[blank_cols] = disp[blank_cols].astype(object)
    disp.loc[tracking["ever_attempted"] == 0, blank_cols] = ""
    st.dataframe(yesno(disp), width="stretch", hide_index=True)

# ============================================================================
# TAB 2 — ACTION QUEUES (enumerator view: what to do now, per open case)
# ============================================================================
with tab_action:
    if cases.empty:
        st.warning("No pids match the current filters.")
    else:
        status = cases["status"].astype(str).str.upper()

        st.subheader("Work queues — open pids")
        st.markdown("Enumerators: review these pids and make sure **each one is addressed**.")
        SHIFT_LEGEND = ("Shift windows — A 07:30–09:30 · B 09:30–12:30 · C 12:30–15:30 · "
                        "D 15:30–17:30 · E 17:30–20:30.  `shifts_to_try` = untried windows, "
                        "or *All tried* / *None tried*.")

        GUIDANCE = {
            "Callback": "Prioritise the **most imminent** callbacks first.",
            "Keep calling": "Keep dialing (no-answer / phone-off), or try the other numbers "
                            "on file (wrong number / incorrect respondent). Watch shifts "
                            "tried and total attempts.",
            "Resume": "Keep trying to resume; if you can't, hand the pid to another enumerator.",
            "Review": "Escalations only — overdue callbacks, or pids tried across ≥4 shifts "
                      "OR >8 attempts and still no completion.",
        }

        # What puts a pid in each queue, printed under its subtitle. Built FROM the
        # rollup's own code sets, so the text can never drift from the actual rule.
        def _codes(s):
            return " · ".join(f"`{c}`" for c in sorted(s))

        DEFN = {
            "Callback": f"**Codes:** {_codes(CC_CALLBACK)} — a callback time was "
                        "agreed, or the call dropped and needs chasing.",
            "Keep calling": f"**Codes:** {_codes(CC_KEEP_CALLING)} — no answer / phone "
                            "off, or a wrong number / incorrect respondent with other "
                            "numbers still to try.",
            "Resume": f"**Codes:** {_codes(CC_RESUME)} — **or any open pid with "
                      "`was_partialsaved` ticked**, whatever its callcode.",
            "Review": "**Not code-based** — any open pid escalated here: an overdue "
                      f"callback (from {_codes(CC_CALLBACK)}), or ≥{SHIFTS_MOST} shifts "
                      f"tried OR >{ATTEMPTS_MAX} attempts with still no completion. Also "
                      "catches any open callcode that matches none of the queues above.",
        }

        open_cases = open_with_buckets(cases, summary)   # shared with the monitoring summary

        BUCKETS = [
            ("Callback", "⏰ To be followed up"),
            ("Keep calling", "📵 To keep calling"),
            ("Resume", "▶️ To be resumed"),
            ("Review", "🔎 To be reviewed"),
        ]
        COLS = {
            # History COUNTS, not ever-flags: for a callback the useful question is
            # how many times this has already happened, not merely whether it has.
            "Callback":     ["pid", "enumerator", "status", "callcode",
                             "total_attempts", "days_open", "last_contact_date",
                             "callback_when", "callback_due", "callback_by",
                             "times_rescheduled", "times_dropped", "times_noanswer",
                             "times_off", "was_partialsaved", "shifts_to_try",
                             "rsd_reason", "last_comment"],
            "Keep calling": ["pid", "enumerator", "status", "callcode",
                             "total_attempts", "days_open", "last_contact_date",
                             "shifts_to_try", "numbers_tried_of_available",
                             "last_comment"],
            # last_section is the readable label, not last_section_n. tab_id comes
            # from tab_id or rs_tab_id, whichever the submission carried.
            "Resume":       ["pid", "enumerator", "status", "callcode",
                             "total_attempts", "days_open", "last_contact_date",
                             "last_section", "tab_id", "was_partialsaved",
                             "times_rescheduled", "times_dropped", "times_noanswer",
                             "times_off", "rsd_reason", "shifts_to_try", "last_comment"],
            "Review":       ["pid", "enumerator", "status", "callcode",
                             "total_attempts", "days_open", "last_contact_date",
                             "review_reason", "callback_when", "was_partialsaved",
                             "shifts_to_try", "numbers_tried_of_available",
                             "rsd_reason", "last_comment"],
        }

        # Open cases laid out down the page, one section per queue.
        for key, label in BUCKETS:
            rows = open_cases[open_cases["action_bucket"] == key].copy()
            if key == "Callback":
                rows = rows.sort_values(["callback_overdue", "callback_due"],
                                        ascending=[False, True])
            elif key == "Keep calling":
                # least-worked first: fewest shift windows tried, then fewest attempts
                rows = rows.sort_values(["shifts_covered_n", "total_attempts"],
                                        ascending=[True, True])
            else:
                rows = rows.sort_values("_active_first", ascending=False)
            rows = rows[[c for c in COLS[key] if c in rows.columns]]
            st.markdown(f"#### {label} ({len(rows)})")
            if GUIDANCE.get(key):
                st.caption(GUIDANCE[key])
            if DEFN.get(key):
                st.caption(DEFN[key])
            if rows.empty:
                st.caption("Nothing in this queue right now.")
            else:
                st.dataframe(yesno(rows), use_container_width=True, hide_index=True)
                st.caption(SHIFT_LEGEND)

        # non-exclusive overlay: WhatsApp outreach (these pids also appear above)
        if SHOW_WHATSAPP:
            wa = open_cases[open_cases["whatsapp"] == 1].copy()
            st.markdown(f"#### 📱 To be reached out on WhatsApp ({len(wa)})")
            st.caption("PIDs attempted **≥3 times** need to be messaged on WhatsApp. "
                       "**Overlaps** the queues above; these pids still appear in their "
                       "primary queue too.")
            if wa.empty:
                st.caption("Nothing to WhatsApp right now.")
            else:
                wa_cols = ["pid", "enumerator", "status", "callcode", "action_bucket",
                           "total_attempts", "shifts_to_try", "numbers_tried_of_available",
                           "last_comment"]
                st.dataframe(yesno(wa[[c for c in wa_cols if c in wa.columns]]),
                             use_container_width=True, hide_index=True)

        # Not yet assigned — recruited but never worked. NOTE: the sample tab has no
        # explicit "assigned" column, so "no submission yet" is the proxy for
        # unassigned.
        # case_state, not ever_attempted: a pid a supervisor closed with no call
        # is Archived, and must not reappear here as work to hand out.
        new_cases = summary[(summary["case_state"] == "Not assigned")
                            & (summary["eligible_to_call"] == 1)].copy()
        if keep_pids is not None:
            new_cases = new_cases[new_cases["pid"].isin(keep_pids)]
        st.markdown(f"#### 🆕 To be assigned ({len(new_cases)})")
        st.caption("Eligible sign-ups not yet worked — top priority to assign first, across the day.")
        if new_cases.empty:
            st.caption("Every recruited respondent has been worked at least once.")
        else:
            # Both roster vintages listed: the 2026-08 data-entry names first,
            # then the earlier ones. Only the columns actually on the sheet show.
            newcols = [c for c in ["pid", "route_recruited", "date_recruited",
                                   "number_of_phone_numbers", "phone_sample_status",
                                   "own_phone", "phones_provided", "rec_signup"]
                       if c in new_cases.columns]
            st.dataframe(yesno(new_cases[newcols]), use_container_width=True, hide_index=True)
            st.caption("Recruited respondents not yet worked — the pool to assign.")

    with st.expander("See raw submissions (one row per call attempt)"):
        st.dataframe(subs, use_container_width=True, hide_index=True)

# ============================================================================
# TAB 3 — BY ENUMERATOR (what's on each plate now + historic effort)
# ============================================================================
with tab_enum:
    st.subheader("👤 By enumerator")
    if cases.empty:
        st.info("No pids match the current filters.")
    else:
        # ---- A. What's on the plate right now --------------------------------
        # ACTIVE pids the enumerator currently HOLDS (they made the last
        # submission). ACTIVE = callcode 3_* or 4_* (see rollup.status_for), and
        # the only codes in those families are 3_SC/3_D and 4_SC/4_D — so the two
        # columns partition the total exactly.
        st.markdown("#### 📋 On the plate now — ACTIVE pids held")
        st.caption("ACTIVE pids each enumerator **currently holds** (they made the last "
                   "submission on it). The two columns **add up to the total**.")
        st.caption("**▶️ resume (4_)** — a logged notification: `4_D` or `4_SC` held partial "
                   "(partial save on the enumerator's device)  ·  **⏰ follow-up** (resc/drop "
                   "within next hour or deep in the survey).")
        st.caption("**🕒 callback times** — when that enumerator's callbacks fall due, "
                   "earliest first (**⚠ = overdue**; the time alone means today). Held "
                   "partials and drops carry no scheduled time, so they show nothing here.")
        act = cases[cases["status"].astype(str).str.upper() == "ACTIVE"].copy()
        act = act[act["enumerator"].astype(str).str.strip() != ""]
        if act.empty:
            st.info("No ACTIVE pids under the current filters.")
        else:
            cc = act["callcode"].astype(str).str.strip()
            act["_rs"] = cc.str.startswith("4_").astype(int)          # held partial / logged
            act["_hd"] = cc.isin(["3_SC", "3_D"]).astype(int)         # imminent or Section D+

            # When each enumerator's callbacks fall due. Only the SC codes carry a
            # scheduled time — a held partial / drop (D) has none, so it adds nothing
            # here. Reference "now" = latest activity in the data, as in rollup().
            now_ref = pd.to_datetime(summary["last_submission_time"], errors="coerce").max()
            act["_cb_dt"] = pd.to_datetime(act["callback_due"], errors="coerce")

            def _cb_label(dt, when):
                # same-day callbacks show the time alone; anything else keeps its date
                if pd.isna(dt):
                    return ""
                same_day = pd.notna(now_ref) and dt.date() == now_ref.date()
                stamp = dt.strftime("%H:%M") if same_day else dt.strftime("%d %b %H:%M")
                return f"⚠ {stamp}" if str(when).strip() == "overdue" else stamp

            act["_cb_lab"] = [_cb_label(d, w) for d, w
                              in zip(act["_cb_dt"], act["callback_when"])]
            # Sorted earliest-first, so the imminent (and overdue) ones lead. Only
            # the first few are spelled out — a long list would be truncated by the
            # column and hide the very entries that matter; the rest are counted,
            # and the full list is the Callback queue on the Active pids tab.
            CB_SHOWN = 3

            def _join_times(labels):
                labels = list(labels)
                shown = "  ·  ".join(labels[:CB_SHOWN])
                extra = len(labels) - CB_SHOWN
                return f"{shown}  (+{extra} more)" if extra > 0 else shown

            times = (act.dropna(subset=["_cb_dt"]).sort_values("_cb_dt")
                        .groupby("enumerator")["_cb_lab"].agg(_join_times))

            plate = act.groupby("enumerator", as_index=False).agg(
                active=("pid", "count"), rs=("_rs", "sum"), hd=("_hd", "sum"),
            )
            plate["cbt"] = plate["enumerator"].map(times).fillna("")
            plate = plate.sort_values("active", ascending=False)
            plate.loc[len(plate)] = (["TOTAL"]
                                     + [int(plate[c].sum()) for c in ["active", "rs", "hd"]]
                                     + [""])
            plate = plate.rename(columns={
                "active": "ACTIVE pids", "rs": "▶️ resume (4_)",
                "hd": "⏰ follow-up (3_SC / 3_D)", "cbt": "🕒 callback times"})
            st.dataframe(plate, width="stretch", hide_index=True)

        # ---- B. Historic effort ----------------------------------------------
        # Counted from the submissions, NOT the case rows: the case-level
        # enumerator is only the latest one, so a handed-over pid would credit its
        # whole history to whoever touched it last.
        st.markdown("#### 📈 Historic effort — calls actually made")
        eff = effort_by_enumerator(subs, pids=keep_pids)
        if eff.empty:
            st.info("No attempts recorded under the current filters.")
        else:
            eff = eff.sort_values("attempts", ascending=False)
            eff = eff.rename(columns={"pids_tried": "pids tried"})
            st.dataframe(eff, width="stretch", hide_index=True)
            st.caption("Credited to whoever **made the call**, across the whole history "
                       "(not just pids they hold now). **pids tried** deliberately "
                       "**overlaps between enumerators** — several people try the same "
                       "respondent, so this column does not sum to the sample.")

# ============================================================================
# TAB — PARTIAL SAVES (which tablet is holding each half-finished interview)
# ----------------------------------------------------------------------------
# A partial save lives on the DEVICE it was taken on, so it can only be resumed
# from that tablet. This tab answers the operational question: how many are out
# there, which tablet is each sitting on, and who last touched it.
# ============================================================================
with tab_partial:
    st.subheader("💾 Partial saves")
    st.caption("A partial save is stored **on the tablet it was taken on** — it can "
               "only be resumed from that device. Use this to find which tablet is "
               "holding each half-finished interview, and who to ask for it.")

    NO_TAB = "⚠️ no tablet id recorded"
    ps = summary_view[summary_view.get("was_partialsaved", 0) == 1].copy()
    if "tab_id" in ps.columns:
        ps["tab_id"] = ps["tab_id"].astype(str).str.strip().replace("", NO_TAB)
    else:
        ps["tab_id"] = NO_TAB

    if ps.empty:
        st.info("**No partial saves recorded.**")
        _missing = [c for c in ("was_partialsaved", "tab_id")
                    if c not in summary_view.columns
                    or summary_view[c].astype(str).str.strip().eq("").all()
                    or (summary_view.get(c, pd.Series(dtype=str)) == 0).all()]
        if _missing:
            st.caption("Note: " + ", ".join(f"`{c}`" for c in _missing) +
                       " is present in the export but not yet populated, so this tab "
                       "stays empty until the form starts writing it. Nothing to fix "
                       "on the dashboard side.")
    else:
        n_pids = len(ps)
        n_tabs = int(ps["tab_id"].nunique())
        n_lost = int((ps["tab_id"] == NO_TAB).sum())
        k = st.columns(3)
        k[0].metric("Partial saves", f"{n_pids}")
        k[1].metric("Tablets holding them", f"{n_tabs}")
        k[2].metric("No tablet id", f"{n_lost}",
                    help="Cannot be traced to a device — the tablet id was not recorded.")
        if n_lost:
            st.warning(f"⚠️ **{n_lost} partial save(s) carry no tablet id**, so there is "
                       "no way to tell which device holds them. They still need chasing — "
                       "start from the enumerator who last submitted.")

        # ---- by tablet -------------------------------------------------------
        st.markdown("#### 📱 By tablet")
        by_tab = (ps.groupby("tab_id")
                    .agg(pids=("pid", "nunique"),
                         enumerators=("enumerator", lambda s: ", ".join(
                             sorted({x for x in s.astype(str).str.strip() if x}))),
                         last_submission=("last_submission_time", "max"))
                    .reset_index()
                    .sort_values("pids", ascending=False)
                    .rename(columns={"tab_id": "tablet"}))
        by_tab["which pids"] = by_tab["tablet"].map(
            ps.groupby("tab_id")["pid"].apply(lambda s: ", ".join(sorted(s))))
        st.dataframe(by_tab, width="stretch", hide_index=True)
        st.caption("**enumerators** = everyone who last submitted on a pid held by that "
                   "tablet. **last_submission** = the most recent submission across them.")

        # ---- one row per partial save ---------------------------------------
        st.markdown("#### 📄 Every partial save")
        pcols = [c for c in ["pid", "tab_id", "enumerator", "last_submission_time",
                             "current_status", "current_callcode", "action_bucket",
                             "last_section", "total_attempts", "days_open",
                             "route_recruited", "last_comment"]
                 if c in ps.columns]
        st.dataframe(ps[pcols].sort_values(["tab_id", "last_submission_time"],
                                           ascending=[True, False]),
                     width="stretch", hide_index=True)
        st.caption("**enumerator** and **last_submission_time** are the LAST submission "
                   "on that pid — who touched it most recently, and when. That is not "
                   "necessarily whoever took the partial save.")


# ============================================================================
# TAB 3 — ARCHIVED (closed / inactive cases whose callcode starts with 0_)
# ============================================================================
with tab_archive:
    arch = cases[cases["status"].astype(str).str.upper() == "INACTIVE"].copy()
    # (ineligible_type1 already comes from cases/rollup — don't re-merge it)
    # last_comment = final_comment, replaced by the supervisor's comment_sp when
    # a supervisor closed the case — so no separate comment_sp column is needed.
    # route lives on the sample tab; drop the blank one from cases before merging.
    if route_col and route_col in arch.columns:
        arch = arch.drop(columns=[route_col])
    scols = ["pid", "total_attempts", "last_comment", "is_supervisor", "refuse_why",
             "numbers_wrong", "numbers_tried_of_available", "is_incorrect",
             "is_othercontact", "was_partialsaved"]
    if route_col and route_col in summary.columns:
        scols.insert(1, route_col)
    arch = arch.merge(summary[scols], on="pid", how="left")
    st.subheader(f"🗄️ Inactive pids ({len(arch)})")
    st.caption("**Inactive pids** — completed interviews (callcode `1`) and closed pids "
               "(callcode starts with `0_`): 0_R refusal · 0_UN ineligible · 0_IN incorrect "
               "respondent · 0_WN wrong number.")
    st.caption("**`ineligible_type1`** = respondent is ineligible — under 18 (`d02_check`), "
               "lives outside the Western Area (`d04_yn`), or had no phone at recruitment. "
               "**`is_supervisor`** = Yes when a supervisor closed the pid.")
    st.warning("⚠️ **Supervisors:** review this list regularly and make sure these closed "
               "pids are pulled out of the active calling pile as the days go by — so "
               "respondents who shouldn't be contacted don't keep getting re-attempted.")
    if arch.empty:
        st.info("No inactive pids yet.")
    else:
        base = ["pid", "enumerator", "callcode", "attrition_reason", "ineligible_type1",
                "is_incorrect", "is_othercontact", "refuse_why", "numbers_wrong",
                "numbers_tried_of_available", "is_supervisor", "was_partialsaved",
                "total_attempts", "last_contact_date", "last_comment"]
        if route_col:
            base.insert(2, route_col)
        acols = [c for c in base if c in arch.columns]
        st.dataframe(yesno(arch[acols]), use_container_width=True, hide_index=True)

# ============================================================================
# FOOTER — what this page is built from
# ----------------------------------------------------------------------------
# Sits below the tabs, so it shows whichever tab is open. Everything needed to
# answer "is the dashboard looking at the right data, and is it running the
# right code?" without opening a terminal.
# ============================================================================
st.divider()
with st.expander("🔧 What this page is built from — sources, filters, checks",
                 expanded=True):
    _a, _s = meta["attempts"], meta["sample"]

    st.markdown("**Reading**")
    st.dataframe(pd.DataFrame([
        {"what": "Call attempts", "origin": _a["origin"], "tab": _a["tab"],
         "gid": _a["gid"], "rows": _a["rows"], "read at": _a["read"]},
        {"what": "Roster (data entry)", "origin": _s["origin"], "tab": _s["tab"],
         "gid": _s["gid"], "rows": _s["rows"], "read at": _s["read"]},
    ]), width="stretch", hide_index=True)
    st.caption(f"**Rollup logic {meta['code_stamp']}** — when the calculation code this "
               f"process is running was last edited. Sheet data auto-refreshes every "
               f"{REFRESH_SECONDS // 60} min (**🔄 Refresh now** re-reads immediately), "
               "but picking up changed *code* needs a full restart of this window.")

    st.markdown("**Roster filter** — who this dashboard reports on")
    # Split by the sheet's own phone_sample_status rather than lumping the two
    # excluded groups together: "refused" and "ineligible" are different things
    # and the combined line hid which was which.
    _status = summary_full["phone_sample_status"].astype(str).str.strip()         if "phone_sample_status" in summary_full.columns else pd.Series(dtype=str)
    _n_ref = int((_status == "Eligible - refused").sum())
    _n_inel = int((_status == "Not eligible").sum())
    _n_other = n_full - len(summary) - _n_ref - _n_inel
    _rows = [
        {"phone_sample_status": "Eligible - signed up", "pids": len(summary),
         "in the dashboard?": "✅ yes — this is the roster"},
        {"phone_sample_status": "Eligible - refused", "pids": _n_ref,
         "in the dashboard?": "❌ no — refused at recruitment"},
        {"phone_sample_status": "Not eligible", "pids": _n_inel,
         "in the dashboard?": "❌ no — INELIGIBLE, failed a screening determinant"},
    ]
    if _n_other:
        _rows.append({"phone_sample_status": "(unrecognised status)", "pids": _n_other,
                      "in the dashboard?": "❌ no — ⚠️ check the data entry tab"})
    _rows.append({"phone_sample_status": "TOTAL recruited", "pids": n_full,
                  "in the dashboard?": ""})
    st.dataframe(pd.DataFrame(_rows), width="stretch", hide_index=True)
    st.caption("Every figure on this page is denominated on the **Eligible - signed up** "
               "pool. Refusals and ineligibles cannot be called, so they are excluded — "
               "they are counted here so the three add back up to everyone recruited.")

    if _n_inel:
        st.markdown(f"**Why those {_n_inel} are ineligible** — determinants on the data "
                    "entry tab. They **overlap**, so they do not sum to the total.")
        _why = recruit_exclusions(
            summary_full[_status == "Not eligible"], stage=1).sum()
        _why = _why[_why > 0].sort_values(ascending=False)
        st.dataframe(pd.DataFrame({"determinant": _why.index, "pids": _why.values}),
                     width="stretch", hide_index=True)

    st.markdown("**Rows excluded from every count**")
    _strays = int(pd.to_numeric(summary.get("calls_after_complete", 0),
                                errors="coerce").fillna(0).sum())
    st.dataframe(pd.DataFrame([
        {"excluded": "Test submissions (enum_name = Testing)", "rows": _a["dropped_test"]},
        {"excluded": "Calls logged after the pid had completed", "rows": _strays},
        {"excluded": "Attempts on pids with no roster row", "rows": n_offroster_cases},
    ]), width="stretch", hide_index=True)

    st.markdown("**Checks**")
    _rc = int(pd.to_numeric(summary.get("recontacted_after_complete", 0),
                            errors="coerce").fillna(0).sum())
    _checks = [
        {"check": "Signed-up rows that also carry an ineligibility flag",
         "count": n_conflict, "verdict": "OK" if not n_conflict else "⚠️ check data entry"},
        {"check": "Completed, then called again anyway",
         "count": _rc, "verdict": "OK" if not _rc else "⚠️ see Calls summary"},
        {"check": "Pids with attempts but no roster row",
         "count": n_offroster_cases, "verdict": "OK" if not n_offroster_cases else "older sample"},
    ]
    st.dataframe(pd.DataFrame(_checks), width="stretch", hide_index=True)

    st.caption(f"In view now: **{len(summary_view)} pids**, **{len(cases)} cases**, "
               f"**{len(subs)} attempt rows** (after any sidebar filters). "
               "Run `CHECK.bat` for the full validation suite.")
