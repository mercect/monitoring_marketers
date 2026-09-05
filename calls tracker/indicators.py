# indicators.py — the Indicators tab (headline aggregates over the sample).
# -----------------------------------------------------------------------------
# Lives in its own module because it is rendered by the Principal Investigators
# dashboard (pi_app.py). The call-tracking monitor (app.py) no longer shows it.
# -----------------------------------------------------------------------------
import pandas as pd
import streamlit as st

from rollup import (summary_kpis, recruit_eligible, recruit_exclusions,
                    _signup_class, _first_col, ELIG_FLAG_LABELS,
                    SIGNUP_STATUS_COLS, STAGE2_CALLCODES, SIGNED_UP_STATUS,
                    CALL_OUTCOME_EXCLUSIONS, ineligible_columns)
from data_io import default_routes


def _formula(*lines):
    """Show the literal definition of the indicator above, as code.

    Built from the same constants the calculation uses, so the definition on
    screen cannot drift away from what the numbers actually do."""
    st.caption("**How it is calculated**")
    st.code(chr(10).join(lines), language="text")


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
    # Opens on DEFAULT_ROUTES (see data_io), same as the call-tracking sidebar,
    # so the two dashboards report the same population unless a PI widens it.
    rec_pick = (st.multiselect("Filter recruitment by route", rec_routes,
                               default=default_routes(rec_routes),
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
            "**Recruitment rate = Eligible sign-ups ÷ (Eligible sign-ups + Refusals).**"
        )
        st.markdown(
            "**Eligible sign-ups** are the people who signed up and were not screened out. "
            "Two kinds of thing screen someone out, and both count the same way — every "
            "one of them is itemised in **Who is screened out of the base** below:"
        )
        st.markdown(
            "- **Recruitment determinants**, from the sample tab — no phone number, no "
            "number of their own, underage, living outside the Western Area, deaf or mute, "
            "a language issue, seated in row X, or not a passenger card."
        )
        st.markdown(
            "- **Call outcomes** that closed the pid as never reachable or not the right "
            "person — `0_WN` wrong number, `0_IN` incorrect respondent, `0_NA` no answer, "
            "`0_OF` phone off."
        )
        st.markdown(
            "**Refusals stay in the denominator** — they leave no phone or demographic "
            "data to screen on, so they are assumed to have been eligible."
        )

        # ONE base. It used to be reported as two stages, the second differing
        # only by the call outcomes; those are now ordinary screen-out reasons
        # alongside the recruitment determinants, so there is a single number and
        # the base already accounts for them.
        n_signed = int(signed.sum())
        n_el = int(recruit_eligible(recruit_base, stage=2).sum())
        den = n_el + n_ref
        k = st.columns(3)
        k[0].metric("Recruitment rate", f"{round(100 * n_el / den)}%" if den else "0%",
                    help=f"{n_el} eligible sign-ups / {den} base")
        k[1].metric("Eligible sign-ups", f"{n_el}",
                    help=f"of {n_signed} who signed up; the rest were screened out")
        k[2].metric("Base (denominator)", f"{den}",
                    help=f"{n_el} eligible sign-ups + {n_ref} refusals")
        st.dataframe(pd.DataFrame([{
            "signed up": n_signed,
            "screened out": n_signed - n_el,
            "eligible sign-ups": n_el,
            "refusals": n_ref,
            "base (denominator)": den,
            "recruitment rate": f"{round(100 * n_el / den)}%" if den else "0%",
        }]), width="stretch", hide_index=True)

        # ---- the same base, split by route ----------------------------------
        # The routes are separate recruitment batches run on different days, so a
        # combined rate averages populations that have nothing to do with each
        # other. Only routes CURRENTLY SELECTED above appear, so this table always
        # reconciles with the single-row summary rather than quietly reporting a
        # wider population than the rest of the page.
        _el_mask = recruit_eligible(recruit_base, stage=2)
        if route_col and recruit_base[route_col].nunique() > 1:
            _per = []
            for _r in sorted(x for x in recruit_base[route_col].dropna().unique()
                             if str(x).strip()):
                _in = recruit_base[route_col] == _r
                _rs, _re_, _rr = (int((signed & _in).sum()), int((_el_mask & _in).sum()),
                                  int((refusal & _in).sum()))
                _rd = _re_ + _rr
                _per.append({
                    "route": _r, "signed up": _rs, "screened out": _rs - _re_,
                    "eligible sign-ups": _re_, "refusals": _rr,
                    "base (denominator)": _rd,
                    "recruitment rate": f"{round(100 * _re_ / _rd)}%" if _rd else "0%",
                })
            st.markdown("**The base, by route**")
            st.dataframe(pd.DataFrame(_per), width="stretch", hide_index=True)

        # Take the determinant names from recruit_exclusions itself, not from
        # ELIG_FLAG_LABELS: the phone-based ones ("no phone number", "no number is
        # their own") are derived from the phone-count columns and are not in that
        # dict, so listing the dict alone silently omitted the two largest reasons.
        _ex1 = recruit_exclusions(recruit_base, stage=1)
        _dets = ", ".join(_ex1.columns)
        _s2 = ", ".join(sorted(STAGE2_CALLCODES))
        _formula(
            f"signed_up  = {status_col} is \"Eligible - signed up\"",
            f"refusal    = {status_col} is \"Eligible - refused\"",
            "",
            "eligible_signups = signed_up",
            "                   AND no recruitment determinant fired",
            f"                   AND current_callcode NOT IN ({_s2})",
            "",
            "recruitment rate = eligible_signups / (eligible_signups + refusals)",
            "",
            f"determinants = {_dets}",
            "               (any ONE of them screens the respondent out; they overlap,",
            "                and a blank flag is NOT read as a 0)",
            "",
            "NB the call outcomes screen someone out exactly as a determinant does,",
            "   so they make the base SMALLER. Refusals stay in the denominator.",
        )

        # Why people fall out of the base, one row per determinant. Counted over
        # the sign-ups only: refusals and missing are never screened (see above).
        ex2 = recruit_exclusions(recruit_base, stage=2)[signed]
        _call_labels = set(CALL_OUTCOME_EXCLUSIONS.values())
        brk = pd.DataFrame({
            "excluded for": list(ex2.columns),
            "kind": ["call outcome" if c in _call_labels else "recruitment determinant"
                     for c in ex2.columns],
        })
        # One column per route, then the total. Same selection as the tables
        # above, so every figure on the page is about the same people. The route
        # of a sign-up is taken from the ROSTER row, not from anything the calls
        # produced, so it is defined for everyone including the never-called.
        _sig_routes = (recruit_base.loc[signed, route_col] if route_col else None)
        _route_names = (sorted(x for x in _sig_routes.dropna().unique() if str(x).strip())
                        if _sig_routes is not None else [])
        if len(_route_names) > 1:
            for _r in _route_names:
                brk[_r] = [int(ex2.loc[_sig_routes == _r, c].sum()) for c in ex2.columns]
        brk["all routes"] = [int(ex2[c].sum()) for c in ex2.columns]
        brk = brk.sort_values(["kind", "all routes"], ascending=[True, False])
        st.markdown("**Who is screened out of the base**")
        st.dataframe(brk, width="stretch", hide_index=True)
        if len(_route_names) <= 1:
            st.caption("Showing one route. **Select both routes in the filter above** "
                       "to get a column per route and compare them side by side.")
        st.caption(
            f"Counted over the {int(signed.sum())} sign-ups; reasons **overlap**, so they "
            f"do not add up to the {int(signed.sum()) - n_el} screened out. Outside the base: "
            f"**{n_miss} missing** sign-up status. Blank flags are **not** read as 0 — a "
            "respondent with no value recorded stays eligible."
        )
        _found = ineligible_columns(recruit_base)
        if _found:
            st.caption("Determinants are **every `ineligible_*` column on the data entry "
                       "tab**, discovered automatically — currently "
                       + ", ".join(f"`{c}`" for c in _found)
                       + ". Add a new one to the sheet and it is applied here with no "
                       "code change. (`ineligible_type1` / `ineligible_type2` are "
                       "computed FROM these and are deliberately not counted as "
                       "determinants.)")
        else:
            st.warning("No `ineligible_*` columns found on the sample tab, so **no "
                       "determinant is screening anyone out**. Falling back to the older "
                       "`is_*` flags if present.")
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

    _formula(
        "attrited = current_status is INACTIVE AND is_complete = 0",
        "           (a case that closed without an interview)",
        "",
        "base A   = ineligible_type1 = 0     rate A = attrited / base A",
        "base B   = ineligible_type2 = 0     rate B = attrited / base B",
        "",
        "ineligible_type1 = the recruitment determinants (demographics, no phone)",
        "ineligible_type2 = type1 PLUS the call outcomes that show the person was",
        "                   never reachable / not the right one",
        "",
        "NOT counted as attrition: never-attempted pids, and pids still open.",
        "Completion is terminal, so a completed pid is never attrited.",
    )

    # One name for the never-attempted bucket, used by the mask AND by the two
    # filters below. They were separate string literals and drifted apart when
    # "Not called" was renamed: the filters kept matching the old text, so every
    # never-attempted pid was counted as a RESOLVED disposition.
    NOT_ATTEMPTED, IN_PROGRESS = "Not attempted", "In progress"

    def _disposition(df):
        cc = df["current_callcode"].astype(str).str.strip()
        su = df["current_status"].astype(str).str.upper()
        d = pd.Series("Other closure", index=df.index)
        d = d.mask(cc == "0_IN", "Incorrect respondent")
        d = d.mask(cc == "0_WN", "Wrong number")
        d = d.mask(cc == "0_R", "Refusal")
        d = d.mask(df["is_complete"] == 1, "Completed")
        d = d.mask(su.isin(["ACTIVE", "PENDING"]), IN_PROGRESS)
        d = d.mask(df["ever_attempted"] == 0, NOT_ATTEMPTED)
        return d

    def _disp_summary(df):
        disp = _disposition(df)
        base = len(df)
        n_prog = int((disp == "In progress").sum())
        n_not = int((disp == NOT_ATTEMPTED).sum())
        resolved = disp[~disp.isin([IN_PROGRESS, NOT_ATTEMPTED])]
        vc = resolved.value_counts()
        d = vc.rename_axis("disposition").reset_index(name="pids")
        d["%"] = ((100 * d["pids"] / base).round().astype(int).astype(str) + "%") if base else "0%"
        return d, n_prog, n_not, base

    st.caption("Resolved disposition of the eligible base (% of eligible base). Attrition = "
               "the closure rows (Refusal / Wrong number / Incorrect respondent / Other closure).")
    _formula(
        "one disposition per pid, FIRST match wins (top to bottom):",
        "",
        "  Not attempted          ever_attempted = 0",
        "  In progress            current_status is ACTIVE or PENDING",
        "  Completed              is_complete = 1",
        "  Refusal                current_callcode = 0_R",
        "  Wrong number           current_callcode = 0_WN",
        "  Incorrect respondent   current_callcode = 0_IN",
        "  Other closure          anything else that is closed",
        "",
        "'Not attempted' and 'In progress' are excluded from the table - they are",
        "unresolved, not outcomes. % is of the whole eligible base.",
    )
    la, lb = st.columns(2)
    for col, elig in [(la, "ineligible_type1"), (lb, "ineligible_type2")]:
        with col:
            st.markdown(f"**Attrition {'A' if elig.endswith('1') else 'B'} — eligible base**")
            d, n_prog, n_not, base = _disp_summary(recruit_base[recruit_base[elig] == 0])
            st.dataframe(d, width="stretch", hide_index=True)
            st.caption(f"Not counted: {n_prog} in progress · {n_not} not attempted "
                       f"(of {base} eligible).")
