# Call-Tracking Monitor — user guide (draft)

The dashboard is kept short on purpose: headings, a few flags, and tables.
Everything that explains *why* or *how it is counted* lives here.

## General

- **Sidebar filters.** The dashboard opens on the newest recruitment batch
  (date and route). Widen either filter to see earlier batches.
  The 🛂 Supervisor review tab ignores the filters.
- **Refresh now** re-reads the Google Sheet. Otherwise the data refreshes by
  itself every 5 minutes.
- **"Now".** Overdue, "due in the next hour" and similar timings are measured
  against the **latest submission in the data**, not the clock. During field
  hours the two are minutes apart.
- **Who is in the dashboard.** Only roster pids whose `phone_sample_status` is
  *Eligible - signed up*. Refusals and ineligibles at recruitment cannot be
  called, so they are excluded from every figure.
- **Red rows** are overdue callbacks.
- **Shift windows:** A 07:30–09:30 · B 09:30–12:30 · C 12:30–15:30 ·
  D 15:30–17:30 · E 17:30–20:30. `shifts_to_try` lists the windows not tried
  yet, or *All tried* / *None tried*.

### Status from the callcode

| Callcode | Status |
|---|---|
| `1` | Completed (inactive) |
| `0_*` | Closed (inactive) |
| `2_*` | Pending (open) |
| `3_*`, `4_*` | Active (open) |

- A **completion is final**: calls logged after it are ignored (and flagged).
- A **supervisor closure is final**: enumerators cannot reopen it. A completion
  still beats an earlier closure.
- An **enumerator's closure** is not final until the supervisor validates it
  (see Supervisor review).

### Closing codes

| Code | Meaning |
|---|---|
| `0_R` | Refusal |
| `0_UN` | Ineligible — under 18, outside the Western Area, or no phone |
| `0_IN` | Incorrect respondent — wrong person, numbers exhausted |
| `0_WN` | Wrong number — numbers exhausted |
| `0_OC` | Other contact |
| `0_NA`, `0_OF`, `0_D`, `0_SC` | Supervisor only: no answer, phone off, keeps dropping, keeps rescheduling |

## 🧾 Calls summary

- **Indicators at a glance**
  - *Ever attempted* — pids dialled at least once.
  - *Completed — of sample* — completions over everyone in the sample.
  - *Completed — of contacted* — completions over the pids who ever picked up.
  - *Total attempts* — and the average per attempted pid.
- **Active pids** — how many pids sit in each work queue (see Active pids).
  *To be resumed* overlaps the others: a `4_SC` held partial also has a
  callback time.
- **Inactive pids** — completed and closed pids, by reason.
- **Warnings** appear when a pid was called **after it completed**, or
  **after a supervisor closed it**. The pid stays closed and out of the queues;
  the warning is there because the call should not have happened. The
  enumerator and callcode shown are from the latest call.

## ✅ Active pids — the work queues

Every open pid sits in exactly one queue, except *To be resumed*, which also
shows held partials that belong to another queue.

### ⏰ To be followed up

- **Codes:** `2_D`, `2_OC`, `2_SC`, `3_SC`, `4_SC` — a callback time was agreed,
  or the call dropped and needs chasing.
- Most imminent first; **overdue rows are red** and sit at the top.
- A callback stays here however many shifts or attempts it has taken and however
  overdue it is: a time was promised to the respondent. Callbacks are **never**
  moved to *To be reviewed*.
- `callback_due` — *Not specified, check final comments* when the form has no
  time (a drop, or a reschedule logged without one).
- `callback_by` — who calls back, as recorded on the form (`enumerator` or
  `respondent`). Blank on the form = `enumerator`.
- `comments` — the reason given (`rsd_reason`), then the final comment.
- Flags: **overdue**, **due in the next hour**.

### 📵 To keep calling

- **Codes:** `2_IN`, `2_NA`, `2_OF`, `2_WN` — no answer / phone off, or a wrong
  number / incorrect respondent with other numbers still to try.
- **Fewest attempts first**, then fewest shift windows tried, then whoever has
  waited longest.
- `numbers_wrong` — the most numbers any single call found wrong. The form
  counts per call, not in total, so this never overcounts a dud found again.
- Flag: **1 attempt only**.

### ▶️ To be resumed (partial saved)

- Every pid whose **last submission is a `4_`** (`4_D`, `4_SC`, `4_NA`, `4_OF`)
  — a half-finished interview still held on a tablet — plus `3_D`.
- Resume it from **that tablet**; if that is not possible, hand the pid to
  another enumerator.
- A blank `callback_due` is normal on a `4_D` / `4_NA` / `4_OF` hold: only an
  *Updated reschedule* log asks for a day and time.
- A pid leaves this list as soon as anything later is logged against it.
- Flags: **held on a tablet now**, **no tablet id**.

### 🔎 To be reviewed (hard to reach)

- Open pids worked hard with nothing to show: **4 or more of the 5 shift
  windows tried, or more than 8 attempts**, and still not complete. Also any
  open callcode that matches none of the queues above.
- Callbacks are excluded (they stay in *To be followed up*).
- `review_reason` says which condition fired.
- Flags: **more than 8 attempts**, **4+ shifts tried**.

### 🆕 To be assigned

- Eligible sign-ups not worked yet — assign these first, across the day.
- A pid closed by a supervisor without a call is *not* listed here.

### Supervisor note

When the supervisor sends a case back or says *keep trying* in Supervisor
review, the note appears as `supervisor_note`, right after the pid.

## 🗄️ Inactive pids

- Completed interviews (`1`) and closed pids (`0_`).
- `validated` — ✔ once the supervisor has confirmed or corrected it in
  Supervisor review (final: a later call will not reopen it), *pending* until
  then. A supervisor's own closure counts as validated.
- `ineligible_type1` — ineligible: under 18 (`d02_check`), outside the Western
  Area (`d04_yn`), or no phone at recruitment.
- `is_supervisor` — Yes when a supervisor closed the pid.
- `was_partialsaved` — history: was it ever partial-saved on the way to
  being closed.
- Supervisors: check regularly that closed pids are out of everyone's calling
  pile, so people who should not be contacted are not called again.

## 💾 Partial saves

- A partial save is stored **on the tablet it was taken on** and can only be
  resumed from that device. This tab shows which tablet holds each one and who
  to ask.
- What counts: the pid's **last submission is a `4_`** (`4_D` held partial,
  `4_SC` upcoming reschedule, `4_NA` / `4_OF` a further attempt on a held
  case). Only what is still outstanding; earlier partial saves that were
  resumed or closed are in `was_partialsaved` on the Tracking sheet.
- **No tablet id** — cannot be traced to a device; start from the enumerator
  who last submitted.
- **Tablet id that is not a tablet** — a callcode or the pid was typed in the
  `rs_tab_id` box. A data-entry problem worth raising with the field team.
- *By tablet*: `enumerators` = everyone who last submitted on a pid held by
  that tablet; `last_submission` = the latest among them.
- *Every partial save*: `enumerator` and `last_submission_time` are the log
  that put it on the tablet; `current_callcode` says which kind of hold it is.

## 👤 By enumerator

- **On the plate now** — ACTIVE pids each enumerator currently holds (they made
  the last submission). *resume (4_)* = held partials on their device;
  *follow-up (3_SC / 3_D)*. The two add up to the total.
- **Callback times** — earliest first; ⚠ = overdue; a time alone means today.
  Held partials and drops carry no scheduled time.
- **Historic effort** — credited to whoever made each call, across the whole
  history. *pids tried* overlaps between enumerators (several people try the
  same respondent), so it does not add up to the sample.

## 📋 Tracking sheet

One row per respondent. *Current state* columns come from the most recent
submission; *history* columns add up all submissions. Never-called respondents
show an empty current state.

## 🛂 Supervisor review (editing is password-locked)

- Covers the **whole roster** — the sidebar filters do not apply.
- Every save adds a row to the `decisions` tab of the Google Sheet; nothing is
  overwritten, and the newest row per pid counts.
- **Validate closures** — every case an enumerator closed (`0_`) or completed
  (`1`) not yet reviewed.
  - *Confirm* / *Correct* make it final; a later enumerator call will not reopen
    it (it is flagged instead). A wrong complete is corrected to a `0_` code.
  - *Send back* returns it to the queue it came from, with your comment shown
    to the enumerators.
  - Tick several rows and use *Confirm selected* for the backlog.
- **Decide open cases** — the *To be resumed* and *To be reviewed* queues.
  *Close* (with a `0_` code, final) or *Keep trying* (the comment is shown to
  the enumerators).
- **Reviewing one pid** shows its history (attempts, last real call,
  reschedules, drops, shifts not yet tried, partial saved) and, in the
  dropdown, every observation. Grey rows are Communications form submissions
  (timing / status only, never interview content); blue italic rows are
  earlier dashboard entries.
- **Log** — the `decisions` tab, newest first.
