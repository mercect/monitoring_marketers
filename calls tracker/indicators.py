# indicators.py — the Indicators tab (headline aggregates over the sample).
# -----------------------------------------------------------------------------
# Lives in its own module because it is rendered by the Principal Investigators
# dashboard (pi_app.py). The call-tracking monitor (app.py) no longer shows it.
# -----------------------------------------------------------------------------
import pandas as pd
import streamlit as st

from rollup import (summary_kpis, recruit_eligible, recruit_exclusions,
                    _signup_class, _first_col, ELIG_FLAG_LABELS,
                    SIGNUP_STATUS_COLS)


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
    rec_routes = (sorted(x for x in summary[route_col].dropna().unique() if str(x).strip())
                  if route_col else [])
    rec_pick = (st.multiselect("Filter recruitment by route", rec_routes, default=rec_routes,
                               key="rec_route") if rec_routes else [])
    recruit_base = summary
    if route_col and rec_pick and len(rec_pick) != len(rec_routes):
        recruit_base = summary[summary[route_col].isin(rec_pick)]
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

        st.markdown(
            "**Recruitment rate = Signed Up ÷ (Signed Up + Refusals)**, reported at two "
            "stages. Same formula throughout — only the eligibility screen widens:"
        )
        st.markdown(
            "- **Stage I — after data entry.** Eligible sign-ups exclude anyone screened "
            "out by a recruitment determinant on the sample tab — no phone number, no "
            "number of their own, underage, living outside the Western Area, deaf or mute, "
            "a language issue, seated in row X, or not a passenger card. The determinants "
            "actually present on your sheet are listed in the breakdown below."
        )
        st.markdown(
            "- **Stage II — after phone calls.** All of the above, **plus** pids the calls "
            "closed as `0_WN` wrong number, `0_IN` incorrect respondent, `0_NA` no answer "
            "or `0_OF` phone off — people who turned out never to be reachable."
        )
        st.markdown(
            "**Refusals stay in the denominator at both stages** (assumed they would have "
            "been eligible — they leave no phone or demographic data to screen on), so the "
            "two rates differ only through the numerator."
        )

        stages = []
        for stage, label in [(1, "Stage I — after data entry"),
                             (2, "Stage II — after phone calls")]:
            n_el = int(recruit_eligible(recruit_base, stage=stage).sum())
            den = n_el + n_ref
            stages.append({
                "stage": label,
                "eligible sign-ups": n_el,
                "refusals": n_ref,
                "base (denominator)": den,
                "recruitment rate": f"{round(100 * n_el / den)}%" if den else "0%",
            })
        for col, row in zip(st.columns(2), stages):
            col.metric(row["stage"], row["recruitment rate"],
                       help=f"{row['eligible sign-ups']} / {row['base (denominator)']}")
        st.dataframe(pd.DataFrame(stages), width="stretch", hide_index=True)

        # Why people fall out of the base, one row per determinant. Counted over
        # the sign-ups only: refusals and missing are never screened (see above).
        ex2 = recruit_exclusions(recruit_base, stage=2)[signed]
        brk = pd.DataFrame({
            "excluded for": list(ex2.columns),
            "applies from": ["Stage I"] * (len(ex2.columns) - 1) + ["Stage II"],
            "sign-ups excluded": [int(ex2[c].sum()) for c in ex2.columns],
        })
        st.markdown("**Who is screened out of the base**")
        st.dataframe(brk, width="stretch", hide_index=True)
        st.caption(
            f"Counted over the {int(signed.sum())} sign-ups; reasons **overlap**, so they "
            f"do not add up to the difference between the two bases. Outside every base: "
            f"**{n_miss} missing** sign-up status. Blank flags are **not** read as 0 — a "
            "respondent with no value recorded stays eligible."
        )
        _absent = [(label, cols) for label, cols in ELIG_FLAG_LABELS.items()
                   if not _first_col(recruit_base, cols)]
        if _absent:
            st.warning("Not on the sample tab, so contributing **0 exclusions**: "
                       + ", ".join(f"{label} (`{cols[0]}`)" for label, cols in _absent)
                       + ". Add the column and it is applied automatically.")
    else:
        st.info("No sign-up status column found on the sample tab — expected one of "
                + ", ".join(f"`{c}`" for c in SIGNUP_STATUS_COLS) + ".")

    # ---- Attrition (roster-level, shares the recruitment route filter) ----------
    st.divider()
    st.markdown("### Attrition")
    ak = summary_kpis(recruit_base)
    st.markdown("**Attrition rate = attrited ÷ eligible base.**  A pid is *attrited* when it "
                "is eligible and **closed (INACTIVE) without completing**. Never-called and "
                "in-progress pids are not counted. "
                "**A** = broad (base excludes demographic / no-phone ineligibles); "
                "**B** = conservative (base also excludes wrong-number, wrong-respondent, "
                "never-reached).")
    ac = st.columns(2)
    ac[0].metric("Attrition A (broad)",
                 f"{ak['def_a']['rate']}%  ({ak['def_a']['attrited']}/{ak['def_a']['base']})")
    ac[1].metric("Attrition B (conservative)",
                 f"{ak['def_b']['rate']}%  ({ak['def_b']['attrited']}/{ak['def_b']['base']})")

    def _disposition(df):
        cc = df["current_callcode"].astype(str).str.strip()
        su = df["current_status"].astype(str).str.upper()
        d = pd.Series("Other closure", index=df.index)
        d = d.mask(cc == "0_IN", "Incorrect respondent")
        d = d.mask(cc == "0_WN", "Wrong number")
        d = d.mask(cc == "0_R", "Refusal")
        d = d.mask(df["is_complete"] == 1, "Completed")
        d = d.mask(su.isin(["ACTIVE", "PENDING"]), "In progress")
        d = d.mask(df["ever_attempted"] == 0, "Not attempted")
        return d

    def _disp_summary(df):
        disp = _disposition(df)
        base = len(df)
        n_prog = int((disp == "In progress").sum())
        n_not = int((disp == "Not called").sum())
        resolved = disp[~disp.isin(["In progress", "Not called"])]
        vc = resolved.value_counts()
        d = vc.rename_axis("disposition").reset_index(name="pids")
        d["%"] = ((100 * d["pids"] / base).round().astype(int).astype(str) + "%") if base else "0%"
        return d, n_prog, n_not, base

    st.caption("Resolved disposition of the eligible base (% of eligible base). Attrition = "
               "the closure rows (Refusal / Wrong number / Incorrect respondent / Other closure).")
    la, lb = st.columns(2)
    for col, elig in [(la, "ineligible_type1"), (lb, "ineligible_type2")]:
        with col:
            st.markdown(f"**Attrition {'A' if elig.endswith('1') else 'B'} — eligible base**")
            d, n_prog, n_not, base = _disp_summary(recruit_base[recruit_base[elig] == 0])
            st.dataframe(d, width="stretch", hide_index=True)
            st.caption(f"Not counted: {n_prog} in progress · {n_not} not called (of {base} eligible).")
