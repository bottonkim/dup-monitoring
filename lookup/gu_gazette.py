"""
구청 구보 조회 모듈
구보(區報)는 구청에서 발행하는 공보로, 고시 전문(결정조서 포함)이 게재됨.
최근 구보 목록 → 상세 페이지 본문에서 구역명 검색 → 매칭된 구보만 반환.
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

_DETAIL_KEYWORDS = ("건폐율", "용적률", "허용용도", "불허용도", "높이제한", "결정조서")

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
    구청 구보 게시판에서 최근 호 조회 → 상세 페이지 본문에서 구역명 검색.
    구보 제목은 "제XXXX호" 형식이라 제목 매칭 불가 → 본문 매칭.
    미설정 구청은 빈 리스트 반환.
    """
    config = GU_GAZETTE_CONFIGS.get(sgg_nm)
    if not config:
        return []

    base = config["base_url"]

    # 1단계: 최근 구보 목록 수집 (키워드 필터 없이)
    candidates = _fetch_recent_list(config, base, timeout, max_items=6)
    if not candidates:
        return []

    # 2단계: 상세 페이지 본문에서 구역명 검색
    results = []
    for cand in candidates:
        if len(results) >= limit:
            break
        try:
            detail = _fetch_detail(cand["detail_url"], base, timeout)
            body = detail.get("body", "")

            # 구역명 키워드 매칭
            matched_kw = None
            for kw in zone_keywords:
                if kw and len(kw) >= 2 and kw in body:
                    matched_kw = kw
                    break

            if not matched_kw:
                continue

            # content_quality 판별
            quality = "summary"
            if sum(1 for dk in _DETAIL_KEYWORDS if dk in body) >= 2:
                quality = "detailed"

            results.append({
                "source": "gu_gazette",
                "title": cand["title"],
                "url": cand["detail_url"],
                "published_at": cand["pub_date"],
                "district": sgg_nm,
                "category": "구보",
                "body": body[:3000],
                "pdf_urls": detail.get("pdf_urls", []),
                "content_quality": quality,
                "matched_keyword": matched_kw,
            })

        except Exception as e:
            logger.debug(f"구보 상세 조회 실패 ({sgg_nm}, {cand['title']}): {e}")
        time.sleep(0.3)

    if results:
        logger.info(f"구보 검색 ({sgg_nm}): {len(candidates)}호 중 {len(results)}건 매칭")
    return results[:limit]


def _fetch_recent_list(config: dict, base: str, timeout: int, max_items: int = 6) -> list[dict]:
    """구보 목록 페이지에서 최근 N건 수집 (필터 없이)"""
    try:
        params = dict(config.get("params", {}))
        params[config.get("page_param", "pageIndex")] = 1

        url = base + config["list_path"]
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
        resp.raise_for_status()
        resp.encoding = "utf-8"

        soup = BeautifulSoup(resp.text, "lxml")
        table = soup.select_one("table")
        if not table:
            return []

        rows = table.select("tbody tr")
        if not rows:
            rows = table.find_all("tr")[1:]

        candidates = []
        for row in rows:
            a_tag = row.find("a")
            if not a_tag:
                continue
            title = a_tag.get_text(strip=True)
            if not title:
                continue

            # 상세 페이지 URL 구성
            detail_url = _build_detail_url(a_tag, config, base)
            if not detail_url:
                continue

            # 날짜 추출
            pub_date = ""
            for td in row.find_all("td"):
                text = td.get_text(strip=True)
                m = re.match(r"\d{4}[-./]\d{2}[-./]\d{2}", text)
                if m:
                    pub_date = m.group().replace(".", "-").replace("/", "-")
                    break

            candidates.append({
                "title": title,
                "detail_url": detail_url,
                "pub_date": pub_date,
            })

            if len(candidates) >= max_items:
                break

        return candidates

    except Exception as e:
        logger.debug(f"구보 목록 조회 실패: {e}")
        return []


def _build_detail_url(a_tag, config: dict, base: str) -> str:
    """링크 태그에서 상세 페이지 URL 구성"""
    href = a_tag.get("href", "")

    # onclick에서 nttNo 추출 (eGov BBS 패턴)
    onclick = a_tag.get("onclick", "")
    ntt_match = re.search(r"nttNo[=,]\s*['\"]?(\d+)", onclick)
    if not ntt_match:
        ntt_match = re.search(r"fn_detail\(['\"]?(\d+)", onclick)
    if not ntt_match:
        ntt_match = re.search(r"nttNo=(\d+)", href)

    if ntt_match:
        ntt_no = ntt_match.group(1)
        view_path = config.get("view_path", "")
        params = dict(config.get("params", {}))
        if "{id}" in view_path:
            return base + view_path.replace("{id}", ntt_no)
        # eGov BBS: /main/selectBbsNttView.do?bbsNo=182&nttNo=XXX
        param_str = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{base}{view_path}?{param_str}&nttNo={ntt_no}"

    # 직접 href 사용
    if href and href != "#":
        return href if href.startswith("http") else base + href

    return ""


def _fetch_detail(url: str, base: str, timeout: int = 15) -> dict:
    """구보 상세 페이지에서 본문 텍스트 + PDF URL 추출"""
    if not url:
        return {"body": "", "pdf_urls": []}

    resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "lxml")

    # 본문 추출 (다양한 선택자)
    body = ""
    for selector in [".ntt_cn_container", ".post-content", ".bbs-view", ".view_con",
                     ".contents", ".board_view", "#contents", ".cn_body"]:
        el = soup.select_one(selector)
        if el:
            body = el.get_text(separator="\n", strip=True)
            if len(body) > 20:
                break

    # PDF URL 추출
    pdf_urls = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        fname = (a.get_text() or "").strip().lower()
        if ".pdf" in href.lower() or ".pdf" in fname:
            full = href if href.startswith("http") else base + href
            if full not in pdf_urls:
                pdf_urls.append(full)

    return {"body": body, "pdf_urls": pdf_urls}
