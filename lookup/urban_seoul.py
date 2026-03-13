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
                ntfc["gazette_history"] = _enrich_history_from_ntfc_api(
                    sess, ntfc["zone_name"],
                    ntfc.get("gazette_history", []),
                    timeout=min(timeout, 15),
                )

        if best_ntfc:
            result["notification"] = best_ntfc.get("notification")
            result["gazette_history"] = best_ntfc.get("gazette_history", [])
            result["drawing_documents"] = best_ntfc.get("drawing_documents", [])
            notice_code = best_ntfc.get("notification", {}).get("notice_code", "")
            if notice_code:
                result["notice_url"] = (
                    f"https://urban.seoul.go.kr/view/html/PMNU4030100001"
                    f"?noticeCode={notice_code}"
                )

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
                    desc_detail = rest[:200]
            else:
                desc_detail = raw_desc[:200]

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


def _enrich_history_from_ntfc_api(
    sess: requests.Session,
    zone_name: str,
    existing_history: list[dict],
    timeout: int = 15,
) -> list[dict]:
    """
    UPIS getNtfcList.json으로 구역명 키워드 검색 → 기존 연혁에 없는 고시 추가.
    getList.json content 필드에는 원본 결정 체인만 포함되므로,
    일괄 변경(용적률 완화, 조례 개정 등)은 이 API로 보강.
    """
    if not zone_name:
        return existing_history

    existing_nos = {h.get("no", "") for h in existing_history}

    # 구역명에서 검색 키워드 추출
    # "왕십리 광역중심 지구단위계획구역" → 고유명사 "왕십리"
    kw_full = zone_name
    for suffix in ["지구단위계획구역", "지구단위계획", "구역"]:
        kw_full = kw_full.replace(suffix, "").strip()
    # 첫 번째 단어 = 지역 고유명사 (더 넓은 검색)
    kw_parts = kw_full.split()
    kw = kw_parts[0] if kw_parts else kw_full
    if not kw or len(kw) < 2:
        return existing_history

    try:
        search_payload = json.dumps({
            "pageNo": 1,
            "pageSize": 50,
            "keywordList": [kw],
            "pubSiteCode": "",
            "organCode": "",
            "bgnDate": "",
            "endDate": "",
            "srchType": "title",
            "noticeCode": "",
        })

        resp = sess.post(
            _NTFC_LIST_API,
            data=search_payload,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return existing_history

        data = json.loads(resp.content.decode("utf-8-sig"))
        items = data.get("content", [])
        if not items:
            return existing_history

        added = []
        for item in items:
            notice_no = item.get("noticeNo", "")
            if not notice_no or notice_no in existing_nos:
                continue

            title = item.get("title", "")
            # 구역명 매칭:
            # 1) 구역명 전체 "왕십리 광역중심" (공백 무시)
            # 2) 구역명 고유명사 + "지구단위" (구역명 변경 전 이름 대응)
            #    예: "왕십리부도심권지구단위계획" → kw="왕십리" + "지구단위"
            title_nospace = title.replace(" ", "")
            kw_nospace = kw_full.replace(" ", "")
            title_has_zone = kw_nospace in title_nospace
            if not title_has_zone and kw in title and "지구단위" in title:
                title_has_zone = True
            if not title_has_zone:
                continue

            notice_date_raw = item.get("noticeDate", "")
            notice_date = notice_date_raw[:10] if notice_date_raw else ""
            notice_code = item.get("noticeCode", "")
            drw_images = item.get("tnDrwImage") or []

            # desc: 제목에서 핵심 내용 추출
            desc = "결정(변경)"
            title_clean = re.sub(r"도시관리계획\s*\[", "", title)
            title_clean = re.sub(r"\]\s*결정.*$", "", title_clean)
            if "결정(변경)" in title:
                desc = "결정(변경)"
            elif "변경" in title:
                desc = "변경"
            elif "결정" in title:
                desc = "결정"

            # desc_detail: 제목 전체를 상세 설명으로
            desc_detail = title[:200] if len(title) > len(desc) + 5 else ""

            # source_prefix 추출
            source_prefix = "서울특별시고시"
            pub_site = item.get("pubSiteCode", "")
            if pub_site and "구" in str(item.get("deptCode", "")):
                # 구청 고시인 경우
                dept = item.get("dept", {})
                instt = dept.get("insttFullName", "") if isinstance(dept, dict) else ""
                if "구" in instt:
                    gu_match = re.search(r"([가-힣]+구)", instt)
                    if gu_match:
                        source_prefix = gu_match.group(1) + "고시"

            existing_nos.add(notice_no)
            added.append({
                "no": notice_no,
                "date": notice_date,
                "desc": desc,
                "desc_detail": desc_detail,
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
            })

        if added:
            logger.info(f"UPIS 연혁 보강: {zone_name} — {len(added)}건 추가 (기존 {len(existing_history)}건)")
            merged = existing_history + added
            merged.sort(key=lambda h: h.get("date", ""), reverse=True)
            return merged

    except Exception as e:
        logger.debug(f"UPIS getNtfcList 연혁 보강 실패 ({zone_name}): {e}")

    return existing_history


def _layer_to_id(layer_name: str) -> int:
    """레이어명 → layer ID 역매핑."""
    for lid, (lname, _) in _QUERY_LAYERS.items():
        if lname == layer_name:
            return lid
    return -1
