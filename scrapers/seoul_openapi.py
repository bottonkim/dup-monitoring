"""
서울 열린데이터광장 REST API 스크래퍼
- OA-20283: 도시계획 결정고시
- OA-2482: 고시공고 정보
"""
import logging
import time
from pathlib import Path

import requests

from .base import ScraperBase, AnnouncementRecord, content_hash

logger = logging.getLogger(__name__)

_BASE = "http://openapi.seoul.go.kr:8088"

_SERVICES = [
    # (서비스명, 카테고리 힌트)
    ("ListNewsNotice", "고시공고"),       # OA-2482 서울시 고시공고 정보
    ("upisAnnouncement", "결정고시"),    # OA-20283 서울시 도시계획 결정고시 정보
]

_KEYWORDS = [
    "지구단위계획", "정비구역", "특별계획구역",
    "결정고시", "열람공고", "지정고시", "변경고시",
    "재정비촉진지구", "도시계획", "토지거래계약",
]

_DISTRICTS = [
    "강남구", "강동구", "강북구", "강서구", "관악구", "광진구", "구로구",
    "금천구", "노원구", "도봉구", "동대문구", "동작구", "마포구", "서대문구",
    "서초구", "성동구", "성북구", "송파구", "양천구", "영등포구", "용산구",
    "은평구", "종로구", "중구", "중랑구",
]


def _detect_category(title: str) -> str:
    for cat in ["결정고시", "지정고시", "변경고시", "해제고시", "열람공고", "결정공고"]:
        if cat in title:
            return cat
    if "고시" in title:
        return "고시"
    if "공고" in title:
        return "공고"
    return ""


def _extract_district(text: str) -> str:
    for d in _DISTRICTS:
        if d in text:
            return d
    return ""


def _is_relevant(title: str) -> bool:
    return any(kw in title for kw in _KEYWORDS)


def _normalize_date(raw: str) -> str:
    """YYYYMMDDHHmmss or YYYY-MM-DDTHH:mm:ss.sss → YYYY-MM-DD"""
    if not raw:
        return ""
    raw = raw.strip()
    if len(raw) >= 8 and raw[4:5].isdigit():  # YYYYMMDDxxx
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw[:10]  # already YYYY-MM-DD or similar


class SeoulOpenAPIScraper(ScraperBase):
    name = "seoul_openapi"

    def fetch(self) -> list[AnnouncementRecord]:
        records = []
        api_key = self.settings.seoul_api_key
        if not api_key:
            logger.warning("SEOUL_API_KEY 미설정 - seoul_openapi 스크래퍼 건너뜀")
            return records

        for service_name, category_hint in _SERVICES:
            try:
                fetched = self._fetch_service(api_key, service_name, category_hint)
                records.extend(fetched)
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"{service_name} 조회 실패: {e}")

        return records

    def _fetch_service(self, api_key: str, service_name: str, category_hint: str) -> list[AnnouncementRecord]:
        records = []
        page_size = 100
        max_pages = self.settings.max_pages_per_source

        for page in range(max_pages):
            start = page * page_size + 1
            end = start + page_size - 1
            url = f"{_BASE}/{api_key}/json/{service_name}/{start}/{end}"

            try:
                resp = requests.get(url, timeout=self.settings.request_timeout)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.warning(f"{service_name} 페이지{page+1} 요청 실패: {e}")
                break

            # 응답 구조 파싱 (서울 Open API 공통 구조)
            service_data = data.get(service_name, {})
            if not service_data:
                break

            err = service_data.get("RESULT", {})
            if err.get("CODE", "") not in ("INFO-000", ""):
                logger.warning(f"{service_name} API 오류: {err}")
                break

            rows = service_data.get("row", [])
            if not rows:
                break

            total = int(service_data.get("list_total_count", 0))

            for item in rows:
                # ListNewsNotice: TITLE, BOARD_ID, CREATE_DATE, FILE_URL
                # upisAnnouncement: TTL, ANCMNT_MNG_CD, ANCMNT_YMD
                title = (item.get("TTL") or item.get("TITLE") or "").strip()
                if not title or not _is_relevant(title):
                    continue

                raw_id = (item.get("BOARD_ID") or item.get("ANCMNT_MNG_CD") or "")
                source_id = f"{service_name}_{raw_id}" if raw_id else f"{service_name}_{content_hash(title)}"

                pub_date = (item.get("CREATE_DATE") or item.get("ANCMNT_YMD") or "")

                # FILE_URL은 ListNewsNotice 첨부파일, upisAnnouncement는 URL 없음
                file_url = item.get("FILE_URL") or ""
                pdf_urls = []
                if file_url and file_url.lower().endswith(".pdf"):
                    pdf_urls = [file_url]
                url_val = file_url

                records.append(AnnouncementRecord(
                    source=self.name,
                    source_id=source_id,
                    title=title,
                    content_hash=content_hash(title, str(item)),
                    category=_detect_category(title) or category_hint,
                    district=_extract_district(title),
                    zone_name="",
                    published_at=_normalize_date(pub_date),
                    url=url_val,
                    raw_content=str(item)[:2000],
                    pdf_urls=pdf_urls,
                ))

            logger.debug(f"{service_name} 페이지{page+1}: {len(rows)}행 조회, 관련 {len(records)}건")
            if end >= total:
                break
            time.sleep(0.3)

        return records
