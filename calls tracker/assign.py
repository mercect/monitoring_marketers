# assign.py — first assignments, made and recorded in the dashboard.
# -----------------------------------------------------------------------------
# Whoever hands out a never-called pid does it here: tick it, pick the
# enumerator, Assign. Each assignment is one row in the `decisions` log
# (review.py), sup_type "assignment": when (Sierra Leone time = UTC), who
# assigned it (sup_by), the pid, the enumerator (assigned_to), and the reason
# when it was handed out out of order. Only FIRST assignments: an assigned pid
# leaves To be assigned and cannot be assigned again here.
#
# The log is then checked against what actually happened in the submissions:
#   - assigned but not called within LATE_MINUTES
#   - called without being assigned (only after assignments started)
#   - first called by someone other than the enumerator it was assigned to
# Assigning out of order (skipping: a lower call_order in the same call list
# still waiting) is allowed, but the dashboard says what is being skipped and
# requires a reason, which goes into the log (sup_comment). In-order
# assignments carry no comment.
# -----------------------------------------------------------------------------
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

import review as rv
import column_help as ch
from calllist import with_list_order

LATE_MINUTES = 30          # assigned, and no call within this many minutes



def utc_now():
    """Real current time in UTC = Sierra Leone time, tz-naive, like the data."""
    return pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))


def assignments(decisions):
    """One row per assigned pid (its FIRST assignment since any undo): pid,
    assigned_to, assigned_by, assigned_at, reason."""
    cols = ["pid", "assigned_to", "assigned_by", "assigned_at", "reason"]
    if decisions is None or decisions.empty:
        return pd.DataFrame(columns=cols)
    a = decisions[decisions["sup_type"] == "assignment"].copy()
    if a.empty:
        return pd.DataFrame(columns=cols)
    a["assigned_at"] = pd.to_datetime(a["sup_time"], errors="coerce")
    a = a.sort_values("assigned_at", kind="stable")
    # An undo ("unassigned") cancels every assignment before it: only an
    # assignment made after the pid's last undo counts, so an undone pid goes
    # back to To be assigned until it is handed out again.
    undone = a[a["sup_action"] == "unassigned"].groupby("pid")["assigned_at"].max()
    d = a[a["sup_action"] == "assigned"]
    if len(undone):
        since = pd.to_datetime(d["pid"].map(undone))
        d = d[since.isna() | (d["assigned_at"] > since)]
    if d.empty:
        return pd.DataFrame(columns=cols)
    d = d.groupby("pid").head(1)
    return pd.DataFrame({
        "pid": d["pid"].values,
        "assigned_to": d.get("assigned_to", pd.Series("", index=d.index)).values,
        "assigned_by": d["sup_by"].values,
        "assigned_at": d["assigned_at"].values,
        "reason": d["sup_comment"].values,
    })[cols]


def first_calls(subs):
    """pid -> its first real call: first_call_at, first_caller. Supervisor rows
    are paperwork, not calls."""
    cols = ["pid", "first_call_at", "first_caller"]
    if subs.empty or "pid" not in subs.columns:
        return pd.DataFrame(columns=cols)
    d = subs[~rv._is_sup(subs)].copy()
    t = pd.to_datetime(d.get("endtime", pd.Series("", index=d.index)), errors="coerce",
                       utc=True, format="ISO8601").dt.tz_localize(None)
    if "starttime" in d.columns:
        t = t.fillna(pd.to_datetime(d["starttime"], errors="coerce", utc=True,
                                    format="ISO8601").dt.tz_localize(None))
    d["first_call_at"] = t
    d["first_caller"] = (d["enum_name"].astype(str).str.strip()
                         if "enum_name" in d.columns else "")
    d = d.dropna(subset=["first_call_at"]).sort_values("first_call_at").groupby("pid").head(1)
    return d[cols].reset_index(drop=True)


def out_of_order(ticked, roster, handed):
    """{ticked pid: [lower-numbered pids in the same call list still waiting]}.

    "Waiting" = never called, not assigned, and not ticked in this batch: they
    would be passed over by this assignment."""
    if not ticked:
        return {}
    d = with_list_order(roster)
    d["_list"] = d["date_recruited"].astype(str) + "|" + d["_day"].astype(str)
    waiting = d[(d["case_state"].astype(str) == "Not assigned")
                & ~d["pid"].isin(handed) & ~d["pid"].isin(ticked) & d["_ord"].notna()]
    out = {}
    for _, r in d[d["pid"].isin(ticked) & d["_ord"].notna()].iterrows():
        behind = waiting[(waiting["_list"] == r["_list"]) & (waiting["_ord"] < r["_ord"])]
        if len(behind):
            out[r["pid"]] = [f"#{int(o)} {p}" for p, o in
                             zip(behind.sort_values("_ord")["pid"], behind.sort_values("_ord")["_ord"])]
    return out


def checks(roster, subs, asg, now=None):
    """The assignment log against the submissions. Returns three tables:
    late (assigned, no call within LATE_MINUTES), unassigned (first called
    without being assigned, since assignments started), other (first called
    by someone other than the enumerator assigned)."""
    now = now if now is not None else utc_now()
    fc = first_calls(subs)
    empty = pd.DataFrame()
    if asg.empty:
        return {"late": empty, "unassigned": empty, "other": empty}
    key = lambda s: s.astype(str).str.strip().str.upper()
    fc = fc.assign(_k=key(fc["pid"]))
    m = asg.assign(_k=key(asg["pid"])).merge(fc.drop(columns="pid"), on="_k", how="left")
    # Still waiting = never called AND nothing else happened to it: a pid with
    # any submission (a supervisor closure, a notification form, a call typed
    # with the pid in another case) is no longer "Not assigned" in the roster,
    # and a pid not on the roster or not eligible to call was never callable.
    still = pd.Series(True, index=m.index)
    if "case_state" in roster.columns:
        fresh = roster["case_state"].astype(str) == "Not assigned"
        if "eligible_to_call" in roster.columns:
            fresh &= pd.to_numeric(roster["eligible_to_call"], errors="coerce").fillna(0) == 1
        still = m["_k"].isin(set(key(roster.loc[fresh, "pid"])))
    else:
        still = m["_k"].isin(set(key(roster["pid"])))
    m = m.drop(columns="_k")
    waiting = m[m["first_call_at"].isna() & still].copy()
    waiting["minutes"] = ((now - pd.to_datetime(waiting["assigned_at"])).dt.total_seconds() // 60)
    late = waiting[waiting["minutes"] > LATE_MINUTES]
    called = m[m["first_call_at"].notna()]
    other = called[(called["assigned_to"].astype(str).str.strip() != "")
                   & (called["first_caller"].astype(str).str.strip() != "")
                   & (called["first_caller"] != called["assigned_to"])]
    go_live = pd.to_datetime(asg["assigned_at"]).min()
    roster_pids = set(roster["pid"])
    unassigned = fc[(fc["first_call_at"] >= go_live) & ~fc["pid"].isin(set(asg["pid"]))
                    & fc["pid"].isin(roster_pids)]
    return {"late": late, "unassigned": unassigned, "other": other,
            "waiting": waiting}


def _fmt(t):
    return f"{pd.Timestamp(t):%d %b %H:%M}" if pd.notna(t) else ""


@st.fragment
def assign_panel(table, roster, handed, names, assigners):
    """The tick-and-assign table. A FRAGMENT: ticking and picking re-run only
    this panel, not the whole dashboard. Saving re-runs everything.

    table: the never-called, never-assigned pids in view, already sorted, with
    the columns to show."""
    if table.empty:
        return
    problem = rv.writer_problem()
    if "assign_round" not in st.session_state:
        st.session_state["assign_round"] = 0
    if st.session_state.get("assign_flash"):
        st.success(st.session_state.pop("assign_flash"))

    show = table.copy()
    show.insert(0, "assign_to", "")
    show.insert(0, "assign", False)
    edited = ch.data_editor(
        show, hide_index=True, width="stretch",
        key=f"assign_editor_{st.session_state['assign_round']}",
        disabled=[c for c in show.columns if c not in ("assign", "assign_to")],
        column_config={
            "assign": st.column_config.CheckboxColumn("assign", width="small"),
            "assign_to": st.column_config.SelectboxColumn("assign to", options=names,
                                                          width="medium"),
        })
    picked = edited[edited["assign"]].copy()
    if picked.empty:
        st.caption("Tick the pids you are handing out.")
        return

    # A plain, visible dropdown for the ticked pids. The in-table "assign to"
    # cell only opens on a double-click, which is easy to miss; a name chosen
    # there still wins, for handing different pids to different people at once.
    own = picked["assign_to"].fillna("").astype(str).str.strip()
    left = int(own.eq("").sum())
    if left:
        to_all = st.selectbox(
            f"Assign to ({left} ticked pid{'s' if left > 1 else ''} without a name in the table)",
            names, index=None, placeholder="Choose the enumerator", key="assign_to_all")
        if to_all:
            picked.loc[own.eq(""), "assign_to"] = to_all

    # The people marked supervisor = 1 on the enumerators tab.
    if not assigners:
        st.info("Nobody is marked as a supervisor on the `enumerators` tab "
                "(`supervisor` = 1), so there is no one to pick under Assigned by.")
    who = st.selectbox("Assigned by", assigners, index=None, key="assign_by",
                       placeholder="Who is assigning (goes into the log)") or ""
    gaps = out_of_order(list(picked["pid"]), roster, handed)
    # Skipping = ticking a pid while a lower number in the same call list is
    # still waiting. Only then: say what is being skipped and REQUIRE a reason.
    # An in-order assignment needs no comment, so no box is shown.
    reason = ""
    if gaps:
        st.error("⚠️ **You are skipping** — " + " · ".join(
            f"assigning **{p}** while {', '.join(g[:3])}{' …' if len(g) > 3 else ''} "
            f"{'is' if len(g) == 1 else 'are'} still waiting" for p, g in gaps.items())
            + ". Write why to continue, or tick the waiting one(s) first.")
        reason = st.text_area("Reason for skipping (required)", key="assign_reason")
    missing = picked["assign_to"].fillna("").astype(str).str.strip().eq("")
    ready = (not problem and who.strip() and not missing.any()
             and (not gaps or reason.strip()))
    if problem:
        st.info(f"Saving is off: {problem}")
    elif missing.any():
        st.caption(f"Choose the enumerator for {int(missing.sum())} ticked pid(s).")
    if st.button(f"📨 Assign {len(picked)} pid(s)", disabled=not ready, type="primary"):
        rows = [{"sup_time": rv.now_text(), "sup_by": who.strip(), "pid": r["pid"],
                 "sup_type": "assignment", "sup_action": "assigned",
                 # the reason belongs to the pids that skip, not to the others
                 "sup_comment": reason.strip() if r["pid"] in gaps else "",
                 "assigned_to": r["assign_to"]} for _, r in picked.iterrows()]
        try:
            rv.append_decisions(rows)
        except Exception as e:
            st.error(f"Not saved — {e}")
            return
        st.session_state["assign_round"] += 1          # a fresh, unticked table
        st.session_state.pop("assign_reason", None)
        st.session_state.pop("assign_to_all", None)
        st.session_state["assign_flash"] = f"✅ Assigned {len(rows)} pid(s)."
        st.cache_data.clear()
        st.rerun()
