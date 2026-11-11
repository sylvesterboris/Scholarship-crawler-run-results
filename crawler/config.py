"""Load YAML configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    settings: dict
    domains: dict
    root: Path = ROOT

    @property
    def db_path(self) -> Path:
        p = Path(os.environ.get("SCHOLARSHIP_DB", self.settings.get("database", "data/scholarships.db")))
        return p if p.is_absolute() else self.root / p

    def crawl(self, key, default=None):
        return self.settings.get("crawl", {}).get(key, default)

    def discovery(self, key, default=None):
        return self.settings.get("discovery", {}).get(key, default)

    def verification(self, key, default=None):
        return self.settings.get("verification", {}).get(key, default)

    def llm(self, key, default=None):
        return self.settings.get("llm", {}).get(key, default)

    @property
    def seeds(self) -> list[dict]:
        return self.settings.get("seeds", [])


def load_config(settings_path: str | None = None, domains_path: str | None = None) -> Config:
    sp = Path(settings_path) if settings_path else ROOT / "config" / "settings.yaml"
    dp = Path(domains_path) if domains_path else ROOT / "config" / "domains.yaml"
    with open(sp, encoding="utf-8") as f:
        settings = yaml.safe_load(f) or {}
    with open(dp, encoding="utf-8") as f:
        domains = yaml.safe_load(f) or {}
    return Config(settings=settings, domains=domains)
