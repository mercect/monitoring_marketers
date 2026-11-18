# column_help.py — what each column means, shown as a tooltip on its heading.
# -----------------------------------------------------------------------------
# Hover over a column heading in any table (tap it on a tablet or phone) to see
# its explanation. One list for the whole dashboard, so a column means the same
# thing wherever it appears. A column not listed here simply has no tooltip.
# Times are Sierra Leone time (GMT).
# -----------------------------------------------------------------------------
import streamlit as st

HELP = {
    # who / which list
    "pid": "The respondent's ID.",
    "assigned_list": "Which call list the pid is on: the day-4 or day-5 list of its recruitment day.",
    "call_order": "The pid's position in its call list. First calls go strictly in this order.",
    "route_recruited": "Route where the respondent was recruited.",
    "date_recruited": "Day the respondent was recruited.",
    "phone_sample_status": "Sign-up status at recruitment (only 'Eligible - signed up' can be called).",
    "batch_code": "Recruitment batch code.",
    "case_state": "Overall state: Not assigned, Active or Archived.",
    # the last call
    "callcode": "Code of the latest submission for this pid.",
    "last_callcode": "Code of the latest submission for this pid.",
    "current_callcode": "Code of the latest submission for this pid.",
    "status": "Open (ACTIVE / PENDING) or closed (INACTIVE), from the callcode.",
    "current_status": "Open (ACTIVE / PENDING) or closed (INACTIVE), from the callcode.",
    "enumerator": "Who made the latest submission.",
    "last_enumerator": "Who made the latest submission.",
    "last_submission_time": "When the latest form of ANY kind was submitted for this pid "
                            "(including no answer, phone off, or a supervisor entry).",
    "last_contact_date": "When someone last actually SPOKE to the respondent (answered, "
                         "rescheduled, dropped mid-call or completed). 'never reached' = nobody "
                         "has spoken to them yet.",
    "comments": "The reason given (rsd_reason), then the final comment of the latest call.",
    "last_comment": "Final comment of the latest call (the supervisor's comment for a supervisor closure).",
    "rsd_reason": "Reason given for the reschedule or drop.",
    "supervisor_note": "Note from the supervisor (keep trying / sent back / reopened). Read it before calling.",
    # callbacks
    "callback_due": "Time agreed for the callback. 'Not specified' = no time on the form; check the comments.",
    "callback_by": "Who calls back: enumerator or respondent (blank on the form = enumerator).",
    "callback_when": "How soon the callback is due: overdue, within 1h, today, tomorrow, later.",
    # effort
    "total_attempts": "Calls made to this pid so far (supervisor entries do not count).",
    "ever_picked_up": "Has the respondent ever been on the line?",
    "times_dropped": "Calls that ended in a drop.",
    "times_rescheduled": "Calls that ended in a reschedule.",
    "times_noanswer": "Calls nobody answered.",
    "times_off": "Calls that found the phone off.",
    "times_refused": "Calls that ended in a refusal.",
    "times_wrongnumber": "Calls that ended on a wrong number.",
    "shifts_to_try": "Shift windows nobody has called in yet (A 07:30–09:30 · B 09:30–12:30 · "
                     "C 12:30–15:30 · D 15:30–17:30 · E 17:30–20:30).",
    "numbers_wrong": "The most numbers any single call found wrong (the form counts per call).",
    "numbers_tried_of_available": "Phone numbers tried out of those the respondent gave.",
    "days_open": "Days since the first submission for this pid.",
    "review_reason": "Why the pid is hard to reach: 4+ of 5 shifts tried and/or more than 8 attempts.",
    # partial saves
    "active_partialsave": "Is a half-finished interview still held on a tablet?",
    "was_partialsaved": "Was this pid ever partial-saved (history)?",
    "tab_id": "Tablet holding the partial save (rs_tab, or rs_tab_oth when typed as other).",
    "last_section": "How far the interview got.",
    # assigning
    "skipped": "Never called although its call list has moved more than one round past it.",
    "days_waiting": "Days since the respondent was recruited.",
    "assign": "Tick to hand this pid out.",
    "assign_to": "Enumerator who gets the pid.",
    "assigned_to": "Enumerator the pid was assigned to.",
    "assigned_by": "Supervisor who recorded the assignment.",
    "assigned_at": "When it was assigned.",
    "minutes_waiting": "Minutes since it was assigned (or reactivated) without a call. For assignments, red after 30.",
    "reactivated_at": "When the supervisor sent the case back or reopened it.",
    "action": "What the Data Team decided: back to work (call it, read the note) or closed (stop calling, update the tracking sheet).",
    "called_at": "First call after the case was sent back.",
    "done_at": "When the supervisor ticked done in To be revised (blank = never).",
    "reactivated_by": "Supervisor who sent the case back or reopened it.",
    "reason": "Reason given for assigning out of order.",
    "called_by": "Who made the first call.",
    "first_call_at": "When the first call was made.",
    # attendance
    "off_plan": "Pids this person worked that day on a list other than their planned one.",
    "off_plan_%": "Share of this person's pids that day that were on another list.",
    "assigned_only": "Assigned to them that day, not called by them yet.",
    "on_plan": "✅ the planned list · 🟠 another list · 🔵 the person is not on the schedule.",
    "planned_list": "The list the schedule gives this person for the day.",
    "shift": "Planned shift (from the schedule; from the check-in form if not planned).",
    "checked_in": "Filled the start-of-day form that day.",
    "arrived": "Arrival time on the start-of-day form.",
    "late_min": "Minutes after the shift start, from the start-of-day form. Flagged after 15.",
    "left": "Time on the end-of-day form, if filled.",
    "tablet": "Tablet taken for the day, from the start-of-day form.",
    "topup": "Top-up check on the start-of-day form (sod_topup).",
    "device_end": "Device status on the end-of-day form, if filled.",
    "topup_end": "Top-up status (or amount) on the end-of-day form.",
    "balance_end": "Airtime balance left, on the end-of-day form.",
    "calls": "Calls this person submitted on the day.",
    "on_list": "Calls on pids of their planned list.",
    "other_list": "Calls on pids of another list (all calls, if not on the schedule).",
    "other_lists": "Which other lists, and how many calls on each.",
    "pid_list": "The list (assigned_list) of the pid called.",
    "time": "Time of the call.",
    "list": "Call list (assigned_list), e.g. 12Nov-D4.",
    "people": "Who is planned: ✅ checked in · ⏳ not checked in · 🟠 calling another list.",
    # completed / partially completed
    "topup_amount": "Top-up sent (topup_rate; for a partial save, the resume top-up if that is the only one).",
    "topup_mobile_lab": "How the top-up was sent: Top Up or Mobile Money.",
    "topup_received": "Has this pid already been paid any top-up (partial interview, partial save log or complete)?",
    "topup_total": "Sum of every top-up paid to this pid so far.",
    "topups": "Each top-up: amount · how · date (partial interview / partial save log / complete).",
    "full_end_survey": "End-of-survey answer on a full interview (from the form).",
    "partial_end_survey": "End-of-survey answer on a partial interview (from the form).",
    "duration_min": "Length of the completing interview in minutes (the form's duration).",
    "stop_reason": "Why the interview stopped: reschedule or drop.",
    "last_section_n": "Number of the last section reached.",
    "qc1_noise": "Survey quality: background noise (not on the form yet if blank).",
    "qc2_drops": "Survey quality: call drops during the interview.",
    "qc3_underst": "Survey quality: did the respondent understand the questions?",
    "qc4_attention": "Survey quality: was the respondent paying attention?",
    "qc4_attention_yes": "Survey quality: details on attention.",
    "qc6_difficult": "Survey quality: was anything difficult?",
    "qc6_difficult_other": "Survey quality: what else was difficult.",
    # pace today
    "on_shift": "On now = their scheduled shift covers the current time.",
    "new_cases": "Pids whose first ever call this person made today.",
    "completes": "Interviews completed today.",
    "first_call": "Time of their first call today.",
    "last_call": "Time of their latest call today.",
    "idle_min": "Minutes since their last submission, while on shift. Flagged after 45.",
    "calls_per_hour": "Calls today per hour on shift so far (first to last call if not on the schedule).",
    # closed pids
    "validated": "✔ once a supervisor confirmed or corrected the closure (final); pending until then.",
    "is_supervisor": "Was the pid closed by a supervisor?",
    "ineligible_type1": "Ineligible: under 18, outside the Western Area, or no phone at recruitment.",
    "attrition_reason": "Reason the case was lost, when recorded.",
    "refuse_why": "Reason given for refusing (rs_refuse_why when the refusal came on the notification form).",
    "notification_form": "Yes = the refusal was recorded on the notification (Communications) form, not the Long Survey.",
    "c01": "Consent question (c01) answer on the form.",
    "is_incorrect": "Wrong person reached.",
    "is_othercontact": "Reached someone else who knows the respondent.",
    "calls_after_close": "Calls logged after a supervisor closed the pid.",
    # review tab
    "kind": "closure (0_) or complete (1).",
    "flag": "Warning about this closure, e.g. an enumerator using a supervisor-only code.",
    "queue": "Which list the pid comes from. 💬 says closed = its comments say the case is closed.",
    "select": "Tick to act on several pids at once.",
}


def help_config(columns, base=None):
    """column_config for st.dataframe / st.data_editor: a tooltip on every
    known column heading. Settings already in `base` (e.g. a checkbox column)
    keep their type and gain the help text if they had none."""
    cfg = dict(base or {})
    for c in columns:
        text = HELP.get(c)
        if not text:
            continue
        if c in cfg and cfg[c] is not None:
            existing = cfg[c]
            if isinstance(existing, dict):
                existing.setdefault("help", text)
            continue
        cfg[c] = st.column_config.Column(help=text)
    return cfg


def _columns_of(data):
    """Columns of a DataFrame or of a pandas Styler wrapping one."""
    inner = getattr(data, "data", None)
    return list(inner.columns) if inner is not None and hasattr(inner, "columns") \
        else list(getattr(data, "columns", []))


def dataframe(data, **kwargs):
    """st.dataframe with a tooltip on every known column heading."""
    kwargs["column_config"] = help_config(_columns_of(data), kwargs.get("column_config"))
    return st.dataframe(data, **kwargs)


def data_editor(data, **kwargs):
    """st.data_editor with a tooltip on every known column heading."""
    kwargs["column_config"] = help_config(_columns_of(data), kwargs.get("column_config"))
    return st.data_editor(data, **kwargs)
