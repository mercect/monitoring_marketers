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

FLASH = "_review_flash"
HISTORY_COLS = ["endtime", "enum_name", "survey_type", "callcode", "last_section",
                "rs_section", "tab_id", "rs_tab_id", "final_comment", "comment_sp", "KEY"]


def _code_label(code):
    return f"{code} — {rv.CLOSE_CODES[code]}" if code in rv.CLOSE_CODES else code


def _history(subs, pid):
    """Every submission for the pid, oldest first. Communications form rows are
    greyed: they report timing / status only and never carry interview content."""
    h = subs[subs["pid"].astype(str).str.strip() == pid].copy()
    h["_t"] = pd.to_datetime(h.get("endtime", ""), errors="coerce", utc=True, format="ISO8601")
    h = h.sort_values("_t", na_position="first")
    cols = [c for c in HISTORY_COLS if c in h.columns]
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
        st.caption("Both lists cover the **whole roster** — the sidebar filters do not "
                   "apply here, so nothing waiting is hidden. Each save adds a row to "
                   f"the `{rv.TAB}` tab of the Google Sheet; nothing is ever overwritten.")
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
    dq = open_cases[(open_cases.get("resume_queue", 0) == 1)
                    | (open_cases["action_bucket"] == "Review")].copy()
    dq["queue"] = [" + ".join(q for q, on in (("▶️ Resume", r == 1), ("🔎 Review", b == "Review")) if on)
                   for r, b in zip(dq.get("resume_queue", 0), dq["action_bucket"])]

    section = st.radio("Section", [f"✔ Validate closures ({len(vq)})",
                                   f"⚖ Decide open cases ({len(dq)})",
                                   f"📜 Log ({len(decisions)})"],
                       horizontal=True, label_visibility="collapsed", key="review_section")

    if section.startswith("✔"):
        _validate(vq, subs, can_save)
    elif section.startswith("⚖"):
        _decide(dq, subs, keys, can_save)
    else:
        _log(decisions)


# -----------------------------------------------------------------------------
def _validate(vq, subs, can_save):
    st.markdown("Every case an **enumerator** closed (`0_`) or completed (`1`) that you "
                "have not validated yet. Your own closures count as validated already. "
                "**Confirm** or **Correct** makes it final — a later enumerator call "
                "will not reopen it. **Send back** returns it to the queue it came from, "
                "with your comment shown to the enumerators.")
    if vq.empty:
        st.info("Nothing waiting for validation.")
        return
    n_flag = int(vq["flag"].ne("").sum())
    if n_flag:
        st.warning(f"⚠️ {n_flag} closure(s) use a **supervisor-only code** "
                   "(0_NA / 0_OF / 0_D / 0_SC) on an enumerator's own submission.")

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

    edited = st.data_editor(
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
    st.dataframe(_history(subs, pid), hide_index=True, width="stretch")
    st.caption("Grey rows = Communications form (timing / status only, never interview "
               "content). Blue italic rows = earlier dashboard entries.")

    current = str(rec["current_callcode"]).strip()
    actions = ["Confirm", "Correct"] + (["Send back"] if rec["kind"] == "closure" else [])
    action = st.radio("Action", actions, horizontal=True, key=f"review_vact_{pid}")
    code = current
    if action == "Correct":
        options = [c for c in rv.CLOSE_CODES if c != current]
        code = st.selectbox("Correct to", options, format_func=_code_label,
                            key=f"review_vcode_{pid}")
    comment = st.text_area(
        "Comment" + ("" if action == "Confirm" else " (required)"),
        key=f"review_vcom_{pid}",
        placeholder="What happened — for Send back, this is what the enumerators will read.")
    need_comment = action != "Confirm" and not comment.strip()
    if st.button("💾 Save", key=f"review_vsave_{pid}", disabled=not can_save or need_comment):
        act = {"Confirm": "confirmed", "Correct": "corrected", "Send back": "sent_back"}[action]
        _save([_entry(pid, "validation", act, "" if act == "sent_back" else code,
                      comment, rec["ref_key"])], f"{pid} ({act})")


# -----------------------------------------------------------------------------
def _decide(dq, subs, keys, can_save):
    st.markdown("Open pids in **▶️ To be resumed (partial saved)** and **🔎 To be "
                "reviewed (hard to reach)**. **Close** ends the case with a `0_` code "
                "(final). **Keep trying** leaves it in its queue and shows your comment "
                "to the enumerators.")
    if dq.empty:
        st.info("Nothing in the Resume or Review queues.")
        return
    cols = [c for c in ["pid", "queue", "enumerator", "callcode", "total_attempts",
                        "days_open", "last_section", "tab_id", "review_reason",
                        "last_comment", "supervisor_note"] if c in dq.columns]
    st.dataframe(dq[cols], hide_index=True, width="stretch")

    st.markdown("#### Decide one pid")
    labels = {f"{p} · {q} · {c}": p for p, q, c in zip(dq["pid"], dq["queue"], dq["callcode"])}
    choice = st.selectbox("Pid", list(labels), key="review_dpid")
    pid = labels[choice]
    st.dataframe(_history(subs, pid), hide_index=True, width="stretch")
    st.caption("Grey rows = Communications form (timing / status only, never interview "
               "content). Blue italic rows = earlier dashboard entries.")

    action = st.radio("Action", ["Close", "Keep trying"], horizontal=True,
                      key=f"review_dact_{pid}")
    code = ""
    if action == "Close":
        code = st.selectbox("Close with", list(rv.CLOSE_CODES), format_func=_code_label,
                            key=f"review_dcode_{pid}")
    comment = st.text_area("Comment (required)", key=f"review_dcom_{pid}",
                           placeholder="What happened / what the enumerators should do next.")
    if st.button("💾 Save", key=f"review_dsave_{pid}",
                 disabled=not can_save or not comment.strip()):
        act = "closed" if action == "Close" else "keep_trying"
        _save([_entry(pid, "decision", act, code, comment, keys.get(pid, ""))],
              f"{pid} ({act})")


# -----------------------------------------------------------------------------
def _log(decisions):
    st.markdown(f"The `{rv.TAB}` tab as stored, newest first. The newest row per pid "
                "is the one that counts; older rows are history.")
    if decisions.empty:
        st.info("No entries yet.")
        return
    st.dataframe(decisions.sort_values("sup_time", ascending=False),
                 hide_index=True, width="stretch")
