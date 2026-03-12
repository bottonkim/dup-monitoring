"""
구청 구보 조회 모듈
구보(區報)는 구청에서 발행하는 공보로, 고시 전문(결정조서 포함)이 게재됨.
구보 PDF(5-30MB)에서 구역명 검색 → 관련 페이지 추출.
"""
import logging
import re
import time

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

_RELEVANCE_KEYWORDS = [
    "지구단위계획", "정비구역", "특별계획구역",
    "결정고시", "열람공고", "지정고시", "변경고시",
    "재정비촉진지구", "도시계획",
]

# 구청 구보 게시판 설정 (구보는 고시공고와 별도 게시판)
GU_GAZETTE_CONFIGS = {
    "성동구": {
        "base_url": "https://www.sd.go.kr",
        "list_path": "/main/selectBbsNttList.do",
        "view_path": "/main/selectBbsNttView.do",
        "params": {"bbsNo": "182", "key": "1471"},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
    },
    "강남구": {
        "base_url": "https://www.gangnam.go.kr",
        "list_path": "/board/B_000058/list.do",
        "view_path": "/board/B_000058/{id}/view.do",
        "params": {"mid": "ID03_010102"},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
    },
    "마포구": {
        "base_url": "https://www.mapo.go.kr",
        "list_path": "/site/main/nPortal/list",
        "view_path": "/site/main/nPortal/detail",
        "params": {"pageCode": "mapogonbo"},
        "page_param": "cp",
        "search_param": "query",
    },
}


def fetch_gu_gazette(
    sgg_nm: str,
    zone_keywords: list[str],
    limit: int = 3,
    timeout: int = 15,
) -> list[dict]:
    """
    구청 구보 게시판에서 최근 호 조회 → 제목에 구역명 포함 여부 확인.
    구보 PDF는 용량이 커서 본문만 우선 검색, PDF URL만 수집.
    미설정 구청은 빈 리스트 반환.
    """
    config = GU_GAZETTE_CONFIGS.get(sgg_nm)
    if not config:
        return []

    results = []
    base = config["base_url"]

    for keyword in zone_keywords[:2]:
        if not keyword or len(keyword) < 2:
            continue
        try:
            params = dict(config.get("params", {}))
            params[config.get("page_param", "pageIndex")] = 1
            params[config.get("search_param", "searchKeyword")] = keyword

            url = base + config["list_path"]
            resp = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
            resp.raise_for_status()
            resp.encoding = "utf-8"

            soup = BeautifulSoup(resp.text, "lxml")
            table = soup.select_one("table")
            if not table:
                continue

            rows = table.select("tbody tr")
            if not rows:
                rows = table.find_all("tr")[1:]

            for row in rows:
                a_tag = row.find("a")
                if not a_tag:
                    continue
                title = a_tag.get_text(strip=True)
                if not title:
                    continue
                # 구보 제목은 "제XXXX호" 패턴이 많음 — 키워드 매칭은 덜 엄격
                if keyword not in title and not any(kw in title for kw in _RELEVANCE_KEYWORDS):
                    continue

                # 날짜 추출
                pub_date = ""
                for td in row.find_all("td"):
                    text = td.get_text(strip=True)
                    m = re.match(r"\d{4}[-./]\d{2}[-./]\d{2}", text)
                    if m:
                        pub_date = m.group().replace(".", "-").replace("/", "-")
                        break

                # PDF URL (구보는 대부분 PDF 첨부)
                pdf_urls = []
                for a in row.find_all("a", href=True):
                    href = a["href"]
                    if ".pdf" in href.lower() or "download" in href.lower():
                        full = href if href.startswith("http") else base + href
                        pdf_urls.append(full)

                results.append({
                    "source": "gu_gazette",
                    "title": title,
                    "url": "",
                    "published_at": pub_date,
                    "district": sgg_nm,
                    "category": "구보",
                    "body": "",
                    "pdf_urls": pdf_urls,
                    "content_quality": "minimal",
                })

                if len(results) >= limit:
                    break

        except Exception as e:
            logger.debug(f"구청 구보 검색 실패 ({sgg_nm}, '{keyword}'): {e}")

        if len(results) >= limit:
            break
        time.sleep(0.3)

    return results[:limit]
