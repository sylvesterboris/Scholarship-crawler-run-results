#!/usr/bin/env python3
"""Run the Scholarship Intelligence Crawler.

  python run_crawler.py                      # one live run (re-verify known records, then discover new ones)
  python run_crawler.py --every 24           # keep running, one crawl every 24 hours
  python run_crawler.py --archive 20250901   # replay Wayback Machine copies of the official pages as they were
                                             # on that date (used to show real change detection between two crawls)
  python run_crawler.py --max-pages 150      # smaller budget for a quick demo
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from crawler.config import load_config
from crawler.pipeline import Pipeline
from crawler.report import print_run_report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", help="YYYYMMDD — replay archived copies of official pages (Wayback Machine)")
    ap.add_argument("--max-pages", type=int, help="fetch budget for this run")
    ap.add_argument("--every", type=float, help="repeat every N hours (auto-crawler mode)")
    ap.add_argument("--db", help="path to SQLite database (overrides config)")
    ap.add_argument("--no-search", action="store_true", help="disable web-search discovery")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("urllib3", "pdfminer", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    cfg = load_config()
    if a.db:
        cfg.settings["database"] = a.db
    if a.no_search:
        cfg.settings.setdefault("discovery", {})["use_search"] = False

    while True:
        p = Pipeline(cfg, archive=a.archive, max_pages=a.max_pages)
        rid = p.run()
        print_run_report(p.db, rid)
        if not a.every:
            break
        logging.info("Sleeping %.1f h until next crawl", a.every)
        time.sleep(a.every * 3600)


if __name__ == "__main__":
    sys.exit(main())
