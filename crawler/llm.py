"""Optional local-LLM gap filler (Ollama, free & local). Disabled automatically if Ollama is not running.

The LLM is only allowed to fill fields the rule-based extractor left empty, and only by returning a
verbatim QUOTE from the page. The quote is then checked against the snapshot exactly like every other
evidence span; if the quote is not on the page, or the value's numbers/dates are not inside the quote,
the answer is discarded. The LLM never produces the confidence score.
"""
from __future__ import annotations

import json
import logging
import re

import requests

from .extractor import Ev
from .textutil import find_dates, norm_for_match, parse_inr_amounts

log = logging.getLogger("llm")

FILLABLE = ["benefit", "income_criteria", "academic_requirement", "age_criteria", "domicile_criteria",
            "selection_process", "renewal_requirements", "documents_required", "closing_date"]

PROMPT = """You extract facts about ONE scholarship from an official web page.
Scholarship: {name}
For each field below, copy the exact sentence from the PAGE that states it. If the page does not state it, use null.
Never paraphrase, never use outside knowledge, never guess.
Fields: {fields}
Return JSON: {{"<field>": {{"value": "<short value>", "quote": "<exact sentence copied from PAGE>"}} or null}}

PAGE:
\"\"\"{page}\"\"\""""


class LLMFiller:
    def __init__(self, cfg):
        self.url = cfg.llm("ollama_url", "http://localhost:11434")
        self.model = cfg.llm("model", "qwen2.5:7b-instruct")
        mode = str(cfg.llm("enabled", "auto")).lower()
        self.enabled = False
        if mode in ("auto", "true", "on"):
            try:
                r = requests.get(self.url + "/api/tags", timeout=2)
                names = [m.get("name", "") for m in r.json().get("models", [])]
                self.enabled = r.ok and any(n.startswith(self.model.split(":")[0]) for n in names)
            except Exception:
                self.enabled = False
        log.info("LLM gap-filler %s", "enabled (%s)" % self.model if self.enabled else "disabled")

    def fill(self, cand, page_text: str) -> list[str]:
        if not self.enabled:
            return []
        missing = [f for f in FILLABLE if f not in cand.fields]
        if not missing:
            return []
        lo, hi = cand.line_range
        text = "\n".join(page_text.split("\n")[lo:hi])[:12000]
        try:
            r = requests.post(self.url + "/api/generate", timeout=120, json={
                "model": self.model, "format": "json", "stream": False, "options": {"temperature": 0},
                "prompt": PROMPT.format(name=cand.name.value, fields=", ".join(missing), page=text)})
            data = json.loads(r.json().get("response", "{}"))
        except Exception as e:
            log.debug("llm failed: %s", e)
            return []
        hay = norm_for_match(page_text)
        added = []
        for f in missing:
            item = data.get(f)
            if not isinstance(item, dict) or not item.get("quote") or not item.get("value"):
                continue
            q, v = str(item["quote"]), str(item["value"])
            if norm_for_match(q) not in hay:                      # quote must be verbatim on the page
                continue
            nums_v = re.findall(r"\d[\d,.]*", v)
            if any(n.replace(",", "") not in q.replace(",", "") for n in nums_v):  # value numbers must come from quote
                continue
            parsed = None
            if f == "closing_date":
                ds = find_dates(q)
                if not ds:
                    continue
                parsed = ds[-1][0]
                v = parsed.isoformat()
            if f == "benefit":
                am = parse_inr_amounts(q)
                parsed = max(a[0] for a in am) if am else None
            cand.fields[f] = Ev(v[:600], [q], f"llm:{self.model}+quote-verified", parsed)
            added.append(f)
        return added
