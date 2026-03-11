"""
APScheduler 잡 정의
- job_run_scrapers: 모든 스크래퍼 실행 + PDF 파이프라인
- job_daily_digest: 일일 이메일 다이제스트
- job_alert_check: 결정고시 즉시 알림 체크
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def job_run_scrapers(settings):
    """모든 스크래퍼 실행 후 신규 항목 PDF 파이프라인 처리"""
    from scrapers.seoul_openapi import SeoulOpenAPIScraper
    from scrapers.seoul_gazette import SeoulGazetteScraper
    from scrapers.seoul_notice import SeoulNoticeScraper
    from scrapers.tojieum_monitor import TojieumMonitorScraper

    scraper_classes = [
        SeoulOpenAPIScraper,
        SeoulGazetteScraper,
        SeoulNoticeScraper,
        TojieumMonitorScraper,
    ]

    all_new_ids = []
    for cls in scraper_classes:
        scraper = cls(settings.db_path, settings)
        try:
            result = scraper.run()
            logger.info(
                f"{scraper.name}: 수집={result.items_found}, 신규={result.items_new}, "
                f"상태={result.status}"
            )
            all_new_ids.extend(result.new_announcement_ids)
        except Exception as e:
            logger.error(f"{scraper.name} 실행 실패: {e}", exc_info=True)

    # 신규 공고 PDF 파이프라인
    if all_new_ids:
        _process_new_pdfs(all_new_ids, settings)


def _process_new_pdfs(announcement_ids: list[int], settings):
    """신규 공고의 PDF를 다운로드·추출·AI 분석"""
    from db.database import get_connection
    from pdf.downloader import download_pdf
    from pdf.extractor import extract_text, is_image_pdf
    from pdf.claude_analyzer import analyze_pdf, analyze_image_pdf

    if not announcement_ids:
        return

    conn = get_connection(settings.db_path)
    placeholders = ",".join("?" * len(announcement_ids))
    rows = conn.execute(
        f"""SELECT pa.id, pa.pdf_url, a.title, a.published_at, a.source
            FROM pdf_attachments pa
            JOIN announcements a ON pa.announcement_id = a.id
            WHERE pa.announcement_id IN ({placeholders}) AND pa.download_status='pending'""",
        announcement_ids
    ).fetchall()

    for row in rows:
        try:
            local_path = download_pdf(
                row["pdf_url"],
                settings.pdf_cache_dir,
                max_bytes=settings.max_pdf_size_mb * 1024 * 1024,
                timeout=settings.request_timeout,
            )
            raw_text, page_count, char_count = extract_text(local_path)

            meta = {
                "title": row["title"],
                "source": row["source"],
                "published_at": row["published_at"] or "",
            }

            if settings.anthropic_api_key:
                if is_image_pdf(char_count):
                    result = analyze_image_pdf(local_path, meta, settings.anthropic_api_key, settings.claude_model)
                else:
                    result = analyze_pdf(
                        raw_text, meta, settings.anthropic_api_key,
                        settings.claude_model, settings.claude_max_tokens,
                        settings.claude_max_pdf_chars,
                    )
                structured_json = json.dumps(result, ensure_ascii=False)
            else:
                structured_json = json.dumps({"error": "ANTHROPIC_API_KEY 미설정"}, ensure_ascii=False)

            from db.database import now_iso
            conn.execute(
                """INSERT OR IGNORE INTO pdf_extractions
                   (pdf_attachment_id, extracted_at, claude_model, raw_text_chars, structured_json, extraction_status)
                   VALUES (?,?,?,?,?,'done')""",
                (row["id"], now_iso(), settings.claude_model, char_count, structured_json)
            )
            conn.execute(
                "UPDATE pdf_attachments SET download_status='done', local_path=?, downloaded_at=? WHERE id=?",
                (str(local_path), now_iso(), row["id"])
            )
            conn.commit()
            logger.info(f"PDF 처리 완료: {row['pdf_url'][:60]}")

        except Exception as e:
            logger.error(f"PDF 처리 실패 ({row['pdf_url'][:60]}): {e}")
            conn.execute(
                "UPDATE pdf_attachments SET download_status='failed' WHERE id=?",
                (row["id"],)
            )
            conn.commit()

    conn.close()


def job_daily_digest(settings):
    """미알림 공고 일일 다이제스트 이메일 발송"""
    from db.database import get_connection, get_pending_notifications, mark_notified
    from notifications.email_sender import send_daily_digest

    conn = get_connection(settings.db_path)
    try:
        pending = get_pending_notifications(conn)
        if not pending:
            logger.info("미알림 공고 없음 - 다이제스트 발송 건너뜀")
            return

        send_daily_digest(list(pending), settings)
        mark_notified(conn, [row["id"] for row in pending])
        logger.info(f"일일 다이제스트 발송: {len(pending)}건")
    finally:
        conn.close()


def job_alert_check(settings):
    """결정고시/지정고시 즉시 알림 발송 (4시간 주기)"""
    from db.database import get_connection, mark_notified
    from notifications.email_sender import send_immediate_alert

    conn = get_connection(settings.db_path)
    try:
        urgent = conn.execute(
            """SELECT a.*, pe.structured_json as structured_json
               FROM announcements a
               LEFT JOIN pdf_attachments pa ON pa.announcement_id = a.id
               LEFT JOIN pdf_extractions pe ON pe.pdf_attachment_id = pa.id
               WHERE a.category IN ('결정고시','지정고시') AND a.is_new=1 AND a.notified_at IS NULL"""
        ).fetchall()

        for row in urgent:
            try:
                ann = dict(row)
                send_immediate_alert(ann, settings)
                mark_notified(conn, [ann["id"]])
                logger.info(f"즉시 알림 발송: {ann.get('title', '')[:50]}")
            except Exception as e:
                logger.error(f"즉시 알림 실패 (id={row['id']}): {e}")
    finally:
        conn.close()
