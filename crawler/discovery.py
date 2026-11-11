"""Discovery: a prioritised frontier fed by four channels.

  seed      hub pages from config (entry points only)
  link      links found on fetched pages, scored by anchor/URL relevance
  sitemap   sitemap.xml of official domains, filtered for scholarship URLs
  search    DuckDuckGo HTML results for configured queries (free, no API key)

Aggregator pages are crawled only to mine their *outbound* links to official sites.
"""
from __future__ import annotations

import heapq
import itertools
import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from .classifier import SourceClassifier, host_of, registrable
from .fetcher import canonical
from .textutil import has_scholarship_term

log = logging.getLogger("discovery")

POS = re.compile(r"scholar|fellowship|chhatravritti|chatravritti|vidyadhan|stipend|merit[- ]cum|post[- ]?matric|pre[- ]?matric|"
                 r"financial[- ]assistance|student[- ]?(support|welfare|development)|pragati|saksham|inspire|yasasvi|"
                 r"scheme", re.I)
HELP = re.compile(r"apply|eligib|guideline|notification|advertisement|announcement|detail|read more|know more|official website|"
                  r"visit (the )?(website|portal)|click here", re.I)
NEG = re.compile(r"tender|recruit|vacanc|career|\bjobs?\b|gallery|photo|video|press[- ]release|\brti\b|log-?in|sign-?in|sitemap|"
                 r"privacy|disclaimer|terms|feedback|contact|faq|annual[- ]report|budget|hindi|/hi/|lang=hi|\.(jpg|jpeg|png|gif|zip|docx?|xlsx?|pptx?)$|"
                 r"facebook|twitter|instagram|youtube|linkedin|whatsapp|share|print|accessibility|screen[- ]reader|"
                 r"result|merit[- ]?list|selected[- ]candidates|corrigendum|minutes|utili[sz]ation", re.I)
SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "whatsapp:", "data:")


@dataclass(order=True)
class Item:
    prio: float
    seq: int
    url: str = ""
    depth: int = 0
    via: str = ""
    parent: str = ""
    hint: str = ""


class Frontier:
    def __init__(self, cfg, classifier: SourceClassifier):
        self.cfg = cfg
        self.cls = classifier
        self.heap: list[Item] = []
        self.seen: set[str] = set()
        self.per_domain: dict[str, int] = {}
        self.counter = itertools.count()
        self.max_depth = cfg.crawl("max_depth", 3)
        self.max_per_domain = cfg.crawl("max_pages_per_domain", 40)
        self.hints: dict[str, str] = {}
        self.barren: set[str] = set()            # domains that used >= N pages without yielding a candidate

    def push(self, url, prio, depth, via, parent="", hint=""):
        url = canonical(url)
        if not url.startswith("http") or url in self.seen:
            return False
        if depth > self.max_depth:
            return False
        self.seen.add(url)
        if hint:
            self.hints[registrable(url)] = hint
        heapq.heappush(self.heap, Item(-prio, next(self.counter), url, depth, via, parent, hint or self.hints.get(registrable(url), "")))
        return True

    def pop(self) -> Item | None:
        while self.heap:
            it = heapq.heappop(self.heap)
            dom = registrable(it.url)
            if it.via not in ("reverify", "spec-doc") and (self.per_domain.get(dom, 0) >= self.max_per_domain or dom in self.barren):
                continue
            self.per_domain[dom] = self.per_domain.get(dom, 0) + 1
            return it
        return None

    def __len__(self):
        return len(self.heap)

    # ------------------------------------------------------------------ link expansion
    @staticmethod
    def link_score(url: str, text: str) -> float:
        s = 0.0
        blob = f"{text} {unquote(urlparse(url).path)}"
        if POS.search(text or ""):
            s += 3
        elif POS.search(blob):
            s += 2
        if has_scholarship_term(text or ""):
            s += 1
        if HELP.search(text or ""):
            s += 1
        if url.lower().endswith(".pdf") and POS.search(blob):
            s += 0.5
        if NEG.search(blob):
            s -= 4
        return s

    def expand(self, page, item: Item, boost: float = 0.0) -> int:
        """Enqueue relevant links from a fetched page. Returns number enqueued.
        `boost` > 0 when the page itself yielded scholarship records (focused crawling)."""
        added = 0
        src_dom = registrable(page.final_url or page.url)
        src_is_agg = self.cls.is_aggregator(page.url)
        for ln in page.links:
            u = ln.url
            if u.startswith(SKIP_SCHEMES) or not u.startswith("http"):
                continue
            sc = self.link_score(u, ln.text)
            if boost and re.search(r"details|read more|know more|click here|view|apply|program", ln.text or "", re.I):
                sc += 1.5
            if sc <= 0:
                continue
            sc += boost
            tgt_dom = registrable(u)
            same = tgt_dom == src_dom
            tgt_class = self.cls.domain_class(u)
            if tgt_class == "aggregator" and not same:
                continue                       # never pull aggregators in from official pages
            if src_is_agg:
                if same and sc >= 3 and item.depth < 2:
                    added += self.push(u, sc, item.depth + 1, "link", page.url)
                elif not same and (sc >= 2 or re.search(r"official|apply|website|portal", ln.text or "", re.I)):
                    added += self.push(u, sc + 2, item.depth + 1, "aggregator-outlink", page.url)
                continue
            if same:
                if sc >= 2 or (item.depth == 0 and sc >= 1):
                    added += self.push(u, sc - 0.3 * item.depth, item.depth + 1, "link", page.url)
            else:
                if tgt_class in ("government", "university", "international") and sc >= 2.5:
                    added += self.push(u, sc - 0.5, item.depth + 1, "link", page.url)
                elif tgt_class == "other" and (sc >= 3.5 or (sc >= 2 and re.search(r"official|apply|website|portal|know more|read more", ln.text or "", re.I))):
                    added += self.push(u, sc - 1, item.depth + 1, "link", page.url)
        return added

    # ------------------------------------------------------------------ sitemaps
    def sitemap_urls(self, fetcher, base_url: str, cap: int = 25) -> list[str]:
        p = urlparse(base_url)
        root = f"{p.scheme}://{p.netloc}"
        found: list[str] = []
        candidates = [root + "/sitemap.xml", root + "/sitemap_index.xml"]
        try:
            r = fetcher.s.get(root + "/robots.txt", timeout=10)
            if r.ok:
                candidates = re.findall(r"(?im)^sitemap:\s*(\S+)", r.text) + candidates
        except Exception:
            pass
        visited = 0
        for sm in candidates:
            if visited >= 4 or len(found) >= cap:
                break
            try:
                r = fetcher.s.get(sm, timeout=15)
                visited += 1
                if not r.ok or "<" not in r.text[:200]:
                    continue
                locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)
                subs = [l for l in locs if l.endswith(".xml")]
                pages = [l for l in locs if not l.endswith(".xml")]
                found.extend(l for l in pages if POS.search(unquote(l)) and not NEG.search(l))
                candidates.extend(s for s in subs if POS.search(s) or "page" in s or "post" in s)
            except Exception:
                continue
        return list(dict.fromkeys(found))[:cap]

    # ------------------------------------------------------------------ search
    def search(self, fetcher, query: str, k: int = 10) -> list[tuple[str, str]]:
        """DuckDuckGo HTML endpoint. Returns [(url, title)]. Gives up quietly if blocked / CAPTCHA."""
        try:
            r = fetcher.s.post("https://html.duckduckgo.com/html/", data={"q": query, "kl": "in-en"}, timeout=20)
        except Exception as e:
            log.info("search unavailable: %s", e)
            return []
        if r.status_code != 200 or "anomaly" in r.text.lower() or "captcha" in r.text.lower():
            log.info("search blocked (HTTP %s) — skipping, no CAPTCHA solving", r.status_code)
            return []
        soup = BeautifulSoup(r.text, "lxml")
        out = []
        for a in soup.select("a.result__a"):
            href = a.get("href", "")
            if "uddg=" in href:
                href = unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
            if href.startswith("http"):
                out.append((href, a.get_text(" ", strip=True)))
            if len(out) >= k:
                break
        return out
