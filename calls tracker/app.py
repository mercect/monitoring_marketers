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
import pathlib

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from auth import require_password
from data_io import (load_all, route_column, newest_batch, parse_dates,
                     REFRESH_SECONDS, code_stamp, EXPECTED_SAMPLE_TAB)
from rollup import (OPEN_STATUSES, effort_by_enumerator, summary_kpis,
                    eligible_roster, recruit_exclusions,
                    CC_CALLBACK, CC_KEEP_CALLING, CC_RESUME,
                    is_attempt_row, is_contact_row, _to_dt)
import review
import review_tab

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
    "recontacted_after_close",
    "active_partialsave",
}


def yesno(df):
    """Display copy: map known 0/1 flag columns to No/Yes (leaves everything else)."""
    out = df.copy()
    for c in set(out.columns) & FLAG_COLS:
        s = out[c].astype(str).str.strip()
        out[c] = s.map({"1": "Yes", "1.0": "Yes", "0": "No", "0.0": "No"}).fillna(s)
    return out


# Overdue callbacks are RED rather than filtered out. They stay in "To be
# followed up" with every other callback — an overdue promise is the most owed
# call on the list, not one to hide, and it is never escalated away to Review
# (see open_with_buckets) — so the only thing left to do is make it impossible
# to miss while scanning.
OVERDUE_CSS = "color: #e03131; font-weight: 600"


def style_overdue(df, when_col="callback_when", mask=None):
    """Red, bold text on every row whose callback is overdue.

    Returns a Styler (st.dataframe renders one directly), or the frame untouched
    when there is nothing to mark — a queue that carries no callback timing, or
    an empty one. Written with axis=None so the mask is applied by LABEL: the
    callback queue is sorted before it gets here, so a positional lookup would
    colour the wrong rows."""
    if df.empty:
        return df
    if mask is not None:
        # explicit rows to paint (the follow-up queue, which shows no overdue column)
        overdue = mask.reindex(df.index, fill_value=False).astype(bool)
    elif when_col in df.columns:
        overdue = df[when_col].astype(str).str.strip().str.lower().eq("overdue")
    else:
        return df
    if not overdue.any():
        return df
    styles = pd.DataFrame("", index=df.index, columns=df.columns)
    styles.loc[overdue, :] = OVERDUE_CSS
    return df.style.apply(lambda _: styles, axis=None)


RED, ORANGE, GREY = "#e03131", "#f08c00", "#868e96"
BLUE, GREEN = "#1c7ed6", "#2f9e44"


def flag_tiles(items):
    """A row of big count tiles: [(count, label, colour), ...], or with a 4th
    item, a small "where to go" line under the label. A tile with a count of 0
    turns grey, so only what needs attention stands out. Plain HTML with a
    translucent fill, so it reads in both the light and dark theme."""
    tiles = []
    for item in items:
        n, label, colour = item[:3]
        hint = item[3] if len(item) > 3 else ""
        c = colour if n else GREY
        tiles.append(
            f'<div style="border:1px solid {c};border-left:6px solid {c};'
            f'border-radius:8px;padding:8px 18px;min-width:170px;background:{c}1A">'
            f'<div style="font-size:1.9rem;font-weight:700;line-height:1.15;color:{c}">{n}</div>'
            f'<div style="font-size:0.95rem;opacity:.85">{label}</div>'
            + (f'<div style="font-size:0.78rem;opacity:.6;margin-top:2px">{hint}</div>'
               if hint else "") + '</div>')
    st.markdown('<div style="display:flex;gap:12px;flex-wrap:wrap;margin:2px 0 12px">'
                + "".join(tiles) + "</div>", unsafe_allow_html=True)


def queue_flags(key, rows, red=None):
    """The flag tiles above each work queue: the one or two counts that say
    what needs attention first. `rows` is the queue before columns are trimmed."""
    def num(col):
        return pd.to_numeric(rows.get(col, pd.Series(0, index=rows.index)),
                             errors="coerce").fillna(0)
    if key == "Callback":
        when = rows.get("callback_when", pd.Series("", index=rows.index)).astype(str)
        return [(int(red.sum()) if red is not None else 0, "overdue", RED),
                (int(when.eq("within 1h").sum()), "due in the next hour", ORANGE)]
    if key == "Keep calling":
        return [(int((num("total_attempts") <= 1).sum()), "1 attempt only", ORANGE)]
    if key == "Resume":
        held = num("active_partialsave") == 1
        tab = rows.get("tab_id", pd.Series("", index=rows.index)).fillna("").astype(str).str.strip()
        return [(int(held.sum()), "held on a tablet now", BLUE),
                (int((held & tab.eq("")).sum()), "no tablet id", RED)]
    if key == "Review":
        return [(int((num("total_attempts") > ATTEMPTS_MAX).sum()),
                 f"more than {ATTEMPTS_MAX} attempts", RED),
                (int((num("shifts_covered_n") >= SHIFTS_MOST).sum()),
                 f"{SHIFTS_MOST}+ shifts tried", ORANGE)]
    return []


# A never-called pid recruited this many days ago or more is flagged: people go
# cold fast, and "17 never called" alone does not say whether they signed up
# yesterday or last week.
WAIT_DAYS = 3


def data_now(summary):
    """The dashboard's "now": the latest submission in the data, the same
    reference rollup() uses for overdue callbacks."""
    return pd.to_datetime(summary["last_submission_time"], errors="coerce").max()


def days_waiting(frame, now):
    """Whole days since each pid was recruited (blank date -> NaN)."""
    if "date_recruited" not in frame.columns or pd.isna(now):
        return pd.Series(float("nan"), index=frame.index)
    rec = parse_dates(frame["date_recruited"])
    rec.index = frame.index
    return (now.normalize() - rec.dt.normalize()).dt.days


def todays_pace(subs, day):
    """Calls made, respondents reached and interviews completed on `day`, in
    total and per enumerator. Calls = attempts (supervisor rows are paperwork);
    reached = calls where the respondent was on the line; completed = pids
    whose interview finished that day."""
    cols = ["enumerator", "calls", "reached", "completed"]
    if subs.empty or pd.isna(day) or "pid" not in subs.columns:
        return {"calls": 0, "reached": 0, "completed": 0}, pd.DataFrame(columns=cols)
    t = _to_dt(subs["endtime"] if "endtime" in subs.columns else pd.Series("", index=subs.index))
    if "starttime" in subs.columns:
        t = t.fillna(_to_dt(subs["starttime"]))
    d = subs.loc[t.dt.date == day.date()].copy()
    if d.empty:
        return {"calls": 0, "reached": 0, "completed": 0}, pd.DataFrame(columns=cols)
    d["_call"] = is_attempt_row(d)
    d["_reached"] = is_contact_row(d) & d["_call"]
    d["_done"] = d["callcode"].astype(str).str.strip().eq("1") if "callcode" in d.columns else False
    d["enumerator"] = (d["enum_name"].astype(str).str.strip()
                       if "enum_name" in d.columns else "")
    calls = d[d["_call"]]
    per = (calls.groupby("enumerator")
                .agg(calls=("pid", "size"), reached=("_reached", "sum"))
                .reset_index())
    done_by = calls[calls["_done"]].groupby("enumerator")["pid"].nunique()
    per["completed"] = per["enumerator"].map(done_by).fillna(0).astype(int)
    per["reached"] = per["reached"].astype(int)
    per = per[per["enumerator"] != ""].sort_values("calls", ascending=False)
    totals = {"calls": int(len(calls)), "reached": int(calls["_reached"].sum()),
              "completed": int(calls.loc[calls["_done"], "pid"].nunique())}
    return totals, per[cols]


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
    out["_day"] = pd.to_numeric(out["assigned_day"].astype(str).str.extract(r"(\d+)")[0],
                                errors="coerce")
    out["_ord"] = pd.to_numeric(out["ordering"], errors="coerce")
    if "date_recruited" in out.columns:
        rec = parse_dates(out["date_recruited"])
        rec.index = out.index
        out["_rec"] = rec
    else:
        out["_rec"] = pd.NaT
    return out


NO_CALLBACK_TIME = "Not specified, check final comments"


def _blank(s):
    return s.fillna("").astype(str).str.strip().replace({"nan": ""}).eq("")


def followup_view(rows):
    """Display columns for ⏰ To be followed up (the Callback queue).

    overdue      Yes / No, from callback_overdue (red rows, see style_overdue)
    callback_due blank -> "Not specified, check final comments": a 2_D drop or a
                 reschedule logged without a time carries none, and the time, if
                 any, was written in the comment instead
    callback_by  WHO calls back: the survey records "enumerator" or "respondent".
                 blank -> "enumerator" (our side calls back unless the respondent
                 said they would)
    comments     rsd_reason and last_comment (the final comment) in one column,
                 so the queue needs one free-text column instead of two"""
    out = rows.copy()
    out["overdue"] = (pd.to_numeric(out.get("callback_overdue", 0), errors="coerce")
                      .fillna(0).eq(1).map({True: "Yes", False: "No"}))
    due = out.get("callback_due", pd.Series("", index=out.index)).fillna("").astype(str).str.strip()
    out["callback_due"] = due.mask(_blank(due), NO_CALLBACK_TIME)
    by = out.get("callback_by", pd.Series("", index=out.index)).fillna("").astype(str).str.strip()
    out["callback_by"] = by.mask(_blank(by), "enumerator")
    rsd = out.get("rsd_reason", pd.Series("", index=out.index)).fillna("").astype(str).str.strip()
    com = out.get("last_comment", pd.Series("", index=out.index)).fillna("").astype(str).str.strip()
    out["comments"] = [
        r if (not c or c == r) else c if not r else f"{r} — {c}"
        for r, c in zip(rsd.replace({"nan": ""}), com.replace({"nan": ""}))]
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
    # NB: `active_partialsave` is NOT pulled in here. It already rides on `cases`
    # (rollup derives it from the case's own callcode), so merging the summary's
    # copy too would collide into active_partialsave_x/_y and disappear from every
    # table that names it - the same trap documented for was_partialsaved.
    eff = summary[[c for c in
                   ["pid", "total_attempts", "shifts_covered_n", "shifts_to_try",
                    "numbers_tried_of_available", "numbers_wrong",
                    "times_rescheduled", "times_dropped",
                    "times_noanswer", "times_off",
                    "last_section", "tab_id", "last_comment", "last_submission_time",
                    "assigned_day", "ordering", "date_recruited"]
                   if c in summary.columns]]
    oc = oc.merge(eff, on="pid", how="left")
    oc["shifts_tried"] = (oc["shifts_covered_n"].fillna(0).astype(int).astype(str) + "/5")
    ta = oc["total_attempts"].fillna(0)
    sc = oc["shifts_covered_n"].fillna(0)
    # Escalation = a pid worked hard and still not finished. Shifts and attempts
    # are an OR, not an AND: either one on its own is grounds for a supervisor to
    # look. `is_complete` is always 0 here (open cases only), but the test is kept
    # so the rule reads as written: "and still no completion".
    #
    # A CALLBACK IS NEVER ESCALATED, on either count. A scheduled callback is a
    # promise to the respondent with a time attached, so the only useful thing to
    # do with it is ring it at that time — moving it to Review buried it in a
    # supervisor list and took it off the call list, which is the opposite of what
    # a due appointment needs. Overdue used to escalate too, and does not any
    # more: it is a colour on the row now (style_overdue), not a different queue.
    # Callbacks therefore stay put, and "To be followed up" is once again exactly
    # the Callback bucket — no overlay needed, unlike Resume below.
    _is_callback = oc["action_bucket"] == "Callback"
    many_shifts = sc >= SHIFTS_MOST
    many_att = ta > ATTEMPTS_MAX
    esc_worked = (many_shifts | many_att) & (oc["is_complete"] != 1) & ~_is_callback
    # say which condition fired, so the queue is actionable rather than a bare label
    _why = pd.DataFrame({
        f"{SHIFTS_MOST}+/5 shifts": many_shifts & esc_worked,
        f">{ATTEMPTS_MAX} attempts": many_att & esc_worked,
    })
    # A list comprehension, NOT _why.apply(..., axis=1): on an EMPTY frame apply
    # returns a DataFrame rather than a Series, and assigning that to a single
    # column raises. Zero open cases is normal (every queue filtered out, or a
    # roster with nothing open), so this path has to survive it.
    oc["review_reason"] = [" + ".join(_why.columns[row]) for row in _why.values]
    oc.loc[esc_worked, "action_bucket"] = "Review"
    # non-exclusive overlay: the Resume queue is every pid STILL holding a partial
    # save (last submission a 4_* - see rollup.is_active_partialsave) plus the
    # code-based Resume bucket (3_D). An overlay rather than a bucket because a
    # 4_SC partial also carries a scheduled callback time and belongs in the
    # Callback list too, and a partial escalated for review has to stay visible
    # here as well. `action_bucket` remains the exclusive partition the monitoring
    # summary totals on, so nothing is double-counted there.
    _aps = (pd.to_numeric(oc["active_partialsave"], errors="coerce").fillna(0)
            if "active_partialsave" in oc.columns else pd.Series(0, index=oc.index))
    oc["resume_queue"] = ((oc["action_bucket"] == "Resume") | (_aps == 1)).astype(int)
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
    # Supervisor review log (review.py). Never raises: when the service account
    # is not set up it comes back empty and the dashboard runs exactly as before.
    decisions, decisions_meta = review.load_decisions()
    subs, sample, cases, summary, meta = load_all(decisions=decisions)
except Exception as e:
    st.error(f"Could not load / roll up the data.\n\n{e}")
    st.stop()

# ---- WRONG-ROSTER GUARD -----------------------------------------------------
# Reading the wrong sample tab is the one failure that looks completely healthy:
# every figure computes fine, they are just about the wrong people. Say so loudly
# and at the top, because nothing further down the page would give it away.
_sample_tab = meta["sample"]["tab"]
if _sample_tab != EXPECTED_SAMPLE_TAB:
    st.error(
        f"🛑 **Reading the wrong roster tab: `{_sample_tab}`** — "
        f"expected `{EXPECTED_SAMPLE_TAB}`. Every number on this page is about "
        "the wrong set of respondents. The page still renders normally, which "
        "is exactly why this needs saying here."
    )
    st.caption(
        "**Fix:** open this app's ⚙ **Settings → Secrets** and set "
        "`sample_csv_url` to the link ending in `gid=517204918`. The app "
        "restarts on its own. Locally the same line lives in "
        "`dashboard/.streamlit/secrets.toml`."
    )

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

# Supervisor review: the note for the enumerators (keep trying / sent back) rides
# on every case row so each queue can show it, and a validated flag for the
# Inactive tab. cases_roster is kept BEFORE the sidebar filters, for the review
# tab, which must list everything waiting whichever batch the sidebar is on.
_notes = review.notes_by_pid(decisions)
cases["supervisor_note"] = cases["pid"].map(_notes).fillna("")
_validated = review.validated_pids(decisions)
cases_roster = cases


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


def date_range_filter(label, dates, pids, help=None, default_from=None):
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
    # Opens at `default_from` (the newest recruitment date) rather than at the
    # start of the data, so the dashboard lands on the batch being worked now.
    # Ignored if it falls outside the data, so a stale default cannot produce an
    # out-of-range picker. Widening back to the full span still means "no filter".
    start = default_from if (default_from and lo <= default_from <= hi) else lo
    got = st.sidebar.date_input(label, value=(start, hi), min_value=lo, max_value=hi,
                                help=help)
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
# Both filters open on the MOST RECENT recruitment batch — see newest_batch().
# They are derived from the same call, so the route and the date can never
# contradict each other and leave the page empty.
_batch_date, _batch_routes = newest_batch(summary, route_col)
picked_route = (st.sidebar.multiselect("Route", route_options,
                                       default=_batch_routes or route_options)
                if route_options else [])

# when the respondent was recruited (roster date)
rec_pids = date_range_filter(
    "Recruitment date", summary.get("date_recruited", pd.Series(dtype=str)), summary["pid"],
    help="When the respondent was recruited (sample tab).",
    default_from=_batch_date)
if _batch_date:
    st.sidebar.caption(f"Latest batch: **{_batch_date:%d %b}**"
                       + (f" ({', '.join(_batch_routes)})" if _batch_routes else ""))

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
(tab_summary, tab_action, tab_archive, tab_partial, tab_enum, tab_track,
 tab_review, tab_guide) = st.tabs(
    ["🧾 Calls summary", "✅ Active pids", "🗄️ Inactive pids",
     "💾 Partial saves", "👤 By enumerator", "📋 Tracking sheet",
     "🛂 Supervisor review", "📖 Guide"])

# ============================================================================
# TAB 1 — RESPONDENT SUMMARY (supervisor view: one row per respondent, whole sample)
# ============================================================================
with tab_summary:
    # Reads top to bottom in the order an enumerator works: what to do now (by
    # priority), what has gone wrong, then how far along we are. Explanations
    # live in guide.html (the 📖 Guide tab).
    sk = summary_kpis(summary_view)
    resp = sk["respondents"] or 1
    ea_pct = round(100 * sk["ever_attempted"] / resp)
    total_att = int(pd.to_numeric(summary_view["total_attempts"], errors="coerce").fillna(0).sum())
    avg_att = round(total_att / sk["ever_attempted"], 1) if sk["ever_attempted"] else 0
    # "contacted" = ever picked up; it denominates the second completion tile.
    n_picked = int((summary_view["ever_picked_up"] == 1).sum())
    # TWO completion rates: how far through the sample we are, vs. how well the
    # interview converts once someone answers. Completing requires answering, so
    # the second cannot exceed 100%.
    done_of_contacted = round(100 * sk["completed"] / n_picked) if n_picked else 0

    # ---- the work, counted once for the whole page ----------------------------
    ocb = open_with_buckets(cases, summary)
    cnt = ocb["action_bucket"].value_counts() if len(ocb) else pd.Series(dtype=int)
    n_followup, n_resume = int(cnt.get("Callback", 0)), int(cnt.get("Resume", 0))
    # What the Resume queue LISTS: held partials + 3_D, overlapping the other
    # queues. `n_resume` (the exclusive bucket) is what `actionable` totals on,
    # so nothing double-counts.
    n_resume_shown = int(ocb["resume_queue"].sum()) if len(ocb) else 0
    n_keep, n_review = int(cnt.get("Keep calling", 0)), int(cnt.get("Review", 0))
    n_assign = int(((summary_view["case_state"] == "Not assigned")
                    & (summary_view["eligible_to_call"] == 1)).sum())
    n_whatsapp = int(ocb["whatsapp"].sum()) if len(ocb) else 0   # hidden unless SHOW_WHATSAPP
    total = len(summary_view)
    actionable = n_followup + n_resume + n_keep + n_review + n_assign
    pct = round(100 * actionable / total) if total else 0
    # the follow-up queue split by urgency, so the most owed calls lead
    _cb = ocb[ocb["action_bucket"] == "Callback"] if len(ocb) else ocb
    n_overdue = (int((pd.to_numeric(_cb["callback_overdue"], errors="coerce").fillna(0) == 1).sum())
                 if len(_cb) and "callback_overdue" in _cb.columns else 0)
    n_next_hour = (int(_cb["callback_when"].astype(str).eq("within 1h").sum())
                   if len(_cb) and "callback_when" in _cb.columns else 0)
    n_other_cb = max(n_followup - n_overdue - n_next_hour, 0)
    # never-called pids split by how long they have waited since recruitment
    _now = data_now(summary)
    _unassigned = summary_view[(summary_view["case_state"] == "Not assigned")
                               & (summary_view["eligible_to_call"] == 1)]
    n_wait_old = int((days_waiting(_unassigned, _now) >= WAIT_DAYS).sum())
    n_wait_new = max(n_assign - n_wait_old, 0)

    # ---- 1. DO NOW: the queues in priority order -------------------------------
    st.subheader(f"🎯 Do now — {actionable} open pids ({pct}% of {total})")
    _todo = [(n_overdue, "overdue callbacks", RED, "call first · ⏰ To be followed up"),
             (n_next_hour, "callbacks due in the next hour", ORANGE, "⏰ To be followed up"),
             (n_other_cb, "other callbacks", BLUE, "⏰ To be followed up"),
             (n_resume_shown, "partial saves to resume", BLUE, "▶️ To be resumed"),
             (n_wait_old, f"never called, waiting {WAIT_DAYS}+ days", RED,
              "assign first · 🆕 To be assigned"),
             (n_wait_new, "never called, recruited recently", BLUE, "🆕 To be assigned"),
             (n_keep, "to keep calling", BLUE, "📵 To keep calling"),
             (n_review, "hard to reach", ORANGE, "supervisor · 🔎 To be reviewed")]
    if SHOW_WHATSAPP:
        _todo.append((n_whatsapp, "to message on WhatsApp", BLUE, "📱 WhatsApp"))
    flag_tiles(_todo)
    st.caption("Left to right = priority. The lists are in ✅ Active pids.")

    # ---- 2. FIX: things that went wrong (each shown only when it happened) -----
    _rc = summary_view[summary_view.get("recontacted_after_complete", 0) == 1]
    _ac = summary_view[summary_view.get("recontacted_after_close", 0) == 1]
    _held = summary_view[pd.to_numeric(summary_view.get("active_partialsave", 0),
                                       errors="coerce").fillna(0) == 1]
    _tab = (_held["tab_id"] if "tab_id" in _held.columns
            else pd.Series("", index=_held.index)).fillna("").astype(str).str.strip()
    _no_tab = _held[_tab.eq("")]
    _sup = (pd.to_numeric(summary_view["is_supervisor"], errors="coerce").fillna(0)
            if "is_supervisor" in summary_view.columns
            else pd.Series(0, index=summary_view.index))
    _closed = summary_view[
        (summary_view["current_status"].astype(str).str.upper() == "INACTIVE")
        & (_sup != 1) & ~summary_view["pid"].isin(_validated)]
    _fix = [(len(_rc), "called again after completing", RED, "table below"),
            (len(_ac), "called after a supervisor closed them", RED, "table below"),
            (len(_no_tab), "partial saves with no tablet id", RED, "💾 Partial saves"),
            (len(_closed), "closures waiting for validation", ORANGE,
             "supervisor · 🛂 Supervisor review")]
    st.divider()
    if any(f[0] for f in _fix):
        st.subheader("⚠️ To fix")
        flag_tiles([f for f in _fix if f[0]])
    else:
        st.subheader("✅ Nothing to fix")
    if len(_rc):
        st.markdown(f"**Called again after completing ({len(_rc)})**")
        st.dataframe(_rc[[c for c in ["pid", "route_recruited", "enumerator",
                                      "current_callcode", "total_attempts",
                                      "last_submission_time"] if c in _rc.columns]],
                     width="stretch", hide_index=True)
    if len(_ac):
        st.markdown(f"**Called after a supervisor closed them ({len(_ac)})**")
        st.dataframe(_ac[[c for c in ["pid", "route_recruited", "enumerator",
                                      "current_callcode", "calls_after_close",
                                      "last_submission_time"] if c in _ac.columns]],
                     width="stretch", hide_index=True)

    # ---- 3. TODAY: the pace of the day -----------------------------------------
    # "Today" is the day of the latest submission (the dashboard's "now"), named
    # in the heading so a morning view of yesterday's work is never mislabelled.
    _subs_view = subs[subs["pid"].isin(keep_pids)] if keep_pids is not None else subs
    _pace, _per = todays_pace(_subs_view, _now)
    st.divider()
    st.subheader(f"📞 Today — {_now:%d %b}" if pd.notna(_now) else "📞 Today")
    flag_tiles([(_pace["calls"], "calls made", BLUE),
                (_pace["reached"], "respondents reached", BLUE),
                (_pace["completed"], "interviews completed", GREEN)])
    if len(_per):
        st.dataframe(_per, width="stretch", hide_index=True)

    # ---- 4. PROGRESS: how far through the sample --------------------------------
    st.divider()
    st.subheader("📈 Progress")
    flag_tiles([
        (sk["respondents"], "respondents in the sample", BLUE),
        (f"{ea_pct}%", f"ever attempted ({sk['ever_attempted']}) · "
                       f"{sk['never_attempted']} not yet", BLUE),
        (f"{sk['completion_rate']}%", f"completed of sample ({sk['completed']} of "
                                      f"{sk['respondents']})", GREEN),
        (f"{done_of_contacted}%", f"completed of contacted ({sk['completed']} of "
                                  f"{n_picked} who picked up)", GREEN),
        (total_att, f"total attempts ({avg_att} per attempted pid)", BLUE),
    ])

    # Finished pids by reason. Completion is absorbing, so a pid that finished
    # and was then rung again counts as completed on is_complete even though its
    # LAST callcode is a call outcome; keying on the callcode would lose those.
    _inact_rows = summary_view[
        summary_view["current_status"].astype(str).str.upper() == "INACTIVE"]
    inactive_cc = _inact_rows["current_callcode"].astype(str).str.strip()
    _is_done = _inact_rows["is_complete"] == 1
    _n_inact = len(inactive_cc)
    _inact_pct = round(100 * _n_inact / total) if total else 0
    st.markdown(f"**Finished: {_n_inact} pids ({_inact_pct}%)**")
    _inact = [("1", "completed"), ("0_R", "refusal (0_R)"), ("0_UN", "ineligible (0_UN)"),
              ("0_IN", "incorrect respondent (0_IN)"), ("0_WN", "wrong number (0_WN)")]
    # _other is the REMAINDER, so the tiles always add up to the finished total.
    _pids = [int(_is_done.sum())]
    _pids += [int(((inactive_cc == code) & ~_is_done).sum()) for code, _ in _inact[1:]]
    _pids.append(max(_n_inact - sum(_pids), 0))
    _what = [d for _, d in _inact] + ["other closure"]
    flag_tiles([(n, w, GREEN if i == 0 else BLUE)
                for i, (n, w) in enumerate(zip(_pids, _what))])


# ============================================================================
# TAB — TRACKING SHEET (one row per respondent, full detail)
# ============================================================================
with tab_track:
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
    # NB: only the QUEUES depend on `cases` (they are built from call attempts).
    # "To be assigned" is built from `summary` and is exactly what you need to see
    # when nothing has been called yet — so it must live OUTSIDE this guard, or a
    # roster with no submissions shows an empty tab instead of the whole workload.
    if cases.empty:
        st.info("No calls logged yet.")
    else:
        status = cases["status"].astype(str).str.upper()

        st.subheader("Work queues — open pids")
        # One line of key for the shift letters; every other explanation of the
        # queues (codes, rules, columns) lives in guide.html (the 📖 Guide tab).
        st.caption("Shifts: A 07:30–09:30 · B 09:30–12:30 · C 12:30–15:30 · "
                   "D 15:30–17:30 · E 17:30–20:30")

        open_cases = open_with_buckets(cases, summary)   # shared with the monitoring summary

        BUCKETS = [
            ("Callback", "⏰ To be followed up"),
            ("Keep calling", "📵 To keep calling"),
            ("Resume", "▶️ To be resumed (partial saved)"),
            ("Review", "🔎 To be reviewed (hard to reach)"),
        ]
        COLS = {
            # History COUNTS, not ever-flags: for a callback the useful question is
            # how many times this has already happened, not merely whether it has.
            # Trimmed to what the enumerator needs to make the callback. The
            # display columns overdue / comments are built in the loop below
            # (followup_view): blanks in callback_due / callback_by are filled
            # in, and rsd_reason + last_comment are merged into one column.
            # No overdue column: an overdue row is simply red (style_overdue).
            "Callback":     ["pid", "callcode", "callback_due",
                             "callback_by", "shifts_to_try", "active_partialsave",
                             "total_attempts", "last_contact_date",
                             "last_submission_time", "comments"],
            # Trimmed to what an enumerator needs to pick up the phone: who,
            # what happened last, how hard it has been tried, which numbers are
            # left, and whether any of them is a dud. status / days_open /
            # last_contact_date are dropped - every pid here is open by
            # definition, and neither date changes what to dial next.
            # numbers_wrong is the survey's own count of this respondent's
            # numbers that turned out WRONG - per call on the form, so the
            # summary keeps the highest any call recorded. Not times_wrongnumber, which counts
            # ATTEMPTS that ended on a wrong-number code: a number can be known
            # bad without another attempt being spent on it, so that one reads 0
            # while a number really is wrong.
            # The call-list position first, then which shifts are left and
            # whether they ever picked up, then the effort as TOTALS, then the
            # last call: its code, who made it, and the comment.
            # callcode / enumerator are shown as last_callcode / last_enumerator.
            "Keep calling": ["pid", "ordering", "assigned_day", "shifts_to_try",
                             "ever_picked_up", "total_attempts", "times_noanswer",
                             "times_off", "times_rescheduled", "times_dropped",
                             "numbers_wrong", "last_submission_time",
                             "callcode", "enumerator", "last_comment"],
            # last_section is the readable label, not last_section_n. tab_id comes
            # from tab_id or rs_tab_id, whichever the submission carried.
            # callback_when / callback_due belong here as much as on the Callback
            # queue: a 4_SC held partial is a partial WITH an appointment, and
            # without these the enumerator could see it was held but not when it
            # was promised. Blank for a 4_D / 4_NA / 4_OF hold, which carries no
            # scheduled time - see the caption under this queue.
            "Resume":       ["pid", "enumerator", "status", "callcode",
                             "callback_when", "callback_due",
                             "total_attempts", "days_open", "last_contact_date",
                             "last_section", "tab_id", "active_partialsave",
                             "times_rescheduled", "times_dropped", "times_noanswer",
                             "times_off", "rsd_reason", "shifts_to_try", "last_comment"],
            "Review":       ["pid", "enumerator", "status", "callcode",
                             "total_attempts", "days_open", "last_contact_date",
                             "review_reason", "callback_when", "active_partialsave",
                             "shifts_to_try", "numbers_tried_of_available",
                             "rsd_reason", "last_comment"],
        }

        # Open cases laid out down the page, one section per queue.
        for key, label in BUCKETS:
            # Resume reads its non-exclusive overlay (held partials + 3_D);
            # every other queue reads the exclusive bucket. Callbacks are never
            # escalated, so the Callback bucket is already the whole list.
            rows = (open_cases[open_cases["resume_queue"] == 1].copy() if key == "Resume"
                    else open_cases[open_cases["action_bucket"] == key].copy())
            if key == "Callback":
                # Overdue first, then soonest due. Sorted on the PARSED time, not
                # the raw string: callback_due is free text like "2026-09-02 8:00",
                # so a lexical sort put 8:00 after 16:00 and floated the untimed
                # ones (a 2_D drop has no scheduled time) above real appointments.
                rows["_cb_dt"] = pd.to_datetime(rows["callback_due"], errors="coerce")
                rows = rows.sort_values(["callback_overdue", "_cb_dt"],
                                        ascending=[False, True],
                                        na_position="last").drop(columns="_cb_dt")
                rows = followup_view(rows)
            elif key == "Keep calling":
                # The next-attempt pile: fewest attempts first, and within the same
                # number of attempts, CALL-LIST ORDER (recruitment day, assigned
                # day, then `ordering`). Without an ordering yet, fewest shift windows tried
                # and the longest left come next, as before.
                rows = with_list_order(rows)
                rows["_last"] = pd.to_datetime(rows.get("last_submission_time"),
                                               errors="coerce")
                rows = rows.sort_values(
                    ["total_attempts", "_rec", "_day", "_ord", "shifts_covered_n", "_last"],
                    na_position="last").drop(columns=["_last", "_rec", "_day", "_ord"])
            else:
                rows = rows.sort_values("_active_first", ascending=False)
            # The supervisor's note (Supervisor review: keep trying / sent back)
            # sits right after the pid, so the enumerator reads it before
            # dialling. Only shown when some pid in this queue has one.
            _cols = [c for c in COLS[key] if c in rows.columns]
            if "supervisor_note" in rows.columns and rows["supervisor_note"].ne("").any():
                _cols.insert(1, "supervisor_note")
            # Which rows to paint red, read BEFORE trimming the columns: the
            # follow-up queue no longer shows its overdue flag, only the colour.
            _red = (rows["overdue"].eq("Yes") if key == "Callback" and "overdue" in rows.columns
                    else None)
            # Each queue: heading, a flag or two for what needs attention, the
            # table. Explanations live in guide.html (the 📖 Guide tab). Counted BEFORE the
            # columns are trimmed, since some flags read columns not shown.
            _flags = queue_flags(key, rows, _red)
            rows = rows[_cols]
            if key == "Keep calling":
                rows = rows.rename(columns={"callcode": "last_callcode",
                                            "enumerator": "last_enumerator"})
            st.markdown(f"#### {label} ({len(rows)})")
            if len(rows):
                flag_tiles(_flags)
                st.dataframe(style_overdue(yesno(rows), mask=_red),
                             width="stretch", hide_index=True)
            else:
                st.caption("Nothing here right now.")

        # non-exclusive overlay: WhatsApp outreach (these pids also appear above)
        if SHOW_WHATSAPP:
            wa = open_cases[open_cases["whatsapp"] == 1].copy()
            st.markdown(f"#### 📱 To be reached out on WhatsApp ({len(wa)})")
            if wa.empty:
                st.caption("Nothing here right now.")
            else:
                wa_cols = ["pid", "enumerator", "status", "callcode", "action_bucket",
                           "total_attempts", "shifts_to_try", "numbers_tried_of_available",
                           "last_comment"]
                st.dataframe(yesno(wa[[c for c in wa_cols if c in wa.columns]]),
                             width="stretch", hide_index=True)

    # Not yet assigned — recruited but never worked. Built from `summary`, so it
    # renders whether or not any calls exist. NOTE: the sample tab has no explicit
    # "assigned" column, so "no submission yet" is the proxy for unassigned.
    # case_state, not ever_attempted: a pid a supervisor closed with no call is
    # Archived, and must not reappear here as work to hand out.
    new_cases = summary[(summary["case_state"] == "Not assigned")
                        & (summary["eligible_to_call"] == 1)].copy()
    if keep_pids is not None:
        new_cases = new_cases[new_cases["pid"].isin(keep_pids)]
    st.markdown(f"#### 🆕 To be assigned ({len(new_cases)})")
    if new_cases.empty:
        st.caption("Nothing to assign.")
    else:
        # CALL-LIST ORDER: first attempts go strictly in list order, so the top
        # row is always the next pid to hand out. Oldest recruitment day first
        # (the longest waiting), then its day-4 list before its day-5 list, each
        # in `ordering`.
        new_cases["days_waiting"] = days_waiting(new_cases, data_now(summary))
        new_cases = (with_list_order(new_cases)
                     .sort_values(["_rec", "_day", "_ord"], na_position="last")
                     .drop(columns=["_rec", "_day", "_ord"]))
        flag_tiles([(int((new_cases["days_waiting"] >= WAIT_DAYS).sum()),
                     f"waiting {WAIT_DAYS}+ days since recruitment", RED)])
        # Both roster vintages listed: the 2026-08 data-entry names first,
        # then the earlier ones. Only the columns actually on the sheet show.
        newcols = [c for c in ["pid", "assigned_day", "ordering", "days_waiting", "route_recruited", "date_recruited",
                               "number_of_phone_numbers", "phone_sample_status",
                               "own_phone", "phones_provided", "rec_signup"]
                   if c in new_cases.columns]
        _red_wait = new_cases["days_waiting"] >= WAIT_DAYS
        _show = yesno(new_cases[newcols])
        _show["days_waiting"] = new_cases["days_waiting"].astype("Int64")
        st.dataframe(style_overdue(_show, mask=_red_wait), width="stretch",
                     hide_index=True)

    with st.expander(f"See raw submissions ({len(subs)} rows, one per call attempt)"):
        st.dataframe(subs, width="stretch", hide_index=True)

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

# ============================================================================
# TAB — PARTIAL SAVES (which tablet is holding each half-finished interview)
# ----------------------------------------------------------------------------
# A partial save lives on the DEVICE it was taken on, so it can only be resumed
# from that tablet. This tab answers the operational question: how many are out
# there, which tablet is each sitting on, and who last touched it.
# ============================================================================
with tab_partial:
    st.subheader("💾 Partial saves")

    NO_TAB = "⚠️ no tablet id recorded"
    ps = summary_view[summary_view.get("active_partialsave", 0) == 1].copy()
    if "tab_id" in ps.columns:
        ps["tab_id"] = ps["tab_id"].astype(str).str.strip().replace("", NO_TAB)
    else:
        ps["tab_id"] = NO_TAB

    if ps.empty:
        st.info("No partial saves held now.")
    else:
        n_pids = len(ps)
        n_tabs = int(ps["tab_id"].nunique())
        n_lost = int((ps["tab_id"] == NO_TAB).sum())
        flag_tiles([(n_pids, "held now", BLUE), (n_tabs, "tablets holding them", BLUE),
                    (n_lost, "no tablet id", RED)])

        # A tablet id that is really a callcode or the pid itself. `rs_tab_id` is a
        # free-text box sitting between two coded questions on the 4_* log, and it
        # is regularly filled with the wrong thing. Untraceable in practice, so say
        # so rather than presenting "0_OC" as if it were a device. Nothing is
        # rewritten - the value shown is what the enumerator typed.
        _looks_like_cc = ps["tab_id"].str.match(r"^[0-4]_[A-Za-z]{1,4}$|^1$")
        _is_the_pid = (ps["tab_id"].str.upper().str.strip()
                       == ps["pid"].astype(str).str.upper().str.strip())
        _bad = ps[_looks_like_cc | _is_the_pid]
        if len(_bad):
            st.warning(
                f"⚠️ **{len(_bad)} with a tablet id that is not a tablet:** "
                + "  ·  ".join(f"`{p}` → *{t}* ({e})" for p, t, e in
                               zip(_bad["pid"], _bad["tab_id"], _bad["enumerator"])))

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

        # ---- one row per partial save ---------------------------------------
        st.markdown("#### 📄 Every partial save")
        pcols = [c for c in ["pid", "tab_id", "enumerator", "last_submission_time",
                             "current_status", "current_callcode",
                             "callback_when", "callback_due", "action_bucket",
                             "last_section", "total_attempts", "days_open",
                             "route_recruited", "last_comment"]
                 if c in ps.columns]
        st.dataframe(ps[pcols].sort_values(["tab_id", "last_submission_time"],
                                           ascending=[True, False]),
                     width="stretch", hide_index=True)


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
    # Supervisor review: validated = confirmed / corrected in the review tab, or
    # closed by a supervisor in the first place (Data Team Correction form or a
    # dashboard decision). Everything else closed is still waiting for review.
    _sup = pd.to_numeric(arch["is_supervisor"], errors="coerce").fillna(0) == 1
    arch["validated"] = ["✔" if (p in _validated or s) else "pending"
                         for p, s in zip(arch["pid"], _sup)]
    st.subheader(f"🗄️ Inactive pids ({len(arch)})")
    if len(arch):
        flag_tiles([(int((arch["validated"] == "pending").sum()),
                     "waiting for supervisor validation", ORANGE)])
    if arch.empty:
        st.info("No inactive pids yet.")
    else:
        base = ["pid", "validated", "enumerator", "callcode", "attrition_reason", "ineligible_type1",
                "is_incorrect", "is_othercontact", "refuse_why", "numbers_wrong",
                "numbers_tried_of_available", "is_supervisor", "was_partialsaved",
                "total_attempts", "last_contact_date", "last_comment"]
        if route_col:
            base.insert(2, route_col)
        acols = [c for c in base if c in arch.columns]
        st.dataframe(yesno(arch[acols]), width="stretch", hide_index=True)

# ============================================================================
# TAB 7 — SUPERVISOR REVIEW (validate closures, decide hard cases) — review_tab.py
# Whole eligible roster, NOT the sidebar-filtered view: see review_tab.py.
# ============================================================================
with tab_review:
    review_tab.render(summary, subs, open_with_buckets(cases_roster, summary),
                      decisions, decisions_meta)

# ============================================================================
# TAB 8 — GUIDE (how to read every tab, queue, flag and code) — guide.html
# Shown in its own frame so its styles cannot leak into the dashboard. The
# frame scrolls on its own, and the contents links jump within it.
# ============================================================================
with tab_guide:
    _guide = pathlib.Path(__file__).with_name("guide.html")
    if _guide.exists():
        components.html(_guide.read_text(encoding="utf-8"), height=1100, scrolling=True)
    else:
        st.info("The guide file (guide.html) is missing next to app.py.")

# ============================================================================
# FOOTER — what this page is built from
# ----------------------------------------------------------------------------
# Sits below the tabs, so it shows whichever tab is open. Everything needed to
# answer "is the dashboard looking at the right data, and is it running the
# right code?" without opening a terminal.
# ============================================================================
st.divider()
with st.expander("🔧 What this page is built from — sources, filters, checks",
                 expanded=False):
    _a, _s = meta["attempts"], meta["sample"]

    st.markdown("**Reading**")
    st.dataframe(pd.DataFrame([
        {"what": "Call attempts", "origin": _a["origin"], "tab": _a["tab"],
         "gid": _a["gid"], "rows": _a["rows"], "read at": _a["read"]},
        {"what": "Roster (data entry)", "origin": _s["origin"], "tab": _s["tab"],
         "gid": _s["gid"], "rows": _s["rows"], "read at": _s["read"]},
        {"what": "Supervisor decisions",
         "origin": "Google Sheet (service account)" if decisions_meta.get("ok") else "⚠️ not read",
         "tab": review.TAB, "gid": "", "rows": decisions_meta.get("rows", 0),
         "read at": decisions_meta.get("msg", "") or "ok"},
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
    _ac_n = int(pd.to_numeric(summary.get("recontacted_after_close", 0),
                              errors="coerce").fillna(0).sum())
    _checks = [
        {"check": "Signed-up rows that also carry an ineligibility flag",
         "count": n_conflict, "verdict": "OK" if not n_conflict else "⚠️ check data entry"},
        {"check": "Completed, then called again anyway",
         "count": _rc, "verdict": "OK" if not _rc else "⚠️ see Calls summary"},
        {"check": "Closed by a supervisor, then logged against anyway",
         "count": _ac_n, "verdict": "OK" if not _ac_n else "⚠️ see Calls summary"},
        {"check": "Pids with attempts but no roster row",
         "count": n_offroster_cases, "verdict": "OK" if not n_offroster_cases else "older sample"},
    ]
    st.dataframe(pd.DataFrame(_checks), width="stretch", hide_index=True)

    st.caption(f"In view now: **{len(summary_view)} pids**, **{len(cases)} cases**, "
               f"**{len(subs)} attempt rows** (after any sidebar filters). "
               "Run `CHECK.bat` for the full validation suite.")
