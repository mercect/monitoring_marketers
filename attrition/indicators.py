# indicators.py — the PI dashboard's two views: Recruitment and Attrition.
# -----------------------------------------------------------------------------
# Lives in its own module because it is rendered by the Principal Investigators
# dashboard (pi_app.py). The call-tracking monitor (app.py) no longer shows it.
#
# Two entry points, one per tab in pi_app.py:
#   render_indicators(...) — Recruitment: the rate at both eligibility stages.
#   render_attrition(...)  — Attrition: both definitions and their dispositions.
# Each owns its own route filter (separate widget keys), so a PI can narrow one
# tab without silently moving the numbers on the other.
# -----------------------------------------------------------------------------
import pandas as pd
import streamlit as st

from rollup import (recruit_eligible, screen_out_masks, screen_out_sources,
                    PARTIALSAVE_SECTIONS,
                    _signup_class, _first_col, _s, ELIG_FLAG_LABELS,
                    SIGNUP_STATUS_COLS, NO_PHONE, NO_PHONE_SUBCATS,
                    STAGE_CALLS, STAGE_CALLS_V2, STAGE2_CALLCODES,
                    STAGE2_V2_CALLCODES, CLOSING_CALLCODE_LABELS)


# Both views draw the same shape of table, so the rate-row highlight and the
# fit-to-rows height live here rather than being redefined per view.
RATE_CSS = "background-color: #E7F0F9; color: #0B5394; font-weight: 700;"


def _fit(df):
    """Height that shows EVERY row without an inner scrollbar.

    Streamlit's grid defaults to ~9 visible rows and scrolls the rest inside
    itself, which hid the Stage II call codes at the bottom of the screen-out
    table. 35px per row plus the header, matching the theme's default row
    height."""
    return 35 * (len(df) + 1) + 3


def _rate_style(tbl, rate_row):
    """The rate row bold and colour-banded; every other row plain."""
    return tbl.style.apply(
        lambda r: [RATE_CSS if r.iloc[0] == rate_row else ""] * len(r), axis=1)


def resume_sections(subs):
    """pid -> the DEEPEST section a held partial ever reached, as a label.

    `rs_section` is written on the drop / reschedule notification that holds a
    partial, and it is the only place the depth survives: the Long Survey
    submission carrying those answers is not always in the export (EV87199A2
    reached Section P with no answer column filled in). So the notification is
    the evidence, and the max across a pid's submissions is how far they got —
    `last_section` is only the MOST RECENT call, which is usually shallower."""
    if "rs_section" not in subs.columns:
        return {}
    deepest = {}
    for pid, v in zip(subs["pid"], subs["rs_section"]):
        v = str(v).strip()
        if v.isdigit() and int(v) > deepest.get(pid, -1):
            deepest[pid] = int(v)
    return {pid: PARTIALSAVE_SECTIONS.get(str(n), f"section {n}")
            for pid, n in deepest.items()}


def _route_filter(summary, route_col, label, key):
    """Draw a route multiselect and return the frame it narrows `summary` to.

    Each view passes its own `key`, so the two tabs hold independent selections —
    Streamlit renders every tab on each run, and a shared key would make filtering
    one tab silently move the other tab's numbers."""
    routes = (sorted(x for x in summary[route_col].dropna().unique() if str(x).strip())
              if route_col else [])
    pick = st.multiselect(label, routes, default=routes, key=key) if routes else []
    if route_col and pick and len(pick) != len(routes):
        return summary[summary[route_col].isin(pick)]
    return summary


def submission_days(subs):
    """Every calendar day that carries a submission, oldest first.

    The enumerator's own `today` leads; SubmissionDate is the fallback so a row
    with a blank `today` still lands on a day rather than vanishing."""
    d = _s(subs, "today").astype(str).str.strip()
    fallback = _s(subs, "SubmissionDate").astype(str).str.strip().str.slice(0, 10)
    day = d.where(d != "", fallback)
    return day, sorted(x for x in day.unique() if x)


def last_submission_day(subs):
    """pid -> the day of its LAST submission.

    The last submission is the one that produced the pid's current state, so it
    is the date an outcome belongs to: the day it completed, or the day it was
    closed."""
    day, _ = submission_days(subs)
    order = _s(subs, "SubmissionDate").astype(str)
    tmp = pd.DataFrame({"pid": subs["pid"], "day": day, "_o": order})
    tmp = tmp.sort_values("_o").drop_duplicates("pid", keep="last")
    return dict(zip(tmp["pid"], tmp["day"]))


def _day_picker(subs, key):
    """Multiselect of submission days -> the set of pids resolved on those days.

    Returns None when every day is selected, which means "do not filter at all"
    — distinct from an empty set, which would mean "nothing qualifies".

    Deliberately does NOT narrow the frame it is used with. Filtering the base
    by call day would delete every never-called pid and every refusal from the
    denominator (they have no submission day at all), which sends the rate to
    100% and answers a question nobody asked. The base stays fixed; only which
    outcomes fall inside the window moves."""
    day, days = submission_days(subs)
    if not days:
        return None
    pick = st.multiselect("Filter by day of submission", days, default=days, key=key)
    if not pick or len(pick) == len(days):
        return None
    last = last_submission_day(subs)
    return {pid for pid, d in last.items() if d in pick}


def render_indicators(summary, route_col):
    """Draw the whole Indicators view.

    summary   = one row per pid (sample tab rolled up with the attempts)
    route_col = name of the route column on that frame, or None if absent

    Deliberately reads the UNFILTERED summary: the view has its own route
    filter, so it always reports over the whole sample unless a PI narrows it.
    """
    st.subheader("📊 Indicators")

    # ---- Recruitment (roster-level, with its own route filter) ------------------
    st.markdown("### Recruitment")
    recruit_base = _route_filter(summary, route_col,
                                 "Filter recruitment by route", "rec_route")
    status_col = _first_col(recruit_base, SIGNUP_STATUS_COLS)
    if status_col:
        # Sign-up status is read off `phone_sample_status` (2026-08 data-entry
        # sheet) or `rec_signup` (earlier roster). Every eligibility determinant
        # is its own variable on the sample tab and is applied here, not read off
        # the status — except that `phone_sample_status = "Not eligible"` says
        # only that some determinant fired, so those rows are classed as sign-ups
        # and then removed by the determinants themselves.
        sg = _signup_class(recruit_base)
        signed, refusal, missing = (sg == "signup"), (sg == "refusal"), (sg == "missing")
        n_ref, n_miss = int(refusal.sum()), int(missing.sum())

        # The rate line is repeated inside each subsection rather than stated once
        # up top, so a stage can be read (or screenshotted) on its own.
        FORMULA = ("**Recruitment rate = Eligible Signed Up / (Eligible Signed Up + Refusals)**")
        RATE_ROW = "recruitment rate"

        n_signed = int(signed.sum())

        # ---- trial-arm split -------------------------------------------------
        # `pitch` is the recruitment arm. It reaches this view only because
        # pi_app.py asks load_all() to keep it (rollup.HIDE_FROM_DASHBOARD keeps
        # it off the call-tracking monitor). Read from the data rather than
        # hardcoded, so a renamed or third arm shows up instead of vanishing.
        PITCH = "pitch"
        arms = (sorted(x for x in recruit_base[PITCH].dropna().unique() if str(x).strip())
                if PITCH in recruit_base.columns else [])

        def _slices():
            """(column name, frame) for the aggregate and then one per arm."""
            out = [("all", recruit_base)]
            out += [(a, recruit_base[recruit_base[PITCH] == a]) for a in arms]
            return out

        def _rate_figures(df, stage):
            """The four figures of the rate table for any sub-frame.

            Recomputed per arm rather than apportioned from the total: refusals
            and the eligibility screen both have to be re-counted inside the arm,
            and a rate is not additive across slices."""
            n_el = int(recruit_eligible(df, stage=stage).sum())
            n_r = int((_signup_class(df) == "refusal").sum())
            den = n_el + n_r
            return [n_el, n_r, den, f"{round(100 * n_el / den)}%" if den else "0%"]

        def _rate_table(stage):
            """Rate rows down the page, one column for the aggregate and one per arm."""
            data = {"": ["Signed Up", "refusals", "total eligible sign ups", RATE_ROW]}
            for name, df in _slices():
                data[name] = [str(v) for v in _rate_figures(df, stage)]
            return pd.DataFrame(data)

        def _screened_out(stage):
            """Why sign-ups fall out of THIS stage's base, one row per reason,
            split by which moment identified it and then by trial arm.

            Rows come from rollup.screen_out_masks(), the same thing eligibility
            is computed from, so nothing can screen a person out without showing
            up here.

            `no phone number` is a GROUP: the parent row is the UNION of its four
            sub-categories. They overlap, so the children need not add up to it.
            Reasons overlap across rows too (one person can be both underage and
            have no phone), so no column sums to the number screened out. And one
            determinant can be established at BOTH moments for the same pid, so
            the two `identified at` columns must not be added together either."""
            NA = "—"
            src_of = screen_out_sources(stage)

            def _counts(df):
                """reason -> (recruitment, phone survey, either) over df's sign-ups."""
                masks = screen_out_masks(df, stage)
                sgd = _signup_class(df) == "signup"
                out = {r: (int((rec & sgd).sum()), int((call & sgd).sum()),
                           int(((rec | call) & sgd).sum()))
                       for r, (rec, call) in masks.items()}
                kids = [masks[c] for c in NO_PHONE_SUBCATS if c in masks]
                rec_any, call_any = kids[0][0].copy(), kids[0][1].copy()
                for rec, call in kids[1:]:
                    rec_any, call_any = rec_any | rec, call_any | call
                out[NO_PHONE] = (int((rec_any & sgd).sum()),
                                 int((call_any & sgd).sum()),
                                 int(((rec_any | call_any) & sgd).sum()))
                return out

            masks0 = screen_out_masks(recruit_base, stage)
            reasons = ([NO_PHONE]
                       + [c for c in NO_PHONE_SUBCATS if c in masks0]
                       + [r for r in masks0 if r not in NO_PHONE_SUBCATS])
            counts = {name: _counts(df) for name, df in _slices()}

            data = {
                "excluded for": reasons,
                "identified at recruitment": [
                    str(counts["all"][r][0]) if src_of[r][0] else NA for r in reasons],
            }
            # Only Stage II can identify anyone by call outcome, so at Stage I
            # that column would be a full height of "—" and is left off entirely.
            if stage >= 2:
                data["identified at phone survey"] = [
                    str(counts["all"][r][1]) if src_of[r][1] else NA for r in reasons]
            # Share of ALL sign-ups carrying this reason — the row total (either
            # moment), over the same constant denominator on every row, so the
            # rows are comparable. Not a share of those screened out, and the
            # column does not sum to 100%: the reasons overlap.
            data["% of sign-ups"] = [
                (f"{round(100 * counts['all'][r][2] / n_signed)}%" if n_signed else "0%")
                for r in reasons]
            # Arm columns split the row TOTAL (either moment), so a pid found
            # ineligible at both moments is still counted once here.
            for arm in arms:
                data[arm] = [str(counts[arm][r][2]) for r in reasons]

            # Distinct pids, not a column total: the reasons overlap, so one
            # person can appear on several rows and no column adds up to this.
            hit = None
            for rec, call in masks0.values():
                m = rec | call
                hit = m if hit is None else (hit | m)
            return pd.DataFrame(data), int((hit & signed).sum())

        def _render_stage(stage, heading, definition):
            """One subsection: heading, formula, definition, the rate table, and
            the screen-out breakdown for THIS stage."""
            st.markdown(heading)
            st.markdown(FORMULA)
            st.markdown(definition)
            n_el = int(recruit_eligible(recruit_base, stage=stage).sum())
            tbl = _rate_table(stage)
            st.dataframe(_rate_style(tbl, RATE_ROW),
                         width="stretch", hide_index=True, height=_fit(tbl))
            out, n_distinct = _screened_out(stage)
            st.markdown(f"**Who is screened out of the base** — {n_distinct} distinct pids")
            st.dataframe(out, width="stretch", hide_index=True, height=_fit(out))
            st.caption(
                f"Counted over the {n_signed} sign-ups, which is also what **% of "
                f"sign-ups** divides by. Reasons **overlap**, so neither the counts nor "
                f"the percentages add up to the {n_distinct} screened out of this "
                "stage's base.")

        # ---- Subsection 1 — Stage I ------------------------------------------
        _render_stage(
            1,
            "#### Recruitment rate — Stage I (after data entry)",
            "Eligible sign-ups exclude anyone screened out by a recruitment determinant "
            "on the sample tab — no phone number, no number of their own, underage, "
            "living outside the Western Area, deaf or mute, a language issue, or seated "
            "in row X. The determinants actually present on your sheet are listed in the "
            "breakdown below.",
        )

        # ---- Subsection 2 — Stage II -----------------------------------------
        _render_stage(
            2,
            "#### Recruitment rate — Stage II (after phone calls)",
            "Every Stage I determinant above, **plus** what the calls established: "
            "`0_WN` wrong number and `0_IN` incorrect respondent (no working way to "
            "reach this respondent), and `0_UN` — ineligible found on the call, filed "
            "under the determinant its reason variable names (`no_phone_ineligible`, "
            "`d02_check`, `d04_yn`). Two call outcomes are **not** screen-outs here: "
            "`0_NA` no answer, and `0_OF` phone off — both leave the case unresolved "
            "rather than establishing ineligibility. **Stage II v2 below is identical "
            "except that it does count `0_OF`.**",
        )

        # ---- Subsection 3 — Stage II v2 --------------------------------------
        _render_stage(
            3,
            "#### Recruitment rate — Stage II v2 (phone off counted as unreachable)",
            "**The only difference from Stage II is `0_OF` phone off.** Stage II treats "
            "a phone that is switched off or out of coverage as *unresolved* — it may "
            "come back on — so those pids stay in the base. Stage II v2 treats it as "
            "*never reachable* and screens them out, adding the `↳ phone non-reachable "
            "(0_OF)` row below. Every other determinant is identical, so the gap between "
            "the two rates is exactly the cost of that one judgement call.",
        )

        st.caption(f"Outside every base at both stages: **{n_miss} missing** sign-up "
                   "status.")
        _absent = [(label, cols) for label, cols in ELIG_FLAG_LABELS.items()
                   if not _first_col(recruit_base, cols)]
        if _absent:
            st.warning("Not on the sample tab, so contributing **0 exclusions**: "
                       + ", ".join(f"{label} (`{cols[0]}`)" for label, cols in _absent)
                       + ". Add the column and it is applied automatically.")
    else:
        st.info("No sign-up status column found on the sample tab — expected one of "
                + ", ".join(f"`{c}`" for c in SIGNUP_STATUS_COLS) + ".")


def render_attrition(summary, subs, route_col):
    """Draw the Attrition view — its own tab in pi_app.py.

    summary   = one row per pid (sample tab rolled up with the attempts)
    route_col = name of the route column on that frame, or None if absent

    Two subsections, each pinned to one of the Recruitment tab's call-stage
    eligible bases, so the two views cannot drift apart: A reports over Stage II,
    B over Stage II v2. Those two bases differ only in whether `0_OF` phone off
    counts as never-reachable, so the gap between the rates is exactly the cost
    of that one judgement.

    Carries its own route filter rather than inheriting the Recruitment tab's —
    the two views are separate tabs, so a filter set on one must not move the
    other's numbers from off screen."""
    st.subheader("📉 Attrition")
    base_df = _route_filter(summary, route_col,
                            "Filter attrition by route", "att_route")
    # A set of pids resolved inside the chosen days, or None for "all days".
    # It never touches base_df — see _day_picker on why the base must not move.
    window = _day_picker(subs, "att_day")
    # pid -> deepest section a held partial reached. Presence of a section is
    # what separates `incomplete` from `attrited`: a partial was saved at a named
    # point in the instrument, so there is an interview to resume.
    resumed = resume_sections(subs)

    # The asterisk ties the formula to the footnote each subsection prints under
    # it — what "eligible base" actually excludes differs between A and B, and
    # that is the only thing separating them.
    FORMULA = r"**Attrition rate = attrited / total eligible base**\*"
    # No separate "attrition rate" row: the `attrited` row's % cell IS that rate,
    # against the same denominator, so a rate row only restated it.
    BASE_ROW = "total eligible base"
    OTHER_ROW = "↳ other 0_ closure"

    # The three buckets are the point of this table, so each gets its own colour:
    # green = finished, red = lost, amber = still open. Both background AND text
    # colour are set, so the rows stay legible whichever theme the viewer uses.
    # The `↳` children of `attrited` stay plain — colouring them too would
    # bury the parent they add up to.
    BUCKET_CSS = {
        "completed": "background-color: #E6F4EA; color: #14682C; font-weight: 700;",
        "incomplete": "background-color: #E8F0FE; color: #174EA6; font-weight: 700;",
        "attrited": "background-color: #FCE8E6; color: #B3261E; font-weight: 700;",
        "in progress": "background-color: #FEF3E0; color: #8A5300; font-weight: 700;",
    }

    def _bucket_style(tbl):
        """Colour the three bucket rows; leave children and the base row plain."""
        return tbl.style.apply(
            lambda r: [BUCKET_CSS.get(r.iloc[0], "")] * len(r), axis=1)

    # Same trial-arm split as the Recruitment tab, from the same `pitch` column
    # pi_app.py keeps for this dashboard. Read from the data, not hardcoded.
    PITCH = "pitch"
    arms = (sorted(x for x in base_df[PITCH].dropna().unique() if str(x).strip())
            if PITCH in base_df.columns else [])

    def _slices():
        """(column name, frame) for the aggregate and then one per arm."""
        return ([("all", base_df)]
                + [(a, base_df[base_df[PITCH] == a]) for a in arms])

    def _counts(df, stage, screened_out_codes, window=None):
        """row label -> count, over this frame's slice of the stage's base.

        A closing `0_` code does one of two things, decided by the STAGE's base:
        either it screened the pid out (not in the base at all, so not a loss),
        or the pid is in the base and the closure is a loss. That is the whole
        difference between A and B for `0_OF` — Stage II keeps those pids, so a
        switched-off closure is attrition there; Stage II v2 removes them, so it
        cannot be.

        completed / incomplete / attrited / in progress are a partition, not four
        overlapping filters — `in progress` is the remainder, so they always sum
        to the base.
        The attrited children are mutually exclusive (one callcode per pid), so
        they sum to the attrited parent exactly.

        `window` (a set of pids resolved inside the selected days) narrows which
        outcomes COUNT, never who is in the base. A pid resolved outside the
        window is not deleted — it falls back into `in progress`, because as far
        as the selected days are concerned it has not been resolved yet. So the
        base holds still and the three buckets still sum to it."""
        base = df[recruit_eligible(df, stage=stage)]
        cc = base["current_callcode"].astype(str).str.strip().str.upper()
        done = base["is_complete"] == 1
        closed = cc.str.startswith("0_") & ~cc.isin(screened_out_codes) & ~done
        # An interview that got somewhere and was saved is INCOMPLETE, not lost:
        # a resume section means there is a real partial to go back to. Note the
        # wrong-respondent codes never reach here — they are screened out of the
        # base upstream — so a partial belonging to the wrong person cannot land
        # in this row.
        part = closed & base["pid"].isin(resumed)
        lost = closed & ~part
        if window is not None:
            inside = base["pid"].isin(window)
            done, part, lost = done & inside, part & inside, lost & inside
        counted = [c for c in CLOSING_CALLCODE_LABELS
                   if c not in screened_out_codes]
        out = {"completed": int(done.sum()), "incomplete": int(part.sum())}
        # where each incomplete interview was left off, deepest section first
        secs = sorted({resumed[p] for p in base.loc[part, "pid"]})
        for sname in secs:
            out[f"↳ {sname}"] = int(
                (part & base["pid"].map(lambda x: resumed.get(x) == sname)).sum())
        out["attrited"] = int(lost.sum())
        for c in counted:
            out[f"↳ {c} ({CLOSING_CALLCODE_LABELS[c]})"] = int(
                (lost & (cc == c)).sum())
        # A 0_ code the form does not define — a supervisor can type one in.
        # Kept so the children can never fail to add up to the parent.
        out[OTHER_ROW] = int((lost & ~cc.isin(counted)).sum())
        out["in progress"] = int((~done & ~part & ~lost).sum())
        out[BASE_ROW] = len(base)
        return out

    def _render_def(stage, codes, heading, definition):
        """One subsection: heading, formula, definition, then the table.

        Each slice gets a count column and a **% column reading against its OWN
        base**, which is what makes the arms comparable: an arm with fewer
        eligible pids would otherwise look like less attrition purely for being
        smaller."""
        st.markdown(heading)
        st.markdown(FORMULA)
        # Which closures count as a loss under THIS definition. Built from the
        # same code list the rows are built from, so the sentence can never
        # advertise a reason the table does not count (or miss one it does).
        counted = [c for c in CLOSING_CALLCODE_LABELS if c not in codes]
        st.markdown(
            "**Counted as attrition:** "
            + ", ".join(f"{CLOSING_CALLCODE_LABELS[c]} (`{c}`)" for c in counted)
            + " — a case closed with any of these, without a completed interview "
            + "**and with no partial saved**. One that did save a partial is "
            + "`incomplete` above, not a loss.")
        st.caption(definition)      # the footnote the asterisk points at

        cols = [(name, _counts(df, stage, codes, window)) for name, df in _slices()]
        allc = dict(cols[0][1])
        # union across slices: an arm can hold a section the aggregate ordering
        # has not seen yet, and dropping it would break the children-sum check.
        for _, c in cols[1:]:
            for k in c:
                allc.setdefault(k, 0)
        rows = [r for r in allc if r != BASE_ROW]
        if not allc[OTHER_ROW]:
            rows.remove(OTHER_ROW)          # only shown when it actually happens
        rows.append(BASE_ROW)

        data = {"": rows}
        for name, c in cols:
            base = c[BASE_ROW]
            data[name] = [str(c.get(r, 0)) for r in rows]
            # "% of THIS column's eligible base" — so the arms stay comparable
            # when they differ in size. The `attrited` cell here is the attrition
            # rate for that column; the base row is 100% by construction.
            data[f"{name} %"] = [
                (f"{round(100 * c.get(r, 0) / base)}%" if base else "0%")
                for r in rows]

        tbl = pd.DataFrame(data)
        st.dataframe(_bucket_style(tbl),
                     width="stretch", hide_index=True, height=_fit(tbl))

    # ---- Subsection 1 — definition A -------------------------------------
    _render_def(
        STAGE_CALLS, STAGE2_CALLCODES,
        "#### Attrition A — broad (Stage II eligible base)",
        r"\*Reported over the **Stage II** eligible base — That is, excluding wrong "
        "numbers, incorrect respondent, other contact and other ineligibility reasons "
        "(underage, outside of western area, etc). They are not accounted as part of "
        "the eligible respondents here.",
    )

    # ---- Subsection 2 — definition B -------------------------------------
    _render_def(
        STAGE_CALLS_V2, STAGE2_V2_CALLCODES,
        "#### Attrition B — conservative (Stage II v2 eligible base)",
        r"\*Reported over the **Stage II v2** eligible base — the same exclusions as A, "
        "**plus** phone off (`0_OF`) counted as never reachable. Those pids are not "
        "accounted as part of the eligible respondents here either, so they cannot "
        "count as losses and `0_OF` has no row below — which is the only difference "
        "between A and B.",
    )
