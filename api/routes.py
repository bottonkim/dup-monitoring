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
            settings,
        )
        return JSONResponse(content=result)

    return app


def _run_gazette_analysis(
    zone_name: str, gazette_ref: str, ann_title: str, ann_cn: str,
    upis_content: str, settings,
) -> dict:
    """시보 PDF 분석 또는 CN/UPIS 내용 분석 (동기, 3단계 폴백, 세마포어로 동시 1건)"""
    acquired = _pdf_analysis_lock.acquire(timeout=5)
    try:
        return _run_gazette_analysis_inner(
            zone_name, gazette_ref, ann_title, ann_cn, upis_content, settings
        )
    finally:
        if acquired:
            _pdf_analysis_lock.release()


def _run_gazette_analysis_inner(
    zone_name: str, gazette_ref: str, ann_title: str, ann_cn: str,
    upis_content: str, settings,
) -> dict:
    """시보 PDF 분석 또는 CN/UPIS 내용 분석 (3단계 폴백)"""
    # 1차: 시보 PDF 분석 (subprocess 격리)
    if gazette_ref:
        try:
            from lookup.gazette_pdf import analyze_gazette_for_zone
            analysis = analyze_gazette_for_zone(
                gazette_ref, zone_name,
                settings.anthropic_api_key, settings.pdf_cache_dir,
                settings.claude_model,
            )
            if analysis and not analysis.get("error"):
                return analysis
        except Exception as e:
            logger.debug(f"시보 PDF 분석 실패: {e}")

    # 2차: CN 내용 분석 폴백
    if ann_cn and len(ann_cn) >= 10:
        try:
            from lookup.announcements import analyze_announcement_with_claude
            analysis = analyze_announcement_with_claude(
                ann_title, ann_cn,
                settings.anthropic_api_key, settings.claude_model,
            )
            if analysis and not analysis.get("error"):
                return analysis
        except Exception as e:
            logger.debug(f"Claude 고시 분석 실패: {e}")

    # 3차: UPIS 고시 content 분석 폴백
    if upis_content and len(upis_content) >= 10:
        try:
            from lookup.announcements import analyze_announcement_with_claude
            analysis = analyze_announcement_with_claude(
                f"{zone_name} 결정고시", upis_content,
                settings.anthropic_api_key, settings.claude_model,
            )
            if analysis and not analysis.get("error"):
                return analysis
        except Exception as e:
            logger.debug(f"UPIS 고시 분석 실패: {e}")

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
                        "drawing_documents": [], "portal_url": "", "notice_url": None}
            from lookup.urban_seoul import fetch_zone_data
            return fetch_zone_data(pnu, timeout=min(to, 25))

        def _task_tojieum():
            return fetch_land_use_plan(pnu, to, jibun_address=addr_info.get("address_full", ""))

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

        # 폴백 계층: 구체적 구역명 → 동 이름 → 자치구명
        emd_nm = addr_info.get("emdNm", "")
        emd_zone_names = [emd_nm] if emd_nm and emd_nm not in seen else []
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
            # 분석 대상 고시 찾기
            for ann in result["announcements"]:
                cat = ann.get("category", "")
                if "고시" not in cat and "결정" not in (ann.get("title", "")):
                    continue
                cn = ann.get("raw_content") or ann.get("cn_content") or ""
                gazette_ref = cn if cn else ann.get("title", "")
                if gazette_ref and primary_zone:
                    upis_ct = ""
                    if isinstance(upis_data, dict):
                        ntfc = upis_data.get("notification") or {}
                        upis_ct = ntfc.get("content", "") if isinstance(ntfc, dict) else ""
                    result["_ai_pending"] = {
                        "zone_name": primary_zone,
                        "gazette_ref": gazette_ref[:500],
                        "ann_title": ann.get("title", ""),
                        "ann_cn": cn[:3000],
                        "upis_content": upis_ct[:3000],
                    }
                    break

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
