"""
구청 지구단위계획 게시판 조회 모듈
일부 구청은 지구단위계획 전용 게시판을 운영.
구역명으로 검색 → 결정고시/열람공고 반환.
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

_DETAIL_KEYWORDS = ("건폐율", "용적률", "허용용도", "불허용도", "높이제한", "결정조서",
                    "허용 용도", "불허 용도", "상한용적률")

# 구청 지구단위계획/도시계획 전용 게시판 설정
GU_PLANNING_CONFIGS = {
    "강남구": {
        "base_url": "https://www.gangnam.go.kr",
        "list_path": "/board/B_000155/list.do",
        "view_path": "/board/B_000155/{id}/view.do",
        "params": {"mid": "ID05_040201"},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
    },
    "서초구": {
        "base_url": "https://www.seocho.go.kr",
        "list_path": "/site/seocho/ex/bbs/List.do",
        "view_path": "/site/seocho/ex/bbs/View.do",
        "params": {"cbIdx": "292"},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
    },
    "송파구": {
        "base_url": "https://www.songpa.go.kr",
        "list_path": "/eGovernCivil/selectBbsNttList.do",
        "view_path": "/eGovernCivil/selectBbsNttView.do",
        "params": {"bbsNo": "157", "key": "2320"},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
    },
}


def fetch_gu_planning(
    sgg_nm: str,
    zone_keywords: list[str],
    limit: int = 5,
    timeout: int = 15,
) -> list[dict]:
    """
    구청 지구단위계획 전용 게시판에서 구역명 검색.
    미설정 구청은 빈 리스트 반환.
    """
    config = GU_PLANNING_CONFIGS.get(sgg_nm)
    if not config:
        return []

    results = []
    base = config["base_url"]

    for keyword in zone_keywords[:3]:
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
                cols = row.find_all("td")
                if len(cols) < 3:
                    continue

                title_td = cols[1] if len(cols) >= 4 else cols[0]
                a_tag = title_td.find("a")
                if not a_tag:
                    continue

                title = a_tag.get_text(strip=True)
                if not title:
                    continue

                # 날짜 추출
                pub_date = ""
                for td in reversed(cols):
                    text = td.get_text(strip=True)
                    m = re.match(r"\d{4}[-./]\d{2}[-./]\d{2}", text)
                    if m:
                        pub_date = m.group().replace(".", "-").replace("/", "-")
                        break

                # 상세 URL
                detail_url = _build_detail_url(a_tag, config)

                results.append({
                    "source": "gu_planning",
                    "title": title,
                    "url": detail_url,
                    "detail_url": detail_url,
                    "published_at": pub_date,
                    "district": sgg_nm,
                    "category": _detect_category(title),
                    "body": "",
                    "pdf_urls": [],
                    "content_quality": "minimal",
                })

                if len(results) >= limit:
                    break

        except Exception as e:
            logger.debug(f"구청 지구단위 검색 실패 ({sgg_nm}, '{keyword}'): {e}")

        if len(results) >= limit:
            break
        time.sleep(0.3)

    # 상세페이지 방문
    for item in results[:limit]:
        if not item.get("detail_url"):
            continue
        try:
            detail = _fetch_detail(item["detail_url"], config, timeout)
            item["body"] = detail.get("body", "")[:10000]
            item["pdf_urls"] = detail.get("pdf_urls", [])
            item["content_quality"] = _classify_quality(item["body"])
            time.sleep(0.2)
        except Exception as e:
            logger.debug(f"구청 지구단위 상세 실패: {e}")

    return results[:limit]


def _build_detail_url(a_tag, config: dict) -> str:
    """링크에서 상세 URL 구성"""
    base = config["base_url"]
    href = a_tag.get("href", "")
    onclick = a_tag.get("onclick", "")

    if onclick:
        m = re.search(r"['\"](\d+)['\"]", onclick)
        if m:
            ntt_no = m.group(1)
            view_path = config.get("view_path", "")
            if "{id}" in view_path:
                return base + view_path.replace("{id}", ntt_no)
            params = dict(config.get("params", {}))
            params["nttNo"] = ntt_no
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            return base + view_path + "?" + qs

    if href and not href.startswith("javascript"):
        return href if href.startswith("http") else base + href

    if href:
        m = re.search(r"['\"](\d+)['\"]", href)
        if m:
            ntt_no = m.group(1)
            view_path = config.get("view_path", "")
            params = dict(config.get("params", {}))
            params["nttNo"] = ntt_no
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            return base + view_path + "?" + qs

    return ""


def _fetch_detail(url: str, config: dict, timeout: int) -> dict:
    """상세페이지 본문 + PDF 추출"""
    resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "lxml")

    body = ""
    for selector in [".ntt_cn_container", ".post-content", ".bbs-view",
                     ".view_con", ".contents", ".board_view", "#contents"]:
        el = soup.select_one(selector)
        if el:
            body = el.get_text(separator="\n", strip=True)
            if len(body) > 20:
                break

    pdf_urls = []
    base = config.get("base_url", "")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        fname = (a.get_text() or "").strip().lower()
        if ".pdf" in href.lower() or ".pdf" in fname:
            full = href if href.startswith("http") else base + href
            if full not in pdf_urls:
                pdf_urls.append(full)

    return {"body": body, "pdf_urls": pdf_urls}


def _detect_category(title: str) -> str:
    for cat in ["결정고시", "지정고시", "변경고시", "해제고시", "열람공고", "결정공고"]:
        if cat in title:
            return cat
    return "고시공고"


def _classify_quality(text: str) -> str:
    if not text or len(text) < 10:
        return "minimal"
    hits = sum(1 for kw in _DETAIL_KEYWORDS if kw in text)
    return "detailed" if hits >= 2 else "summary"
