"""
FastAPI 라우트 정의
"""
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

# 동시 PDF 분석 방지 (한 번에 1건만)
_pdf_analysis_lock = threading.Semaphore(1)

_TEMPLATES_DIR = Path(__file__).parent.parent / "frontend" / "templates"
_STATIC_DIR = Path(__file__).parent.parent / "frontend" / "static"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
# CSS 캐시 버스터: style.css 수정 시각을 쿼리 파라미터로 사용
_css_mtime = int((_STATIC_DIR / "style.css").stat().st_mtime)
templates.env.globals["css_v"] = _css_mtime


def create_app(settings, db_path: Path) -> FastAPI:
    app = FastAPI(title="서울시 지구단위계획 조회 시스템", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse("index.html", {"request": request})

    @app.post("/lookup", response_class=HTMLResponse)
    async def lookup_post(request: Request, address: str = Form(...)):
        result = await _do_lookup(address, settings, db_path)
        return templates.TemplateResponse("result.html", {
            "request": request,
            "address": address,
            **result,
        })

    @app.get("/lookup")
    async def lookup_get(address: str, request: Request):
        """REST JSON API 엔드포인트"""
        result = await _do_lookup(address, settings, db_path)
        accept = request.headers.get("Accept", "")
        if "text/html" in accept:
            return templates.TemplateResponse("result.html", {
                "request": request,
                "address": address,
                **result,
            })
        return JSONResponse(content=result)

    @app.get("/suggest")
    async def suggest(q: str = ""):
        """주소 자동완성 — juso.go.kr 상위 5건 반환"""
        if not q or len(q.strip()) < 2:
            return JSONResponse(content=[])
        try:
            import requests as _req
            keyword = q.strip()
            if not any(keyword.startswith(p) for p in ["서울", "서울시", "서울특별시"]):
                keyword = "서울특별시 " + keyword
            resp = _req.get(
                "https://www.juso.go.kr/addrlink/addrLinkApi.do",
                params={
                    "confmKey": settings.juso_api_key,
                    "currentPage": 1,
                    "countPerPage": 5,
                    "keyword": keyword,
                    "resultType": "json",
                    "hstryYn": "N",
                    "addInfoYn": "N",
                },
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("results", {}).get("juso", []) or []
            suggestions = [
                {
                    "label": item.get("jibunAddr", ""),
                    "road": item.get("roadAddr", ""),
                    "value": item.get("jibunAddr", ""),
                }
                for item in items
                if item.get("jibunAddr")
            ]
            return JSONResponse(content=suggestions)
        except Exception as e:
            logger.debug(f"주소 자동완성 실패: {e}")
            return JSONResponse(content=[])

    @app.get("/health")
    async def health():
        return {"status": "ok", "db": str(db_path)}

    @app.post("/api/analyze-gazette")
    async def analyze_gazette_api(request: Request):
        """비동기 AI 분석 엔드포인트 — 클라이언트에서 AJAX POST로 호출"""
        if not settings.anthropic_api_key:
            return JSONResponse(content={"error": "API 키 미설정"}, status_code=503)
        body = await request.json()
        import asyncio
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, _run_gazette_analysis,
            body.get("zone_name", ""),
            body.get("gazette_ref", ""),
            body.get("ann_title", ""),
            body.get("ann_cn", ""),
            body.get("upis_content", ""),
            body.get("content_quality", "summary"),
            body.get("pdf_urls", []),
            settings,
        )
        return JSONResponse(content=result)

    @app.post("/api/analyze-gazette-tabs")
    async def analyze_gazette_tabs_api(request: Request):
        """열람공고/결정고시 탭 분리 분석 — 두 건 순차 처리"""
        if not settings.anthropic_api_key:
            return JSONResponse(content={"error": "API 키 미설정"}, status_code=503)
        body = await request.json()
        import asyncio
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, _run_gazette_analysis_tabs, body, settings,
        )
        return JSONResponse(content=result)

    return app


def _run_gazette_analysis(
    zone_name: str, gazette_ref: str, ann_title: str, ann_cn: str,
    upis_content: str, content_quality: str, pdf_urls: list, settings,
) -> dict:
    """AI 분석 (동기, 6단계 폴백, 세마포어로 동시 1건). 시보 PDF는 최후 수단."""
    acquired = _pdf_analysis_lock.acquire(timeout=5)
    try:
        return _run_gazette_analysis_inner(
            zone_name, gazette_ref, ann_title, ann_cn,
            upis_content, content_quality, pdf_urls or [], settings
        )
    finally:
        if acquired:
            _pdf_analysis_lock.release()


def _run_gazette_analysis_tabs(body: dict, settings) -> dict:
    """열람공고/결정고시 두 건 순차 분석 (세마포어 1회 acquire)"""
    zone_name = body.get("zone_name", "")
    upis_content = body.get("upis_content", "")
    yeolam = body.get("yeolam")
    gyeoljeong = body.get("gyeoljeong")

    acquired = _pdf_analysis_lock.acquire(timeout=10)
    try:
        result = {}
        # 결정고시 먼저 (확정 데이터 우선)
        if gyeoljeong:
            result["gyeoljeong"] = _run_gazette_analysis_inner(
                zone_name, gyeoljeong.get("gazette_ref", ""),
                gyeoljeong.get("ann_title", ""), gyeoljeong.get("ann_cn", ""),
                upis_content, gyeoljeong.get("content_quality", "summary"),
                gyeoljeong.get("pdf_urls", []), settings,
            )
        if yeolam:
            result["yeolam"] = _run_gazette_analysis_inner(
                zone_name, yeolam.get("gazette_ref", ""),
                yeolam.get("ann_title", ""), yeolam.get("ann_cn", ""),
                upis_content, yeolam.get("content_quality", "summary"),
                yeolam.get("pdf_urls", []), settings,
            )
        return result
    finally:
        if acquired:
            _pdf_analysis_lock.release()


def _run_gazette_analysis_inner(
    zone_name: str, gazette_ref: str, ann_title: str, ann_cn: str,
    upis_content: str, content_quality: str, pdf_urls: list, settings,
) -> dict:
    """AI 분석 6단계 폴백 — 시보 PDF는 최후 수단.
    1. ann_cn (detailed) — 서울시 고시 상세페이지 본문 등
    2. upis_content (detailed) — UPIS CN 결정조서
    3. ann_cn (summary) — 요약 수준이라도 분석 시도
    4. 첨부 PDF (1-30MB) — 서울시/구청 고시 첨부 PDF 즉시 분석
    5. 시보 PDF (subprocess 격리) — 위에서 미확보 시에만
    6. upis_content (summary 폴백)
    """
    from lookup.announcements import analyze_announcement_with_claude

    _DETAIL_KEYWORDS = ("건폐율", "용적률", "허용용도", "불허용도", "높이제한", "결정조서")

    def _is_detailed(text: str) -> bool:
        return sum(1 for kw in _DETAIL_KEYWORDS if kw in text) >= 2

    def _has_substance(analysis: dict) -> bool:
        """분석 결과에 실질적 내용이 있는지 확인 (건폐율/용적률/높이 등)"""
        substance_keys = [
            "building_coverage_ratio", "floor_area_ratio",
            "base_floor_area_ratio", "allowed_floor_area_ratio",
            "max_floor_area_ratio", "max_height_meters", "max_floors",
            "allowed_uses", "prohibited_uses",
        ]
        for k in substance_keys:
            v = analysis.get(k)
            if v and str(v).strip() and str(v).strip() not in ("", "없음", "해당없음", "-"):
                return True
        return False

    def _try_claude(title: str, content: str, source_label: str) -> dict | None:
        if not content or len(content) < 10:
            return None
        try:
            analysis = analyze_announcement_with_claude(
                title, content,
                settings.anthropic_api_key, settings.claude_model,
            )
            if analysis and not analysis.get("error"):
                analysis["_gazette_source"] = source_label
                return analysis
        except Exception as e:
            logger.debug(f"{source_label} 분석 실패: {e}")
        return None

    # 1차: ann_cn이 detailed (결정조서 키워드 포함)
    if ann_cn and _is_detailed(ann_cn):
        r = _try_claude(ann_title, ann_cn, "고시 상세")
        if r and _has_substance(r):
            return r
        logger.info(f"1차(고시 상세) 실질 내용 없음 → 다음 폴백")

    # 2차: UPIS content가 detailed
    if upis_content and _is_detailed(upis_content):
        r = _try_claude(f"{zone_name} 결정고시", upis_content, "UPIS 상세")
        if r and _has_substance(r):
            return r
        logger.info(f"2차(UPIS 상세) 실질 내용 없음 → 다음 폴백")

    # 3차: ann_cn이 summary 수준이라도 분석 시도
    # summary에서 실질 내용 못 추출하면 → 4차(첨부 PDF), 5차(시보 PDF)로 진행
    best_summary_result = None
    if ann_cn and len(ann_cn) >= 10 and not _is_detailed(ann_cn):
        r = _try_claude(ann_title, ann_cn, "고시공고")
        if r and _has_substance(r):
            return r
        if r:
            best_summary_result = r  # 나중에 폴백 실패 시 반환용
        logger.info(f"3차(고시공고) 실질 내용 없음 → 4차(첨부 PDF)/5차(시보) 시도")

    # 4차: 첨부 PDF 즉시 분석 (1-30MB, subprocess 불필요)
    if pdf_urls:
        try:
            from lookup.pdf_quick_analyze import analyze_small_pdf
            for pu in pdf_urls[:3]:
                r = analyze_small_pdf(pu, ann_title, settings.anthropic_api_key, settings.claude_model, zone_name=zone_name)
                if r and not r.get("error"):
                    return r
        except Exception as e:
            logger.debug(f"첨부 PDF 분석 실패: {e}")

    # 5차: 시보 PDF (최후 수단, subprocess 격리)
    if gazette_ref:
        try:
            from lookup.gazette_pdf import analyze_gazette_for_zone
            analysis = analyze_gazette_for_zone(
                gazette_ref, zone_name,
                settings.anthropic_api_key, settings.pdf_cache_dir,
                settings.claude_model,
            )
            if analysis and not analysis.get("error"):
                analysis["_gazette_source"] = "시보 PDF"
                return analysis
        except Exception as e:
            logger.debug(f"시보 PDF 분석 실패: {e}")

    # 6차: UPIS summary 폴백
    if upis_content and len(upis_content) >= 10 and not _is_detailed(upis_content):
        r = _try_claude(f"{zone_name} 결정고시", upis_content, "UPIS 고시")
        if r and _has_substance(r):
            return r

    # 모든 폴백 실패 → 이전에 받은 요약 결과라도 반환
    if best_summary_result:
        return best_summary_result

    return {"error": "분석 실패"}


async def _do_lookup(address: str, settings, db_path: Path) -> dict:
    """
    지번 조회 메인 파이프라인 (비동기 래퍼)
    실제 작업은 동기 함수들 호출
    """
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_lookup, address, settings, db_path)


def _sync_lookup(address: str, settings, db_path: Path) -> dict:
    """동기 조회 파이프라인"""
    from lookup.address import address_to_pnu, parse_address_input
    from lookup.vworld import query_planning_zones, fetch_parcel_info, fetch_zone_specific_names, fetch_wfs_zones
    from lookup.tojieum import fetch_land_use_plan
    from lookup.announcements import get_announcements_for_zones
    from db.database import get_connection, log_lookup

    result = {
        "error": None,
        "pnu": None,
        "address_full": None,
        "zones": [],
        "announcements": [],
        "tojieum_info": {},
        "parcel_info": {},
    }

    conn = get_connection(db_path)

    try:
        # 1. 지번 → PNU
        cleaned = parse_address_input(address)
        addr_info = address_to_pnu(cleaned, settings.juso_api_key, settings.request_timeout,
                                   vworld_api_key=settings.vworld_api_key,
                                   vworld_domain=settings.vworld_domain)
        pnu = addr_info["pnu"]
        result["pnu"] = pnu
        result["address_full"] = addr_info["address_full"]
        result["address_road"] = addr_info.get("address_road", "")

        # 토이이음 POST 파라미터 사전 계산
        # juso.go.kr: mtYn '0'=일반,'1'=산  /  토이이음: landGbn '1'=일반,'2'=산 (PNU도 동일 매핑)
        if pnu and len(pnu) == 19:
            _mt = pnu[10]
            result["tj_pnu"]     = pnu[:10] + ("1" if _mt == "0" else "2") + pnu[11:]
            result["tj_landgbn"] = "1" if _mt == "0" else "2"
            result["tj_sido"]    = pnu[0:2]
            result["tj_sgg"]     = pnu[2:5]
            result["tj_umd"]     = pnu[5:8].zfill(4)
            result["tj_ri"]      = pnu[8:10]
            result["tj_bobn"]    = str(int(pnu[11:15]))
            result["tj_bubn"]    = str(int(pnu[15:19]))
        result["coord"] = {
            "x": addr_info.get("entX", ""),
            "y": addr_info.get("entY", ""),
        }

        # 2~3. 좌표/PNU 기반 6개 독립 작업 병렬 실행
        has_coords = settings.vworld_api_key and addr_info.get("entX") and addr_info.get("entY")
        logger.info(f"좌표: entX={addr_info.get('entX')}, entY={addr_info.get('entY')}, has_coords={bool(has_coords)}")
        x = float(addr_info["entX"]) if has_coords else 0.0
        y = float(addr_info["entY"]) if has_coords else 0.0
        vk = settings.vworld_api_key
        to = settings.request_timeout

        def _safe(fn, *args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                logger.warning(f"{fn.__name__} 실패: {e}")
                return None

        def _task_zones():
            return query_planning_zones(x, y, vk, to) if has_coords else []

        def _task_parcel():
            return fetch_parcel_info(pnu, vk, to) if vk and pnu else {}

        def _task_specific():
            return fetch_zone_specific_names(x, y, vk, to) if has_coords else []

        def _task_wfs():
            return fetch_wfs_zones(x, y, vk, to) if has_coords else []

        def _task_upis():
            if not pnu:
                return {"zones": [], "notification": None, "gazette_history": [],
                        "drawing_documents": [], "all_notifications": [],
                        "portal_url": "", "notice_url": None}
            from lookup.urban_seoul import fetch_zone_data
            return fetch_zone_data(pnu, timeout=min(to, 25))

        def _task_tojieum():
            # 토지이음용 PNU 변환 (mtYn '0'→'1', '1'→'2')
            tj_pnu = result.get("tj_pnu", pnu)
            return fetch_land_use_plan(tj_pnu, to, jibun_address=addr_info.get("address_full", ""))

        with ThreadPoolExecutor(max_workers=6) as pool:
            f_zones    = pool.submit(_safe, _task_zones)
            f_parcel   = pool.submit(_safe, _task_parcel)
            f_specific = pool.submit(_safe, _task_specific)
            f_wfs      = pool.submit(_safe, _task_wfs)
            f_upis     = pool.submit(_safe, _task_upis)
            f_tojieum  = pool.submit(_safe, _task_tojieum)

        zones    = f_zones.result()    or []
        parcel   = f_parcel.result()   or {}
        specific = f_specific.result() or []
        wfs_zones = f_wfs.result()     or []
        upis_data = f_upis.result()    or {}
        tojieum  = f_tojieum.result()  or {}

        # fetch_zone_data() → dict with zones, notification, etc.
        upis_zones = upis_data.get("zones", []) if isinstance(upis_data, dict) else upis_data or []

        # --- 후처리: 구역별 법적 근거 분류 ---
        _NATL_LAYERS = {"lt_c_uq111", "lt_c_uq121", "lt_c_uq123",
                        "lt_c_uq124", "lt_c_uq125", "lt_c_uq126",
                        "lt_c_uq129", "lt_c_uq130"}
        for z in zones:
            layer = z.get("layer", "")
            ztype = z.get("zone_type", "")
            if layer in _NATL_LAYERS:
                z["law_category"] = "국토계획법"
            elif layer == "lt_c_uq141" and "지구단위계획구역" in ztype:
                z["law_category"] = "국토계획법"
            else:
                z["law_category"] = "다른 법령"

        result["zones"] = zones
        result["yongdo_zone"] = next(
            (z["zone_type"] for z in zones if z.get("layer") == "lt_c_uq111"), "")
        result["parcel_info"] = parcel

        # 후처리: 구체적 구역명 병합 (2c)
        if specific:
            _generic_map = {
                "lt_c_ud501": "지구단위계획구역",
                "lt_c_ub011": "정비구역",
                "lt_c_ud502": "특별계획구역",
            }
            for sz in specific:
                generic_type = _generic_map.get(sz["layer"], "")
                replaced = False
                if generic_type:
                    for z in result["zones"]:
                        if z.get("zone_type") == generic_type and not replaced:
                            z["zone_type"] = sz["zone_name"]
                            z["zone_name"] = sz["zone_name"]
                            replaced = True
                if not replaced:
                    result["zones"].append(sz)

        # 후처리: WFS 구역 추가 (2d)
        if wfs_zones:
            result["zones"].extend(wfs_zones)

        # 후처리: UPIS 구역명 보완 (2e)
        _skip_road = {"대로1류", "대로2류", "소로1류", "소로2류", "중로1류", "중로2류",
                      "세로1류", "세로2류", "사선제한선"}
        _skip_types = {"용도지역"}
        upis_zones = [
            z for z in upis_zones
            if z.get("zone_name") not in _skip_road
            and z.get("zone_type") not in _skip_types
            and (z.get("zone_name") or z.get("location"))
        ]
        if upis_zones:
            for uz in upis_zones:
                uz_name = uz.get("zone_name", "")
                uz_type = uz.get("zone_type", "")
                matched = False
                if any(z.get("zone_name") == uz_name for z in result["zones"]):
                    continue
                if "지구단위계획구역" in uz_type and uz_name:
                    for z in result["zones"]:
                        if z.get("zone_type") == "지구단위계획구역" and not z.get("upis_name"):
                            z["upis_name"] = uz_name
                            matched = True
                            break
                if not matched:
                    result["zones"].append({
                        "zone_type": uz_type or uz_name,
                        "zone_name": uz_name,
                        "location": uz.get("location", ""),
                        "gazette": uz.get("gazette", ""),
                        "layer": uz.get("layer", "urban_seoul"),
                        "law_category": "도시계획법",
                        "source": "urban_seoul",
                    })
            result["upis_zones"] = upis_zones

        # 후처리: UPIS 고시/연혁 정보 (2f)
        if isinstance(upis_data, dict):
            result["upis_notification"] = upis_data.get("notification")
            result["gazette_history"] = upis_data.get("gazette_history", [])
            result["drawing_documents"] = upis_data.get("drawing_documents", [])
            result["all_notifications"] = upis_data.get("all_notifications", [])
            result["urban_portal_url"] = upis_data.get("portal_url", "")
            result["notice_url"] = upis_data.get("notice_url")

        # 후처리: 토지이음 (3)
        result["tojieum_info"] = tojieum
        result["tojieum_url"] = f"https://www.eum.go.kr/web/ar/lu/luLandDet.jsp?pnu={pnu}"

        # 4. 구역명 → 최신 고시공고
        # UPIS 구역명 우선 사용 (가장 정확)
        _SKIP_TYPES = {"용도지역", "lt_c_uq111"}
        # DB 검색에서 너무 많이 매칭되는 범용 구역유형 제외
        _GENERIC_NAMES = {
            "지구단위계획구역", "제1종지구단위계획구역", "제2종지구단위계획구역",
            "토지거래계약에관한허가구역", "정비구역", "특별계획구역",
            "제1종일반주거지역", "제2종일반주거지역", "제3종일반주거지역",
            "일반상업지역", "근린상업지역", "준주거지역", "자연녹지지역",
            "교통광장", "공공공지", "도시계획도로",
        }
        upis_names = [
            uz.get("zone_name") for uz in result.get("upis_zones", [])
            if uz.get("zone_name") and uz.get("zone_type") not in _SKIP_TYPES
        ]
        tojieum_names = tojieum.get("zone_names") or []
        vworld_names = [
            z["zone_name"] for z in zones
            if z.get("zone_name") and z.get("layer") not in _SKIP_TYPES
        ]
        # 구체적 구역명만 합산 (범용 유형 제외, UPIS > 토이이음 > VWORLD)
        seen = set()
        specific_zone_names = []
        for n in upis_names + tojieum_names + vworld_names:
            if n and n not in seen and n not in _GENERIC_NAMES:
                seen.add(n)
                specific_zone_names.append(n)

        # 4b. 5가지 소스 병렬 실시간 검색 (구역명 확보 후)
        # 서울시 고시 + 구청 고시 + 구청 구보 + 구청 지구단위계획
        seoul_notice_results = []
        gu_results = []
        district = addr_info.get("sggNm", "")
        emd_nm = addr_info.get("emdNm", "")

        # 구보/구청 검색용 짧은 키워드 (전체 구역명 대신 핵심 지명 + 동이름)
        def _extract_short_keywords(zone_names, dong_nm):
            _STRIP = ["지구단위계획구역", "광역중심", "정비구역", "특별계획구역",
                       "활성화사업", "역세권", "일원", "일대", "지구단위계획"]
            shorts = []
            for name in zone_names:
                short = name
                for s in _STRIP:
                    short = short.replace(s, "").strip()
                if short and len(short) >= 2 and short not in shorts:
                    shorts.append(short)
            if dong_nm and dong_nm not in shorts:
                shorts.append(dong_nm)
            return shorts[:4]

        gazette_keywords = _extract_short_keywords(specific_zone_names, emd_nm) if specific_zone_names else []

        if specific_zone_names:
            def _search_seoul_notices():
                from lookup.seoul_notice import search_seoul_announcements
                return search_seoul_announcements(specific_zone_names[:3], limit=5, timeout=min(to, 15))

            def _search_gu_announce():
                if not district:
                    return []
                from lookup.gu_announce import fetch_gu_announcements
                return fetch_gu_announcements(district, specific_zone_names[:3], limit=5, timeout=min(to, 15))

            def _search_gu_gazette():
                if not district:
                    return []
                from lookup.gu_gazette import fetch_gu_gazette
                return fetch_gu_gazette(district, gazette_keywords, limit=3, timeout=min(to, 15))

            def _search_gu_planning():
                if not district:
                    return []
                from lookup.gu_planning import fetch_gu_planning
                return fetch_gu_planning(district, specific_zone_names[:3], limit=5, timeout=min(to, 15))

            with ThreadPoolExecutor(max_workers=4) as pool2:
                f_seoul = pool2.submit(_safe, _search_seoul_notices)
                f_gu_ann = pool2.submit(_safe, _search_gu_announce)
                f_gu_gaz = pool2.submit(_safe, _search_gu_gazette)
                f_gu_plan = pool2.submit(_safe, _search_gu_planning)

            seoul_notice_results = f_seoul.result() or []
            gu_ann = f_gu_ann.result() or []
            gu_gaz = f_gu_gaz.result() or []
            gu_plan = f_gu_plan.result() or []
            gu_results = gu_ann + gu_gaz + gu_plan

            total_ext = len(seoul_notice_results) + len(gu_results)
            if total_ext:
                logger.info(f"외부 소스 검색: 서울시 {len(seoul_notice_results)}건, "
                           f"구청 {len(gu_ann)}+{len(gu_gaz)}+{len(gu_plan)}건")

        # 폴백 계층: 구체적 구역명 → 동 이름 → 자치구명
        emd_nm = addr_info.get("emdNm", "")
        emd_zone_names = [emd_nm] if emd_nm and emd_nm not in seen else []
        if not district:
            district = addr_info.get("sggNm", "")
        fallback_zone_names = [district] if district else []

        announcements = get_announcements_for_zones(
            specific_zone_names, conn, settings.seoul_api_key, settings.lookback_days
        )
        if not announcements and emd_zone_names:
            announcements = get_announcements_for_zones(
                emd_zone_names, conn, settings.seoul_api_key, settings.lookback_days
            )
        if not announcements and fallback_zone_names:
            announcements = get_announcements_for_zones(
                fallback_zone_names, conn, settings.seoul_api_key, settings.lookback_days
            )
        # 외부 소스 실시간 검색 결과 병합 (DB 중복 제외)
        existing_titles = {a.get("title", "") for a in announcements}
        for ext in seoul_notice_results + gu_results:
            if ext.get("title") and ext["title"] not in existing_titles:
                existing_titles.add(ext["title"])
                announcements.append(ext)

        # 최신순 정렬 + 상위 15건
        announcements.sort(key=lambda a: a.get("published_at", ""), reverse=True)
        announcements = announcements[:15]
        zone_names = specific_zone_names + fallback_zone_names
        result["announcements"] = [dict(a) if hasattr(a, "keys") else a for a in announcements]

        # 5. 고시공고 structured_json 파싱 (DB 캐시된 것만, Claude 호출 없음)
        for ann in result["announcements"]:
            if ann.get("structured_json") and isinstance(ann["structured_json"], str):
                try:
                    ann["structured_json"] = json.loads(ann["structured_json"])
                except Exception:
                    pass

        # 5b. AI 분석이 필요한 고시 정보를 결과에 포함 (클라이언트에서 AJAX로 요청)
        has_ai = any(
            isinstance(a.get("structured_json"), dict)
            and (a["structured_json"].get("building_coverage_ratio") or a["structured_json"].get("floor_area_ratio"))
            for a in result["announcements"]
        )
        if not has_ai and settings.anthropic_api_key:
            primary_zone = ""
            for n in upis_names:
                if n and len(n) > 4:
                    primary_zone = n
                    break

            # 외부 소스에서 카테고리별 본문 + PDF URL 수집 (탭별 분리)
            ext_yeolam = {"body": "", "title": "", "pdf_urls": []}
            ext_gyeoljeong = {"body": "", "title": "", "pdf_urls": []}
            for ext in seoul_notice_results + gu_results:
                ext_cat = ext.get("category", "")
                ext_title_str = ext.get("title", "")
                # 구보는 category="구보"로 고정 → 본문 앞부분으로 열람/결정 판별
                ext_body_head = ext.get("body", "")[:500] if ext_cat == "구보" else ""
                is_yeolam_ext = ("열람" in ext_cat or ("공고" in ext_cat and "결정" not in ext_cat)
                                 or "열람" in ext_title_str
                                 or (ext_cat == "구보" and "열람" in ext_body_head))
                bucket = ext_yeolam if is_yeolam_ext else ext_gyeoljeong
                for u in (ext.get("pdf_urls") or []):
                    if u and u not in bucket["pdf_urls"]:
                        bucket["pdf_urls"].append(u)
                if not bucket["body"] and ext.get("content_quality") == "detailed" and ext.get("body"):
                    bucket["body"] = ext["body"][:10000]
                    bucket["title"] = ext.get("title", "")

            # primary_kw / primary_zone_core 추출
            primary_kw = ""
            primary_zone_core = ""  # 구역명에서 접미사 제거 (예: "왕십리 광역중심")
            if primary_zone:
                kw_tmp = primary_zone
                for suffix in ["지구단위계획구역", "지구단위계획", "정비구역", "특별계획구역", "구역"]:
                    kw_tmp = kw_tmp.replace(suffix, "").strip()
                primary_zone_core = kw_tmp
                primary_kw = kw_tmp.split()[0] if kw_tmp.split() else ""

            # 열람공고 / 결정고시 각각 최적 매칭 (4단계 우선순위)
            # 1) core_focused: zone_core 포함 + 단독 고시 (합본 아님)
            # 2) core_combined: zone_core 포함 + 합본 고시 (제목에 콤마)
            # 3) kw_matched: primary_kw만 포함
            # 4) fallback: 관련 카테고리이지만 키워드 미매칭
            y_cf, y_cc, y_kw, y_fb = None, None, None, None
            g_cf, g_cc, g_kw, g_fb = None, None, None, None
            for ann in result["announcements"]:
                cat = ann.get("category", "")
                title = ann.get("title", "")
                if not any(k in cat for k in ("고시", "구보", "결정", "지구단위", "공고", "열람")) \
                        and "결정" not in title and "열람" not in title:
                    continue
                cn = ann.get("raw_content") or ann.get("cn_content") or ann.get("body") or ""
                if (not cn and not title) or not primary_zone:
                    continue
                is_yeolam = "열람" in cat or ("공고" in cat and "결정" not in cat) or "열람" in title
                is_combined = "," in title  # 합본 고시 감지
                if is_yeolam:
                    if primary_zone_core and primary_zone_core in title:
                        if not is_combined and not y_cf:
                            y_cf = ann
                        elif is_combined and not y_cc:
                            y_cc = ann
                    elif primary_kw and primary_kw in title and not y_kw:
                        y_kw = ann
                    elif not y_fb:
                        y_fb = ann
                else:
                    if primary_zone_core and primary_zone_core in title:
                        if not is_combined and not g_cf:
                            g_cf = ann
                        elif is_combined and not g_cc:
                            g_cc = ann
                    elif primary_kw and primary_kw in title and not g_kw:
                        g_kw = ann
                    elif not g_fb:
                        g_fb = ann

            yeolam_ann = y_cf or y_cc or y_kw or y_fb
            gyeoljeong_ann = g_cf or g_cc or g_kw or g_fb

            upis_ct = ""
            if isinstance(upis_data, dict):
                ntfc = upis_data.get("notification") or {}
                upis_ct = ntfc.get("content", "") if isinstance(ntfc, dict) else ""

            def _build_tab(target_ann, ext_bucket):
                if not target_ann or not primary_zone:
                    return None
                cn = target_ann.get("raw_content") or target_ann.get("cn_content") or target_ann.get("body") or ""
                gazette_ref_val = cn if cn else target_ann.get("title", "")
                ann_cn = ext_bucket["body"] or cn[:10000]
                # 탭별 PDF URL: 해당 고시 + 해당 카테고리 외부소스만
                tab_pdfs = []
                for u in (target_ann.get("pdf_urls") or []):
                    if u and u not in tab_pdfs:
                        tab_pdfs.append(u)
                for u in ext_bucket.get("pdf_urls", []):
                    if u and u not in tab_pdfs:
                        tab_pdfs.append(u)
                return {
                    "ann_title": ext_bucket["title"] or target_ann.get("title", ""),
                    "ann_cn": ann_cn,
                    "gazette_ref": gazette_ref_val[:500],
                    "content_quality": "detailed" if ext_bucket["body"] else target_ann.get("content_quality", "summary"),
                    "pdf_urls": tab_pdfs[:5],
                }

            yeolam_data = _build_tab(yeolam_ann, ext_yeolam)
            gyeoljeong_data = _build_tab(gyeoljeong_ann, ext_gyeoljeong)
            logger.info(f"[탭매칭] zone_core='{primary_zone_core}' | "
                        f"결정={gyeoljeong_ann.get('title','없음')[:60] if gyeoljeong_ann else '없음'} | "
                        f"열람={yeolam_ann.get('title','없음')[:60] if yeolam_ann else '없음'}")

            if yeolam_data or gyeoljeong_data:
                result["_ai_pending_tabs"] = {
                    "zone_name": primary_zone,
                    "upis_content": upis_ct[:10000],
                    "yeolam": yeolam_data,
                    "gyeoljeong": gyeoljeong_data,
                }
                # 하위 호환: _ai_pending도 유지
                compat_data = gyeoljeong_data or yeolam_data
                result["_ai_pending"] = {
                    "zone_name": primary_zone,
                    "gazette_ref": compat_data["gazette_ref"],
                    "ann_title": compat_data["ann_title"],
                    "ann_cn": compat_data["ann_cn"],
                    "upis_content": upis_ct[:10000],
                    "content_quality": compat_data["content_quality"],
                    "pdf_urls": compat_data["pdf_urls"],
                }

        # 조회 이력 저장
        log_lookup(conn, address, pnu, zone_names, result)

    except ValueError as e:
        result["error"] = str(e)
        logger.info(f"조회 실패 ('{address}'): {e}")
    except Exception as e:
        result["error"] = f"시스템 오류: {e}"
        logger.error(f"조회 오류 ('{address}'): {e}", exc_info=True)
    finally:
        conn.close()

    return result
