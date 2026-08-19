# auth.py — shared password gate for BOTH dashboards.
# -----------------------------------------------------------------------------
# You do NOT need to know Python to use this. In one sentence: if a password is
# set, visitors must type it before they see any data.
#
# How it decides:
#   - Reads  app_password  from the secrets (locally: .streamlit/secrets.toml;
#     on Streamlit Community Cloud: the app's Settings -> Secrets box).
#   - Password set      -> visitors get a password box and nothing else until
#                          they get it right.
#   - Password NOT set  -> the dashboard opens to anyone, and a yellow warning
#                          says so. That is on purpose: it keeps running on your
#                          laptop with no setup, but makes an unprotected
#                          deployment impossible to miss.
#
# To turn the gate on, add ONE line to your secrets:
#     app_password = "choose-something-long"
#
# What this is and isn't:
#   It is one shared password for everyone with the link — enough to keep a
#   public URL from being wide open and search-indexed. It is NOT per-person
#   logins and it does not track who looked at what. If you need that, use
#   Streamlit's "Only specific people can view this app" setting instead.
# -----------------------------------------------------------------------------
import hmac

import streamlit as st

SECRET_KEY = "app_password"      # the name of the secret to look for
STATE_KEY = "_password_ok"       # remembers "already signed in" for this session


def _expected_password():
    """The password from secrets, or "" when none is set / no secrets file."""
    try:
        return str(st.secrets.get(SECRET_KEY, "")).strip()
    except Exception:
        return ""


def require_password(title="🔒 Protected dashboard"):
    """Show a password box and halt the page until the right password is typed.

    Call this ONCE, immediately after st.set_page_config(), before anything else
    is drawn. Returns normally when the visitor is allowed through."""
    expected = _expected_password()

    # No password configured -> stay open, but say so loudly.
    if not expected:
        st.warning(
            "**This dashboard is not password-protected.** Anyone with the link "
            "can read it. To protect it, add a line like "
            "`app_password = \"choose-something-long\"` to your secrets "
            "(locally `.streamlit/secrets.toml`; on Streamlit Community Cloud, "
            "the app's Settings → Secrets).",
            icon="⚠️",
        )
        return

    # Already signed in during this browser session -> straight through.
    if st.session_state.get(STATE_KEY):
        return

    st.title(title)
    st.caption("This dashboard contains respondent data. Ask the survey team for "
               "the password.")

    with st.form("password_gate"):
        typed = st.text_input("Password", type="password",
                              placeholder="Paste or type the password")
        submitted = st.form_submit_button("Enter")

    if submitted:
        # compare_digest: constant-time, so the check can't be timed to guess
        # the password one character at a time.
        if hmac.compare_digest(typed.strip(), expected):
            st.session_state[STATE_KEY] = True
            st.rerun()          # redraw the page, this time with the dashboard
        else:
            st.error("Wrong password. Try again.")

    st.stop()                   # nothing below this line renders until signed in
