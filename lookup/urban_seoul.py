"""
서울시 도시계획정보서비스 (urban.seoul.go.kr) ArcGIS 프록시 API 조회
VWORLD에서 제공하지 않는 상세 구역명 (지구단위계획구역명, 개발제한구역명 등) 보완
"""
import logging
import json
from urllib.parse import quote, urljoin
import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://urban.seoul.go.kr"
_PROXY_URL = f"{_BASE_URL}/proxy/proxy.jsp"
_ARCGIS_BASE = "http://98.33.2.225:6080/arcgis/rest/services/UPIS/20200526_WFS/MapServer"
_LIST_API = f"{_BASE_URL}/api/map/pilji/getList.json"

# ArcGIS layer ID → (layer_name, zone_type_kor)
# C prefix = 현재(current), H = 이력(historical), P = 미래계획(planned)
_QUERY_LAYERS = {
    63: ("UPIS_C_UQ111", "용도지역"),
    25: ("UPIS_C_UQ141", "도시개발구역"),   # + 개발제한구역
    28: ("UPIS_C_UQ161", "지구단위계획구역"),
    29: ("UPIS_C_UQ165", "지구단위계획구역"),
    3:  ("UPIS_C_UQ151", "지구단위계획(가로)"),
    4:  ("UPIS_C_UQ152", "지구단위계획(철도)"),
    13: ("UPIS_C_UQ121", "용도지구"),
    15: ("UPIS_C_UQ123", "고도지구"),
    17: ("UPIS_C_UQ125", "보호지구"),
    18: ("UPIS_C_UQ126", "취락지구"),
    21: ("UPIS_C_UQ129", "개발진흥지구"),
    22: ("UPIS_C_UQ130", "특정용도제한지구"),
    26: ("UPIS_C_UQ142", "도시개발구역(예정)"),
    34: ("UPIS_C_UQ162", "공원"),
    64: ("UPIS_C_UQ181", "토지구획정리사업"),
    65: ("UPIS_C_UQ191", "주거환경정비사업"),
    66: ("UPIS_C_UNEXCUT", "미집행도시계획시설"),
}

# UQ141 only has 2 city-wide features → must check intersection manually
_LARGE_POLYGON_LAYERS = {25}


def _to_upis_pnu(pnu: str) -> str:
    """juso.go.kr PNU → UPIS/토이이음 PNU 변환 (mtYn: '0'→'1', '1'→'2')."""
    if len(pnu) != 19:
        return pnu
    mt = pnu[10]
    if mt == "0":
        return pnu[:10] + "1" + pnu[11:]
    elif mt == "1":
        return pnu[:10] + "2" + pnu[11:]
    return pnu  # 이미 UPIS 포맷


def fetch_zone_names(pnu: str, timeout: int = 20) -> list[dict]:
    """
    PNU로 urban.seoul.go.kr UPIS ArcGIS API에서 상세 구역명 조회.

    Returns:
        [
          {"zone_type": "지구단위계획구역", "zone_name": "왕십리 광역중심 지구단위계획구역",
           "location": "성동구 도선동 ...", "gazette": "서울시_제1999-32호", "layer": "UPIS_C_UQ161"},
          ...
        ]
    """
    if not pnu or len(pnu) != 19:
        return []

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"{_BASE_URL}/view/map/main.html?pnu={pnu}",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded",
    })

    try:
        # LP_PA_CBND는 UPIS PNU 포맷(mtYn '0'→'1') 사용
        upis_pnu = _to_upis_pnu(pnu)

        # Step 1: 필지 geometry 취득 (LP_PA_CBND layer 1)
        bbox = _get_parcel_bbox(sess, upis_pnu, timeout)
        if not bbox:
            # 원본 PNU로도 시도
            bbox = _get_parcel_bbox(sess, pnu, timeout)
        if not bbox:
            logger.debug(f"urban.seoul.go.kr: PNU={pnu} 필지 좌표 조회 실패")
            return []

        xmin, ymin, xmax, ymax = bbox

        # Step 2: 각 구역 레이어에서 필지와 교차하는 피처 조회
        wtnnc_map: dict[str, str] = {}  # WTNNC_SN → layer_name
        _query_zone_layers(sess, xmin, ymin, xmax, ymax, wtnnc_map, timeout)

        if not wtnnc_map:
            return []

        # Step 3: getList.json으로 각 WTNNC_SN의 구역명 조회
        results = []
        seen_names: set[str] = set()  # zone_name 중복 제거
        for wt, layer_name in wtnnc_map.items():
            zone_items = _get_zone_details(sess, wt, timeout)
            for item in zone_items:
                zone_name = item.get("zoneName") or ""
                location = item.get("locationName") or ""
                gazette = item.get("firstDateInfo") or ""
                if not (zone_name or location):
                    continue
                # zone_name이 없으면 location 앞 30자를 식별키로 사용
                dedup_key = zone_name or location[:30]
                if dedup_key in seen_names:
                    continue
                seen_names.add(dedup_key)
                zone_type = _QUERY_LAYERS.get(
                    _layer_to_id(layer_name), (layer_name, layer_name)
                )[1]
                results.append({
                    "zone_type": zone_type,
                    "zone_name": zone_name,
                    "location": location,
                    "gazette": gazette,
                    "layer": layer_name,
                    "wtnnc_sn": wt,
                })

        logger.debug(f"urban.seoul.go.kr 구역명 조회 결과 (PNU={pnu}): {len(results)}건")
        return results

    except Exception as e:
        logger.warning(f"urban.seoul.go.kr 조회 오류 (PNU={pnu}): {e}")
        return []


def _get_parcel_bbox(sess: requests.Session, pnu: str, timeout: int):
    """LP_PA_CBND (layer 1)에서 PNU로 필지 bbox 취득. Returns (xmin, ymin, xmax, ymax) or None."""
    target = (
        f"{_ARCGIS_BASE}/1/query"
        f"?where=PNU%3D%27{pnu}%27"
        f"&outFields=PNU&returnGeometry=true&f=json"
    )
    resp = sess.get(f"{_PROXY_URL}?{target}", timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    features = data.get("features", [])
    if not features:
        return None
    rings = features[0]["geometry"]["rings"][0]
    xs = [p[0] for p in rings]
    ys = [p[1] for p in rings]
    return (min(xs) - 5, min(ys) - 5, max(xs) + 5, max(ys) + 5)


def _query_zone_layers(
    sess: requests.Session,
    xmin: float, ymin: float, xmax: float, ymax: float,
    wtnnc_map: dict,
    timeout: int,
):
    """각 UPIS 구역 레이어에서 교차 피처의 WTNNC_SN 수집."""
    geom_env = {
        "xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax,
        "spatialReference": {"wkid": 102086},
    }
    q_geom = (
        f"geometry={quote(json.dumps(geom_env))}"
        f"&geometryType=esriGeometryEnvelope"
        f"&inSR=102086"
        f"&spatialRel=esriSpatialRelIntersects"
        f"&outFields=WTNNC_SN,PRESENT_SN,DGM_NM"
        f"&returnGeometry=false&f=json"
    )

    for lid, (lname, _) in _QUERY_LAYERS.items():
        try:
            if lid in _LARGE_POLYGON_LAYERS:
                # 전체 피처 취득 후 bbox 교차 수동 확인
                target = (
                    f"{_ARCGIS_BASE}/{lid}/query"
                    f"?where=1%3D1&outFields=WTNNC_SN,PRESENT_SN,DGM_NM"
                    f"&returnGeometry=true&f=json"
                )
                resp = sess.get(f"{_PROXY_URL}?{target}", timeout=timeout)
                data = resp.json()
                for f in data.get("features", []):
                    geom = f.get("geometry", {})
                    rings = geom.get("rings", [[]])
                    if rings and rings[0]:
                        fxs = [p[0] for p in rings[0]]
                        fys = [p[1] for p in rings[0]]
                        if not (max(fxs) < xmin or min(fxs) > xmax or
                                max(fys) < ymin or min(fys) > ymax):
                            wt = f["attributes"].get("WTNNC_SN", "")
                            if wt and wt not in wtnnc_map:
                                wtnnc_map[wt] = lname
            else:
                target = f"{_ARCGIS_BASE}/{lid}/query?{q_geom}"
                resp = sess.get(f"{_PROXY_URL}?{target}", timeout=timeout)
                data = resp.json()
                for f in data.get("features", []):
                    wt = f["attributes"].get("WTNNC_SN", "")
                    if wt and wt not in wtnnc_map:
                        wtnnc_map[wt] = lname
        except Exception as e:
            logger.debug(f"Layer {lid} ({lname}) 조회 오류: {e}")


def _get_zone_details(sess: requests.Session, wtnnc_sn: str, timeout: int) -> list[dict]:
    """getList.json API로 WTNNC_SN에 해당하는 구역 상세 정보 반환."""
    try:
        resp = sess.post(
            _LIST_API,
            data=(
                f"recordCode={wtnnc_sn}"
                f"&recordCodeH=&presentSn=&restrictN=&bsnsPresentSn=&dgmNmYd="
            ),
            timeout=timeout,
        )
        if not resp.content:
            return []
        data = resp.json()
        results = []
        for key in ["usgarWtnnc", "ubplfcWtnnc", "spcfWtnnc", "etczoneWtnnc",
                    "dstplanWtnnc", "fcmtrWtnnc"]:
            for item in data.get(key, []):
                if item and isinstance(item, dict):
                    zn = item.get("zoneName") or ""
                    loc = item.get("locationName") or ""
                    if zn or loc:
                        results.append(item)
        return results
    except Exception as e:
        logger.debug(f"getList.json 오류 (WTNNC={wtnnc_sn}): {e}")
        return []


def _layer_to_id(layer_name: str) -> int:
    """레이어명 → layer ID 역매핑."""
    for lid, (lname, _) in _QUERY_LAYERS.items():
        if lname == layer_name:
            return lid
    return -1
