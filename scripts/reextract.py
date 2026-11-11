#!/usr/bin/env python3
"""Developer tool: re-run segmentation + extraction over snapshots already stored in a database.

No network access. Used to tune the extractor against real pages the crawler has fetched,
then the next crawl run applies the improved extractor to live pages.

    python scripts/reextract.py data/scholarships.db [url-substring]
"""
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crawler.extractor import Extractor  # noqa: E402
from crawler.fetcher import Link, Page  # noqa: E402
from crawler.verifier import verify_candidate  # noqa: E402

db = sqlite3.connect(sys.argv[1])
db.row_factory = sqlite3.Row
flt = sys.argv[2] if len(sys.argv) > 2 else ""
ex = Extractor(date.today())
rows = db.execute("""SELECT s.* FROM snapshots s JOIN (SELECT url, MAX(id) mid FROM snapshots GROUP BY url) m ON m.mid=s.id
                     WHERE s.url LIKE ?""", (f"%{flt}%",)).fetchall()
n = 0
for r in rows:
    links = [Link(u, t) for u, t in json.loads(r["links_json"] or "[]")]
    page = Page(url=r["url"], final_url=r["url"], status=r["http_status"] or 200, title=r["title"] or "", text=r["text"] or "", links=links)
    kind, segs = ex.segment(page.text.split("\n"), page.title, page.meta)
    for seg in segs:
        c = ex.extract(page, seg)
        if not c:
            continue
        ok, dropped = verify_candidate(c, page)
        if "name" not in ok:
            continue
        n += 1
        f = {k: v for k, v in ok.items() if k != "name"}
        print(f"\n[{kind}] {c.name.value}   <{r['url'][:90]}>  signals={c.signals}")
        for k in ("provider", "benefit", "closing_date", "opening_date", "status_statement", "academic_year", "education_level",
                  "income_criteria", "category_criteria", "gender_criteria", "application_url"):
            if k in f:
                print(f"   {k:17} {f[k].value[:110]}")
        if dropped:
            print("   DROPPED (no evidence):", dropped)
print(f"\n{n} candidates from {len(rows)} snapshots")
