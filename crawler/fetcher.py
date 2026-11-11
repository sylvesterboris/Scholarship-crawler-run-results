"""Polite HTTP fetcher: robots.txt, per-domain delay, HTML/PDF -> structured text.

Also supports *archive replay* (Wayback Machine `id_` snapshots of the same official URL),
which is how the change-detection demo compares a real older version of an official page
with the live one.
"""
from __future__ import annotations

import io
import logging
import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup, Comment

try:  # use the OS trust store (fixes many .gov.in sites with incomplete cert chains on macOS/Windows)
    import truststore  # type: ignore
    truststore.inject_into_ssl()
except Exception:  # pragma: no cover
    pass

from .textutil import norm_space, sha1

log = logging.getLogger("fetcher")

BLOCK_TAGS = ["p", "li", "td", "th", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6", "caption", "blockquote", "pre", "label"]
DROP_TAGS = ["script", "style", "noscript", "svg", "iframe", "form", "button", "select", "input", "template"]
CHROME_TAGS = ["nav", "footer", "header", "aside"]


@dataclass
class Link:
    url: str
    text: str


@dataclass
class Page:
    url: str                      # requested URL (canonical, official)
    final_url: str
    status: int
    content_type: str = ""
    title: str = ""
    text: str = ""                # newline-separated blocks; headings prefixed with '## '
    links: list[Link] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    error: str = ""
    archived_from: str = ""       # wayback URL if replayed

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and bool(self.text)

    @property
    def content_hash(self) -> str:
        return sha1(self.text)


def canonical(url: str) -> str:
    url, _ = urldefrag(url.strip())
    p = urlparse(url)
    if not p.scheme:
        return url
    path = re.sub(r"/{2,}", "/", p.path or "/")
    return p._replace(netloc=p.netloc.lower(), path=path).geturl()


class _LegacyTLSAdapter(requests.adapters.HTTPAdapter):
    """Certificate verification stays ON; this only re-enables legacy TLS renegotiation,
    which OpenSSL 3 refuses but many older .gov.in servers still require."""

    def init_poolmanager(self, *a, **kw):
        import ssl
        ctx = ssl.create_default_context()
        ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*a, **kw)


class Fetcher:
    def __init__(self, cfg, archive_timestamp: str | None = None):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.mount("https://", _LegacyTLSAdapter(max_retries=1))
        self.s.headers.update({
            "User-Agent": cfg.crawl("user_agent"),
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9,hi;q=0.6",
        })
        self.timeout = cfg.crawl("request_timeout", 25)
        self.delay = cfg.crawl("delay_per_domain_sec", 1.0)
        self.respect_robots = cfg.crawl("respect_robots_txt", True)
        self.archive_ts = archive_timestamp
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._pw = None

    # ------------------------------------------------------------ politeness
    def _wait(self, host: str):
        last = self._last_hit.get(host, 0)
        gap = time.time() - last
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last_hit[host] = time.time()

    def allowed(self, url: str) -> bool:
        if not self.respect_robots or self.archive_ts:
            return True
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.s.get(base + "/robots.txt", timeout=10)
                if r.status_code == 200 and "html" not in r.headers.get("content-type", ""):
                    rp.parse(r.text.splitlines())
                    self._robots[base] = rp
                else:
                    self._robots[base] = None
            except Exception:
                self._robots[base] = None
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.cfg.crawl("user_agent"), url)

    # ------------------------------------------------------------ fetch
    def fetch(self, url: str) -> Page:
        url = canonical(url)
        target = url
        if self.archive_ts:
            target = f"https://web.archive.org/web/{self.archive_ts}id_/{url}"
        host = urlparse(target).netloc
        if not self.allowed(url):
            return Page(url=url, final_url=url, status=0, error="blocked by robots.txt")
        self._wait(host)
        try:
            try:
                r = self.s.get(target, timeout=self.timeout, allow_redirects=True, stream=True)
            except requests.exceptions.ConnectionError as e:
                if "reset" not in str(e).lower() and "aborted" not in str(e).lower():
                    raise
                time.sleep(4)          # transient resets from rate-limiting firewalls: one slow retry
                r = self.s.get(target, timeout=self.timeout, allow_redirects=True, stream=True)
            ctype = r.headers.get("content-type", "").lower()
            limit = self.cfg.crawl("max_pdf_bytes", 12_000_000)
            body = r.raw.read(limit + 1, decode_content=True)
            r.close()
        except requests.exceptions.SSLError as e:
            return Page(url=url, final_url=url, status=0, error=f"TLS error: {str(e)[:160]}")
        except Exception as e:
            return Page(url=url, final_url=url, status=0, error=f"{type(e).__name__}: {str(e)[:160]}")

        final = r.url
        if self.archive_ts:
            # map wayback URL back to the original URL
            m = re.match(r"https?://web\.archive\.org/web/\d+(?:id_)?/(.*)", final)
            final = m.group(1) if m else url
        page = Page(url=url, final_url=canonical(final), status=r.status_code, content_type=ctype,
                    archived_from=target if self.archive_ts else "")
        if r.status_code >= 400:
            page.error = f"HTTP {r.status_code}"
            return page
        is_pdf = "pdf" in ctype or body[:5] == b"%PDF-"
        try:
            if is_pdf:
                if len(body) > limit:
                    page.error = "PDF too large"
                    return page
                self._parse_pdf(page, body)
            elif "html" in ctype or "xml" in ctype or body.lstrip()[:1] == b"<":
                html = body.decode(r.encoding or "utf-8", errors="replace") if r.encoding else body.decode("utf-8", errors="replace")
                self._parse_html(page, html)
                if self._looks_like_js_shell(page, html):
                    self._render_js(page)
            else:
                page.error = f"unsupported content-type {ctype}"
        except Exception as e:  # parsing should never kill the crawl
            page.error = f"parse error: {e}"
        return page

    def head_ok(self, url: str) -> int:
        """Lightweight reachability check for application URLs (no archive mode)."""
        if self.archive_ts:
            return 0
        try:
            self._wait(urlparse(url).netloc)
            r = self.s.get(url, timeout=self.timeout, allow_redirects=True, stream=True)
            code = r.status_code
            r.close()
            return code
        except Exception:
            return 0

    # ------------------------------------------------------------ parsing
    def _parse_html(self, page: Page, html: str):
        soup = BeautifulSoup(html, "lxml")
        for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
            c.extract()
        page.title = norm_space(soup.title.get_text()) if soup.title else ""
        for m in soup.find_all("meta"):
            k = (m.get("property") or m.get("name") or "").lower()
            if k in ("og:site_name", "og:title", "description", "og:description", "article:published_time",
                     "article:modified_time", "author", "dc.publisher", "last-modified"):
                page.meta[k] = norm_space(m.get("content", ""))
        base = page.final_url
        bt = soup.find("base", href=True)
        if bt:
            base = urljoin(base, bt["href"])
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            txt = norm_space(a.get_text(" ")) or norm_space(a.get("title", ""))
            page.links.append(Link(canonical(urljoin(base, href)), txt[:200]))
        for t in soup(DROP_TAGS):
            t.decompose()
        for t in soup(CHROME_TAGS):
            # keep header/nav text out of the evidence body (menus pollute extraction)
            t.decompose()
        body = soup.body or soup
        # Turn block structure into line breaks; mark headings and list items.
        for br in body.find_all("br"):
            br.replace_with("\n")
        for el in body.find_all(BLOCK_TAGS + ["div", "tr", "table", "section", "article", "ul", "ol"]):
            prefix = ""
            if el.name == "h1":
                prefix = "# "
            elif el.name in ("h2", "h3", "h4", "h5", "h6"):
                prefix = "## "
            elif el.name == "li":
                prefix = "- "
            elif el.name in ("td", "th"):
                prefix = " | "
            el.insert(0, "\n" + prefix if el.name not in ("td", "th") else prefix)
            el.append("\n" if el.name not in ("td", "th") else "")
        lines = []
        for raw in body.get_text(" ").split("\n"):
            line = norm_space(raw)
            line = re.sub(r"^\|\s*", "", line).strip()
            if not line or line in ("-", "#", "##", "|"):
                continue
            if lines and lines[-1] == line:
                continue
            lines.append(line)
        page.text = "\n".join(lines)

    def _parse_pdf(self, page: Page, body: bytes):
        import pdfplumber
        out = []
        with pdfplumber.open(io.BytesIO(body)) as pdf:
            for i, pg in enumerate(pdf.pages[: self.cfg.crawl("max_pdf_pages", 25)]):
                t = pg.extract_text() or ""
                out.extend(norm_space(l) for l in t.split("\n") if norm_space(l))
            page.title = (pdf.metadata or {}).get("Title", "") or ""
        page.text = "\n".join(out)
        if not page.title and out:
            page.title = out[0][:150]
        page.content_type = "application/pdf"
        for m in re.finditer(r"https?://[^\s)>\]]+", page.text):
            page.links.append(Link(canonical(m.group(0).rstrip(".,;")), "url in pdf"))

    # ------------------------------------------------------------ JS-rendered pages (optional)
    def _looks_like_js_shell(self, page: Page, html: str) -> bool:
        return len(page.text) < 200 or (len(page.text) < 400 and ("__NEXT_DATA__" in html or "ng-version" in html or 'id="root"' in html
                                                                   or 'id="app"' in html or "react" in html.lower()))

    def _render_js(self, page: Page):
        if self.cfg.crawl("use_playwright_for_js_pages", "auto") in (False, "false", "off") or self.archive_ts:
            return
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except Exception:
            return
        try:
            if self._pw is None:
                self._pw_ctx = sync_playwright().start()
                self._pw = self._pw_ctx.chromium.launch()
            ctx = self._pw.new_context(user_agent=self.cfg.crawl("user_agent"))
            pg = ctx.new_page()
            pg.goto(page.url, timeout=45000, wait_until="networkidle")
            html = pg.content()
            ctx.close()
            links_before = page.links
            page.links = []
            self._parse_html(page, html)
            if not page.links:
                page.links = links_before
            page.meta["rendered"] = "playwright"
        except Exception as e:
            log.debug("playwright failed for %s: %s", page.url, e)

    def close(self):
        try:
            if self._pw:
                self._pw.close()
                self._pw_ctx.stop()
        except Exception:
            pass
