"""
SQLite 데이터베이스 연결, 마이그레이션, CRUD 유틸리티
"""
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def get_connection(db_path: Path) -> sqlite3.Connection:
    """WAL 모드 SQLite 연결 반환"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def run_migrations(db_path: Path):
    """migrations/ 폴더의 SQL 파일을 순서대로 실행"""
    conn = get_connection(db_path)
    sql_files = sorted(_MIGRATIONS_DIR.glob("*.sql"))

    # ALTER TABLE ADD COLUMN은 IF NOT EXISTS 미지원 → 수동 체크
    existing_columns = {}
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        table = row["name"]
        cols = [c["name"] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        existing_columns[table] = set(cols)

    # announcements.content_quality 컬럼 없으면 추가
    if "content_quality" not in existing_columns.get("announcements", set()):
        try:
            conn.execute("ALTER TABLE announcements ADD COLUMN content_quality TEXT DEFAULT 'summary'")
            conn.commit()
            logger.info("content_quality 컬럼 추가 완료")
        except Exception:
            pass  # 이미 존재

    for sql_file in sql_files:
        logger.debug(f"마이그레이션 적용: {sql_file.name}")
        conn.executescript(sql_file.read_text(encoding="utf-8"))
    conn.commit()
    conn.close()
    logger.info(f"DB 마이그레이션 완료: {len(sql_files)}개 파일")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def upsert_announcement(conn: sqlite3.Connection, record: dict) -> tuple[int, bool]:
    """
    공고 upsert.
    반환: (id, is_new) — is_new=True 이면 신규 또는 내용 변경
    """
    existing = conn.execute(
        "SELECT id, content_hash FROM announcements WHERE source=? AND source_id=?",
        (record["source"], record["source_id"])
    ).fetchone()

    if existing is None:
        cur = conn.execute(
            """INSERT INTO announcements
               (source, source_id, title, category, district, zone_name,
                published_at, fetched_at, url, content_hash, raw_content, is_new,
                content_quality)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)""",
            (
                record["source"], record["source_id"], record["title"],
                record.get("category"), record.get("district"), record.get("zone_name"),
                record.get("published_at"), now_iso(), record.get("url"),
                record["content_hash"], record.get("raw_content"),
                record.get("content_quality", "summary"),
            )
        )
        conn.commit()
        return cur.lastrowid, True

    if existing["content_hash"] != record["content_hash"]:
        conn.execute(
            """UPDATE announcements
               SET title=?, category=?, district=?, zone_name=?,
                   published_at=?, fetched_at=?, url=?,
                   content_hash=?, raw_content=?, is_new=1, notified_at=NULL,
                   content_quality=?
               WHERE id=?""",
            (
                record["title"], record.get("category"), record.get("district"),
                record.get("zone_name"), record.get("published_at"), now_iso(),
                record.get("url"), record["content_hash"], record.get("raw_content"),
                record.get("content_quality", "summary"),
                existing["id"],
            )
        )
        conn.commit()
        return existing["id"], True

    return existing["id"], False


def upsert_pdf_attachment(conn: sqlite3.Connection, announcement_id: int, pdf_url: str, filename: str = None) -> int:
    """PDF 첨부파일 upsert. 반환: id"""
    existing = conn.execute(
        "SELECT id FROM pdf_attachments WHERE pdf_url=?", (pdf_url,)
    ).fetchone()
    if existing:
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO pdf_attachments (announcement_id, pdf_url, filename) VALUES (?,?,?)",
        (announcement_id, pdf_url, filename)
    )
    conn.commit()
    return cur.lastrowid


def get_pending_notifications(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """미발송 신규 공고 목록 반환"""
    return conn.execute(
        """SELECT a.*, pe.structured_json
           FROM announcements a
           LEFT JOIN pdf_attachments pa ON pa.announcement_id = a.id
           LEFT JOIN pdf_extractions pe ON pe.pdf_attachment_id = pa.id
           WHERE a.is_new=1 AND a.notified_at IS NULL
           ORDER BY a.published_at DESC"""
    ).fetchall()


def mark_notified(conn: sqlite3.Connection, ids: list[int]):
    """지정된 공고 ID들을 알림 완료 처리"""
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE announcements SET is_new=0, notified_at=? WHERE id IN ({placeholders})",
        [now_iso()] + ids
    )
    conn.commit()


def search_announcements_by_zone(conn: sqlite3.Connection, zone_names: list[str], limit: int = 10) -> list[sqlite3.Row]:
    """구역명 키워드로 고시공고 검색 (title + zone_name만 — raw_content는 오매칭 방지 위해 제외).
    content_quality='detailed' 우선 정렬."""
    results = []
    seen_ids = set()
    for zone in zone_names:
        keyword = f"%{zone}%"
        rows = conn.execute(
            """SELECT a.*, pe.structured_json
               FROM announcements a
               LEFT JOIN pdf_attachments pa ON pa.announcement_id = a.id
               LEFT JOIN pdf_extractions pe ON pe.pdf_attachment_id = pa.id
               WHERE (a.zone_name LIKE ? OR a.title LIKE ?)
               ORDER BY
                   CASE a.content_quality WHEN 'detailed' THEN 0 ELSE 1 END,
                   a.published_at DESC
               LIMIT ?""",
            (keyword, keyword, limit)
        ).fetchall()
        for row in rows:
            if row["id"] not in seen_ids:
                seen_ids.add(row["id"])
                results.append(row)
    return results


def log_scraper_run(conn: sqlite3.Connection, scraper_name: str, started_at: str,
                    finished_at: str, status: str, items_found: int, items_new: int,
                    error_message: Optional[str] = None):
    conn.execute(
        """INSERT INTO scraper_runs
           (scraper_name, started_at, finished_at, status, items_found, items_new, error_message)
           VALUES (?,?,?,?,?,?,?)""",
        (scraper_name, started_at, finished_at, status, items_found, items_new, error_message)
    )
    conn.commit()


def log_lookup(conn: sqlite3.Connection, address: str, pnu: Optional[str],
               zone_names: list[str], result: dict):
    conn.execute(
        """INSERT INTO lookup_history (queried_at, address, pnu, zone_names, result_json)
           VALUES (?,?,?,?,?)""",
        (now_iso(), address, pnu, json.dumps(zone_names, ensure_ascii=False),
         json.dumps(result, ensure_ascii=False))
    )
    conn.commit()
