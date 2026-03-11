"""
토지이음 (eum.go.kr) 스크래핑으로 상세 토지이용계획 정보 보완
VWORLD에서 잡히지 않는 추가 규제 정보 수집
"""
import logging
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.eum.go.kr"
_LAND_DETAIL_URL = f"{_BASE_URL}/web/ar/lu/luLandDet.jsp"


_MAPPLAN_URL = "https://www.eum.ne.kr:9001/MapPlan"


def fetch_land_use_plan(pnu: str, timeout: int = 15, jibun_address: str = "") -> dict:
    """
    PNU로 토지이음 토지이용계획 조회

    Returns:
        {
            "zones": [{"zone_type": "용도지역", "zone_name": "제2종일반주거지역"}, ...],
            "restrictions": [...],
            "land_info": {"지목": "대", "면적": "123㎡"},
            "zone_names": ["삼성동 제1지구단위계획구역", ...],  # MapPlan API 성공 시
        }
    """
    try:
        session = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Referer": _BASE_URL,
        }
        resp = session.get(_LAND_DETAIL_URL, params={"pnu": pnu}, headers=headers, timeout=timeout)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "euc-kr"
        result = _parse_land_detail(resp.text)

        # MapPlan API로 실제 구역명 취득 시도
        zone_names = _fetch_zone_names(session, pnu, headers, timeout)
        result["zone_names"] = zone_names

        # Playwright로 추가 구역 데이터 취득 시도 (지번 주소 입력 → 열람 방식)
        if jibun_address and not zone_names:
            pw_result = fetch_zones_via_playwright(
                jibun_address, timeout=min(timeout * 2, 45), pnu=pnu
            )
            if pw_result.get("zone_names"):
                result["zone_names"] = pw_result["zone_names"]
            if pw_result.get("zones"):
                result["zones"] = pw_result["zones"]

        return result
    except Exception as e:
        logger.warning(f"토지이음 조회 실패 (PNU={pnu}): {e}")
        return {"zones": [], "restrictions": [], "land_info": {}, "zone_names": [], "raw_html": ""}


def _fetch_zone_names(session: requests.Session, pnu: str, headers: dict, timeout: int) -> list[str]:
    """토지이음 MapPlan API로 지역지구 구역명 조회 (세션 쿠키 필요)"""
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        resp = session.get(
            _MAPPLAN_URL,
            params={"req": "analysis", "pnus": pnu},
            headers={**headers,
                     "Referer": f"{_BASE_URL}/web/ar/lu/luLandDet.jsp?pnu={pnu}",
                     "Origin": _BASE_URL},
            timeout=timeout,
            verify=False,
        )
        if resp.status_code != 200:
            logger.debug(f"MapPlan API {resp.status_code}: 구역명 불가")
            return []
        data = resp.json()
        logger.debug(f"MapPlan API 응답: {str(data)[:300]}")
        names = []
        # 응답 구조에 따라 파싱 (구역명 필드 탐색)
        for key in ("plandList", "planList", "result", "data", "list"):
            items = data.get(key, [])
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        for nk in ("planNm", "planName", "zoneName", "uname", "name", "nm"):
                            v = item.get(nk, "")
                            if v and v not in names:
                                names.append(v)
                break
        return names
    except Exception as e:
        logger.debug(f"MapPlan API 실패: {e}")
        return []


def _parse_land_detail(html: str) -> dict:
    """토지이음 상세 페이지 HTML 파싱

    주의: 지역지구등 지정여부(present_mark1/2/3)는 JS로만 렌더링됨.
    대신 정적 HTML에서 추출 가능한 정보(지목/면적/규제사항)를 파싱.
    """
    soup = BeautifulSoup(html, "lxml")
    zones: list = []
    restrictions: list[str] = []

    tables = soup.find_all("table")

    # Table[0]: 소재지/지목/면적
    # 실제 구조: row[0]=소재지, row[1]=["지목",값,"면적",값]
    land_info = {}
    if tables:
        rows = tables[0].find_all("tr")
        for row in rows:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            # "지목" 헤더가 cells[0]이고 값이 cells[1], 면적 헤더가 cells[2], 값이 cells[3]
            if len(cells) >= 2 and cells[0] == "지목":
                land_info["지목"] = cells[1]
                if len(cells) >= 4 and cells[2] == "면적":
                    land_info["면적"] = cells[3]

    # Table[4]/[5]/[6]: 규제사항 (건축선/접도요건)
    for t in tables[3:8]:
        caption = t.find("caption")
        header_text = t.get_text()[:200]
        if any(kw in header_text for kw in ["규제", "건축선", "접도", "행위제한"]):
            for row in t.find_all("tr")[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                line = " | ".join(c for c in cells if c)
                if line and len(line) > 3:
                    restrictions.append(line)

    return {
        "zones": zones,          # JS 렌더링 필요 - 항상 빈 리스트
        "restrictions": restrictions[:20],  # type: ignore[misc]
        "land_info": land_info,  # 지목, 면적 등 정적 정보
        "raw_html": "",
    }


def fetch_zones_via_playwright(jibun_address: str, timeout: int = 45, pnu: str = "") -> dict:
    """
    Playwright로 토지이음 검색 폼에 지번 주소를 직접 입력 → 열람 → 구역 데이터 수집.

    토이이음 fn_goLand() 흐름:
      1. 주소 입력란에 지번 입력 (예: "도선동 39-2")
      2. 자동완성 목록에서 해당 주소 선택 → #pnu hidden input 자동 설정
      3. 열람 버튼 클릭 → luLandDet.jsp 로드
      4. present_mark1/2/3 div에 구역 정보 렌더링 (MapPlan 타일 로드 후)

    Returns:
        {"zone_names": [...], "zones": [...], "ajax_data": {...}}
        MapPlan 미로드 시 빈 딕셔너리
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.debug("playwright 미설치 - Playwright 조회 건너뜀")
        return {}

    ajax_data: dict = {}

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--enable-webgl",
                    "--use-gl=swiftshader",
                    "--ignore-certificate-errors",
                ],
            )
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="ko-KR",
                viewport={"width": 1280, "height": 900},
            )
            page = ctx.new_page()

            # AJAX 구역 데이터 응답 캡처
            def _on_response(resp):
                for key in ("luLandDetUseGYAjax", "luLandDetUseCAjax", "luLandDetUseKDAjax"):
                    if key in resp.url:
                        try:
                            ajax_data[key] = resp.json()
                        except Exception:
                            pass

            page.on("response", _on_response)

            # 1. 토이이음 홈페이지 접속
            page.goto("https://www.eum.go.kr", timeout=timeout * 1000,
                      wait_until="domcontentloaded")
            page.wait_for_timeout(1500)

            # 2. 지번 텍스트 입력란 탐색
            addr_input = None
            for sel in [
                "input[placeholder*='지번']",
                "input[placeholder*='도로명']",
                "#searchAddr",
                "input.inp-search",
            ]:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    addr_input = el
                    break

            if addr_input is None:
                logger.debug("토이이음: 주소 입력란을 찾지 못함")
                browser.close()
                return {}

            # 지번 부분만 추출 (예: "서울특별시 성동구 도선동 39-2" → "도선동 39-2")
            short = _short_jibun(jibun_address)
            logger.debug(f"토이이음 Playwright 검색: '{short}'")

            # 3. 주소 입력 + 자동완성 대기
            addr_input.click()
            addr_input.fill(short)
            page.wait_for_timeout(2000)

            # 4. 자동완성 항목 클릭 (여러 selector 시도)
            clicked = False
            for list_sel in [
                "ul.search-list li",
                "#addrList li",
                ".addr-list li",
                "ul#ul_juso li",
                "ul li[onclick]",
            ]:
                items = page.query_selector_all(list_sel)
                if items:
                    items[0].click()
                    clicked = True
                    page.wait_for_timeout(1000)
                    break

            if not clicked:
                logger.debug("토이이음: 자동완성 항목 없음 — Enter로 검색 시도")
                addr_input.press("Enter")
                page.wait_for_timeout(2000)

            # #pnu 값 확인
            pnu_val = page.evaluate(
                "() => { const e = document.getElementById('pnu'); return e ? e.value : ''; }"
            )
            logger.debug(f"토이이음 Playwright #pnu={pnu_val!r}")

            # landGbn 강제 설정 (일반=1, 산=2): pnu[10]=='0'→일반, '1'→산
            effective_pnu = pnu_val or pnu
            if len(effective_pnu) > 10:
                land_gbn_val = "1" if effective_pnu[10] == "0" else "2"
                page.evaluate(
                    f"() => {{ const e = document.getElementById('landGbn'); "
                    f"if (e) e.value = '{land_gbn_val}'; }}"
                )
                logger.debug(f"토이이음 landGbn={land_gbn_val} 설정")

            # 5. 열람 버튼 클릭
            clicked_yeoram = False
            for btn_sel in [
                ".btn-search",
                "button[onclick*='goLand']",
                "a[onclick*='goLand']",
                ".see a",
                "[onclick*='fn_goLand']",
                "button.btn_look",
            ]:
                el = page.query_selector(btn_sel)
                if el and el.is_visible():
                    el.click()
                    clicked_yeoram = True
                    break

            if not clicked_yeoram:
                page.evaluate("() => { if (typeof fn_goLand === 'function') fn_goLand(); }")

            # 6. luLandDet.jsp 로드 대기
            try:
                page.wait_for_url("**/luLandDet.jsp**", timeout=15000)
            except Exception:
                pass
            page.wait_for_load_state("networkidle", timeout=20000)
            page.wait_for_timeout(4000)  # MapPlan 타일 로드 여유

            # 7. present_mark 구역 데이터 추출
            zones = []
            zone_names = []
            for mark_id in ("present_mark1", "present_mark2", "present_mark3"):
                html = page.evaluate(
                    f"() => {{ const e = document.getElementById('{mark_id}'); "
                    f"return e ? e.innerHTML : ''; }}"
                )
                if html and html.strip():
                    logger.debug(f"{mark_id} 내용(200자): {html[:200]}")
                    parsed = _parse_present_mark_html(html, mark_id)
                    zones.extend(parsed)
                    zone_names.extend(z["zone_name"] for z in parsed if z.get("zone_name"))

            logger.debug(f"토이이음 Playwright 결과: zones={len(zones)}, ajax={list(ajax_data.keys())}")
            browser.close()

        return {"zone_names": zone_names, "zones": zones, "ajax_data": ajax_data}

    except Exception as e:
        logger.warning(f"Playwright 토이이음 조회 실패: {e}")
        return {}


def _short_jibun(full_address: str) -> str:
    """
    '서울특별시 성동구 도선동 39-2' → '도선동 39-2'
    토이이음 검색창은 동 이름 + 지번 형식을 선호.
    """
    parts = full_address.strip().split()
    # 시 / 구 제거: '동' 또는 '읍', '면', '리'로 끝나는 토큰부터 사용
    for i, p in enumerate(parts):
        if p.endswith(("동", "읍", "면", "리")):
            return " ".join(parts[i:])
    return full_address


def _parse_present_mark_html(html: str, mark_id: str) -> list[dict]:
    """present_mark1/2/3 innerHTML의 테이블에서 구역 정보 추출"""
    soup = BeautifulSoup(html, "lxml")
    results = []
    for row in soup.find_all("tr"):
        cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if len(cells) < 2:
            continue
        zone_name = cells[1] if len(cells) > 1 else cells[0]
        if not zone_name or zone_name in ("지정내용", "구분", ""):
            continue
        results.append({
            "zone_type": cells[0],
            "zone_name": zone_name,
            "law_category": "다른 법령" if mark_id == "present_mark2" else "국토계획법",
            "layer": f"tojieum_{mark_id}",
            "designated_year": cells[2] if len(cells) > 2 else "",
            "gazette_number": cells[3] if len(cells) > 3 else "",
        })
    return results
