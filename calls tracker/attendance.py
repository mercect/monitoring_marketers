# attendance.py — who was planned, who checked in, and which list they called.
# -----------------------------------------------------------------------------
# A separate Google Sheet (its id in Secrets as `checkin_sheet_id`) has two tabs:
#
#   data      the SurveyCTO start/end-of-day form (start_end_day_nov2026): one
#             row per submission, start or end of day (`form_part`). Read:
#             date `today` (else today_temp / starttime / SubmissionDate);
#             name `enum_name` (else enum_oth, else supervisor_name); arrival
#             `sod_arrival_hhmm` (else sod_arrival / sod_time / starttime);
#             `sod_shift_lab`, `sod_late_min` / `sod_late` / `sod_late_reason`,
#             `tablet_name`, `sod_topup`; end of day `eod_time`,
#             `eod_device_status_lab`, `eod_topup_status`, `eod_balance`.
#   schedule  the plan: date | enumerator | team | shift_start | shift_end | list
#             (one row per person per working day; list as in assigned_list,
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
    # Each pass becomes plain dates on its own before they are combined:
    # joining two datetime columns can give plain objects on newer pandas
    # (different time units), and .dt then fails.
    s = pd.Series(series).astype(str).str.strip()
    out = pd.Series(pd.NaT, index=s.index, dtype=object)
    for kw in ({"format": "ISO8601"}, {"format": "mixed", "dayfirst": True}):
        rest = out.isna()
        if not rest.any():
            break
        d = pd.to_datetime(s[rest], errors="coerce", utc=True, **kw)
        out[rest] = pd.Series(d.dt.date, index=d.index).where(d.notna(), pd.NaT)
    return out.values


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
    return one_row_each(out[cols].reset_index(drop=True))


_BLANK = {"", "nan", "none", "null", "."}
_OTHER = {"other", "others", "oth", "-96", "96", "-77", "77"}


def _hhmm(series):
    """'07:32', '7:32:10 AM', '2026-11-16T07:32:00.000Z' or 'Nov 16, 2026
    7:32:10 AM' -> '07:32'. Blank stays blank."""
    # Read straight from the text, not through pd.to_datetime: a column with
    # mixed formats (or time zones) comes back as plain objects on newer
    # pandas, and .dt then fails. The first H:MM in the text is the time; an
    # AM/PM after it is applied. SurveyCTO writes Sierra Leone time (GMT).
    s = pd.Series(series).astype(str).str.strip()
    out = pd.Series("", index=s.index)
    m = s.str.extract(r"(?:^|[T\s])(\d{1,2}):(\d{2})(?::\d{2}(?:\.\d+)?)?\s*([AaPp][Mm])?")
    ok = m[0].notna() & ~s.str.lower().isin(_BLANK)
    if ok.any():
        h = m.loc[ok, 0].astype(int)
        ampm = m.loc[ok, 2].fillna("").str.upper()
        h = h.where(ampm != "PM", h % 12 + 12).where(ampm != "AM", h % 12)
        out[ok] = h.astype(str).str.zfill(2) + ":" + m.loc[ok, 1]
    return out


def _first(*series):
    """Per row, the first non-blank value of the given text columns."""
    out = pd.Series("", index=series[0].index)
    for s in series:
        s = s.astype(str).str.strip()
        out = out.where(~out.str.lower().isin(_BLANK), s)
    return out.where(~out.str.lower().isin(_BLANK), "")


END_COLS = ("left", "device_end", "topup_end", "balance_end")


def one_row_each(checkins):
    """One row per enumerator per day, the start-of-day and end-of-day answers
    side by side. Needed again after align_names, when the start and end
    forms were typed with two spellings of one name: start answers come from
    the first row that has them, end answers from the last."""
    if checkins.empty or not checkins.duplicated(["date", "enumerator"]).any():
        return checkins

    def pick(s, last=False):
        v = [x for x in s if pd.notna(x) and str(x).strip() not in ("", "nan")]
        return (v[-1] if last else v[0]) if v else s.iloc[0]
    agg = {c: (lambda s, c=c: pick(s, last=c in END_COLS))
           for c in checkins.columns if c not in ("date", "enumerator")}
    out = checkins.groupby(["date", "enumerator"], sort=False, as_index=False).agg(agg)
    return out[list(checkins.columns)].reset_index(drop=True)


def clean_checkins(raw, amap):
    """One row per enumerator per day: the first start-of-day check-in, plus
    the end-of-day form (the last one) if they filled it.

    Works on the start_end_day_nov2026 form and older versions. Each field
    falls back to a neighbour when it is blank, so one empty calculate on the
    form does not drop a person who did check in:
      name     enum_name, else enum_oth (an "Other" name), else supervisor_name
      date     today, else today_temp, starttime, SubmissionDate
      arrival  sod_arrival_hhmm, else sod_arrival, sod_time, then the form's
               starttime when the row is a start-of-day form
    A row is a start-of-day check-in when it has an arrival or a sod_ answer,
    or when form_part says start; end of day when eod_time or any eod_ answer
    is filled, or form_part says end."""
    cols = ["date", "enumerator", "arrived", "late_min", "late", "late_reason", "left",
            "shift_form", "tablet", "topup", "device_end", "topup_end", "balance_end"]
    if raw is None or raw.empty:
        return pd.DataFrame(columns=cols)
    r = raw.copy()
    get = lambda c: r[c].astype(str).str.strip() if c in r.columns else pd.Series("", index=r.index)
    blank = lambda s: s.str.lower().isin(_BLANK)

    date = pd.Series(pd.NaT, index=r.index, dtype=object)
    for c in ("today", "today_temp", "starttime", "SubmissionDate"):
        date = date.where(date.notna(), pd.Series(_day(get(c)), index=r.index))

    name = get("enum_name")
    other = blank(name) | name.str.lower().isin(_OTHER)
    name = name.where(~other, _first(get("enum_oth"), get("supervisor_name")))

    part = get("form_part").str.lower()
    says_start = part.str.contains(r"start|sod|begin|morning", regex=True)
    says_end = part.str.contains(r"end|eod|evening|close", regex=True) & ~says_start
    sod_any = pd.Series(False, index=r.index)
    for c in ("sod_shift", "sod_arrival", "sod_arrival_hhmm", "sod_time", "sod_topup", "sod_late"):
        sod_any |= ~blank(get(c))
    eod_any = pd.Series(False, index=r.index)
    for c in ("eod_time", "eod_topup_status", "eod_device_status", "eod_balance", "eod_comments"):
        eod_any |= ~blank(get(c))
    is_start = says_start | (sod_any & ~says_end)
    is_end = says_end | eod_any

    arrived = _first(_hhmm(get("sod_arrival_hhmm")), _hhmm(get("sod_arrival")),
                     _hhmm(get("sod_time")))
    arrived = arrived.where((arrived != "") | ~is_start, _hhmm(get("starttime")))
    left = _first(_hhmm(get("eod_time")), _hhmm(get("endtime")).where(is_end, ""))

    t = pd.to_datetime(get("starttime"), errors="coerce", format="mixed", utc=True)
    df = pd.DataFrame({
        "date": date.values,
        "enumerator": enumerators.canonical(name, amap).values,
        "arrived": arrived.where(is_start, "").values,
        "late_min": pd.to_numeric(get("sod_late_min"), errors="coerce").values,
        "late": get("sod_late").values,
        "late_reason": get("sod_late_reason").values,
        "left": left.where(is_end, "").values,
        "shift_form": _first(get("sod_shift_lab"), get("sod_shift")).values,
        "tablet": _first(get("tablet_name"), get("tablet_oth"), get("tablet")).values,
        "topup": get("sod_topup").values,
        "device_end": _first(get("eod_device_status_lab"), get("eod_device_status")).values,
        "topup_end": _first(get("eod_topup_status"), get("eod_topup_amt")).values,
        "balance_end": get("eod_balance").values,
        "_t": t.values,
    })
    df = df[df["date"].notna() & (df["enumerator"] != "")]
    start_cols = [c for c in df.columns if c not in ("left", "device_end", "topup_end", "balance_end")]
    start = df[df["arrived"] != ""].sort_values("_t").drop_duplicates(["date", "enumerator"])[start_cols]
    end = df[df["left"] != ""].sort_values("_t").drop_duplicates(["date", "enumerator"], keep="last")
    out = start.merge(end[["date", "enumerator", "left", "device_end", "topup_end", "balance_end"]],
                      on=["date", "enumerator"], how="outer")
    for c in cols:
        if c not in out.columns:
            out[c] = ""
    for c in cols[2:]:
        if c != "late_min":
            out[c] = out[c].fillna("").astype(str).replace({"nan": ""})
    return out[cols].reset_index(drop=True)


def calls_of_day(subs, day, pid_list):
    """Real calls made on `day`: enumerator, pid, time, callcode and the pid's
    call list (from assigned_list)."""
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
def _name_score(a, b):
    """How surely two spellings are one person, 0..1. Needs the first names to
    agree and the surnames to be close; middle names and initials may differ
    ('Freda R psongo' ~ 'Freda Rose Psongo', 'Mariama M Sesay' ~ 'Mariama
    Musu Sesay', 'Alie Suffian Keilie' ~ 'Alie suffian Kallie')."""
    from difflib import SequenceMatcher as SM
    ta, tb = enumerators.norm(a).split(), enumerators.norm(b).split()
    if not ta or not tb:
        return 0.0
    # a name typed without its last part: 'Issa Adams' ~ 'Issa Adams Kamara'
    short, long_ = sorted((ta, tb), key=len)
    if 2 <= len(short) < len(long_) and long_[:len(short)] == short:
        return 0.9
    # one word typed for the whole name: 'Adams' ~ 'Issa Adams Kamara'. Only
    # joined when a single planned person has that word (align_names drops
    # ties, so 'Koroma' with two Koromas planned matches nobody).
    if len(short) == 1 and len(long_) >= 2 and len(short[0]) >= 4 and short[0] in long_:
        return 0.7
    if len(ta) < 2 or len(tb) < 2 or ta[0][0] != tb[0][0] or ta[-1][0] != tb[-1][0]:
        return 0.0                       # 'Salieu' is not 'Alieu'
    first = SM(None, ta[0], tb[0]).ratio()
    last = SM(None, ta[-1], tb[-1]).ratio()
    whole = SM(None, " ".join(ta), " ".join(tb)).ratio()
    if first < 0.8 or last < 0.6 or whole < 0.8:
        return 0.0
    return whole


def align_names(day, schedule, checkins, calls):
    """Spellings on the check-in form or the call form that match nobody on
    the schedule are joined to the planned person they clearly are (see
    _name_score), when that person is the one best match both ways. Returns
    (checkins, calls, typed): typed = {planned name: the spelling used}, so
    the row can say which spelling to add to also_typed_as."""
    planned = list(schedule.loc[schedule["date"] == day, "enumerator"].drop_duplicates())
    chk_day = checkins["date"] == day
    have = set(checkins.loc[chk_day, "enumerator"])

    def pair(extra, cands, both_ways=True):
        out = {}
        for n in extra:
            sc = sorted(((_name_score(n, p), p) for p in cands), reverse=True)
            if not sc or sc[0][0] == 0 or (len(sc) > 1 and sc[1][0] == sc[0][0]):
                continue
            p = sc[0][1]
            back = max(((_name_score(m, p), m) for m in extra), default=(0, None))
            if back[1] == n or not both_ways:
                out[n] = p
        return out

    # Check-ins: the start and the end form may each use a different spelling
    # of one planned person (even when one of them is the exact name), so any
    # planned person can be the match and several spellings may join one row.
    extra_chk = [n for n in have if n not in planned]
    m_chk = pair(extra_chk, planned, both_ways=False)
    extra_calls = [n for n in calls["enumerator"].unique() if n not in planned]
    m_calls = {**pair(extra_calls, planned), **{k: v for k, v in m_chk.items()}}
    m_calls = {k: v for k, v in m_calls.items() if k in set(extra_calls)}

    checkins = checkins.copy()
    checkins.loc[chk_day, "enumerator"] = checkins.loc[chk_day, "enumerator"].replace(m_chk)
    checkins = one_row_each(checkins)
    calls = calls.assign(enumerator=calls["enumerator"].replace(m_calls))
    typed = {}
    for k, v in list(m_chk.items()) + list(m_calls.items()):
        typed.setdefault(v, [])
        if k not in typed[v]:
            typed[v].append(k)
    return checkins, calls, {p: " / ".join(s) for p, s in typed.items()}


def day_table(day, schedule, checkins, calls, known=None, typed=None):
    """One row per enumerator planned, checked in or calling on `day`.
    typed: from align_names, the spelling a planned person was matched by."""
    typed = typed or {}
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
        d = c                             # the end-of-day form, if any
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
        if n in typed:
            flags.append(f"🟣 Matched as \"{typed[n]}\": add it to also_typed_as")
        elif known and n not in known:
            flags.append("⚪ Name not in the enumerators tab")
        rows.append({
            "enumerator": n,
            "team": s["team"] if planned else "",
            "planned_list": s["list"] if planned else "",
            "shift": s["shift"] if planned else (c["shift_form"] if c is not None else ""),
            "checked_in": "✅" if c is not None else "—",
            "arrived": c["arrived"] if c is not None else "",
            "late_min": late,
            "tablet": c["tablet"] if c is not None else "",
            "topup": c["topup"] if c is not None else "",
            "left": left,
            "device_end": d["device_end"] if d is not None else "",
            "topup_end": d["topup_end"] if d is not None else "",
            "balance_end": d["balance_end"] if d is not None else "",
            "calls": len(mine),
            "on_list": on,
            "other_list": len(other) if planned else len(mine),
            "other_lists": other_lists,
            "flag": " · ".join(flags),
        })
    out = pd.DataFrame(rows, columns=["enumerator", "team", "planned_list", "shift", "checked_in",
                                      "arrived", "late_min", "tablet", "topup", "left",
                                      "device_end", "topup_end", "balance_end", "calls", "on_list",
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


def lists_worked(day, schedule, calls, asg, pid_list, amap=None):
    """Which call lists each enumerator worked on `day`: a pid counts for a
    person when it was assigned to them in the dashboard that day, or when
    they submitted a call on it that day (once, even if both). One row per
    enumerator x list: enumerator, planned_list, list, pids, called,
    assigned_only (assigned, no call by them yet that day), on_plan (True /
    False; None when the person is not on the schedule)."""
    cols = ["enumerator", "planned_list", "list", "pids", "called", "assigned_only", "on_plan"]
    sch = schedule[schedule["date"] == day].drop_duplicates("enumerator") \
        if schedule is not None and len(schedule) else pd.DataFrame(columns=["enumerator", "list"])
    plan = dict(zip(sch["enumerator"], sch["list"].astype(str)))
    parts = []
    if calls is not None and len(calls):
        parts.append(pd.DataFrame({"enumerator": calls["enumerator"].values,
                                   "pid": calls["pid"].astype(str).str.strip().values,
                                   "how": "called"}))
    if asg is not None and len(asg):
        a = asg[pd.to_datetime(asg["assigned_at"], errors="coerce").dt.date == day]
        if len(a):
            who = (enumerators.canonical(a["assigned_to"], amap).values if amap
                   else a["assigned_to"].astype(str).str.strip().values)
            parts.append(pd.DataFrame({"enumerator": who,
                                       "pid": a["pid"].astype(str).str.strip().values,
                                       "how": "assigned"}))
    if not parts:
        return pd.DataFrame(columns=cols)
    w = pd.concat(parts, ignore_index=True)
    w = w[w["enumerator"].astype(str).str.strip() != ""]
    if w.empty:
        return pd.DataFrame(columns=cols)
    # per person and pid: called that day, or only assigned
    w = w.groupby(["enumerator", "pid"])["how"].agg(lambda s: "called" if "called" in set(s)
                                                    else "assigned").reset_index()
    w["list"] = w["pid"].map(pid_list).fillna("").astype(str).str.strip()
    g = w.groupby(["enumerator", "list"]).agg(
        pids=("pid", "size"), called=("how", lambda s: int((s == "called").sum()))).reset_index()
    g["assigned_only"] = g["pids"] - g["called"]
    g["planned_list"] = g["enumerator"].map(plan).fillna("")
    g["on_plan"] = [None if not p else list_key(l) == list_key(p)
                    for p, l in zip(g["planned_list"], g["list"])]
    return g[cols].sort_values(["enumerator", "list"]).reset_index(drop=True)


def on_now(schedule, checkins, now):
    """Who is on shift at `now` (Sierra Leone time, tz-naive): one row per
    person whose shift today covers the current time. enumerator, list,
    list_key, shift, checked_in (True/False)."""
    cols = ["enumerator", "list", "list_key", "shift", "checked_in"]
    if schedule is None or schedule.empty or pd.isna(now):
        return pd.DataFrame(columns=cols)
    s = schedule[schedule["date"] == now.date()].copy()
    if s.empty:
        return pd.DataFrame(columns=cols)

    def mins(t):
        m = re.match(r"\s*(\d{1,2}):(\d{2})", str(t))
        return int(m[1]) * 60 + int(m[2]) if m else None
    start = s["shift"].astype(str).str.split("–").str[0].map(mins)
    end = s["shift"].astype(str).str.split("–").str[-1].map(mins)
    t = now.hour * 60 + now.minute
    on = s[[a is not None and b is not None and a <= t < b for a, b in zip(start, end)]].copy()
    if on.empty:
        return pd.DataFrame(columns=cols)
    # a check-in typed with another spelling ('Mariama M Sesay' for 'Mariama
    # Musu Sesay', 'Issa Adams' for 'Issa Adams Kamara') counts for the
    # planned person, exactly as in the Attendance tab
    if checkins is not None and len(checkins):
        checkins, _, _ = align_names(now.date(), schedule, checkins,
                                     pd.DataFrame(columns=["enumerator", "pid"]))
    inn = set(checkins.loc[(checkins["date"] == now.date())
                           & checkins["arrived"].astype(str).str.strip().ne(""), "enumerator"]) \
        if checkins is not None and len(checkins) else set()
    on["checked_in"] = on["enumerator"].isin(inn)
    return on[cols].drop_duplicates("enumerator").reset_index(drop=True)
