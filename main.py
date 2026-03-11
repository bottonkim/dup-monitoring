#!/usr/bin/env python3
"""
서울시 지구단위계획 조회·모니터링 시스템
진입점: 웹 서버 + 백그라운드 스케줄러 동시 실행

실행:
    pip install -r requirements.txt
    playwright install chromium   # 최초 1회
    python main.py

접속:
    http://localhost:8000
"""
import argparse
import logging
import signal
import sys
import threading

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import settings
from db.database import run_migrations
from utils.logger import setup_logging

logger = logging.getLogger(__name__)


def start_scheduler(settings) -> BackgroundScheduler:
    """APScheduler 백그라운드 스케줄러 시작"""
    from scheduler.jobs import job_run_scrapers, job_daily_digest, job_alert_check

    scheduler = BackgroundScheduler(timezone="Asia/Seoul")

    # 잡 1: 스크래퍼 + PDF 파이프라인 (기본 6시간마다)
    scheduler.add_job(
        job_run_scrapers,
        CronTrigger.from_crontab(settings.schedule_scraper_cron, timezone="Asia/Seoul"),
        args=[settings],
        id="scraper_job",
        name="스크래퍼 + PDF 파이프라인",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )

    # 잡 2: 일일 다이제스트 이메일 (기본 09:00 KST)
    hour, minute = settings.schedule_digest_time.split(":")
    scheduler.add_job(
        job_daily_digest,
        CronTrigger(hour=int(hour), minute=int(minute), timezone="Asia/Seoul"),
        args=[settings],
        id="digest_job",
        name="일일 다이제스트 이메일",
        max_instances=1,
        coalesce=True,
    )

    # 잡 3: 결정고시 즉시 알림 (기본 4시간마다)
    scheduler.add_job(
        job_alert_check,
        IntervalTrigger(minutes=settings.schedule_alert_interval_minutes),
        args=[settings],
        id="alert_job",
        name="결정고시 즉시 알림 체크",
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    logger.info(
        f"스케줄러 시작 | 스크래퍼: {settings.schedule_scraper_cron} | "
        f"다이제스트: {settings.schedule_digest_time} | "
        f"즉시알림: {settings.schedule_alert_interval_minutes}분 주기"
    )
    return scheduler


def run_once(settings):
    """스케줄 없이 스크래퍼 1회 즉시 실행 (--run-once 플래그)"""
    from scheduler.jobs import job_run_scrapers
    logger.info("즉시 실행 모드: 스크래퍼 1회 실행")
    job_run_scrapers(settings)
    logger.info("즉시 실행 완료")


def main():
    parser = argparse.ArgumentParser(description="서울시 지구단위계획 조회·모니터링 시스템")
    parser.add_argument("--run-once", action="store_true",
                        help="스케줄러 없이 스크래퍼 1회 즉시 실행 후 종료")
    parser.add_argument("--no-scheduler", action="store_true",
                        help="웹 서버만 실행 (스케줄러 비활성화)")
    parser.add_argument("--host", default="0.0.0.0", help="웹 서버 호스트 (기본: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="웹 서버 포트 (기본: 8000)")
    args = parser.parse_args()

    # 로깅 설정
    setup_logging(settings.log_file, settings.log_level)
    logger.info("서울시 지구단위계획 조회·모니터링 시스템 시작")

    # API 키 경고 출력
    warnings = settings.validate()
    for w in warnings:
        logger.warning(w)

    # DB 마이그레이션
    run_migrations(settings.db_path)
    logger.info(f"DB 준비 완료: {settings.db_path}")

    if args.run_once:
        run_once(settings)
        return

    # 고시공고 DB 임포트 (첫 실행 시 ~2분, 이후 스킵)
    def _import_announcements():
        try:
            from lookup.announcements import import_all_upis_announcements
            import_all_upis_announcements(settings.seoul_api_key, settings.db_path)
        except Exception as e:
            logger.warning(f"고시공고 임포트 실패: {e}")
    t_import = threading.Thread(target=_import_announcements, daemon=True)
    t_import.start()

    scheduler = None
    if not args.no_scheduler:
        # 시작 시 스크래퍼 1회 즉시 실행 (백그라운드)
        def _initial_run():
            logger.info("초기 스크래퍼 실행 중...")
            from scheduler.jobs import job_run_scrapers
            job_run_scrapers(settings)
        t = threading.Thread(target=_initial_run, daemon=True)
        t.start()

        scheduler = start_scheduler(settings)

    # FastAPI 앱 생성
    from api.routes import create_app
    app = create_app(settings, settings.db_path)

    # 종료 핸들러
    def shutdown(sig, frame):
        logger.info("종료 신호 수신. 서버 종료 중...")
        if scheduler:
            scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # 웹 서버 실행
    logger.info(f"웹 서버 시작: http://{args.host}:{args.port}")
    logger.info(f"  로컬 접속: http://localhost:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
