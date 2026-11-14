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


def says_closed(open_cases):
    """Open pids whose own comments say the case is closed ("This is a close
    case", "closed by supervisor"): in last_comment, rsd_reason or tab_id
    (where some typed it). Logged with an open code, so the dashboard still
    lists them as work, until a supervisor closes them for real."""
    text = pd.Series("", index=open_cases.index)
    for c in ("last_comment", "rsd_reason", "tab_id"):
        if c in open_cases.columns:
            text = text + " " + open_cases[c].fillna("").astype(str)
    return text.str.contains(r"(?i)\bclosed?\s+case\b|\bcase\s+(?:is\s+)?closed\b|\bclosed\s+by\b", regex=True)


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
    vq = rv.validation_queue(summary, decisions, keys)
    # Open cases to decide on: the Resume and Review queues, plus any open pid
    # whose comments SAY it is closed ("This is a close case") although it was
    # logged with an open code. Those need a supervisor to close them for real.
    _said = says_closed(open_cases)
    dq = open_cases[(open_cases.get("resume_queue", 0) == 1)
                    | (open_cases["action_bucket"] == "Review") | _said].copy()
    dq["says_closed"] = _said.reindex(dq.index, fill_value=False)
    dq["queue"] = [" + ".join(q for q, on in (("▶️ Resume", r == 1), ("🔎 Review", b == "Review"),
                                              ("💬 says closed", c)) if on)
                   for r, b, c in zip(dq.get("resume_queue", 0), dq["action_bucket"],
                                      dq["says_closed"])]

    # Any closed pid can be put back to work, except a completed interview.
    _st = summary["current_status"].astype(str).str.upper()
    _done = pd.to_numeric(summary.get("is_complete", 0), errors="coerce").fillna(0) == 1
    closed = summary[(_st == "INACTIVE") & ~_done].copy()

    section = st.radio("Section", [f"✔ Validate closures ({len(vq)})",
                                   f"⚖ Decide open cases ({len(dq)})",
                                   f"🔁 Reopen a closed case ({len(closed)})",
                                   f"📜 Log ({len(decisions)})"],
                       horizontal=True, label_visibility="collapsed", key="review_section")

    if section.startswith("✔"):
        _validate(vq, summary, subs, can_save)
    elif section.startswith("⚖"):
        _decide(dq, summary, subs, keys, can_save)
    elif section.startswith("🔁"):
        _reopen(closed, summary, subs, keys, can_save)
    else:
        _log(decisions)


# -----------------------------------------------------------------------------
def _validate(vq, summary, subs, can_save):
    if vq.empty:
        st.info("Nothing waiting for validation.")
        return
    n_flag = int(vq["flag"].ne("").sum())
    if n_flag:
        st.warning(f"⚠️ {n_flag} closed with a supervisor-only code")

    show = vq.sort_values(["flag", "kind", "last_submission_time"],
                          ascending=[False, True, True])
    cols = [c for c in ["pid", "kind", "current_callcode", "enumerator",
                        "last_submission_time", "last_comment", "flag"] if c in show.columns]
    table = show[cols].reset_index(drop=True)
    table.insert(0, "select", False)

    kind_filter = st.radio("Show", ["All", "Closures (0_)", "Completes (1)"],
                           horizontal=True, key="review_vkind")
    if kind_filter.startswith("Closures"):
        table = table[table["kind"] == "closure"]
    elif kind_filter.startswith("Completes"):
        table = table[table["kind"] == "complete"]

    edited = ch.data_editor(
        table, hide_index=True, width="stretch", key=f"review_vtable_{kind_filter}",
        disabled=[c for c in table.columns if c != "select"],
        column_config={"select": st.column_config.CheckboxColumn("✔", width="small")})
    picked = edited.loc[edited["select"], "pid"].tolist()
    if st.button(f"✔ Confirm selected ({len(picked)})", disabled=not (can_save and picked),
                 help="Confirms each selected pid with the code it already has."):
        by_pid = vq.set_index("pid")
        rows = [_entry(p, "validation", "confirmed",
                       str(by_pid.at[p, "current_callcode"]).strip(), "",
                       by_pid.at[p, "ref_key"]) for p in picked]
        _save(rows, f"{len(rows)} confirmation(s)")

    st.divider()
    st.markdown("#### Review one pid")
    labels = {f"{p} · {c} · {e}": p for p, c, e in
              zip(show["pid"], show["current_callcode"], show.get("enumerator", ""))}
    choice = st.selectbox("Pid", list(labels), key="review_vpid")
    pid = labels[choice]
    rec = vq[vq["pid"] == pid].iloc[0]
    _case_facts(summary, subs, pid)
    _observations(subs, pid)

    current = str(rec["current_callcode"]).strip()
    # Where the case goes: back to the call piles (ACTIVE) or closed (INACTIVE).
    # A completed interview can only stay INACTIVE (its code can be corrected).
    statuses = (["INACTIVE"] if rec["kind"] == "complete" else ["ACTIVE", "INACTIVE"])
    status = st.radio("Status after review", statuses, index=None, horizontal=True,
                      key=f"review_vstatus_{pid}", format_func=_status_label)
    code, act = "", ""
    if status == "INACTIVE":
        options = [current] + [c for c in rv.CLOSE_CODES if c != current]
        code = st.selectbox("Closing code", options, key=f"review_vcode_{pid}",
                            format_func=lambda c: (f"{c} — keep as completed" if c == "1"
                                                   else _code_label(c)
                                                   + (" (as the enumerator coded it)"
                                                      if c == current else "")))
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


# -----------------------------------------------------------------------------
def _decide(dq, summary, subs, keys, can_save):
    if dq.empty:
        st.info("Nothing in the Resume or Review queues.")
        return
    n_said = int(dq["says_closed"].sum()) if "says_closed" in dq.columns else 0
    if n_said:
        st.warning(f"💬 {n_said} open pid(s) have a comment saying the case is closed, "
                   "but were logged with an open code. Close them here if that is right.")
    only = st.radio("Show", ["All", f"💬 Says closed ({n_said})"], horizontal=True,
                    key="review_dfilter")
    view = dq[dq["says_closed"]] if only.startswith("💬") else dq
    cols = [c for c in ["pid", "queue", "enumerator", "callcode", "total_attempts",
                        "days_open", "last_section", "tab_id", "review_reason",
                        "last_comment", "supervisor_note"] if c in view.columns]
    table = view[cols].reset_index(drop=True)
    table.insert(0, "select", False)
    edited = ch.data_editor(
        table, hide_index=True, width="stretch", key=f"review_dtable_{only}",
        disabled=[c for c in table.columns if c != "select"],
        column_config={"select": st.column_config.CheckboxColumn("✔", width="small")})
    picked = edited.loc[edited["select"], "pid"].tolist()

    # Close several at once: one code and one comment for every ticked pid.
    if picked:
        st.markdown(f"**Close {len(picked)} selected pid(s)**")
        bcode = st.selectbox("Close with", list(rv.CLOSE_CODES), format_func=_code_label,
                             key="review_bulk_code")
        bcom = st.text_area("Comment (required, saved with each)", key="review_bulk_com",
                            placeholder="Why these are being closed")
        if st.button(f"🗂 Close {len(picked)} selected", disabled=not can_save or not bcom.strip(),
                     key="review_bulk_close"):
            rows = [_entry(p, "decision", "closed", bcode, bcom, keys.get(p, ""))
                    for p in picked]
            _save(rows, f"{len(rows)} closure(s)")
    else:
        st.caption("Tick pids to close several at once, or decide one pid below.")

    st.markdown("#### Decide one pid")
    labels = {f"{p} · {q} · {c}": p for p, q, c in zip(dq["pid"], dq["queue"], dq["callcode"])}
    choice = st.selectbox("Pid", list(labels), key="review_dpid")
    pid = labels[choice]
    _case_facts(summary, subs, pid)
    _observations(subs, pid)

    # Where the case goes: back to the call piles (ACTIVE) or closed (INACTIVE).
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
        _save([_entry(pid, "decision", act, code, comment, keys.get(pid, ""))],
              f"{pid} ({status})")


# -----------------------------------------------------------------------------
def _reopen(closed, summary, subs, keys, can_save):
    """Put a closed pid back to work: validated or not, closed by an
    enumerator or a supervisor. Not completes. It then appears first in Active
    pids, under To be revised — Data Team checked, with the reason as the note,
    until someone calls it."""
    if closed.empty:
        st.info("No closed pids to reopen.")
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
def _log(decisions):
    if decisions.empty:
        st.info("No entries yet.")
        return
    ch.dataframe(decisions.sort_values("sup_time", ascending=False),
                 hide_index=True, width="stretch")
