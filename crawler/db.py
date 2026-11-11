"""SQLite persistence."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA.read_text())
        self.conn.commit()

    def q(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        return self.conn.execute(sql, args).fetchone()

    def x(self, sql, args=()):
        cur = self.conn.execute(sql, args)
        return cur.lastrowid

    def commit(self):
        self.conn.commit()

    # ------------------------------------------------------------ runs
    def start_run(self, mode: str) -> int:
        rid = self.x("INSERT INTO runs(started_at, mode) VALUES (?,?)", (now_iso(), mode))
        self.commit()
        return rid

    def finish_run(self, rid: int, **stats):
        cols = ", ".join(f"{k}=?" for k in stats)
        self.x(f"UPDATE runs SET finished_at=?, {cols} WHERE id=?", (now_iso(), *stats.values(), rid))
        self.commit()

    # ------------------------------------------------------------ sources & snapshots
    def upsert_source(self, url, domain, **kw):
        row = self.one("SELECT url FROM sources WHERE url=?", (url,))
        if row is None:
            kw.setdefault("first_seen", now_iso())
            cols = ["url", "domain"] + list(kw)
            self.x(f"INSERT INTO sources({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", (url, domain, *kw.values()))
        elif kw:
            # never overwrite discovery provenance once recorded
            kw = {k: v for k, v in kw.items() if k not in ("discovered_via", "discovered_from", "first_seen", "depth")}
            if kw:
                self.x(f"UPDATE sources SET {', '.join(f'{k}=?' for k in kw)} WHERE url=?", (*kw.values(), url))

    def snapshot(self, page, run_id) -> int:
        """Store page text; reuse the previous snapshot row if content is byte-identical."""
        prev = self.one("SELECT id FROM snapshots WHERE url=? AND content_hash=? AND COALESCE(archived_from,'')=? ORDER BY id DESC LIMIT 1",
                        (page.url, page.content_hash, page.archived_from or ""))
        if prev:
            return prev["id"]
        links = json.dumps([[l.url, l.text] for l in page.links[:1500]], ensure_ascii=False)
        return self.x("""INSERT INTO snapshots(url, run_id, fetched_at, http_status, content_type, content_hash, title, text, links_json, archived_from)
                         VALUES (?,?,?,?,?,?,?,?,?,?)""",
                      (page.url, run_id, now_iso(), page.status, page.content_type, page.content_hash, page.title, page.text,
                       links, page.archived_from or None))

    # ------------------------------------------------------------ scholarships
    def find_existing(self, name_key: str, domain_hint: str | None):
        rows = self.q("SELECT * FROM scholarships WHERE name_key=?", (name_key,))
        if len(rows) <= 1:
            return rows[0] if rows else None
        for r in rows:
            if domain_hint and r["provider_domain"] == domain_hint:
                return r
        return rows[0]

    def upsert_scholarship(self, rec: dict):
        exists = self.one("SELECT id FROM scholarships WHERE id=?", (rec["id"],))
        if exists:
            cols = [k for k in rec if k != "id"]
            self.x(f"UPDATE scholarships SET {', '.join(f'{c}=?' for c in cols)} WHERE id=?", (*[rec[c] for c in cols], rec["id"]))
        else:
            cols = list(rec)
            self.x(f"INSERT INTO scholarships({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", tuple(rec[c] for c in cols))

    def current_evidence(self, sid: str) -> dict[str, list[sqlite3.Row]]:
        out: dict[str, list] = {}
        for r in self.q("SELECT * FROM field_evidence WHERE scholarship_id=? AND is_current=1 ORDER BY id", (sid,)):
            out.setdefault(r["field"], []).append(r)
        return out

    def replace_evidence(self, sid: str, rows: list[dict], run_id: int):
        self.x("UPDATE field_evidence SET is_current=0 WHERE scholarship_id=? AND is_current=1", (sid,))
        for r in rows:
            self.x("""INSERT INTO field_evidence(scholarship_id, field, value, evidence_text, source_url, snapshot_id, char_start, char_end, method, run_id, is_current)
                      VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
                   (sid, r["field"], r["value"], r["evidence_text"], r["source_url"], r["snapshot_id"], r.get("char_start"),
                    r.get("char_end"), r["method"], run_id))

    def log_change(self, **kw):
        kw.setdefault("detected_at", now_iso())
        cols = list(kw)
        self.x(f"INSERT INTO change_log({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", tuple(kw.values()))

    def record_checks(self, sid: str, run_id: int, checks: list[dict]):
        self.x("DELETE FROM verification_checks WHERE scholarship_id=? AND run_id=?", (sid, run_id))
        for c in checks:
            self.x("INSERT INTO verification_checks VALUES (?,?,?,?,?,?,?)",
                   (sid, run_id, c["check"], c["points"], c["max"], int(c["points"] >= c["max"]), c["detail"]))

    def link_source(self, sid, url, authority, role, snapshot_id, closing_date, amount):
        self.x("""INSERT INTO scholarship_sources(scholarship_id, url, authority, role, snapshot_id, closing_date, amount_inr_max, last_seen)
                  VALUES (?,?,?,?,?,?,?,?)
                  ON CONFLICT(scholarship_id, url) DO UPDATE SET authority=excluded.authority, role=excluded.role,
                  snapshot_id=excluded.snapshot_id, closing_date=excluded.closing_date, amount_inr_max=excluded.amount_inr_max,
                  last_seen=excluded.last_seen""",
               (sid, url, authority, role, snapshot_id, closing_date, amount, now_iso()))
