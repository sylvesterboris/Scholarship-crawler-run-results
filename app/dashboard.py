"""Scholarship Intelligence — dashboard (Streamlit).

    streamlit run app/dashboard.py
"""
from __future__ import annotations

import html
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from crawler.textutil import fmt_inr  # noqa: E402

DB_PATH = Path(os.environ.get("SCHOLARSHIP_DB", ROOT / "data" / "scholarships.db"))

st.set_page_config(page_title="Scholarship Intelligence", page_icon="🎓", layout="wide")
st.markdown("""
<style>
.block-container{padding-top:1.6rem;max-width:1400px}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:.78rem;font-weight:600;margin-right:6px}
.b-VERIFIED{background:#dcfce7;color:#166534}.b-REVIEW_REQUIRED{background:#fef3c7;color:#92400e}
.b-ACTIVE{background:#dbeafe;color:#1e40af}.b-EXPIRING_SOON{background:#ffedd5;color:#9a3412}
.b-EXPIRED{background:#e5e7eb;color:#374151}.b-NO_LONGER_VERIFIABLE{background:#fee2e2;color:#991b1b}
.quote{border-left:3px solid #94a3b8;padding:4px 10px;margin:2px 0 8px;color:#334155;font-size:.88rem;background:#f8fafc;white-space:pre-wrap}
mark{background:#fde68a}
</style>""", unsafe_allow_html=True)


@st.cache_resource
def conn():
    c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def q(sql, args=()):
    return pd.read_sql_query(sql, conn(), params=args)


def badge(s):
    return f'<span class="badge b-{s}">{s.replace("_", " ")}</span>' if s else ""


if not DB_PATH.exists():
    st.error(f"No database at {DB_PATH}. Run `python run_crawler.py` first.")
    st.stop()

sch = q("SELECT * FROM scholarships")
runs = q("SELECT * FROM runs ORDER BY id DESC")
changes = q("""SELECT c.*, s.name FROM change_log c JOIN scholarships s ON s.id=c.scholarship_id ORDER BY c.id DESC""")

st.title("🎓 Scholarship Intelligence")
last = runs.iloc[0] if len(runs) else None
st.caption(f"Database: `{DB_PATH.name}` · {len(runs)} crawler runs · last run #{last['id']} ({last['mode']}) finished {last['finished_at']}"
           if last is not None else "No runs yet")

# ------------------------------------------------------------------ KPIs
week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
recent_ids = set(changes[(changes["detected_at"] >= week_ago) & (changes["change_type"] != "NEW")]["scholarship_id"])
k = st.columns(9)
k[0].metric("Total discovered", len(sch))
k[1].metric("Verified (≥95%)", int((sch["verification_status"] == "VERIFIED").sum()))
k[2].metric("Review required", int((sch["verification_status"] == "REVIEW_REQUIRED").sum()))
k[3].metric("Active", int((sch["lifecycle_status"] == "ACTIVE").sum()))
k[4].metric("Expiring soon", int((sch["lifecycle_status"] == "EXPIRING_SOON").sum()))
k[5].metric("Expired", int((sch["lifecycle_status"] == "EXPIRED").sum()))
k[6].metric("No longer verifiable", int((sch["lifecycle_status"] == "NO_LONGER_VERIFIABLE").sum()))
k[7].metric("Recently updated (7d)", len(recent_ids))
k[8].metric("Avg confidence", f"{sch['confidence'].mean():.1f}%" if len(sch) else "–")

tab_list, tab_changes, tab_runs, tab_sources = st.tabs(["Scholarships", "Change log", "Crawler runs", "Discovery & sources"])

# ------------------------------------------------------------------ list + detail
with tab_list:
    c1, c2, c3, c4 = st.columns([3, 1.3, 1.3, 1.6])
    term = c1.text_input("Search name, provider, eligibility", "")
    vf = c2.multiselect("Verification", ["VERIFIED", "REVIEW_REQUIRED"])
    lf = c3.multiselect("Status", ["ACTIVE", "EXPIRING_SOON", "EXPIRED", "REVIEW_REQUIRED", "NO_LONGER_VERIFIABLE"])
    tf = c4.multiselect("Source type", sorted(sch["source_type"].dropna().unique()))
    df = sch.copy()
    if term:
        t = term.lower()
        df = df[df.apply(lambda r: t in " ".join(str(r[c]) for c in ("name", "provider", "eligibility_text", "benefit",
                                                                          "education_level", "category_criteria")).lower(), axis=1)]
    if vf:
        df = df[df["verification_status"].isin(vf)]
    if lf:
        df = df[df["lifecycle_status"].isin(lf)]
    if tf:
        df = df[df["source_type"].isin(tf)]
    df = df.sort_values(["confidence"], ascending=False)
    view = pd.DataFrame({
        "Scholarship": df["name"], "Provider": df["provider"], "Type": df["source_type"],
        "Amount (max)": df["amount_inr_max"].apply(lambda v: fmt_inr(v) if pd.notna(v) else "Not specified"),
        "Deadline": df["closing_date"].fillna("Not specified"), "Status": df["lifecycle_status"],
        "Verification": df["verification_status"], "Confidence": df["confidence"],
        "Last verified": df["last_verified"].str[:10],
    })
    ev = st.dataframe(view, width="stretch", hide_index=True, on_select="rerun", selection_mode="single-row",
                      column_config={"Confidence": st.column_config.ProgressColumn("Confidence", min_value=0, max_value=100, format="%.1f%%")},
                      height=min(560, 38 + 35 * len(view)))
    st.caption(f"{len(view)} of {len(sch)} records · select a row to open the scholarship")
    sel = ev.selection.rows if ev and ev.selection else []
    if sel:
        r = df.iloc[sel[0]]
        sid = r["id"]
        st.divider()
        st.subheader(r["name"])
        st.markdown(badge(r["verification_status"]) + badge(r["lifecycle_status"]) +
                    f'<span class="badge" style="background:#eef2ff;color:#3730a3">{r["confidence"]:.1f}% confidence</span>'
                    f'<span class="badge" style="background:#f1f5f9;color:#334155">{r["source_type"]}</span>', unsafe_allow_html=True)
        st.caption(r["status_reason"] or "")

        left, right = st.columns([1.15, 1])
        with left:
            st.markdown("##### Scholarship details")
            facts = [
                ("Provider", r["provider"]), ("Amount / benefit", r["benefit"]),
                ("Max amount (parsed)", fmt_inr(r["amount_inr_max"]) if pd.notna(r["amount_inr_max"]) else "Not specified"),
                ("Education level", r["education_level"]), ("Course", r["course"]),
                ("Academic requirement", r["academic_requirement"]), ("Income criteria", r["income_criteria"]),
                ("Income limit (parsed)", fmt_inr(r["income_limit_inr"]) if pd.notna(r["income_limit_inr"]) else "Not specified"),
                ("Gender", r["gender_criteria"]), ("Category", r["category_criteria"]), ("Domicile", r["domicile_criteria"]),
                ("Age", r["age_criteria"]), ("Institution", r["institution_criteria"]), ("Disability", r["disability_criteria"]),
                ("Opening date", r["opening_date"] or "Not specified"), ("Closing date", r["closing_date"] or "Not specified"),
                ("Academic year", r["academic_year"]), ("Status statement on source", r["status_statement"]),
                ("Application mode", r["application_mode"]), ("Documents required", r["documents_required"]),
                ("Selection process", r["selection_process"]), ("Renewal", r["renewal_requirements"]),
            ]
            st.table(pd.DataFrame(facts, columns=["Field", "Value"]).set_index("Field"))
            st.markdown(f"**Official source:** [{r['official_source_url']}]({r['official_source_url']})")
            if r["application_url"]:
                st.markdown(f"**Application link:** [{r['application_url']}]({r['application_url']})")
            else:
                st.markdown("**Application link:** Not specified")
            st.markdown(f"**Last verified:** {r['last_verified']} · **First seen:** {r['first_seen']} · **Discovered via:** {r['discovered_via']}")
            with st.expander("Eligibility (verbatim from source)"):
                st.write(r["eligibility_text"])
            with st.expander("Machine-readable eligibility rules"):
                st.json(json.loads(r["eligibility_rules"] or "{}"))
        with right:
            st.markdown("##### Why this score?")
            checks = pd.DataFrame(json.loads(r["score_breakdown"] or "[]"))
            if len(checks):
                checks = checks.rename(columns={"check": "Check", "points": "Points", "max": "Max", "detail": "Evidence"})
                st.dataframe(checks, hide_index=True, width="stretch")
                st.caption("Score = sum of deterministic checks (no LLM-generated numbers). VERIFIED requires ≥95%, "
                           "the provider's own official page, live presence in this run, and no conflicting official source.")
            st.markdown("##### Change history")
            h = changes[changes["scholarship_id"] == sid][["detected_at", "change_type", "field", "old_value", "new_value", "run_id"]]
            st.dataframe(h, hide_index=True, width="stretch") if len(h) else st.write("No changes recorded.")
            src = q("SELECT url, authority, role, closing_date, amount_inr_max, last_seen FROM scholarship_sources WHERE scholarship_id=?", (sid,))
            st.markdown("##### Sources")
            st.dataframe(src, hide_index=True, width="stretch")

        st.markdown("##### Source evidence — Database → Official source → Evidence → Value")
        evd = q("""SELECT e.field, e.value, e.evidence_text, e.source_url, e.snapshot_id, e.method, s.fetched_at, s.archived_from
                   FROM field_evidence e LEFT JOIN snapshots s ON s.id=e.snapshot_id
                   WHERE e.scholarship_id=? AND e.is_current=1 ORDER BY e.id""", (sid,))
        for fld, grp in evd.groupby("field", sort=False):
            g0 = grp.iloc[0]
            st.markdown(f"**{fld}** → `{str(g0['value'])[:160]}`  ·  <small>{g0['method']} · snapshot #{g0['snapshot_id']} · "
                        f"{g0['fetched_at']}</small>", unsafe_allow_html=True)
            for _, e in grp.iterrows():
                st.markdown(f'<div class="quote">{html.escape(str(e["evidence_text"])[:900])}</div>', unsafe_allow_html=True)
        with st.expander("Open the stored snapshot of the official page (evidence highlighted)"):
            snap = conn().execute("SELECT * FROM snapshots WHERE id=?", (int(r["current_snapshot_id"]),)).fetchone()
            if snap:
                txt = html.escape(snap["text"][:60000])
                for e in evd["evidence_text"].dropna().unique():
                    if not e.startswith("link text"):
                        esc = html.escape(e)
                        txt = txt.replace(esc, f"<mark>{esc}</mark>", 1)
                st.caption(f"{snap['url']} · fetched {snap['fetched_at']} · HTTP {snap['http_status']}"
                           + (f" · archived copy {snap['archived_from']}" if snap["archived_from"] else ""))
                st.markdown(f'<div class="quote" style="max-height:480px;overflow:auto">{txt}</div>', unsafe_allow_html=True)

# ------------------------------------------------------------------ changes
with tab_changes:
    st.markdown("Every difference between crawls is logged with old value, new value, source and evidence — nothing is overwritten silently.")
    types = st.multiselect("Change type", sorted(changes["change_type"].unique()), default=[t for t in changes["change_type"].unique() if t != "NEW"])
    cv = changes[changes["change_type"].isin(types)] if types else changes
    st.dataframe(cv[["detected_at", "run_id", "name", "change_type", "field", "old_value", "new_value", "source_url", "old_evidence", "new_evidence"]],
                 hide_index=True, width="stretch", height=520)

# ------------------------------------------------------------------ runs
with tab_runs:
    st.dataframe(runs, hide_index=True, width="stretch")

# ------------------------------------------------------------------ sources
with tab_sources:
    src = q("SELECT * FROM sources")
    a, b = st.columns(2)
    a.markdown("**How pages were discovered**")
    a.dataframe(src.groupby("discovered_via").size().rename("pages").reset_index(), hide_index=True, width="stretch")
    b.markdown("**How scholarship records were discovered**")
    b.dataframe(sch.groupby("discovered_via").size().rename("records").reset_index(), hide_index=True, width="stretch")
    a.markdown("**Records by source type**")
    a.dataframe(sch.groupby("source_type").size().rename("records").reset_index(), hide_index=True, width="stretch")
    b.markdown("**Page classification**")
    b.dataframe(src.groupby("page_kind", dropna=False).size().rename("pages").reset_index(), hide_index=True, width="stretch")
    st.markdown("**All crawled URLs**")
    st.dataframe(src[["url", "discovered_via", "discovered_from", "depth", "page_kind", "authority", "source_type", "last_http_status", "last_error"]],
                 hide_index=True, width="stretch", height=420)
