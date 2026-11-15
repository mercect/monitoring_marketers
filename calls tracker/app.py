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
import re

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from auth import require_password
from data_io import (load_all, route_column, parse_dates,
                     REFRESH_SECONDS, code_stamp, EXPECTED_SAMPLE_TAB)
from rollup import (OPEN_STATUSES, summary_kpis,
                    eligible_roster, recruit_exclusions,
                    CC_CALLBACK, CC_KEEP_CALLING, CC_RESUME,
                    is_attempt_row, is_contact_row, _to_dt)
import assign
import attendance
import column_help as ch
import enumerators
from calllist import assigned_day_number, with_list_order
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
            f'<div style="box-sizing:border-box;border:1px solid {c};border-left:5px solid {c};'
            f'border-radius:8px;padding:6px 12px;min-width:165px;max-width:260px;background:{c}1A">'
            f'<div style="font-size:1.45rem;font-weight:700;line-height:1.2;color:{c}">{n}</div>'
            f'<div style="font-size:0.85rem;opacity:.85;line-height:1.25">{label}</div>'
            + (f'<div style="font-size:0.72rem;opacity:.6;margin-top:1px">{hint}</div>'
               if hint else "") + '</div>')
    st.markdown('<div style="display:flex;gap:10px;flex-wrap:wrap;margin:2px 0 12px">'
                + "".join(tiles) + "</div>", unsafe_allow_html=True)


def category_row(title, total_n, where, items):
    """One line of Do now: the category (name, total, list to open) as a fixed-
    width label, then its count tiles, all the same size so the rows line up.
    Wraps on a narrow screen. Same colour rules as flag_tiles."""
    def tile(item):
        n, label, colour = item[:3]
        hint = item[3] if len(item) > 3 else ""
        c = colour if n else GREY
        return (f'<div style="flex:0 0 165px;box-sizing:border-box;border:1px solid {c};'
                f'border-left:5px solid {c};border-radius:8px;padding:6px 12px;background:{c}1A">'
                f'<div style="font-size:1.45rem;font-weight:700;line-height:1.2;color:{c}">{n}</div>'
                f'<div style="font-size:0.85rem;opacity:.85;line-height:1.25">{label}</div>'
                + (f'<div style="font-size:0.72rem;opacity:.6;margin-top:1px">{hint}</div>'
                   if hint else "") + '</div>')
    head = (f'<div style="flex:0 0 200px;box-sizing:border-box;padding:4px 4px 4px 0">'
            f'<div style="font-size:1.05rem;font-weight:700;line-height:1.3">{title}</div>'
            f'<div style="font-size:1.6rem;font-weight:700;line-height:1.2">{total_n}'
            f'<span style="font-size:0.85rem;font-weight:400;opacity:.7"> pids</span></div>'
            f'<div style="font-size:0.75rem;opacity:.6">{where}</div></div>')
    st.markdown('<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:stretch;'
                'margin:2px 0 10px;padding-bottom:10px;border-bottom:1px solid rgba(128,128,128,.18)">'
                + head + "".join(tile(i) for i in items) + "</div>", unsafe_allow_html=True)


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


# Enumerators working at once = how many pids are handed out together. Those
# are tried at about the same time, so within one round the order can blur;
# a pid more than this many places behind the front of its list was skipped.
ASSIGN_WINDOW = 8


def skipped_in_order(roster, window=ASSIGN_WINDOW, handed=()):
    """pid -> the front of its call list, for never-called pids left behind.

    A call list is one recruitment day's day-4 or day-5 list. Its FRONT is the
    highest `ordering` already tried (any submission at all). A never-called
    pid whose ordering is more than `window` places behind that front should
    already have been handed out: with 8 enumerators trying #20-28, #8 is
    skipped. Pids without an ordering are never flagged."""
    if roster.empty:
        return pd.Series(dtype=float)
    d = with_list_order(roster)
    # tried = called at all, or assigned in the dashboard (assign.py)
    tried = (d["case_state"].astype(str) != "Not assigned") | d["pid"].isin(set(handed))
    lst = d["date_recruited"].astype(str) + "|" + d["_day"].astype(str)
    front = d.loc[tried & d["_ord"].notna()].groupby(lst[tried & d["_ord"].notna()])["_ord"].max()
    d["_front"] = lst.map(front)
    behind = (~tried) & d["_ord"].notna() & d["_front"].notna() & (d["_ord"] < d["_front"] - window)
    return pd.Series(d.loc[behind, "_front"].values, index=d.loc[behind, "pid"].values)


ORANGE_CSS = "color: #f08c00; font-weight: 600"


def style_rows(df, red=None, orange=None):
    """Red rows for `red`, orange for `orange` (red wins where both apply)."""
    if df.empty:
        return df
    red = (red.reindex(df.index, fill_value=False).astype(bool) if red is not None
           else pd.Series(False, index=df.index))
    orange = (orange.reindex(df.index, fill_value=False).astype(bool) if orange is not None
              else pd.Series(False, index=df.index)) & ~red
    if not (red.any() or orange.any()):
        return df
    styles = pd.DataFrame("", index=df.index, columns=df.columns)
    styles.loc[orange, :] = ORANGE_CSS
    styles.loc[red, :] = OVERDUE_CSS
    return df.style.apply(lambda _: styles, axis=None)


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
    out["comments"] = merged_comments(out)
    return out


def merged_comments(rows):
    """rsd_reason, then last_comment (the final comment), in one column: only
    one if the other is empty, once if they are identical. rsd_reason alone is
    blank on most rows, so it never earns a column of its own."""
    rsd = rows.get("rsd_reason", pd.Series("", index=rows.index)).fillna("").astype(str).str.strip()
    com = rows.get("last_comment", pd.Series("", index=rows.index)).fillna("").astype(str).str.strip()
    return [r if (not c or c == r) else c if not r else f"{r} — {c}"
            for r, c in zip(rsd.replace({"nan": ""}), com.replace({"nan": ""}))]


def review_resume_view(rows):
    """Display columns for 🔎 To be reviewed and ▶️ To be resumed: comments
    merged, and "never reached" where nobody has spoken to the respondent yet
    (a blank last_contact_date looked like missing data)."""
    out = rows.copy()
    out["comments"] = merged_comments(out)
    lc = out.get("last_contact_date", pd.Series("", index=out.index)).fillna("").astype(str).str.strip()
    out["last_contact_date"] = lc.mask(lc.isin(["", "nan", "NaT"]), "never reached")
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


st.title("📞 Call-Tracking Monitor Dashboard")

try:
    # Supervisor review log (review.py). Never raises: when the service account
    # is not set up it comes back empty and the dashboard runs exactly as before.
    decisions, decisions_meta = review.load_decisions()
    # One name per enumerator, from the `enumerators` tab (enumerators.py).
    enum_roster, enum_meta = enumerators.load_enumerators()
    name_map = enumerators.alias_map(enum_roster)
    subs, sample, cases, summary, meta = load_all(decisions=decisions, name_map=name_map)
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
# First assignments made in the dashboard (assign.py): who has been handed out,
# and the dropdown of enumerators to hand out to.
_asg = assign.assignments(decisions)
_handed = set(_asg["pid"])
_enum_names = (list(enum_roster["name"]) if len(enum_roster) else
               sorted({n for n in subs.get("enum_name", pd.Series(dtype=str)).astype(str).str.strip()
                       if n} - {review.editor_name()}))
_asg_checks = assign.checks(summary, subs, _asg)
cases_roster = cases


_, right = st.columns([4, 1])
if right.button("🔄 Refresh now"):
    st.cache_data.clear()
    st.rerun()

# Which tabs this view is actually built from, and when they were read. Without
# this there is no way to tell a stale cache from fresh data on screen.
# (the data-source and roster detail now live in the diagnostics panel at the
#  very bottom of the page — see "What this page is built from")

# --- Filter: one row across the top of the page, always visible -------------
# Priority list (assigned_day, e.g. "12Nov-D4"), keyed by pid, so it narrows
# `cases` and the summary through the pid list. Empty = all lists.
#   Supervisor review, To be revised and Attendance ignore it on purpose.
route_col = route_column(summary)          # still shown as a column in tables

_aday = (summary["assigned_day"].fillna("").astype(str).str.strip()
         if "assigned_day" in summary.columns else pd.Series("", index=summary.index))


def _list_key(v):
    """Order the lists by date, then day number: '03Nov-D4' < '03Nov-D5' < '12Nov-D4'.
    Plain values without a date ('4', '5') come first."""
    d = pd.to_datetime(v.split("-")[0], format="%d%b", errors="coerce")
    n = pd.to_numeric(assigned_day_number(pd.Series([v])), errors="coerce").iloc[0]
    return (pd.notna(d), d.dayofyear if pd.notna(d) else 0, n if pd.notna(n) else 0, v)


list_options = sorted({v for v in _aday if v}, key=_list_key)

picked_lists = st.multiselect("Priority list (assigned_day)", list_options,
                              key="flt_lists", placeholder="All lists",
                              help="Pick one or more call lists, e.g. 12Nov-D4. "
                                   "Leave empty for all.")
base_pids = set(summary.loc[_aday.isin(picked_lists).values, "pid"]) if picked_lists else None
keep_pids = base_pids

summary_view = summary
summary_base = summary if base_pids is None else summary[summary["pid"].isin(base_pids)]
if keep_pids is not None:
    cases = cases[cases["pid"].isin(keep_pids)]
    summary_view = summary_view[summary_view["pid"].isin(keep_pids)]
st.caption(f"Showing **{len(summary_view)}** of {len(summary)} pids.")

# Tab ORDER is set here; the `with tab_*:` bodies further down can stay in any
# order, Streamlit renders each into its own tab regardless.
(tab_summary, tab_attend, tab_action, tab_archive, tab_partial,
 tab_review, tab_guide) = st.tabs(
    ["🧾 Calls summary", "🗓️ Attendance", "✅ Active pids", "🗄️ Inactive pids",
     "💾 Partial saves",
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
    # Never-called pids follow only the priority list (no call date
    # to filter on): summary_base, as in To be assigned.
    _unassigned = summary_base[(summary_base["case_state"] == "Not assigned")
                               & (summary_base["eligible_to_call"] == 1)]
    n_assign = len(_unassigned)
    n_whatsapp = int(ocb["whatsapp"].sum()) if len(ocb) else 0   # hidden unless SHOW_WHATSAPP
    total = len(set(summary_view["pid"]) | set(_unassigned["pid"]))
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
    n_wait_old = int((days_waiting(_unassigned, _now) >= WAIT_DAYS).sum())
    n_wait_new = max(n_assign - n_wait_old, 0)

    # ---- 1. DO NOW: the work, grouped by what the enumerator has to do --------
    # Each category is one line: its name and total on the left, the counts
    # that need attention on the right (most urgent first).
    _rc = summary_view[summary_view.get("recontacted_after_complete", 0) == 1]
    _ac = summary_view[summary_view.get("recontacted_after_close", 0) == 1]
    _held = summary_view[pd.to_numeric(summary_view.get("active_partialsave", 0),
                                       errors="coerce").fillna(0) == 1]
    _tab = (_held["tab_id"] if "tab_id" in _held.columns
            else pd.Series("", index=_held.index)).fillna("").astype(str).str.strip()
    _no_tab = _held[_tab.eq("")]
    # never-called pids the call list has moved past (see skipped_in_order)
    _skipped = skipped_in_order(summary, handed=_handed)
    n_skipped = int(summary_view["pid"].isin(_skipped.index).sum())

    def _category(title, total_n, items, where):
        category_row(title, total_n, where, items)

    st.subheader(f"🎯 Do now — {actionable} open pids ({pct}% of {total})")
    _category("⏰ To follow up", n_followup,
              [(n_overdue, "overdue", RED, "call first"),
               (n_next_hour, "due in the next hour", ORANGE),
               (n_other_cb, "later", BLUE)], "⏰ To be followed up")
    _category("▶️ To resume", n_resume_shown,
              [(n_resume_shown, "partial saves to resume", BLUE, "on the tablet that saved it"),
               (len(_no_tab), "with no tablet id", RED, "💾 Partial saves")], "▶️ To be resumed")
    _category("🆕 To assign", n_assign,
              [(n_skipped, "skipped in list order", RED, "assign now"),
               (n_wait_old, f"waiting {WAIT_DAYS}+ days", RED),
               (n_wait_new, "recruited recently", BLUE),
               (len(_asg_checks["late"]), f"assigned, not called in {assign.LATE_MINUTES} min",
                RED, "📨 Assigned, not called yet")], "🆕 To be assigned")
    _category("📵 Keep trying", n_keep + n_review,
              [(n_keep, "to keep calling", BLUE),
               (n_review, "hard to reach", ORANGE, "supervisor")],
              "📵 To keep calling · 🔎 To be reviewed")
    if SHOW_WHATSAPP:
        _category("📱 WhatsApp", n_whatsapp, [(n_whatsapp, "to message", BLUE)], "📱 WhatsApp")
    st.caption("Top to bottom = priority. The lists are in ✅ Active pids.")

    # ---- 2. FIX: calls that should not have happened (only when they did) -----
    _fix = [(len(_rc), "called again after completing", RED, "table below"),
            (len(_ac), "called after a supervisor closed them", RED, "table below"),
            (len(_asg_checks["unassigned"]), "called without being assigned", ORANGE,
             "table below"),
            (len(_asg_checks["other"]), "called by someone other than assigned", ORANGE,
             "table below")]
    st.divider()
    if any(f[0] for f in _fix):
        st.subheader("⚠️ To fix")
        flag_tiles([f for f in _fix if f[0]])
    else:
        st.subheader("✅ Nothing to fix")
    if len(_rc):
        st.markdown(f"**Called again after completing ({len(_rc)})**")
        ch.dataframe(_rc[[c for c in ["pid", "route_recruited", "enumerator",
                                      "current_callcode", "total_attempts",
                                      "last_submission_time"] if c in _rc.columns]],
                     width="stretch", hide_index=True)
    if len(_ac):
        st.markdown(f"**Called after a supervisor closed them ({len(_ac)})**")
        ch.dataframe(_ac[[c for c in ["pid", "route_recruited", "enumerator",
                                      "current_callcode", "calls_after_close",
                                      "last_submission_time"] if c in _ac.columns]],
                     width="stretch", hide_index=True)

    _un = _asg_checks["unassigned"]
    if len(_un):
        st.markdown(f"**Called without being assigned ({len(_un)})**")
        _u = _un.assign(first_call_at=pd.to_datetime(_un["first_call_at"]).dt.strftime("%d %b %H:%M"))
        ch.dataframe(_u.rename(columns={"first_caller": "called_by"}), width="stretch",
                     hide_index=True)
    _ot = _asg_checks["other"]
    if len(_ot):
        st.markdown(f"**Called by someone other than assigned ({len(_ot)})**")
        _o = _ot.assign(first_call_at=pd.to_datetime(_ot["first_call_at"]).dt.strftime("%d %b %H:%M"),
                        assigned_at=pd.to_datetime(_ot["assigned_at"]).dt.strftime("%d %b %H:%M"))
        ch.dataframe(_o[["pid", "assigned_to", "first_caller", "assigned_at", "first_call_at"]]
                     .rename(columns={"first_caller": "called_by"}), width="stretch",
                     hide_index=True)

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
        ch.dataframe(_per, width="stretch", hide_index=True)

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

        # ---- 🔁 To be revised — Data Team checked -----------------------------
        # Cases the supervisor sent back or reopened in Supervisor review, that
        # nobody has called since. First on the page: a direct instruction. The
        # first call afterwards moves the pid back into its normal queue; while
        # it waits here it is left out of the other queues, so nobody calls it
        # twice.
        # Whole roster, whatever the sidebar shows: a supervisor's instruction
        # must not be hidden by the batch filter.
        _open_all = open_with_buckets(cases_roster, summary)
        _react = review.reactivated(decisions, subs, assign.utc_now())
        _react = _react[_react["pid"].isin(_open_all["pid"])]
        st.markdown(f"#### 🔁 To be revised — Data Team checked ({len(_react)})")
        if _react.empty:
            st.caption("Nothing here right now.")
        else:
            _rv = _react.merge(_open_all[[c for c in ["pid", "assigned_day", "shifts_to_try",
                                                      "total_attempts"]
                                          if c in _open_all.columns]], on="pid", how="left")
            # The last REAL call (who, and its code): the latest row is the
            # supervisor's own reopen entry, which says neither.
            _calls = subs[~review._is_sup(subs)].copy()
            _calls["_t"] = pd.to_datetime(_calls.get("endtime"), errors="coerce", utc=True,
                                          format="ISO8601")
            _last = (_calls.sort_values("_t").groupby("pid").tail(1)
                     .set_index("pid")[["callcode", "_t"]])
            _rv["callcode"] = _rv["pid"].map(_last["callcode"]).fillna("")
            _rv["last_submission_time"] = (_rv["pid"].map(_last["_t"])
                                           .dt.tz_localize(None).dt.strftime("%Y-%m-%d %H:%M")
                                           .fillna(""))
            _rv["reactivated_at"] = pd.to_datetime(_rv["reactivated_at"]).dt.strftime("%Y-%m-%d %H:%M")
            _rv["minutes_waiting"] = _rv["minutes"].astype("Int64")
            _rv = _rv.sort_values("minutes_waiting", ascending=False)
            _rv = _rv.rename(columns={"callcode": "last_callcode", "enumerator": "last_enumerator"})
            # Just what the enumerator needs: the pid, the supervisor's note, and
            # the last real call (its code and when).
            _rcols = [c for c in ["pid", "supervisor_note", "last_callcode",
                                  "last_submission_time"] if c in _rv.columns]
            ch.dataframe(_rv[_rcols], width="stretch", hide_index=True)
        open_cases = open_cases[~open_cases["pid"].isin(_react["pid"])]

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
            "Keep calling": ["pid", "ordering", "shifts_to_try",
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
            # Revised: no status (always ACTIVE), no callback_when / callback_due
            # (a hold or drop carries no appointment, so they were always blank),
            # rsd_reason + last_comment merged into comments.
            "Resume":       ["pid", "callcode", "active_partialsave", "tab_id",
                             "last_section", "total_attempts", "times_dropped",
                             "times_rescheduled", "times_noanswer", "times_off",
                             "shifts_to_try", "days_open", "last_contact_date",
                             "last_submission_time", "enumerator", "comments"],
            # Revised: no status (the callcode says it), no callback_when (callbacks
            # never come here, so always blank), no active_partialsave (held
            # partials go to Resume, so always No), comments merged. No
            # review_reason or numbers_tried_of_available either.
            # ever_picked_up and the times_* counts as in Resume; the last call
            # (who, and its code) at the end, before the comments.
            "Review":       ["pid", "shifts_to_try", "total_attempts", "ever_picked_up",
                             "times_dropped", "times_rescheduled", "times_noanswer",
                             "times_off", "days_open",
                             "last_contact_date", "last_submission_time",
                             "enumerator", "callcode", "comments"],
        }

        # One queue section: heading, flags, table. A function so 🔎 To be
        # reviewed and 📵 To keep calling can be drawn at the bottom (below).
        def _render_queue(key, label):
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
                rows = review_resume_view(rows.sort_values("_active_first", ascending=False))
            # The supervisor's note (Supervisor review: keep trying / sent back)
            # sits right after the pid, so the enumerator reads it before
            # dialling. Only shown when some pid in this queue has one.
            # assigned_day (which call list: day 4 or 5) always leads, in front
            # of the pid, in every queue.
            _cols = ([c for c in ["assigned_day"] if c in rows.columns]
                     + [c for c in COLS[key] if c in rows.columns and c != "assigned_day"])
            if "supervisor_note" in rows.columns and rows["supervisor_note"].ne("").any():
                _cols.insert(_cols.index("pid") + 1, "supervisor_note")
            # Which rows to paint red, read BEFORE trimming the columns: the
            # follow-up queue no longer shows its overdue flag, only the colour.
            _red = (rows["overdue"].eq("Yes") if key == "Callback" and "overdue" in rows.columns
                    else None)
            # Each queue: heading, a flag or two for what needs attention, the
            # table. Explanations live in guide.html (the 📖 Guide tab). Counted BEFORE the
            # columns are trimmed, since some flags read columns not shown.
            _flags = queue_flags(key, rows, _red)
            rows = rows[_cols]
            if key in ("Keep calling", "Review", "Resume"):
                rows = rows.rename(columns={"callcode": "last_callcode",
                                            "enumerator": "last_enumerator"})
            st.markdown(f"#### {label} ({len(rows)})")
            if len(rows):
                flag_tiles(_flags)
                ch.dataframe(style_overdue(yesno(rows), mask=_red),
                             width="stretch", hide_index=True)
            else:
                st.caption("Nothing here right now.")

        # Open cases laid out down the page, one section per queue. Review and
        # Keep calling are left out here: they close the tab, after To be
        # assigned and Assigned, not called yet.
        for key, label in BUCKETS:
            if key not in ("Keep calling", "Review"):
                _render_queue(key, label)

        # non-exclusive overlay: WhatsApp outreach (these pids also appear above)
        if SHOW_WHATSAPP:
            wa = open_cases[open_cases["whatsapp"] == 1].copy()
            st.markdown(f"#### 📱 To be reached out on WhatsApp ({len(wa)})")
            if wa.empty:
                st.caption("Nothing here right now.")
            else:
                wa_cols = ["assigned_day", "pid", "enumerator", "status", "callcode", "action_bucket",
                           "total_attempts", "shifts_to_try", "numbers_tried_of_available",
                           "last_comment"]
                ch.dataframe(yesno(wa[[c for c in wa_cols if c in wa.columns]]),
                             width="stretch", hide_index=True)

    # Not yet assigned — recruited but never worked. Built from `summary`, so it
    # renders whether or not any calls exist. NOTE: the sample tab has no explicit
    # "assigned" column, so "no submission yet" is the proxy for unassigned.
    # case_state, not ever_attempted: a pid a supervisor closed with no call is
    # Archived, and must not reappear here as work to hand out.
    new_cases = summary[(summary["case_state"] == "Not assigned")
                        & (summary["eligible_to_call"] == 1)].copy()
    if base_pids is not None:
        new_cases = new_cases[new_cases["pid"].isin(base_pids)]
    # Assigned in the dashboard = handed out: it waits in the list below instead.
    new_cases = new_cases[~new_cases["pid"].isin(_handed)]
    st.markdown(f"#### 🆕 To be assigned ({len(new_cases)})")

    # Headline: what was skipped in call-list order. Measured over the WHOLE
    # roster, so a sidebar filter cannot hide what was already tried.
    _front = skipped_in_order(summary, handed=_handed)
    _n_skip = int(new_cases["pid"].isin(_front.index).sum())
    flag_tiles([(_n_skip, "skipped: should already have been tried", RED)])

    if new_cases.empty:
        st.caption("Nothing to assign.")
    else:
        # CALL-LIST ORDER: first attempts go strictly by `ordering`, so the top
        # row is always the next pid to hand out (blank orderings last).
        new_cases["days_waiting"] = days_waiting(new_cases, data_now(summary))
        new_cases = (with_list_order(new_cases)
                     .sort_values(["_ord", "_day", "_rec"], na_position="last")
                     .drop(columns=["_rec", "_day", "_ord"]))
        new_cases["skipped"] = [f"⚠ list is at #{int(_front[p])}" if p in _front.index else ""
                                for p in new_cases["pid"]]
        _red_skip = new_cases["skipped"].ne("")
        newcols = [c for c in ["assigned_day", "pid", "ordering", "skipped", "days_waiting",
                               "phone_sample_status", "own_phone", "phones_provided",
                               "rec_signup"]
                   if c in new_cases.columns]
        _show = yesno(new_cases[newcols])
        _show["days_waiting"] = new_cases["days_waiting"].astype("Int64")
        # Tick, choose the enumerator, Assign: recorded in the decisions log.
        assign.assign_panel(_show, summary, _handed, _enum_names,
                            enumerators.supervisors(enum_roster))

    # ---- assigned in the dashboard, not called yet ---------------------------
    _wait = _asg_checks.get("waiting", pd.DataFrame())
    if len(_wait) and base_pids is not None:
        _wait = _wait[_wait["pid"].isin(base_pids)]
    st.markdown(f"#### 📨 Assigned, not called yet ({len(_wait)})")
    if _wait.empty:
        st.caption("Nothing waiting.")
    else:
        _late = _wait["minutes"] > assign.LATE_MINUTES
        flag_tiles([(int(_late.sum()), f"not called within {assign.LATE_MINUTES} min", RED)])
        _w = _wait.merge(summary[[c for c in ["pid", "assigned_day", "ordering"]
                                  if c in summary.columns]], on="pid", how="left")
        _w["assigned_at"] = pd.to_datetime(_w["assigned_at"]).dt.strftime("%d %b %H:%M")
        _w["minutes_waiting"] = _w["minutes"].astype("Int64")
        _w = _w.sort_values("minutes_waiting", ascending=False)
        _wcols = [c for c in ["assigned_day", "pid", "ordering", "assigned_to",
                              "assigned_at", "minutes_waiting", "assigned_by", "reason"]
                  if c in _w.columns]
        ch.dataframe(style_rows(_w[_wcols], red=_w["minutes_waiting"] > assign.LATE_MINUTES),
                     width="stretch", hide_index=True)

    # 🔎 To be reviewed, then 📵 To keep calling: the last queues on the page.
    if not cases.empty:
        _render_queue("Review", "🔎 To be reviewed (hard to reach)")
        _render_queue("Keep calling", "📵 To keep calling")

    with st.expander(f"See raw submissions ({len(subs)} rows, one per call attempt)"):
        ch.dataframe(subs, width="stretch", hide_index=True)

# ============================================================================
# TAB — PARTIAL SAVES (which tablet is holding each half-finished interview)
# ----------------------------------------------------------------------------
# A partial save lives on the DEVICE it was taken on, so it can only be resumed
# from that tablet. This tab answers the operational question: how many are out
# there, which tablet is each sitting on, and who last touched it.
# ============================================================================
with tab_attend:
    # Planned (schedule tab) vs checked in (start-of-day form) vs the lists
    # actually called, for one day. Whole roster: the filters at the top do
    # not apply. See attendance.py.
    st.subheader("🗓️ Attendance and lists")
    _raw_chk, _raw_sch, _ameta = attendance.load_attendance()
    if not _ameta["ok"]:
        st.warning(f"Check-in Sheet: {_ameta['msg']}")
    elif _ameta["msg"]:
        st.caption(f"Check-in Sheet: {_ameta['msg']}")
    _sch = attendance.clean_schedule(_raw_sch, name_map)
    _chk = attendance.clean_checkins(_raw_chk, name_map)
    _today = pd.Timestamp.now(tz="UTC").date()        # Sierra Leone = GMT
    _days = sorted(set(_sch["date"]) | set(_chk["date"]) | {_today})
    _past = [d for d in _days if d <= _today]
    _day = st.selectbox("Day", _days, index=_days.index(_past[-1] if _past else _days[0]),
                        format_func=lambda d: f"{d:%a %d %b}" + (" (today)" if d == _today else ""),
                        key="att_day")
    _pid_list = (summary.drop_duplicates("pid").set_index("pid")["assigned_day"]
                 if "assigned_day" in summary.columns else pd.Series(dtype=str))
    _calls = attendance.calls_of_day(subs, _day, _pid_list)
    _rows = attendance.day_table(_day, _sch, _chk, _calls,
                                 known=set(name_map.values()) if name_map else None)
    if _rows.empty:
        st.info("Nobody planned, checked in or calling on this day.")
    else:
        # One tile per planned shift: checked in / expected. Green = everyone,
        # orange = some, red = nobody, grey = the shift has not started yet.
        # Then the one count that needs action: people on the wrong list.
        _plan = _rows[_rows["planned_list"] != ""]
        _now = pd.Timestamp.now(tz="UTC").tz_localize(None)

        def _start_min(shift):
            m = re.match(r"\s*(\d{1,2}):(\d{2})", str(shift))
            return int(m[1]) * 60 + int(m[2]) if m else 24 * 60
        _tiles = []
        for _sft in sorted(_plan["shift"].unique(), key=_start_min):
            _g = _plan[_plan["shift"] == _sft]
            _n, _k = len(_g), int((_g["checked_in"] == "✅").sum())
            _started = _day < _today or (_day == _today
                                         and _now.hour * 60 + _now.minute >= _start_min(_sft))
            _col = (GREY if not _started else GREEN if _k == _n else RED if _k == 0 else ORANGE)
            _hint = (f"starts {str(_sft).split('–')[0]}" if not _started
                     else "everyone in" if _k == _n else f"{_n - _k} not checked in")
            _tiles.append((f"{_k}/{_n}", f"Shift {_sft}", _col, _hint))
        _tiles.append((int(_rows["flag"].str.contains("another list").sum()),
                       "doing another list", ORANGE, "see the list under the table"))
        flag_tiles(_tiles)

        # The day's assignment: who should be on which list, per shift, and who
        # is in (✅), not in yet (⏳) or calling another list (🟠).
        st.markdown("#### 📋 Assignment")
        _asg = attendance.assignment(_rows)
        if _asg.empty:
            st.caption("Nobody is planned on this day.")
        else:
            _asg["_o"] = _asg["shift"].map(_start_min)
            ch.dataframe(_asg.sort_values(["_o", "list"]).drop(columns="_o"),
                         hide_index=True, width="stretch",
                         column_config={"people": st.column_config.Column(
                             help="✅ checked in · ⏳ not checked in · 🟠 calling another list",
                             width="large")})
        st.markdown("#### 👥 Everyone today")
        ch.dataframe(_rows, hide_index=True, width="stretch",
                     column_config={"late_min": st.column_config.NumberColumn(format="%d"),
                                    "flag": st.column_config.Column(
                                        help="🔴 no check-in · 🟠 calls on another list · "
                                             "🔵 not on the schedule · 🟡 late · "
                                             "⚪ name not in the enumerators tab")})
        _off = attendance.off_list_calls(_rows, _calls)
        with st.expander(f"🟠 Calls on another list ({len(_off)})"):
            st.caption("Each call on a pid whose list differs from the caller's planned "
                       "list. A callback, a case sent back from Supervisor review or a "
                       "pid you assigned can be a good reason.")
            ch.dataframe(_off, hide_index=True, width="stretch")

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
        ch.dataframe(by_tab, width="stretch", hide_index=True)

        # ---- one row per partial save ---------------------------------------
        st.markdown("#### 📄 Every partial save")
        pcols = [c for c in ["pid", "tab_id", "enumerator", "last_submission_time",
                             "current_status", "current_callcode",
                             "callback_when", "callback_due", "action_bucket",
                             "last_section", "total_attempts", "days_open",
                             "route_recruited", "last_comment"]
                 if c in ps.columns]
        ch.dataframe(ps[pcols].sort_values(["tab_id", "last_submission_time"],
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
        # One section per way of closing, most common outcome first. A pid
        # that completed counts as completed even if rung again afterwards
        # (is_complete), whatever its last callcode says.
        _cc = arch["callcode"].astype(str).str.strip()
        _done = pd.to_numeric(arch.get("is_complete", 0), errors="coerce").fillna(0) == 1
        _kind = _cc.where(~_done, "1")
        INACTIVE_TYPES = [("1", "✅ Completed"), ("0_R", "Refusal (0_R)"),
                          ("0_UN", "Ineligible (0_UN)"),
                          ("0_IN", "Incorrect respondent (0_IN)"),
                          ("0_WN", "Wrong number (0_WN)"), ("0_OC", "Other contact (0_OC)"),
                          ("0_NA", "No answer — supervisor (0_NA)"),
                          ("0_OF", "Phone off — supervisor (0_OF)"),
                          ("0_D", "Keeps dropping — supervisor (0_D)"),
                          ("0_SC", "Keeps rescheduling — supervisor (0_SC)")]
        _known = {c for c, _ in INACTIVE_TYPES}
        _groups = INACTIVE_TYPES + [("_other", "Other closure")]
        for _code, _label in _groups:
            _mask = (~_kind.isin(_known)) if _code == "_other" else _kind.eq(_code)
            _g = arch[_mask]
            if _g.empty:
                continue
            st.markdown(f"#### {_label} ({len(_g)})")
            ch.dataframe(yesno(_g[acols]), width="stretch", hide_index=True)

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
    ch.dataframe(pd.DataFrame([
        {"what": "Call attempts", "origin": _a["origin"], "tab": _a["tab"],
         "gid": _a["gid"], "rows": _a["rows"], "read at": _a["read"]},
        {"what": "Roster (data entry)", "origin": _s["origin"], "tab": _s["tab"],
         "gid": _s["gid"], "rows": _s["rows"], "read at": _s["read"]},
        {"what": "Supervisor decisions",
         "origin": "Google Sheet (service account)" if decisions_meta.get("ok") else "⚠️ not read",
         "tab": review.TAB, "gid": "", "rows": decisions_meta.get("rows", 0),
         "read at": decisions_meta.get("msg", "") or "ok"},
        {"what": "Enumerator names",
         "origin": "Google Sheet" if enum_meta.get("rows") else "⚠️ not read — names shown as typed",
         "tab": enumerators.TAB, "gid": "", "rows": enum_meta.get("rows", 0),
         "read at": enum_meta.get("msg", "") or "ok"},
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
    ch.dataframe(pd.DataFrame(_rows), width="stretch", hide_index=True)
    st.caption("Every figure on this page is denominated on the **Eligible - signed up** "
               "pool. Refusals and ineligibles cannot be called, so they are excluded — "
               "they are counted here so the three add back up to everyone recruited.")

    if _n_inel:
        st.markdown(f"**Why those {_n_inel} are ineligible** — determinants on the data "
                    "entry tab. They **overlap**, so they do not sum to the total.")
        _why = recruit_exclusions(
            summary_full[_status == "Not eligible"], stage=1).sum()
        _why = _why[_why > 0].sort_values(ascending=False)
        ch.dataframe(pd.DataFrame({"determinant": _why.index, "pids": _why.values}),
                     width="stretch", hide_index=True)

    st.markdown("**Rows excluded from every count**")
    _strays = int(pd.to_numeric(summary.get("calls_after_complete", 0),
                                errors="coerce").fillna(0).sum())
    ch.dataframe(pd.DataFrame([
        {"excluded": "Test submissions (enum_name = Testing)", "rows": _a["dropped_test"]},
        {"excluded": "Calls logged after the pid had completed", "rows": _strays},
        {"excluded": "Attempts on pids with no roster row", "rows": n_offroster_cases},
    ]), width="stretch", hide_index=True)

    st.markdown("**Checks**")
    _rc = int(pd.to_numeric(summary.get("recontacted_after_complete", 0),
                            errors="coerce").fillna(0).sum())
    _ac_n = int(pd.to_numeric(summary.get("recontacted_after_close", 0),
                              errors="coerce").fillna(0).sum())
    _unknown = (enumerators.unknown_names(subs, name_map) if name_map else {})
    _checks = [
        {"check": "Signed-up rows that also carry an ineligibility flag",
         "count": n_conflict, "verdict": "OK" if not n_conflict else "⚠️ check data entry"},
        {"check": "Completed, then called again anyway",
         "count": _rc, "verdict": "OK" if not _rc else "⚠️ see Calls summary"},
        {"check": "Closed by a supervisor, then logged against anyway",
         "count": _ac_n, "verdict": "OK" if not _ac_n else "⚠️ see Calls summary"},
        {"check": "Pids with attempts but no roster row",
         "count": n_offroster_cases, "verdict": "OK" if not n_offroster_cases else "older sample"},
        {"check": "Names in submissions not on the enumerators tab",
         "count": len(_unknown),
         "verdict": ("OK" if not _unknown else "; ".join(f"{k} ({v})" for k, v in
                                                    list(_unknown.items())[:8])
                     + (" …" if len(_unknown) > 8 else ""))},
    ]
    ch.dataframe(pd.DataFrame(_checks), width="stretch", hide_index=True)

    st.caption(f"In view now: **{len(summary_view)} pids**, **{len(cases)} cases**, "
               f"**{len(subs)} attempt rows** (after any sidebar filters). "
               "Run `CHECK.bat` for the full validation suite.")
