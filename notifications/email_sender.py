"""
이메일 발송 모듈 (smtplib + Jinja2 HTML 템플릿)
"""
import json
import logging
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _get_jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=True,
    )


def _send(settings, subject: str, html_body: str):
    """smtplib로 실제 발송"""
    if not settings.smtp_username or not settings.email_to:
        logger.warning("이메일 설정 누락 (SMTP_USERNAME 또는 EMAIL_TO) - 발송 건너뜀")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.email_from or settings.smtp_username
    msg["To"] = ", ".join(settings.email_to)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if settings.smtp_use_tls:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port)

        server.login(settings.smtp_username, settings.smtp_password)
        server.sendmail(settings.smtp_username, settings.email_to, msg.as_string())
        server.quit()
        logger.info(f"이메일 발송 완료: {subject} → {settings.email_to}")
    except Exception as e:
        logger.error(f"이메일 발송 실패: {e}")
        raise


def send_daily_digest(announcements: list, settings):
    """일일 다이제스트 이메일 발송"""
    if not announcements:
        return

    # structured_json 파싱
    processed = []
    for ann in announcements:
        a = dict(ann) if hasattr(ann, "keys") else dict(ann)
        if a.get("structured_json") and isinstance(a["structured_json"], str):
            try:
                a["structured_json"] = json.loads(a["structured_json"])
            except Exception:
                pass
        processed.append(a)

    env = _get_jinja_env()
    tmpl = env.get_template("daily_digest.html")

    count_결정고시 = sum(1 for a in processed if "결정고시" in (a.get("category") or ""))
    count_열람공고 = sum(1 for a in processed if "열람공고" in (a.get("category") or ""))
    count_analyzed = sum(1 for a in processed if a.get("structured_json") and isinstance(a["structured_json"], dict))

    html = tmpl.render(
        report_date=date.today().strftime("%Y년 %m월 %d일"),
        total_new=len(processed),
        count_결정고시=count_결정고시,
        count_열람공고=count_열람공고,
        count_analyzed=count_analyzed,
        announcements=processed,
        admin_email=settings.email_from or settings.smtp_username,
    )

    subject = f"[서울 도시계획 모니터링] {date.today().strftime('%Y-%m-%d')} 신규 {len(processed)}건"
    _send(settings, subject, html)


def send_immediate_alert(announcement: dict, settings):
    """결정고시 즉시 알림 이메일"""
    ann = dict(announcement) if hasattr(announcement, "keys") else dict(announcement)
    if ann.get("structured_json") and isinstance(ann["structured_json"], str):
        try:
            ann["structured_json"] = json.loads(ann["structured_json"])
        except Exception:
            pass

    env = _get_jinja_env()
    tmpl = env.get_template("immediate_alert.html")
    html = tmpl.render(ann=ann)

    district = ann.get("district") or ""
    zone = ann.get("zone_name") or ""
    subject = f"[긴급] 서울시 {ann.get('category', '결정고시')} - {district} {zone}".strip()
    _send(settings, subject, html)
