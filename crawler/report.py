"""Console summary printed after each run."""
from __future__ import annotations


def print_run_report(db, run_id: int):
    r = db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    tot = db.one("""SELECT COUNT(*) n, SUM(verification_status='VERIFIED') v, SUM(verification_status='REVIEW_REQUIRED') rr,
                           SUM(confidence>=95) hi, ROUND(AVG(confidence),1) avg, COUNT(DISTINCT source_type) st
                    FROM scholarships""")
    print("\n" + "=" * 78)
    print(f" RUN {run_id}  [{r['mode']}]  pages fetched {r['pages_fetched']}  failed {r['pages_failed']}  candidates {r['candidates']}")
    print(f" new {r['new_records']}  changed {r['changed_records']}  unchanged {r['unchanged_records']}  newly stale/expired {r['stale_records']}")
    print("-" * 78)
    print(f" Repository: {tot['n']} scholarships | VERIFIED {tot['v'] or 0} | REVIEW_REQUIRED {tot['rr'] or 0} | "
          f">=95%: {tot['hi'] or 0} | avg confidence {tot['avg']} | source types {tot['st']}")
    for row in db.q("SELECT lifecycle_status s, COUNT(*) n FROM scholarships GROUP BY 1 ORDER BY 2 DESC"):
        print(f"   {row['s']:<22}{row['n']}")
    ch = db.q("""SELECT c.change_type, c.field, c.old_value, c.new_value, s.name FROM change_log c JOIN scholarships s ON s.id=c.scholarship_id
                 WHERE c.run_id=? AND c.change_type!='NEW' ORDER BY c.id LIMIT 25""", (run_id,))
    if ch:
        print("-" * 78 + "\n CHANGES DETECTED THIS RUN")
        for c in ch:
            print(f"   [{c['change_type']}] {c['name'][:45]} :: {c['field']}: {str(c['old_value'])[:30]} -> {str(c['new_value'])[:30]}")
    print("=" * 78 + "\n")
