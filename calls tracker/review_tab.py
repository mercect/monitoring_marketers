# review_tab.py — the "🛂 Supervisor review" tab of the call-tracking monitor.
# -----------------------------------------------------------------------------
# Everyone with the dashboard password can SEE this tab. Only someone with the
# separate editor password can SAVE, and saving also needs the service account
# key in Secrets. The rules live in review.py; this file only draws the page.
#
# Both lists ignore the sidebar filters on purpose: the sidebar opens on the
# newest recruitment batch, and a closure waiting from an older batch must not
# be hidden from the person validating it.
# -----------------------------------------------------------------------------
import pandas as pd
import streamlit as st

import review as rv
import column_help as ch

FLASH = "_review_flash"
HISTORY_COLS = ["endtime", "enum_name", "survey_type", "callcode", "last_section",
                "rs_section", "tab_id", "rs_tab_id", "final_comment", "comment_sp", "KEY"]


def _status_label(status):
    return {"ACTIVE": "ACTIVE — back to the call piles",
            "INACTIVE": "INACTIVE — closed"}.get(status, status)


def _code_label(code):
    return f"{code} — {rv.CLOSE_CODES[code]}" if code in rv.CLOSE_CODES else code


def _history(subs, pid, all_columns=False):
    """Every submission for the pid, oldest first. Communications form rows are
    greyed: they report timing / status only and never carry interview content.
    all_columns: the key columns, then every other non-empty column."""
    h = subs[subs["pid"].astype(str).str.strip() == pid].copy()
    h["_t"] = pd.to_datetime(h.get("endtime", ""), errors="coerce", utc=True, format="ISO8601")
    h = h.sort_values("_t", na_position="first")
    cols = [c for c in HISTORY_COLS if c in h.columns]
    if all_columns:
        filled = [c for c in h.columns if c not in cols and not c.startswith("_")
                  and h[c].astype(str).str.strip().replace("nan", "").ne("").any()]
        cols = cols + filled
    out = h[cols].reset_index(drop=True)
    if "endtime" in out.columns:
        out["endtime"] = out["endtime"].astype(str).str[:16].str.replace("T", " ")
    kinds = h["survey_type"].astype(str).tolist() if "survey_type" in h.columns else []

    def shade(row):
        k = kinds[row.name] if row.name < len(kinds) else ""
        if k == "Communications form":
            return ["color: #868e96"] * len(row)
        if k == rv.SYNTHETIC_SURVEY_TYPE:
            return ["font-style: italic; background-color: rgba(51,154,240,0.10)"] * len(row)
        return [""] * len(row)
    return out.style.apply(shade, axis=1)


def _num(v):
    n = pd.to_numeric(pd.Series([v]), errors="coerce").iloc[0]
    return 0 if pd.isna(n) else int(n)


def _case_facts(summary, subs, pid):
    """The history a reviewer checks before deciding: effort, timing, shifts
    still untried and whether a partial was saved. From the summary row (same
    numbers as the Tracking sheet) plus the last REAL call from the submissions
    (supervisor / dashboard rows are paperwork, not attempts)."""
    s = summary[summary["pid"] == pid]
    s = s.iloc[0] if len(s) else pd.Series(dtype=object)
    h = subs[subs["pid"].astype(str).str.strip() == pid]
    calls = h[~rv._is_sup(h)]
    t = pd.to_datetime(calls.get("endtime", pd.Series(dtype=str)), errors="coerce",
                       utc=True, format="ISO8601")
    last_call = f"{t.max():%d %b %H:%M}" if t.notna().any() else "—"

    if _num(s.get("active_partialsave", 0)) == 1:
        # The tablet only: for a held partial the summary's last_section can come
        # from the Communications form, which never counts as section reached.
        where = str(s.get("tab_id", "")).strip()
        partial = "**yes, held now**" + (f" on {where}" if where else "")
    elif _num(s.get("was_partialsaved", 0)) == 1:
        partial = "earlier, not held now"
    else:
        partial = "no"

    # Plain wrapping text rather than st.metric tiles: tiles cut long values
    # ("06 Nov 11:01", "A, B, D, E") to "0…" on a narrow window.
    with st.container(border=True):
        st.markdown(
            f"**Attempts** {_num(s.get('total_attempts', 0))} &nbsp;·&nbsp; "
            f"**Last attempted** {last_call} &nbsp;·&nbsp; "
            f"**Reschedules** {_num(s.get('times_rescheduled', 0))} &nbsp;·&nbsp; "
            f"**Drops** {_num(s.get('times_dropped', 0))}  \n"
            f"**Shifts not yet tried** {s.get('shifts_to_try', '') or '—'} &nbsp;·&nbsp; "
            f"**Partial saved** {partial}")
        st.caption(f"No answer {_num(s.get('times_noanswer', 0))} · phone off "
                   f"{_num(s.get('times_off', 0))} · wrong number "
                   f"{_num(s.get('times_wrongnumber', 0))} · numbers tried "
                   f"{s.get('numbers_tried_of_available', '') or '—'} · first attempt "
                   f"{str(s.get('first_attempt_time', '') or '—')[:16]}")


def _observations(subs, pid):
    """Dropdown with every submission for the pid: the key columns first, then
    every other column that has a value on at least one of its rows."""
    h = subs[subs["pid"].astype(str).str.strip() == pid]
    with st.expander(f"📂 All observations for {pid} ({len(h)})"):
        ch.dataframe(_history(subs, pid, all_columns=True), hide_index=True,
                     width="stretch")


def _save(rows, what):
    try:
        rv.append_decisions(rows)
    except Exception as e:
        st.error(f"Not saved — {e}")
        return
    st.cache_data.clear()            # re-read the log and re-run the rollup
    st.session_state[FLASH] = f"✅ Saved {what}."
    st.rerun()


def _entry(pid, sup_type, action, code, comment, ref_key):
    return {"sup_time": rv.now_text(), "sup_by": rv.editor_name(), "pid": pid,
            "sup_type": sup_type, "sup_action": action, "sup_callcode": code,
            "sup_comment": comment.strip(), "sup_ref_key": ref_key}


@st.fragment
def render(summary, subs, open_cases, decisions, dmeta):
    """summary / open_cases: the whole eligible roster (no sidebar filters).
    subs: submissions with the decisions already applied.

    A FRAGMENT: picking a pid, an action or typing a comment re-runs only this
    tab, not the whole dashboard (which re-does the rollup and takes seconds).
    A save calls st.rerun(), which re-runs the whole app, so every tab then
    shows the new status."""
    st.subheader("🛂 Supervisor review")
    if st.session_state.get(FLASH):
        st.success(st.session_state.pop(FLASH))

    problem = rv.writer_problem()
    unlocked = rv.editor_unlocked()
    can_save = unlocked and not problem
    top_l, top_r = st.columns([3, 1])
    with top_l:
        if problem:
            st.warning(f"**Saving is off:** {problem} The lists below still show "
                       "what is waiting.")
        elif not dmeta.get("ok", True):
            st.warning(f"Decisions log: {dmeta.get('msg')}")
        elif dmeta.get("msg"):
            st.caption(f"Decisions log: {dmeta['msg']}")
    with top_r:
        if unlocked:
            st.markdown(f"🔓 Editing as **{rv.editor_name()}**")
        elif rv.editor_configured():
            with st.popover("🔒 Unlock editing"):
                rv.editor_unlock_form()
        else:
            st.caption("🔒 Read only — no `editor_password` set.")

    keys = rv.status_keys(subs)
    todo = to_validate(summary, open_cases, decisions, keys)
    others = everything_else(summary, open_cases, todo, keys, subs)

    # Any closed pid can be put back to work, except a completed interview.
    # Closures still waiting in To validate are left out: 'ACTIVE' there
    # already sends them back, so this lists only final closures (validated,
    # or closed by a supervisor).
    _st = summary["current_status"].astype(str).str.upper()
    _done = pd.to_numeric(summary.get("is_complete", 0), errors="coerce").fillna(0) == 1
    closed = summary[(_st == "INACTIVE") & ~_done
                     & ~summary["pid"].isin(set(todo["pid"]) if "pid" in todo else set())].copy()

    # Assigned in the dashboard and not called yet: the only assignments a
    # supervisor can still take back.
    import assign
    _asg = assign.assignments(decisions)
    waiting = _asg[~_asg["pid"].isin(set(assign.first_calls(subs)["pid"]))]

    section = st.radio("Section", [f"✔ To validate ({len(todo)})",
                                   f"🔁 Reopen a final closure ({len(closed)})",
                                   f"↩ Undo an assignment ({len(waiting)})",
                                   f"📜 Log ({len(decisions)})"],
                       horizontal=True, label_visibility="collapsed", key="review_section")

    if section.startswith("✔"):
        _validate(todo, others, summary, subs, can_save)
    elif section.startswith("🔁"):
        _reopen(closed, summary, subs, keys, can_save)
    elif section.startswith("↩"):
        _unassign(waiting, summary, can_save)
    else:
        _log(decisions)


# -----------------------------------------------------------------------------
# Which cases need validating (for now; completes get their own check later):
#   1. any pid an ENUMERATOR closed with a 0_ code, not validated yet
#   2. any open pid in ▶️ To be resumed (partial saved)
#   3. any open pid in 🔎 To be reviewed (hard to reach)
# A closed pid is never open, so group 1 never overlaps 2 or 3; a pid can be
# in both 2 and 3, and then shows both labels.
CLOSED_BY_ENUM = "🛑 Closed by enumerator (0_)"
TO_RESUME = "▶️ To be resumed (partial saved)"
TO_REVIEW = "🔎 To be reviewed"
GROUPS = [CLOSED_BY_ENUM, TO_RESUME, TO_REVIEW]
LIST_COLS = ["pid", "why", "callcode", "enumerator", "last_submission_time",
             "last_comment", "flag"]


def to_validate(summary, open_cases, decisions, keys):
    """One row per pid to validate, with `why` (its group or groups), `kind`
    ("closure" for group 1, "open" for 2 and 3) and `ref_key`, the KEY of the
    submission a decision refers to."""
    vq = rv.validation_queue(summary, decisions, keys)
    closures = vq[vq["kind"] == "closure"].rename(columns={"current_callcode": "callcode"})
    closures = closures.assign(why=CLOSED_BY_ENUM)

    oc = open_cases
    res = pd.to_numeric(oc.get("resume_queue", pd.Series(0, index=oc.index)),
                        errors="coerce").fillna(0) == 1
    rev = oc["action_bucket"] == "Review"
    opn = oc[res | rev].copy()
    opn["why"] = [" + ".join(g for g, on in ((TO_RESUME, r), (TO_REVIEW, b)) if on)
                  for r, b in zip(res[res | rev], rev[res | rev])]
    opn["kind"] = "open"
    opn["flag"] = ""
    opn["ref_key"] = opn["pid"].map(keys).fillna("")
    if "enumerator" not in opn.columns and "enumerator" in summary.columns:
        opn["enumerator"] = opn["pid"].map(summary.drop_duplicates("pid")
                                           .set_index("pid")["enumerator"])

    keep = LIST_COLS + ["kind", "ref_key"]
    out = pd.concat([closures[[c for c in keep if c in closures.columns]],
                     opn[[c for c in keep if c in opn.columns]]], ignore_index=True)
    out = out.fillna("")
    # Group order first, then the oldest last activity first (waiting longest).
    out["_g"] = out["why"].map(lambda w: next((i for i, g in enumerate(GROUPS)
                                               if w.startswith(g)), 9))
    out = out.sort_values(["_g", "last_submission_time"], kind="stable")
    return out.drop(columns="_g").reset_index(drop=True)


# Every other pid that has been called, so anything can be edited: open pids
# in the other queues, completed interviews and final closures. Not part of
# "All" (that stays the cases waiting for validation). Pids never called are
# left out: a decision needs a call to attach to (rv.apply_decisions).
OTHERS = "📄 Everything else"
BUCKET_LABEL = {"Callback": "⏰ To be followed up", "Keep calling": "📵 To keep calling",
                "Resume": "▶️ To be resumed", "Review": "🔎 To be reviewed"}


def everything_else(summary, open_cases, todo, keys, subs):
    """One row per called pid not in `todo`, with `why` (where it sits now)
    and `kind`: "open", "complete" (a completed interview) or "final" (a
    closure already validated, or closed by the Data Team)."""
    cols = LIST_COLS + ["kind", "ref_key"]
    if subs.empty or "pid" not in subs.columns or summary.empty:
        return pd.DataFrame(columns=cols)
    called = set(subs["pid"].astype(str).str.strip())
    skip = set(todo["pid"]) if "pid" in todo.columns else set()
    s = summary[summary["pid"].isin(called) & ~summary["pid"].isin(skip)] \
        .drop_duplicates("pid").copy()
    if s.empty:
        return pd.DataFrame(columns=cols)
    bucket = open_cases.drop_duplicates("pid").set_index("pid")["action_bucket"] \
        if "action_bucket" in open_cases.columns else pd.Series(dtype=str)
    done = pd.to_numeric(s.get("is_complete", 0), errors="coerce").fillna(0) == 1
    closed = s["current_status"].astype(str).str.upper().eq("INACTIVE")
    s["kind"] = ["complete" if d else "final" if c else "open" for d, c in zip(done, closed)]
    s["why"] = [("✅ Completed" if k == "complete" else "🗄️ Closed (final)" if k == "final"
                 else BUCKET_LABEL.get(bucket.get(p, ""), "Open"))
                for k, p in zip(s["kind"], s["pid"])]
    s["callcode"] = s.get("current_callcode", "")
    s["flag"] = ""
    s["ref_key"] = s["pid"].map(keys).fillna("")
    out = s[[c for c in cols if c in s.columns]].fillna("")
    return out.sort_values("last_submission_time", ascending=False, kind="stable") \
              .reset_index(drop=True)


def _validate(todo, others, summary, subs, can_save):
    # Group filter, with how many are waiting in each. Everything else is the
    # last button, outside All.
    counts = {g: int(todo["why"].str.contains(g, regex=False).sum()) for g in GROUPS}
    counts[OTHERS] = len(others)
    pick = st.segmented_control(
        "Show", ["All"] + GROUPS + [OTHERS], default="All", key="review_group",
        format_func=lambda g: f"All ({len(todo)})" if g == "All" else f"{g} ({counts[g]})",
        label_visibility="collapsed")
    if pick == OTHERS:
        view = others
        st.caption("Every other pid that has been called: open in another queue, "
                   "completed, or already closed for good. Pick one to edit it.")
    else:
        view = todo if pick in (None, "All") else todo[todo["why"].str.contains(pick, regex=False)]
    if view.empty:
        st.info("Nothing to validate." if pick in (None, "All") else "Nothing in this group.")
        return
    n_flag = int(view["flag"].ne("").sum())
    if n_flag:
        st.warning(f"⚠️ {n_flag} closed by an enumerator with a supervisor-only code")

    cols = [c for c in LIST_COLS if c in view.columns and (c != "flag" or n_flag)]
    table = view[cols].reset_index(drop=True)
    # Folded by default: the list is long and pushes "Review one pid" down.
    with st.expander(f"📋 Show the list ({len(table)})"):
        st.caption("Click a row to review that pid below.")
        sel = ch.dataframe(table, hide_index=True, width="stretch", on_select="rerun",
                           selection_mode="single-row", key=f"review_list_{pick}")
    rows = sel.selection.rows if sel is not None else []
    clicked = table.at[rows[0], "pid"] if rows else None

    st.divider()
    st.markdown("#### Review one pid")
    labels = {f"{p} · {w} · {c}": p for p, w, c in
              zip(view["pid"], view["why"], view["callcode"])}
    pids = list(labels.values())
    # The dropdown follows the clicked row: a new click gives it a new key, so
    # it starts again on that row.
    choice = st.selectbox("Pid", list(labels), key=f"review_pid_{pick}_{clicked}",
                          index=pids.index(clicked) if clicked in pids else 0)
    pid = labels[choice]
    rec = view[view["pid"] == pid].iloc[0]
    st.markdown(f"**{'Where it is now' if pick == OTHERS else 'Why it needs validating'}:** "
                f"{rec['why']}")
    _case_facts(summary, subs, pid)
    _observations(subs, pid)
    if rec["kind"] == "closure":
        _closure_form(pid, rec, can_save)
    elif rec["kind"] == "complete":
        _complete_form(pid, rec, can_save)
    elif rec["kind"] == "final":
        _final_form(pid, rec, can_save)
    else:
        _open_form(pid, rec, can_save)


def _closure_form(pid, rec, can_save):
    """An enumerator's 0_ closure: confirm it, correct the code, or send it
    back to the call piles."""
    current = str(rec["callcode"]).strip()
    status = st.radio("Status after review", ["ACTIVE", "INACTIVE"], index=None,
                      horizontal=True, key=f"review_vstatus_{pid}", format_func=_status_label)
    code, act = "", ""
    if status == "INACTIVE":
        options = [current] + [c for c in rv.CLOSE_CODES if c != current]
        code = st.selectbox("Closing code", options, key=f"review_vcode_{pid}",
                            format_func=lambda c: _code_label(c)
                            + (" (as the enumerator coded it)" if c == current else ""))
        act = "confirmed" if code == current else "corrected"
    elif status == "ACTIVE":
        act = "sent_back"
    need = status == "ACTIVE" or act == "corrected"
    comment = st.text_area(
        "Comment" + (" (required, shown to the enumerators)" if status == "ACTIVE"
                     else " (required)" if need else " (optional)"),
        key=f"review_vcom_{pid}",
        placeholder="What happened, and for ACTIVE what the enumerator should do now.")
    if st.button("💾 Save", key=f"review_vsave_{pid}",
                 disabled=not can_save or not status or (need and not comment.strip())):
        _save([_entry(pid, "validation", act, "" if act == "sent_back" else code,
                      comment, rec["ref_key"])], f"{pid} ({status})")


def _complete_form(pid, rec, can_save):
    """A completed interview: it can only stay closed. Correct it to a 0_
    code when it was not a real complete (the correction beats the
    completion); a complete cannot be sent back to the call piles."""
    st.caption("A completed interview stays INACTIVE. Change the code only if it was "
               "not a real complete.")
    code = st.selectbox("Closing code", ["1"] + list(rv.CLOSE_CODES), key=f"review_ccode_{pid}",
                        format_func=lambda c: "1 — completed (keep)" if c == "1" else _code_label(c))
    comment = st.text_area("Comment (required)", key=f"review_ccom_{pid}",
                           placeholder="Why this was not a real complete.")
    if st.button("💾 Save", key=f"review_csave_{pid}",
                 disabled=not can_save or code == "1" or not comment.strip(),
                 help="Choose a 0_ code to correct the complete. Keeping 1 needs no save."):
        _save([_entry(pid, "validation", "corrected", code, comment, rec["ref_key"])],
              f"{pid} (corrected to {code})")


def _final_form(pid, rec, can_save):
    """A closure that is already final: send it back to work (a reopen, as in
    the Reopen section) or correct its code."""
    current = str(rec["callcode"]).strip()
    status = st.radio("Status after review", ["ACTIVE", "INACTIVE"], index=None,
                      horizontal=True, key=f"review_fstatus_{pid}", format_func=_status_label)
    code = ""
    if status == "INACTIVE":
        options = [current] + [c for c in rv.CLOSE_CODES if c != current]
        code = st.selectbox("Closing code", options, key=f"review_fcode_{pid}",
                            format_func=lambda c: _code_label(c)
                            + (" (as it is now)" if c == current else ""))
    comment = st.text_area(
        "Comment (required" + (", shown to the enumerators)" if status == "ACTIVE" else ")"),
        key=f"review_fcom_{pid}",
        placeholder="What happened, and for ACTIVE what the enumerator should do now.")
    same = status == "INACTIVE" and code == current
    if same:
        st.caption("Same code as now: nothing to change.")
    if st.button("💾 Save", key=f"review_fsave_{pid}",
                 disabled=not can_save or not status or same or not comment.strip()):
        if status == "ACTIVE":
            _save([_entry(pid, "decision", "reopened", "", comment, rec["ref_key"])],
                  f"{pid} (reopened)")
        else:
            _save([_entry(pid, "validation", "corrected", code, comment, rec["ref_key"])],
                  f"{pid} (corrected to {code})")


def _open_form(pid, rec, can_save):
    """An open pid (to be resumed / to be reviewed): keep it in the call piles
    with an instruction, or close it with a 0_ code."""
    status = st.radio("Status after review", ["ACTIVE", "INACTIVE"], index=None,
                      horizontal=True, key=f"review_dstatus_{pid}", format_func=_status_label)
    code = ""
    if status == "INACTIVE":
        code = st.selectbox("Closing code", list(rv.CLOSE_CODES), format_func=_code_label,
                            key=f"review_dcode_{pid}")
    comment = st.text_area(
        "Comment (required" + (", shown to the enumerators)" if status == "ACTIVE" else ")"),
        key=f"review_dcom_{pid}",
        placeholder="What happened, and for ACTIVE what the enumerator should do now.")
    if st.button("💾 Save", key=f"review_dsave_{pid}",
                 disabled=not can_save or not status or not comment.strip()):
        act = "closed" if status == "INACTIVE" else "keep_trying"
        _save([_entry(pid, "decision", act, code, comment, rec["ref_key"])],
              f"{pid} ({status})")


# -----------------------------------------------------------------------------
def _reopen(closed, summary, subs, keys, can_save):
    """Put a closed pid back to work: validated or not, closed by an
    enumerator or a supervisor. Not completes. It then appears first in Active
    pids, under To be revised — Data Team checked, with the reason as the note,
    until someone calls it."""
    if closed.empty:
        st.info("No final closures to reopen. Closures not validated yet are in ✔ To validate.")
        return
    labels = {f"{p} · {c} · {e}": p for p, c, e in
              zip(closed["pid"], closed["current_callcode"], closed.get("enumerator", ""))}
    choice = st.selectbox("Pid", list(labels), index=None, key="review_rpid",
                          placeholder="Type or choose a closed pid")
    if not choice:
        return
    pid = labels[choice]
    _case_facts(summary, subs, pid)
    _observations(subs, pid)
    comment = st.text_area("Reason for reopening (required, shown to the enumerators)",
                           key=f"review_rcom_{pid}",
                           placeholder="What the enumerator should do now")
    if st.button("🔁 Reopen", key=f"review_rsave_{pid}",
                 disabled=not can_save or not comment.strip()):
        _save([_entry(pid, "decision", "reopened", "", comment, keys.get(pid, ""))],
              f"{pid} (reopened)")



# -----------------------------------------------------------------------------
def _unassign(waiting, summary, can_save):
    """Take back assignments nobody has called yet: the pids go back to 🆕 To
    be assigned. Saved as assignment / unassigned in the decisions log, under
    the editor's name; the original assignment stays in the log."""
    if waiting.empty:
        st.info("No assignments waiting for a first call.")
        return
    w = waiting.merge(summary[[c for c in ["pid", "assigned_list", "call_order"]
                               if c in summary.columns]].drop_duplicates("pid"),
                      on="pid", how="left")
    w["assigned_at"] = pd.to_datetime(w["assigned_at"]).dt.strftime("%d %b %H:%M")
    w = w.sort_values("assigned_at", ascending=False)
    cols = [c for c in ["assigned_list", "pid", "call_order", "assigned_to", "assigned_by",
                        "assigned_at"] if c in w.columns]
    show = w[cols].copy()
    show.insert(0, "undo", False)
    rnd = st.session_state.setdefault("unassign_round", 0)
    ed = ch.data_editor(show, hide_index=True, width="stretch", key=f"unassign_editor_{rnd}",
                        disabled=cols, column_config={"undo": st.column_config.CheckboxColumn(
                            "↩ undo", width="small")})
    picked = ed[ed["undo"]]
    if picked.empty:
        st.caption("Tick the assignments to take back. They go back to 🆕 To be assigned.")
        return
    why = st.text_input("Reason (optional)", key=f"unassign_why_{rnd}")
    if st.button(f"↩ Undo {len(picked)} assignment(s)", type="primary", disabled=not can_save):
        rows = []
        for p, t in zip(picked["pid"], picked["assigned_to"]):
            e = _entry(p, "assignment", "unassigned", "", why, "")
            e["assigned_to"] = t
            rows.append(e)
        st.session_state["unassign_round"] = rnd + 1
        _save(rows, f"{len(rows)} undone assignment(s): back in To be assigned")

# -----------------------------------------------------------------------------
def _log(decisions):
    if decisions.empty:
        st.info("No entries yet.")
        return
    ch.dataframe(decisions.sort_values("sup_time", ascending=False),
                 hide_index=True, width="stretch")
