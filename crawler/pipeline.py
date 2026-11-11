"""Discover -> Crawl -> Extract -> Verify -> Score -> Store -> Update."""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from .classifier import SourceClassifier, host_of, registrable
from .db import DB, now_iso
from .discovery import Frontier
from .extractor import NOT_SPECIFIED, Ev, Extractor
from .fetcher import Fetcher, Page, canonical
from .llm import LLMFiller
from .textutil import name_key, norm_for_match, sha1
from .verifier import ScoreInput, lifecycle, score, verification_status, verify_candidate

log = logging.getLogger("pipeline")

AUTH_RANK = {"PRIMARY_OFFICIAL": 3, "SECONDARY_OFFICIAL": 2, "UNVERIFIED": 1, "AGGREGATOR": 0}
TEXT_FIELDS = ["provider", "benefit", "education_level", "course", "academic_requirement", "income_criteria", "age_criteria",
               "gender_criteria", "category_criteria", "domicile_criteria", "institution_criteria", "disability_criteria",
               "eligibility_text", "application_mode", "documents_required", "selection_process", "renewal_requirements",
               "academic_year", "status_statement"]
TRACKED = ["benefit", "amount_inr_max", "closing_date", "opening_date", "income_limit_inr", "education_level",
           "gender_criteria", "category_criteria", "application_url", "official_source_url", "academic_year",
           "status_statement", "lifecycle_status", "verification_status"]
FIELD_FOR_EVIDENCE = {"amount_inr_max": "benefit", "income_limit_inr": "income_criteria", "official_source_url": "name"}


DOC_ANCHOR = re.compile(r"^(scheme )?(specifications?|guidelines?|scheme details|details|view details|more details|read more|know more)$", re.I)
# gender / category are taken from the listing entry's own name, never from a (possibly multi-scheme) guideline PDF.
# When one document is linked from several listing entries it describes several schemes, so only scheme-neutral
# fields may be taken from it.
SHARED_DOC_FILL = ["provider", "documents_required", "application_mode"]
DOC_FILL = ["provider", "benefit", "eligibility_text", "income_criteria", "academic_requirement", "age_criteria", "domicile_criteria", "institution_criteria", "disability_criteria", "course", "education_level",
            "documents_required", "selection_process", "renewal_requirements", "application_mode"]
NOT_FOR_INDIANS = re.compile(r"\b(u\.\s?s\.? citizens?|us citizens?|citizens? of the united states|for u-s-citizen|uk nationals?|"
                             r"eu citizens?|german nationals?)\b", re.I)
FOR_INDIANS = re.compile(r"\b(indian (citizens?|nationals?|students)|citizens? of india|resident of india|domicile)\b", re.I)


@dataclass
class Obs:
    key: str
    cand: object
    page: Page
    snapshot_id: int
    source_type: str
    authority: str
    via: str
    second_pass: dict
    dropped: list = field(default_factory=list)
    role: str = "page"                  # page | document (a specification linked from a listing entry)


class Pipeline:
    def __init__(self, cfg, archive: str | None = None, max_pages: int | None = None, today: date | None = None):
        self.cfg = cfg
        self.db = DB(cfg.db_path)
        self.archive = archive
        self.as_of = datetime.strptime(archive[:8], "%Y%m%d").date() if archive else (today or date.today())
        self.max_pages = max_pages or cfg.crawl("max_pages_per_run", 400)
        self.cls = SourceClassifier(cfg.domains)
        self.fetcher = Fetcher(cfg, archive_timestamp=archive)
        self.extractor = Extractor(self.as_of)
        self.frontier = Frontier(cfg, self.cls)
        self.llm = LLMFiller(cfg) if not archive else None
        self.fetched: dict[str, Page] = {}
        self.app_status: dict[str, int] = {}
        self.stats = defaultdict(int)
        self.domain_pages: dict[str, int] = {}
        self.doc_for: dict[str, list] = defaultdict(list)   # linked document URL -> [(key, name Ev, listing url)]
        self.domain_yield: dict[str, int] = {}

    # ================================================================== run
    def run(self) -> int:
        mode = f"archive:{self.archive}" if self.archive else "live"
        self.run_id = self.db.start_run(mode)
        log.info("Run %s started (%s, as-of %s, budget %s pages)", self.run_id, mode, self.as_of, self.max_pages)
        self._seed_frontier()
        obs: list[Obs] = []
        while self.stats["pages_fetched"] + self.stats["pages_failed"] < self.max_pages:
            it = self.frontier.pop()
            if it is None:
                break
            obs.extend(self._process(it))
            if (self.stats["pages_fetched"] + self.stats["pages_failed"]) % 25 == 0:
                log.info("  fetched=%s failed=%s frontier=%s candidates=%s", self.stats["pages_fetched"],
                         self.stats["pages_failed"], len(self.frontier), len(obs))
                self.db.commit()
        self.db.commit()
        touched = self._merge_and_store(obs)
        self._handle_unobserved(touched)
        self.db.finish_run(self.run_id, pages_fetched=self.stats["pages_fetched"], pages_failed=self.stats["pages_failed"],
                           candidates=len(obs), new_records=self.stats["new"], changed_records=self.stats["changed"],
                           unchanged_records=self.stats["unchanged"], stale_records=self.stats["stale"],
                           notes=json.dumps({"search_results": self.stats["search_results"], "sitemap_urls": self.stats["sitemap_urls"],
                                             "llm": bool(self.llm and self.llm.enabled)}))
        self.fetcher.close()
        log.info("Run %s done: %s", self.run_id, dict(self.stats))
        return self.run_id

    # ================================================================== frontier
    def _seed_frontier(self):
        # 1) re-verify everything we already know (highest priority)
        for r in self.db.q("SELECT official_source_url FROM scholarships WHERE official_source_url IS NOT NULL"):
            self.frontier.push(r["official_source_url"], 100, 0, "reverify")
        for r in self.db.q("SELECT DISTINCT url FROM scholarship_sources WHERE role='corroborating'"):
            self.frontier.push(r["url"], 40, 1, "reverify")
        # 2) configured entry points
        for s in self.cfg.seeds:
            self.frontier.push(s["url"], 10, 0, "seed", hint=s.get("hint", ""))
            self.db.upsert_source(canonical(s["url"]), host_of(s["url"]), discovered_via="seed", depth=0)
        if self.archive:
            return  # search / sitemaps would hit the live web
        # 3) sitemaps of official seed domains
        if self.cfg.discovery("use_sitemaps", True):
            done = set()
            for s in self.cfg.seeds:
                dom = registrable(s["url"])
                if dom in done or self.cls.domain_class(s["url"]) == "aggregator":
                    continue
                done.add(dom)
                for u in self.fetcher_sitemaps(s["url"]):
                    if self.frontier.push(u, 6, 1, "sitemap", s["url"], s.get("hint", "")):
                        self.stats["sitemap_urls"] += 1
                        self.db.upsert_source(u, host_of(u), discovered_via="sitemap", discovered_from=s["url"], depth=1)
        # 4) web search (free HTML endpoint)
        if self.cfg.discovery("use_search", True):
            k = self.cfg.discovery("max_search_results_per_query", 10)
            for q in self.cfg.discovery("search_queries", []):
                for u, title in self.frontier.search(self.fetcher, q, k):
                    prio = 7 if self.cls.domain_class(u) != "aggregator" else 4
                    if self.frontier.push(u, prio, 1, "search", q):
                        self.stats["search_results"] += 1
                        self.db.upsert_source(canonical(u), host_of(u), discovered_via="search", discovered_from=q, depth=1)

    def fetcher_sitemaps(self, url):
        try:
            return self.frontier.sitemap_urls(self.fetcher, url)
        except Exception:
            return []

    # ================================================================== per page
    def _process(self, it) -> list[Obs]:
        page = self.fetcher.fetch(it.url)
        self.fetched[it.url] = page
        if page.final_url != it.url:
            self.fetched.setdefault(page.final_url, page)
        dom = host_of(it.url)
        self.db.upsert_source(it.url, dom, discovered_via=it.via, discovered_from=it.parent, depth=it.depth,
                              last_fetched=now_iso(), last_http_status=page.status, last_error=page.error or None,
                              content_hash=page.content_hash if page.ok else None)
        if not page.ok:
            self.stats["pages_failed"] += 1
            self.db.upsert_source(it.url, dom, page_kind="error")
            log.debug("fail %s %s", it.url, page.error)
            return []
        self.stats["pages_fetched"] += 1
        snap = self.db.snapshot(page, self.run_id)

        if self.cls.is_aggregator(it.url):
            n = self.frontier.expand(page, it)
            self.db.upsert_source(it.url, dom, page_kind="aggregator", source_type="Aggregator", authority="AGGREGATOR")
            log.debug("aggregator %s -> %s official-candidate links", it.url, n)
            return []

        lines = page.text.split("\n")
        if it.url in self.doc_for:
            return self._process_document(it, page, snap)
        kind, segs = self.extractor.segment(lines, page.title, page.meta)
        out: list[Obs] = []
        seg_docs = self._segment_documents(lines, page, segs) if kind in ("multi", "listing") else {}
        for seg in segs:
            cand = self.extractor.extract(page, seg)
            if cand is None:
                continue
            if self.llm:
                self.llm.fill(cand, page.text)
            verified, dropped = verify_candidate(cand, page)
            if "name" not in verified:
                continue
            cand.fields = {k: v for k, v in verified.items() if k != "name"}
            if cand.signals < 2 and len(cand.fields) < 4 and not (kind == "listing" and "closing_date" in cand.fields):
                continue
            scope = "\n".join(lines[seg[1]:seg[2]])[:4000] + " " + page.final_url
            if NOT_FOR_INDIANS.search(scope) and not FOR_INDIANS.search(scope):
                continue                  # e.g. Fulbright programmes for U.S. citizens: not open to Indian students
            prov = cand.fields.get("provider").value if cand.fields.get("provider") else None
            stype, auth = self.cls.classify(page.final_url, prov, page.text, it.hint, cand.name.value)
            p2 = self.extractor.extract(page, seg, use_sections=False)
            second = {"closing_date": p2.fields["closing_date"].value if p2 and "closing_date" in p2.fields else None,
                      "amount": p2.fields["benefit"].parsed if p2 and "benefit" in p2.fields else None}
            key = name_key(cand.name.value)
            if len(key.split()) <= 2:
                key += "@" + registrable(page.final_url)
            out.append(Obs(key, cand, page, snap, stype, auth, it.via, second, dropped))
            doc = seg_docs.get(seg)
            if doc and doc != page.final_url:
                self.doc_for[doc].append((key, cand.name, page.final_url, it.hint))
                self.frontier.seen.discard(doc)
                self.frontier.push(doc, 60, it.depth + 1, "spec-doc", page.url)
            if auth != "PRIMARY_OFFICIAL" and prov:
                self._chase_provider(page, prov, it)
        self.db.upsert_source(it.url, dom, page_kind=kind if (kind != "hub" or not out) else "detail",
                              source_type=out[0].source_type if out else None, authority=out[0].authority if out else None)
        reg = registrable(it.url)
        self.domain_pages[reg] = self.domain_pages.get(reg, 0) + 1
        if out:
            self.domain_yield[reg] = self.domain_yield.get(reg, 0) + len(out)
        elif self.domain_pages[reg] >= 15 and not self.domain_yield.get(reg):
            self.frontier.barren.add(reg)
        self.frontier.expand(page, it, boost=2.0 if out else 0.0)
        return out

    def _segment_documents(self, lines, page, segs) -> dict:
        """Map each listing entry to the document it links to ('Specifications', 'Guidelines', 'Read more' ...).
        Anchor lines in the text and anchor links in the HTML appear in the same order, so the k-th
        'Specifications' line belongs to the k-th 'Specifications' link."""
        out = {}
        by_text: dict[str, list] = defaultdict(list)
        for ln in page.links:
            t = (ln.text or "").strip()
            if DOC_ANCHOR.match(t):
                by_text[t.lower()].append(ln.url)
        for t, urls in by_text.items():
            rx = re.compile(r"(^|[\s|-])" + re.escape(t) + r"($|[\s|])", re.I)
            pos = [i for i, l in enumerate(lines) if len(l) < 120 and rx.search(l.strip())]
            if len(pos) != len(urls):
                continue                      # cannot align safely
            for seg in segs:
                if seg in out:
                    continue
                for i, u in zip(pos, urls):
                    if seg[1] <= i < seg[2] and registrable(u) == registrable(page.final_url) and not u.endswith("/null"):
                        out[seg] = u
                        break
        return out

    def _process_document(self, it, page, snap) -> list[Obs]:
        """A specification/guideline document linked from a listing entry: extract fields for that entry."""
        out = []
        n = len(page.text.split("\n"))
        for key, name_ev, listing_url, hint in self.doc_for[it.url]:
            cand = self.extractor.extract(page, (-1, 0, n), forced_name=name_ev)
            if cand is None:
                continue
            verified, dropped = verify_candidate(cand, page)
            allowed = SHARED_DOC_FILL if len({k for k, *_ in self.doc_for[it.url]}) > 1 else DOC_FILL
            cand.fields = {k: v for k, v in verified.items() if k != "name" and k in allowed}
            for ev in cand.fields.values():
                ev.src = (page, snap)
            prov = cand.fields.get("provider").value if cand.fields.get("provider") else None
            stype, auth = self.cls.classify(page.final_url, prov, page.text, hint, name_ev.value)
            out.append(Obs(key, cand, page, snap, stype, auth, "spec-doc", {}, dropped, role="document"))
        self.db.upsert_source(it.url, host_of(it.url), page_kind="document", authority=out[0].authority if out else None)
        return out

    def _chase_provider(self, page, provider, it):
        """On a secondary page, look for a link to the provider's own site and queue it."""
        toks = self.cls.provider_tokens(provider)
        for ln in page.links:
            if self.cls.is_aggregator(ln.url):
                continue
            t = (ln.text or "").lower()
            if (toks and toks[0] in host_of(ln.url).replace("-", "")) or re.search(r"official (web)?site|visit website", t):
                self.frontier.push(ln.url, 9, it.depth + 1, "provider-chase", page.url)

    # ================================================================== merge + persist
    def _merge_and_store(self, obs: list[Obs]) -> set[str]:
        groups: dict[str, list[Obs]] = defaultdict(list)
        for o in obs:
            groups[o.key].append(o)
        touched = set()
        for key, group in groups.items():
            def rank(o: Obs):
                ay = o.cand.fields.get("academic_year")
                return (AUTH_RANK[o.authority], int(ay.value[:4]) if ay else 0, len(o.cand.fields), o.cand.signals)
            docs = [o for o in group if o.role == "document"]
            group = [o for o in group if o.role != "document"]
            if not group:
                continue
            group.sort(key=rank, reverse=True)
            prim = group[0]
            if prim.authority == "AGGREGATOR":
                continue
            for d in docs:
                if d.authority not in ("PRIMARY_OFFICIAL", "SECONDARY_OFFICIAL"):
                    continue
                for fname, ev in d.cand.fields.items():
                    if fname not in prim.cand.fields:
                        prim.cand.fields[fname] = ev
                group.append(d)
            seen_urls = {prim.page.final_url}
            corr = []
            for o in group[1:]:
                if o.role == "document":
                    if o.page.final_url not in seen_urls:
                        seen_urls.add(o.page.final_url)
                        corr.append({"url": o.page.final_url, "authority": o.authority, "snapshot_id": o.snapshot_id,
                                     "closing_date": None, "amount": None, "academic_year": None, "independent": False,
                                     "role": "specification"})
                    continue
                if o.page.final_url in seen_urls:
                    continue
                seen_urls.add(o.page.final_url)
                corr.append({"url": o.page.final_url, "authority": o.authority, "snapshot_id": o.snapshot_id,
                             "closing_date": o.cand.fields["closing_date"].value if "closing_date" in o.cand.fields else None,
                             "amount": o.cand.fields["benefit"].parsed if "benefit" in o.cand.fields else None,
                             "academic_year": o.cand.fields["academic_year"].value if "academic_year" in o.cand.fields else None,
                             "independent": registrable(o.page.final_url) != registrable(prim.page.final_url)})
            sid = self._store(prim, corr)
            touched.add(sid)
        self.db.commit()
        return touched

    def _check_app(self, url: str) -> int | None:
        if self.archive:
            return None
        if url not in self.app_status:
            self.app_status[url] = self.fetcher.head_ok(url)
        return self.app_status[url]

    def _store(self, o: Obs, corr: list[dict]) -> str:
        f = o.cand.fields
        page = o.page
        url = page.final_url
        nk = name_key(o.cand.name.value)
        existing = self.db.find_existing(nk, registrable(url))
        sid = existing["id"] if existing else sha1(o.key)[:16]
        app = f.get("application_url")
        app_status = self._check_app(app.value) if app else None
        si = ScoreInput(authority=o.authority, http_ok=page.ok, archived=bool(self.archive), page_text=page.text,
                        page_title=page.title, name=o.cand.name.value, fields=f, app_url_status=app_status,
                        second_pass=o.second_pass, corroborations=corr, today=self.as_of,
                        ay_start_month=self.cfg.verification("current_academic_year_start_month", 6))
        conf, checks, conflicts = score(si)
        live_ok = page.ok and not self.archive
        vstat, vreason = verification_status(conf, o.authority, conflicts, live_ok, self.cfg.verification("verified_threshold", 95.0))
        life, life_reason = lifecycle(f, self.as_of, self.cfg.verification("expiring_soon_days", 30),
                                      self.cfg.verification("current_academic_year_start_month", 6))
        checks.append({"check": "Verification gates", "points": 0, "max": 0, "detail": vreason})

        rec = {"id": sid, "name": o.cand.name.value, "name_key": nk, "provider_domain": registrable(url),
               "source_type": o.source_type, "official_source_url": url,
               "application_url": app.value if app else None}
        for t in TEXT_FIELDS:
            rec[t] = f[t].value if t in f else NOT_SPECIFIED
        rec["amount_inr_max"] = f["benefit"].parsed if "benefit" in f else None
        rec["income_limit_inr"] = f["income_criteria"].parsed if "income_criteria" in f else None
        rec["opening_date"] = f["opening_date"].value if "opening_date" in f else None
        rec["closing_date"] = f["closing_date"].value if "closing_date" in f else None
        rec["eligibility_rules"] = json.dumps(self._rules(f), ensure_ascii=False)
        rec.update({"lifecycle_status": life, "status_reason": life_reason, "verification_status": vstat,
                    "confidence": conf, "score_breakdown": json.dumps(checks, ensure_ascii=False),
                    "last_seen": now_iso(), "consecutive_failures": 0, "current_snapshot_id": o.snapshot_id})
        rec["last_verified"] = (self.as_of.isoformat() if self.archive else now_iso()) if page.ok else (existing["last_verified"] if existing else None)
        if existing is None:
            rec.update({"first_seen": now_iso(), "discovered_via": o.via, "last_changed": now_iso()})

        # ---------------- change detection (never silently overwrite)
        new_ev_rows = self._evidence_rows(o)
        old_ev = self.db.current_evidence(sid) if existing is not None else {}
        if existing is None:
            rec["last_changed"] = now_iso()
        self.db.upsert_scholarship(rec)
        if existing is None:
            self.stats["new"] += 1
            self.db.log_change(scholarship_id=sid, run_id=self.run_id, change_type="NEW", field=None, old_value=None,
                               new_value=rec["name"], source_url=url, new_evidence=o.cand.name.spans[0][:500],
                               new_snapshot_id=o.snapshot_id)
        else:
            changed = False
            for col in TRACKED:
                ov, nv = existing[col], rec[col]
                if self._same(ov, nv):
                    continue
                changed = True
                ef = FIELD_FOR_EVIDENCE.get(col, col)
                oe = "\n".join(r["evidence_text"] for r in old_ev.get(ef, []))[:800] or None
                ne = "\n".join(r["evidence_text"] for r in new_ev_rows if r["field"] == ef)[:800] or None
                empty = lambda v: v is None or str(v) in ("", "Not specified")
                if col in ("lifecycle_status", "verification_status"):
                    ctype = "STATUS_CHANGED"
                elif empty(ov):
                    ctype = "FIELD_ADDED"        # value newly found (e.g. a linked document was read this run)
                elif empty(nv):
                    ctype = "FIELD_REMOVED"      # source no longer states it
                else:
                    ctype = "FIELD_CHANGED"
                if col == "official_source_url" and corr and any(c["url"] == ov for c in corr):
                    ctype = "CONTRADICTED"
                self.db.log_change(scholarship_id=sid, run_id=self.run_id, change_type=ctype, field=col,
                                   old_value=None if ov is None else str(ov), new_value=None if nv is None else str(nv),
                                   source_url=url, old_evidence=oe, new_evidence=ne,
                                   old_snapshot_id=existing["current_snapshot_id"], new_snapshot_id=o.snapshot_id)
            if rec["lifecycle_status"] == "EXPIRED" and existing["lifecycle_status"] != "EXPIRED":
                self.stats["stale"] += 1
            if changed:
                self.db.x("UPDATE scholarships SET last_changed=? WHERE id=?", (now_iso(), sid))
                self.stats["changed"] += 1
            else:
                self.stats["unchanged"] += 1
        self.db.replace_evidence(sid, new_ev_rows, self.run_id)
        self.db.record_checks(sid, self.run_id, checks)
        self.db.link_source(sid, url, o.authority, "primary", o.snapshot_id, rec["closing_date"], rec["amount_inr_max"])
        for c in corr:
            self.db.link_source(sid, c["url"], c["authority"], c.get("role", "corroborating"), c["snapshot_id"], c["closing_date"], c["amount"])
        return sid

    @staticmethod
    def _same(a, b) -> bool:
        if a is None and b is None:
            return True
        return norm_for_match(str(a)) == norm_for_match(str(b))

    def _evidence_rows(self, o: Obs) -> list[dict]:
        rows = []
        for fname, ev in [("name", o.cand.name)] + list(o.cand.fields.items()):
            pg, snap = ev.src if ev.src else (o.page, o.snapshot_id)
            txt = pg.text
            for sp in ev.spans:
                pos = txt.find(sp)
                rows.append({"field": fname, "value": ev.value[:1500], "evidence_text": sp[:1500],
                             "source_url": pg.final_url, "snapshot_id": snap,
                             "char_start": pos if pos >= 0 else None, "char_end": pos + len(sp) if pos >= 0 else None,
                             "method": ev.method})
        return rows

    @staticmethod
    def _rules(f: dict) -> dict:
        """Machine-readable eligibility built ONLY from evidenced fields (absent = not stated by the source)."""
        r = {}
        if "income_criteria" in f and f["income_criteria"].parsed:
            r["max_annual_family_income_inr"] = f["income_criteria"].parsed
        if "academic_requirement" in f and f["academic_requirement"].parsed:
            r["min_percentage"] = f["academic_requirement"].parsed
        if "gender_criteria" in f:
            r["gender"] = f["gender_criteria"].parsed
        if "category_criteria" in f:
            r["categories"] = f["category_criteria"].parsed
        if "education_level" in f:
            r["education_levels"] = f["education_level"].parsed
        if "domicile_criteria" in f and f["domicile_criteria"].parsed:
            r["domicile_states"] = f["domicile_criteria"].parsed
        if "disability_criteria" in f:
            r["disability_mentioned"] = True
        return r

    # ================================================================== stale detection
    def _handle_unobserved(self, touched: set[str]):
        thr = self.cfg.verification("unreachable_runs_before_stale", 2)
        for r in self.db.q("SELECT * FROM scholarships"):
            if r["id"] in touched:
                continue
            url = r["official_source_url"]
            pg = self.fetched.get(url)
            upd: dict = {}
            change = None
            if pg is None:
                # not visited this run: only time-based re-evaluation
                if r["closing_date"] and r["lifecycle_status"] in ("ACTIVE", "EXPIRING_SOON"):
                    d = date.fromisoformat(r["closing_date"])
                    if d < self.as_of:
                        upd = {"lifecycle_status": "EXPIRED", "status_reason": f"Closing date {d} has passed"}
            elif not pg.ok:
                fails = (r["consecutive_failures"] or 0) + 1
                upd = {"consecutive_failures": fails, "verification_status": "REVIEW_REQUIRED"}
                if fails >= thr:
                    upd.update({"lifecycle_status": "NO_LONGER_VERIFIABLE",
                                "status_reason": f"Official source unreachable for {fails} consecutive runs ({pg.error})"})
                    change = ("SOURCE_UNREACHABLE", pg.error)
                else:
                    upd["status_reason"] = f"Could not re-verify in run {self.run_id}: {pg.error}"
                    change = ("SOURCE_UNREACHABLE", pg.error)
            else:
                hay = norm_for_match(pg.title + "\n" + pg.text)
                toks = r["name_key"].split()
                present = sum(t in hay for t in toks) / max(1, len(toks))
                if present < 0.6:
                    upd = {"lifecycle_status": "NO_LONGER_VERIFIABLE", "verification_status": "REVIEW_REQUIRED",
                           "status_reason": "Scholarship no longer appears on its official source page"}
                    change = ("REMOVED_FROM_SOURCE", f"{present:.0%} of name tokens still on page")
                else:
                    upd = {"verification_status": "REVIEW_REQUIRED",
                           "status_reason": "Page still mentions the scholarship but its details could not be re-extracted"}
            if not upd:
                continue
            for col in ("lifecycle_status", "verification_status"):
                if col in upd and upd[col] != r[col]:
                    self.db.log_change(scholarship_id=r["id"], run_id=self.run_id, change_type="STATUS_CHANGED", field=col,
                                       old_value=r[col], new_value=upd[col], source_url=url,
                                       new_evidence=upd.get("status_reason"), old_snapshot_id=r["current_snapshot_id"])
            if change:
                self.db.log_change(scholarship_id=r["id"], run_id=self.run_id, change_type=change[0], field="official_source_url",
                                   old_value=url, new_value=None, source_url=url, new_evidence=change[1],
                                   old_snapshot_id=r["current_snapshot_id"])
            if upd.get("lifecycle_status") in ("NO_LONGER_VERIFIABLE", "EXPIRED") and r["lifecycle_status"] != upd["lifecycle_status"]:
                self.stats["stale"] += 1
            if any(upd.get(c) not in (None, r[c]) for c in ("lifecycle_status", "verification_status")) or change:
                upd["last_changed"] = now_iso()
            self.db.x(f"UPDATE scholarships SET {', '.join(f'{k}=?' for k in upd)} WHERE id=?", (*upd.values(), r["id"]))
        self.db.commit()
