#!/usr/bin/env python3
"""Export the repository to sample_data/ (CSV + JSON with full evidence trail)."""
import csv
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
db = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "scholarships.db"
out = ROOT / "sample_data"
out.mkdir(exist_ok=True)
c = sqlite3.connect(db)
c.row_factory = sqlite3.Row

rows = [dict(r) for r in c.execute("SELECT * FROM scholarships ORDER BY confidence DESC, name")]
cols = [k for k in rows[0] if k not in ("score_breakdown",)] if rows else []
with open(out / "scholarships.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)

full = []
for r in rows:
    r = dict(r)
    r["score_breakdown"] = json.loads(r["score_breakdown"] or "[]")
    r["eligibility_rules"] = json.loads(r["eligibility_rules"] or "{}")
    r["evidence"] = [dict(e) for e in c.execute(
        "SELECT field, value, evidence_text, source_url, snapshot_id, method FROM field_evidence WHERE scholarship_id=? AND is_current=1",
        (r["id"],))]
    r["change_history"] = [dict(e) for e in c.execute(
        "SELECT detected_at, run_id, change_type, field, old_value, new_value, source_url, old_evidence, new_evidence FROM change_log WHERE scholarship_id=? ORDER BY id",
        (r["id"],))]
    full.append(r)
(out / "scholarships_with_evidence.json").write_text(json.dumps(full, indent=2, ensure_ascii=False))
changes = [dict(x) for x in c.execute("""SELECT c.detected_at, c.run_id, s.name, c.change_type, c.field, c.old_value, c.new_value, c.source_url,
                                          c.old_evidence, c.new_evidence FROM change_log c JOIN scholarships s ON s.id=c.scholarship_id
                                          WHERE c.change_type != 'NEW' ORDER BY c.id""")]
(out / "change_log.json").write_text(json.dumps(changes, indent=2, ensure_ascii=False))
print(f"exported {len(rows)} scholarships, {len(changes)} changes -> {out}")
