#!/usr/bin/env python
"""Generate a daily snapshot CSV of the respondent tracking sheet.

Reads the two Google-Sheet tabs (from .streamlit/secrets.toml), rolls them up
with the SAME logic as the dashboard, and writes a timestamped CSV to OUTPUT_DIR.
Meant to run twice a day (start and end of day) via Windows Task Scheduler.

Run manually:  python generate_daily_csv.py
"""
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from rollup import summarize, sheet_csv, drop_test_attempts

HERE = Path(__file__).parent
SECRETS = HERE / ".streamlit" / "secrets.toml"

# ---- change this to wherever you want the daily CSVs to land -----------------
OUTPUT_DIR = HERE.parent / "exports"
# -----------------------------------------------------------------------------


def read_secret(key):
    if not SECRETS.exists():
        return ""
    for line in SECRETS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(key) and "=" in line:
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main():
    att_url = sheet_csv(read_secret("sheet_csv_url"))
    smp_url = sheet_csv(read_secret("sample_csv_url"))
    if not att_url or not smp_url:
        print("ERROR: sheet URLs not set in .streamlit/secrets.toml")
        return 1

    attempts = pd.read_csv(att_url, dtype=str, keep_default_na=False)
    sample = pd.read_csv(smp_url, dtype=str, keep_default_na=False)
    # test submissions are not observations - dropped before the rollup, so the
    # exported input_attempts.csv matches exactly what produced the summary.
    attempts, n_test = drop_test_attempts(attempts)
    summary = summarize(sample, attempts)          # the case rollup (one row per pid)

    # one dated folder per run holding BOTH the inputs and the output, so you can
    # re-run the rollup yourself and verify every column end-to-end.
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    outdir = OUTPUT_DIR / stamp
    outdir.mkdir(parents=True, exist_ok=True)
    attempts.to_csv(outdir / "input_attempts.csv", index=False, encoding="utf-8")
    sample.to_csv(outdir / "input_sample.csv", index=False, encoding="utf-8")
    summary.to_csv(outdir / "output_respondent_summary.csv", index=False, encoding="utf-8")
    print(f"[{stamp}] input: {len(attempts)} attempts ({n_test} test row(s) dropped), "
          f"{len(sample)} sample  ->  output: {len(summary)} pids  ({outdir})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
