# review.py — Supervisor review: the decisions log in the Google Sheet.
# -----------------------------------------------------------------------------
# The supervisor labels cases from the dashboard instead of the SurveyCTO Data
# Team Correction form. Every label is ONE ROW appended to a `decisions` tab in
# the project's Google Sheet. Rows are never overwritten: the newest row for a
# pid is the one that counts, and the full history stays in the tab, which the
# cleaning code reads through its CSV link (it picks the observation to keep).
#
# Two kinds of entry (sup_type):
#   validation - every case an ENUMERATOR closed (0_IN/0_UN/0_WN/0_R/0_OC) or
#                completed (1). Actions: confirmed, corrected, sent_back.
#   decision   - an open case in the Resume or Review queue. Actions: closed,
#                keep_trying.
#
# What each action does to the dashboard (apply_decisions):
#   confirmed  closure -> a supervisor closure row with the same code (FINAL)
#              complete -> nothing to add, a completion is already final
#   corrected  -> a supervisor closure row with the new 0_ code (FINAL); on a
#                 complete it carries sup_override=1 so it beats the completion
#   sent_back  -> a supervisor RE-OPEN row: the pid goes back to the state of
#                 its last open submission, so it returns to the queue it came
#                 from; the comment shows to the enumerators as a note
#   closed     -> a supervisor closure row with the chosen 0_ code (FINAL)
#   keep_trying-> nothing added; the comment shows to the enumerators as a note
#
# Writing needs a Google service account with Editor access to the Sheet (see
# secrets.toml.example). Reading the log uses the same account. Without it the
# tab still lists what is waiting, but nothing can be saved.
# -----------------------------------------------------------------------------
import hmac
import json
import re
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from rollup import status_for, _to_dt

TAB = "decisions"
# assigned_to was added for first assignments (assign.py). Older logs have no
# such column: the header is extended on the next save, and reading pads it.
COLUMNS = ["sup_time", "sup_by", "pid", "sup_type", "sup_action",
           "sup_callcode", "sup_comment", "sup_ref_key", "assigned_to"]

TYPES = {"validation": {"confirmed", "corrected", "sent_back"},
         # reopened = any closed case put back to work by the supervisor
         "decision": {"closed", "keep_trying", "reopened"},
         # a pid handed out for its first call (assign.py); changes no status
         "assignment": {"assigned"}}
FINAL_VALIDATION = {"confirmed", "corrected"}
NOTE_ACTIONS = {"sent_back", "keep_trying", "reopened"}
# Entries that put a closed case back to work: listed in "To be revised —
# Data Team checked" until someone calls the pid again.
REACTIVATE = {"sent_back", "reopened"}

# The 0_ codes a supervisor can close or correct with.
CLOSE_CODES = {
    "0_NA": "No answer",
    "0_OF": "Phone off",
    "0_D": "Keeps dropping the call",
    "0_SC": "Keeps rescheduling",
    "0_R": "Refusal",
    "0_UN": "Ineligible",
    "0_IN": "Incorrect respondent",
    "0_WN": "Wrong number",
    "0_OC": "Other contact",
}
# Codes an enumerator closes with. Anything else behind a 0_ on an enumerator's
# own submission is a supervisor code they should not be using -> flagged.
ENUMERATOR_CODES = {"0_IN", "0_UN", "0_WN", "0_R", "0_OC"}

SYNTHETIC_SURVEY_TYPE = "Dashboard review"
# Fields a sent_back re-open copies from the pid's last open submission, so the
# case returns with its callback time, tablet and progress, not a blank slate.
REOPEN_STATE = ["callcode", "status", "callback_due", "callback_by", "tab_id",
                "rs_tab_id", "rs_section", "last_section", "last_section_n",
                "retake_mode", "numbers_wrong", "numbers_tried",
                "number_phonenumbers"]


# =============================================================================
# Secrets and the connection to Google
# =============================================================================
def _secret(key, default=""):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


def sheet_id():
    """The Sheet to write to: `sheet_id` if set, else the id inside the
    sheet_csv_url link the dashboard already reads from."""
    sid = str(_secret("sheet_id", "")).strip()
    if sid:
        return sid
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", str(_secret("sheet_csv_url", "")))
    return m.group(1) if m else ""


def writer_problem():
    """"" when the dashboard can read and write the log, else what is missing."""
    try:
        import gspread  # noqa: F401
    except ImportError:
        return "the `gspread` package is not installed (it is in requirements.txt)."
    raw = str(_secret("gcp_service_account_json", "")).strip()
    if not raw:
        return "no `gcp_service_account_json` in Secrets."
    try:
        json.loads(raw)
    except Exception:
        return ("`gcp_service_account_json` is not valid JSON — paste the whole "
                ".json key file again, unchanged, between ''' and '''.")
    if not sheet_id():
        return "no Sheet id: `sheet_csv_url` is not a Google Sheets link."
    return ""


@st.cache_resource
def _client():
    import gspread
    return gspread.service_account_from_dict(
        json.loads(str(_secret("gcp_service_account_json"))))


def read_tab_values(name):
    """All cell values of another tab of the same Sheet (a list of rows), or
    None when there is no such tab. Raises on any other problem."""
    import gspread
    try:
        return _client().open_by_key(sheet_id()).worksheet(name).get_all_values()
    except gspread.WorksheetNotFound:
        return None


def _worksheet(create=False):
    import gspread
    sh = _client().open_by_key(sheet_id())
    try:
        return sh.worksheet(TAB)
    except gspread.WorksheetNotFound:
        if not create:
            return None
        ws = sh.add_worksheet(TAB, rows=1000, cols=len(COLUMNS))
        ws.append_row(COLUMNS, value_input_option="RAW")
        return ws


def empty_log():
    return pd.DataFrame(columns=COLUMNS)


@st.cache_data(ttl=300)
def load_decisions():
    """(decisions, meta). Never raises: a problem is reported in meta["msg"]
    and the dashboard carries on without applying any decisions."""
    problem = writer_problem()
    if problem:
        return empty_log(), {"ok": False, "msg": problem, "rows": 0}
    try:
        ws = _worksheet(create=False)
        if ws is None:
            return empty_log(), {"ok": True, "rows": 0,
                                 "msg": f"no `{TAB}` tab yet — it is created on the first save."}
        values = ws.get_all_values()
    except Exception as e:
        return empty_log(), {"ok": False, "rows": 0,
                             "msg": f"could not read the `{TAB}` tab: {e}"}
    if not values:
        return empty_log(), {"ok": True, "rows": 0, "msg": ""}
    # Rows can be wider than the header (a log written before assigned_to
    # existed, then extended): pad everything to the widest row.
    width = max(len(r) for r in values)
    head = [str(h).strip() or f"_col{i}" for i, h in
            enumerate(values[0] + [""] * (width - len(values[0])))]
    body = [r + [""] * (width - len(r)) for r in values[1:]]
    df = pd.DataFrame(body, columns=head) if body else pd.DataFrame(columns=head)
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = ""
    df = clean_log(df[COLUMNS])
    return df, {"ok": True, "rows": len(df), "msg": ""}


def clean_log(df):
    """Strip whitespace and drop rows that cannot be applied (no pid, or an
    unknown type/action), so a hand edit in the Sheet cannot break the page."""
    df = df.astype(str).apply(lambda s: s.str.strip())
    known = pd.Series([a in TYPES.get(t, set())
                       for t, a in zip(df["sup_type"], df["sup_action"])],
                      index=df.index, dtype=bool)
    return df.loc[df["pid"].ne("") & known].reset_index(drop=True)


def now_text():
    """UTC, which is Sierra Leone local time (UTC+0 all year)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def append_decisions(rows):
    """Append rows (list of dicts) to the log. Raises on failure, so the caller
    can show the error; one append call, so a save is all-or-nothing."""
    problem = writer_problem()
    if problem:
        raise RuntimeError(problem)
    for r in rows:
        if r.get("sup_action") not in TYPES.get(r.get("sup_type"), set()):
            raise ValueError(f"unknown action {r.get('sup_type')}/{r.get('sup_action')}")
        if not str(r.get("pid", "")).strip():
            raise ValueError("a decision without a pid")
    ws = _worksheet(create=True)
    # An older log lacks the newer columns: widen the grid (it was created
    # exactly as wide as the old header) and extend the header first.
    if ws.col_count < len(COLUMNS):
        ws.add_cols(len(COLUMNS) - ws.col_count)
    head = ws.row_values(1)
    if head[:len(COLUMNS)] != COLUMNS:
        ws.update(values=[COLUMNS], range_name="A1")
    ws.append_rows([[str(r.get(c, "")) for c in COLUMNS] for r in rows],
                   value_input_option="RAW")


# =============================================================================
# Applying the log to the submissions (pure pandas, no Streamlit)
# =============================================================================
def _ordered(subs):
    d = subs.copy()
    d["_t"] = _to_dt(d["endtime"] if "endtime" in d.columns else pd.Series("", index=d.index))
    d["_ts"] = _to_dt(d["SubmissionDate"] if "SubmissionDate" in d.columns
                      else pd.Series("", index=d.index))
    d["_k"] = d["KEY"].astype(str) if "KEY" in d.columns else ""
    return d.sort_values(["_t", "_ts", "_k"], na_position="first")


def _is_sup(df):
    def flag(c):
        return (df[c].astype(str).str.strip().str.lower().isin(["1", "1.0", "true", "yes"])
                if c in df.columns else pd.Series(False, index=df.index))
    return flag("is_supervisor_closure") | flag("sup_correction")


def _reopen_state(hist, ref_key):
    """State of the pid's last OPEN enumerator submission before the closure
    being sent back. Falls back to 2_<same suffix> (e.g. 0_WN -> 2_WN, still
    has numbers to try), or 2_NA, when there is no earlier open submission."""
    h = hist[~_is_sup(hist)]
    if ref_key and "KEY" in h.columns and (h["KEY"] == ref_key).any():
        h = h.iloc[:int(((h["KEY"] == ref_key).values).argmax())]
    cc = h["callcode"].astype(str).str.strip() if "callcode" in h.columns else pd.Series(dtype=str)
    opn = h[cc.str.startswith(("2_", "3_", "4_"))]
    if len(opn):
        last = opn.iloc[-1]
        return {c: last.get(c, "") for c in REOPEN_STATE}
    closing = (hist.loc[hist["KEY"] == ref_key, "callcode"].astype(str).str.strip()
               if ref_key and "KEY" in hist.columns else pd.Series(dtype=str))
    suffix = closing.iloc[0].split("_", 1)[1] if len(closing) and "_" in closing.iloc[0] else ""
    cc = f"2_{suffix}" if suffix in {"NA", "OF", "WN", "IN", "OC", "SC", "D"} else "2_NA"
    return {"callcode": cc, "status": status_for(cc)}


def _iso(sup_time):
    t = pd.to_datetime(str(sup_time).strip(), errors="coerce")
    if pd.isna(t):
        return ""
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def apply_decisions(subs, decisions):
    """subs + one synthetic supervisor row per decision that changes a status.

    The rollup then treats them exactly like SurveyCTO supervisor rows (see
    rollup.is_supervisor_row / is_supervisor_closure_row). Notes (keep_trying)
    and confirmed completes add nothing. Pure function: safe to test offline."""
    if decisions is None or decisions.empty or subs.empty or "pid" not in subs.columns:
        return subs
    d = _ordered(subs)
    pids = set(d["pid"].astype(str).str.strip())
    new = []
    for i, r in enumerate(decisions.sort_values("sup_time", kind="stable").itertuples(index=False)):
        pid = str(r.pid).strip()
        if pid not in pids:
            continue
        hist = d[d["pid"].astype(str).str.strip() == pid]
        cc_hist = hist["callcode"].astype(str).str.strip() if "callcode" in hist.columns \
            else pd.Series(dtype=str)
        complete = bool(cc_hist.eq("1").any())
        row = None
        if r.sup_action in ("confirmed", "corrected", "closed"):
            code = str(r.sup_callcode).strip()
            if not code.startswith("0_"):
                if r.sup_action == "confirmed" and code == "1":
                    continue                    # a confirmed complete: already final
                continue                        # nothing valid to close with
            row = {"callcode": code, "status": status_for(code)}
            if r.sup_action == "corrected" and complete:
                row["sup_override"] = "1"       # beats the completion
        elif r.sup_action in REACTIVATE:
            row = _reopen_state(hist, str(r.sup_ref_key).strip())
        if row is None:
            continue
        # Exactly SurveyCTO's format ("2026-11-12T10:05:00.000Z"). A mixed
        # format in one column makes pandas parse the odd ones as NaT, which
        # would sort the supervisor row FIRST instead of last.
        t = _iso(r.sup_time)
        if not t:
            continue
        row.update({
            "pid": pid, "endtime": t, "starttime": t, "SubmissionDate": t,
            "KEY": f"dashboard:{r.sup_time}:{pid}:{i}",
            "enum_name": r.sup_by, "survey_type": SYNTHETIC_SURVEY_TYPE,
            "is_supervisor_closure": "1", "sup_correction": "1",
            "comment_sp": r.sup_comment, "final_comment": r.sup_comment,
            "is_answered": "", "pickup": "",
        })
        new.append(row)
    if not new:
        return subs
    out = pd.concat([subs, pd.DataFrame(new)], ignore_index=True)
    return out.fillna("")


# =============================================================================
# The lists the tab shows
# =============================================================================
def latest_per_pid(decisions, sup_type=None):
    d = decisions if sup_type is None else decisions[decisions["sup_type"] == sup_type]
    if d.empty:
        return d
    return d.sort_values("sup_time", kind="stable").groupby("pid").tail(1)


def status_keys(subs):
    """pid -> KEY of the submission that sets its status: the first completing
    one for a completed pid, otherwise the latest. Same ordering as summarize."""
    if subs.empty or "pid" not in subs.columns or "KEY" not in subs.columns:
        return {}
    d = _ordered(subs)
    d["_pid"] = d["pid"].astype(str).str.strip()
    cc = d["callcode"].astype(str).str.strip() if "callcode" in d.columns \
        else pd.Series("", index=d.index)
    keys = d.groupby("_pid")["KEY"].last()                       # latest
    first_done = d[cc.eq("1")].groupby("_pid")["KEY"].first()    # first completion
    keys.update(first_done)
    return keys.to_dict()


def validation_queue(summary, decisions, keys):
    """Closed (0_) or complete (1) pids whose status came from an ENUMERATOR
    and that are not validated yet. A supervisor's own closures (Data Team
    Correction form, dashboard decisions) count as validated already."""
    cc = summary["current_callcode"].astype(str).str.strip()
    sup = pd.to_numeric(summary.get("is_supervisor", 0), errors="coerce").fillna(0) == 1
    q = summary[(cc.str.startswith("0_") | cc.eq("1")) & ~sup].copy()
    q = q[~q["pid"].isin(validated_pids(decisions))]
    q["ref_key"] = q["pid"].map(keys).fillna("")
    qcc = q["current_callcode"].astype(str).str.strip()
    q["flag"] = ""
    q.loc[qcc.str.startswith("0_") & ~qcc.isin(ENUMERATOR_CODES), "flag"] = \
        "⚠️ supervisor-only code"
    q["kind"] = qcc.map(lambda c: "complete" if c == "1" else "closure")
    return q


def notes_by_pid(decisions):
    """pid -> the supervisor's note for the enumerators: the comment on the
    pid's newest entry when that entry is a keep_trying or a sent_back."""
    lp = latest_per_pid(decisions)
    if lp.empty:
        return {}
    lp = lp[lp["sup_action"].isin(NOTE_ACTIONS) & lp["sup_comment"].ne("")]
    return {p: f"{c} ({str(t)[:10]})" for p, c, t in
            zip(lp["pid"], lp["sup_comment"], lp["sup_time"])}


def validated_pids(decisions):
    """Pids whose closure is validated: their newest validation (or reopen)
    entry is a confirm or a correction. A reopen afterwards cancels it, so a
    pid closed again after being reopened needs validating again."""
    if decisions is None or decisions.empty:
        return set()
    d = decisions[(decisions["sup_type"] == "validation")
                  | (decisions["sup_action"] == "reopened")]
    lv = latest_per_pid(d)
    return set(lv.loc[lv["sup_action"].isin(FINAL_VALIDATION), "pid"]) if len(lv) else set()


# =============================================================================
# Who may edit
# =============================================================================
EDITOR_STATE = "_review_editor_ok"


def editor_name():
    return str(_secret("editor_name", "")).strip() or "supervisor"


def editor_configured():
    return bool(str(_secret("editor_password", "")).strip())


def editor_unlocked():
    return bool(st.session_state.get(EDITOR_STATE))


def editor_unlock_form():
    """A small password box. Separate from app_password: viewing the dashboard
    does not let anyone write to the log."""
    expected = str(_secret("editor_password", "")).strip()
    if not expected:
        st.info("Editing is off: there is no `editor_password` in Secrets.")
        return
    with st.form("review_unlock"):
        typed = st.text_input("Editor password", type="password")
        if st.form_submit_button("🔓 Unlock editing"):
            if hmac.compare_digest(typed.strip(), expected):
                st.session_state[EDITOR_STATE] = True
                st.rerun()
            else:
                st.error("Wrong editor password.")


def reactivated(decisions, subs, now):
    """Pids put back to work (sent back or reopened) that nobody has called
    since: pid, reactivated_at, reactivated_by, supervisor_note, minutes. Only
    the pid's NEWEST entry counts, so closing it again later takes it off."""
    cols = ["pid", "reactivated_at", "reactivated_by", "supervisor_note", "minutes"]
    if decisions is None or decisions.empty or subs.empty:
        return pd.DataFrame(columns=cols)
    lp = latest_per_pid(decisions[decisions["sup_type"] != "assignment"])
    lp = lp[lp["sup_action"].isin(REACTIVATE)].copy()
    if lp.empty:
        return pd.DataFrame(columns=cols)
    lp["reactivated_at"] = pd.to_datetime(lp["sup_time"], errors="coerce")
    calls = subs[~_is_sup(subs)]
    t = pd.to_datetime(calls.get("endtime", pd.Series("", index=calls.index)), errors="coerce",
                       utc=True, format="ISO8601").dt.tz_localize(None)
    last_call = t.groupby(calls["pid"].astype(str).str.strip()).max()
    lp["_last_call"] = lp["pid"].map(last_call)
    waiting = lp[lp["_last_call"].isna() | (lp["_last_call"] < lp["reactivated_at"])].copy()
    waiting["minutes"] = ((now - waiting["reactivated_at"]).dt.total_seconds() // 60)
    return pd.DataFrame({"pid": waiting["pid"].values,
                         "reactivated_at": waiting["reactivated_at"].values,
                         "reactivated_by": waiting["sup_by"].values,
                         "supervisor_note": waiting["sup_comment"].values,
                         "minutes": waiting["minutes"].values})[cols]
