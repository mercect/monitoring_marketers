# pace.py — is the day moving? Rates and projections for the ⏱️ Pace tab.
# -----------------------------------------------------------------------------
# Everything here is about TODAY (Sierra Leone time = UTC) and is computed from
# data the dashboard already has: the call submissions, the roster
# (assigned_list, case_state), the check-in Sheet (schedule + start-of-day
# form) and the decisions log (assignments). Pure functions, so they can be
# tested offline; the tab itself is drawn in app.py.
#
#   lists_today      per call list worked today: pids started today (first ever
#                    call today), left to start, completes, the start rate and
#                    when the list would be fully started at that rate.
#   people_today     per enumerator: calls, new cases, completes, first / last
#                    call, minutes since their last submission while on shift,
#                    calls per hour, and a flag when they look idle.
#   calls_by_hour    calls per hour today, for the chart.
#   assign_lag       minutes from a dashboard assignment to the first call.
# -----------------------------------------------------------------------------
import re

import pandas as pd

IDLE_MIN = 45          # on shift and nothing submitted for this long -> flagged
NOT_STARTED_MIN = 30   # on shift this long without a single call -> flagged
DAY_END = "21:00"      # when no schedule says otherwise


def _mins(t):
    m = re.match(r"\s*(\d{1,2}):(\d{2})", str(t))
    return int(m[1]) * 60 + int(m[2]) if m else None


def calls_of(subs, is_sup):
    """Real calls (supervisor rows are paperwork): pid, enumerator, t (time),
    callcode."""
    cols = ["pid", "enumerator", "t", "callcode"]
    if subs is None or subs.empty or "pid" not in subs.columns:
        return pd.DataFrame(columns=cols)
    d = subs[~is_sup(subs)]
    t = pd.to_datetime(d.get("endtime", pd.Series("", index=d.index)), errors="coerce",
                       utc=True, format="ISO8601").dt.tz_localize(None)
    if "starttime" in d.columns:
        t = t.fillna(pd.to_datetime(d["starttime"], errors="coerce", utc=True,
                                    format="ISO8601").dt.tz_localize(None))
    out = pd.DataFrame({"pid": d["pid"].astype(str).str.strip().values,
                        "enumerator": d.get("enum_name", pd.Series("", index=d.index))
                        .astype(str).str.strip().values,
                        "t": t.values,
                        "callcode": d.get("callcode", pd.Series("", index=d.index))
                        .astype(str).str.strip().values})
    return out.dropna(subset=["t"])


def lists_today(roster, calls, lists, now, list_end=None):
    """One row per call list in `lists`: list, pids, started_today, left,
    completed_today, last_hour (pids started in the last 60 min), rate_per_hour
    (since the first start today), projected (time all would be started at
    that rate, or None), day_end, on_track (True / False / None)."""
    cols = ["list", "pids", "started_today", "left", "completed_today", "last_hour",
            "rate_per_hour", "projected", "day_end", "on_track"]
    if roster is None or roster.empty or not lists:
        return pd.DataFrame(columns=cols)
    first = calls.sort_values("t").drop_duplicates("pid").set_index("pid")["t"] \
        if len(calls) else pd.Series(dtype="datetime64[ns]")
    today = now.normalize()
    done_today = set(calls.loc[(calls["callcode"] == "1") & (calls["t"] >= today), "pid"]) \
        if len(calls) else set()
    al = roster["assigned_list"].fillna("").astype(str).str.strip()
    state = roster.get("case_state", pd.Series("", index=roster.index)).astype(str)
    elig = pd.to_numeric(roster.get("eligible_to_call", 1), errors="coerce").fillna(1) == 1
    rows = []
    for name in lists:
        g = roster[(al == name) & elig]
        f = g["pid"].map(first)
        started = f[(f >= today)]
        left = int((state[g.index] == "Not assigned").sum())
        last_hour = int((started >= now - pd.Timedelta(hours=1)).sum())
        rate, proj = None, None
        if len(started):
            hours = max((now - started.min()).total_seconds() / 3600, 0.25)
            rate = len(started) / hours
            proj = now + pd.Timedelta(hours=left / rate) if left and rate else None
        end_txt = (list_end or {}).get(name, DAY_END)
        end_m = _mins(end_txt) or _mins(DAY_END)
        end = today + pd.Timedelta(minutes=end_m)
        on_track = None if not left else (proj is not None and proj <= end)
        rows.append({"list": name, "pids": len(g), "started_today": len(started), "left": left,
                     "completed_today": int(g["pid"].isin(done_today).sum()),
                     "last_hour": last_hour, "rate_per_hour": rate, "projected": proj,
                     "day_end": end_txt, "on_track": on_track})
    return pd.DataFrame(rows, columns=cols)


def people_today(calls, schedule, checkins, now, first_call_pids=None):
    """One row per enumerator planned today or calling today: enumerator, list,
    shift, on_shift, checked_in, calls, new_cases, completes, first_call,
    last_call, idle_min (minutes since the last submission, only while on
    shift), calls_per_hour (calls / hours on shift so far), flag."""
    cols = ["enumerator", "list", "shift", "on_shift", "checked_in", "calls", "new_cases",
            "completes", "first_call", "last_call", "idle_min", "calls_per_hour", "flag"]
    today = now.normalize()
    c = calls[calls["t"] >= today] if len(calls) else calls
    sch = schedule[schedule["date"] == now.date()].drop_duplicates("enumerator") \
        if schedule is not None and len(schedule) else pd.DataFrame(columns=["enumerator", "list", "shift"])
    inn = set(checkins.loc[(checkins["date"] == now.date())
                           & checkins["arrived"].astype(str).str.strip().ne(""), "enumerator"]) \
        if checkins is not None and len(checkins) else set()
    # a "new case" = a pid whose first ever call is this person's, today
    _first = calls.sort_values("t").drop_duplicates("pid") if len(calls) else calls
    _first = _first[_first["t"] >= today] if len(_first) else _first
    new_by = _first.groupby("enumerator").size() if len(_first) else pd.Series(dtype=int)
    t_now = now.hour * 60 + now.minute
    names = list(dict.fromkeys(list(sch["enumerator"]) + list(c["enumerator"] if len(c) else [])))
    s_i = sch.set_index("enumerator")
    rows = []
    for n in names:
        if not n:
            continue
        mine = c[c["enumerator"] == n] if len(c) else c
        shift = s_i.loc[n, "shift"] if n in s_i.index else ""
        a = _mins(str(shift).split("–")[0]) if shift else None
        b = _mins(str(shift).split("–")[-1]) if shift else None
        on = a is not None and b is not None and a <= t_now < b
        last = mine["t"].max() if len(mine) else None
        idle = int((now - last).total_seconds() // 60) if (on and last is not None) else None
        worked = None
        if a is not None:
            worked = (min(t_now, b or t_now) - a) / 60
        elif len(mine) > 1:            # not on the schedule: first to last call
            worked = (mine["t"].max() - mine["t"].min()).total_seconds() / 3600
        cph = round(len(mine) / worked, 1) if worked and worked > 0.25 else None
        flag = ""
        if on and last is None and a is not None and t_now - a >= NOT_STARTED_MIN:
            flag = f"🟠 no call yet, {t_now - a} min into the shift"
        elif idle is not None and idle >= IDLE_MIN:
            flag = f"🔴 nothing submitted for {idle} min"
        rows.append({"enumerator": n,
                     "list": s_i.loc[n, "list"] if n in s_i.index else "",
                     "shift": shift, "on_shift": "on now" if on else "",
                     "checked_in": "✅" if n in inn else "—",
                     "calls": len(mine),
                     "new_cases": int(new_by.get(n, 0)),
                     "completes": int((mine["callcode"] == "1").sum()) if len(mine) else 0,
                     "first_call": f"{mine['t'].min():%H:%M}" if len(mine) else "",
                     "last_call": f"{last:%H:%M}" if last is not None else "",
                     "idle_min": idle, "calls_per_hour": cph, "flag": flag})
    out = pd.DataFrame(rows, columns=cols)
    out["idle_min"] = pd.to_numeric(out["idle_min"], errors="coerce").astype("Int64")
    out["calls_per_hour"] = pd.to_numeric(out["calls_per_hour"], errors="coerce")
    # flagged first, then on shift now, then the rest of today's schedule,
    # then people calling who are not on the schedule
    order = [0 if f.startswith("🔴") else 1 if f.startswith("🟠") else 2 if o else 3 if sh else 4
             for f, o, sh in zip(out["flag"], out["on_shift"], out["shift"])]
    return out.assign(_o=order).sort_values(["_o", "list", "enumerator"]).drop(columns="_o") \
              .reset_index(drop=True)


def calls_by_hour(calls, now):
    """Calls per hour today: hour (e.g. '09:00'), calls, new_cases."""
    today = now.normalize()
    c = calls[calls["t"] >= today] if len(calls) else calls
    if c.empty:
        return pd.DataFrame(columns=["hour", "calls", "new_cases"])
    first = calls.sort_values("t").drop_duplicates("pid")
    new_rows = set(zip(first["pid"], first["t"]))
    c = c.assign(_h=c["t"].dt.hour,
                 _new=[(p, t) in new_rows for p, t in zip(c["pid"], c["t"])])
    hours = range(int(c["_h"].min()), max(int(c["_h"].max()), now.hour) + 1)
    g = c.groupby("_h").agg(calls=("pid", "size"), new_cases=("_new", "sum"))
    return pd.DataFrame({"hour": [f"{h:02d}:00" for h in hours],
                         "calls": [int(g["calls"].get(h, 0)) for h in hours],
                         "new_cases": [int(g["new_cases"].get(h, 0)) for h in hours]})


def assign_lag(assignments, calls, now):
    """For assignments made today: median minutes to the first call (of those
    called), how many were called, how many are still waiting."""
    today = now.normalize()
    if assignments is None or assignments.empty:
        return {"median_min": None, "called": 0, "waiting": 0}
    a = assignments[pd.to_datetime(assignments["assigned_at"]) >= today]
    if a.empty:
        return {"median_min": None, "called": 0, "waiting": 0}
    first = calls.sort_values("t").drop_duplicates("pid").set_index("pid")["t"] \
        if len(calls) else pd.Series(dtype="datetime64[ns]")
    fc = a["pid"].map(first)
    lag = (fc - pd.to_datetime(a["assigned_at"])).dt.total_seconds() / 60
    lag = lag[lag >= 0]
    return {"median_min": int(lag.median()) if len(lag) else None,
            "called": int(len(lag)), "waiting": int(fc.isna().sum())}
