"""
서울시 고시공고 실시간 검색 (lookup 모듈)
주소 검색 시 해당 구역명으로 www.seoul.go.kr 고시공고를 실시간 조회.
백그라운드 스크래퍼(scrapers/seoul_notice.py)와 달리, 특정 키워드로 검색 후
상세페이지 본문 + 첨부 PDF URL까지 수집.
"""
import logging
import time
import re

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_LIST_URL = "https://www.seoul.go.kr/news/news_notice.do"
_BASE_URL = "https://www.seoul.go.kr"
_BBS_NO = "277"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

_RELEVANCE_KEYWORDS = [
    "지구단위계획", "정비구역", "특별계획구역",
    "결정고시", "열람공고", "지정고시", "변경고시",
    "재정비촉진지구", "도시계획",
]


def search_seoul_announcements(
    zone_keywords: list[str],
    limit: int = 5,
    timeout: int = 15,
) -> list[dict]:
    """
    서울시 고시공고에서 구역명 키워드로 실시간 검색.
    각 키워드로 검색 → 관련 결과의 상세페이지 방문 → 본문 + 첨부 PDF 수집.

    반환: [{"title", "url", "ntt_no", "published_at", "district", "category",
            "body", "pdf_urls", "content_quality", "source"}, ...]
    """
    results = []
    seen_ntt = set()

    for keyword in zone_keywords:
        if not keyword or len(keyword) < 2:
            continue
        try:
            page_results = _search_keyword(keyword, timeout=timeout)
            for item in page_results:
                ntt = item.get("ntt_no", "")
                if ntt in seen_ntt:
                    continue
                seen_ntt.add(ntt)
                results.append(item)
                if len(results) >= limit:
                    break
        except Exception as e:
            logger.warning(f"서울시 고시 검색 실패 ('{keyword}'): {e}")

        if len(results) >= limit:
            break
        time.sleep(0.3)

    # 상세페이지 방문하여 본문 + PDF 수집 (상위 건만)
    for item in results[:limit]:
        if not item.get("url"):
            continue
        try:
            from scrapers.seoul_notice import fetch_detail_page
            detail = fetch_detail_page(item["url"], timeout=timeout)
            item["body"] = detail.get("body", "")[:10000]
            item["pdf_urls"] = detail.get("pdf_urls", [])
            item["content_quality"] = detail.get("content_quality", "summary")
            time.sleep(0.2)
        except Exception as e:
            logger.debug(f"서울시 고시 상세페이지 실패 ({item.get('ntt_no')}): {e}")
            item["body"] = item.get("body", "")
            item["pdf_urls"] = item.get("pdf_urls", [])
            item["content_quality"] = "minimal"

    return results[:limit]


def _search_keyword(keyword: str, timeout: int = 15) -> list[dict]:
    """단일 키워드로 서울시 고시공고 검색 (목록 페이지 1~2페이지)"""
    results = []

    for page in range(1, 3):
        resp = requests.get(
            _LIST_URL,
            params={
                "bbsNo": _BBS_NO,
                "curPage": page,
                "srchKey": "sj",  # 제목 검색
                "srchText": keyword,
            },
            headers=_HEADERS,
            timeout=timeout,
        )
        resp.raise_for_status()
        resp.encoding = "utf-8"

        soup = BeautifulSoup(resp.text, "lxml")
        board = soup.find(id="seoul-common-board")
        if not board:
            break

        rows = board.select("table tbody tr")
        if not rows:
            break

        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 5:
                continue

            raw_title = cols[1].get_text(strip=True)
            title = raw_title.replace("파일있음", "").strip()
            if not title:
                continue

            # 관련성 확인: 도시계획 관련 키워드 포함 여부
            if not any(kw in title for kw in _RELEVANCE_KEYWORDS):
                continue

            pub_date = cols[4].get_text(strip=True) if len(cols) > 4 else ""

            # nttNo 추출
            a_tag = row.find("a")
            ntt_no = ""
            if a_tag:
                href = a_tag.get("href", "")
                m = re.search(r"fnTbbsView\('?(\d+)'?\)", href)
                if m:
                    ntt_no = m.group(1)

            url = (f"{_BASE_URL}/news/news_notice.do?bbsNo={_BBS_NO}&nttNo={ntt_no}"
                   if ntt_no else "")

            district = _extract_district(title)
            category = _detect_category(title)

            results.append({
                "source": "seoul_notice_search",
                "ntt_no": ntt_no,
                "title": title,
                "url": url,
                "published_at": pub_date,
                "district": district,
                "category": category,
                "body": "",
                "pdf_urls": [],
                "content_quality": "minimal",
            })

        time.sleep(0.3)

    return results


def _detect_category(title: str) -> str:
    for cat in ["결정고시", "지정고시", "변경고시", "해제고시", "열람공고", "결정공고"]:
        if cat in title:
            return cat
    if "고시" in title:
        return "고시"
    if "공고" in title:
        return "공고"
    return ""


_DISTRICTS = [
    "강남구", "강동구", "강북구", "강서구", "관악구", "광진구", "구로구",
    "금천구", "노원구", "도봉구", "동대문구", "동작구", "마포구", "서대문구",
    "서초구", "성동구", "성북구", "송파구", "양천구", "영등포구", "용산구",
    "은평구", "종로구", "중구", "중랑구",
]


def _extract_district(text: str) -> str:
    for d in _DISTRICTS:
        if d in text:
            return d
    return ""
