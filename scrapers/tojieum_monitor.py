"""
토지이음 (eum.go.kr) 고시정보 모니터링 스크래퍼
URL: https://www.eum.go.kr/web/gs/gv/gvGosiList.jsp
인코딩: EUC-KR
"""
import logging
import re
import time

import requests
from bs4 import BeautifulSoup

from .base import ScraperBase, AnnouncementRecord, content_hash

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.eum.go.kr"
_LIST_URL = f"{_BASE_URL}/web/gs/gv/gvGosiList.jsp"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": _BASE_URL,
}

_KEYWORDS = [
    "지구단위계획", "정비구역", "특별계획구역",
    "결정고시", "열람공고", "지정고시", "변경고시",
    "재정비촉진지구", "토지거래계약",
]

_SEOUL_SGG_CD = "11"  # 서울특별시 시도코드


def _is_relevant(title: str) -> bool:
    return any(kw in title for kw in _KEYWORDS)


class TojieumMonitorScraper(ScraperBase):
    name = "tojieum"

    def fetch(self) -> list[AnnouncementRecord]:
        records = []
        try:
            records.extend(self._fetch_notice_list())
        except Exception as e:
            logger.warning(f"토지이음 목록 스크래핑 실패: {e}")

        logger.info(f"토지이음: {len(records)}건 수집")
        return records

    def _fetch_notice_list(self) -> list[AnnouncementRecord]:
        records = []

        for page in range(1, self.settings.max_pages_per_source + 1):
            params = {
                "pageNo": page,
                "pageUnit": 20,
                "selSggCd": _SEOUL_SGG_CD,
            }
            try:
                resp = requests.get(
                    _LIST_URL, params=params, headers=_HEADERS,
                    timeout=self.settings.request_timeout
                )
                resp.raise_for_status()
                resp.encoding = resp.apparent_encoding or "euc-kr"
                soup = BeautifulSoup(resp.text, "lxml")

                page_records = self._parse_list(soup)
                records.extend(page_records)

                if len(page_records) == 0:
                    break
                time.sleep(1.0)

            except Exception as e:
                logger.warning(f"토지이음 페이지{page} 실패: {e}")
                break

        return records

    def _parse_list(self, soup: BeautifulSoup) -> list[AnnouncementRecord]:
        records = []

        for row in soup.select("table tbody tr"):
            cols = row.find_all("td")
            if len(cols) < 2:
                continue

            link = row.find("a")
            if link:
                title = re.sub(r"\s+", " ", link.get_text(strip=True)).strip()
            elif len(cols) > 1:
                title = re.sub(r"\s+", " ", cols[1].get_text(strip=True)).strip()
            else:
                continue

            if not title or not _is_relevant(title):
                continue

            href = ""
            if link:
                raw_href = link.get("href", "")
                if raw_href.startswith("http"):
                    href = raw_href
                elif raw_href:
                    href = _BASE_URL + "/web/gs/gv/" + raw_href.lstrip("/")

            # 날짜: 첫 번째 컬럼이 보통 날짜
            pub_date = ""
            for col in cols:
                text = col.get_text(strip=True)
                cleaned = text.replace(".", "-").replace("/", "-")
                if len(cleaned) == 10 and cleaned[4] == "-" and cleaned[7] == "-":
                    pub_date = cleaned
                    break

            pdf_urls = []
            for a in row.find_all("a", href=True):
                h = a["href"]
                if ".pdf" in h.lower() or "download" in h.lower():
                    full = h if h.startswith("http") else _BASE_URL + h
                    pdf_urls.append(full)

            source_id = f"tojieum_{content_hash(title + pub_date)}"

            records.append(AnnouncementRecord(
                source=self.name,
                source_id=source_id,
                title=title,
                content_hash=content_hash(title, pub_date),
                category=self._detect_category(title),
                district=self._extract_district(title),
                zone_name="",
                published_at=pub_date,
                url=href,
                raw_content=row.get_text(separator=" ", strip=True)[:500],
                pdf_urls=pdf_urls,
            ))

        return records

    def _detect_category(self, title: str) -> str:
        for cat in ["결정고시", "지정고시", "변경고시", "해제고시", "열람공고"]:
            if cat in title:
                return cat
        return "고시"

    def _extract_district(self, text: str) -> str:
        districts = [
            "강남구", "강동구", "강북구", "강서구", "관악구", "광진구", "구로구",
            "금천구", "노원구", "도봉구", "동대문구", "동작구", "마포구", "서대문구",
            "서초구", "성동구", "성북구", "송파구", "양천구", "영등포구", "용산구",
            "은평구", "종로구", "중구", "중랑구",
        ]
        for d in districts:
            if d in text:
                return d
        return ""
