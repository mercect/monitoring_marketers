# attendance.py — who was planned, who checked in, and which list they called.
# -----------------------------------------------------------------------------
# A separate Google Sheet (its id in Secrets as `checkin_sheet_id`) has two tabs:
#
#   data      the SurveyCTO start/end-of-day form: one row per check-in, with
#             `today` (date), `enum_name`, `sod_arrival_hhmm` (arrival),
#             `sod_shift_lab` (shift), `sod_late_min` / `sod_late`, `eod_time`.
#   schedule  the plan: date | enumerator | team | shift_start | shift_end | list
#             (one row per person per working day; list as in assigned_day,
#             e.g. "12Nov-D4").
#
# For one day, every enumerator who was planned, checked in or made a call gets
# a row: planned list and shift, check-in and arrival time, calls made, how
# many were on pids of their planned list and how many on other lists. Names
# go through the enumerators tab (enumerators.py), so spellings that differ
# between the form, the schedule and the call form still meet.
#
# Read with the dashboard's service account (review.py), which must be a
# Viewer on that Sheet. Never raises: without it the tab says what is missing.
# -----------------------------------------------------------------------------
import re

import pandas as pd
import streamlit as st

import enumerators

CHECKIN_TAB = "data"
SCHEDULE_TAB = "schedule"
LATE_MIN = 15          # minutes after the shift start that count as late


def sheet_id():
    import review
    return str(review._secret("checkin_sheet_id", "")).strip()


def _frame(values):
    if not values:
        return pd.DataFrame()
    head = [str(h).strip() for h in values[0]]
    return pd.DataFrame(values[1:], columns=head).astype(str) if len(values) > 1 \
        else pd.DataFrame(columns=head)


@st.cache_data(ttl=300)
def load_attendance():
    """(checkins, schedule, meta) as raw text frames."""
    import review
    sid = sheet_id()
    if not sid:
        return pd.DataFrame(), pd.DataFrame(), {"ok": False, "msg": "no `checkin_sheet_id` in Secrets."}
    problem = review.writer_problem()
    if problem and "Sheet id" not in problem:
        return pd.DataFrame(), pd.DataFrame(), {"ok": False, "msg": problem}
    try:
        import gspread
        sh = review._client().open_by_key(sid)
        out = []
        for tab in (CHECKIN_TAB, SCHEDULE_TAB):
            try:
                out.append(_frame(sh.worksheet(tab).get_all_values()))
            except gspread.WorksheetNotFound:
                out.append(pd.DataFrame())
    except Exception as e:
        msg = str(e) or type(e).__name__
        if "PERMISSION" in msg.upper() or "403" in msg:
            msg = ("the dashboard's service account cannot open the check-in Sheet: "
                   "share it with that account as Viewer.")
        return pd.DataFrame(), pd.DataFrame(), {"ok": False, "msg": msg}
    miss = [t for t, f in zip((CHECKIN_TAB, SCHEDULE_TAB), out) if f.empty and not len(f.columns)]
    return out[0], out[1], {"ok": True, "msg": (f"no `{'`, `'.join(miss)}` tab." if miss else "")}


# ---------------------------------------------------------------- cleaning ---
def list_key(v):
    """Comparable form of a call list: '12Nov-D4', '12 Nov D4', '12nov-d4' and
    '03Nov-D4' / '3Nov-D4' all meet. Blank stays blank."""
    s = re.sub(r"[\s_]", "", str(v or "")).upper()
    if s in ("", "NAN", "NONE"):
        return ""
    m = re.match(r"^0*(\d{1,2})([A-Z]{3})-?D(\d)$", s)
    return f"{int(m[1])}{m[2]}-D{m[3]}" if m else s


def _day(series):
    """Dates from '2026-11-16', '16/11/2026', '15-Nov-2026' or a timestamp."""
    s = pd.Series(series).astype(str).str.strip()
    d = pd.to_datetime(s, errors="coerce", format="ISO8601", utc=True)
    rest = d.isna()
    if rest.any():
        d2 = pd.to_datetime(s[rest], errors="coerce", dayfirst=True, utc=True)
        d = d.where(~rest, d2)
    return d.dt.tz_localize(None).dt.date


def clean_schedule(raw, amap):
    cols = ["date", "enumerator", "team", "shift", "list", "list_key", "shift_start"]
    if raw is None or raw.empty:
        return pd.DataFrame(columns=cols)
    r = raw.rename(columns=lambda c: str(c).strip().lower())
    out = pd.DataFrame({
        "date": _day(r.get("date", "")),
        "enumerator": enumerators.canonical(r.get("enumerator", ""), amap).values,
        "team": r.get("team", pd.Series("", index=r.index)).astype(str).str.strip().values,
        "shift_start": r.get("shift_start", pd.Series("", index=r.index)).astype(str).str.strip().values,
        "list": r.get("list", pd.Series("", index=r.index)).astype(str).str.strip().values,
    })
    end = r.get("shift_end", pd.Series("", index=r.index)).astype(str).str.strip().values
    out["shift"] = [f"{a}–{b}" if a and b else a or b for a, b in zip(out["shift_start"], end)]
    out["list_key"] = out["list"].map(list_key)
    out = out[out["date"].notna() & (out["enumerator"] != "")]
    return out[cols].reset_index(drop=True)


def clean_checkins(raw, amap):
    """One row per enumerator per day: the first start-of-day check-in, plus
    the end-of-day time if they filled it."""
    cols = ["date", "enumerator", "arrived", "late_min", "late", "late_reason", "left", "shift_form"]
    if raw is None or raw.empty:
        return pd.DataFrame(columns=cols)
    r = raw.copy()
    get = lambda c: r[c].astype(str).str.strip() if c in r.columns else pd.Series("", index=r.index)
    date = _day(get("today"))
    fallback = _day(get("starttime"))
    date = pd.Series(date, index=r.index).where(pd.Series(date, index=r.index).notna(), fallback)
    df = pd.DataFrame({
        "date": date.values,
        "enumerator": enumerators.canonical(get("enum_name"), amap).values,
        "arrived": get("sod_arrival_hhmm").values,
        "late_min": pd.to_numeric(get("sod_late_min"), errors="coerce").values,
        "late": get("sod_late").values,
        "late_reason": get("sod_late_reason").values,
        "left": get("eod_time").values,
        "shift_form": get("sod_shift_lab").values,
        "_t": get("starttime").values,
    })
    df = df[df["date"].notna() & (df["enumerator"] != "")]
    start = df[df["arrived"] != ""].sort_values("_t").drop_duplicates(["date", "enumerator"])
    end = df[df["left"] != ""].sort_values("_t").drop_duplicates(["date", "enumerator"], keep="last")
    out = start.drop(columns=["left"]).merge(end[["date", "enumerator", "left"]],
                                             on=["date", "enumerator"], how="outer")
    for c in cols:
        if c not in out.columns:
            out[c] = ""
    for c in ("arrived", "late", "late_reason", "left", "shift_form"):
        out[c] = out[c].fillna("")
    return out[cols].reset_index(drop=True)


def calls_of_day(subs, day, pid_list):
    """Real calls made on `day`: enumerator, pid, time, callcode and the pid's
    call list (from assigned_day)."""
    cols = ["enumerator", "pid", "time", "callcode", "list", "list_key"]
    if subs is None or subs.empty or "enum_name" not in subs.columns:
        return pd.DataFrame(columns=cols)
    import review
    c = subs[~review._is_sup(subs)] if "pid" in subs.columns else subs
    t = pd.to_datetime(c.get("endtime", pd.Series("", index=c.index)), errors="coerce",
                       utc=True, format="ISO8601").dt.tz_localize(None)
    c = c.assign(_t=t.values)
    c = c[c["_t"].dt.date == day]
    out = pd.DataFrame({
        "enumerator": c["enum_name"].astype(str).str.strip().values,
        "pid": c["pid"].astype(str).str.strip().values,
        "time": c["_t"].dt.strftime("%H:%M").values,
        "callcode": c.get("callcode", pd.Series("", index=c.index)).astype(str).values,
    })
    out["list"] = out["pid"].map(pid_list).fillna("")
    out["list_key"] = out["list"].map(list_key)
    return out[out["enumerator"] != ""][cols].reset_index(drop=True)


# ------------------------------------------------------------------ the day ---
def day_table(day, schedule, checkins, calls, known=None):
    """One row per enumerator planned, checked in or calling on `day`."""
    sch = schedule[schedule["date"] == day].drop_duplicates("enumerator")
    chk = checkins[checkins["date"] == day].drop_duplicates("enumerator")
    names = list(dict.fromkeys(list(sch["enumerator"]) + list(chk["enumerator"])
                               + list(calls["enumerator"])))
    sch_i, chk_i = sch.set_index("enumerator"), chk.set_index("enumerator")
    rows = []
    for n in names:
        planned = n in sch_i.index
        s = sch_i.loc[n] if planned else None
        c = chk_i.loc[n] if n in chk_i.index else None
        left = c["left"] if c is not None else ""
        if c is not None and not str(c["arrived"]).strip():
            c = None                      # only an end-of-day form: no check-in
        mine = calls[calls["enumerator"] == n]
        want = s["list_key"] if planned else ""
        on = int((mine["list_key"] == want).sum()) if want else 0
        other = mine[mine["list_key"] != want] if want else mine.iloc[0:0]
        other_lists = ", ".join(f"{k or 'no list'} ×{v}" for k, v in
                                other["list"].replace("", "no list").value_counts().items())
        late = c["late_min"] if c is not None else float("nan")
        flags = []
        if planned and c is None:
            flags.append("🔴 Calling without check-in" if len(mine) else "🔴 No check-in")
        if not planned:
            flags.append("🔵 Not on the schedule")
        if planned and len(other):
            flags.append(f"🟠 {len(other)} call(s) on another list")
        if c is not None and pd.notna(late) and late > LATE_MIN:
            flags.append(f"🟡 Late {int(late)} min")
        if known and n not in known:
            flags.append("⚪ Name not in the enumerators tab")
        rows.append({
            "enumerator": n,
            "team": s["team"] if planned else "",
            "planned_list": s["list"] if planned else "",
            "shift": s["shift"] if planned else (c["shift_form"] if c is not None else ""),
            "checked_in": "✅" if c is not None else "—",
            "arrived": c["arrived"] if c is not None else "",
            "late_min": late,
            "left": left,
            "calls": len(mine),
            "on_list": on,
            "other_list": len(other) if planned else len(mine),
            "other_lists": other_lists,
            "flag": " · ".join(flags),
        })
    out = pd.DataFrame(rows, columns=["enumerator", "team", "planned_list", "shift", "checked_in",
                                      "arrived", "late_min", "left", "calls", "on_list",
                                      "other_list", "other_lists", "flag"])
    order = out["flag"].map(lambda f: 0 if "🔴" in f else 1 if "🟠" in f else 2 if f else 3)
    return out.assign(_o=order).sort_values(["_o", "team", "enumerator"]).drop(columns="_o") \
              .reset_index(drop=True)


def assignment(day_rows):
    """The plan for the day, one row per shift and list: how many are in out of
    how many expected, and each person marked ✅ in, ⏳ not in, 🟠 calling
    another list."""
    plan = day_rows[day_rows["planned_list"] != ""]
    if plan.empty:
        return pd.DataFrame(columns=["shift", "list", "checked_in", "people"])

    def mark(r):
        if "another list" in r["flag"]:
            return f"🟠 {r['enumerator']}"
        return f"{'✅' if r['checked_in'] == '✅' else '⏳'} {r['enumerator']}"
    rows = []
    for (shift, lst), g in plan.groupby(["shift", "planned_list"], sort=False):
        g = g.sort_values(["checked_in", "enumerator"], ascending=[False, True])
        rows.append({"shift": shift, "list": lst,
                     "checked_in": f"{int((g['checked_in'] == '✅').sum())}/{len(g)}",
                     "people": ", ".join(g.apply(mark, axis=1))})
    return pd.DataFrame(rows)


def off_list_calls(day_rows, calls):
    """The calls behind the 🟠 flag: pid, time, code, the pid's list and the
    caller's planned list."""
    plan = day_rows.set_index("enumerator")["planned_list"].to_dict()
    c = calls[calls["enumerator"].isin([n for n, p in plan.items() if p])].copy()
    c["planned_list"] = c["enumerator"].map(plan)
    c = c[c["list_key"] != c["planned_list"].map(list_key)]
    return c[["enumerator", "planned_list", "pid", "list", "time", "callcode"]] \
        .rename(columns={"list": "pid_list"}).sort_values(["enumerator", "time"]).reset_index(drop=True)
