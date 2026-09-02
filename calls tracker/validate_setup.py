#!/usr/bin/env python
"""Replicable setup check for the tracking sheet.

Reads the two published-CSV links from .streamlit/secrets.toml, loads both tabs,
runs the rollup, and reports anything the codebook needs that's missing — WITHOUT
going through anyone. Run it whenever you change the export or the sheet:

    cd dashboard
    python validate_setup.py

Exit code 0 = good to go; 1 = something to fix (details printed).
"""
import sys
from pathlib import Path

import pandas as pd
from rollup import (summarize, summary_kpis, sheet_csv,
                    drop_test_attempts, TEST_ENUMERATORS)

SECRETS = Path(__file__).parent / ".streamlit" / "secrets.toml"

# Columns the codebook reads from each tab (see context/tracking_sheet_codebook.md).
NEED_ATTEMPTS = [
    "pid", "endtime", "starttime", "SubmissionDate", "KEY",
    "is_supervisor_closure", "held_partial", "is_answered", "pickup",
    "is_incorrect", "is_correct", "is_dk_contact", "is_othercontact",
    "callcode", "status", "stop_reason", "resch", "duration",
    "numbers_wrong", "numbers_tried", "number_phonenumbers",
    "ineligible_type1", "ineligible_type2", "last_section_n",
    "callback_due", "callback_by", "retake_mode", "comment_sp",
    "cc_attempt_sp", "final_comment", "d02_check", "d04_yn",
]
PHONE_COLS = ["no_phone", "number_of_phone_numbers", "ineligible_no_owned_phone",
              "own_phone", "phones_provided", "phone_1_prefill",
              "phone_2_prefill", "phone_1", "phone_2", "has_phone"]

# Known value sets for data-quality checks.
KNOWN_CALLCODES = {"", "1", "0_R", "0_UN", "0_IN", "0_WN", "0_OC", "0_ATTR",
                   "2_NA", "2_OF", "2_WN", "2_IN", "2_OC", "2_SC", "2_D",
                   # 4_NA / 4_OF: the notification path with rs_type=3 (a fresh
                   # attempt on a case already held as a partial save). Real
                   # codes the form emits - they were missing here, so every
                   # export carrying one raised a spurious "unknown callcode".
                   "3_SC", "3_D", "3_NA", "3_OF", "4_SC", "4_D", "4_NA", "4_OF"}
KNOWN_STATUS = {"", "ACTIVE", "PENDING", "INACTIVE", "NOTIFICATION"}
KNOWN_REC = {"", "eligible", "ineligible", "refusal", "missing"}
# phone_sample_status (2026-08 data-entry sheet) — the sign-up status column that
# replaced rec_signup. rollup.STATUS_SIGNUP_CLASS classes anything outside this
# set as missing, so an unexpected value silently drops out of every base.
KNOWN_SAMPLE_STATUS = {"", "eligible - signed up", "eligible - refused", "not eligible"}


def data_quality(attempts, sample, summ, warnings):
    """Advisory checks — catch a bad export before it quietly skews the numbers."""
    # duplicate submissions / duplicate roster pids
    if "KEY" in attempts.columns:
        k = attempts["KEY"].astype(str).str.strip()
        d = int(k[k != ""].duplicated().sum())
        if d:
            warnings.append(f"{d} duplicate KEY(s) in attempts (duplicate submissions)")
    if "pid" in sample.columns:
        d = int(sample["pid"].astype(str).str.strip().duplicated().sum())
        if d:
            warnings.append(f"{d} duplicate pid(s) in the sample tab")
    # value sets
    if "callcode" in attempts.columns:
        bad = set(attempts["callcode"].astype(str).str.strip().unique()) - KNOWN_CALLCODES
        if bad:
            warnings.append(f"unknown callcode value(s): {sorted(bad)}")
    if "status" in attempts.columns:
        bad = set(attempts["status"].astype(str).str.strip().str.upper().unique()) - KNOWN_STATUS
        if bad:
            warnings.append(f"unexpected status value(s): {sorted(bad)}")
    if "rec_signup" in sample.columns:
        bad = set(sample["rec_signup"].astype(str).str.strip().str.lower().unique()) - KNOWN_REC
        if bad:
            warnings.append(f"unexpected rec_signup value(s): {sorted(bad)}")
    if "phone_sample_status" in sample.columns:
        bad = (set(sample["phone_sample_status"].astype(str).str.strip().str.lower().unique())
               - KNOWN_SAMPLE_STATUS)
        if bad:
            warnings.append(f"unexpected phone_sample_status value(s): {sorted(bad)} - "
                            "these count as MISSING sign-up status, so they fall outside "
                            "both the eligible and the refusal base")
    # 0/1 eligibility determinants must actually be 0/1
    for c in [c for c in sample.columns if c.startswith("ineligible_")]:
        v = sample[c].astype(str).str.strip()
        bad = sorted(set(v.unique()) - {"", "0", "1"})
        if bad:
            warnings.append(f"non-0/1 value(s) in sample.{c}: {bad} - blanks and anything "
                            "unrecognised are read as NOT excluded")
    # numeric recruitment fields
    for c in ("number_of_phone_numbers", "own_phone", "phones_provided"):
        if c in sample.columns:
            v = sample[c].astype(str).str.strip()
            bad = int((pd.to_numeric(v, errors="coerce").isna() & (v != "")).sum())
            if bad:
                warnings.append(f"{bad} non-numeric value(s) in sample.{c}")
    # datetimes parse
    for c in ("starttime", "endtime", "SubmissionDate"):
        if c in attempts.columns:
            v = attempts[c].astype(str).str.strip()
            bad = int((pd.to_datetime(attempts[c], errors="coerce", utc=True).isna() & (v != "")).sum())
            if bad:
                warnings.append(f"{bad} unparseable datetime(s) in attempts.{c}")
    # numbers tried can't exceed numbers available
    if {"numbers_tried", "number_phonenumbers"} <= set(attempts.columns):
        t = pd.to_numeric(attempts["numbers_tried"], errors="coerce")
        a = pd.to_numeric(attempts["number_phonenumbers"], errors="coerce")
        bad = int((t > a).sum())
        if bad:
            warnings.append(f"{bad} row(s) where numbers_tried > numbers_available")
    # activity but no attempt: a pid whose ONLY submissions are notifications or
    # supervisor corrections counts as "ever called" while total_attempts is 0, so
    # it reads as un-attempted on the tracking sheet yet lands in the called KPI.
    _ghost = summ[(summ["ever_attempted"] == 1) & (summ["total_attempts"] == 0)]
    if len(_ghost):
        warnings.append(
            f"{len(_ghost)} pid(s) count as attempted but have 0 attempts "
            f"(e.g. {list(_ghost['pid'].head(5))}) - their only submissions are "
            "notifications / supervisor corrections, which are excluded from "
            "total_attempts. They show as un-attempted on the tracking sheet.")

    # off-hours attempts = timezone canary
    if summ is not None and "attempts_off_hours" in summ.columns:
        off = int(pd.to_numeric(summ["attempts_off_hours"], errors="coerce").fillna(0).sum())
        if off:
            warnings.append(f"{off} attempt(s) outside 07:30-20:30 (possible timezone issue)")


def read_secret(key):
    if not SECRETS.exists():
        return ""
    for line in SECRETS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(key) and "=" in line:
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main():
    problems, warnings = [], []

    attempts_url = sheet_csv(read_secret("sheet_csv_url"))
    sample_url = sheet_csv(read_secret("sample_csv_url"))
    if not attempts_url:
        problems.append("sheet_csv_url (attempts tab) is not set in .streamlit/secrets.toml")
    if not sample_url:
        problems.append("sample_csv_url (sample tab) is not set in .streamlit/secrets.toml")
    if problems:
        print("SETUP INCOMPLETE:\n  - " + "\n  - ".join(problems))
        print("\nCopy .streamlit/secrets.toml.example to .streamlit/secrets.toml and paste your two links.")
        return 1

    def _load(url, label):
        try:
            df = pd.read_csv(url, dtype=str, keep_default_na=False)
            print(f"  {label}: {len(df)} rows, {len(df.columns)} cols")
            return df
        except Exception as e:
            hint = ""
            if "401" in str(e) or "403" in str(e):
                hint = ("  -> the sheet is NOT public. In Sheets: Share -> General access "
                        "-> 'Anyone with the link' -> Viewer. (If your org blocks that, we "
                        "need the private service-account route instead.)")
            elif "400" in str(e):
                hint = "  -> check the URL / gid is correct."
            problems.append(f"could not load the {label} tab: {e}\n{hint}")
            return None

    print("Loading tabs...")
    attempts = _load(attempts_url, "attempts")
    sample = _load(sample_url, "sample")
    if attempts is None or sample is None:
        print("\nFIX THESE:\n  - " + "\n  - ".join(problems))
        return 1

    # test submissions are not observations - validate what the dashboards see
    attempts, n_test = drop_test_attempts(attempts)
    if n_test:
        print(f"  dropped {n_test} test submission(s) "
              f"(enum_name in {sorted(TEST_ENUMERATORS)})")

    # pid presence + overlap
    if "pid" not in sample.columns:
        problems.append("sample tab has no 'pid' column")
    if "pid" not in attempts.columns:
        problems.append("attempts tab has no 'pid' column")
    if "pid" in sample.columns and "pid" in attempts.columns:
        sp = set(sample["pid"].astype(str).str.strip())
        ap = set(attempts["pid"].astype(str).str.strip())
        orphans = ap - sp
        print(f"  pids: {len(sp)} in sample, {len(ap)} in attempts, "
              f"{len(ap & sp)} matched, {len(orphans)} attempt-pids NOT in sample")
        if orphans:
            warnings.append(f"{len(orphans)} pid(s) in attempts are missing from the sample tab "
                            f"(e.g. {sorted(orphans)[:5]}) - they won't get covariates/eligibility")

    # optional columns (rollup fills missing ones with defaults, so these only WARN)
    miss_att = [c for c in NEED_ATTEMPTS if c not in attempts.columns]
    if miss_att:
        warnings.append(f"attempts tab missing {len(miss_att)} column(s): {miss_att} "
                        "(those indicators will be blank until the column exists)")
    if not any(c in sample.columns for c in PHONE_COLS):
        warnings.append(f"sample tab has no no_phone/phone column (any of {PHONE_COLS}) - "
                        "never-called pids can't be flagged ineligible for 'no phone'")

    # try the actual rollup (this is the hard test)
    summ = None
    try:
        summ = summarize(sample, attempts)
        k = summary_kpis(summ)
        print(f"\nRollup OK: {len(summ)} respondents, {len(summ.columns)} columns.")
        print(f"  ever-attempted {k['ever_attempted']} | never-attempted {k['never_attempted']} | "
              f"completed {k['completed']} ({k['completion_rate']}%) | "
              f"attrition A {k['def_a']['rate']}% / B {k['def_b']['rate']}%")
    except Exception as e:
        problems.append(f"rollup crashed: {e}")

    # data-quality checks (advisory)
    data_quality(attempts, sample, summ, warnings)

    print()
    if warnings:
        print("WARNINGS (not blocking):\n  - " + "\n  - ".join(warnings) + "\n")
    if problems:
        print("FIX THESE:\n  - " + "\n  - ".join(problems))
        return 1
    print("ALL GOOD - run:  streamlit run app.py      (call-tracking monitor)")
    print("            or:  streamlit run pi_app.py   (PI indicators dashboard)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
