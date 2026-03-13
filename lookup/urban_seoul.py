"""
서울시 도시계획정보서비스 (urban.seoul.go.kr) ArcGIS 프록시 API 조회
VWORLD에서 제공하지 않는 상세 구역명 (지구단위계획구역명, 개발제한구역명 등) 보완
+ 지구단위계획 결정/변경 연혁 및 고시 정보 추출
"""
import logging
import json
import re
from urllib.parse import quote, urljoin
import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://urban.seoul.go.kr"
_PROXY_URL = f"{_BASE_URL}/proxy/proxy.jsp"
_ARCGIS_BASE = "http://98.33.2.225:6080/arcgis/rest/services/UPIS/20200526_WFS/MapServer"
_LIST_API = f"{_BASE_URL}/api/map/pilji/getList.json"
_NTFC_LIST_API = f"{_BASE_URL}/ntfc/getNtfcList.json"
_NTFC_DT_API = f"{_BASE_URL}/ntfc/getNtfcDt.json"
_CUQ161_API = f"{_BASE_URL}/dstplan/getCUq161.json"
_PROPEL_LIST_API = f"{_BASE_URL}/dstplan/getPropelList.json"

# 사업정보 파일 그룹코드 → 한글 라벨
_FILE_GROUP_LABELS = {
    "DFL01": "고시문",
    "DFL02": "결정도",
    "DFL03": "결정조서",
    "DFL04": "민간시행지침",
    "DFL05": "공공시행지침",
    "DFL06": "기타자료",
}

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

# getList.json API 카테고리 → 한글 라벨
_CATEGORY_LABELS = {
    "usgarWtnnc": "용도지역/지구/구역",
    "ubplfcWtnnc": "도시계획시설",
    "spcfWtnnc": "특정구역",
    "etczoneWtnnc": "기타구역",
    "dstplanWtnnc": "지구단위계획",
    "fcmtrWtnnc": "시설물",
}


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


def fetch_zone_data(pnu: str, timeout: int = 20) -> dict:
    """
    PNU로 urban.seoul.go.kr에서 구역명 + 최신 고시 정보 + 연혁 조회.

    Returns:
        {
          "zones": [...],  # fetch_zone_names() 와 동일
          "notification": {  # 최신 고시 정보 (없으면 None)
              "notice_no": "2024-88",
              "notice_date": "2024-02-15",
              "title": "...",
              "content": "...",
              "notice_code": "11200NTC202403150003",
              "site": "성동구청 ...",
              "charger": "김영재",
              "phone": "02-...",
          },
          "gazette_history": [  # content에서 파싱한 고시 연혁
              {"no": "2024-88", "date": "2024-02-15", "desc": "결정(변경)"},
              {"no": "2016-220", "date": "2016-07-28", "desc": "결정(변경)"},
              ...
          ],
          "drawing_documents": [  # 도면 문서 목록
              {"name": "참고자료_...", "code": "11200DRI..."},
              ...
          ],
          "portal_url": "https://urban.seoul.go.kr/view/map/main.html?pnu=...",
          "notice_url": "https://urban.seoul.go.kr/view/html/PMNU4030100001?noticeCode=...",
        }
    """
    result = {
        "zones": [],
        "notification": None,
        "gazette_history": [],
        "drawing_documents": [],
        "all_notifications": [],
        "portal_url": f"https://urban.seoul.go.kr/view/map/main.html?pnu={pnu}",
        "notice_url": None,
    }

    if not pnu or len(pnu) != 19:
        return result

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"{_BASE_URL}/view/map/main.html?pnu={pnu}",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded",
    })

    try:
        upis_pnu = _to_upis_pnu(pnu)

        bbox = _get_parcel_bbox(sess, upis_pnu, timeout)
        if not bbox:
            bbox = _get_parcel_bbox(sess, pnu, timeout)
        if not bbox:
            return result

        xmin, ymin, xmax, ymax = bbox

        wtnnc_map: dict[str, str] = {}
        _query_zone_layers(sess, xmin, ymin, xmax, ymax, wtnnc_map, timeout)

        if not wtnnc_map:
            return result

        zones = []
        seen_names: set[str] = set()
        best_ntfc = None
        all_notifications = []

        for wt, layer_name in wtnnc_map.items():
            zone_items, ntfc_data, wt_notifications = _get_zone_details_full(sess, wt, timeout)

            for item in zone_items:
                zone_name = item.get("zoneName") or ""
                location = item.get("locationName") or ""
                gazette = item.get("firstDateInfo") or ""
                if not (zone_name or location):
                    continue
                dedup_key = zone_name or location[:30]
                if dedup_key in seen_names:
                    continue
                seen_names.add(dedup_key)
                zone_type = _QUERY_LAYERS.get(
                    _layer_to_id(layer_name), (layer_name, layer_name)
                )[1]
                zones.append({
                    "zone_type": zone_type,
                    "zone_name": zone_name,
                    "location": location,
                    "gazette": gazette,
                    "layer": layer_name,
                    "wtnnc_sn": wt,
                })

            # 전체 카테고리 알림 수집
            all_notifications.extend(wt_notifications)

            # best: dstplanWtnnc 최우선
            if ntfc_data and ntfc_data.get("notice_no"):
                if best_ntfc is None:
                    best_ntfc = ntfc_data
                elif ntfc_data.get("category_key") == "dstplanWtnnc" and best_ntfc.get("category_key") != "dstplanWtnnc":
                    best_ntfc = ntfc_data
                else:
                    new_date = ntfc_data.get("notification", {}).get("notice_date", "")
                    old_date = best_ntfc.get("notification", {}).get("notice_date", "")
                    if new_date > old_date:
                        best_ntfc = ntfc_data

        result["zones"] = zones
        # 전체 카테고리 알림 (zone_name 기준 중복 제거)
        seen_ntfc = set()
        deduped_notifications = []
        for ntfc in all_notifications:
            key = (ntfc.get("category_key", ""), ntfc.get("zone_name", ""))
            if key not in seen_ntfc:
                seen_ntfc.add(key)
                deduped_notifications.append(ntfc)
        result["all_notifications"] = deduped_notifications

        # 지구단위계획 연혁 보강 (getNtfcList.json 키워드 검색)
        for ntfc in deduped_notifications:
            if ntfc.get("category_key") == "dstplanWtnnc" and ntfc.get("zone_name"):
                ntfc_content = ntfc.get("notification", {}).get("content", "")
                ntfc["gazette_history"] = _enrich_history_from_ntfc_api(
                    sess, ntfc["zone_name"],
                    ntfc.get("gazette_history", []),
                    ntfc_content=ntfc_content,
                    timeout=min(timeout, 15),
                )

        # gazette_history/drawing: 보강된 dstplanWtnnc 최우선, 없으면 best_ntfc
        dstplan_ntfc = None
        for ntfc in deduped_notifications:
            if ntfc.get("category_key") == "dstplanWtnnc" and ntfc.get("gazette_history"):
                if dstplan_ntfc is None or len(ntfc.get("gazette_history", [])) > len(dstplan_ntfc.get("gazette_history", [])):
                    dstplan_ntfc = ntfc

        history_source = dstplan_ntfc or best_ntfc
        if best_ntfc:
            result["notification"] = best_ntfc.get("notification")
            notice_code = best_ntfc.get("notification", {}).get("notice_code", "")
            if notice_code:
                result["notice_url"] = (
                    f"https://urban.seoul.go.kr/view/html/PMNU4030100001"
                    f"?noticeCode={notice_code}"
                )
        if history_source:
            result["gazette_history"] = history_source.get("gazette_history", [])
            result["drawing_documents"] = history_source.get("drawing_documents", [])

        # 사업정보 파일 목록 보강 (getPropelList — 각 고시별 전체 첨부파일)
        # all_notifications 내 dstplanWtnnc의 gazette_history에 매핑
        dstplan_wt = None
        for z in zones:
            if z.get("zone_type") in ("지구단위계획구역",):
                dstplan_wt = z.get("wtnnc_sn")
                break
        if dstplan_wt:
            for ntfc in deduped_notifications:
                if ntfc.get("category_key") == "dstplanWtnnc" and ntfc.get("gazette_history"):
                    _enrich_files_from_propel(
                        sess, dstplan_wt, ntfc["gazette_history"], timeout
                    )
                    break  # 하나의 dstplanWtnnc만 처리

        logger.debug(
            f"urban.seoul.go.kr 조회 (PNU={pnu}): "
            f"{len(zones)}구역, 고시={'있음' if best_ntfc else '없음'}"
        )
        return result

    except Exception as e:
        logger.warning(f"urban.seoul.go.kr 조회 오류 (PNU={pnu}): {e}")
        result["zones"] = []
        return result


def _get_zone_details_full(
    sess: requests.Session, wtnnc_sn: str, timeout: int
) -> tuple[list[dict], dict | None, list[dict]]:
    """
    getList.json API로 WTNNC_SN에 해당하는 구역 상세 + 고시 정보 반환.

    Returns:
        (zone_items, ntfc_data)
        ntfc_data = {
            "notice_no": ..., "notification": {...},
            "gazette_history": [...], "drawing_documents": [...]
        } or None
    """
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
            return [], None, []
        data = resp.json()

        # 구역 정보 추출 (기존 로직)
        zone_items = []
        for key in ["usgarWtnnc", "ubplfcWtnnc", "spcfWtnnc", "etczoneWtnnc",
                    "dstplanWtnnc", "fcmtrWtnnc"]:
            for item in data.get(key, []):
                if item and isinstance(item, dict):
                    zn = item.get("zoneName") or ""
                    loc = item.get("locationName") or ""
                    if zn or loc:
                        zone_items.append(item)

        # 고시 정보 추출 — 6개 카테고리 전체에서 tnNtfc 수집
        all_notifications = []
        best_ntfc = None  # dstplanWtnnc 최우선

        for cat_key, cat_label in _CATEGORY_LABELS.items():
            for item in data.get(cat_key, []):
                if not isinstance(item, dict):
                    continue
                tn_ntfc = item.get("tnNtfc")
                if not isinstance(tn_ntfc, dict):
                    continue

                notice_no = tn_ntfc.get("noticeNo") or ""
                if not notice_no:
                    continue

                notice_date_raw = tn_ntfc.get("noticeDate") or ""
                notice_date = notice_date_raw[:10] if notice_date_raw else ""
                content = tn_ntfc.get("content") or ""
                title = tn_ntfc.get("title") or ""
                notice_code = tn_ntfc.get("noticeCode") or ""

                notification = {
                    "notice_no": notice_no,
                    "notice_date": notice_date,
                    "title": title,
                    "content": content,
                    "notice_code": notice_code,
                    "site": tn_ntfc.get("site") or "",
                    "charger": tn_ntfc.get("charger") or "",
                    "phone": tn_ntfc.get("phone") or "",
                }

                # 도면 문서
                drawing_docs = []
                for drw in tn_ntfc.get("tnDrwImage", []):
                    if isinstance(drw, dict) and drw.get("dImageName"):
                        d_path = drw.get("dImagePath", "")
                        d_name = drw["dImageName"]
                        dl_url = ""
                        if d_path and d_name:
                            encoded_name = quote(d_name, safe="")
                            dl_url = f"{_BASE_URL}/{d_path}/{encoded_name}"
                        drawing_docs.append({
                            "name": d_name,
                            "code": drw.get("dImageCode", ""),
                            "download_url": dl_url,
                        })

                gazette_history = _parse_gazette_history(
                    content, notice_no, notice_date, notice_code
                )

                zone_name = item.get("zoneName") or ""
                location = item.get("locationName") or ""

                # 개요 정보 추출
                area_raw = item.get("areaAfter")
                area_str = ""
                if area_raw:
                    try:
                        area_val = float(area_raw)
                        area_str = f"{area_val:,.0f}㎡" if area_val >= 1 else ""
                    except (ValueError, TypeError):
                        pass
                first_date_raw = item.get("firstDate") or ""
                first_date = first_date_raw[:10] if first_date_raw else ""
                first_date_info = item.get("firstDateInfo") or ""
                dcsnobj = tn_ntfc.get("dcsnobj") or ""

                ntfc_entry = {
                    "notice_no": notice_no,
                    "category_key": cat_key,
                    "category_label": cat_label,
                    "zone_name": zone_name,
                    "location": location,
                    "area": area_str,
                    "first_date": first_date,
                    "first_date_info": first_date_info,
                    "dcsnobj": dcsnobj,
                    "notification": notification,
                    "gazette_history": gazette_history,
                    "drawing_documents": drawing_docs,
                }
                all_notifications.append(ntfc_entry)

                # best: dstplanWtnnc 최우선, 그 외 최신 날짜
                if best_ntfc is None:
                    best_ntfc = ntfc_entry
                elif cat_key == "dstplanWtnnc" and best_ntfc["category_key"] != "dstplanWtnnc":
                    best_ntfc = ntfc_entry
                elif cat_key == best_ntfc["category_key"] and notice_date > best_ntfc["notification"]["notice_date"]:
                    best_ntfc = ntfc_entry

        return zone_items, best_ntfc, all_notifications

    except Exception as e:
        logger.debug(f"getList.json 오류 (WTNNC={wtnnc_sn}): {e}")
        return [], None, []


def _parse_gazette_history(
    content: str, current_no: str, current_date: str,
    current_notice_code: str = "",
) -> list[dict]:
    """
    tnNtfc.content 텍스트에서 고시번호와 날짜를 파싱하여 연혁 목록 생성.

    content 예시:
    "서울특별시고시 제1999-32호(1999.02.12.)로 지구단위계획구역 결정,
     서울특별시고시 제2002-235호 (2002.06.24.), ..."
    """
    history = []
    seen = set()

    # 1차: 모든 고시번호 위치 수집 (desc 추출용)
    pattern = r"제(\d{4}-\d+)호\s*\(\s*(\d{4})\s*\.\s*(\d{1,2})\s*\.\s*(\d{1,2})\s*\.?\s*\)"
    matches = list(re.finditer(pattern, content))

    for idx, m in enumerate(matches):
        no = m.group(1)
        date = f"{m.group(2)}-{int(m.group(3)):02d}-{int(m.group(4)):02d}"
        if no in seen:
            continue
        seen.add(no)

        # 고시 발행 주체 파싱 (서울특별시고시, XX구고시 등)
        before_full = content[max(0, m.start() - 30):m.start()]
        source_prefix = ""
        sp_match = re.search(r"(서울특별시|[가-힣]{2,4}구)\s*고시", before_full)
        if sp_match:
            source_prefix = sp_match.group(1) + "고시"

        # 서술형 텍스트 추출: 고시번호 뒤 ~ 다음 고시번호 전
        after_start = m.end()
        if idx + 1 < len(matches):
            next_before = content[max(0, matches[idx + 1].start() - 30):matches[idx + 1].start()]
            np_match = re.search(r"(서울특별시|[가-힣]{2,4}구)\s*고시", next_before)
            if np_match:
                after_end = matches[idx + 1].start() - 30 + np_match.start()
            else:
                after_end = matches[idx + 1].start()
        else:
            after_end = min(after_start + 300, len(content))
        raw_desc = content[after_start:after_end].strip()
        raw_desc = re.sub(r"^[로으,.\s]+", "", raw_desc)
        raw_desc = re.sub(r"[,.\s]+$", "", raw_desc)

        # desc(짧은 라벨) + desc_detail(서술형) 분리
        desc = ""
        desc_detail = ""
        if raw_desc:
            # 짧은 라벨 추출: "결정", "결정(변경)", "변경" 등
            label_match = re.match(r"^((?:지구단위계획구역\s*)?(?:결정\s*\(?\s*변경\s*\)?|결정|변경|지정|해제|폐지))", raw_desc)
            if label_match:
                desc = label_match.group(1).strip()
                rest = raw_desc[label_match.end():].strip()
                rest = re.sub(r"^[,.\s]+", "", rest)
                if rest:
                    desc_detail = rest[:100] + ("…" if len(rest) > 100 else "")
            else:
                desc_detail = raw_desc[:100] + ("…" if len(raw_desc) > 100 else "")

        if not desc:
            after_short = content[m.end():m.end() + 30]
            for kw in ["결정(변경)", "결정", "변경", "지정", "해제", "폐지"]:
                if kw in after_short:
                    desc = kw
                    break
            if not desc:
                desc = "결정(변경)"

        nc = current_notice_code if no == current_no else ""
        history.append({
            "no": no, "date": date, "desc": desc,
            "desc_detail": desc_detail,
            "notice_code": nc, "source_prefix": source_prefix,
        })

    # 날짜 없는 고시번호 2차 수집
    pattern2 = r"제(\d{4}-\d+)호"
    for m2 in re.finditer(pattern2, content):
        no = m2.group(1)
        if no in seen:
            continue
        seen.add(no)
        before_full = content[max(0, m2.start() - 30):m2.start()]
        source_prefix = ""
        sp_match = re.search(r"(서울특별시|[가-힣]{2,4}구)\s*고시", before_full)
        if sp_match:
            source_prefix = sp_match.group(1) + "고시"
        nc = current_notice_code if no == current_no else ""
        history.append({
            "no": no, "date": "", "desc": "", "desc_detail": "",
            "notice_code": nc, "source_prefix": source_prefix,
        })

    # 현재 고시가 content에 없으면 맨 앞에 추가
    if current_no and current_no not in seen:
        history.insert(0, {
            "no": current_no,
            "date": current_date,
            "desc": "결정(변경)",
            "desc_detail": "",
            "notice_code": current_notice_code,
            "source_prefix": "서울특별시고시",
        })

    # 날짜 역순 정렬 (최신순)
    history.sort(key=lambda h: h.get("date", ""), reverse=True)
    return history


# UPIS API에 미등록된 고시 보충 데이터 (구역명 → 고시 목록)
# archive_url: UPIS 아카이브 직접 PDF 링크 (notice_code 대신 사용)
_SUPPLEMENTARY_HISTORY: dict[str, list[dict]] = {
    "왕십리 광역중심": [
        {
            "no": "2021-92",
            "date": "2021-03-18",
            "desc": "결정(변경)",
            "desc_detail": "세종로 지구단위계획구역 외 301개 \"상한용적률 인센티브 산정기준\" 변경, 62개 \"생활숙박시설 관리기준\" 변경",
            "source_prefix": "서울특별시고시",
            "archive_url": "https://urban.seoul.go.kr/UpisArchive/DATA/PM/DS/11200UQ161PS202403270001/ST/%EC%84%9C%EC%9A%B8%ED%8A%B9%EB%B3%84%EC%8B%9C_%EC%A0%9C2021-92%ED%98%B8_%EA%B3%A0%EC%8B%9C.pdf",
        },
        {
            "no": "2019-312",
            "date": "2019-09-26",
            "desc": "결정(변경)",
            "desc_detail": "「서울특별시 도시계획조례」 개정에 따른 \"건축물의 용적률계획\" 변경(상업지역의 주거용적률 등 완화하는 사항 변경)",
            "source_prefix": "서울특별시고시",
            "archive_url": "https://urban.seoul.go.kr/UpisArchive/DATA/PM/DS/11200UQ161PS202403270001/ST/%EC%84%9C%EC%9A%B8%ED%8A%B9%EB%B3%84%EC%8B%9C_%EC%A0%9C2019-312%ED%98%B8_%EA%B3%A0%EC%8B%9C.pdf",
        },
        {
            "no": "2019-313",
            "date": "2019-09-26",
            "desc": "결정(변경)",
            "desc_detail": "「서울특별시 도시계획조례」 개정에 따른 \"건축물의 용적률계획\" 변경(준주거지역 내 공공임대주택 확보시 용적률 적용 기준 완화)",
            "source_prefix": "서울특별시고시",
            "archive_url": "https://urban.seoul.go.kr/UpisArchive/DATA/PM/DS/11200UQ161PS202403270001/ST/%EC%84%9C%EC%9A%B8%ED%8A%B9%EB%B3%84%EC%8B%9C_%EC%A0%9C2019-313%ED%98%B8_%EA%B3%A0%EC%8B%9C.pdf",
        },
    ],
}


def _summarize_ntfc_content(text: str) -> str:
    """고시 content에서 '~에 대하여' 앞까지 핵심 내용 추출.

    예: '강남세브란스병원 지구단위계획구역 등 264개 지구단위계획구역
         "임대의무기간 임대주택 건립 용적률 완화 기준" 결정 변경에 대하여
         2025년 서울특별시 도시·건축공동위원회 제3차 수권...'
    → '강남세브란스병원 지구단위계획구역 등 264개 지구단위계획구역 "임대의무기간 임대주택 건립 용적률 완화 기준" 결정 변경'
    """
    if not text or len(text) < 5:
        return ""

    # "에 대하여/대해" 앞까지가 핵심 내용
    m = re.search(r"에\s*대하여|에\s*대해", text)
    if m:
        core = text[:m.start()].strip()
        if len(core) > 10:
            return core

    # "을/를 거쳐" 앞까지
    m = re.search(r"[을를]\s*거쳐", text)
    if m:
        core = text[:m.start()].strip()
        if len(core) > 10:
            return core

    # 폴백: 첫 문장 또는 120자
    return text[:120] + ("…" if len(text) > 120 else "")


def _enrich_history_from_ntfc_api(
    sess: requests.Session,
    zone_name: str,
    existing_history: list[dict],
    ntfc_content: str = "",
    timeout: int = 15,
) -> list[dict]:
    """
    UPIS getNtfcList.json으로 구역명 키워드 검색 → 기존 연혁에 없는 고시 추가.

    3단계 보강:
    1) 구역명 고유명사로 제목 검색 (기존)
    2) 과거 구역명 변형으로 추가 검색 (ntfc_content에서 추출)
    3) 일괄 변경 건 검색 ("등 N개 지구단위계획구역" 패턴)
    """
    if not zone_name:
        return existing_history

    existing_nos = {h.get("no", "") for h in existing_history}

    # 구역명에서 검색 키워드 추출
    # "왕십리 광역중심 지구단위계획구역" → 고유명사 "왕십리"
    kw_full = zone_name
    for suffix in ["지구단위계획구역", "지구단위계획", "구역"]:
        kw_full = kw_full.replace(suffix, "").strip()
    kw_parts = kw_full.split()
    kw = kw_parts[0] if kw_parts else kw_full
    if not kw or len(kw) < 2:
        return existing_history

    added = []

    def _search_ntfc_list(keywords: list[str], page_size: int = 50) -> list[dict]:
        """getNtfcList.json 검색 헬퍼."""
        try:
            resp = sess.post(
                _NTFC_LIST_API,
                data=json.dumps({
                    "pageNo": 1, "pageSize": page_size,
                    "keywordList": keywords,
                    "pubSiteCode": "", "organCode": "",
                    "bgnDate": "", "endDate": "",
                    "srchType": "title", "noticeCode": "",
                }),
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
            if resp.status_code != 200:
                return []
            data = json.loads(resp.content.decode("utf-8-sig"))
            return data.get("content", [])
        except Exception:
            return []

    def _make_entry(item: dict) -> dict:
        """getNtfcList 항목 → gazette_history 엔트리 변환."""
        notice_no = item.get("noticeNo") or ""
        title = item.get("title") or ""
        content = item.get("content") or ""
        notice_date_raw = item.get("noticeDate") or ""
        notice_date = notice_date_raw[:10] if notice_date_raw else ""
        notice_code = item.get("noticeCode") or ""
        drw_images = item.get("tnDrwImage") or []

        desc = "결정(변경)"
        if "실효" in title:
            desc = "실효"
        elif "결정(변경)" in title:
            desc = "결정(변경)"
        elif "변경" in title:
            desc = "변경"
        elif "결정" in title:
            desc = "결정"

        # desc_detail: title이 구체적이면 title, 형식적이면 content 요약
        # 형식적 제목: "도시관리계획(...) 결정(변경) 및 지형도면 고시" 등
        _is_generic = (
            not title
            or len(title) <= 10
            or ("도시관리계획" in title and "고시" in title)
            or ("지형도면" in title and "고시" in title)
            or re.match(r"^(결정|변경|결정\(변경\)|고시|공고)$", title.strip())
        )
        if _is_generic and content and len(content) > 10:
            desc_detail = _summarize_ntfc_content(content)
        elif title and len(title) > 10:
            desc_detail = title
        else:
            desc_detail = ""

        source_prefix = "서울특별시고시"
        if "구" in str(item.get("deptCode", "")):
            dept = item.get("dept", {})
            instt = dept.get("insttFullName", "") if isinstance(dept, dict) else ""
            if "구" in instt:
                gu_match = re.search(r"([가-힣]+구)", instt)
                if gu_match:
                    source_prefix = gu_match.group(1) + "고시"

        return {
            "no": notice_no,
            "date": notice_date,
            "desc": desc,
            "desc_detail": desc_detail,
            "title": title,
            "notice_code": notice_code,
            "source_prefix": source_prefix,
            "drawing_documents": [
                {
                    "name": d.get("dImageName", ""),
                    "code": d.get("dImageCode", ""),
                    "download_url": (
                        f"{_BASE_URL}/{d.get('dImagePath', '')}/{quote(d['dImageName'], safe='')}"
                        if d.get("dImagePath") and d.get("dImageName") else ""
                    ),
                }
                for d in drw_images
                if isinstance(d, dict) and d.get("dImageName")
            ],
        }

    _ZONE_KW = ("지구단위", "특별계획구역", "세부개발계획")
    _BULK_PATTERN = re.compile(r"등\s*\d+\s*개\s*(지구단위|구역)")

    # 기존 항목 고시번호 → 인덱스 매핑 (도면/상세 보강용)
    _existing_idx = {h.get("no", ""): i for i, h in enumerate(existing_history)}

    def _backfill_from_api(item: dict):
        """기존 항목의 desc_detail/title/도면이 비어있으면 API 데이터로 보강."""
        notice_no = item.get("noticeNo") or ""
        if notice_no not in _existing_idx:
            return
        idx = _existing_idx[notice_no]
        entry = _make_entry(item)
        h = existing_history[idx]
        # desc_detail/title 보강
        if not h.get("desc_detail") and not h.get("title"):
            if entry.get("desc_detail"):
                h["desc_detail"] = entry["desc_detail"]
            if entry.get("title"):
                h["title"] = entry["title"]
        # notice_code 보강
        if not h.get("notice_code") and entry.get("notice_code"):
            h["notice_code"] = entry["notice_code"]
        # 도면 보강
        if not h.get("drawing_documents") and entry.get("drawing_documents"):
            h["drawing_documents"] = entry["drawing_documents"]

    try:
        # ── Phase 1: 구역명 고유명사로 검색 ──
        items = _search_ntfc_list([kw])
        for item in items:
            notice_no = item.get("noticeNo") or ""
            _backfill_from_api(item)
            if not notice_no or notice_no in existing_nos:
                continue
            title = item.get("title") or ""
            title_nospace = title.replace(" ", "")
            kw_nospace = kw_full.replace(" ", "")
            match = kw_nospace in title_nospace
            if not match:
                if kw in title and any(zk in title for zk in _ZONE_KW) and "뉴타운" not in title:
                    match = True
            if match:
                existing_nos.add(notice_no)
                added.append(_make_entry(item))

        # ── Phase 2: 과거 구역명으로 추가 검색 ──
        # ntfc_content에서 과거 구역명 변형 추출
        # 예: "왕십리부도심권" → "부도심" 키워드는 이미 Phase 1에서 처리됨
        # 여기서는 content에 언급된 다른 구역명을 추출
        if ntfc_content:
            # "국제빌딩주변" 같은 별도 구역명 추출
            alt_names = set()
            # 패턴: "OOO 지구단위계획" 또는 "OOO지구단위계획"
            for m in re.finditer(
                r"([\uac00-\ud7a30-9A-Za-z]+(?:\s[\uac00-\ud7a30-9A-Za-z]+)*)"
                r"\s*(?:제?\d*종?\s*)?지구단위계획",
                ntfc_content,
            ):
                name = m.group(1).strip()
                # 짧은 접속사나 조사 제거
                name = re.sub(r"^(및|의|에|로|을|를|이|가)\s*", "", name)
                if len(name) >= 2 and name != kw and name != kw_full:
                    alt_names.add(name)

            for alt_kw in alt_names:
                alt_items = _search_ntfc_list([alt_kw])
                for item in alt_items:
                    notice_no = item.get("noticeNo") or ""
                    _backfill_from_api(item)
                    if not notice_no or notice_no in existing_nos:
                        continue
                    title = item.get("title") or ""
                    if alt_kw in title and any(zk in title for zk in _ZONE_KW) and "뉴타운" not in title:
                        existing_nos.add(notice_no)
                        added.append(_make_entry(item))

        # ── Phase 3: 일괄 변경 건 (전체 지구단위계획구역 대상) ──
        # 2개 키워드로 검색하여 누락 방지
        bulk_items = []
        for bulk_kw in ["지구단위계획구역", "도시관리계획(지구단위"]:
            bulk_items.extend(_search_ntfc_list([bulk_kw], page_size=100))
        seen_bulk = set()
        for item in bulk_items:
            notice_no = item.get("noticeNo") or ""
            _backfill_from_api(item)
            if not notice_no or notice_no in existing_nos or notice_no in seen_bulk:
                continue
            seen_bulk.add(notice_no)
            title = item.get("title") or ""
            content = item.get("content") or ""
            # 제목 또는 content에 "등 N개 지구단위" 패턴이 있으면 일괄 변경
            if _BULK_PATTERN.search(title) or _BULK_PATTERN.search(content):
                existing_nos.add(notice_no)
                entry = _make_entry(item)
                entry["bulk_change"] = True  # 일괄 변경 표시
                added.append(entry)

        # ── Phase 4: 보충 데이터 (UPIS 미등록 고시) ──
        for supp_key, supp_items in _SUPPLEMENTARY_HISTORY.items():
            if supp_key in zone_name:
                for entry in supp_items:
                    if entry["no"] not in existing_nos:
                        existing_nos.add(entry["no"])
                        added.append(dict(entry))  # 원본 변경 방지

        if added:
            logger.info(f"UPIS 연혁 보강: {zone_name} — {len(added)}건 추가 (기존 {len(existing_history)}건)")
            merged = existing_history + added
            merged.sort(key=lambda h: h.get("date", ""), reverse=True)
            return merged

    except Exception as e:
        logger.debug(f"UPIS getNtfcList 연혁 보강 실패 ({zone_name}): {e}")

    return existing_history


def _enrich_files_from_propel(
    sess: requests.Session,
    wtnnc_sn: str,
    gazette_history: list[dict],
    timeout: int = 15,
):
    """
    사업정보 API(getPropelList)로 각 고시별 전체 첨부파일 목록 보강.

    기존 tnDrwImage(참고자료 6건)가 아닌 결정도(22)+결정조서(1)+시행지침(1)+고시문(1) 등
    전체 파일을 가져와 gazette_history 각 항목의 drawing_documents에 매핑.
    """
    try:
        # Step 1: wtnnc_sn → presentSn
        resp1 = sess.post(
            _CUQ161_API,
            data=wtnnc_sn,
            headers={"Content-Type": "application/json; charset=UTF-8"},
            timeout=timeout,
        )
        if not resp1.content:
            return
        present_sn = resp1.json().get("presentSn")
        if not present_sn:
            return

        # Step 2: presentSn → 고시별 파일 목록
        resp2 = sess.post(
            _PROPEL_LIST_API,
            data={"presentSn": present_sn},
            timeout=timeout,
        )
        if not resp2.content:
            return
        propel_items = resp2.json()
        if not isinstance(propel_items, list):
            return

        # noticeNo → fileList 매핑
        no_to_files: dict[str, list[dict]] = {}
        for item in propel_items:
            tn = item.get("tnNtfc")
            if not isinstance(tn, dict):
                continue
            notice_no = tn.get("noticeNo") or ""
            if not notice_no:
                continue
            file_list = item.get("fileList")
            if not isinstance(file_list, list) or not file_list:
                continue
            no_to_files[notice_no] = file_list

        if not no_to_files:
            return

        # gazette_history 각 항목에 파일 매핑
        enriched = 0
        for h in gazette_history:
            h_no = h.get("no", "")
            if h_no not in no_to_files:
                continue
            files = no_to_files[h_no]
            docs = []
            for f in files:
                file_name = f.get("fileName") or ""
                file_url = f.get("fileUrl") or ""
                group_code = f.get("groupCode") or ""
                if not file_name:
                    continue
                dl_url = ""
                if file_url and file_name:
                    dl_url = f"{_BASE_URL}/{file_url}/{quote(file_name, safe='')}"
                group_label = _FILE_GROUP_LABELS.get(group_code, group_code)
                docs.append({
                    "name": file_name,
                    "code": group_code,
                    "group_label": group_label,
                    "download_url": dl_url,
                })
            if docs:
                h["drawing_documents"] = docs
                enriched += 1

        if enriched:
            logger.info(
                f"사업정보 파일 보강: {enriched}건 고시에 첨부파일 매핑 "
                f"(presentSn={present_sn})"
            )

    except Exception as e:
        logger.debug(f"사업정보 파일 보강 실패 (WTNNC={wtnnc_sn}): {e}")


def _layer_to_id(layer_name: str) -> int:
    """레이어명 → layer ID 역매핑."""
    for lid, (lname, _) in _QUERY_LAYERS.items():
        if lname == layer_name:
            return lid
    return -1
