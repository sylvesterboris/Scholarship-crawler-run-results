"""Source classification: what kind of site is this, and can it be authoritative?

authority levels
  PRIMARY_OFFICIAL    the provider's own site, or the government portal that administers the scheme
  SECONDARY_OFFICIAL  an official site that re-publishes someone else's scheme (a university notice about a
                      CSR scholarship, a government catalogue such as myScheme)
  AGGREGATOR          blogs, news, scholarship listing sites — used only to discover official links
  UNVERIFIED          an unknown site whose relationship to the provider cannot be established
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import tldextract

_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)  # bundled PSL snapshot, no network

STOP = {"the", "of", "for", "and", "india", "indian", "limited", "ltd", "pvt", "private", "trust", "foundation",
        "society", "charitable", "education", "educational", "scholarship", "scholarships", "programme", "program",
        "department", "ministry", "government", "govt", "national", "council", "institute", "university", "group"}


def host_of(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def registrable(url_or_host: str) -> str:
    h = url_or_host if "://" not in url_or_host else (urlparse(url_or_host).hostname or "")
    e = _extract(h)
    return ".".join(p for p in (e.domain, e.suffix) if p) or h


def _host_matches(host: str, entries) -> bool:
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in entries or [])


class SourceClassifier:
    def __init__(self, domains_cfg: dict):
        self.d = domains_cfg

    def is_aggregator(self, url: str) -> bool:
        return _host_matches(host_of(url), self.d.get("aggregators"))

    def is_application_platform(self, url: str) -> bool:
        return _host_matches(host_of(url), self.d.get("application_platforms"))

    def is_official_catalog(self, url: str) -> bool:
        return _host_matches(host_of(url), self.d.get("official_catalogs"))

    def domain_class(self, url: str) -> str:
        """government | university | international | aggregator | other"""
        host = host_of(url)
        if self.is_aggregator(url):
            return "aggregator"
        if _host_matches(host, self.d.get("international_hosts")) or _host_matches(host, self.d.get("foreign_government_suffixes")):
            return "international"
        if _host_matches(host, self.d.get("government_hosts")) or _host_matches(host, self.d.get("government_suffixes")):
            return "government"
        if _host_matches(host, self.d.get("university_suffixes")):
            return "university"
        return "other"

    def government_level(self, url: str) -> str:
        host = host_of(url)
        return "State Government" if any(m in host for m in self.d.get("state_markers", [])) else "Central Government"

    # ------------------------------------------------------------------ provider <-> domain affinity
    @staticmethod
    def provider_tokens(provider: str) -> list[str]:
        toks = [t for t in re.findall(r"[a-z0-9]+", (provider or "").lower()) if t not in STOP and len(t) > 1]
        return toks

    def provider_matches_domain(self, provider: str, url: str) -> bool:
        """True when the provider's name is visibly the owner of the domain.
        e.g. 'Reliance Foundation' ~ reliancefoundation.org, 'Kotak Education Foundation' ~ kotakeducationfoundation.org,
        'All India Council for Technical Education' ~ aicte-india.org (acronym)."""
        if not provider:
            return False
        label = _extract(host_of(url)).domain.replace("-", "")
        full = re.sub(r"[^a-z0-9]", "", provider.lower())
        toks = self.provider_tokens(provider)
        words = [w for w in re.findall(r"[a-z]+", provider.lower()) if w not in ("of", "for", "and", "the", "in")]
        acronym = "".join(w[0] for w in words)
        if label and (label in full or full in label):
            return True
        if acronym and len(acronym) >= 3 and (label.startswith(acronym) or acronym in label):
            return True
        if toks and all(t in label for t in toks[:2]):
            return True
        # first distinctive token (brand) in domain label: 'santoor' in santoorscholarships
        return bool(toks) and len(toks[0]) >= 4 and label.startswith(toks[0])

    # ------------------------------------------------------------------ main decision
    def name_matches_domain(self, name: str, url: str) -> bool:
        """A site dedicated to the scholarship itself, e.g. 'Santoor Women's Scholarship' on santoorscholarships.com."""
        label = _extract(host_of(url)).domain.replace("-", "")
        toks = [t for t in self.provider_tokens(name) if t not in ("women", "girls", "merit", "post", "pre", "matric", "research")]
        return bool(toks) and len(toks[0]) >= 4 and toks[0] in label

    def classify(self, url: str, provider: str | None, page_text: str = "", hint: str | None = None,
                 name: str = "") -> tuple[str, str]:
        """Return (source_type, authority) for a page that describes a scholarship from `provider`."""
        dc = self.domain_class(url)
        text = (page_text or "")[:20000].lower()
        if dc == "aggregator":
            return "Aggregator", "AGGREGATOR"
        prov = (provider or "").lower()
        prov_is_gov = bool(re.search(r"\b(ministry|department|government|govt|directorate|commission|council|"
                                     r"board|aicte|ugc|nta|welfare|sainik)\b", prov))
        prov_is_univ = bool(re.search(r"\b(university|institute of technology|iit|nit|iiit|college|vidyapeeth|iisc|aiims)\b", prov))
        matches = self.provider_matches_domain(provider or "", url) or (dc == "other" and self.name_matches_domain(name, url))

        if self.is_official_catalog(url):
            return ("State Government" if "state" in prov else "Central Government"), "SECONDARY_OFFICIAL"

        if dc == "government":
            level = self.government_level(url)
            if not provider or prov_is_gov or matches:
                return level, "PRIMARY_OFFICIAL"
            # a government site describing a private/foreign scheme -> official, but not the provider
            return self._type_from_provider(prov, text, hint), "SECONDARY_OFFICIAL"

        if dc == "international":
            return "International", "PRIMARY_OFFICIAL" if (matches or not provider or "fulbright" in prov or "chevening" in prov) else "SECONDARY_OFFICIAL"

        if dc == "university":
            if not provider or prov_is_univ or matches:
                return "University", "PRIMARY_OFFICIAL"
            # university notice board re-posting an external scheme
            return self._type_from_provider(prov, text, hint), "SECONDARY_OFFICIAL"

        # dc == other (.org/.com/.in ...)
        if matches:
            return self._type_from_provider(prov, text, hint), "PRIMARY_OFFICIAL"
        return self._type_from_provider(prov, text, hint), "UNVERIFIED"

    @staticmethod
    def _type_from_provider(prov: str, text: str, hint: str | None) -> str:
        if re.search(r"\b(ministry|department|government|govt)\b", prov):
            return "Central Government"
        if re.search(r"\b(university|institute|college)\b", prov):
            return "University"
        if re.search(r"\b(fulbright|chevening|daad|commonwealth|rhodes|study abroad|abroad)\b", prov + " " + text[:3000]):
            if re.search(r"\b(usa|united states|uk|united kingdom|germany|abroad|overseas)\b", text[:5000]):
                return "International"
        if re.search(r"\b(ltd|limited|bank|corporation|industries|motors|capital|finance|power|oil|csr)\b", prov) or \
                re.search(r"corporate social responsibility|\bcsr\b", text[:8000]):
            return "Corporate CSR"
        if re.search(r"\b(foundation|trust|society|endowment|charitable|ngo|sabha|samiti)\b", prov):
            return "NGO/Trust"
        if hint and hint not in ("Aggregator",):
            return hint
        return "NGO/Trust" if prov else "Unknown"
