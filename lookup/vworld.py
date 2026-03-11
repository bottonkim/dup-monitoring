"""
좌표 -> 도시계획 구역 정보 조회
VWORLD Data API / WFS API (국가공간정보오픈플랫폼) 사용

확인된 레이어 (Data API 정상 동작, 2026년 3월 기준):
  lt_c_uq111: 용도지역 (uname: 제2종일반주거지역 등)
  lt_c_uq121: 용도지구 (uname: 자연경관지구 등)
  lt_c_uq123: 고도지구
  lt_c_uq124: 방화지구
  lt_c_uq125: 보호지구
  lt_c_uq126: 취락지구
  lt_c_uq129: 개발진흥지구
  lt_c_uq130: 특정용도제한지구
  lt_c_uq141: 도시계획구역 복합 (uname: 지구단위계획구역, 토지거래계약허가구역 등)
  lt_c_uq162: 공원/공공공지
  lt_c_up201: 재해위험지구

WFS API 전용 레이어 (2026년 3월 기준, CQL_FILTER 좌표 필터 동작 확인):
  lt_c_upisuq151: 도시계획(도로) - atr_nam 필드가 실제 도로명(소로2류 등)
  lt_c_ud801: 개발제한구역
  lt_c_uo101: 교육환경보호구역 (remark 필드에 학교명 포함)
  lt_c_uo301: 국가유산 지정/보호구역
  ※ lt_c_uf151(산림보호구역)은 좌표 필터 오동작으로 제외

각 레이어의 uname 필드가 실제 구역 유형명을 담고 있음.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
import requests

logger = logging.getLogger(__name__)

_VWORLD_DATA_URL = "https://api.vworld.kr/req/data"

# (layer, fallback_category) - uname 필드가 없을 때 사용
_LAYERS = [
    ("lt_c_uq111", "용도지역"),
    ("lt_c_uq121", "용도지구"),
    ("lt_c_uq123", "고도지구"),
    ("lt_c_uq124", "방화지구"),
    ("lt_c_uq125", "보호지구"),
    ("lt_c_uq126", "취락지구"),
    ("lt_c_uq129", "개발진흥지구"),
    ("lt_c_uq130", "특정용도제한지구"),
    ("lt_c_uq141", "도시계획구역"),
    ("lt_c_uq162", "공원/공공공지"),
    ("lt_c_up201", "재해위험지구"),
]

def query_planning_zones(x: float, y: float, api_key: str, timeout: int = 15) -> list[dict]:
    """
    좌표(경도 x, 위도 y)로 해당 위치의 도시계획 구역 목록 조회

    Returns:
        [
            {
                "zone_type": "제2종일반주거지역",   # uname 값 (실제 구역 유형명)
                "zone_name": "제2종일반주거지역",   # 동일 (VWORLD는 세부명 없음)
                "layer": "lt_c_uq111",
                "designated_year": "2003",
                "gazette_number": "0456",
                "sigg_name": "성동구",
            },
            ...
        ]
    중복 제거됨 (같은 zone_type + layer 조합은 1개만 유지).
    """
    def _query_layer(layer, fallback):
        try:
            return layer, fallback, _fetch_features(layer, x, y, api_key, timeout)
        except Exception as e:
            logger.debug(f"레이어 {layer} 조회 건너뜀: {e}")
            return layer, fallback, []

    seen = set()
    zones = []
    with ThreadPoolExecutor(max_workers=len(_LAYERS)) as pool:
        futures = [pool.submit(lambda l=layer, f=fb: _query_layer(l, f)) for layer, fb in _LAYERS]
        for fut in futures:
            layer, fallback, features = fut.result()
            for feat in features:
                props = feat.get("properties", {})
                uname = props.get("uname", "").strip()
                if uname in ('미분류', ''):
                    continue
                zone_type = uname or fallback
                key = (layer, zone_type)
                if key in seen:
                    continue
                seen.add(key)
                zones.append({
                    "zone_type": zone_type,
                    "zone_name": zone_type,
                    "layer": layer,
                    "designated_year": props.get("dyear", ""),
                    "gazette_number": props.get("dnum", ""),
                    "sigg_name": props.get("sigg_name", ""),
                })
    return zones


_VWORLD_NED_URL = "https://api.vworld.kr/ned/data/getLandCharacteristics"


def fetch_parcel_info(pnu: str, api_key: str, timeout: int = 15) -> dict:
    """
    PNU로 필지 기본정보(지목, 면적) 조회
    VWORLD NED getLandCharacteristics API 사용

    Returns:
        {"jimok": "대", "area": "171.6 ㎡"} or {}
    """
    import datetime
    current_year = datetime.date.today().year
    # juso.go.kr mtYn이 틀리는 경우 대비: 원본 + 반전값 모두 시도
    mt_yn_alt = "1" if pnu[10] == "0" else "0"
    pnu_alt = pnu[:10] + mt_yn_alt + pnu[11:]
    candidates = [pnu, pnu_alt]

    for try_pnu in candidates:
        # 최신 연도부터 시도 (최대 3년 전까지)
        for year in range(current_year, current_year - 3, -1):
            try:
                resp = requests.get(
                    _VWORLD_NED_URL,
                    params={
                        "key": api_key,
                        "pnu": try_pnu,
                        "stdrYear": str(year),
                        "numOfRows": "1",
                        "pageNo": "1",
                        "format": "json",
                    },
                    timeout=timeout,
                )
                resp.raise_for_status()
                resp.encoding = "utf-8"
                data = resp.json()

                wrapper = data.get("landCharacteristicss", {})
                if wrapper.get("resultCode") and wrapper["resultCode"] not in ("", "00"):
                    continue

                fields = wrapper.get("field", [])
                if not fields:
                    continue

                f = fields[0]
                jimok = str(f.get("lndcgrCodeNm", "")).strip()
                area_raw = f.get("lndpclAr", "")
                area = ""
                if area_raw:
                    try:
                        area = f"{float(area_raw):.1f} ㎡"
                    except (ValueError, TypeError):
                        area = str(area_raw)

                if jimok or area:
                    logger.debug(f"필지정보 조회 성공 (PNU={try_pnu}, year={year}): 지목={jimok}, 면적={area}")
                    return {"jimok": jimok, "area": area}
            except Exception as e:
                logger.debug(f"필지정보 조회 실패 (PNU={try_pnu}, year={year}): {e}")
    return {}


# 지구단위계획구역, 정비구역, 특별계획구역 전용 레이어 (성공 시 구체적 구역명 반환)
_ZONE_NAME_LAYERS = [
    ("lt_c_ud501", "지구단위계획구역"),
    ("lt_c_ub011", "정비구역"),
    ("lt_c_ud502", "특별계획구역"),
]


def fetch_zone_specific_names(x: float, y: float, api_key: str, timeout: int = 15) -> list[dict]:
    """
    좌표로 지구단위계획구역/정비구역/특별계획구역의 구체적 구역명 조회 시도
    VWORLD 전용 레이어 사용 (레이어 접근 불가 시 빈 리스트 반환)

    Returns:
        [{"zone_type": "지구단위계획구역", "zone_name": "삼성동 제1지구단위계획구역", ...}, ...]
    """
    def _query_zone_layer(layer, zone_type):
        try:
            params = {
                "service": "data",
                "request": "GetFeature",
                "data": layer,
                "key": api_key,
                "domain": "localhost",
                "format": "json",
                "size": "5",
                "page": "1",
                "geomFilter": f"POINT({x} {y})",
                "crs": "EPSG:4326",
            }
            resp = requests.get(_VWORLD_DATA_URL, params=params, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            response = data.get("response", {})
            if response.get("status") != "OK":
                return []
            if response.get("record", {}).get("total", "0") == "0":
                return []
            features = (response
                        .get("result", {})
                        .get("featureCollection", {})
                        .get("features", []))
            items = []
            seen = set()
            for feat in features:
                props = feat.get("properties", {})
                name = ""
                for nf in ("planNm", "planName", "plandNm", "zoneName", "uname", "nm", "name"):
                    v = str(props.get(nf, "")).strip()
                    if v and v not in ("None", "null", "", "미분류"):
                        name = v
                        break
                if name and name not in seen:
                    seen.add(name)
                    items.append({
                        "zone_type": zone_type,
                        "zone_name": name,
                        "layer": layer,
                        "designated_year": props.get("dyear", ""),
                        "gazette_number": props.get("dnum", ""),
                        "sigg_name": props.get("sigg_name", ""),
                    })
            return items
        except Exception as e:
            logger.debug(f"구역명 레이어 {layer} 조회 실패: {e}")
            return []

    results = []
    with ThreadPoolExecutor(max_workers=len(_ZONE_NAME_LAYERS)) as pool:
        futures = [pool.submit(lambda l=layer, zt=zone_type: _query_zone_layer(l, zt))
                   for layer, zone_type in _ZONE_NAME_LAYERS]
        for fut in futures:
            results.extend(fut.result())
    return results


def _fetch_features(layer: str, x: float, y: float, api_key: str, timeout: int) -> list[dict]:
    """VWORLD Data API로 단일 레이어 피처 조회"""
    params = {
        "service": "data",
        "request": "GetFeature",
        "data": layer,
        "key": api_key,
        "domain": "localhost",
        "format": "json",
        "size": "20",
        "page": "1",
        "geomFilter": f"POINT({x} {y})",
        "crs": "EPSG:4326",
    }
    resp = requests.get(_VWORLD_DATA_URL, params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    response = data.get("response", {})
    if response.get("status") != "OK":
        return []
    if response.get("record", {}).get("total", "0") == "0":
        return []

    return (response
            .get("result", {})
            .get("featureCollection", {})
            .get("features", []))


# ─── WFS API 전용 레이어 ─────────────────────────────────────────────────────
_VWORLD_WFS_URL = "https://api.vworld.kr/req/wfs"

# (layer, zone_type, fallback_name, law_category, name_fields)
_WFS_LAYERS = [
    # 도시계획시설(도로) — 국토계획법
    ("lt_c_upisuq151", "도시계획도로", "도로", "국토계획법",
     ["atr_nam", "dgm_nm", "lcl_nam"]),
    # 개발제한구역 — 개발제한구역의 지정 및 관리에 관한 특별조치법
    ("lt_c_ud801", "개발제한구역", "개발제한구역", "다른 법령",
     ["uname", "nm", "name"]),
    # 교육환경보호구역 — 교육환경 보호에 관한 법률 (remark에 학교명 포함)
    ("lt_c_uo101", "교육환경보호구역", "교육환경보호구역", "다른 법령",
     ["uname", "remark", "alias", "nm", "name"]),
    # 국가유산 보호구역 — 국가유산기본법
    ("lt_c_uo301", "국가유산보호구역", "국가유산보호구역", "다른 법령",
     ["uname", "nm", "name"]),
]


def fetch_wfs_zones(x: float, y: float, api_key: str, timeout: int = 15) -> list[dict]:
    """
    VWORLD WFS API로 추가 도시계획 구역 조회
    (Data API 미지원 레이어: 도시계획도로, 개발제한구역 등)

    Returns:
        [{"zone_type": "도시계획도로", "zone_name": "소로2류",
          "layer": "lt_c_upisuq151", "law_category": "국토계획법", ...}, ...]
    """
    def _query_wfs_layer(layer, zone_type, fallback_name, law_cat, name_fields):
        try:
            features = _fetch_wfs_features(layer, x, y, api_key, timeout)
            items = []
            seen: set[str] = set()
            for feat in features:
                props = feat.get("properties", {})
                name = ""
                for nf in name_fields:
                    v = str(props.get(nf, "")).strip()
                    if v and v not in ("None", "null", "", "미분류"):
                        name = v
                        break
                if not name:
                    name = fallback_name
                if name in seen:
                    continue
                seen.add(name)
                gazette = str(
                    props.get("wtnnc_sn") or props.get("ntfc_sn") or
                    props.get("dnum") or ""
                ).strip()
                year = _extract_year_from_sn(gazette) if gazette else str(props.get("dyear", ""))
                items.append({
                    "zone_type": zone_type,
                    "zone_name": name,
                    "layer": layer,
                    "designated_year": year,
                    "gazette_number": gazette,
                    "sigg_name": str(props.get("sig_nam") or props.get("sigg_name") or ""),
                    "law_category": law_cat,
                })
            return items
        except Exception as e:
            logger.debug(f"WFS 레이어 {layer} 조회 건너뜀: {e}")
            return []

    results = []
    with ThreadPoolExecutor(max_workers=len(_WFS_LAYERS)) as pool:
        futures = [pool.submit(lambda l=layer, zt=zone_type, fn=fallback_name, lc=law_cat, nf=name_fields:
                               _query_wfs_layer(l, zt, fn, lc, nf))
                   for layer, zone_type, fallback_name, law_cat, name_fields in _WFS_LAYERS]
        for fut in futures:
            results.extend(fut.result())
    return results


def _extract_year_from_sn(sn: str) -> str:
    """고시번호 문자열에서 연도 추출 (예: '11000NTC20080529...' → '2008')"""
    import re
    m = re.search(r"(20\d{2})", sn)
    return m.group(1) if m else ""


def _fetch_wfs_features(layer: str, x: float, y: float, api_key: str, timeout: int) -> list[dict]:
    """VWORLD WFS API로 단일 레이어 피처 조회 (CQL_FILTER 좌표 필터)"""
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeName": layer,
        "key": api_key,
        "domain": "localhost",
        "outputFormat": "application/json",
        "CQL_FILTER": f"INTERSECTS(the_geom,POINT({x} {y}))",
        "srsName": "EPSG:4326",
        "count": "20",
    }
    resp = requests.get(_VWORLD_WFS_URL, params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return data.get("features", [])
