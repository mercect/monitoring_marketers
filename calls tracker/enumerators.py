# enumerators.py — one name per enumerator, whatever they typed in the form.
# -----------------------------------------------------------------------------
# `enum_name` is free text on the SurveyCTO form, so the same person appears
# under several spellings ("Issa Adams", "ISSA ADAMS", "Issa Adams Kamara",
# "Issa Adsms") and is counted as several people. The `enumerators` tab of the
# Google Sheet lists each enumerator once, with the other spellings they use:
#
#     name                  | also_typed_as
#     Issa Adams Kamara     | Issa Adams; Issa Adsms
#
# Every submission's enum_name is replaced by the official `name` before the
# rollup, so every tab counts each person once. Capitals, extra spaces, dots
# and hyphens never need listing: "ISSA ADAMS" matches "Issa Adams" anyway.
# A name that matches nobody on the list is kept as typed, so no call is lost.
#
# The list lives in the Sheet, not in this repository, so staff names stay out
# of git and can be edited without a code change. It is read through the same
# service account as the decisions log (review.py), or, failing that, through
# an `enumerators_csv_url` link in Secrets.
# -----------------------------------------------------------------------------
import re

import pandas as pd
import streamlit as st

TAB = "enumerators"


def norm(name):
    """A spelling-insensitive key: lower case, dots / hyphens / underscores
    read as spaces, runs of spaces collapsed."""
    return " ".join(re.sub(r"[.\-_]", " ", str(name).lower()).split())


def alias_map(roster):
    """{normalised spelling: official name} from the enumerators tab. The
    official name maps to itself, so a capitalisation slip still matches."""
    out = {}
    if roster is None or roster.empty or "name" not in roster.columns:
        return out
    also = roster["also_typed_as"] if "also_typed_as" in roster.columns \
        else pd.Series("", index=roster.index)
    for name, extra in zip(roster["name"], also):
        official = str(name).strip()
        if not official:
            continue
        for spelling in [official] + re.split(r"[;\n]", str(extra or "")):
            if spelling.strip():
                out.setdefault(norm(spelling), official)
    return out


def canonical(names, amap):
    """Each name replaced by its official spelling; unknown names unchanged."""
    s = pd.Series(names).fillna("").astype(str).str.strip()
    if not amap:
        return s
    return s.map(lambda x: amap.get(norm(x), x) if x else x)


def apply_names(subs, amap):
    """subs with enum_name replaced by the official names."""
    if not amap or subs.empty or "enum_name" not in subs.columns:
        return subs
    out = subs.copy()
    out["enum_name"] = canonical(out["enum_name"], amap).values
    return out


def unknown_names(subs, amap, ignore=()):
    """{name as typed: submissions} for names that match nobody on the list.
    Supervisor rows are paperwork, so their names are left out."""
    if not amap or subs.empty or "enum_name" not in subs.columns:
        return {}
    d = subs
    for c in ("is_supervisor_closure", "sup_correction"):
        if c in d.columns:
            d = d[~d[c].astype(str).str.strip().str.lower().isin(["1", "1.0", "true", "yes"])]
    official = set(amap.values())
    typed = d["enum_name"].fillna("").astype(str).str.strip()
    typed = typed[(typed != "") & ~typed.isin(official) & ~typed.isin(set(ignore))]
    return typed.value_counts().to_dict()


@st.cache_data(ttl=300)
def load_enumerators():
    """(roster, meta). Never raises: without the tab or the service account the
    dashboard carries on with the names exactly as typed."""
    cols = ["name", "also_typed_as"]
    values, msg = None, ""
    try:
        import review
        if not review.writer_problem():
            values = review.read_tab_values(TAB)
            if values is None:
                msg = f"no `{TAB}` tab in the Sheet yet."
        else:
            url = str(review._secret("enumerators_csv_url", "")).strip()
            if url:
                from rollup import sheet_csv
                df = pd.read_csv(sheet_csv(url), dtype=str, keep_default_na=False)
                values = [list(df.columns)] + df.values.tolist()
            else:
                msg = "not read: no service account (and no enumerators_csv_url)."
    except Exception as e:
        return pd.DataFrame(columns=cols), {"ok": False, "rows": 0,
                                            "msg": f"could not read the `{TAB}` tab: {e}"}
    if not values:
        return pd.DataFrame(columns=cols), {"ok": not msg.startswith("could"), "rows": 0,
                                            "msg": msg}
    head = [str(h).strip().lower() for h in values[0]]
    df = pd.DataFrame(values[1:], columns=head) if len(values) > 1 else pd.DataFrame(columns=head)
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    df = df[cols].astype(str).apply(lambda s: s.str.strip())
    df = df[df["name"] != ""].reset_index(drop=True)
    return df, {"ok": True, "rows": len(df), "msg": ""}
