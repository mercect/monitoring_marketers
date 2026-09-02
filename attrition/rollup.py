# rollup.py — turn the raw phone-survey submission export into a case table.
# -----------------------------------------------------------------------------
# The survey (Kobo) emits ONE ROW PER CALL ATTEMPT / SUBMISSION. Monitoring needs
# ONE ROW PER CASE (pid). This module does that rollup, implementing the division
# of labor described in context/monitoring_flags.md:
#   - survey emits per-submission facts (callcode, status, flags…);
#   - here we add everything that needs cross-submission history or a live "now"
#     (attempts, resume_rounds, days_open, callback_overdue, action_bucket).
#
# It is intentionally plain pandas so it can run inside the dashboard with no
# external pipeline. Column names follow the survey field names exactly.
# -----------------------------------------------------------------------------
import re

import pandas as pd


def sheet_csv(url: str) -> str:
    """Turn a normal Google-Sheets URL (the one from your browser address bar,
    e.g. .../spreadsheets/d/<id>/edit#gid=<gid>) into a CSV endpoint pandas can
    read. Requires the sheet be shared 'Anyone with the link – Viewer'. Links
    that are already CSV/published/export are returned unchanged; anything that
    isn't a Sheets URL is passed straight through (e.g. a local file path)."""
    url = (url or "").strip()
    if not url or any(t in url for t in ("format=csv", "output=csv", "tqx=out:csv", "/pub?")):
        return url
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", url)
    if not m:
        return url
    gid = re.search(r"[#&?]gid=([0-9]+)", url)
    return (f"https://docs.google.com/spreadsheets/d/{m.group(1)}"
            f"/export?format=csv&gid={gid.group(1) if gid else '0'}")

# Status vocabulary, derived from the callcode prefix (authoritative):
#   1 / 0_* -> INACTIVE (incl. supervisor closures), 2_* -> PENDING,
#   3_* / 4_* -> ACTIVE.
OPEN_STATUSES = {"ACTIVE", "PENDING", "NOTIFICATION"}


def status_for(callcode, emitted=""):
    """Status family from the callcode prefix. Overrides the survey's emitted
    status so 4_* reads ACTIVE (not NOTIFICATION) and 0_* supervisor closures read
    INACTIVE. Falls back to the emitted status when the callcode is blank/odd."""
    cc = str(callcode).strip()
    if cc == "1" or cc.startswith("0_"):
        return "INACTIVE"
    if cc.startswith("2_"):
        return "PENDING"
    if cc.startswith(("3_", "4_")):
        return "ACTIVE"
    return str(emitted).strip().upper()

# callcode groupings used for bucketing / issues.
# Keep-calling = keep dialing (no-answer/off) OR still has numbers to try (wrong
# number / incorrect respondent).
CC_KEEP_CALLING = {"2_NA", "2_OF", "2_WN", "2_IN"}
CC_CALLBACK = {"2_SC", "3_SC", "4_SC", "2_OC", "2_D"}  # scheduled callback OR dropped-call follow-up
CC_RESUME = {"3_D", "4_D", "4_NA", "4_OF"}             # a held drop/partial to resume
CC_EXHAUSTED = {"0_WN", "0_IN"}                        # all numbers tried -> closed (issues only)


# =============================================================================
# ACTIVE PARTIAL SAVE
# -----------------------------------------------------------------------------
# A partial save is a half-finished interview sitting on the tablet it was taken
# on. It only EXISTS while it is still being held, so the question is about the
# pid's CURRENT state, not its history.
#
# The survey logs one through the notification path (survey_id = -3), which is
# the only producer of 4_* callcodes — 4_D held partial, 4_SC upcoming
# reschedule, 4_NA / 4_OF a fresh attempt on a case already held. Every one of
# them asks rs_tab_id ("the ID in which this case is stored as a partial save"),
# so a 4_* IS a held partial, whichever suffix it carries. Hence:
#
#       the LATEST submission's callcode starts with 4_  ->  still partial-saved
#
# Anything logged since — a completion (1), a 2_*/3_* call outcome, a supervisor
# closure — means it was resumed or closed, so it is NOT a partial save any more.
#
# NB: this deliberately replaces the old ever-flag test on `was_partialsaved`,
# which never expired: a pid partial-saved once stayed flagged forever, including
# after it completed. On the 2026-09 sheet that flagged 6 pids, 3 of them already
# finished, while missing 9 partials that were genuinely still out there.
# `was_partialsaved` survives on the summary as HISTORY ("was this ever partial-
# saved?"), but nothing asking "is there a partial save to chase?" may read it.
# =============================================================================
def is_active_partialsave(callcode) -> bool:
    """True when this callcode means a partial save is STILL held on a device."""
    return str(callcode).strip().startswith("4_")


# rs_section value -> the readable label the enumerator picked (XLSForm choice
# list `section_partialsave`). A 4_* submission records how far the interview got
# in rs_section and leaves last_section blank, so the Partial saves tab has to
# translate it. There is no 5.
PARTIALSAVE_SECTIONS = {
    "1": "Section A. Identity confirmation",
    "2": "Section B. Recall",
    "3": "Section C. Consent",
    "4": "Section D. Demographics",
    "6": "Section P. Purchases",
    "7": "Section S. Symptoms and drugs purchased/taken",
    "8": "Section E. Travel and marketer exposure",
    "9": "Section EO. Economic outcomes",
    "10": "Section: Pharmacy",
    "11": "Section M. Marketers",
}


# =============================================================================
# TEST / DUMMY SUBMISSIONS
# -----------------------------------------------------------------------------
# Rows the field team logged while trying the form out. They are NOT observations
# and must never reach a count, a rate, a queue or an export. Dropped at every
# entry point (data_io.load_submissions, generate_daily_csv, validate_setup,
# test_rollup) so no consumer can accidentally include them.
# Matched on enum_name, case- and whitespace-insensitive. To retire another test
# account, add its lower-cased name here — that is the only change needed.
# =============================================================================
TEST_ENUMERATORS = {"testing"}


def drop_test_attempts(submissions: pd.DataFrame):
    """(clean submissions, n_dropped). Removes rows whose enum_name is a known
    test account. A sheet with no enum_name column passes through untouched."""
    if "enum_name" not in submissions.columns or submissions.empty:
        return submissions, 0
    is_test = (submissions["enum_name"].astype(str).str.strip().str.lower()
               .isin(TEST_ENUMERATORS))
    return submissions.loc[~is_test].copy(), int(is_test.sum())


def _norm_cols(df):
    """Kobo CSV exports prefix fields with their group path (e.g.
    'callcodes_group/status'). Keep only the final segment so lookups are simple."""
    df = df.copy()
    df.columns = [str(c).split("/")[-1].split(".")[-1].strip() for c in df.columns]
    # de-duplicate collisions (keep first)
    df = df.loc[:, ~pd.Index(df.columns).duplicated()]
    return df


def _s(df, name, default=""):
    return df[name] if name in df.columns else pd.Series([default] * len(df), index=df.index)


def _truthy(series):
    return series.astype(str).str.strip().str.lower().isin(["1", "1.0", "true", "yes", "y"])


def _to_dt(series):
    # Normalize to tz-naive. Survey times are UTC ('...Z'); other fields
    # (e.g. callback_due) are naive. Sierra Leone is UTC+0 year-round, so
    # wall-clock == local time and utc=True doesn't shift the hour.
    dt = pd.to_datetime(series, errors="coerce", utc=True)
    return dt.dt.tz_localize(None)


# =============================================================================
# RECRUITMENT ELIGIBILITY  (roster-level, from the SAMPLE tab)
# -----------------------------------------------------------------------------
# Eligibility determinants now arrive as INDEPENDENT variables on the sample
# tab, one per reason, so they are counted separately as well as OR-ed together.
# Two stages, same formula, different exclusion sets:
#   Stage I  (after data entry)  - recruitment-time determinants only.
#   Stage II (after phone calls) - Stage I PLUS the call outcomes that show the
#                                  person was never reachable / not the right one.
# =============================================================================

# label shown in the breakdown -> the sample-tab column(s) that can carry it, in
# priority order. The FIRST one present on the sheet wins, so each determinant is
# counted exactly once whether the tab uses the 2026-08 data-entry names
# (`ineligible_*`) or the earlier roster names (`is_*`). A determinant whose
# columns are all absent contributes nothing, so one can be added to the sheet
# later and is picked up here automatically.
ELIG_FLAG_LABELS = {
    "underage": ("ineligible_underage", "is_underage"),
    "outside Western Area": ("ineligible_outside_western_area", "is_owa"),
    "deaf or mute": ("ineligible_deaf_mute", "is_disabled"),
    "language issue": ("ineligible_language_barrier", "is_language"),
    "seated in row X": ("ineligible_row_x",),
}
# Retired determinant. `ineligible_nonpassenger_card` is still on the sample tab
# but is no longer read, so it screens nobody out and shows no breakdown row.
# Verified when it was dropped: all 30 sign-ups it fired on were already excluded
# by another determinant, so no reported figure moved. Put the entry back in
# ELIG_FLAG_LABELS above to reinstate it.
# Sign-up status and phone-count columns, same first-one-wins rule.
SIGNUP_STATUS_COLS = ("phone_sample_status", "rec_signup")
PHONE_COUNT_COLS = ("number_of_phone_numbers", "phones_provided")

# Sample-tab columns the DASHBOARD must not show or hand out. `pitch` is the
# recruitment arm and the rest are baseline covariates — none of them belong in
# a monitoring view the field team reads. Dropped by data_io.load_all() AFTER
# the rollup, so eligibility and every derived column are still computed from
# the full sheet; only the published frame is trimmed. The on-disk audit export
# (generate_daily_csv.py) is deliberately NOT trimmed — it is the verification
# trail and stays local.
HIDE_FROM_DASHBOARD = (
    "pitch", "signup_scan", "age", "education",
    "pre_symptom_pain", "pre_symptom_fever", "pre_symptom_malaria",
    "pre_symptom_cold", "pre_symptom_cough", "pre_symptom_none",
)


def hide_columns(df, keep=()):
    """Drop HIDE_FROM_DASHBOARD from a frame about to be published on screen.

    `keep` un-hides named columns for a view that legitimately needs them. The PI
    dashboard passes `pitch` so it can break recruitment down by trial arm; the
    call-tracking monitor passes nothing, so the arm still never reaches the
    screen the field team reads. Keep it that way — showing callers which arm a
    respondent was recruited under is what this list exists to prevent."""
    return df.drop(columns=[c for c in HIDE_FROM_DASHBOARD
                            if c in df.columns and c not in keep])
# =============================================================================
# STAGE-II (CALL-SIDE) DETERMINANTS
# -----------------------------------------------------------------------------
# Two ways a call can screen someone out.
#
# (a) A closing callcode that means the number never reaches the right person.
#     These roll up under "no phone number", because that is what they amount to:
#     we have no working way to reach this respondent. `0_NA` is deliberately NOT
#     here - no answer is an unresolved case, not an established ineligibility.
#
# (b) A `0_UN` closure - "ineligible respondent, established during the call".
#     The code alone does not say WHY, so the reason is read off a companion
#     variable on the same submission and the pid is counted under the SAME
#     heading as its data-entry equivalent, just identified at the phone survey
#     instead of at recruitment. That is the whole point of splitting the
#     breakdown into "identified at recruitment" / "identified at phone survey":
#     one determinant, two possible moments of discovery.
# =============================================================================
UN_CALLCODE = "0_UN"

# reason label -> (submission column, the value that establishes it).
# `d02_check` is "Are you over 18 years old?" and `d04_yn` is "Do you live in
# the Western Area?", so 0 (No) is what makes the respondent ineligible.
# `no_phone_ineligible` is the form's own flag (a12=0 and a13=0).
UN_REASONS = {
    "no number is their own": ("no_phone_ineligible", "1"),
    "underage": ("d02_check", "0"),
    "outside Western Area": ("d04_yn", "0"),
}

# The "no phone number" group and its sub-categories, in display order.
# Closures that mean we have no working way to reach the intended respondent:
# the number is not theirs (`0_WN`), the person reached is not them (`0_IN`), or
# the only contact we have is someone else and every number has been tried
# (`0_OC`). All three are "wrong respondent", not "unresolved".
WRONG_PERSON_CODES = ("0_IN", "0_WN", "0_OC")
UNREACHABLE_CODES = ("0_OF",)

# Children are labelled with the arrow alone - they sit directly under the
# parent row in the table, so repeating "no phone number" on every line only
# pushed the part that differs off to the right. The codes are interpolated from
# the tuples above rather than retyped, so a code can never be added to the rule
# while the row label still advertises the old list.
NO_PHONE = "no phone number"
NO_PHONE_FROM_ENTRY = "↳ from data entry"
NO_PHONE_NOT_OWN = "↳ no number is their own"
NO_PHONE_WRONG_PERSON = ("↳ wrong respondent ("
                         + " / ".join(WRONG_PERSON_CODES) + ")")
NO_PHONE_UNREACHABLE = ("↳ phone non-reachable ("
                        + " / ".join(UNREACHABLE_CODES) + ")")
NO_PHONE_SUBCATS = (NO_PHONE_FROM_ENTRY, NO_PHONE_NOT_OWN,
                    NO_PHONE_WRONG_PERSON, NO_PHONE_UNREACHABLE)

# The three eligibility stages the Indicators view reports. They are cumulative,
# and the ONLY difference between II and II-v2 is `0_OF` (phone off / out of
# coverage): Stage II treats it as unresolved and keeps those pids in the base,
# Stage II v2 treats it as never-reachable and removes them. Everything else is
# identical, so comparing the two rates isolates the cost of that one judgement.
STAGE_ENTRY = 1        # after data entry
STAGE_CALLS = 2        # after phone calls
STAGE_CALLS_V2 = 3     # after phone calls, counting 0_OF as never-reachable

# Every CLOSING (`0_`) callcode the form can emit, in display order, with a
# plain-English label. Eight come from the `callcode` choice list, which
# `callcode_sp` reuses for supervisor closures (`concat('0_', callcode_sp)`);
# `0_OC` is emitted by the callcode calculation itself for an other-contact
# closure and is not in that list. The Attrition view breaks `attrited` down by
# these, minus whichever ones the stage already screens out of the base.
# A `0_` code outside this dict still counts — see the catch-all in the app —
# so a supervisor entering something unexpected can never silently vanish.
CLOSING_CALLCODE_LABELS = {
    "0_R": "refusal",
    "0_D": "call drop",
    "0_SC": "reschedule",
    "0_NA": "no answer",
    "0_OF": "phone off",
    "0_OC": "other contact",
    "0_WN": "wrong number",
    "0_IN": "incorrect respondent",
    "0_UN": "ineligible respondent",
}

STAGE2_CALLCODES = set(WRONG_PERSON_CODES) | {UN_CALLCODE}
STAGE2_V2_CALLCODES = STAGE2_CALLCODES | set(UNREACHABLE_CODES)

# phone_sample_status (2026-08 sheet), punctuation-stripped -> sign-up class.
# 'Not eligible' folds the eligibility screen INTO the status, which rec_signup
# kept separate: it says the person was screened out, not whether they would have
# signed up. Classed as a sign-up here and then removed by recruit_exclusions() —
# exactly how the old sheet recorded them (an ineligible respondent still carried
# rec_signup='Sign Up'). That keeps them out of the refusal denominator, which is
# what the recruitment rate needs.
STATUS_SIGNUP_CLASS = {
    "eligiblesignedup": "signup",
    "eligiblerefused": "refusal",
    "noteligible": "signup",
}


def _first_col(df, names):
    """The first of `names` present on the frame, or None."""
    return next((c for c in names if c in df.columns), None)


def _signup_class(df):
    """Sign-up status normalised to signup / refusal / missing (missing = "").

    Reads `phone_sample_status` (2026-08 data-entry sheet), falling back to
    `rec_signup` (earlier roster). Punctuation and case are stripped so
    'Sign Up', 'sign-up' and 'SignUp' all read the same. A status value that is
    neither recognised nor blank reads as missing, so it lands outside every
    base rather than silently inflating one."""
    col = _first_col(df, SIGNUP_STATUS_COLS) or "rec_signup"
    s = (_s(df, col).astype(str).str.strip().str.lower()
         .str.replace(r"[^a-z]", "", regex=True))
    if col == "rec_signup":
        return s
    return s.map(STATUS_SIGNUP_CLASS).fillna("")


def _no_own_number(df, provided):
    """Series: none of the numbers on file is the respondent's own.

    The 2026-08 sheet carries this as a flag; the older roster carried
    `own_phone`, a COUNT of how many of the provided numbers are the
    respondent's own (verified: it never exceeds the provided count)."""
    if "ineligible_no_owned_phone" in df.columns:
        return _truthy(_s(df, "ineligible_no_owned_phone"))
    return pd.to_numeric(_s(df, "own_phone"), errors="coerce") == 0


def screen_out_masks(df: pd.DataFrame, stage: int = 1):
    """reason -> (identified-at-recruitment mask, identified-at-phone-survey mask).

    The single source of truth for the screen-out breakdown AND for eligibility:
    recruit_exclusions() is just the OR of the two masks per reason, so a reason
    can never screen someone out without also appearing on screen, or vice versa.

    Both masks are returned per reason because the SAME determinant can be
    established at either moment - the data-entry team can record a respondent as
    underage, or a caller can discover it and close the case `0_UN` with
    `d02_check = 0`. For one pid both can be true at once, so the two masks may
    overlap and their counts must not be added together.

    Blanks are NOT read as 0 - a missing flag leaves the respondent eligible,
    which matters because refusals carry no phone/demographic data at all."""
    none = pd.Series(False, index=df.index)
    cc = _s(df, "current_callcode").astype(str).str.strip().str.upper()

    def by_code(*wanted):
        """Closed by the calls with one of these codes (Stage II only)."""
        return cc.isin(wanted) if stage >= STAGE_CALLS else none

    def by_un(col, want):
        """A 0_UN closure whose reason variable reads `want` (Stage II only)."""
        if stage < STAGE_CALLS or not col or col not in df.columns:
            return none
        return (cc == UN_CALLCODE) & (_s(df, col).astype(str).str.strip() == want)

    prov = pd.to_numeric(
        _s(df, _first_col(df, PHONE_COUNT_COLS) or PHONE_COUNT_COLS[-1]), errors="coerce")

    m = {
        # "no phone number", split by HOW we came to know it.
        NO_PHONE_FROM_ENTRY: (prov == 0, none),
        # Kept disjoint from the row above so one person is not counted twice.
        NO_PHONE_NOT_OWN: (_no_own_number(df, prov) & (prov != 0),
                           by_un(*UN_REASONS["no number is their own"])),
    }
    # Call-established reasons appear only from the stage that can actually
    # establish them - a row that could never fire would be a full height of "—".
    if stage >= STAGE_CALLS:
        m[NO_PHONE_WRONG_PERSON] = (none, by_code(*WRONG_PERSON_CODES))
    if stage >= STAGE_CALLS_V2:
        m[NO_PHONE_UNREACHABLE] = (none, by_code(*UNREACHABLE_CODES))
    for label, cols in ELIG_FLAG_LABELS.items():
        rec = _truthy(_s(df, _first_col(df, cols) or cols[0]))
        col, want = UN_REASONS.get(label, (None, None))
        m[label] = (rec, by_un(col, want))
    return {k: (a.fillna(False).astype(bool), b.fillna(False).astype(bool))
            for k, (a, b) in m.items()}


def screen_out_sources(stage: int = 1):
    """reason -> (can be identified at recruitment?, can be identified on a call?).

    Structural, not data-dependent: it says where a reason CAN come from, so the
    breakdown prints "—" for a cell that is impossible rather than a 0, which
    would read as "we checked and found none". Derived from UN_REASONS and the
    callcode groups above, so adding a call-side determinant there makes its
    column light up here without a second edit."""
    call_only = {NO_PHONE_WRONG_PERSON, NO_PHONE_UNREACHABLE}
    # reasons a 0_UN closure can establish, under their display labels
    un_capable = {NO_PHONE_NOT_OWN} | (set(UN_REASONS) & set(ELIG_FLAG_LABELS))
    out = {}
    for r in (NO_PHONE, *NO_PHONE_SUBCATS, *ELIG_FLAG_LABELS):
        can_call = stage >= STAGE_CALLS and (
            r in call_only or r in un_capable or r == NO_PHONE)
        out[r] = (r not in call_only, can_call)
    return out


def recruit_exclusions(df: pd.DataFrame, stage: int = 1) -> pd.DataFrame:
    """One boolean column per exclusion reason, so the app can count each reason
    on its own AND take .any(axis=1) for the overall mask.

    A reason is true if EITHER moment established it - see screen_out_masks()."""
    return pd.DataFrame(
        {k: (rec | call) for k, (rec, call) in screen_out_masks(df, stage).items()},
        index=df.index)


def recruit_eligible(df: pd.DataFrame, stage: int = 1) -> pd.Series:
    """Signed up at recruitment AND not excluded at the given stage."""
    return (_signup_class(df) == "signup") & ~recruit_exclusions(df, stage).any(axis=1)


# The call-tracking monitor's ROSTER — its starting point for every figure it
# reports. These are the respondents who signed up AND cleared the eligibility
# screen, i.e. the only people the callers may ring.
SIGNED_UP_STATUS = "eligiblesignedup"      # phone_sample_status, punctuation-stripped


def eligible_roster(df: pd.DataFrame) -> pd.Series:
    """Boolean mask: `phone_sample_status = "Eligible - signed up"`.

    That column is the data-entry team's own verdict, so it is taken as
    authoritative rather than re-derived. On the older roster (no
    `phone_sample_status`) the equivalent pool is rec_signup='Sign Up' minus the
    determinants — recruit_eligible(stage=1) — so both sheet vintages resolve to
    the same idea. Verified on the 2026-08 sheet: the two agree on all 378 rows.

    Deliberately NOT stage 2: that keys off call outcomes, so it would drop pids
    the callers still have to work."""
    if "phone_sample_status" in df.columns:
        norm = (_s(df, "phone_sample_status").astype(str).str.strip().str.lower()
                .str.replace(r"[^a-z]", "", regex=True))
        return norm == SIGNED_UP_STATUS
    return recruit_eligible(df, stage=1)


def rollup(submissions: pd.DataFrame, now=None) -> pd.DataFrame:
    """submissions (one row per call attempt) -> cases (one row per pid)."""
    df = _norm_cols(submissions)
    if "pid" not in df.columns:
        raise ValueError("submissions must have a 'pid' column")

    df["_end"] = _to_dt(_s(df, "endtime"))
    df["_start"] = _to_dt(_s(df, "starttime"))
    df["_order"] = df["_end"].fillna(df["_start"])
    df["_is_answered"] = _truthy(_s(df, "is_answered")) | _truthy(_s(df, "pickup"))
    df["_is_resumed"] = _truthy(_s(df, "is_resumed")) | _truthy(_s(df, "rawfollow"))
    df["_survey_id"] = pd.to_numeric(_s(df, "survey_id"), errors="coerce")

    if now is None:                       # reference "now": latest activity in the data
        now = df["_order"].max()
    now = pd.to_datetime(now)

    rows = []
    for pid, g in df.groupby("pid", sort=False):
        g = g.sort_values("_order", na_position="first")
        # Completion is ABSORBING (codebook item C) and TERMINAL: once a
        # submission carries callcode 1 the interview is done, so everything
        # logged afterwards is a stray re-contact, not an observation. The group
        # is cut at the completing row, so no post-completion call reaches an
        # attempt count, a shift bucket, an effort table or the case status.
        # They are reported as a flag + a count instead of being counted.
        _done = g["callcode"].astype(str).str.strip().eq("1")
        ever_complete = bool(_done.any())
        n_after_complete = 0
        if ever_complete:
            _first = int(_done.values.argmax())          # first completing row
            n_after_complete = len(g) - _first - 1
            g = g.iloc[:_first + 1]
        recontacted = n_after_complete > 0

        latest = g.iloc[-1]
        callcode = str(latest.get("callcode", "")).strip()
        status = status_for(callcode, latest.get("status", ""))
        open_case = status in OPEN_STATUSES and status != "NOTIFICATION"

        # was (survey_id == 2), but this export has no survey_id column, so it
        # silently counted 0 for every case. Uses the shared attempt rule now.
        attempts = int(is_attempt_row(g).sum())
        ever_picked = bool(g["_is_answered"].any())
        resume_rounds = int(g["_is_resumed"].sum())

        answered_dates = g.loc[g["_is_answered"], "_end"].dropna()
        last_contact = answered_dates.max() if len(answered_dates) else pd.NaT
        first_seen = g["_order"].min()
        days_open = ""
        if status != "INACTIVE" and pd.notna(first_seen):
            days_open = int((now.normalize() - first_seen.normalize()).days)

        callback_due_raw = str(latest.get("callback_due", "")).strip()
        callback_due = _to_dt(pd.Series([callback_due_raw])).iloc[0]
        callback_overdue = int(
            open_case and callcode in CC_CALLBACK
            and pd.notna(callback_due) and callback_due < now
        )
        # when to call, relative to the reference "now"
        callback_when = ""
        if pd.notna(callback_due):
            if callback_due < now:
                callback_when = "overdue"
            elif callback_due <= now + pd.Timedelta(hours=1):
                callback_when = "within 1h"
            elif callback_due.date() == now.date():
                callback_when = "today"
            elif callback_due.date() == (now + pd.Timedelta(days=1)).date():
                callback_when = "tomorrow"
            else:
                callback_when = "later"

        numbers_wrong = pd.to_numeric(pd.Series([latest.get("numbers_wrong", 0)]), errors="coerce").iloc[0] or 0
        numbers_exhausted = int(callcode in CC_EXHAUSTED
                                or _truthy(pd.Series([latest.get("cc_incorrectnums", 0)])).iloc[0])
        new_number = str(latest.get("new_number", "") or latest.get("othercontact_phone", "")).strip()
        has_new_number = int(new_number not in ("", "nan"))
        # Is a partial save STILL held on a device? Latest callcode only — see
        # is_active_partialsave(). This used to be an ever-flag over the whole
        # history, so it never expired.
        active_partialsave = is_active_partialsave(callcode)
        weak_lead = int(callcode == "2_OC" and not has_new_number)

        # --- action_bucket (priority order) — open cases only -----------------
        # Held-up (partial saves) first, then callbacks/drop follow-ups, then the
        # keep-calling pool (no-answer/off + still-have-numbers-to-try); anything
        # else open -> Review (which the app narrows to escalations).
        #
        # The Resume test is now purely code-based. It used to be
        # `was_partialsaved or callcode in CC_RESUME`, which dragged every pid
        # ever partial-saved into Resume regardless of what happened since.
        # CC_RESUME already holds 4_D / 4_NA / 4_OF, so every active partial save
        # lands here EXCEPT 4_SC — which keeps its Callback slot below because it
        # carries a scheduled time worth sorting on. The app then ALSO lists it
        # under Resume via the `active_partialsave` overlay, so a held 4_SC shows
        # in both queues rather than having to pick one.
        bucket = ""
        if open_case or status == "NOTIFICATION":
            if callcode in CC_RESUME:
                bucket = "Resume"
            elif callcode in CC_CALLBACK:
                bucket = "Callback"
            elif callcode in CC_KEEP_CALLING:
                bucket = "Keep calling"
            else:
                bucket = "Review"

        rows.append({
            "pid": pid,
            "enumerator": latest.get("enum_name", "") or latest.get("enumerator", ""),
            "status": status,
            "callcode": callcode,
            "attrition_reason": latest.get("attrition_reason", ""),
            "is_complete": int(ever_complete),
            # Whether a partial save is still out there. Derived from `callcode`,
            # which is right here on the case row, so it lives on CASES — and
            # deliberately is NOT merged in from the summary in the app's
            # open_with_buckets(), or it would collide into
            # active_partialsave_x/_y and vanish from every table naming it.
            "active_partialsave": int(active_partialsave),
            # NB: was_partialsaved deliberately lives on the SUMMARY only. Both
            # the action queues and the Inactive tab merge summary onto cases, so
            # carrying it here too would collide into was_partialsaved_x/_y and
            # silently disappear from every table that asks for it by name.
            # completed, then called again anyway - a coordination failure worth
            # showing supervisors rather than silently absorbing. The calls
            # themselves are NOT counted anywhere; only recorded here.
            "recontacted_after_complete": int(recontacted),
            "calls_after_complete": n_after_complete,
            "attempts_total": attempts,
            "ever_picked_up": int(ever_picked),
            "resume_rounds": resume_rounds,
            "last_contact_date": last_contact.date().isoformat() if pd.notna(last_contact) else "",
            "days_open": days_open,
            "is_correction": int(latest.get("is_supervisor_closure", 0) in (1, "1", "1.0", 1.0)),
            "action_bucket": bucket,
            "callback_due": callback_due_raw,
            "callback_by": str(latest.get("callback_by", "") or "").strip(),
            "callback_overdue": callback_overdue,
            "callback_when": callback_when,
            "contact_issue": str(latest.get("contact_issue", "") or "").strip(),
            "numbers_exhausted": numbers_exhausted,
            "has_new_number": has_new_number,
            "new_number": new_number if has_new_number else "",
            "stop_reason": str(latest.get("stop_reason", "") or "").strip(),
            "last_section_n": latest.get("last_section_n", ""),
            "rsd_reason": str(latest.get("rsd_reason", "") or "").strip(),
            "retake_mode": latest.get("retake_mode", ""),
            "weak_lead": weak_lead,
            "ineligible_type1": int(_truthy(pd.Series([latest.get("ineligible_type1", 0)])).iloc[0]),
            "ineligible_type2": int(_truthy(pd.Series([latest.get("ineligible_type2", 0)])).iloc[0]),
            "route_recruited": latest.get("route_recruited", ""),
            "treatment": latest.get("treatment", ""),
        })

    cases = pd.DataFrame(rows)
    return cases


def is_attempt_row(df: pd.DataFrame) -> pd.Series:
    """Which submissions count as a CALL ATTEMPT. ONE definition, used by
    rollup(), summarize() and effort_by_enumerator() so their totals reconcile.

    Everything except a supervisor correction. `held_partial` marks a
    held-partial log; those rows carry 4_* callcodes (drop / reschedule
    notifications), and a 4_* IS a real call outcome — someone dialled and the
    call dropped — so they ARE counted. Only a held-partial log that is not a
    4_* stays out.

    NB: is_partialsaved is deliberately not consulted — it fires on any deep
    survey (including completions), so it would drop real attempts."""
    cc = _s(df, "callcode").astype(str).str.strip().str.upper()
    correction = _truthy(_s(df, "is_supervisor_closure"))
    held_non4 = _truthy(_s(df, "held_partial")) & ~cc.str.startswith("4_")
    return ~(correction | held_non4)


def drop_after_complete(df: pd.DataFrame, order_cols, complete_mask=None):
    """(rows up to and including each pid's completing call, n dropped per pid).

    Completion is TERMINAL: once a pid has a callcode 1 submission, anything
    logged after it is a stray re-contact and must not be counted as an
    observation — not as an attempt, a shift, or enumerator credit. `order_cols`
    is how submissions are put in time order within a pid."""
    if "pid" not in df.columns or not len(df):
        return df, pd.Series(dtype=int)
    if complete_mask is None:
        complete_mask = _s(df, "callcode").astype(str).str.strip() == "1"
    d = df.assign(_cmp=complete_mask.astype(int)).sort_values(
        ["pid"] + list(order_cols), na_position="first")
    post = (d.groupby("pid")["_cmp"].cumsum() - d["_cmp"]) > 0
    return d.loc[~post].drop(columns=["_cmp"]), post.groupby(d["pid"]).sum().astype(int)


def effort_by_enumerator(attempts: pd.DataFrame, pids=None) -> pd.DataFrame:
    """Historic effort credited to the enumerator who ACTUALLY made each call.

    Case-level columns carry the LATEST enumerator only, so a handed-over pid
    takes its whole history with it. This counts submissions instead, which is
    what you want for "how much has this person dialed": pids deliberately
    OVERLAP across enumerators, because several people try the same respondent.
    ATTEMPT is defined exactly as in summarize() so the totals reconcile."""
    cols = ["enumerator", "attempts", "pids_tried", "partial_saves", "completed"]
    df = _norm_cols(attempts)
    if "pid" not in df.columns or not len(df):
        return pd.DataFrame(columns=cols)
    df = df.copy()
    df["_enum"] = _s(df, "enum_name").astype(str).str.strip()
    df["_pid"] = df["pid"].astype(str).str.strip()
    if pids is not None:
        df = df[df["_pid"].isin(set(pids))]
    # a call placed after the pid had already completed is not credited effort
    _order = [c for c in ("endtime", "starttime", "SubmissionDate") if c in df.columns]
    if _order:
        df, _ = drop_after_complete(df, _order)
    att = df[is_attempt_row(df) & (df["_enum"] != "")]
    if not len(att):
        return pd.DataFrame(columns=cols)
    att = att.assign(_ps=_truthy(_s(att, "was_partialsaved")).astype(int))
    out = (att.groupby("_enum")
              .agg(attempts=("_pid", "size"), pids_tried=("_pid", "nunique"),
                   partial_saves=("_ps", "sum"))
              .reset_index())
    done = att[_s(att, "callcode").astype(str).str.strip() == "1"]
    done_n = done.groupby("_enum")["_pid"].nunique() if len(done) else pd.Series(dtype=int)
    out["completed"] = out["_enum"].map(done_n).fillna(0).astype(int)
    return out.rename(columns={"_enum": "enumerator"})[cols]


def attrition_summary(cases: pd.DataFrame) -> dict:
    """Two attrition definitions (see monitoring_flags.md §2)."""
    final = cases
    n = len(final)
    completed = int((final["callcode"] == "1").sum())
    inactive = final["status"] == "INACTIVE"
    not_complete = final["callcode"] != "1"

    def _rate(inelig_col):
        base = int((~_bool(final[inelig_col])).sum())          # recruited − ineligible
        attrited = int((inactive & not_complete & ~_bool(final[inelig_col])).sum())
        rate = round(100 * attrited / base) if base else 0
        return {"base": base, "attrited": attrited, "rate": rate}

    return {
        "cases": n,
        "completed": completed,
        "completion_rate": round(100 * completed / n) if n else 0,
        "def_a": _rate("ineligible_type1"),   # broad attrition
        "def_b": _rate("ineligible_type2"),   # conservative "true" attrition
    }


def _bool(series):
    return series.astype(str).str.strip().str.lower().isin(["1", "1.0", "true", "yes", "y"])


# =============================================================================
# FULL-SAMPLE RESPONDENT SUMMARY  (one row per pid over the ENTIRE sample)
# -----------------------------------------------------------------------------
# rollup() above only covers pids that produced >=1 attempt. The summary sheet
# starts from the SAMPLE tab (the full recruited roster) and left-joins the
# attempt rollup, so never-called respondents still appear. Each row =
#   identity/covariates (from the sample tab, passed through untouched)
#   + CURRENT STATE  (from the respondent's most recent attempt)
#   + HISTORY AGGREGATE (across ALL of the respondent's attempts).
# =============================================================================

# Covariate columns we can reconstruct a frame from when no sample tab is set.
FRAME_PASSTHRU = [
    "resp_name", "first_name", "firstname_inperson", "surname_inperson",
    "phone_1_prefill", "phone_2_prefill", "route_recruited", "date_recruited",
    "gender_pre", "age_pre", "educ_pre", "seat", "treatment", "batch_code",
    "recruited_before",
]


def frame_from_attempts(attempts: pd.DataFrame) -> pd.DataFrame:
    """Fallback 'sample' frame: distinct pids + their recruitment covariates,
    taken from the latest attempt. Lets the summary run before a sample tab is
    wired up — but it CANNOT contain never-called pids (they have no attempt)."""
    df = _norm_cols(attempts)
    if "pid" not in df.columns:
        return pd.DataFrame(columns=["pid"])
    df = df.copy()
    df["_ord"] = _to_dt(_s(df, "endtime")).fillna(_to_dt(_s(df, "starttime")))
    df = df.sort_values("_ord")
    cols = ["pid"] + [c for c in FRAME_PASSTHRU if c in df.columns]
    return df[cols].groupby("pid", as_index=False).last()


# --- submission classification vocab ----------------------------------------
RESCHEDULE_CODES = {"2_SC", "3_SC", "4_SC"}   # incl. 4_SC reschedule notification
DROP_CODES = {"2_D", "3_D", "4_D"}            # incl. 4_D held-partial notification
# 5 NON-overlapping shift windows, minutes since midnight, [lo, hi). Timed off
# starttime. Attempts outside 07:30–20:30 fall into off_hours; the 6 buckets sum
# to total_attempts.
SHIFTS = {
    "0730_0930": (450, 570),
    "0930_1230": (570, 750),
    "1230_1530": (750, 930),
    "1530_1730": (930, 1050),
    "1730_2030": (1050, 1230),
}


def _fmt_dt(x):
    return x.strftime("%Y-%m-%d %H:%M") if pd.notna(x) else ""


def _last_nonempty(series):
    for v in reversed(series.tolist()):
        s = str(v).strip()
        if s and s.lower() != "nan":
            return s
    return ""


def _lv(row, name):
    """A latest-row string value, trimmed."""
    return str(row.get(name, "") or "").strip()


def _minutes_of_day(dt_series):
    return dt_series.dt.hour * 60 + dt_series.dt.minute


def summarize(sample: pd.DataFrame, attempts: pd.DataFrame, now=None) -> pd.DataFrame:
    """Full tracking sheet: one row per respondent over the ENTIRE sample.
    Latest-submission state + history aggregated across all submissions.
    Columns follow context/tracking_sheet_codebook.md (FINAL 2026-08-02)."""
    frame = _norm_cols(sample).copy()
    if "pid" not in frame.columns:
        raise ValueError("the sample sheet must have a 'pid' column")
    frame["pid"] = frame["pid"].astype(str).str.strip()
    frame = frame.loc[~frame["pid"].duplicated(keep="first")].reset_index(drop=True)

    df = _norm_cols(attempts).copy()
    if "pid" in df.columns and len(df):
        df["pid"] = df["pid"].astype(str).str.strip()
        # recency keys: endtime, tie-break SubmissionDate, then KEY
        df["_end"] = _to_dt(_s(df, "endtime"))
        df["_start"] = _to_dt(_s(df, "starttime"))
        df["_subdate"] = _to_dt(_s(df, "SubmissionDate"))
        df["_key"] = _s(df, "KEY").astype(str)
        df["_ord"] = df["_end"].fillna(df["_start"])
        df["_mod"] = _minutes_of_day(df["_start"].fillna(df["_end"]))
        # ATTEMPT = not a supervisor correction and not a 4_D held-partial log.
        # NB: is_partialsaved is deliberately NOT excluded — it fires on any deep
        # survey (incl. completions), so it would drop real attempts.
        df["_correction"] = _truthy(_s(df, "is_supervisor_closure"))
        df["_is_attempt"] = is_attempt_row(df)
        df["_answered"] = _truthy(_s(df, "is_answered")) | _truthy(_s(df, "pickup"))
        df["_incorrect"] = _truthy(_s(df, "is_incorrect"))
        df["_correct"] = _truthy(_s(df, "is_correct"))
        df["_dk"] = _truthy(_s(df, "is_dk_contact"))
        df["_oc"] = _truthy(_s(df, "is_othercontact"))
        df["_complete"] = _s(df, "callcode").astype(str).str.strip() == "1"
        df["_cc"] = _s(df, "callcode").astype(str).str.strip()
        # callcode suffix (the part after the first "_"): 2_SC -> SC, 0_R -> R, 1 -> ""
        df["_suffix"] = df["_cc"].apply(lambda cc: cc.split("_", 1)[1] if "_" in cc else "")
        df["_stop"] = _s(df, "stop_reason").astype(str).str.strip().str.lower()
        df["_resch"] = _s(df, "resch").astype(str).str.strip()
        df["_resched"] = (df["_cc"].isin(RESCHEDULE_CODES) | (df["_resch"] == "1")
                          | (df["_stop"] == "reschedule"))
        df["_drop"] = (df["_cc"].isin(DROP_CODES) | (df["_resch"] == "2")
                       | (df["_stop"] == "drop"))
        df["_dur"] = pd.to_numeric(_s(df, "duration"), errors="coerce").fillna(0)
        df["_nwrong"] = pd.to_numeric(_s(df, "numbers_wrong"), errors="coerce").fillna(0)
        df["_ntried"] = pd.to_numeric(_s(df, "numbers_tried"), errors="coerce").fillna(0)
        df["_navail"] = pd.to_numeric(_s(df, "number_phonenumbers"), errors="coerce").fillna(0)
        df["_elig1"] = _truthy(_s(df, "ineligible_type1"))
        df["_elig2"] = _truthy(_s(df, "ineligible_type2"))
        for key, (lo, hi) in SHIFTS.items():
            df[f"_sh_{key}"] = df["_is_attempt"] & df["_mod"].between(lo, hi, inclusive="left")
        in_shift = df["_mod"].between(450, 1230, inclusive="left")
        df["_sh_off"] = df["_is_attempt"] & ~in_shift.fillna(False)

        # Cut every pid at its completing submission — same terminal rule as
        # rollup(). Anything logged after callcode 1 is a stray re-contact, so it
        # must not reach total_attempts, the times_* counters, the shift buckets
        # or the per-enumerator effort table. Counted once here, then dropped.
        df, _n_after = drop_after_complete(df, ["_end", "_subdate", "_key"],
                                           complete_mask=df["_complete"])

        rows = []
        for pid, g in df.groupby("pid", sort=False):
            g = g.sort_values(["_end", "_subdate", "_key"], na_position="first")
            latest = g.iloc[-1]
            att = g[g["_is_attempt"]]
            first_att = att["_start"].min() if len(att) else g["_ord"].min()
            last_comment = _lv(latest, "comment_sp") if bool(latest.get("_correction")) \
                else _lv(latest, "final_comment")

            # ---- active partial save (see is_active_partialsave) -------------
            # The notification path that emits 4_* records the device in
            # rs_tab_id and the progress in rs_section, leaving tab_id and
            # last_section blank on that row. So when the pid is STILL holding a
            # partial, read both off that latest submission. Scanning the whole
            # history instead (as this used to) returns a tab_id from some older
            # submission — a device that is not the one holding it now.
            active_ps = is_active_partialsave(latest.get("_cc", ""))
            _hist_tab = (_last_nonempty(g.get("tab_id", pd.Series([], dtype=str)))
                         or _last_nonempty(g.get("rs_tab_id", pd.Series([], dtype=str))))
            _hist_section = _last_nonempty(g.get("last_section", pd.Series([], dtype=str)))
            if active_ps:
                tab_id = _lv(latest, "rs_tab_id") or _lv(latest, "tab_id") or _hist_tab
                section = (PARTIALSAVE_SECTIONS.get(_lv(latest, "rs_section"), "")
                           or _lv(latest, "last_section") or _hist_section)
            else:
                tab_id, section = _hist_tab, _hist_section

            rows.append({
                "pid": pid,
                # ---- current state (latest submission) -----------------------
                "enumerator": _lv(latest, "enum_name"),
                "current_status": status_for(latest.get("_cc", ""), latest.get("status", "")),
                "last_submission_time": _fmt_dt(latest.get("_ord")),
                "current_callcode": latest.get("_cc", ""),
                "last_comment": last_comment,
                "rsd_reason": _lv(latest, "rsd_reason"),
                "is_supervisor": int(bool(latest.get("_correction"))),
                "sup_attempt": _lv(latest, "cc_attempt_sp"),
                "refuse_why": _last_nonempty(g.get("refuse_why", pd.Series([], dtype=str))),
                "call_length_min": round(float(latest.get("_dur", 0)) / 60, 1),
                "is_complete": int(bool(g["_complete"].any())),          # absorbing
                # ever-flag, like the other history columns: 1 if ANY submission
                # for this pid was partially saved. Blank values read as 0, so an
                # export where the field is not yet populated shows 0, not noise.
                "was_partialsaved": int(bool(_truthy(
                    g.get("was_partialsaved", pd.Series("", index=g.index))).any())),
                # HISTORY vs NOW, kept apart deliberately:
                #   was_partialsaved   - was this pid EVER partial-saved?
                #   active_partialsave - is a partial save still held RIGHT NOW?
                # Only the second one may drive the Partial saves tab or the
                # Resume queue; the first never expires, so a completed pid stays
                # flagged forever. See is_active_partialsave().
                "active_partialsave": int(active_ps),
                "calls_after_complete": int(_n_after.get(pid, 0)),
                "recontacted_after_complete": int(_n_after.get(pid, 0) > 0),
                "last_is_answered": int(bool(latest.get("_answered", False))),
                "last_is_incorrect": int(bool(latest.get("_incorrect", False))),
                "last_section_n": _lv(latest, "last_section_n"),
                # the readable label ("Section D. Demographics"), not the number.
                # For a pid still holding a partial this is rs_section off the
                # latest 4_* row, translated; otherwise the last non-empty
                # last_section. Resolved above.
                "last_section": section,
                # tab id lives in one of two fields depending on the path taken —
                # rs_tab_id on the 4_* notification path, tab_id on the reschedule
                # path. Resolved above, latest-4_* first.
                "tab_id": tab_id,
                "callback_due": _lv(latest, "callback_due"),
                "callback_by": _lv(latest, "callback_by"),
                "retake_mode": _lv(latest, "retake_mode"),
                # ---- history (aggregated across submissions) -----------------
                "d02_check": _last_nonempty(g.get("d02_check", pd.Series([], dtype=str))),
                "d04_yn": _last_nonempty(g.get("d04_yn", pd.Series([], dtype=str))),
                # why a 0_UN closure fired: the form sets this to 1 when the
                # respondent owns no phone of their own (a12=0 and a13=0).
                "no_phone_ineligible": _last_nonempty(
                    g.get("no_phone_ineligible", pd.Series([], dtype=str))),
                "total_attempts": int(len(att)),
                "times_pickedup": int(g["_answered"].sum()),
                "attempts_0730_0930": int(g["_sh_0730_0930"].sum()),
                "attempts_0930_1230": int(g["_sh_0930_1230"].sum()),
                "attempts_1230_1530": int(g["_sh_1230_1530"].sum()),
                "attempts_1530_1730": int(g["_sh_1530_1730"].sum()),
                "attempts_1730_2030": int(g["_sh_1730_2030"].sum()),
                "attempts_off_hours": int(g["_sh_off"].sum()),
                "first_attempt_time": _fmt_dt(first_att),
                "times_rescheduled": int((g["_suffix"] == "SC").sum()),
                "times_dropped": int((g["_suffix"] == "D").sum()),
                "times_refused": int((g["_suffix"] == "R").sum()),
                "times_wrongnumber": int((g["_suffix"] == "WN").sum()),
                "times_noanswer": int((g["_suffix"] == "NA").sum()),
                "times_off": int((g["_suffix"] == "OF").sum()),
                "times_incorrect": int((g["_suffix"] == "IN").sum()),
                "times_ineligible": int((g["_suffix"] == "UN").sum()),
                "numbers_wrong": int(g["_nwrong"].max()),
                "times_dk_contact": int(g["_dk"].sum()),
                "times_othercontact": int(g["_oc"].sum()),
                "numbers_tried": int(g["_ntried"].max()),
                "numbers_available": int(g["_navail"].max()),
                # ---- eligibility building blocks (any submission) ------------
                "_elig1": int(g["_elig1"].any()),
                "_elig2": int(g["_elig2"].any()),
                # explicit "this pid produced a case row" marker. ever_attempted used
                # to test current_status for emptiness, which silently inverts if
                # the left-join's fillna("") is skipped - an all-NaN column comes
                # back float64, not object, so every never-called pid then reads
                # as the string "nan" and counts as CALLED.
                "_has_case": 1,
            })
        cases = pd.DataFrame(rows)
    else:
        cases = pd.DataFrame(columns=["pid"])

    # append computed columns absent from the frame (frame keeps recruitment truth)
    add = ["pid"] + [c for c in cases.columns if c != "pid" and c not in frame.columns]
    out = frame.merge(cases[add], on="pid", how="left")

    # never-called pids get NaN from the left-join; blank the string columns so
    # they don't read as the literal "nan" (ints are zero-filled below).
    for c in [c for c in add if c != "pid"]:
        if out[c].dtype == object:
            out[c] = out[c].fillna("")
    if "current_status" not in out.columns:
        out["current_status"] = ""

    # ---- ever called / never-called defaults ---------------------------------
    # EVER ATTEMPTED = the pid was attempted at least once. Not "has a submission":
    # a row that is only a supervisor correction is paperwork, not a call, and
    # must not make a respondent look worked. Reads the attempt count, which is
    # is_attempt_row() — so a 4_* outcome counts, per the field rule.
    #
    # It must never key off a string being non-empty. It used to test
    # `current_status != ""`, which silently inverts: never-called pids get NaN
    # from the left-join, the fillna("") above only runs on object columns, and a
    # column that arrives ENTIRELY NaN is float64 — so astype(str) produced the
    # literal "nan", every never-called pid counted as called, and the
    # "to be assigned" queue emptied to 0.
    _has_case = (pd.to_numeric(_s(out, "_has_case"), errors="coerce").fillna(0) > 0
                 if "_has_case" in out.columns else pd.Series(False, index=out.index))
    out["ever_attempted"] = (
        pd.to_numeric(_s(out, "total_attempts"), errors="coerce").fillna(0) >= 1
    ).astype(int)
    # A pid with NO submission at all carries no current state, so nothing
    # downstream can read a stray "nan" as a real status. Keyed on _has_case, not
    # on ever_attempted: a correction-only pid keeps its status, but was not tried.
    for _c in ("current_status", "current_callcode", "enumerator", "last_submission_time"):
        if _c in out.columns:
            out.loc[~_has_case, _c] = ""
    # Two DIFFERENT questions, kept apart deliberately:
    #   ever_attempted - was anyone dialled?      (attempts >= 1)
    #   _has_case      - is there ANY submission? (incl. a supervisor correction)
    # A supervisor closure with no call is NOT an attempt, but it IS resolved and
    # it DOES carry survey data - so case_state and the ineligibility columns key
    # off _has_case, never off ever_attempted.
    out["_has_case"] = _has_case.astype(int)
    int_cols = ["is_complete", "last_is_answered", "last_is_incorrect", "is_supervisor",
                "total_attempts",
                "times_pickedup", "attempts_0730_0930", "attempts_0930_1230",
                "attempts_1230_1530", "attempts_1530_1730", "attempts_1730_2030",
                "attempts_off_hours", "times_rescheduled", "times_dropped", "times_refused",
                "times_wrongnumber", "times_noanswer", "times_off", "times_ineligible",
                "numbers_wrong", "times_incorrect", "times_dk_contact", "times_othercontact",
                "numbers_tried", "numbers_available", "_elig1", "_elig2",
                # never-called pids have no attempt row, so these arrive NaN
                "calls_after_complete", "recontacted_after_complete",
                "was_partialsaved", "active_partialsave", "ever_attempted"]
    for c in int_cols:
        out[c] = pd.to_numeric(_s(out, c), errors="coerce").fillna(0).astype(int)
    out["numbers_tried_of_available"] = (out["numbers_tried"].astype(str) + "/"
                                         + out["numbers_available"].astype(str))

    # shift coverage across the 5 windows (off_hours excluded): have we tried this
    # respondent at different times of day, or always the same slot?
    _shift_cols = ["attempts_0730_0930", "attempts_0930_1230", "attempts_1230_1530",
                   "attempts_1530_1730", "attempts_1730_2030"]
    out["shifts_covered_n"] = (out[_shift_cols] >= 1).sum(axis=1).astype(int)
    out["all_shifts_covered"] = (out["shifts_covered_n"] >= 5).astype(int)
    # which time windows are STILL to be tried, as letters (legend in the app):
    #   A 07:30-09:30 · B 09:30-12:30 · C 12:30-15:30 · D 15:30-17:30 · E 17:30-20:30
    _shift_letters = ["A", "B", "C", "D", "E"]

    def _to_try(r):
        n_tried = sum(1 for col in _shift_cols if r[col] > 0)
        if n_tried == 0:
            return "None tried"
        if n_tried == len(_shift_cols):
            return "All tried"
        return ", ".join(ltr for col, ltr in zip(_shift_cols, _shift_letters) if r[col] == 0)

    out["shifts_to_try"] = out.apply(_to_try, axis=1)

    # ever-flags (handy on the callback queue)
    out["ever_picked_up"] = (out["times_pickedup"] > 0).astype(int)
    out["ever_rescheduled"] = (out["times_rescheduled"] > 0).astype(int)
    out["ever_dropped"] = (out["times_dropped"] > 0).astype(int)
    out["is_incorrect"] = (out["times_incorrect"] > 0).astype(int)
    out["is_othercontact"] = (out["times_othercontact"] > 0).astype(int)

    # ---- eligibility: never-called keys off "no phone at recruitment" --------
    # Prefer an explicit no_phone flag; else the recruitment phone count / own-number
    # determinant = 0 (blanks are NOT 0); else fall back to empty prefill columns.
    if "no_phone" in out.columns:
        no_phone = _truthy(_s(out, "no_phone"))
    elif (_first_col(out, PHONE_COUNT_COLS) or "own_phone" in out.columns
          or "ineligible_no_owned_phone" in out.columns):
        prov = pd.to_numeric(
            _s(out, _first_col(out, PHONE_COUNT_COLS) or PHONE_COUNT_COLS[-1]),
            errors="coerce")
        no_phone = (prov == 0) | _no_own_number(out, prov)
    else:
        p1 = _s(out, "phone_1_prefill") if "phone_1_prefill" in out.columns else _s(out, "phone_1")
        p2 = _s(out, "phone_2_prefill") if "phone_2_prefill" in out.columns else _s(out, "phone_2")
        no_phone = (p1.astype(str).str.strip() == "") & (p2.astype(str).str.strip() == "")
    _seen = out["_has_case"] == 1
    out["ineligible_type1"] = out["_elig1"].where(_seen, no_phone.astype(int))
    out["ineligible_type2"] = out["_elig2"].where(_seen, no_phone.astype(int))
    out = out.drop(columns=["_elig1", "_elig2"])

    # eligible_to_call = the pool that CAN be called = Stage-I recruitment
    # eligibility: signed up, has a number of their own, and cleared every
    # ineligibility determinant on the sheet. (Excludes refusals and missing.) Stage II is NOT used here: it keys off call outcomes, so it would
    # remove pids the callers still have to work.
    if _first_col(out, SIGNUP_STATUS_COLS):
        out["eligible_to_call"] = recruit_eligible(out, stage=1).astype(int)
    else:
        out["eligible_to_call"] = (~no_phone).astype(int)

    # ---- open / attrition (DEFAULT rule; see note in the app) ----------------
    su_up = _s(out, "current_status").astype(str).str.upper()
    # Post-completion rows are already dropped above, so a completed pid's latest
    # row IS its completing row and status_for("1") already yields INACTIVE. Kept
    # as an explicit guarantee that completion closes the case.
    out.loc[out["is_complete"] == 1, "current_status"] = "INACTIVE"
    su_up = _s(out, "current_status").astype(str).str.upper()
    out["is_open"] = su_up.isin([s.upper() for s in OPEN_STATUSES]).astype(int)
    # plain-language state next to status: Action (open) / Archived (closed) / Not assigned
    out["case_state"] = "Action"
    out.loc[su_up == "INACTIVE", "case_state"] = "Archived"
    # "Not assigned" = nothing has been logged at all. A pid closed by a
    # supervisor without a call is Archived, not waiting to be handed out.
    out.loc[out["_has_case"] == 0, "case_state"] = "Not assigned"
    # attrited = an eligible pid CLOSED (INACTIVE) without completing.
    # Never-called and in-progress (open) pids are NOT attrition — they're unresolved.
    resolved = (su_up == "INACTIVE") & (out["is_complete"] == 0)
    out["attrited_a"] = (resolved & (out["ineligible_type1"] == 0)).astype(int)
    out["attrited_b"] = (resolved & (out["ineligible_type2"] == 0)).astype(int)

    return out.drop(columns=["_has_case"])


def summary_kpis(summary: pd.DataFrame) -> dict:
    """Headline numbers computed over the FULL sample (not just called cases)."""
    n = len(summary)
    attempted = int(summary["ever_attempted"].sum())
    completed = int((summary["is_complete"] == 1).sum())
    base_a = int((summary["ineligible_type1"] == 0).sum())
    base_b = int((summary["ineligible_type2"] == 0).sum())
    att_a, att_b = int(summary["attrited_a"].sum()), int(summary["attrited_b"].sum())
    r = lambda x, b: round(100 * x / b) if b else 0
    return {
        "respondents": n, "ever_attempted": attempted, "never_attempted": n - attempted,
        "completed": completed, "completion_rate": r(completed, n),
        "def_a": {"base": base_a, "attrited": att_a, "rate": r(att_a, base_a)},
        "def_b": {"base": base_b, "attrited": att_b, "rate": r(att_b, base_b)},
    }


if __name__ == "__main__":
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else "../sample_database.csv"
    subs = pd.read_csv(src, dtype=str, keep_default_na=False)
    cases = rollup(subs)
    print(f"submissions: {len(subs)}  ->  cases: {len(cases)}")
    print("\nstatus:\n", cases["status"].value_counts().to_string())
    print("\naction_bucket (open cases):\n",
          cases[cases["action_bucket"] != ""]["action_bucket"].value_counts().to_string())
    print("\nattrition:\n", attrition_summary(cases))
