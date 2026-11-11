"""Text helpers: normalisation, Indian date and rupee-amount parsing, name keys.

All parsers here are deterministic. They only *read* values that literally appear in text;
nothing is inferred or defaulted.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date

# ---------------------------------------------------------------- normalisation

def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def norm_for_match(s: str) -> str:
    """Normalisation used when checking that an evidence quote exists in a snapshot."""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


def sha1(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8", "ignore")).hexdigest()


SCHOLARSHIP_TERMS = re.compile(
    r"\b(scholarships?|fellowships?|chhatra?vritti|chatravritti|shishyavrutti|vidyadhan|vidyasiri|"
    r"stipends?|bursar(y|ies)|merit[- ]cum[- ]means|financial assistance|education(al)? (grant|loan subsidy|assistance)|"
    r"(post|pre)[- ]matric|top class education|yojana|protsahan|incentive scheme|"
    r"scheme for (students|girls)|award for (students|girls))\b",
    re.I,
)


def has_scholarship_term(s: str) -> bool:
    return bool(SCHOLARSHIP_TERMS.search(s or ""))


_YEAR_RX = re.compile(r"\b(19|20)\d{2}\s*[-–/]\s*(\d{2}|\d{4})\b|\b(19|20)\d{2}\b")
_GENERIC_NAMES = re.compile(
    r"^(the )?(list of |all |various |other |central |state |government |online |national )*"
    r"(scholarships?|fellowships?|schemes?)( (and|&) (fellowships?|scholarships?|internships?|schemes?))*"
    r"( (schemes?|portal|section|cell|notice|notices|details|information|page|division|corner|programmes?))*$",
    re.I,
)


def clean_name(name: str) -> str:
    name = norm_space(name)
    name = re.sub(r"\s*\(\s*[^()A-Za-z]*[\u0900-\u0DFF][^()]*\)", "", name)        # Hindi/regional-script asides
    name = re.sub(r"\s*(click here|read more|know more|apply now|learn more)\b.*$", "", name, flags=re.I)
    name = re.sub(r"\s*[|•»>]\s*.*$", "", name) if len(name) > 90 else name
    name = name.strip(" -:|.,")
    return name


def name_key(name: str) -> str:
    """Year-independent key so 'X Scholarship 2025-26' and 'X Scholarship 2026-27' match."""
    s = norm_for_match(name)
    s = _YEAR_RX.sub(" ", s)
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\b(the|for|of|and|apply|online|now|open|invites?|applications?|notification|programme|program)\b", " ", s)
    s = re.sub(r"\bscholarships\b", "scholarship", s)
    return norm_space(s)


_ENUM_START = re.compile(r"^\s*(\(?\d{1,2}(\.\d+)*[.)]|\d{1,2}(\.\d+)+\s|\(?[ivxIVX]{1,5}[.)]|\(?[A-Za-z][.)])\s?|^[^A-Za-z0-9\s(]")
_JUNK = re.compile(r"\b(announc\w*|invites?|invited|results?|awarded|story|stories|investing|giving|behind|notices?|opportunit\w*|about|"
                   r"amounts?|allocation|rates? of|details|procedure|completion|issue of|following|guidelines?|circular|minutes|"
                   r"selected|faq|how to|mentioned|below|as under|regarding|extension|extended|corrigendum|webinar|events?|news|"
                   r"press|media|ceremony|felicitat\w*|congratulat\w*|winners?|recipients?|awardees?|testimonials?|alumni|"
                   r"applications? (open|invited|closed)|apply|download|form|portal|login|register|registration|status|tenure|"
                   r"important|proposed|thrust areas?|commission in|values|database|archive|duration|number of|stages|structure|renewal of|process|cases|criteria|eligibility|benefits?|overview|introduction|objectives?|purpose|background|annexure|appendix)\b", re.I)
_GENERIC_WORDS = {"scholarship", "scholarships", "fellowship", "fellowships", "internship", "internships", "scheme", "schemes",
                  "and", "for", "the", "of", "students", "student", "award", "awards", "programme", "program", "programmes",
                  "india", "indian", "national", "government", "state", "central", "online", "new", "all", "other", "various", "to", "in",
                  "programs", "study", "opportunities", "financial", "assistance", "support", "our", "current", "open", "available"}


def is_generic_name(name: str) -> bool:
    raw = norm_space(name)
    if _ENUM_START.match(raw) or (raw[:1].islower()):
        return True
    words = raw.split()
    if len(words) > 26 or (len(words) > 6 and re.search(r"[:;]", raw)) or raw.endswith((" is", " are", " as", " the", " of", " to", "?")):
        return True
    if re.search(r"\b(is|are|will|shall|provides?|entitled|offered under|was|were|has|have)\b", raw) or \
            re.match(r"(who|what|which|when|where|how|am i|each|these|this|our|your|finding|information|instructions?)\b", raw, re.I):
        return True
    if _JUNK.search(raw):
        return True
    rest = [w for w in re.findall(r"[A-Za-z]+", _YEAR_RX.sub(" ", raw).lower()) if w not in _GENERIC_WORDS and len(w) >= 2]
    if not rest:
        return True
    n = norm_space(re.sub(r"[^A-Za-z&\- ]", " ", _YEAR_RX.sub(" ", name)))
    if len(n) < 8:
        return True
    if _GENERIC_NAMES.match(n):
        return True
    bad = re.compile(r"\b(result|merit list|selected candidates|list of (selected|eligible)|tender|corrigendum|"
                     r"faq|frequently asked|login|sign in|grievance|helpdesk|contact us|guidelines? for (institutes|nodal))\b", re.I)
    return bool(bad.search(n))


# ---------------------------------------------------------------- dates

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
DATE_PATTERNS = [
    # 31-10-2026, 31/10/2026, 31.10.2026  (Indian dd-mm-yyyy)
    re.compile(r"\b(?P<d>[0-3]?\d)\s*[./-]\s*(?P<m>[01]?\d)\s*[./-]\s*(?P<y>20\d{2})\b"),
    # 31st October 2026, 31 Oct, 2026, 31-Oct-2026
    re.compile(r"\b(?P<d>[0-3]?\d)\s?(?:st|nd|rd|th)?\s*[-\s,]?\s*(?:of\s+)?(?P<mon>" + _MON + r")\.?\s*[-\s,]?\s*(?P<y>20\d{2})\b", re.I),
    # October 31, 2026
    re.compile(r"\b(?P<mon>" + _MON + r")\.?\s+(?P<d>[0-3]?\d)(?:st|nd|rd|th)?\s*,?\s*(?P<y>20\d{2})\b", re.I),
]


def find_dates(text: str) -> list[tuple[date, int, int, str]]:
    """Return [(date, start, end, matched_text)] for every unambiguous date in text."""
    out = []
    taken: list[tuple[int, int]] = []
    for rx in DATE_PATTERNS:
        for m in rx.finditer(text or ""):
            if any(not (m.end() <= a or m.start() >= b) for a, b in taken):
                continue
            try:
                d = int(m.group("d"))
                y = int(m.group("y"))
                if "mon" in m.groupdict() and m.groupdict().get("mon"):
                    mo = MONTHS[m.group("mon").lower().rstrip(".")[:3] if m.group("mon").lower()[:4] != "sept" else "sep"]
                else:
                    mo = int(m.group("m"))
                out.append((date(y, mo, d), m.start(), m.end(), m.group(0)))
                taken.append((m.start(), m.end()))
            except (ValueError, KeyError):
                continue
    out.sort(key=lambda t: t[1])
    return out


ACADEMIC_YEAR_RX = re.compile(r"\b(20\d{2})\s*[-–/]\s*(20)?(\d{2})\b")


def academic_years(text: str) -> list[str]:
    ys = []
    for m in ACADEMIC_YEAR_RX.finditer(text or ""):
        a = int(m.group(1))
        b = int(m.group(3))
        if (a + 1) % 100 == b:
            ys.append(f"{a}-{b:02d}")
    return ys


def current_academic_year(today: date, start_month: int = 6) -> str:
    a = today.year if today.month >= start_month else today.year - 1
    return f"{a}-{(a + 1) % 100:02d}"


# ---------------------------------------------------------------- money

_AMT_RX = re.compile(
    r"(?:₹|rs\.?|inr|rupees)\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>lakhs?|lacs?|lakh|crores?|cr\b|k\b|thousand)?"
    r"|(?P<num2>\d[\d,]*(?:\.\d+)?)\s*(?P<unit2>lakhs?|lacs?|crores?)\b"
    r"|(?P<num3>\d[\d,]*(?:\.\d+)?)\s*/-",
    re.I,
)


def ocr_suspect(num: str) -> bool:
    """Numbers that look like a scanned '/-' read as a trailing '1' (120001, 75,0001, 30001),
    or with impossible digit grouping. Such amounts are left unparsed rather than guessed."""
    groups = num.split(",")
    if len(groups) > 1 and (len(groups[-1].split(".")[0]) != 3 or any(len(g) not in (2, 3) for g in groups[1:-1])):
        return True
    digits = num.replace(",", "").split(".")[0]
    return len(digits) >= 5 and bool(re.fullmatch(r"\d+0{2,}1", digits))


def parse_inr_amounts(text: str) -> list[tuple[int, int, int, str]]:
    """Return [(rupees, start, end, matched)] for each rupee amount in text."""
    out = []
    for m in _AMT_RX.finditer(text or ""):
        num = m.group("num") or m.group("num2") or m.group("num3")
        unit = (m.group("unit") or m.group("unit2") or "").lower()
        if ocr_suspect(num):
            continue        # e.g. "Rs. 12000/-" read from a scanned PDF as "Rs. 120001": never guess the real figure
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        if unit.startswith(("lakh", "lac")):
            v *= 100_000
        elif unit.startswith(("crore", "cr")):
            v *= 10_000_000
        elif unit in ("k", "thousand"):
            v *= 1000
        if v < 100:          # "Rs. 5" style noise / page numbers
            continue
        out.append((int(round(v)), m.start(), m.end(), m.group(0)))
    return out


def fmt_inr(v: int | None) -> str:
    if v is None:
        return "Not specified"
    s = str(int(v))
    if len(s) <= 3:
        return "₹" + s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return "₹" + ",".join(parts) + "," + tail


def sentence_around(text: str, start: int, end: int, max_len: int = 320) -> tuple[str, int, int]:
    """Expand [start,end) to the enclosing line/sentence; returns (span, s, e) of verbatim text."""
    s = max(text.rfind("\n", 0, start), 0)
    s = s + 1 if text[s:s + 1] == "\n" else s
    e = text.find("\n", end)
    e = len(text) if e == -1 else e
    if e - s > max_len:
        # narrow to sentence boundaries inside the line
        ss = max(text.rfind(". ", s, start), s)
        ss = ss + 2 if text[ss:ss + 2] == ". " else ss
        ee = text.find(". ", end, e)
        ee = e if ee == -1 else ee + 1
        s, e = ss, ee
        if e - s > max_len:
            s = max(s, start - max_len // 2)
            e = min(e, end + max_len // 2)
    return text[s:e].strip(), s, e
