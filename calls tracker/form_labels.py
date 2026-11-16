# form_labels.py — answer codes from the Long Survey shown as their labels.
# -----------------------------------------------------------------------------
# SurveyCTO exports a select question as its choice value ("2"; "2 3" for a
# select-multiple). The labels below are copied from the choices sheet of
# "Passenger Phone Survey - Launch 0 - Nov 2026.xlsx"; update them here when
# the form's choices change. A code with no label here is shown as typed.
# -----------------------------------------------------------------------------
import pandas as pd

ANSWER_LABELS = {
    # qc_noise (value 1 has no label on the form: it is the "none" option)
    "qc1_noise": {"1": "None", "2": "Some, didn't affect understanding",
                  "3": "A lot, had to repeat questions", "4": "So loud parts may be unreliable"},
    # qc_underst
    "qc3_underst": {"1": "Very good", "2": "Good", "3": "Fair", "4": "Poor"},
    # qc_attention
    "qc4_attention": {"1": "Fully engaged", "2": "Sometimes distracted",
                      "3": "Mostly distracted", "4": "Rushing to finish"},
    # qc_difficult (select multiple)
    "qc6_difficult": {"1": "It was fine, no difficulty", "2": "Noise", "3": "Network",
                      "4": "Respondent busy or distracted", "5": "Language or comprehension",
                      "6": "Respondent suspicious or hesitant", "7": "Others present",
                      "8": "Length", "-55": "Other, specify"},
    # yesno
    "full_end_survey": {"1": "Yes", "0": "No"},
    # partialsurvey
    "partial_end_survey": {
        "1": "Completed up to the end of purchases, before exposure",
        "0": "Did NOT complete purchases and symptoms",
        "2": "Completed ONLY exposure, pharmacy and pending"},
}
MULTI = {"qc6_difficult"}


def _code(v):
    t = str(v).strip()
    return t[:-2] if t.endswith(".0") else t


def label_value(col, v):
    """One answer as its label; a select-multiple as 'label · label'."""
    t = _code(v)
    if t.lower() in ("", "nan", "none"):
        return ""
    labels = ANSWER_LABELS.get(col, {})
    if col in MULTI:
        return " · ".join(labels.get(_code(p), p) for p in t.split())
    return labels.get(t, t)


def label_answers(df):
    """A copy of df with every known coded column shown as its labels."""
    out = df.copy()
    for c in ANSWER_LABELS:
        if c in out.columns:
            out[c] = [label_value(c, v) for v in out[c]]
    return out


def completion_duration(subs):
    """pid -> duration_min: length of the completing interview (callcode 1,
    Long Survey), from the form's `duration` (seconds); endtime - starttime
    when that is blank. The latest complete if there are several."""
    cols = ["pid", "duration_min"]
    if subs is None or subs.empty or "callcode" not in subs.columns:
        return pd.DataFrame(columns=cols)
    d = subs[subs["callcode"].astype(str).str.strip().map(_code) == "1"]
    if "survey_type" in d.columns:
        d = d[d["survey_type"].astype(str).str.strip().eq("Long Survey")]
    if d.empty:
        return pd.DataFrame(columns=cols)
    get = lambda c: d[c] if c in d.columns else pd.Series("", index=d.index)
    sec = pd.to_numeric(get("duration"), errors="coerce")
    st = pd.to_datetime(get("starttime"), errors="coerce", utc=True, format="mixed")
    en = pd.to_datetime(get("endtime"), errors="coerce", utc=True, format="mixed")
    sec = sec.where(sec.notna() & (sec > 0), (en - st).dt.total_seconds())
    d = d.assign(duration_min=(sec / 60).round(1), _t=en).sort_values("_t")
    return d.groupby("pid", as_index=False)["duration_min"].last()[cols]
