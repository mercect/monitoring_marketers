#!/usr/bin/env python
"""Robust, replicable checks for the rollup.  Run:  python test_rollup.py

Three layers of verification:
  1. INVARIANTS  — properties that MUST hold for any input (structural correctness).
     Run against the demo fixture and, if secrets.toml is set, the LIVE sheet too.
  2. BASELINE    — the demo fixture is deterministic, so its key numbers are pinned.
     If the logic changes unintentionally, these fail. (If you change logic on
     purpose, update EXPECTED below.)
  3. DETERMINISM — summarize() run twice on the same input must be identical.

Exit code 0 = all passed; 1 = at least one failed (details printed).
"""
import sys
from pathlib import Path

import pandas as pd

from rollup import (summarize, frame_from_attempts, sheet_csv, status_for,
                    drop_test_attempts)

HERE = Path(__file__).parent
SHIFT_COLS = ["attempts_0730_0930", "attempts_0930_1230", "attempts_1230_1530",
              "attempts_1530_1730", "attempts_1730_2030", "attempts_off_hours"]
FLAGS = ["is_complete", "ever_attempted", "ever_picked_up", "ineligible_type1",
         "ineligible_type2", "attrited_a", "attrited_b", "is_open",
         "recontacted_after_complete"]

# Pinned baseline for the committed demo fixture (../sample_database.csv).
# total_attempts 77 -> 78 on 2026-08-31: a 4_* callcode is a real call outcome and
# now counts as an attempt (fixture pid P023, a 4_D held-partial log).
# ever_attempted 40 -> 39 on 2026-08-31: it means ATTEMPTED at least once, and
# fixture pid P019 is a supervisor closure with no call behind it.
EXPECTED = {"rows": 40, "completed": 4, "ever_attempted": 39, "attrited_a": 10,
            "attrited_b": 3, "total_attempts": 78, "times_refused": 2,
            "times_rescheduled": 7}


def check_invariants(s, label, fails):
    def ok(name, cond):
        if not bool(cond):
            fails.append(f"[{label}] {name}")

    ok("one row per pid", s["pid"].is_unique)
    ok("shift buckets sum to total_attempts",
       (s[SHIFT_COLS].sum(axis=1) == s["total_attempts"]).all())
    for f in FLAGS:
        ok(f"{f} is 0/1", set(pd.unique(s[f])) <= {0, 1})
    ok("shifts_covered_n in 0..5", s["shifts_covered_n"].between(0, 5).all())

    # Only validate the derivation for callcodes with a RECOGNIZED prefix. An odd or
    # blank callcode makes status fall back to the sheet's value — that's a data issue
    # (caught by validate_setup's "unknown callcode" warning), not a rollup bug.
    def _recognized(cc):
        cc = str(cc).strip()
        return cc == "1" or cc.startswith(("0_", "2_", "3_", "4_"))
    # Completion is absorbing and closes the case, so a pid that finished and was
    # then rung again is INACTIVE while its LAST callcode is still a call outcome.
    # Those are exempt here and covered by the completion invariants below.
    called = s[s["current_callcode"].map(_recognized) & (s["is_complete"] == 0)]
    ok("current_status matches callcode prefix (recognized, not-yet-complete)",
       (called["current_status"] == called["current_callcode"].map(status_for)).all())

    done = s["is_complete"] == 1
    ok("a completed pid is always INACTIVE",
       (s.loc[done, "current_status"].astype(str).str.upper() == "INACTIVE").all())
    ok("a completed pid is never open", (s.loc[done, "is_open"] == 0).all())
    ok("recontacted_after_complete implies complete",
       (s.loc[s["recontacted_after_complete"] == 1, "is_complete"] == 1).all())
    # completion is terminal: the last submission kept for a completed pid IS the
    # completing one, and the stray calls after it are counted, never aggregated.
    ok("a completed pid's current callcode is 1",
       (s.loc[done, "current_callcode"].astype(str).str.strip() == "1").all())
    ok("recontacted_after_complete == (calls_after_complete > 0)",
       (s["recontacted_after_complete"] == (s["calls_after_complete"] > 0).astype(int)).all())
    ok("calls_after_complete is 0 for pids that never completed",
       (s.loc[~done, "calls_after_complete"] == 0).all())

    su = s["current_status"].astype(str).str.upper()
    # EVER CALLED means attempted at least once - not "has a submission".
    ok("ever_attempted == (total_attempts >= 1)",
       (s["ever_attempted"] == (s["total_attempts"] >= 1).astype(int)).all())
    ok("a 'Not assigned' pid carries no current state",
       (s.loc[s["case_state"] == "Not assigned", "current_callcode"]
         .astype(str).str.strip() == "").all())
    # Not assigned => never called, but NOT the converse: a supervisor closure
    # with no call is Archived and never-called at the same time.
    ok("'Not assigned' implies never attempted",
       (s.loc[s["case_state"] == "Not assigned", "ever_attempted"] == 0).all())
    ok("case_state 'Archived' iff INACTIVE",
       ((s["case_state"] == "Archived") == (su == "INACTIVE")).all())

    at = s["attrited_a"] == 1
    # NB: not "and called" - a supervisor can close a pid nobody ever dialled, and
    # that is still a resolved case that did not complete.
    ok("attrited_a => INACTIVE, not complete, eligible",
       ((su[at] == "INACTIVE") & (s.loc[at, "is_complete"] == 0)
        & (s.loc[at, "ineligible_type1"] == 0)).all())
    ok("attrited_b is a subset of attrited_a", (s["attrited_b"] <= s["attrited_a"]).all())
    ok("ever_picked_up == (times_pickedup>0)",
       (s["ever_picked_up"] == (s["times_pickedup"] > 0).astype(int)).all())
    ok("reason counts are non-negative",
       (s[["times_refused", "times_dropped", "times_rescheduled", "times_wrongnumber",
           "times_noanswer", "times_off", "times_incorrect"]] >= 0).all().all())


def check_review_rules(fails):
    """Supervisor review (review.py): each action has the agreed effect.
    Built from a tiny hand-made frame, so it runs without any data file."""
    import warnings
    warnings.filterwarnings("ignore", message="No runtime found")
    from review import apply_decisions, clean_log, COLUMNS

    def sub(pid, t, cc, key, **kw):
        return dict(pid=pid, endtime=f"2026-11-0{t}T10:00:00.000Z", callcode=cc,
                    KEY=key, enum_name="E", survey_type="Long Survey", **kw)
    att = pd.DataFrame([
        sub("A", 1, "2_NA", "a1"), sub("A", 2, "0_WN", "a2"),      # confirm
        sub("B", 1, "2_SC", "b1", callback_due="2026-11-05 09:00"),
        sub("B", 2, "0_IN", "b2"),                                  # send back
        sub("C", 1, "1", "c1"),                                     # wrong complete
        sub("D", 1, "4_D", "d1"),                                   # close open case
        sub("E", 1, "2_NA", "e1"), sub("E", 2, "0_R", "e2"),        # unvalidated
    ])
    dec = clean_log(pd.DataFrame([dict(zip(COLUMNS, r)) for r in [
        ("2026-11-03 08:00:00", "S", "A", "validation", "confirmed", "0_WN", "", "a2"),
        ("2026-11-03 08:01:00", "S", "B", "validation", "sent_back", "", "go on", "b2"),
        ("2026-11-03 08:02:00", "S", "C", "validation", "corrected", "0_R", "x", "c1"),
        ("2026-11-03 08:03:00", "S", "D", "decision", "closed", "0_NA", "y", "d1"),
    ]]))
    # enumerators keep calling A and E after the review
    late = pd.DataFrame([sub("A", 4, "2_NA", "a4"), sub("E", 4, "2_NA", "e4")])
    a1 = apply_decisions(pd.concat([att, late], ignore_index=True), dec)
    s = summarize(frame_from_attempts(a1), a1).set_index("pid")

    def ok(name, cond):
        if not bool(cond):
            fails.append(f"[review rules] {name}")
    ok("confirmed closure is final (later call ignored, flagged)",
       s.at["A", "current_callcode"] == "0_WN" and s.at["A", "recontacted_after_close"] == 1)
    ok("sent back returns to the last open state", s.at["B", "current_callcode"] == "2_SC")
    ok("sent back keeps the callback time", s.at["B", "callback_due"] == "2026-11-05 09:00")
    # B's own 2_SC is a contact (a reschedule needs a conversation); the
    # supervisor's re-open row, which copies that 2_SC, must not add another.
    ok("a supervisor row is not a contact or an attempt",
       s.at["B", "total_attempts"] == 2 and s.at["B", "times_pickedup"] == 1)
    ok("corrected complete -> 0_ code, no longer complete",
       s.at["C", "current_callcode"] == "0_R" and s.at["C", "is_complete"] == 0)
    ok("closed decision closes the case", s.at["D", "current_callcode"] == "0_NA")
    ok("unvalidated closure reopens on a later call", s.at["E", "current_callcode"] == "2_NA")
    ok("clean_log drops unknown actions",
       len(clean_log(pd.DataFrame([dict(zip(COLUMNS, ["t", "S", "X", "decision",
                                                     "bogus", "", "", ""]))]))) == 0)


def check_enumerator_names(fails):
    """enumerators.py: every spelling becomes the official name; unknown names
    stay as typed; supervisor rows are not reported as unknown."""
    import warnings
    warnings.filterwarnings("ignore", message="No runtime found")
    from enumerators import alias_map, apply_names, unknown_names
    roster = pd.DataFrame({"name": ["Issa Adams Kamara", "Mohammed Tunis"],
                           "also_typed_as": ["Issa Adams; Issa Adsms", "Mohamed Tunis"]})
    amap = alias_map(roster)
    subs = pd.DataFrame({"enum_name": ["Issa Adams", "ISSA ADAMS", "Issa Adsms",
                                       "issa adams kamara", "Mohamed  Tunis", "Someone Else",
                                       "Supervisor"],
                         "is_supervisor_closure": ["0", "0", "0", "0", "0", "0", "1"]})
    got = apply_names(subs, amap)["enum_name"].tolist()
    want = ["Issa Adams Kamara"] * 4 + ["Mohammed Tunis", "Someone Else", "Supervisor"]
    if got != want:
        fails.append(f"[enumerator names] merged to {got}")
    from enumerators import supervisors
    sup = pd.DataFrame({"name": ["A", "B", "C", "D"], "supervisor": ["1", "", "yes", "0"]})
    if supervisors(sup) != ["A", "C"]:
        fails.append(f"[enumerator names] supervisors read as {supervisors(sup)}")
    if unknown_names(apply_names(subs, amap), amap) != {"Someone Else": 1}:
        fails.append("[enumerator names] unknown names should be just 'Someone Else'")


def check_assignments(fails):
    """assign.py: first assignment per pid; late / unassigned / other-caller
    checks; out-of-order detection within one call list."""
    import warnings
    warnings.filterwarnings("ignore", message="No runtime found")
    import assign
    from review import COLUMNS

    def row(t, pid, to, reason=""):
        return dict(zip(COLUMNS, [t, "Sup", pid, "assignment", "assigned", "", reason, "", to]))
    dec = pd.DataFrame([row("2026-11-14 08:00:00", "A", "Enum 1"),
                        row("2026-11-14 08:00:00", "B", "Enum 2"),
                        row("2026-11-14 08:00:00", "C", "Enum 3"),
                        row("2026-11-14 09:00:00", "A", "Enum 9")])   # a later one is ignored
    asg = assign.assignments(dec)
    ok = lambda name, cond: None if cond else fails.append(f"[assignments] {name}")
    ok("first assignment kept", asg.set_index("pid").at["A", "assigned_to"] == "Enum 1")
    subs = pd.DataFrame({
        "pid": ["A", "C", "D", "E"],
        "endtime": ["2026-11-14T08:10:00.000Z", "2026-11-14T08:12:00.000Z",
                    "2026-11-14T08:20:00.000Z", "2026-11-14T07:00:00.000Z"],
        "enum_name": ["Enum 1", "Enum 7", "Enum 4", "Enum 5"]})
    roster = pd.DataFrame({"pid": list("ABCDE")})
    c = assign.checks(roster, subs, asg, now=pd.Timestamp("2026-11-14 08:45"))
    ok("B late (assigned 45 min ago, never called)", list(c["late"]["pid"]) == ["B"])
    ok("C called by someone else", list(c["other"]["pid"]) == ["C"])
    ok("D called without assignment, E (before go-live) not",
       list(c["unassigned"]["pid"]) == ["D"])
    # out of order: list 06/11 day 4, #1 waiting, ticking #3 skips it; #2 is assigned
    r = pd.DataFrame({"pid": ["p1", "p2", "p3"], "date_recruited": "06/11/2026",
                      "assigned_list": "4", "call_order": ["1", "2", "3"],
                      "case_state": "Not assigned"})
    gaps = assign.out_of_order(["p3"], r, {"p2"})
    ok("ticking #3 while #1 waits is out of order", list(gaps) == ["p3"] and "p1" in gaps["p3"][0])
    ok("ticking #1 is in order", assign.out_of_order(["p1"], r, {"p2"}) == {})


def check_reopen(fails):
    """review.py: a validated (final) closure that is reopened goes back to its
    last open state, is listed as reactivated until the next call, and needs
    validating again if closed afterwards."""
    import warnings
    warnings.filterwarnings("ignore", message="No runtime found")
    from review import (apply_decisions, clean_log, COLUMNS, reactivated, validated_pids)

    def sub(pid, t, cc, key):
        return dict(pid=pid, endtime=f"2026-11-0{t}T10:00:00.000Z", callcode=cc, KEY=key,
                    enum_name="E", survey_type="Long Survey")
    att = pd.DataFrame([sub("A", 1, "2_SC", "a1"), sub("A", 2, "0_WN", "a2")])

    def dec(rows):
        return clean_log(pd.DataFrame([dict(zip(COLUMNS, r + ("",))) for r in rows]))
    d = dec([("2026-11-03 08:00:00", "S", "A", "validation", "confirmed", "0_WN", "", "a2"),
             ("2026-11-04 08:00:00", "S", "A", "decision", "reopened", "", "try again", "a2")])
    ok = lambda name, cond: None if cond else fails.append(f"[reopen] {name}")
    s = summarize(frame_from_attempts(apply_decisions(att, d)), apply_decisions(att, d)).set_index("pid")
    ok("reopened final closure is open again", s.at["A", "current_callcode"] == "2_SC")
    now = pd.Timestamp("2026-11-04 09:00")
    r = reactivated(d, apply_decisions(att, d), now)
    ok("listed as reactivated, 60 min waiting",
       list(r["pid"]) == ["A"] and int(r["minutes"].iloc[0]) == 60)
    ok("reopen cancels the earlier validation", "A" not in validated_pids(d))
    called = pd.concat([att, pd.DataFrame([sub("A", 5, "2_NA", "a5")])], ignore_index=True)
    ok("a call afterwards takes it off the list",
       reactivated(d, apply_decisions(called, d), now).empty)
    # ACTIVE on an open case (keep trying): listed, but no extra row added
    o = pd.DataFrame([sub("B", 1, "2_NA", "b1")])
    k = dec([("2026-11-04 08:00:00", "S", "B", "decision", "keep_trying", "", "evening", "b1")])
    ok("keep trying adds no row", len(apply_decisions(o, k)) == 1)
    ok("keep trying is listed in To be revised", list(reactivated(k, o, now)["pid"]) == ["B"])


def check_attendance(fails):
    """attendance.py: planned vs checked in vs the lists actually called."""
    import warnings
    warnings.filterwarnings("ignore", message="No runtime found")
    import datetime as dt
    import attendance as at
    from enumerators import alias_map
    ok = lambda name, cond: None if cond else fails.append(f"[attendance] {name}")
    ok("list spellings meet", at.list_key("12 Nov D4") == at.list_key("12Nov-D4")
       == at.list_key("12nov-d4") and at.list_key("03Nov-D5") == at.list_key("3Nov-D5"))
    roster = pd.DataFrame({"name": ["Salieu Nabay", "Ann B", "Cy D", "Di E"],
                           "also_typed_as": ["Sallieu Nabay", "", "", ""], "supervisor": ""})
    amap = alias_map(roster)
    sch = at.clean_schedule(pd.DataFrame({
        "date": ["2026-11-16"] * 3, "enumerator": ["Salieu Nabay", "Ann B", "Cy D"],
        "team": "A1", "shift_start": ["14:00", "7:00", "7:00"], "shift_end": ["21:00", "14:00", "14:00"],
        "list": ["12Nov-D4", "12Nov-D4", "12Nov-D5"]}), amap)
    chk = at.clean_checkins(pd.DataFrame({
        "today": ["16-Nov-2026", "16-Nov-2026", "16-Nov-2026"],
        "starttime": ["2026-11-16T13:50:00.000+00", "2026-11-16T07:40:00.000+00", "2026-11-16T09:00:00.000+00"],
        "enum_name": ["Sallieu Nabay", "Ann B", "Di E"],
        "sod_arrival_hhmm": ["13:50", "07:40", "09:00"], "sod_late_min": ["", "40", ""],
        "sod_shift_lab": ["14:00-21:00", "7:00-14:00", "7:00-14:00"]}), amap)
    day = dt.date(2026, 11, 16)
    subs = pd.DataFrame({
        "enum_name": ["Salieu Nabay", "Salieu Nabay", "Cy D", "Ann B"],
        "pid": ["p1", "p9", "p5", "p1"], "callcode": "2_NA",
        "endtime": ["2026-11-16T15:00:00.000Z", "2026-11-16T15:10:00.000Z",
                    "2026-11-16T08:00:00.000Z", "2026-11-15T08:00:00.000Z"]})
    pid_list = pd.Series({"p1": "12Nov-D4", "p9": "13Nov-D5", "p5": "12Nov-D5"})
    calls = at.calls_of_day(subs, day, pid_list)
    ok("only that day's calls", len(calls) == 3)
    t = at.day_table(day, sch, chk, calls, known=set(amap.values())).set_index("enumerator")
    ok("check-in spelling matched to the schedule", t.at["Salieu Nabay", "checked_in"] == "✅")
    ok("one call on list, one on another", t.at["Salieu Nabay", "on_list"] == 1
       and t.at["Salieu Nabay", "other_list"] == 1 and "another list" in t.at["Salieu Nabay", "flag"])
    ok("calling without check-in", "Calling without check-in" in t.at["Cy D", "flag"])
    ok("late flagged", "Late 40" in t.at["Ann B", "flag"] and "No check-in" not in t.at["Ann B", "flag"])
    ok("checked in but not planned", "Not on the schedule" in t.at["Di E", "flag"])
    off = at.off_list_calls(t.reset_index(), calls)
    ok("off-list call listed", list(off["pid"]) == ["p9"])
    a = at.assignment(t.reset_index())
    d4 = a[a["list"] == "12Nov-D4"]
    ok("assignment groups by shift and list", len(a) == 3 and set(d4["checked_in"]) == {"1/1"})
    ok("assignment marks who is off-list", "🟠 Salieu Nabay" in " ".join(a["people"]))

    # start_end_day_nov2026 as SurveyCTO writes it to the Sheet: blank
    # calculates must not drop a check-in.
    new = at.clean_checkins(pd.DataFrame({
        "starttime": ["Nov 16, 2026 7:32:10 AM", "Nov 16, 2026 7:48:00 AM", "Nov 16, 2026 7:59:00 AM",
                      "Nov 16, 2026 2:01:00 PM", "Nov 16, 2026 10:12:00 AM"],
        "today": ["Nov 16, 2026", "", "Nov 16, 2026", "Nov 16, 2026", "Nov 16, 2026"],
        "form_part": ["start", "start", "start", "end", "start"],
        "enum_name": ["Ann B", "Cy D", "Other", "Ann B", "Ann B"],
        "enum_oth": ["", "", "Di E", "", ""],
        "sod_arrival": ["7:30:00 AM", "", "7:58:00 AM", "", "10:10:00 AM"],
        "sod_arrival_hhmm": ["07:30", "", "", "", ""],
        "tablet_name": ["T03", "T07", "T11", "", "T03"],
        "eod_time": ["", "", "", "2:00 PM", ""]}), amap).set_index("enumerator")
    ok("new form: blank date and arrival fall back to starttime",
       new.at["Cy D", "arrived"] == "07:48" and new.at["Cy D", "date"] == day)
    ok("new form: an Other name is read from enum_oth", new.at["Di E", "arrived"] == "07:58")
    ok("new form: first arrival kept, end of day joined",
       new.at["Ann B", "arrived"] == "07:30" and new.at["Ann B", "left"] == "14:00")
    ok("new form: tablet read", new.at["Cy D", "tablet"] == "T07")


def check_lists_due(fails):
    """calllist.py: which list is due on which day, and how many are left."""
    import datetime as dt
    from calllist import first_call_date, lists_due
    ok = lambda name, cond: None if cond else fails.append(f"[lists due] {name}")
    r = pd.DataFrame({"pid": ["a", "b", "c", "d", "e"],
                      "date_recruited": ["12/11/2026", "12/11/2026", "12/11/2026", "13/11/2026", "01/11/2026"],
                      "assigned_list": ["12Nov-D4", "12Nov-D4", "12Nov-D5", "13Nov-D4", "4"]})
    fc = list(first_call_date(r))
    ok("12Nov-D4 is first called on 16 Nov", fc[0] == dt.date(2026, 11, 16))
    ok("12Nov-D5 on 17 Nov, 13Nov-D4 on 17 Nov", fc[2] == fc[3] == dt.date(2026, 11, 17))
    d16 = lists_due(r, dt.date(2026, 11, 16))
    ok("on the 16th only 12Nov-D4, 2 left", list(d16["assigned_list"]) == ["12Nov-D4"] and int(d16["left"].iloc[0]) == 2)
    d17 = lists_due(r, dt.date(2026, 11, 17))
    ok("on the 17th: today's two lists, then 12Nov-D4 overdue",
       list(d17["assigned_list"]) == ["12Nov-D5", "13Nov-D4", "12Nov-D4"])
    ok("old lists are left out", "4" not in set(lists_due(r, dt.date(2026, 11, 17))["assigned_list"]))


def check_baseline(s, fails):
    got = {"rows": len(s), "completed": int((s["is_complete"] == 1).sum()),
           "ever_attempted": int(s["ever_attempted"].sum()),
           "attrited_a": int(s["attrited_a"].sum()), "attrited_b": int(s["attrited_b"].sum()),
           "total_attempts": int(s["total_attempts"].sum()),
           "times_refused": int(s["times_refused"].sum()),
           "times_rescheduled": int(s["times_rescheduled"].sum())}
    for k, want in EXPECTED.items():
        if got[k] != want:
            fails.append(f"[baseline] {k}: got {got[k]}, expected {want}")


def _secret(key):
    p = HERE / ".streamlit" / "secrets.toml"
    if not p.exists():
        return ""
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(key) and "=" in line:
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _live_only(fails):
    """The live-sheet half of main(), for when the demo fixture is not present."""
    a_url, s_url = sheet_csv(_secret("sheet_csv_url")), sheet_csv(_secret("sample_csv_url"))
    if a_url and s_url:
        attempts = pd.read_csv(a_url, dtype=str, keep_default_na=False)
        sample = pd.read_csv(s_url, dtype=str, keep_default_na=False)
        attempts, _ = drop_test_attempts(attempts)
        live = summarize(sample, attempts)
        check_invariants(live, "phone survey sample", fails)
        print(f"phone survey sample: {len(live)} pids checked")
    else:
        print("phone survey sample: skipped (no secrets.toml)")
    print()
    if fails:
        sep = chr(10) + "  - "
        print("FAILED:" + sep + sep.join(fails))
        return 1
    print("ALL CHECKS PASSED")
    return 0

def main():
    fails = []

    # --- Supervisor review rules (hand-made frame, always runs) ----------------
    check_review_rules(fails)
    check_enumerator_names(fails)
    check_assignments(fails)
    check_reopen(fails)
    check_attendance(fails)
    check_lists_due(fails)
    print("supervisor review rules + enumerator names + assignments + reopen + attendance: checked")

    # --- fixture: invariants + baseline + determinism -------------------------
    # The demo fixture is real-data-shaped but synthetic. It is NOT in the git
    # repo (the repo is code-only and gitignores *.csv), so skip this layer when
    # it is absent rather than crashing — the live-sheet checks below still run.
    _fixture = HERE.parent / "sample_database.csv"
    if not _fixture.exists():
        print("data entry sample:     skipped (no sample_database.csv beside the repo)")
        return _live_only(fails)
    subs = pd.read_csv(_fixture, dtype=str, keep_default_na=False)
    frame = frame_from_attempts(subs)
    s1 = summarize(frame, subs)
    check_invariants(s1, "data entry sample", fails)
    check_baseline(s1, fails)
    if not s1.equals(summarize(frame, subs)):
        fails.append("[data entry sample] summarize() is not deterministic")
    print(f"data entry sample:     {len(s1)} pids checked")

    # --- live sheet (optional): invariants only -------------------------------
    a_url, s_url = sheet_csv(_secret("sheet_csv_url")), sheet_csv(_secret("sample_csv_url"))
    if a_url and s_url:
        attempts = pd.read_csv(a_url, dtype=str, keep_default_na=False)
        sample = pd.read_csv(s_url, dtype=str, keep_default_na=False)
        attempts, _ = drop_test_attempts(attempts)   # as the dashboards see it
        live = summarize(sample, attempts)
        check_invariants(live, "phone survey sample", fails)
        print(f"phone survey sample: {len(live)} pids checked")
    else:
        print("phone survey sample: skipped (no secrets.toml)")

    print()
    if fails:
        print("FAILED:\n  - " + "\n  - ".join(fails))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
