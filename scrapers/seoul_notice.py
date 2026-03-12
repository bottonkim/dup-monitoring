"""
서울시 공식 고시공고 스크래퍼
www.seoul.go.kr/news/news_notice.do
게시판 내용은 정적 HTML에 포함됨 (#seoul-common-board) — requests로 파싱
"""
import logging
import re
import time

import requests
from bs4 import BeautifulSoup

from .base import ScraperBase, AnnouncementRecord, content_hash

logger = logging.getLogger(__name__)

_LIST_URL = "https://www.seoul.go.kr/news/news_notice.do"
_BASE_URL = "https://www.seoul.go.kr"
_BBS_NO = "277"
_PAGE_PARAM = "curPage"  # 서울시 공고 게시판 페이지 파라미터

_KEYWORDS = [
    "지구단위계획", "정비구역", "특별계획구역",
    "결정고시", "열람공고", "지정고시", "변경고시",
    "재정비촉진지구", "도시계획", "토지거래계약",
]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
}


def _is_relevant(title: str) -> bool:
    return any(kw in title for kw in _KEYWORDS)


def _detect_category(title: str) -> str:
    for cat in ["결정고시", "지정고시", "변경고시", "해제고시", "열람공고", "결정공고"]:
        if cat in title:
            return cat
    return "고시공고"


_FILE_URL_BASE = "https://seoulboard.seoul.go.kr/comm/getFile"

_DETAIL_KEYWORDS = ("건폐율", "용적률", "허용용도", "불허용도", "높이제한", "결정조서",
                    "허용 용도", "불허 용도", "상한용적률")


def fetch_detail_page(url: str, timeout: int = 15) -> dict:
    """서울시 고시공고 상세페이지에서 본문 텍스트 + 첨부 PDF URL 추출.
    반환: {"body": str, "pdf_urls": list[str], "content_quality": str}
    """
    resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "lxml")

    # 본문 텍스트 추출 (#scrabArea)
    body = ""
    scrab = soup.find(id="scrabArea")
    if scrab:
        body = scrab.get_text(separator="\n", strip=True)
    if not body:
        # 폴백: .sib-viw-type-basic
        content_div = soup.select_one(".sib-viw-type-basic")
        if content_div:
            body = content_div.get_text(separator="\n", strip=True)

    # 첨부 PDF URL 추출
    pdf_urls = []
    # 패턴 1: data-url 버튼 (.sib-viw-file 내)
    for btn in soup.select(".sib-viw-file button[data-url]"):
        fname = (btn.get("data-name") or "").lower()
        if fname.endswith(".pdf"):
            data_url = btn.get("data-url", "")
            if data_url:
                full = data_url if data_url.startswith("http") else _BASE_URL + data_url
                pdf_urls.append(full)
    # 패턴 2: getFile 링크
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "getFile" in href and ".pdf" in (a.get_text() or "").lower():
            full = href if href.startswith("http") else _BASE_URL + href
            pdf_urls.append(full)
    # 패턴 3: 직접 .pdf 링크
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".pdf") and href not in pdf_urls:
            full = href if href.startswith("http") else _BASE_URL + href
            pdf_urls.append(full)

    # content_quality 판별
    hits = sum(1 for kw in _DETAIL_KEYWORDS if kw in body) if body else 0
    quality = "detailed" if hits >= 2 else ("summary" if body and len(body) >= 10 else "minimal")

    return {"body": body, "pdf_urls": pdf_urls, "content_quality": quality}


class SeoulNoticeScraper(ScraperBase):
    name = "seoul_notice"

    def fetch(self) -> list[AnnouncementRecord]:
        records = []
        max_pages = min(self.settings.max_pages_per_source, 10)

        for pg in range(1, max_pages + 1):
            try:
                resp = requests.get(
                    _LIST_URL,
                    params={_PAGE_PARAM: pg, "bbsNo": _BBS_NO},
                    headers=_HEADERS,
                    timeout=self.settings.request_timeout,
                )
                resp.raise_for_status()
                resp.encoding = "utf-8"

                row_count, page_records = self._parse_html_with_count(resp.text)
                records.extend(page_records)
                logger.debug(f"서울시 공고 페이지{pg}: 전체{row_count}행, 관련{len(page_records)}건")

                if row_count == 0:
                    break
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"서울시 공고 페이지{pg} 실패: {e}")
                break

        # 상세 페이지 방문하여 본문 + 첨부 PDF 수집
        for rec in records:
            if rec.url and rec.url != _LIST_URL:
                try:
                    detail = fetch_detail_page(rec.url, timeout=self.settings.request_timeout)
                    if detail.get("body"):
                        rec.raw_content = detail["body"][:10000]
                        rec.content_hash = content_hash(rec.title, rec.raw_content)
                    if detail.get("pdf_urls"):
                        rec.pdf_urls = detail["pdf_urls"]
                    time.sleep(0.3)
                except Exception as e:
                    logger.debug(f"상세페이지 실패 ({rec.source_id}): {e}")

        logger.info(f"서울시 공고: {len(records)}건 수집")
        return records

    def _parse_html_with_count(self, html: str) -> tuple:
        """(총 행수, 관련 레코드 목록) 반환 — 총 행수 0이면 마지막 페이지"""
        soup = BeautifulSoup(html, "lxml")
        records = []

        board = soup.find(id="seoul-common-board")
        if not board:
            logger.warning("서울시 공고: #seoul-common-board 미발견")
            return 0, records

        rows = board.select("table tbody tr")
        total_rows = len(rows)

        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 5:
                continue

            # 컬럼: [번호, 제목, 마감일, 담당부서, 작성일, 조회수]
            raw_title = cols[1].get_text(strip=True)
            title = raw_title.replace("파일있음", "").strip()
            if not title or not _is_relevant(title):
                continue

            pub_date = cols[4].get_text(strip=True) if len(cols) > 4 else ""

            # nttNo 추출: javascript:fnTbbsView('453915')
            a_tag = row.find("a")
            ntt_no = ""
            if a_tag:
                href = a_tag.get("href", "")
                m = re.search(r"fnTbbsView\('?(\d+)'?\)", href)
                if m:
                    ntt_no = m.group(1)

            url = (f"{_BASE_URL}/news/news_notice.do?bbsNo={_BBS_NO}&nttNo={ntt_no}"
                   if ntt_no else _LIST_URL)

            pdf_urls = []
            for a in row.find_all("a", href=True):
                h = a["href"]
                if ".pdf" in h.lower():
                    full = h if h.startswith("http") else _BASE_URL + h
                    pdf_urls.append(full)

            source_id = f"notice_{ntt_no}" if ntt_no else f"notice_{content_hash(title + pub_date)}"

            records.append(AnnouncementRecord(
                source=self.name,
                source_id=source_id,
                title=title,
                content_hash=content_hash(title, pub_date),
                category=_detect_category(title),
                district=self._extract_district(title),
                zone_name="",
                published_at=pub_date,
                url=url,
                raw_content=row.get_text(separator=" ", strip=True)[:500],
                pdf_urls=pdf_urls,
            ))

        return total_rows, records

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
