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
