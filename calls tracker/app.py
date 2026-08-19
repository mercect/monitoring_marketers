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
from data_io import load_all, route_column
from rollup import OPEN_STATUSES, _to_dt, effort_by_enumerator, summary_kpis

st.set_page_config(page_title="Call-Tracking Monitor Dashboard", page_icon="📞", layout="wide")

# Password gate — see auth.py. Must come straight after set_page_config, before
# anything is drawn, so no data leaks onto the page for a signed-out visitor.
require_password("🔒 Call-Tracking Monitor")

# 0/1 flag columns shown as No/Yes on screen (the CSV download stays numeric 0/1).
FLAG_COLS = {
    "ever_called", "ever_attempted", "ever_picked_up", "ever_rescheduled", "ever_dropped",
    "is_complete", "last_is_answered", "last_is_incorrect", "ineligible_type1",
    "ineligible_type2", "is_open", "attrited_a", "attrited_b", "all_shifts_covered",
    "callback_overdue", "no_phone", "whatsapp_any", "is_supervisor",
    "is_incorrect", "is_othercontact", "eligible_to_call",
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


def open_with_buckets(cases, summary):
    """Open pids with effort columns merged and the Review escalation applied.
    Single source of truth for the action queues AND the monitoring summary."""
    su = cases["status"].astype(str).str.upper()
    oc = cases[su.isin({s.upper() for s in OPEN_STATUSES})].copy()
    oc["_active_first"] = (oc["status"].str.upper() == "ACTIVE").astype(int)
    eff = summary[["pid", "total_attempts", "shifts_covered_n", "shifts_to_try",
                   "numbers_tried_of_available", "ever_rescheduled", "ever_dropped",
                   "last_comment"]]
    oc = oc.merge(eff, on="pid", how="left")
    oc["shifts_tried"] = (oc["shifts_covered_n"].fillna(0).astype(int).astype(str) + "/5")
    ta = oc["total_attempts"].fillna(0)
    sc = oc["shifts_covered_n"].fillna(0)
    esc_overdue = (oc["action_bucket"] == "Callback") & (oc["callback_when"] == "overdue")
    esc_worked = ((oc["action_bucket"] == "Keep calling") & (sc >= SHIFTS_MOST) & (ta > ATTEMPTS_MAX))
    oc["review_reason"] = ""
    oc.loc[esc_overdue, "review_reason"] = "overdue callback"
    oc.loc[esc_worked, "review_reason"] = f"{SHIFTS_MOST}+/5 shifts, >{ATTEMPTS_MAX} attempts, no contact"
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
    subs, sample, cases, summary, source, sample_source = load_all()
except Exception as e:
    st.error(f"Could not load / roll up the data.\n\n{e}")
    st.stop()

left, right = st.columns([4, 1])
left.markdown(
    "**🧾 Calls summary** — indicators + what's active / inactive  ·  "
    "**📋 Tracking sheet** — one row per pid, full detail  ·  "
    "**👤 By enumerator** — what's on each plate now, + calls made  ·  "
    "**✅ Active pids** — open pids to work, grouped by what to do  ·  "
    "**🗄️ Inactive pids** — closed pids"
)
if right.button("🔄 Refresh now"):
    st.cache_data.clear()
    st.rerun()

# --- Sidebar filters (apply to both tabs) -----------------------------------
# All three filters are keyed by pid: route and both dates live on the sample tab
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

# when the respondent was recruited (roster date) and when the case first went out
# to be called — the first attempt is what flips a pid off "Not assigned".
rec_pids = date_range_filter(
    "Recruitment date", summary.get("date_recruited", pd.Series(dtype=str)), summary["pid"],
    help="When the respondent was recruited (sample tab).")
asg_pids = date_range_filter(
    "Assigned date", summary.get("first_attempt_time", pd.Series(dtype=str)), summary["pid"],
    help="When the pid was first called, i.e. when it entered the call workload. "
         "Never-assigned pids drop out once this range is narrowed.")

summary_view = summary
# combine the pid-keyed filters (None = that filter is untouched). keep_pids stays
# available afterwards so the raw submissions can be narrowed the same way.
route_pids = None
if route_col and picked_route and len(picked_route) != len(route_options):
    route_pids = set(summary.loc[summary[route_col].isin(picked_route), "pid"])
keep_pids = None
for _sel in (route_pids, rec_pids, asg_pids):
    if _sel is not None:
        keep_pids = _sel if keep_pids is None else (keep_pids & _sel)
if keep_pids is not None:
    cases = cases[cases["pid"].isin(keep_pids)]
    summary_view = summary_view[summary_view["pid"].isin(keep_pids)]

tab_summary, tab_track, tab_enum, tab_action, tab_archive = st.tabs(
    ["🧾 Calls summary", "📋 Tracking sheet", "👤 By enumerator",
     "✅ Active pids", "🗄️ Inactive pids"])

# ============================================================================
# TAB 1 — RESPONDENT SUMMARY (supervisor view: one row per respondent, whole sample)
# ============================================================================
with tab_summary:
    sk = summary_kpis(summary_view)
    resp = sk["respondents"] or 1
    ec_pct = round(100 * sk["ever_called"] / resp)
    total_att = int(pd.to_numeric(summary_view["total_attempts"], errors="coerce").fillna(0).sum())
    avg_att = round(total_att / sk["ever_called"], 1) if sk["ever_called"] else 0
    contact = round(100 * (summary_view["ever_picked_up"] == 1).mean()) if len(summary_view) else 0

    # top indicators: title (bold) / description (metric label) / number (metric value)
    def _ind(col, title, desc, value, green=False):
        with col:
            st.markdown(f"<div style='margin-bottom:-0.8rem'><strong>{title}</strong></div>",
                        unsafe_allow_html=True)
            st.metric(desc, f":green[{value}]" if green else value)

    st.subheader("Indicators at a glance")
    m = st.columns(5)
    _ind(m[0], "Respondents", "in the sample", f"{sk['respondents']}")
    _ind(m[1], "Ever called", f"{sk['never_called']} not yet assigned",
         f"{ec_pct}%  ({sk['ever_called']})")
    _ind(m[2], "Completed", "share of interviewed among all respondents",
         f"{sk['completion_rate']}%  ({sk['completed']})", green=True)
    _ind(m[3], "Contact rate", "share ever picked up", f"{contact}%")
    _ind(m[4], "Total attempts", "total dials · avg per called pid",
         f"{total_att}  ({avg_att}/pid)")

    # --- Monitoring status summary: what needs doing, at a glance ---------------
    st.divider()
    st.subheader("Monitoring status summary")
    ocb = open_with_buckets(cases, summary)
    cnt = ocb["action_bucket"].value_counts() if len(ocb) else pd.Series(dtype=int)
    n_followup, n_resume = int(cnt.get("Callback", 0)), int(cnt.get("Resume", 0))
    n_keep, n_review = int(cnt.get("Keep calling", 0)), int(cnt.get("Review", 0))
    n_assign = int(((summary_view["ever_called"] == 0) & (summary_view["eligible_to_call"] == 1)).sum())
    n_whatsapp = int(ocb["whatsapp"].sum()) if len(ocb) else 0
    total = len(summary_view)
    actionable = n_followup + n_resume + n_keep + n_review + n_assign
    pct = round(100 * actionable / total) if total else 0
    st.markdown(f"### Active pids: {pct}%  ({actionable} of {total})")
    mon = pd.DataFrame({
        "action": ["To be followed up", "To be resumed", "To be assigned",
                   "To keep calling", "To be reviewed", "To be reached out on WhatsApp"],
        "pids": [n_followup, n_resume, n_assign, n_keep, n_review, n_whatsapp],
        "what it is": [
            "reschedules or drops — a callback time was set or the call dropped",
            "the survey was held mid-way to resume",
            "not attempted yet",
            "no answer / phone off, or wrong number / incorrect respondent with other numbers to try",
            "overdue callback, or ≥4/5 shifts tried with >8 attempts and no contact",
            "PIDs attempted ≥3 times need to be messaged on WhatsApp (overlaps the rows above)",
        ],
    })
    st.dataframe(mon, width="stretch", hide_index=True)

    # --- Inactive pids: closed / completed, by reason (mirrors the Active table)-
    inactive_cc = summary_view.loc[
        summary_view["current_status"].astype(str).str.upper() == "INACTIVE",
        "current_callcode"].astype(str).str.strip()
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
    _pids = [int((inactive_cc == code).sum()) for code, _ in _inact]
    _other = int((inactive_cc.str.startswith("0_") & ~inactive_cc.isin([c for c, _ in _inact])).sum())
    _pids.append(_other)
    _what = [d for _, d in _inact] + ["Other 0_ closure"]
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
    disp.loc[tracking["ever_called"] == 0, blank_cols] = ""
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
                      "with >8 attempts and still no contact.",
        }

        open_cases = open_with_buckets(cases, summary)   # shared with the monitoring summary

        BUCKETS = [
            ("Callback", "⏰ To be followed up"),
            ("Keep calling", "📵 To keep calling"),
            ("Resume", "▶️ To be resumed"),
            ("Review", "🔎 To be reviewed"),
        ]
        COLS = {
            "Callback":     ["pid", "enumerator", "status", "callcode", "callback_when",
                             "callback_due", "callback_by", "ever_picked_up",
                             "ever_rescheduled", "ever_dropped", "total_attempts",
                             "shifts_to_try", "rsd_reason", "last_comment"],
            "Keep calling": ["pid", "enumerator", "status", "callcode", "total_attempts",
                             "shifts_to_try", "numbers_tried_of_available", "days_open",
                             "last_contact_date", "last_comment"],
            "Resume":       ["pid", "enumerator", "status", "callcode", "last_section_n",
                             "rsd_reason", "total_attempts", "shifts_to_try", "days_open",
                             "last_contact_date", "last_comment"],
            "Review":       ["pid", "enumerator", "status", "callcode", "review_reason",
                             "callback_when", "total_attempts", "shifts_to_try",
                             "numbers_tried_of_available", "days_open", "rsd_reason",
                             "last_comment"],
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
            if rows.empty:
                st.caption("Nothing in this queue right now.")
            else:
                st.dataframe(yesno(rows), use_container_width=True, hide_index=True)
                st.caption(SHIFT_LEGEND)

        # non-exclusive overlay: WhatsApp outreach (these pids also appear above)
        wa = open_cases[open_cases["whatsapp"] == 1].copy()
        st.markdown(f"#### 📱 To be reached out on WhatsApp ({len(wa)})")
        st.caption("PIDs attempted **≥3 times** need to be messaged on WhatsApp. **Overlaps** "
                   "the queues above; these pids still appear in their primary queue too.")
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
        # unassigned — and that first submission is also the Assigned date filter's
        # key, so narrowing that range empties this pool (nothing here has one).
        new_cases = summary[(summary["ever_called"] == 0) & (summary["eligible_to_call"] == 1)].copy()
        if keep_pids is not None:
            new_cases = new_cases[new_cases["pid"].isin(keep_pids)]
        st.markdown(f"#### 🆕 To be assigned ({len(new_cases)})")
        st.caption("Eligible sign-ups not yet worked — top priority to assign first, across the day.")
        if new_cases.empty:
            st.caption("Every recruited respondent has been worked at least once.")
        else:
            newcols = [c for c in ["pid", "route_recruited", "date_recruited", "own_phone",
                                   "phones_provided", "rec_signup"]
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
             "numbers_wrong", "numbers_tried_of_available", "is_incorrect", "is_othercontact"]
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
                "numbers_tried_of_available", "is_supervisor", "total_attempts",
                "last_contact_date", "last_comment"]
        if route_col:
            base.insert(2, route_col)
        acols = [c for c in base if c in arch.columns]
        st.dataframe(yesno(arch[acols]), use_container_width=True, hide_index=True)
