"""
지번주소 → PNU(필지고유번호 19자리) 변환
행정안전부 주소정보누리집 API (juso.go.kr) 사용
좌표는 VWORLD 지오코딩 API로 보완
"""
import logging
import requests

logger = logging.getLogger(__name__)

_JUSO_URL = "https://www.juso.go.kr/addrlink/addrLinkApi.do"
_VWORLD_GEOCODE_URL = "https://api.vworld.kr/req/address"


def _vworld_geocode(address_full: str, vworld_key: str, timeout: int, domain: str = "localhost") -> tuple[str, str]:
    """VWORLD 지오코딩 API로 지번주소 → 경도/위도 변환. 실패 시 ('', '') 반환."""
    try:
        resp = requests.get(_VWORLD_GEOCODE_URL, params={
            "service": "address", "request": "getcoord", "version": "2.0",
            "crs": "epsg:4326", "address": address_full,
            "refine": "true", "simple": "false", "format": "json",
            "type": "PARCEL", "key": vworld_key, "domain": domain,
        }, timeout=timeout)
        resp.raise_for_status()
        d = resp.json()
        if d.get("response", {}).get("status") == "OK":
            pt = d["response"]["result"]["point"]
            return pt.get("x", ""), pt.get("y", "")
    except Exception as e:
        logger.warning(f"VWORLD 지오코딩 실패: {e}")
    return "", ""


def address_to_pnu(address: str, api_key: str, timeout: int = 10,
                   vworld_api_key: str = "", vworld_domain: str = "localhost") -> dict:
    """
    지번주소 문자열 → PNU 및 좌표 반환

    Args:
        address: "강남구 삼성동 100-1" 형태 (서울 생략 가능)
        api_key: juso.go.kr 인증키

    Returns:
        {
            "pnu": "1168010500101000001",  # 19자리
            "address_full": "서울특별시 강남구 삼성동 100-1",
            "admCd": "1168010500",         # 행정코드 10자리
            "rnMgtSn": ...,
            "entX": "127.056917",          # 경도
            "entY": "37.514575",           # 위도
        }

    Raises:
        ValueError: 주소를 찾을 수 없는 경우
        requests.RequestException: API 호출 실패
    """
    # 서울 접두어가 없으면 추가 (검색 정확도 향상)
    keyword = address.strip()
    if not any(keyword.startswith(p) for p in ["서울", "서울시", "서울특별시"]):
        keyword = "서울특별시 " + keyword

    params = {
        "confmKey": api_key,
        "currentPage": 1,
        "countPerPage": 5,
        "keyword": keyword,
        "resultType": "json",
        "hstryYn": "N",
        "firstSort": "none",
        "addInfoYn": "Y",  # 좌표 포함
    }

    resp = requests.get(_JUSO_URL, params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    results = data.get("results", {})
    error_code = results.get("common", {}).get("errorCode", "0")
    if error_code != "0":
        err_msg = results.get("common", {}).get("errorMessage", "알 수 없는 오류")
        raise ValueError(f"주소 API 오류 [{error_code}]: {err_msg}")

    juso_list = results.get("juso", [])
    if not juso_list:
        raise ValueError(f"주소를 찾을 수 없습니다: '{address}'")

    # 첫 번째 결과 사용
    item = juso_list[0]

    # PNU 19자리 구성: admCd(10) + mtYn(1) + lnbrMnnm(4) + lnbrSlno(4)
    adm_cd = item.get("admCd", "").zfill(10)          # 행정코드 10자리
    mt_yn = item.get("mtYn", "0").zfill(1)             # 산여부 1자리 (0=일반, 1=산)
    lnbr_mnnm = item.get("lnbrMnnm", "0").zfill(4)    # 본번 4자리
    lnbr_slno = item.get("lnbrSlno", "0").zfill(4)    # 부번 4자리
    pnu = adm_cd + mt_yn + lnbr_mnnm + lnbr_slno

    logger.info(f"주소 변환: '{address}' → PNU={pnu}")

    info = {
        "pnu": pnu,
        "address_full": item.get("jibunAddr", keyword),
        "address_road": item.get("roadAddr", ""),
        "admCd": adm_cd,
        "siNm": item.get("siNm", ""),
        "sggNm": item.get("sggNm", ""),
        "emdNm": item.get("emdNm", ""),
        "lnbrMnnm": lnbr_mnnm,
        "lnbrSlno": lnbr_slno,
        "mtYn": mt_yn,
        "entX": item.get("entX", "") or "",
        "entY": item.get("entY", "") or "",
    }

    # juso.go.kr가 좌표를 주지 않으면 VWORLD 지오코딩으로 보완
    if not info["entX"] and vworld_api_key:
        logger.info(f"juso.go.kr 좌표 없음 → VWORLD 지오코딩 시도 (domain={vworld_domain})")
        ex, ey = _vworld_geocode(info["address_full"], vworld_api_key, timeout, domain=vworld_domain)
        info["entX"] = ex
        info["entY"] = ey
        if ex:
            logger.info(f"VWORLD 지오코딩 좌표: ({ex}, {ey})")
        else:
            logger.warning(f"VWORLD 지오코딩 좌표 획득 실패 → 용도지역/지목/면적 조회 불가")

    return info


def parse_address_input(raw: str) -> str:
    """사용자 입력 주소 정규화 (간단한 전처리)"""
    raw = raw.strip()
    # 괄호 및 특수문자 제거
    for ch in ["(", ")", "[", "]", "번지"]:
        raw = raw.replace(ch, "")
    return raw.strip()
