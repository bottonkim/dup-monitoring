"""
설정 모듈 - .env 파일을 로드하고 타입이 지정된 설정값을 노출합니다.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

# 프로젝트 루트 기준으로 .env 로드
_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env")


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"필수 환경변수 '{key}' 가 설정되지 않았습니다. .env 파일을 확인하세요.")
    return val


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default)


@dataclass
class Settings:
    # 주소 API
    juso_api_key: str = field(default_factory=lambda: _get("JUSO_API_KEY"))
    vworld_api_key: str = field(default_factory=lambda: _get("VWORLD_API_KEY"))
    seoul_api_key: str = field(default_factory=lambda: _get("SEOUL_API_KEY"))

    # Claude API
    anthropic_api_key: str = field(default_factory=lambda: _get("ANTHROPIC_API_KEY"))
    claude_model: str = field(default_factory=lambda: _get("CLAUDE_MODEL", "claude-sonnet-4-6"))
    claude_max_tokens: int = field(default_factory=lambda: int(_get("CLAUDE_MAX_TOKENS", "4096")))
    claude_max_pdf_chars: int = field(default_factory=lambda: int(_get("CLAUDE_MAX_PDF_CHARS", "100000")))

    # 이메일
    smtp_host: str = field(default_factory=lambda: _get("SMTP_HOST", "smtp.gmail.com"))
    smtp_port: int = field(default_factory=lambda: int(_get("SMTP_PORT", "587")))
    smtp_use_tls: bool = field(default_factory=lambda: _get("SMTP_USE_TLS", "true").lower() == "true")
    smtp_username: str = field(default_factory=lambda: _get("SMTP_USERNAME"))
    smtp_password: str = field(default_factory=lambda: _get("SMTP_PASSWORD"))
    email_from: str = field(default_factory=lambda: _get("EMAIL_FROM"))
    email_to: list = field(default_factory=lambda: [e.strip() for e in _get("EMAIL_TO").split(",") if e.strip()])

    # 스케줄
    schedule_scraper_cron: str = field(default_factory=lambda: _get("SCHEDULE_SCRAPER_CRON", "0 */6 * * *"))
    schedule_digest_time: str = field(default_factory=lambda: _get("SCHEDULE_DIGEST_TIME", "09:00"))
    schedule_alert_interval_minutes: int = field(default_factory=lambda: int(_get("SCHEDULE_ALERT_INTERVAL_MINUTES", "240")))

    # 경로
    db_path: Path = field(default_factory=lambda: _ROOT / _get("DB_PATH", "data/db/monitoring.db"))
    pdf_cache_dir: Path = field(default_factory=lambda: _ROOT / _get("PDF_CACHE_DIR", "data/pdfs"))
    log_file: Path = field(default_factory=lambda: _ROOT / _get("LOG_FILE", "logs/app.log"))
    log_level: str = field(default_factory=lambda: _get("LOG_LEVEL", "INFO"))

    # VWORLD 도메인 (외부 배포 시 변경)
    vworld_domain: str = field(default_factory=lambda: _get("VWORLD_DOMAIN", "localhost"))

    # 스크래핑
    max_pages_per_source: int = field(default_factory=lambda: int(_get("MAX_PAGES_PER_SOURCE", "5")))
    request_timeout: int = field(default_factory=lambda: int(_get("REQUEST_TIMEOUT", "30")))
    max_retry_attempts: int = field(default_factory=lambda: int(_get("MAX_RETRY_ATTEMPTS", "3")))
    retry_backoff_seconds: int = field(default_factory=lambda: int(_get("RETRY_BACKOFF_SECONDS", "5")))
    lookback_days: int = field(default_factory=lambda: int(_get("LOOKBACK_DAYS", "30")))

    # PDF
    max_pdf_size_mb: int = field(default_factory=lambda: int(_get("MAX_PDF_SIZE_MB", "50")))

    def validate(self):
        """필수 API 키 존재 여부 확인"""
        warnings = []
        if not self.juso_api_key:
            warnings.append("JUSO_API_KEY 미설정 - 지번 조회 불가")
        if not self.vworld_api_key:
            warnings.append("VWORLD_API_KEY 미설정 - 도시계획 구역 조회 불가")
        if not self.anthropic_api_key:
            warnings.append("ANTHROPIC_API_KEY 미설정 - PDF AI 분석 불가")
        if not self.seoul_api_key:
            warnings.append("SEOUL_API_KEY 미설정 - 서울 고시공고 API 불가")
        return warnings


# 전역 싱글톤
settings = Settings()
