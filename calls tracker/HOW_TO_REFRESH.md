# How to make sure changes show up

Two kinds of change. They need different things.

| What changed | What to do |
|---|---|
| **The Google Sheet** (new calls, new data entry rows) | Click **🔄 Refresh now** in the dashboard |
| **The code** (anything Claude edits) | **Fully close and reopen** the dashboard window |

The Refresh button re-reads the **sheet**. It cannot load new **code**.

---

## After a code change — 3 steps

**1. Close it properly.**
Close the black `START_DASHBOARD.bat` terminal window — not just the browser tab.
Closing the browser leaves the dashboard running in the background.

**2. Reopen it.**
Double-click `START_DASHBOARD.bat`.

**3. Check the stamp.**
Under the title, the caption shows:

```
Attempts: ...  ·  Sample: ...  ·  rollup logic 16:01:51  ·  auto-refresh every 5 min
```

- `rollup logic` = when the calculation code was last edited.
- Matches (or is later than) when Claude said they finished? You are on the new code.
- Older, or the line is missing entirely? The restart did not take — close and reopen again.

If a window will not close, force it:

```
taskkill /F /IM python.exe
```

Then reopen `START_DASHBOARD.bat`.

---

## Before trusting new numbers

Double-click **`CHECK.bat`**. About 10 seconds. It prints either:

- `ALL CHECKS PASSED` — the numbers hold together, or
- a list of exactly what is wrong.

Worth running after any change that touches how numbers are calculated.

---

## Why this is needed at all

Streamlit reloads `app.py` by itself when it changes — that is the labels, the
layout, the text on screen.

It does **not** reload `rollup.py`, which holds every calculation. Python loads
that once when the dashboard starts and keeps it in memory until the process
stops.

So without a full restart you can end up looking at **new labels with old
numbers** — the page looks updated while the maths is stale. That is what the
`rollup logic` stamp is there to catch.

---

## Quick reference

| Symptom | Cause | Fix |
|---|---|---|
| Numbers look stale | sheet cached (5 min) | 🔄 Refresh now |
| Labels changed but numbers did not | old `rollup.py` in memory | full restart |
| `rollup logic` time is old | restart did not take | close window again, or `taskkill` |
| No caption line under the title | running old code entirely | full restart |
| Not sure the numbers are sound | — | run `CHECK.bat` |
