"""Verification engine.

1. Evidence gate  – every extracted value must be re-found verbatim in the stored snapshot
                    (or, for links, in the snapshot's link table). Values that fail are dropped
                    and the field becomes "Not specified". This is the anti-hallucination guard.
2. Confidence     – a deterministic, additive score built from observable checks (no LLM involved).
3. Lifecycle      – ACTIVE / EXPIRING_SOON / EXPIRED / REVIEW_REQUIRED / NO_LONGER_VERIFIABLE,
                    derived from evidenced dates and status statements.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .textutil import current_academic_year, find_dates, name_key, norm_for_match

AUTHORITY_POINTS = {"PRIMARY_OFFICIAL": 25.0, "SECONDARY_OFFICIAL": 15.0, "UNVERIFIED": 6.0, "AGGREGATOR": 0.0}
CRITICAL = ["provider", "benefit", "eligibility", "education_level", "deadline_or_status"]


# =============================================================================== evidence gate
def locate(span: str, haystack_norm: str) -> int:
    return haystack_norm.find(norm_for_match(span))


def verify_candidate(cand, page) -> tuple[dict, list[str]]:
    """Return ({field: Ev}, [dropped field names]). Name is checked too."""
    hay = norm_for_match((page.title or "") + "\n" + (page.meta.get("og:title", "")) + "\n" + page.text)
    link_set = {l.url for l in page.links}
    ok: dict = {}
    dropped: list[str] = []

    def span_ok(span: str) -> bool:
        m = re.match(r'link text "(.*)" -> (\S+)$', span, re.S)
        if m:
            return m.group(2) in link_set
        return locate(span, hay) >= 0

    for f, ev in [("name", cand.name)] + list(cand.fields.items()):
        if ev.spans and all(span_ok(s) for s in ev.spans):
            ok[f] = ev
        else:
            dropped.append(f)
    return ok, dropped


# =============================================================================== scoring
@dataclass
class ScoreInput:
    authority: str
    http_ok: bool
    archived: bool
    page_text: str
    page_title: str
    name: str
    fields: dict                         # verified fields {name: Ev}
    app_url_status: int | None           # HTTP status from checking the application URL (None = not checked)
    second_pass: dict                    # {"closing_date": str|None, "amount": int|None} from whole-page extraction
    corroborations: list[dict] = field(default_factory=list)   # other official pages [{url, authority, closing_date, amount}]
    today: date = field(default_factory=date.today)
    ay_start_month: int = 6


def _chk(name, pts, mx, detail):
    return {"check": name, "points": round(max(0.0, min(pts, mx)), 2), "max": mx, "detail": detail}


def score(si: ScoreInput) -> tuple[float, list[dict], list[str]]:
    """Return (confidence 0-100, checks, conflicts)."""
    f = si.fields
    checks = []
    # 1. Source authority (25)
    ap = AUTHORITY_POINTS.get(si.authority, 0)
    checks.append(_chk("Official source", ap, 25, f"Source classified as {si.authority}"))

    # 2. Present on the official source right now (15)
    live = (8.0 if si.http_ok and not si.archived else 4.0 if si.http_ok else 0.0)
    nk = name_key(si.name).split()
    hay = norm_for_match(si.page_title + "\n" + si.page_text)
    present = [t for t in nk if t in hay]
    ratio = len(present) / len(nk) if nk else 0
    det = ("Fetched live (HTTP 2xx)" if si.http_ok and not si.archived else
           "Archived copy only (not live)" if si.http_ok else "Source not reachable")
    checks.append(_chk("Currently present on source", live + 7.0 * ratio, 15,
                       f"{det}; {len(present)}/{len(nk)} name tokens found on page"))

    # 3. Official application path (10)
    app = f.get("application_url")
    mode = f.get("application_mode")
    if app:
        pts = 5.0
        if si.app_url_status and 200 <= si.app_url_status < 400:
            pts += 5.0
            d = f"Application link on official page; resolves (HTTP {si.app_url_status})"
        elif si.app_url_status:
            d = f"Application link on official page; returned HTTP {si.app_url_status}"
        else:
            d = "Application link on official page; reachability not checked"
        checks.append(_chk("Official application URL", pts, 10, d))
    elif mode and mode.parsed == "offline":
        checks.append(_chk("Official application URL", 7.0, 10, "Source states an offline application procedure"))
    elif mode:
        checks.append(_chk("Official application URL", 3.0, 10, "Source says apply online but no application link found"))
    else:
        checks.append(_chk("Official application URL", 0, 10, "No application link or procedure found"))

    # 4. Critical fields traceable to evidence (30 = 5 x 6)
    have = {
        "provider": "provider" in f,
        "benefit": "benefit" in f,
        "eligibility": "eligibility_text" in f or sum(k in f for k in ("income_criteria", "academic_requirement", "category_criteria",
                                                                       "gender_criteria", "domicile_criteria", "institution_criteria")) >= 2,
        "education_level": "education_level" in f,
        "deadline_or_status": "closing_date" in f or "status_statement" in f,
    }
    missing = [k for k, v in have.items() if not v]
    checks.append(_chk("Critical fields backed by evidence", 6.0 * sum(have.values()), 30,
                       "All critical fields evidenced" if not missing else "Missing: " + ", ".join(missing)))

    # 5. Freshness (10): evidence that the page describes the current cycle
    cur_ay = current_academic_year(si.today, si.ay_start_month)
    pts, notes = 0.0, []
    ay = f.get("academic_year")
    cycle_dates = [f[k].parsed for k in ("closing_date", "opening_date") if k in f and isinstance(f[k].parsed, date)]
    cycle_dates = [d for d in cycle_dates if -365 <= (d - si.today).days <= 400]
    if ay and int(ay.value[:4]) >= int(cur_ay[:4]):
        pts += 5
        notes.append(f"mentions AY {ay.value} (current {cur_ay})")
    elif cycle_dates:
        pts += 5
        notes.append(f"application dates in the last/next 12 months ({', '.join(d.isoformat() for d in cycle_dates)})")
    elif ay and int(ay.value[:4]) == int(cur_ay[:4]) - 1:
        pts += 2
        notes.append(f"mentions previous AY {ay.value}")
    else:
        notes.append(f"no current-cycle evidence (AY {ay.value})" if ay else "no academic year or recent application dates")
    dates = [d for d, *_ in find_dates(si.page_text)]
    recent = [d for d in dates if abs((d - si.today).days) <= 400]
    if recent:
        pts += 3
        notes.append(f"{len(recent)} date(s) on page within ~13 months")
    if si.http_ok and not si.archived:
        pts += 2
        notes.append("verified in this run")
    checks.append(_chk("Information is current", pts, 10, "; ".join(notes)))

    # 6. Consistency (10): two independent extraction passes + agreement across official sources
    cd = f.get("closing_date")
    b = f.get("benefit")
    amt = b.parsed if b else None
    agree = True
    why = []
    sp = si.second_pass or {}
    if (cd.value if cd else None) != sp.get("closing_date") and sp.get("closing_date") is not None and cd is not None:
        agree = False
        why.append(f"deadline {cd.value} vs {sp.get('closing_date')}")
    if amt is not None and sp.get("amount") is not None and amt != sp.get("amount"):
        agree = False
        why.append(f"amount {amt} vs {sp.get('amount')}")
    conflicts = []
    for c in si.corroborations:
        if c["authority"] not in ("PRIMARY_OFFICIAL", "SECONDARY_OFFICIAL"):
            continue
        if cd and c.get("closing_date") and c["closing_date"] != cd.value and c.get("academic_year") in (None, f.get("academic_year").value if f.get("academic_year") else None):
            conflicts.append(f"{c['url']} states deadline {c['closing_date']} (record: {cd.value})")
    pts = (5.0 if agree else 0.0) + (5.0 if not conflicts else 0.0)
    det = ("Section-scoped and whole-page extraction agree" if agree else "Extraction passes disagree: " + "; ".join(why))
    det += "; no conflicting official sources" if not conflicts else "; CONFLICT: " + " | ".join(conflicts)
    checks.append(_chk("Consistent extraction & no conflicts", pts, 10, det))

    total = sum(c["points"] for c in checks)
    # Bonus: independent official corroboration (another official domain lists the same scholarship)
    indep = [c for c in si.corroborations if c["authority"] in ("PRIMARY_OFFICIAL", "SECONDARY_OFFICIAL") and c.get("independent")]
    if indep:
        bonus = min(5.0, 100 - total)
        checks.append({"check": "Independent official corroboration (bonus)", "points": round(bonus, 2), "max": 5.0,
                       "detail": f"{len(indep)} other official source(s): " + ", ".join(c['url'] for c in indep[:3])})
        total += bonus
    if conflicts:
        checks.append({"check": "Conflict penalty", "points": -10.0, "max": 0.0, "detail": "; ".join(conflicts)})
        total -= 10
    return round(max(0.0, min(100.0, total)), 1), checks, conflicts


def verification_status(conf: float, authority: str, conflicts: list, live_ok: bool, threshold: float = 95.0) -> tuple[str, str]:
    reasons = []
    if conf < threshold:
        reasons.append(f"confidence {conf}% < {threshold}%")
    if authority != "PRIMARY_OFFICIAL":
        reasons.append(f"source is {authority}, not the provider's own official page")
    if conflicts:
        reasons.append("conflicting official sources")
    if not live_ok:
        reasons.append("not confirmed on the live official page in this run")
    return ("VERIFIED", "All gates passed") if not reasons else ("REVIEW_REQUIRED", "; ".join(reasons))


# =============================================================================== lifecycle
def lifecycle(fields: dict, as_of: date, expiring_days: int, ay_start_month: int) -> tuple[str, str]:
    cd = fields.get("closing_date")
    st = fields.get("status_statement")
    ay = fields.get("academic_year")
    cur = current_academic_year(as_of, ay_start_month)
    stmt = (st.parsed or "") if st else ""
    if cd:
        d = cd.parsed if isinstance(cd.parsed, date) else date.fromisoformat(cd.value)
        if d < as_of:
            return "EXPIRED", f"Closing date {d.isoformat()} has passed"
        if (d - as_of).days <= expiring_days:
            return "EXPIRING_SOON", f"Closes on {d.isoformat()} ({(d - as_of).days} days left)"
        return "ACTIVE", f"Open until {d.isoformat()}"
    if re.search(r"closed|not accepting", stmt):
        return "EXPIRED", f'Source states: "{st.value[:120]}"'
    if ay and int(ay.value[:4]) < int(cur[:4]):
        return "EXPIRED", f"Source refers to AY {ay.value}; current AY is {cur} and no newer cycle is published"
    if re.search(r"open|invited|being accepted|live|extended", stmt):
        return "ACTIVE", f'Source states: "{st.value[:120]}" (no closing date published)'
    return "REVIEW_REQUIRED", "No closing date or open/closed statement on the official source"
