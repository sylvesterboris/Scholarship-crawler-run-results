"""End-to-end test on a local fixture site (fictional pages, temporary database).

Run 1  : Alpha, Beta, Gamma discovered from the hub (not hard-coded)
Run 2  : Alpha deadline changes, Beta page disappears, Gamma unchanged, Delta appears
Checks : change log keeps old + new value + evidence; Beta goes stale; nothing is invented.
"""
import http.server
import os
import re
import shutil
import socketserver
import threading
from datetime import date
from functools import partial
from pathlib import Path

import pytest

from crawler.config import load_config
from crawler.pipeline import Pipeline

FIX = Path(__file__).parent / "fixtures" / "v1"


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture()
def site(tmp_path):
    root = tmp_path / "site"
    shutil.copytree(FIX, root)
    handler = partial(Quiet, directory=str(root))
    srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield root, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def make_cfg(base, db):
    cfg = load_config()
    cfg.settings["database"] = str(db)
    cfg.settings["seeds"] = [{"url": base + "/index.html", "hint": "Central Government"}]
    cfg.settings["discovery"] = {"use_sitemaps": False, "use_search": False}
    cfg.settings["crawl"].update({"delay_per_domain_sec": 0, "respect_robots_txt": False, "max_pages_per_run": 30})
    cfg.settings["llm"] = {"enabled": False}
    cfg.domains["government_hosts"] = list(cfg.domains["government_hosts"]) + ["127.0.0.1"]
    return cfg


def test_two_runs(site, tmp_path):
    root, base = site
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    cfg = make_cfg(base, tmp_path / "t.db")
    today = date(2026, 8, 10)

    p1 = Pipeline(cfg, today=today)
    r1 = p1.run()
    db = p1.db
    rows = {r["name"]: r for r in db.q("SELECT * FROM scholarships")}
    names = " | ".join(rows)
    assert any("Alpha" in n for n in rows), names
    assert any("Beta" in n for n in rows), names
    alpha = next(r for n, r in rows.items() if "Alpha" in n)
    assert alpha["closing_date"] == "2026-08-31"
    assert alpha["amount_inr_max"] == 50000
    assert alpha["income_limit_inr"] == 250000
    assert alpha["gender_criteria"] == "Female students only"
    assert alpha["lifecycle_status"] == "EXPIRING_SOON"
    assert alpha["age_criteria"] == "Not specified"        # page says nothing about age -> not invented
    gamma = next(r for n, r in rows.items() if "Gamma" in n)
    assert gamma["lifecycle_status"] == "EXPIRED"
    # every evidence span is verbatim in its snapshot
    for ev in db.q("SELECT e.evidence_text, s.text, s.title, s.links_json FROM field_evidence e JOIN snapshots s ON s.id=e.snapshot_id"):
        span = ev["evidence_text"]
        if span.startswith("link text"):
            assert re.search(r"-> (\S+)$", span).group(1) in ev["links_json"]
        else:
            assert span in ev["text"] or span in ev["title"], span

    # ---- mutate the site
    a = (root / "alpha.html").read_text().replace("31-08-2026", "15-09-2026")
    (root / "alpha.html").write_text(a)
    (root / "beta.html").unlink()
    (root / "delta.html").write_text((root / "gamma.html").read_text()
                                     .replace("Gamma Research Fellowship", "Delta Doctoral Fellowship")
                                     .replace("15-01-2026", "30-11-2026"))
    idx = (root / "index.html").read_text().replace("</ul>", '<li><a href="/delta.html">Delta Doctoral Fellowship</a></li></ul>')
    (root / "index.html").write_text(idx)

    p2 = Pipeline(cfg, today=today)
    r2 = p2.run()
    ch = db.q("SELECT * FROM change_log WHERE run_id=?", (r2,))
    alpha_change = [c for c in ch if c["field"] == "closing_date"]
    assert alpha_change and alpha_change[0]["old_value"] == "2026-08-31" and alpha_change[0]["new_value"] == "2026-09-15"
    assert "31-08-2026" in alpha_change[0]["old_evidence"] and "15-09-2026" in alpha_change[0]["new_evidence"]
    assert any(c["change_type"] == "SOURCE_UNREACHABLE" for c in ch)
    assert any(c["change_type"] == "NEW" and "Delta" in c["new_value"] for c in ch)

    # third run: Beta still gone -> NO_LONGER_VERIFIABLE
    p3 = Pipeline(cfg, today=today)
    p3.run()
    beta = db.one("SELECT * FROM scholarships WHERE name LIKE 'Beta%'")
    assert beta["lifecycle_status"] == "NO_LONGER_VERIFIABLE"


def test_listing_with_linked_specifications(tmp_path):
    """A portal listing gives names + current dates; each entry's linked specification gives eligibility & amount."""
    root = tmp_path / "site"
    shutil.copytree(Path(__file__).parent / "fixtures" / "listing", root)
    srv = socketserver.TCPServer(("127.0.0.1", 0), partial(Quiet, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    cfg = make_cfg(base, tmp_path / "l.db")
    p = Pipeline(cfg, today=date(2026, 11, 11))
    p.run()
    rows = p.db.q("SELECT * FROM scholarships ORDER BY name")
    srv.shutdown()
    assert len(rows) == 5, [r["name"] for r in rows]
    r = next(x for x in rows if x["name"].startswith("Sigma"))
    assert r["closing_date"] == "2026-11-30"                 # from the listing, not the old 2023 guideline date
    assert r["amount_inr_max"] == 20000 and r["income_limit_inr"] == 300000
    assert r["lifecycle_status"] == "EXPIRING_SOON"
    ev = {e["field"]: e for e in p.db.q("SELECT * FROM field_evidence WHERE scholarship_id=? AND is_current=1", (r["id"],))}
    assert ev["closing_date"]["source_url"].endswith("/index.html")
    assert ev["benefit"]["source_url"].endswith("/spec1.html")  # per-field provenance
