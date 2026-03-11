"""
서울시보 (서울특별시 공보) 스크래퍼
URL: https://event.seoul.go.kr/seoulsibo/list.do
각 호별 상세페이지에서 개별 고시/공고 제목 추출
"""
import logging
import re
import time

import requests
from bs4 import BeautifulSoup

from .base import ScraperBase, AnnouncementRecord, content_hash

logger = logging.getLogger(__name__)

_BASE_URL = "https://event.seoul.go.kr"
_LIST_URL = f"{_BASE_URL}/seoulsibo/list.do"
_DETAIL_URL = f"{_BASE_URL}/seoulsibo/detailview.do"

_KEYWORDS = [
    "지구단위계획", "정비구역", "특별계획구역",
    "결정고시", "열람공고", "지정고시", "변경고시",
    "재정비촉진지구", "도시계획", "토지거래계약",
]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": _LIST_URL,
}

# 개별 공고 제목 패턴: 제YYYY-NNN호  제목
_NOTICE_PATTERN = re.compile(r"(제\d{4}-\d+호)\s{1,4}(.{5,120})")


def _is_relevant(title: str) -> bool:
    return any(kw in title for kw in _KEYWORDS)


def _detect_category(title: str) -> str:
    for cat in ["결정고시", "지정고시", "변경고시", "해제고시", "열람공고", "결정공고"]:
        if cat in title:
            return cat
    if "고시" in title:
        return "고시"
    if "공고" in title:
        return "공고"
    return "고시공고"


class SeoulGazetteScraper(ScraperBase):
    name = "seoul_gazette"

    def fetch(self) -> list[AnnouncementRecord]:
        records = []
        sess = requests.Session()
        sess.headers.update(_HEADERS)

        try:
            sess.get(_LIST_URL, timeout=self.settings.request_timeout)
        except Exception as e:
            logger.warning(f"서울시보 세션 초기화 실패: {e}")
            return records

        max_pages = self.settings.max_pages_per_source

        for page in range(1, max_pages + 1):
            try:
                issues = self._get_issue_list(sess, page)
                if not issues:
                    break
                for seq, issue_no, pub_date in issues:
                    recs = self._fetch_issue(sess, seq, issue_no, pub_date)
                    records.extend(recs)
                    time.sleep(0.3)
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"서울시보 페이지{page} 실패: {e}")
                break

        logger.info(f"서울시보: {len(records)}건 수집")
        return records

    def _get_issue_list(self, sess, page: int) -> list[tuple]:
        resp = sess.post(_LIST_URL, data={"pageIndex": page, "pageCnt": 20},
                         timeout=self.settings.request_timeout)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "lxml")

        issues = []
        for row in soup.select("table tbody tr"):
            a = row.find("a")
            if not a:
                continue
            m = re.search(r"goView\('(\d+)'\)", a.get("href", ""))
            if not m:
                continue
            seq = m.group(1)
            cols = row.find_all("td")
            issue_no = cols[0].get_text(strip=True) if cols else ""
            raw_date = cols[1].get_text(strip=True) if len(cols) > 1 else ""
            pub_date = raw_date.replace(".", "-")
            if len(pub_date) > 10:
                pub_date = pub_date[:10]
            issues.append((seq, issue_no, pub_date))
        return issues

    def _fetch_issue(self, sess, seq: str, issue_no: str, pub_date: str) -> list[AnnouncementRecord]:
        records = []
        try:
            resp = sess.post(_DETAIL_URL, data={"cn_seq": seq},
                             timeout=self.settings.request_timeout)
            resp.raise_for_status()
            resp.encoding = "utf-8"
        except Exception as e:
            logger.debug(f"서울시보 seq={seq} 요청 실패: {e}")
            return records

        soup = BeautifulSoup(resp.text, "lxml")
        content = soup.find("div", class_="content")
        if not content:
            return records

        text = content.get_text(separator="\n", strip=True)

        for m in _NOTICE_PATTERN.finditer(text):
            notice_no = m.group(1)
            title = m.group(2).strip()
            if not title or not _is_relevant(title):
                continue

            source_id = f"gazette_{seq}_{notice_no.replace(' ', '')}"

            records.append(AnnouncementRecord(
                source=self.name,
                source_id=source_id,
                title=title,
                content_hash=content_hash(title, pub_date),
                category=_detect_category(title),
                district=self._extract_district(title),
                zone_name="",
                published_at=pub_date,
                url=_LIST_URL,
                raw_content=f"{issue_no} {notice_no} {title}",
                pdf_urls=[],
            ))

        return records

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
