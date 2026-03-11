"""
스크래퍼 추상 기반 클래스
"""
import hashlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def content_hash(title: str, content: str = "") -> str:
    raw = (title + content).strip().lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass
class AnnouncementRecord:
    source: str
    source_id: str
    title: str
    content_hash: str
    category: Optional[str] = None
    district: Optional[str] = None
    zone_name: Optional[str] = None
    published_at: Optional[str] = None
    url: Optional[str] = None
    raw_content: Optional[str] = None
    pdf_urls: list = field(default_factory=list)


@dataclass
class ScraperResult:
    scraper_name: str
    items_found: int = 0
    items_new: int = 0
    new_announcement_ids: list = field(default_factory=list)
    status: str = "success"
    error_message: Optional[str] = None


class ScraperBase(ABC):
    name: str = "base"

    def __init__(self, db_path: Path, settings):
        self.db_path = db_path
        self.settings = settings

    @abstractmethod
    def fetch(self) -> list[AnnouncementRecord]:
        """고시공고 목록 가져오기"""
        ...

    def run(self) -> ScraperResult:
        """스크래핑 실행 + DB upsert + 결과 로깅"""
        from db.database import get_connection, upsert_announcement, upsert_pdf_attachment, log_scraper_run, now_iso

        started_at = now_iso()
        result = ScraperResult(scraper_name=self.name)

        conn = get_connection(self.db_path)
        try:
            records = self.fetch()
            result.items_found = len(records)

            for record in records:
                try:
                    ann_id, is_new = upsert_announcement(conn, {
                        "source": record.source,
                        "source_id": record.source_id,
                        "title": record.title,
                        "category": record.category,
                        "district": record.district,
                        "zone_name": record.zone_name,
                        "published_at": record.published_at,
                        "url": record.url,
                        "content_hash": record.content_hash,
                        "raw_content": record.raw_content,
                    })

                    if is_new:
                        result.items_new += 1
                        result.new_announcement_ids.append(ann_id)
                        # PDF 첨부 등록
                        for pdf_url in record.pdf_urls:
                            upsert_pdf_attachment(conn, ann_id, pdf_url)

                except Exception as e:
                    logger.warning(f"{self.name}: 레코드 upsert 실패 ({record.source_id}): {e}")

            result.status = "success"

        except Exception as e:
            result.status = "failed"
            result.error_message = str(e)
            logger.error(f"{self.name} 스크래퍼 실패: {e}", exc_info=True)
        finally:
            log_scraper_run(
                conn, self.name, started_at, now_iso(),
                result.status, result.items_found, result.items_new, result.error_message
            )
            conn.close()

        return result
