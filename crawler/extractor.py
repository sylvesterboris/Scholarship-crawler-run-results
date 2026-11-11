"""Evidence-first extraction.

Every field is produced together with the *verbatim* span of page text it was read from.
If no span supports a field, the field is "Not specified" — nothing is guessed, defaulted or
completed from background knowledge.

The extractor works on the line-structured text produced by fetcher.Page:
  '# '  = h1, '## ' = other headings, '- ' = list items, ' | ' = table cells.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .textutil import (academic_years, clean_name, find_dates, has_scholarship_term, is_generic_name,
                       norm_space, parse_inr_amounts, SCHOLARSHIP_TERMS)

NOT_SPECIFIED = "Not specified"


@dataclass
class Ev:
    value: str
    spans: list[str]                 # verbatim evidence strings (each must exist in the snapshot)
    method: str
    parsed: object = None            # int rupees / date / list, when applicable
    src: object = None               # (page, snapshot_id) when the value comes from a linked document, not the primary page


@dataclass
class Candidate:
    name: Ev
    fields: dict[str, Ev] = field(default_factory=dict)
    line_range: tuple[int, int] = (0, 0)
    signals: int = 0

    def get(self, f: str) -> Optional[Ev]:
        return self.fields.get(f)

    def value(self, f: str) -> str:
        e = self.fields.get(f)
        return e.value if e else NOT_SPECIFIED


# ------------------------------------------------------------------------------------------ patterns
SECTION_PATTERNS = {
    "eligibility": r"eligib|who can apply|criteria for (the )?(award|selection)|conditions? (of|for) (the )?(award|eligibility)|qualification",
    "benefits": r"benefit|amount|value of (the )?(scholarship|award|fellowship)|financial (assistance|support)|rates? of|"
                r"stipend|award (amount|details)|what (you|students|scholars) (get|will get|receive)|quantum|emolument|"
                r"scholarship (amount|value|details)|fellowship (amount|value)|grant amount|coverage",
    "documents": r"documents?\b|checklist|enclosures|certificates required",
    "selection": r"selection|how (are|will) .*selected|shortlist",
    "renewal": r"renewal|continuation|renew",
    "apply": r"how to apply|application (process|procedure)|procedure (for|to) apply|steps to apply|mode of application|application guidelines",
    "dates": r"important dates|key dates|timeline|deadlines?|schedule",
}
SECTION_RX = {k: re.compile(v, re.I) for k, v in SECTION_PATTERNS.items()}

CLOSE_LABEL = re.compile(
    r"last date|closing date|clos(e|es|ed|ing) on|deadline|end date|apply (by|before|till|until)|due date|last day|"
    r"portal (will )?close|(applications?|registration|submission) (will )?(close|end)s?|till|until|upto|up to", re.I)
OPEN_LABEL = re.compile(
    r"opening date|open(s|ed)? (on|from)|start(ing)? date|commence(s|ment)?|starts? (on|from)|"
    r"(applications?|registration|portal) (will )?(open|start)s?|launch(ed)? on|from", re.I)
NON_STUDENT_DEADLINE = re.compile(
    r"institut\w* (level )?verification|\b(dno|sno|ino|l1|l2|nodal)\b|verification by|result|interview|"
    r"announce|disburs|defective|re-?open(ing)? of defective|hard ?copy (to|at) (the )?institut|revert|right to give up|"
    r"grievance|correction|sent back|bank|aadhaar|seeding|withdraw|refund|tender|recruit|vacanc|admission to (the )?hostel", re.I)
STATUS_RX = re.compile(
    r"(applications?\b[^.\n]{0,80}?\b(are|is) (now )?(open|closed|invited|being accepted))|(closed for applications)|(applications? (are )?now (open|closed))|((registration|portal|application window) (is )?(now )?(open|closed|live))|"
    r"(last date (has been |is )?extended)|(currently closed)|(will (re)?open soon)|(coming soon)|(applications? closed)|"
    r"(no longer accepting (fresh |new )?applications)|(not accepting applications)|(closed for (the )?(session|year|academic year))", re.I)

LEVEL_RX = [
    ("Pre-matric (Class 1-10)", re.compile(r"pre[- ]matric|class(es)?\s*(i{1,3}|iv|v|vi{1,3}|ix|x|[1-9]|10)(th)?\b|\b(9th|10th) (class|standard|grade)", re.I)),
    ("Class 11-12", re.compile(r"class(es)?\s*(xi|xii|11|12)(th)?\b|\b(11th|12th) (class|standard|grade)|higher secondary|intermediate|\+2\b|plus two|junior college|puc\b", re.I)),
    ("Post-matric", re.compile(r"post[- ]matric", re.I)),
    ("Diploma/ITI/Polytechnic", re.compile(r"\bdiploma\b|\biti\b|polytechnic", re.I)),
    ("Undergraduate", re.compile(r"under ?graduate|\bug\b|bachelor|b\.?\s?tech|\bb\.e\.?|mbbs|\bbds\b|b\.?sc\b|b\.?com\b|\bb\.?a\.|graduation (course|degree|level)|"
                                 r"degree (course|programme)|first[- ]year (of )?(college|graduation|ug)|professional courses?", re.I)),
    ("Postgraduate", re.compile(r"post ?graduate|post-graduate|\bpg\b|master'?s|m\.?\s?tech|\bm\.e\.?|m\.?sc\b|\bmba\b|\bm\.?a\.|m\.?pharm|\bgate\b|\bgpat\b|\bllm\b", re.I)),
    ("Doctoral/Research", re.compile(r"ph\.?\s?d|doctoral|research (scholar|fellow)|\bjrf\b|\bsrf\b|post[- ]?doc", re.I)),
]
CATEGORY_RX = [
    ("SC", re.compile(r"\bSCs?\b|(?i:scheduled castes?)")),
    ("ST", re.compile(r"\bSTs?\b|(?i:scheduled tribes?)")),
    ("OBC", re.compile(r"\bOBCs?\b|other backward class", re.I)),
    ("EBC", re.compile(r"\bEBCs?\b|economically backward class", re.I)),
    ("DNT", re.compile(r"\bDNTs?\b|de-?notified", re.I)),
    ("EWS", re.compile(r"\bEWS\b|economically weaker section", re.I)),
    ("Minority", re.compile(r"\bminorit(y|ies)\b|muslim|christian|sikh|buddhist|jain|parsi|zoroastrian", re.I)),
    ("BPL", re.compile(r"\bBPL\b|below poverty line", re.I)),
]
INDIAN_STATES = ["Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", "Haryana",
                 "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur",
                 "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana",
                 "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal", "Delhi", "Jammu and Kashmir", "Jammu & Kashmir",
                 "Ladakh", "Puducherry", "Chandigarh", "Andaman", "Lakshadweep", "Dadra", "Daman"]

ORG_RX = [
    re.compile(r"\b(?:(?:offered|instituted|sponsored|funded|awarded|implemented|administered|run|managed|supported|launched)\s+by|"
               r"(?:an |a flagship |the )?(?:initiative|programme|program|project|CSR initiative) of)\s+(?:the\s+)?"
               r"((?:[A-Z][\w&.'-]*\s*){1,8}?(?:Foundation|Trust|Endowment|Society|Limited|Ltd\.?|Bank|Corporation|Council|Commission|"
               r"Board|University|Institute(?: of [A-Z]\w+)*|Fund|Office|Agency|Ministry of [A-Z][\w&' ]+|Department of [A-Z][\w&' ]+|Government of [A-Z]\w+(?: [A-Z]\w+)?|Group))"),
    re.compile(r"\b(?:an |a |the )?(?:initiative|CSR initiative|flagship programme|programme) (?:of|by)\s+(?:the\s+)?((?:[A-Z][\w&.'-]+\s?){1,6})"),
    re.compile(r"\b(Ministry of (?:[A-Z][a-z]+|&|and|of)(?:\s+(?:[A-Z][a-z]+|&|and|of)){0,6})"),
    re.compile(r"\b(Department of (?:[A-Z][a-z]+|&|and|of)(?:\s+(?:[A-Z][a-z]+|&|and|of)){0,6})"),
    re.compile(r"\b(Directorate of (?:[A-Z][a-z]+|&|and|of)(?:\s+(?:[A-Z][a-z]+|&|and|of)){0,5})"),
    re.compile(r"\b(All India Council for Technical Education|University Grants Commission|Kendriya Sainik Board|Department of Science and Technology|"
               r"National Testing Agency|United States-India Educational Foundation)\b"),
    re.compile(r"\b((?:[A-Z][A-Za-z.&'-]+\s){0,4}[A-Z][A-Za-z.&'-]+\s(?:Foundation|Trust|Endowment|Charitable Trust|Educational Trust|Education Foundation))\b"),
    re.compile(r"\b(Government of (?:India|[A-Z][a-z]+(?: [A-Z][a-z]+)?))\b"),
]
GENERIC_ORG = re.compile(r"^(the |a |this |our |your )?(foundation|trust|society|board|council|commission|ministry|department|government)$", re.I)


# ------------------------------------------------------------------------------------------ helpers
def _strip(line: str) -> str:
    return re.sub(r"^(#{1,2} |- )", "", line).strip()


def _is_heading(line: str) -> bool:
    if line.startswith(("# ", "## ")):
        return True
    t = line.strip()
    return 3 <= len(t) <= 70 and t.endswith(":") and not re.search(r"\d{2,}", t)


def _heading_level(line: str) -> int:
    return 1 if line.startswith("# ") else 2


class Extractor:
    def __init__(self, today: date):
        self.today = today
        self._current_name = ""

    # ====================================================================== page analysis
    def find_sections(self, lines: list[str], lo: int, hi: int) -> dict[str, tuple[int, int]]:
        """Return {section: (start_line, end_line)} for headings inside [lo, hi).
        Real headings (#/##, 'Label:') win; short label-like lines are used only for sections not found that way.
        List items ('- Benefit' in a menu) are never treated as section headings."""
        real = [i for i in range(lo, hi) if _is_heading(lines[i])]
        pseudo = [i for i in range(lo, hi) if not _is_heading(lines[i]) and not lines[i].startswith("- ")
                  and self._pseudo_heading(lines[i])]
        out: dict[str, tuple[int, int]] = {}
        for heads in (real, sorted(set(real) | set(pseudo))):
            for k, i in enumerate(heads):
                h = _strip(lines[i]).rstrip(":")
                if len(h) > 90:
                    continue
                nxt = heads[k + 1] if k + 1 < len(heads) else hi
                end = min(nxt, i + 45)
                for name, rx in SECTION_RX.items():
                    if name not in out and rx.search(h):
                        body = [l for l in lines[i + 1:end] if l.strip()]
                        if sum(len(l) for l in body) >= 30:      # a label followed by real content, not a tab strip
                            out[name] = (i, end)
                        break
        return out

    @staticmethod
    def _pseudo_heading(line: str) -> bool:
        t = _strip(line)
        return 3 <= len(t) <= 45 and any(rx.fullmatch(t.rstrip(":").strip()) or (rx.match(t) and len(t) < 35)
                                         for rx in SECTION_RX.values())

    def segment(self, lines: list[str], title: str, meta: dict) -> tuple[str, list[tuple[int, int, int]]]:
        """Decide page kind and return segments [(name_line, start, end)].
        name_line == -1 means the name comes from <title>/og:title."""
        n = len(lines)
        term_heads = [i for i, l in enumerate(lines)
                      if _is_heading(l) and has_scholarship_term(l) and not is_generic_name(clean_name(_strip(l)))
                      and len(clean_name(_strip(l))) < 140]
        page_signals = self._signals(lines, 0, n)

        # specific name in h1 or title -> detail page
        h1 = [i for i in term_heads if lines[i].startswith("# ")]
        name_from_title = self._title_name(title, meta)
        if (page_signals >= 3 or (page_signals >= 2 and (h1 or name_from_title))) and (h1 or name_from_title) and len(term_heads) <= 3:
            # content before the page's h1 is navigation / portal-wide banners, not this scholarship
            return "detail", [(h1[0], h1[0], n)] if h1 else [(-1, 0, n)]

        # several scheme headings each followed by criteria -> multi-scheme page
        segs = []
        for k, i in enumerate(term_heads):
            lvl = _heading_level(lines[i])
            end = n
            for j in term_heads[k + 1:]:
                if _heading_level(lines[j]) <= lvl:
                    end = j
                    break
            end = min(end, i + 120)
            if self._signals(lines, i, end) >= 2:
                segs.append((i, i, end))
        if len(segs) >= 2:
            return "multi", segs
        # official listings (e.g. NSP "Schemes on NSP"): many scheme headings, each with its own labelled dates
        dated = []
        for k, i in enumerate(term_heads):
            end = next((j for j in range(i + 1, min(n, i + 15)) if lines[j].startswith(("# ", "## "))), min(n, i + 15))
            blk = "\n".join(lines[i:end])
            if find_dates(blk) and CLOSE_LABEL.search(blk):
                dated.append((i, i, end))
        if len(dated) >= 5:
            return "listing", dated
        if len(segs) == 1 and page_signals >= 3:
            return "detail", [(segs[0][0], segs[0][0], n)]
        if page_signals >= 3 and (term_heads or name_from_title):
            return "detail", [(term_heads[0], term_heads[0], n)] if term_heads else [(-1, 0, n)]
        return "hub", []

    def _signals(self, lines, lo, hi) -> int:
        block = "\n".join(lines[lo:hi])
        s = 0
        if re.search(r"eligib|who can apply", block, re.I):
            s += 1
        if any(not re.search(r"income", block[max(0, a - 80):b + 40], re.I) for _, a, b, _ in parse_inr_amounts(block)) \
                or re.search(r"tuition fee waiver|full tuition|stipend of", block, re.I):
            s += 1
        if find_dates(block) and CLOSE_LABEL.search(block):
            s += 1
        if re.search(r"how to apply|apply online|application form|documents required|selection (process|procedure)", block, re.I):
            s += 1
        return s

    @staticmethod
    def _title_name(title: str, meta: dict) -> str:
        for src in (meta.get("og:title", ""), title):
            for part in re.split(r"\s+[|–—:-]\s+|\s*\|\s*", src or ""):
                part = clean_name(part)
                if has_scholarship_term(part) and not is_generic_name(part) and 8 <= len(part) <= 140:
                    return part
        return ""

    # ====================================================================== extraction
    def extract(self, page, seg: tuple[int, int, int], use_sections: bool = True, forced_name: Optional[Ev] = None) -> Optional[Candidate]:
        lines = page.text.split("\n")
        name_line, lo, hi = seg
        if forced_name is not None:     # a document linked from a listing entry: the name comes from the listing
            return self._fields(page, lines, -1, lo, hi, forced_name, forced_name.value, use_sections)
        # ---- name
        nm = ""
        if name_line >= 0:
            nm = clean_name(_strip(lines[name_line]))
            name = Ev(nm, [lines[name_line]], "rule:heading")
            if is_generic_name(nm) and lo == name_line and seg[2] == len(lines):
                name_line, nm = -1, ""          # detail page with a junk heading: try the <title>
        if name_line < 0:
            nm = self._title_name(page.title, page.meta)
            if not nm:
                # PDFs: first lines containing a scholarship term
                for i in range(lo, min(hi, lo + 25)):
                    t = clean_name(_strip(lines[i]))
                    if has_scholarship_term(t) and not is_generic_name(t) and len(t) <= 140:
                        nm = t
                        name = Ev(nm, [lines[i]], "rule:first-line")
                        break
                else:
                    return None
            else:
                name = Ev(nm, [nm], "page-meta:title")
                if len(nm.split()) <= 3:   # "Women's Scholarship" -> "Santoor Women's Scholarship" if the page says so
                    rx = re.compile(r"((?:[A-Z][A-Za-z'’&.-]+\s){1,3})(?i:" + re.escape(nm).replace("'", "['’]") + ")")
                    best = {}
                    for i in range(lo, hi):
                        for m in rx.finditer(lines[i]):
                            full = norm_space(m.group(0))
                            if full[0].isupper() and not is_generic_name(full):
                                best.setdefault(full, []).append(lines[i])
                    if best:
                        full, ls = max(best.items(), key=lambda kv: len(kv[1]))
                        nm = clean_name(full)
                        name = Ev(nm, [ls[0]], "rule:title-expanded")
        if not nm or is_generic_name(nm) or not has_scholarship_term(nm):
            return None
        return self._fields(page, lines, name_line, lo, hi, name, nm, use_sections)

    def _fields(self, page, lines, name_line, lo, hi, name: Ev, nm: str, use_sections: bool) -> Optional[Candidate]:
        self._current_name = nm
        block = lines[lo:hi]
        sections = self.find_sections(lines, lo, hi) if use_sections else {}
        cand = Candidate(name=name, line_range=(lo, hi))
        f = cand.fields

        def sec_lines(key) -> list[str]:
            if key not in sections:
                return []
            a, b = sections[key]
            return lines[a + 1:b]

        elig = sec_lines("eligibility")
        elig_scope = elig if elig else block
        # ---- provider
        prov = self._provider(lines, lo, hi, page)
        if prov:
            f["provider"] = prov
        # ---- benefit / amount
        b = self._benefit(sec_lines("benefits"), block)
        if b:
            f["benefit"] = b
        # ---- eligibility (verbatim block)
        if elig:
            body = [l for l in elig if l.strip()][:25]
            f["eligibility_text"] = Ev(" ".join(_strip(l) for l in body)[:1500], ["\n".join(body)], "rule:section")
        # ---- structured criteria
        for key, fn in (("income_criteria", self._income), ("academic_requirement", self._academic),
                        ("age_criteria", self._age), ("domicile_criteria", self._domicile),
                        ("institution_criteria", self._institution), ("disability_criteria", self._disability),
                        ("course", self._course)):
            ev = fn(elig_scope)
            if ev:
                f[key] = ev
        lv = self._levels(elig_scope + [lines[name_line]] if name_line >= 0 else elig_scope, nm)
        if lv:
            f["education_level"] = lv
        g = self._gender(elig_scope, name)
        if g:
            f["gender_criteria"] = g
        c = self._category(elig_scope, name)
        if c:
            f["category_criteria"] = c
        # ---- dates and status
        date_scope = sec_lines("dates") + block
        cd = self._date(date_scope, CLOSE_LABEL, prefer_last=True)
        if cd:
            f["closing_date"] = cd
        od = self._date(date_scope, OPEN_LABEL, prefer_last=False, exclude=cd.parsed if cd else None)
        if od and (not cd or od.parsed <= cd.parsed):
            f["opening_date"] = od
        st = self._status(block)
        if st:
            f["status_statement"] = st
        ay = self._academic_year(block, nm)
        if ay:
            f["academic_year"] = ay
        # ---- lists / processes
        docs = self._list_section(sec_lines("documents"), 15)
        if docs:
            f["documents_required"] = docs
        sel = self._para_section(sec_lines("selection"), 5) or self._grep(block, r"selected on the basis|selection (will|shall) be|shortlisted|merit list will", 2, "rule:selection")
        if sel:
            f["selection_process"] = sel
        ren = self._para_section(sec_lines("renewal"), 5) or self._grep(block, r"\brenew(al|ed|able)?\b", 2, "rule:renewal")
        if ren:
            f["renewal_requirements"] = ren
        # ---- application
        mode = self._app_mode(block)
        if mode:
            f["application_mode"] = mode
        app = self._application_url(page, block)
        if app:
            f["application_url"] = app
        cand.signals = self._signals(lines, lo, hi)
        return cand

    # ---------------------------------------------------------------------- field extractors
    def _provider(self, lines, lo, hi, page) -> Optional[Ev]:
        for i in range(lo, min(hi, lo + 80) - 1):
            if re.fullmatch(r"(#{1,2} )?(department( name)?|ministry|implementing (agency|department)|nodal (agency|department)|"
                            r"provider|offered by|sponsor(ed by)?|administered by)\s*:?", lines[i].strip(), re.I):
                val = _strip(lines[i + 1])
                if 4 <= len(val) <= 120 and not _is_heading(lines[i + 1]) and len(val.split()) >= 2 and val[:1].isupper() \
                        and not SCHOLARSHIP_TERMS.search(val):
                    return Ev(val, [lines[i + 1]], "rule:provider-labelled")
        scope = lines[lo:hi][:200]
        if lo > 0:  # multi-scheme page: provider may be stated at top of page
            scope = scope + lines[:30]
        counts: dict[str, list[str]] = {}
        first_explicit = None
        for l in scope:
            for k, rx in enumerate(ORG_RX):
                for m in rx.finditer(l):
                    org = norm_space(m.group(1)).strip(" .,")
                    org = re.sub(r"\s+(and|of|for|the|&)$", "", org)
                    if GENERIC_ORG.match(org) or len(org) < 6:
                        continue
                    counts.setdefault(org, []).append(l)
                    if k == 0 and first_explicit is None:
                        first_explicit = org
        if first_explicit:
            return Ev(first_explicit, [counts[first_explicit][0]], "rule:provider-explicit")
        if counts:
            name_toks = set(re.findall(r"[A-Z]{3,}", self._current_name or ""))

            def acr(org):
                return "".join(w[0] for w in re.findall(r"[A-Z][a-z]+|[A-Z]{2,}", org) if w not in ("Of", "For", "And", "The"))

            def rank(kv):
                org, ls = kv
                verifier = all(re.search(r"verif|forward|nodal|attest|countersign", l, re.I) for l in ls)
                return (acr(org) not in name_toks, verifier, org.startswith("Government of"), -len(ls))
            best = sorted(counts.items(), key=rank)[0]
            return Ev(best[0], [best[1][0]], "rule:provider-org-mention")
        # scheme named after its body ("AICTE - Pragati ...", "ICAR National Talent ..."): the acronym itself, if the page uses it
        if not counts or all(k.startswith("Government of") for k in counts):
            m = re.match(r"^\s*([A-Z]{3,6})\b", self._current_name or "")
            if m:
                for l in scope:
                    if re.search(r"\b" + m.group(1) + r"\b", l):
                        return Ev(m.group(1), [l], "rule:provider-acronym")
        site = page.meta.get("og:site_name", "")
        if site and len(site) >= 4 and not has_scholarship_term(site):
            return Ev(site, [site], "page-meta:og:site_name")
        return None

    def _benefit(self, sec: list[str], block: list[str]) -> Optional[Ev]:
        picked = []
        amounts = []
        kw = re.compile(r"scholarship|award|per (annum|year|month|semester)|p\.?a\.?\b|p\.?m\.?\b|stipend|fellowship|tuition|"
                        r"fee|grant|reimburs|allowance|assistance|amount|one[- ]time|lump ?sum|maintenance", re.I)
        bad = re.compile(r"income|salary|application fee|registration fee|penalty|fine of|turnover|budget|crore have been|"
                         r"disbursed (so far|till)|total of rs", re.I)
        for scope, need_kw in ((sec, False), (block, True)):
            banded = any(re.search(r"income (limit|ceiling|slab|group|range)", x, re.I) for x in scope)
            for j, l in enumerate(scope):
                if banded and re.match(r"^(- )?([\d.]+\s*)?(up ?to|upto|below|above|less than|more than)\b", l.strip(), re.I):
                    continue          # an income band in a benefits table, not a scholarship amount
                t = _strip(l)
                if bad.search(t) or len(t) > 400:
                    continue
                near = " ".join(scope[max(0, j - 2):j + 2])
                if re.search(r"income", near, re.I) and not re.search(r"scholarship (amount|of)|per (month|annum) (as|for) (scholarship|stipend)", t, re.I):
                    continue
                am = parse_inr_amounts(t)
                if am and (not need_kw or kw.search(t)):
                    picked.append(l)
                    amounts.extend(a[0] for a in am)
                elif not need_kw and re.search(r"tuition fee|full fee|waiver|stipend|allowance|laptop|mentor|coaching", t, re.I) and len(t) > 15:
                    picked.append(l)
                if len(picked) >= 4:
                    break
            if picked:
                break
        if not picked:
            return None
        val = " / ".join(_strip(l) for l in picked)[:600]
        return Ev(val, picked, "rule:benefit", max(amounts) if amounts else None)

    def _grep(self, scope, pattern, k, method, require=None) -> Optional[Ev]:
        rx = re.compile(pattern, re.I)
        hits = []
        for l in scope:
            t = _strip(l)
            if rx.search(t) and len(t) <= 500 and (require is None or require(t)):
                hits.append(l)
                if len(hits) >= k:
                    break
        if not hits:
            return None
        return Ev(" / ".join(_strip(h) for h in hits), hits, method)

    def _income(self, scope) -> Optional[Ev]:
        hits = []
        limits = []
        for l in scope:
            t = _strip(l)
            if re.search(r"income", t, re.I) and len(t) <= 500:
                am = parse_inr_amounts(t)
                if am or re.search(r"income (certificate|limit|ceiling|criteria)|annual (family )?income", t, re.I):
                    hits.append(l)
                    if am and re.search(r"not (exceed|more than)|less than|below|up ?to|upto|maximum|ceiling|limit|within|≤|<|does not", t, re.I):
                        limits.extend(a[0] for a in am)
            if len(hits) >= 3:
                break
        if not hits:
            return None
        return Ev(" / ".join(_strip(h) for h in hits), hits, "rule:income", max(limits) if limits else None)

    def _academic(self, scope):
        def pct(t):
            return bool(re.search(r"(minimum|at least|not less than|above|securing|secured|scored|obtained)[^.]{0,60}"
                                  r"(\d{2}(\.\d+)?\s*(%|percent|per cent)|cgpa|marks|percentile)"
                                  r"|\d{2}(\.\d+)?\s*(%|percent|per cent)[^.]{0,40}(marks|aggregate|cgpa)"
                                  r"|\b(jee|neet|gate|gpat|cat|nmms) (score|rank|qualified|examination)", t, re.I))
        scope = [l for l in scope if not l.startswith(("# ", "## "))]
        ev = self._grep(scope, r".", 2, "rule:academic",
                        require=lambda t: pct(t) and not re.search(r"income|disab|reserv|earmark|scholarships? (are|is|will be) (reserved|earmarked)", t, re.I))
        if ev:
            m = re.search(r"(\d{2}(?:\.\d+)?)\s*(?:%|percent|per cent)", ev.value)
            ev.parsed = float(m.group(1)) if m else None
        return ev

    def _age(self, scope):
        return self._grep(scope, r"\bage\b|years of age|aged\b|born (on or )?(after|before)", 2, "rule:age",
                          require=lambda t: bool(re.search(r"\d", t)))

    def _domicile(self, scope):
        ev = self._grep(scope, r"domicile|resident of|native of|permanent resident|belong(s|ing)? to the state|bonafide resident", 2, "rule:domicile")
        if ev:
            ev.parsed = [s for s in INDIAN_STATES if s.lower() in ev.value.lower()]
        return ev

    def _institution(self, scope):
        return self._grep(scope, r"(aicte|ugc|nmc|pci|nirf|naac)[- ]?(approved|recogni[sz]ed|accredited)|recogni[sz]ed (institution|university|college|board)|"
                                 r"government (college|institution|school)|enrolled (in|at)|admitted (to|in)|admission in|studying in|regular (student|course)|"
                                 r"full[- ]time", 2, "rule:institution")

    def _disability(self, scope):
        return self._grep(scope, r"disab|divyang|\bpwd\b|differently[- ]abled|handicap", 2, "rule:disability")

    def _course(self, scope):
        return self._grep(scope, r"\b(courses?|programmes?|streams?|disciplines?|branch(es)?)\b", 1, "rule:course",
                          require=lambda t: bool(re.search(r"engineering|medical|mbbs|technical|science|arts|commerce|law|nursing|pharmacy|"
                                                           r"management|agricultur|professional|vocational|any", t, re.I)))

    def _levels(self, scope, name) -> Optional[Ev]:
        found = []
        spans = []
        text_lines = list(scope)
        for label, rx in LEVEL_RX:
            for l in text_lines:
                if rx.search(l):
                    found.append(label)
                    if l not in spans:
                        spans.append(l)
                    break
        if not found:
            return None
        # "scored 75% in Class 12" is an academic requirement, not the level the scholarship is for
        higher = {"Diploma/ITI/Polytechnic", "Undergraduate", "Postgraduate", "Doctoral/Research"}
        if higher & set(found):
            prior = re.compile(r"passed|scored|marks|percentage|completed|cleared|qualif|secured|obtained|examination|board|beyond|after (passing )?class|passed out", re.I)
            keep_f, keep_s = [], []
            for label, rx in LEVEL_RX:
                if label not in found:
                    continue
                ls = [l for l in text_lines if rx.search(l)]
                if label in ("Pre-matric (Class 1-10)", "Class 11-12") and all(prior.search(l) for l in ls):
                    continue
                keep_f.append(label)
                keep_s.append(ls[0])
            found, spans = keep_f, list(dict.fromkeys(keep_s))
        return Ev(", ".join(found), spans[:4], "rule:level-keywords", found)

    def _gender(self, scope, name: Ev) -> Optional[Ev]:
        """Female-only only when the source says so; a quota or reservation for girls is not a restriction."""
        female = re.compile(r"\b(girls?|female|women|woman|kanya)\b", re.I)
        both = re.compile(r"boys and girls|girls and boys|male and female|both (genders|boys)|all genders", re.I)
        quota = re.compile(r"reserv|earmark|\d+\s*%|percent|quota|preference|priority|in the absence|single girl child|slots|seats", re.I)
        restrict = re.compile(r"\b(only|exclusively|meant for|open (only )?for|eligible|applicants? must be|for (meritorious |deserving )?(girl|women|female))\b", re.I)
        nm = name.value
        if female.search(nm) and not both.search(nm):
            return Ev("Female students only", [name.spans[0]], "rule:gender(name)", "female")
        for l in scope:
            t = _strip(l)
            if l.startswith(("# ", "## ")):
                continue
            if both.search(t):
                return None
            if female.search(t) and restrict.search(t) and not quota.search(t):
                return Ev("Female students only", [l], "rule:gender", "female")
        return None

    def _category(self, scope, name: Ev) -> Optional[Ev]:
        """Categories named in the scheme name or in eligibility lines (not in document checklists or quota notes)."""
        found, spans = [], []
        skip = re.compile(r"certificate|reserv|earmark|\d+\s*%|quota|slots|seats|proportion|ratio|roster", re.I)
        lines = [name.spans[0]] + [l for l in scope if not l.startswith(("# ", "## "))]
        for l in lines:
            t = _strip(l)
            if l is not name.spans[0] and (skip.search(t) or re.search(r"allocation|fund|budget|norms|niti|sub-?plan", t, re.I)
                                           or not re.search(r"belong|students?|candidates?|applicants?|eligib|communit|caste", t, re.I)):
                continue
            for label, rx in CATEGORY_RX:
                if label not in found and rx.search(t):
                    found.append(label)
                    if l not in spans:
                        spans.append(l)
        if not found:
            return None
        return Ev(", ".join(found), spans[:4], "rule:category", found)

    def _date(self, scope, label_rx, prefer_last: bool, exclude=None) -> Optional[Ev]:
        best = None
        for i, l in enumerate(scope):
            t = _strip(l)
            lm = label_rx.search(t)
            if not lm:
                continue
            ds = find_dates(t)
            line_used = l
            if not ds and i + 1 < len(scope):
                ds = find_dates(_strip(scope[i + 1]))
                line_used = l + "\n" + scope[i + 1]
                if ds:
                    ds = [(d, a, b, s) for d, a, b, s in ds]
            if not ds:
                continue
            if NON_STUDENT_DEADLINE.search(t) and not re.search(r"student|candidate|applicant|online application", t, re.I):
                continue
            strength = 1
            lab = lm.group(0).lower()
            if lab in ("till", "until", "upto", "up to", "from"):
                strength = 0
            if re.search(r"last date|closing date|deadline|opening date|start date|commence", lab):
                strength = 2
            if re.search(r"student|application|apply|submission|registration|fresh|candidate", t, re.I):
                strength += 1
            if re.search(r"extended", t, re.I):
                strength += 2
            # choose the date nearest after the label
            after = [d for d in ds if d[1] >= lm.start()] or ds
            if label_rx is OPEN_LABEL and len(ds) >= 2 and re.search(r"\bfrom\b.*\b(to|till|until)\b", t, re.I):
                d = ds[0]
            else:
                d = after[0]
            if exclude and d[0] == exclude:
                continue
            if strength < 1:
                continue                      # bare "till"/"up to" without an application context is too weak
            if re.search(r"(dated|dt\.?|w\.e\.f\.?)\s*:?\s*$", t[:d[1]], re.I):
                continue                      # "GR dated 8 July 2024" is a document date, not a deadline
            if d[0].year < 2015:
                continue
            key = (strength, -i)
            if best is None or key > best[0]:
                best = (key, d, line_used)
        if not best:
            return None
        d = best[1][0]
        spans = best[2].split("\n") if "\n" in best[2] else [best[2]]
        return Ev(d.isoformat(), spans, "rule:date-label", d)

    def _status(self, block) -> Optional[Ev]:
        for l in block:
            t = _strip(l)
            m = STATUS_RX.search(t)
            if m and len(t) < 400:
                return Ev(t[:300], [l], "rule:status-statement", m.group(0).lower())
        return None

    def _academic_year(self, block, name) -> Optional[Ev]:
        ys = academic_years(name)
        if ys:
            return Ev(ys[0], [name], "rule:academic-year")
        best = None
        for l in block[:120]:      # the most recent cycle the page talks about
            ys = academic_years(l)
            if ys and re.search(r"scholarship|session|academic|year|fellowship|scheme|applications?|program|batch|cycle", l, re.I):
                y = max(ys)
                if best is None or y > best[0]:
                    best = (y, l)
        return Ev(best[0], [best[1]], "rule:academic-year") if best else None

    def _list_section(self, sec, k) -> Optional[Ev]:
        items = [l for l in sec if l.startswith("- ") or re.match(r"^\(?([0-9]{1,2}|[a-z]|[ivx]+)[.)]\s", l)]
        if not items:
            items = [l for l in sec if 4 < len(l) < 250][:k]
        items = items[:k]
        if not items:
            return None
        return Ev("; ".join(re.sub(r"^\(?([0-9]{1,2}|[a-z]|[ivx]+)[.)]\s", "", _strip(i)) for i in items)[:1500], items, "rule:list-section")

    def _para_section(self, sec, k) -> Optional[Ev]:
        body = [l for l in sec if len(_strip(l)) > 3][:k]
        if not body:
            return None
        return Ev(" ".join(_strip(l) for l in body)[:1000], body, "rule:section")

    def _app_mode(self, block) -> Optional[Ev]:
        for l in block:
            t = _strip(l)
            if re.search(r"apply online|online application|applications? (must|should|can|are to) be (submitted|made) online|through (the )?(online )?(portal|nsp|website)|online mode", t, re.I):
                return Ev("Online", [l], "rule:application-mode", "online")
        for l in block:
            t = _strip(l)
            if re.search(r"offline|by (speed |registered )?post|hard ?copy of the application|send (the |your )?(completed )?(application|form)", t, re.I):
                return Ev("Offline", [l], "rule:application-mode", "offline")
        return None

    def _application_url(self, page, block) -> Optional[Ev]:
        good = re.compile(r"\bapply\b|application form|online application|register|registration|apply now|apply here|login to apply|"
                          r"submit (your )?application|fresh application|student login|new registration", re.I)
        bad = re.compile(r"guideline|faq|status|result|track|helpdesk|grievance|how to apply|eligib|merit list|selected|"
                         r"notice|circular|renewal list|institute (login|registration)|nodal|verification", re.I)
        best = None
        block_text = "\n".join(block)
        from .classifier import registrable as _reg
        page_dom = _reg(page.final_url or page.url)
        platform = re.compile(r"buddy4study|vidyasaarathi|scholarships\.gov\.in|gov\.in|nic\.in|ac\.in|forms\.gle|docs\.google|alchemer|"
                              r"surveymonkey|typeform|zohopublic|microsoft|jotform", re.I)
        topical = re.compile(r"scholar|fellow|scheme|student|applicant|award|programme|program|kanya|vidyadhan|register", re.I)
        for ln in page.links:
            if _reg(ln.url) != page_dom and not platform.search(ln.url) and not topical.search(ln.url):
                continue          # off-site link that is neither an application platform nor scholarship-related (e.g. a jobs board)
            t = ln.text or ""
            if not t or not good.search(t) or bad.search(t):
                continue
            if ln.url.startswith(("mailto:", "tel:")):
                continue
            sc = 2 + (1 if re.search(r"apply (now|online|here)|register now|online application", t, re.I) else 0) \
                 + (1 if t in block_text else 0) + (1 if re.search(r"apply|register|application|login|signup", ln.url, re.I) else 0)
            if best is None or sc > best[0]:
                best = (sc, ln)
        if not best:
            return None
        ln = best[1]
        return Ev(ln.url, [f'link text "{ln.text}" -> {ln.url}'], "rule:link-anchor", ln.text)
