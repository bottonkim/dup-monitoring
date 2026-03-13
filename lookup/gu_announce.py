"""
구청 고시공고 실시간 조회 모듈
주소 검색 시 해당 구청 웹사이트에서 구역명으로 고시공고를 실시간 검색.
구청별 CMS 플랫폼에 맞는 파서 사용.
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

# ---------------------------------------------------------------------------
# 구청별 설정
# ---------------------------------------------------------------------------

GU_CONFIGS = {
    "성동구": {
        "platform": "egov_bbs",
        "base_url": "https://www.sd.go.kr",
        "list_path": "/main/selectBbsNttList.do",
        "view_path": "/main/selectBbsNttView.do",
        "params": {"bbsNo": "184", "key": "1473"},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
        "search_type_param": "searchCtgry",
        "search_type_value": "",  # 전체
    },
    "강남구": {
        "platform": "gangnam_board",
        "base_url": "https://www.gangnam.go.kr",
        "list_path": "/board/B_000060/list.do",
        "view_path": "/board/B_000060/{id}/view.do",
        "params": {"mid": "ID03_010104"},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
        "search_type_param": "searchCondition",
        "search_type_value": "1",  # 제목
    },
    "마포구": {
        "platform": "asa_portal",
        "base_url": "https://www.mapo.go.kr",
        "list_path": "/site/main/nPortal/list",
        "view_path": "/site/main/nPortal/detail",
        "params": {},
        "page_param": "cp",
        "search_param": "query",
    },
    "강동구": {
        "platform": "egov_bbs",
        "base_url": "https://www.gangdong.go.kr",
        "list_path": "/web/newPortal/selectBbsNttList.do",
        "view_path": "/web/newPortal/selectBbsNttView.do",
        "params": {"bbsNo": "267", "key": "1577"},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
    },
    "송파구": {
        "platform": "egov_bbs",
        "base_url": "https://www.songpa.go.kr",
        "list_path": "/eGovernCivil/selectBbsNttList.do",
        "view_path": "/eGovernCivil/selectBbsNttView.do",
        "params": {"bbsNo": "121", "key": "2284"},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
    },
    "서초구": {
        "platform": "egov_bbs",
        "base_url": "https://www.seocho.go.kr",
        "list_path": "/site/seocho/ex/bbs/List.do",
        "view_path": "/site/seocho/ex/bbs/View.do",
        "params": {"cbIdx": "256"},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
    },
    "영등포구": {
        "platform": "egov_bbs",
        "base_url": "https://www.ydp.go.kr",
        "list_path": "/www/selectBbsNttList.do",
        "view_path": "/www/selectBbsNttView.do",
        "params": {"bbsNo": "102", "key": "1060"},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
    },
    "용산구": {
        "platform": "egov_bbs",
        "base_url": "https://www.yongsan.go.kr",
        "list_path": "/portal/bbs/selectBbsNttList.do",
        "view_path": "/portal/bbs/selectBbsNttView.do",
        "params": {"bbsNo": "62", "key": "1440"},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
    },
    "종로구": {
        "platform": "egov_bbs",
        "base_url": "https://www.jongno.go.kr",
        "list_path": "/portal/bbs/selectBbsNttList.do",
        "view_path": "/portal/bbs/selectBbsNttView.do",
        "params": {"bbsNo": "38", "key": "1582"},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
    },
    # --- 추가 구청 (eGovFrame BBS) ---
    "구로구": {
        "platform": "egov_bbs",
        "base_url": "https://www.guro.go.kr",
        "list_path": "/www/selectBbsNttList.do",
        "view_path": "/www/selectBbsNttView.do",
        "params": {"bbsNo": "663", "key": "1791"},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
    },
    "금천구": {
        "platform": "egov_bbs",
        "base_url": "https://www.geumcheon.go.kr",
        "list_path": "/portal/selectBbsNttList.do",
        "view_path": "/portal/selectBbsNttView.do",
        "params": {"bbsNo": "156", "key": "2386"},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
    },
    # --- 추가 구청 (포탈 BBS — 강남구형) ---
    "광진구": {
        "platform": "gangnam_board",
        "base_url": "https://www.gwangjin.go.kr",
        "list_path": "/portal/bbs/B0000003/list.do",
        "view_path": "/portal/bbs/B0000003/{id}/view.do",
        "params": {"menuNo": "200192"},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
        "search_type_param": "searchCondition",
        "search_type_value": "1",
    },
    "동작구": {
        "platform": "gangnam_board",
        "base_url": "https://www.dongjak.go.kr",
        "list_path": "/portal/bbs/B0001297/list.do",
        "view_path": "/portal/bbs/B0001297/{id}/view.do",
        "params": {"menuNo": "201317"},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
        "search_type_param": "searchCondition",
        "search_type_value": "1",
    },
    # --- 추가 구청 (관악구 bbsNew) ---
    "관악구": {
        "platform": "gwanak_bbs",
        "base_url": "https://www.gwanak.go.kr",
        "list_path": "/site/gwanak/ex/bbsNew/List.do",
        "view_path": "/site/gwanak/ex/bbsNew/View.do",
        "params": {"typeCode": "1"},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
    },
    # --- 추가 구청 (강서구 커스텀) ---
    "강서구": {
        "platform": "gangseo_custom",
        "base_url": "https://www.gangseo.seoul.kr",
        "list_path": "/gs040301",
        "view_path": "/gs040301/view",
        "params": {},
        "page_param": "curPage",
        "search_param": "srchText",
    },
    # --- 추가 구청 (eminwon 연계) ---
    "동대문구": {
        "platform": "eminwon",
        "base_url": "https://www.ddm.go.kr",
        "list_path": "/www/selectEminwonWebList.do",
        "view_path": "/www/selectEminwonWebView.do",
        "params": {"key": "3291", "searchNotAncmtSeCode": "01,02,04,05,06,07"},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
    },
    "성북구": {
        "platform": "eminwon",
        "base_url": "https://www.sb.go.kr",
        "list_path": "/www/selectEminwonList.do",
        "view_path": "/www/selectEminwonView.do",
        "params": {"key": "6977", "notAncmtSeCode": "01"},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
    },
    "은평구": {
        "platform": "eminwon",
        "base_url": "https://www.ep.go.kr",
        "list_path": "/www/selectEminwonList.do",
        "view_path": "/www/selectEminwonView.do",
        "params": {"key": "754"},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
    },
    # --- 추가 구청 (노원구 BD_ board) ---
    "노원구": {
        "platform": "nowon_bbs",
        "base_url": "https://www.nowon.kr",
        "list_path": "/www/user/bbs/BD_selectBbsList.do",
        "view_path": "/www/user/bbs/BD_selectBbs.do",
        "params": {"q_bbsCode": "1003", "q_estnColumn1": "11", "q_clCode": "0"},
        "page_param": "q_currPage",
        "search_param": "q_searchVal",
    },
    # --- 추가 구청 (도봉구 ASP) ---
    "도봉구": {
        "platform": "dobong_asp",
        "base_url": "https://www.dobong.go.kr",
        "list_path": "/wdb_dev/gosigong_go/default.asp",
        "view_path": "/wdb_dev/gosigong_go/detail.asp",
        "params": {},
        "page_param": "intPage",
        "search_param": "keyword",
    },
    # --- 강북구 (eminwon 서브도메인) ---
    "강북구": {
        "platform": "eminwon_sub",
        "base_url": "https://eminwon.gangbuk.go.kr",
        "list_path": "/emwp/jsp/ofr/OfrNotAncmtLSub.jsp",
        "view_path": "/emwp/jsp/ofr/OfrNotAncmtLSub.jsp",
        "params": {"not_ancmt_se_code": "01,02,04", "list_gubun": "Y"},
        "page_param": "pageIndex",
        "search_param": "not_ancmt_sj",
    },
    # --- 중랑구 (portal BBS — 강남구형) ---
    "중랑구": {
        "platform": "gangnam_board",
        "base_url": "https://www.jungnang.go.kr",
        "list_path": "/portal/bbs/list/B0000117.do",
        "view_path": "/portal/bbs/view/B0000117/{id}.do",
        "params": {"menuNo": "200475"},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
        "search_type_param": "searchCondition",
        "search_type_value": "1",
    },
    # --- 서대문구 ---
    "서대문구": {
        "platform": "sdm_bbs",
        "base_url": "https://www.sdm.go.kr",
        "list_path": "/news/notice/notice.do",
        "view_path": "/news/notice/notice.do",
        "params": {},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
    },
    # --- 양천구 (eminwon 변형) ---
    "양천구": {
        "platform": "yangcheon_bbs",
        "base_url": "https://www.yangcheon.go.kr",
        "list_path": "/site/yangcheon/ex/seolCollectList.do",
        "view_path": "/site/yangcheon/ex/seolContentDeailView.do",
        "params": {},
        "page_param": "pageIndex",
        "search_param": "searchKeyword",
    },
    # --- 중구 ---
    "중구": {
        "platform": "junggu_cms",
        "base_url": "https://www.junggu.seoul.kr",
        "list_path": "/content.do",
        "view_path": "/content.do",
        "params": {"cmsid": "14232"},
        "page_param": "pageIndex",
        "search_param": "keyword",
    },
}


def fetch_gu_announcements(
    sgg_nm: str,
    zone_keywords: list[str],
    limit: int = 5,
    timeout: int = 15,
) -> list[dict]:
    """
    해당 구청 고시공고에서 구역명 검색.
    미설정 구청은 빈 리스트 반환 (graceful skip).
    """
    config = GU_CONFIGS.get(sgg_nm)
    if not config:
        logger.debug(f"구청 설정 없음: {sgg_nm}")
        return []

    platform = config["platform"]
    results = []

    for keyword in zone_keywords[:3]:
        if not keyword or len(keyword) < 2:
            continue
        try:
            search_fn = _PLATFORM_SEARCH.get(platform)
            if search_fn:
                items = search_fn(config, keyword, timeout)
            else:
                items = []

            for item in items:
                if not any(r.get("title") == item.get("title") for r in results):
                    results.append(item)
                if len(results) >= limit:
                    break
        except Exception as e:
            logger.warning(f"구청 고시 검색 실패 ({sgg_nm}, '{keyword}'): {e}")

        if len(results) >= limit:
            break
        time.sleep(0.3)

    # 상세페이지 방문 (본문 + PDF)
    for item in results[:limit]:
        if not item.get("detail_url"):
            continue
        try:
            detail = _fetch_detail_generic(item["detail_url"], config, timeout)
            item["body"] = detail.get("body", "")[:10000]
            item["pdf_urls"] = detail.get("pdf_urls", [])
            item["content_quality"] = _classify_quality(item["body"])
            time.sleep(0.2)
        except Exception as e:
            logger.debug(f"구청 상세페이지 실패 ({item.get('title', '')[:20]}): {e}")

    return results[:limit]


# ---------------------------------------------------------------------------
# Platform: eGovFrame BBS
# ---------------------------------------------------------------------------

def _search_egov_bbs(config: dict, keyword: str, timeout: int) -> list[dict]:
    """eGovFrame 기반 게시판 검색"""
    base = config["base_url"]
    params = dict(config["params"])
    params[config.get("page_param", "pageIndex")] = 1
    params[config.get("search_param", "searchKeyword")] = keyword
    if config.get("search_type_param"):
        params[config["search_type_param"]] = config.get("search_type_value", "")

    url = base + config["list_path"]
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "utf-8"

    return _parse_table_list(resp.text, config)


def _parse_table_list(html: str, config: dict) -> list[dict]:
    """공통 테이블 목록 파싱 (eGovFrame / 강남구)"""
    soup = BeautifulSoup(html, "lxml")
    results = []

    # 테이블 찾기 (다양한 선택자 시도)
    table = (soup.select_one("table.brd_list") or
             soup.select_one("table.board_list") or
             soup.select_one(".board_list table") or
             soup.select_one("table"))
    if not table:
        return results

    rows = table.select("tbody tr")
    if not rows:
        rows = table.find_all("tr")[1:]  # 헤더 제외

    base = config["base_url"]

    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 3:
            continue

        # 제목 찾기 (보통 두 번째 컬럼)
        title_td = cols[1] if len(cols) >= 4 else cols[0]
        a_tag = title_td.find("a")
        if not a_tag:
            continue

        title = a_tag.get_text(strip=True)
        if not title:
            continue

        # 관련성 확인
        if not any(kw in title for kw in _RELEVANCE_KEYWORDS):
            continue

        # 날짜 (마지막 또는 끝에서 두 번째 컬럼)
        pub_date = ""
        for td in reversed(cols):
            text = td.get_text(strip=True)
            if re.match(r"\d{4}[-./]\d{2}[-./]\d{2}", text):
                pub_date = text.replace(".", "-").replace("/", "-")[:10]
                break

        # 상세 URL 구성
        detail_url = _extract_detail_url(a_tag, config)

        results.append({
            "source": f"gu_{config.get('platform', 'unknown')}",
            "title": title,
            "url": detail_url,
            "detail_url": detail_url,
            "published_at": pub_date,
            "district": "",
            "category": _detect_category(title),
            "body": "",
            "pdf_urls": [],
            "content_quality": "minimal",
        })

    return results


def _extract_detail_url(a_tag, config: dict) -> str:
    """링크 태그에서 상세페이지 URL 추출"""
    base = config["base_url"]
    href = a_tag.get("href", "")

    # javascript: onClick 패턴
    onclick = a_tag.get("onclick", "")
    if onclick:
        # fn_detail('123') 또는 fn_egov_modal_detail('123') 등
        m = re.search(r"(?:detail|view|read|nttNo)['\"]?\s*[,:=]\s*['\"]?(\d+)", onclick, re.I)
        if m:
            ntt_no = m.group(1)
            view_path = config.get("view_path", "")
            if "{id}" in view_path:
                return base + view_path.replace("{id}", ntt_no)
            params = dict(config.get("params", {}))
            params["nttNo"] = ntt_no
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            return base + view_path + "?" + qs

    # 일반 href
    if href and not href.startswith("javascript"):
        if href.startswith("http"):
            return href
        if href.startswith("/"):
            return base + href
        return base + "/" + href

    # href에 JS 함수
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


# ---------------------------------------------------------------------------
# Platform: 강남구 Board
# ---------------------------------------------------------------------------

def _search_gangnam_board(config: dict, keyword: str, timeout: int) -> list[dict]:
    """강남구 커스텀 보드 검색"""
    base = config["base_url"]
    params = dict(config["params"])
    params[config.get("page_param", "pageIndex")] = 1
    params[config.get("search_param", "searchKeyword")] = keyword
    if config.get("search_type_param"):
        params[config["search_type_param"]] = config.get("search_type_value", "1")

    url = base + config["list_path"]
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "utf-8"

    return _parse_table_list(resp.text, config)


# ---------------------------------------------------------------------------
# Platform: ASA CMS (마포구 등)
# ---------------------------------------------------------------------------

def _search_asa_portal(config: dict, keyword: str, timeout: int) -> list[dict]:
    """ASA CMS 기반 포털 검색"""
    base = config["base_url"]
    params = {config.get("search_param", "query"): keyword}
    page_param = config.get("page_param", "cp")
    params[page_param] = 1

    url = base + config["list_path"]
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "utf-8"

    soup = BeautifulSoup(resp.text, "lxml")
    results = []

    # ASA CMS는 <ul><li> 또는 <table> 모두 사용
    # 테이블 먼저 시도
    table = soup.select_one("table")
    if table:
        return _parse_table_list(resp.text, config)

    # <li> 기반 목록
    items = soup.select(".bbs_style_li li, .list_wrap li, .board_list li")
    for item in items:
        a_tag = item.find("a")
        if not a_tag:
            continue
        title = a_tag.get_text(strip=True)
        if not title or not any(kw in title for kw in _RELEVANCE_KEYWORDS):
            continue

        href = a_tag.get("href", "")
        detail_url = href if href.startswith("http") else base + href

        # 날짜 추출
        pub_date = ""
        date_el = item.select_one(".date, .day, span")
        if date_el:
            text = date_el.get_text(strip=True)
            m = re.search(r"\d{4}[-./]\d{2}[-./]\d{2}", text)
            if m:
                pub_date = m.group().replace(".", "-").replace("/", "-")

        results.append({
            "source": "gu_asa_portal",
            "title": title,
            "url": detail_url,
            "detail_url": detail_url,
            "published_at": pub_date,
            "district": "",
            "category": _detect_category(title),
            "body": "",
            "pdf_urls": [],
            "content_quality": "minimal",
        })

    return results


# ---------------------------------------------------------------------------
# 상세페이지 공통 파싱
# ---------------------------------------------------------------------------

def _fetch_detail_generic(url: str, config: dict, timeout: int = 15) -> dict:
    """구청 상세페이지에서 본문 + 첨부 PDF 추출 (범용)"""
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
    base = config.get("base_url", "")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        fname = (a.get_text() or "").strip().lower()
        # PDF 파일 링크
        if ".pdf" in href.lower() or ".pdf" in fname:
            full = href if href.startswith("http") else base + href
            if full not in pdf_urls:
                pdf_urls.append(full)
        # 다운로드 링크 (파일명에 pdf 포함)
        elif "download" in href.lower() and ".pdf" in fname:
            full = href if href.startswith("http") else base + href
            if full not in pdf_urls:
                pdf_urls.append(full)

    return {"body": body, "pdf_urls": pdf_urls}


# ---------------------------------------------------------------------------
# 유틸리티
# ---------------------------------------------------------------------------

def _detect_category(title: str) -> str:
    for cat in ["결정고시", "지정고시", "변경고시", "해제고시", "열람공고", "결정공고"]:
        if cat in title:
            return cat
    if "고시" in title:
        return "고시"
    if "공고" in title:
        return "공고"
    return ""


def _classify_quality(text: str) -> str:
    if not text or len(text) < 10:
        return "minimal"
    hits = sum(1 for kw in _DETAIL_KEYWORDS if kw in text)
    return "detailed" if hits >= 2 else "summary"


# ---------------------------------------------------------------------------
# Platform: eminwon (새올전자민원 — 동대문구, 성북구, 은평구)
# ---------------------------------------------------------------------------

def _search_eminwon(config: dict, keyword: str, timeout: int) -> list[dict]:
    """새올전자민원 고시공고 검색"""
    base = config["base_url"]
    params = dict(config["params"])
    params[config.get("page_param", "pageIndex")] = 1
    params[config.get("search_param", "searchKeyword")] = keyword

    url = base + config["list_path"]
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "utf-8"

    return _parse_table_list(resp.text, config)


# ---------------------------------------------------------------------------
# Platform: gwanak_bbs (관악구 bbsNew)
# ---------------------------------------------------------------------------

def _search_gwanak_bbs(config: dict, keyword: str, timeout: int) -> list[dict]:
    """관악구 bbsNew 게시판 검색"""
    base = config["base_url"]
    params = dict(config["params"])
    params[config.get("page_param", "pageIndex")] = 1
    params[config.get("search_param", "searchKeyword")] = keyword

    url = base + config["list_path"]
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "utf-8"

    return _parse_table_list(resp.text, config)


# ---------------------------------------------------------------------------
# Platform: gangseo_custom (강서구)
# ---------------------------------------------------------------------------

def _search_gangseo(config: dict, keyword: str, timeout: int) -> list[dict]:
    """강서구 커스텀 게시판 검색"""
    base = config["base_url"]
    params = {
        config.get("search_param", "srchText"): keyword,
        config.get("page_param", "curPage"): 1,
    }

    url = base + config["list_path"]
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "utf-8"

    soup = BeautifulSoup(resp.text, "lxml")
    results = []

    # 강서구는 테이블 또는 리스트 형태
    table = soup.select_one("table")
    if table:
        return _parse_table_list(resp.text, config)

    # 리스트 형태 폴백
    for a in soup.select("a[href]"):
        title = a.get_text(strip=True)
        if not title or len(title) < 5:
            continue
        if not any(kw in title for kw in _RELEVANCE_KEYWORDS):
            continue
        href = a.get("href", "")
        detail_url = href if href.startswith("http") else base + href
        results.append({
            "source": "gu_gangseo_custom",
            "title": title,
            "url": detail_url,
            "detail_url": detail_url,
            "published_at": "",
            "district": "",
            "category": _detect_category(title),
            "body": "",
            "pdf_urls": [],
            "content_quality": "minimal",
        })

    return results


# ---------------------------------------------------------------------------
# Platform: nowon_bbs (노원구 BD_ board)
# ---------------------------------------------------------------------------

def _search_nowon_bbs(config: dict, keyword: str, timeout: int) -> list[dict]:
    """노원구 BD_ 게시판 검색"""
    base = config["base_url"]
    params = dict(config["params"])
    params[config.get("page_param", "q_currPage")] = 1
    params[config.get("search_param", "q_searchVal")] = keyword
    params["q_searchKey"] = "sj"  # 제목 검색

    url = base + config["list_path"]
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "utf-8"

    return _parse_table_list(resp.text, config)


# ---------------------------------------------------------------------------
# Platform: dobong_asp (도봉구 Classic ASP)
# ---------------------------------------------------------------------------

def _search_dobong_asp(config: dict, keyword: str, timeout: int) -> list[dict]:
    """도봉구 Classic ASP 게시판 검색"""
    base = config["base_url"]
    params = {
        config.get("search_param", "keyword"): keyword,
        config.get("page_param", "intPage"): 1,
    }

    url = base + config["list_path"]
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    # Classic ASP — 인코딩 감지
    if "euc-kr" in resp.text[:500].lower() or "euc-kr" in resp.headers.get("content-type", "").lower():
        resp.encoding = "euc-kr"
    else:
        resp.encoding = "utf-8"

    return _parse_table_list(resp.text, config)


# ---------------------------------------------------------------------------
# Platform: eminwon_sub (강북구 — eminwon 서브도메인)
# ---------------------------------------------------------------------------

def _search_eminwon_sub(config: dict, keyword: str, timeout: int) -> list[dict]:
    """강북구 eminwon 서브도메인 검색"""
    base = config["base_url"]
    params = dict(config["params"])
    params[config.get("page_param", "pageIndex")] = 1
    params[config.get("search_param", "not_ancmt_sj")] = keyword

    url = base + config["list_path"]
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "utf-8"

    return _parse_table_list(resp.text, config)


# ---------------------------------------------------------------------------
# Platform: sdm_bbs (서대문구)
# ---------------------------------------------------------------------------

def _search_sdm_bbs(config: dict, keyword: str, timeout: int) -> list[dict]:
    """서대문구 고시공고 검색"""
    base = config["base_url"]
    params = dict(config["params"])
    params[config.get("page_param", "pageIndex")] = 1
    params[config.get("search_param", "searchKeyword")] = keyword

    url = base + config["list_path"]
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "utf-8"

    return _parse_table_list(resp.text, config)


# ---------------------------------------------------------------------------
# Platform: yangcheon_bbs (양천구)
# ---------------------------------------------------------------------------

def _search_yangcheon_bbs(config: dict, keyword: str, timeout: int) -> list[dict]:
    """양천구 고시공고 검색"""
    base = config["base_url"]
    params = dict(config["params"])
    params[config.get("page_param", "pageIndex")] = 1
    params[config.get("search_param", "searchKeyword")] = keyword

    url = base + config["list_path"]
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=timeout, verify=False)
    resp.raise_for_status()
    resp.encoding = "utf-8"

    return _parse_table_list(resp.text, config)


# ---------------------------------------------------------------------------
# Platform: junggu_cms (중구)
# ---------------------------------------------------------------------------

def _search_junggu_cms(config: dict, keyword: str, timeout: int) -> list[dict]:
    """중구 CMS 게시판 검색"""
    base = config["base_url"]
    params = dict(config["params"])
    params[config.get("page_param", "pageIndex")] = 1
    params[config.get("search_param", "keyword")] = keyword

    url = base + config["list_path"]
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = "utf-8"

    return _parse_table_list(resp.text, config)


# ---------------------------------------------------------------------------
# 플랫폼 → 검색 함수 매핑
# ---------------------------------------------------------------------------

_PLATFORM_SEARCH = {
    "egov_bbs": _search_egov_bbs,
    "gangnam_board": _search_gangnam_board,
    "asa_portal": _search_asa_portal,
    "eminwon": _search_eminwon,
    "gwanak_bbs": _search_gwanak_bbs,
    "gangseo_custom": _search_gangseo,
    "nowon_bbs": _search_nowon_bbs,
    "dobong_asp": _search_dobong_asp,
    "eminwon_sub": _search_eminwon_sub,
    "sdm_bbs": _search_sdm_bbs,
    "yangcheon_bbs": _search_yangcheon_bbs,
    "junggu_cms": _search_junggu_cms,
}
