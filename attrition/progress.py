# progress.py — the "In Progress" tab: day-to-day operational monitoring.
# -----------------------------------------------------------------------------
# The Recruitment and Attrition tabs answer "where does the study stand?".
# This one answers "where should the team put effort tomorrow?" — so it reads the
# ATTEMPTS (one row per call), not just the per-pid rollup, and slices them by
# day, survey type, time of day and stopping point.
#
# Scope note: the caseload section reports the pids Attrition A counts as
# `in progress` — Stage II eligible, not completed, not attrited — so the two
# tabs cannot disagree about who is still open.
# -----------------------------------------------------------------------------
import pandas as pd
import streamlit as st

from rollup import (is_attempt_row, recruit_eligible, drop_after_complete,
                    SHIFTS, PARTIALSAVE_SECTIONS, STAGE_CALLS,
                    STAGE2_CALLCODES, _s)
from indicators import _fit, _route_filter

# Shift bands come from rollup.SHIFTS so this view and the per-pid
# `shifts_covered` columns can never disagree about where a boundary falls.
SHIFT_LABELS = {
    "0730_0930": "A 07:30-09:30", "0930_1230": "B 09:30-12:30",
    "1230_1530": "C 12:30-15:30", "1530_1730": "D 15:30-17:30",
    "1730_2030": "E 17:30-20:30",
}
UNKNOWN = "unknown"


def _day(att):
    """Calendar day of the attempt: the enumerator's `today`, else the submission
    date. `today` is what the field team recognises, so it leads."""
    d = _s(att, "today").astype(str).str.strip()
    fallback = _s(att, "SubmissionDate").astype(str).str.strip().str.slice(0, 10)
    return d.where(d != "", fallback).replace("", UNKNOWN)


def _shift(att):
    """Time-of-day band the call started in, from `starttime`."""
    t = pd.to_datetime(_s(att, "starttime"), errors="coerce", utc=True)
    mins = t.dt.hour * 60 + t.dt.minute
    out = pd.Series(UNKNOWN, index=att.index)
    for key, (lo, hi) in SHIFTS.items():
        out = out.mask(mins.between(lo, hi, inclusive="left"), SHIFT_LABELS[key])
    return out


def _pivot(rows, cols, label):
    """Counts of rows x cols with a Total column, heaviest row first."""
    t = pd.crosstab(rows, cols)
    if t.empty:
        return pd.DataFrame({label: [], "Total": []})
    t = t.reindex(sorted(t.columns), axis=1)
    t["Total"] = t.sum(axis=1)
    return t.sort_values("Total", ascending=False).rename_axis(label).reset_index()


def render_progress(summary, subs, route_col):
    """Draw the In Progress view — its own tab in pi_app.py.

    summary = one row per pid; subs = the raw attempts export."""
    st.subheader("🔧 In Progress — daily monitoring")

    base_df = _route_filter(summary, route_col, "Filter by route", "prog_route")

    # Attempts, restricted to the pids in view and with post-completion calls
    # dropped — the same two rules the rollup applies, so counts reconcile with
    # the other tabs instead of quietly running higher.
    att = subs[is_attempt_row(subs)].copy()
    att = att[att["pid"].isin(set(base_df["pid"]))]
    att, _ = drop_after_complete(att, ["SubmissionDate"])
    if att.empty:
        st.info("No call attempts yet for the routes in view.")
        return

    att["day"] = _day(att)
    att["shift"] = _shift(att)
    att["cc"] = (_s(att, "callcode").astype(str).str.strip().str.upper()
                 .replace("", "(blank)"))
    att["done"] = _s(att, "is_complete").astype(str).str.strip() == "1"
    st.caption(f"{len(att)} call attempts across {att['day'].nunique()} day(s), "
               f"on {att['pid'].nunique()} pids in view.")

    # ---- 1. Day by day ------------------------------------------------------
    st.markdown("### Day by day")
    st.markdown("**What each day produced.** `completed` is interviews finished that "
                "day; `cumulative completed` is the running total — the line to watch "
                "for whether the pace is holding.")
    by_day = att.groupby("day").agg(
        attempts=("pid", "size"), pids_worked=("pid", "nunique"),
        completed=("done", "sum")).reset_index().sort_values("day")
    by_day["completed"] = by_day["completed"].astype(int)
    by_day["cumulative completed"] = by_day["completed"].cumsum()
    by_day["attempts per completed"] = [
        (round(a / c, 1) if c else "—")
        for a, c in zip(by_day["attempts"], by_day["completed"])]
    st.dataframe(by_day, width="stretch", hide_index=True, height=_fit(by_day))
    st.caption("`attempts per completed` is what a completion cost that day — rising "
               "means the cases still open are the harder ones, not that the team "
               "slowed down.")

    # ---- 2. Call codes by day ----------------------------------------------
    st.markdown("### Call codes by day")
    st.markdown("**Which outcomes dominate, and how the mix moves.** Ordered by total, "
                "so the top rows are where the effort is actually going.")
    t = _pivot(att["cc"], att["day"], "callcode")
    st.dataframe(t, width="stretch", hide_index=True, height=_fit(t))

    # ---- 3. By survey type --------------------------------------------------
    st.markdown("### By survey type")
    st.markdown("A `Long Survey` row is a real interview attempt; the notification and "
                "communications forms are follow-up paperwork. Splitting them keeps a "
                "day of notifications from reading as a day of calling.")
    t = _pivot(_s(att, "survey_type").replace("", UNKNOWN), att["cc"], "survey type")
    st.dataframe(t, width="stretch", hide_index=True, height=_fit(t))

    # ---- 4. By time of day --------------------------------------------------
    st.markdown("### By time of day")
    st.markdown("**When calls actually connect.** `completion rate` is completions ÷ "
                "attempts in that band — the band with the best rate is where an extra "
                "shift pays off.")
    sh = att.groupby("shift").agg(attempts=("pid", "size"),
                                  completed=("done", "sum")).reset_index()
    sh["completed"] = sh["completed"].astype(int)
    sh["completion rate"] = [f"{round(100 * c / a)}%" if a else "0%"
                             for c, a in zip(sh["completed"], sh["attempts"])]
    order = {v: i for i, v in enumerate(list(SHIFT_LABELS.values()) + [UNKNOWN])}
    sh = sh.sort_values(by="shift", key=lambda s: s.map(order).fillna(99))
    st.dataframe(sh, width="stretch", hide_index=True, height=_fit(sh))

    st.markdown("**Attempts per shift, by day** — which bands are actually being worked.")
    t = _pivot(att["shift"], att["day"], "shift")
    st.dataframe(t, width="stretch", hide_index=True, height=_fit(t))

    # ---- 5. Where people stop ----------------------------------------------
    st.markdown("### Where people stop")
    st.markdown("For calls that **dropped or were rescheduled**, the section the "
                "respondent had reached. A section that keeps recurring is a place the "
                "instrument is losing people, not a run of bad luck.")
    stopped = att[_s(att, "stop_reason").astype(str).str.strip() != ""].copy()
    if stopped.empty:
        st.info("No drop or reschedule submissions yet for the routes in view.")
    else:
        # `rs_section` is the coded section; `last_section` is free text and is
        # used only where the code is blank, so one row never answers twice.
        sec = _s(stopped, "rs_section").astype(str).str.strip().map(PARTIALSAVE_SECTIONS)
        fallback = _s(stopped, "last_section").astype(str).str.strip()
        stopped["section"] = sec.fillna(fallback.replace("", UNKNOWN))
        t = _pivot(stopped["section"],
                   _s(stopped, "stop_reason").astype(str).str.strip(),
                   "section reached")
        st.dataframe(t, width="stretch", hide_index=True, height=_fit(t))
        st.caption(f"{len(stopped)} drop / reschedule submissions.")

    # ---- 6. The open caseload ----------------------------------------------
    st.markdown("### The open caseload")
    elig = base_df[recruit_eligible(base_df, stage=STAGE_CALLS)]
    cc = elig["current_callcode"].astype(str).str.strip().str.upper()
    done = elig["is_complete"] == 1
    lost = cc.str.startswith("0_") & ~cc.isin(STAGE2_CALLCODES) & ~done
    open_pids = elig[~done & ~lost].copy()
    st.markdown(f"The **{len(open_pids)}** pids Attrition A counts as *in progress* — "
                "Stage II eligible, not completed, not closed. Same definition as that "
                "tab, so the two cannot disagree about who is still open.")
    if open_pids.empty:
        st.info("Nothing open in the routes in view.")
        return

    st.markdown("**Effort already spent, by current outcome.** `shifts covered` counts "
                "how many of the five bands have been tried: a pid low on that has "
                "cheap options left, one at 5 needs a different approach rather than "
                "another dial.")
    # A blank current_callcode means two different things and they must not share
    # a row: never dialled at all, versus dialled but the submission carried no
    # code. The attempt count is what separates them.
    _cc = cc[open_pids.index]
    _cc = _cc.mask(_cc == "", open_pids["total_attempts"].astype(int).map(
        lambda n: "not yet called" if n == 0 else "(no callcode recorded)"))
    eff = open_pids.assign(_cc=_cc)
    eff = eff.groupby("_cc").agg(
        pids=("pid", "size"),
        total_attempts=("total_attempts", "sum"),
        mean_attempts=("total_attempts", "mean"),
        mean_shifts_covered=("shifts_covered_n", "mean")).reset_index()
    eff["mean_attempts"] = eff["mean_attempts"].round(1)
    eff["mean_shifts_covered"] = eff["mean_shifts_covered"].round(1)
    eff = eff.rename(columns={"_cc": "current callcode"}).sort_values(
        "pids", ascending=False)
    st.dataframe(eff, width="stretch", hide_index=True, height=_fit(eff))

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Attempts per pid**")
        d = (open_pids["total_attempts"].astype(int).value_counts()
             .rename_axis("attempts").reset_index(name="pids").sort_values("attempts"))
        st.dataframe(d, width="stretch", hide_index=True, height=_fit(d))
    with c2:
        st.markdown("**Shifts still untried**")
        d = (open_pids["shifts_to_try"].value_counts()
             .rename_axis("shifts to try").reset_index(name="pids"))
        st.dataframe(d, width="stretch", hide_index=True, height=_fit(d))
    st.caption("Legend — A 07:30-09:30 · B 09:30-12:30 · C 12:30-15:30 · "
               "D 15:30-17:30 · E 17:30-20:30.")
