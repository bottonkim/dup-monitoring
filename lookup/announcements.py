"""
구역명 → 최신 고시공고 조회
1. 로컬 DB 캐시 우선
2. DB에 없으면 서울 열린데이터광장 API 직접 조회
3. 결정고시 CN 내용 Claude 분석 (건축 제한 추출)
"""
import logging
import time
import hashlib
import requests
from pathlib import Path

logger = logging.getLogger(__name__)

_SEOUL_API_BASE = "http://openapi.seoul.go.kr:8088"

_DISTRICTS = [
    "강남구", "강동구", "강북구", "강서구", "관악구", "광진구", "구로구",
    "금천구", "노원구", "도봉구", "동대문구", "동작구", "마포구", "서대문구",
    "서초구", "성동구", "성북구", "송파구", "양천구", "영등포구", "용산구",
    "은평구", "종로구", "중구", "중랑구",
]

# 결정조서 상세 콘텐츠 판별 키워드
_DETAIL_KEYWORDS = ("건폐율", "용적률", "허용용도", "불허용도", "높이제한", "결정조서",
                    "허용 용도", "불허 용도", "상한용적률")


def _classify_content_quality(text: str) -> str:
    """텍스트의 결정조서 상세 포함 여부 판별"""
    if not text or len(text) < 10:
        return "minimal"
    hits = sum(1 for kw in _DETAIL_KEYWORDS if kw in text)
    return "detailed" if hits >= 2 else "summary"


def get_announcements_for_zones(
    zone_names: list[str],
    conn,
    seoul_api_key: str,
    lookback_days: int = 30,
    limit: int = 10,
) -> list[dict]:
    """
    구역명 목록으로 관련 고시공고 조회.
    """
    from db.database import search_announcements_by_zone

    # 1. 로컬 DB 먼저 조회
    db_rows = search_announcements_by_zone(conn, zone_names, limit=limit)
    results = [dict(row) for row in db_rows]

    if results:
        logger.info(f"로컬 DB에서 {len(results)}건 조회 (구역: {zone_names[:2]})")
        return results

    # 2. DB에 없으면 서울 열린데이터광장 API 직접 호출
    if seoul_api_key:
        api_results = _search_seoul_api(zone_names, seoul_api_key, limit)
        if api_results:
            logger.info(f"서울 Open API에서 {len(api_results)}건 조회")
            return api_results

    return []


# ---------------------------------------------------------------------------
# 서울 Open API 직접 검색
# ---------------------------------------------------------------------------

def _search_seoul_api(zone_names: list[str], api_key: str, limit: int = 10) -> list[dict]:
    """서울 열린데이터광장 upisAnnouncement API에서 구역명 검색"""
    all_results = []
    seen_ids = set()

    # upisAnnouncement 최근 3000건 스캔 (30페이지)
    max_pages = 30
    for page in range(max_pages):
        start = page * 100 + 1
        end = start + 99
        url = f"{_SEOUL_API_BASE}/{api_key}/json/upisAnnouncement/{start}/{end}"
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("upisAnnouncement", {}).get("row", []) or []
            if not items:
                break

            for item in items:
                title = (item.get("TTL") or "").strip()
                cn = (item.get("CN") or "").strip()
                if not title:
                    continue

                # 구역명 매칭 (제목 + 내용)
                if not _is_zone_match(title, cn, zone_names):
                    continue

                ann_id = item.get("ANCMNT_MNG_CD") or title[:20]
                if ann_id in seen_ids:
                    continue
                seen_ids.add(ann_id)

                pub_date = item.get("ANCMNT_YMD") or ""
                all_results.append({
                    "source": "seoul_api_direct",
                    "source_id": str(ann_id),
                    "title": title,
                    "category": _detect_category(title),
                    "district": _extract_district(title + " " + cn),
                    "zone_name": _extract_zone_name(title + " " + cn, zone_names),
                    "published_at": _normalize_date(pub_date),
                    "url": "",
                    "structured_json": None,
                    "cn_content": cn,
                    "raw_content": cn[:10000],
                    "content_quality": _classify_content_quality(cn),
                    "gazette_no": item.get("ANCMNT_NO") or "",
                })

            if len(all_results) >= limit:
                break
            time.sleep(0.2)

        except Exception as e:
            logger.warning(f"upisAnnouncement 페이지{page+1} 조회 실패: {e}")
            break

    return all_results[:limit]


def _is_zone_match(title: str, cn: str, zone_names: list[str]) -> bool:
    """제목/내용에 구역명(또는 핵심 키워드)이 포함되는지 확인"""
    text = title + " " + cn
    for zone in zone_names:
        if not zone or len(zone) < 2:
            continue
        if zone in text:
            return True
        # 부분 매칭: "왕십리 광역중심 지구단위계획구역" → "왕십리" 포함 여부
        for part in zone.split():
            if len(part) >= 2 and part not in ("지구단위계획구역", "지구단위계획", "구역", "정비구역") and part in text:
                return True
    return False


# ---------------------------------------------------------------------------
# 고시공고 일괄 임포트 (upisAnnouncement 전체)
# ---------------------------------------------------------------------------

def import_all_upis_announcements(api_key: str, db_path: Path, max_pages: int = 450):
    """
    upisAnnouncement 전체 항목을 DB에 일괄 임포트.
    첫 실행 시 ~2분 소요 (43k 건), 이후에는 신규분만 추가.
    """
    from db.database import get_connection, upsert_announcement

    conn = get_connection(db_path)

    # 이미 충분한 데이터가 있으면 스킵
    count = conn.execute("SELECT COUNT(*) FROM announcements WHERE source='upis_api'").fetchone()[0]
    if count > 30000:
        logger.info(f"upisAnnouncement 이미 {count}건 임포트됨 — 스킵")
        conn.close()
        return count

    logger.info(f"upisAnnouncement 일괄 임포트 시작 (기존 {count}건)")
    imported = 0
    new_count = 0

    for page in range(max_pages):
        start = page * 100 + 1
        end = start + 99
        url = f"{_SEOUL_API_BASE}/{api_key}/json/upisAnnouncement/{start}/{end}"

        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            svc = data.get("upisAnnouncement", {})

            err_code = svc.get("RESULT", {}).get("CODE", "")
            if err_code not in ("INFO-000", ""):
                logger.warning(f"upisAnnouncement API 오류: {svc.get('RESULT')}")
                break

            items = svc.get("row", [])
            if not items:
                break

            total = int(svc.get("list_total_count", 0))

            for item in items:
                title = (item.get("TTL") or "").strip()
                if not title:
                    continue

                cn = (item.get("CN") or "").strip()
                ann_code = item.get("ANCMNT_MNG_CD") or ""
                pub_date = item.get("ANCMNT_YMD") or ""
                gazette_no = item.get("ANCMNT_NO") or ""

                c_hash = hashlib.sha256((title + cn).encode()).hexdigest()[:32]
                ann_id, is_new = upsert_announcement(conn, {
                    "source": "upis_api",
                    "source_id": f"upis_{ann_code}" if ann_code else f"upis_{c_hash}",
                    "title": title,
                    "category": _detect_category(title),
                    "district": _extract_district(title + " " + cn),
                    "zone_name": "",
                    "published_at": _normalize_date(pub_date),
                    "url": "",
                    "content_hash": c_hash,
                    "raw_content": cn[:10000],
                    "content_quality": _classify_content_quality(cn),
                })
                imported += 1
                if is_new:
                    new_count += 1

            if page % 50 == 0 and page > 0:
                logger.info(f"  ... {imported}건 처리 / {new_count}건 신규 (페이지 {page+1}/{max_pages})")

            if end >= total:
                break

            time.sleep(0.1)

        except Exception as e:
            logger.warning(f"upisAnnouncement 임포트 페이지{page+1} 실패: {e}")
            time.sleep(1)

    conn.close()
    logger.info(f"upisAnnouncement 임포트 완료: 총 {imported}건 처리, {new_count}건 신규")
    return imported


# ---------------------------------------------------------------------------
# Claude 분석: 결정고시 CN 내용 → 건축 제한 구조화
# ---------------------------------------------------------------------------

def _build_prompt_parts(title: str, cn_content: str) -> tuple:
    """시스템 프롬프트와 유저 프롬프트 반환 (API 호출 없이 프롬프트만 생성)"""
    system = """당신은 서울시 도시계획 문서를 전문으로 분석하는 한국어 도시계획 문서 분석가입니다.
지구단위계획, 정비구역, 특별계획구역 등 서울시 공식 고시·공문서에서 핵심 정보를 정확하게 추출합니다.

규칙:
1. 반드시 JSON 형식으로만 응답 (마크다운 코드블록 없이 순수 JSON)
2. **현재 적용 중인 규제 내용을 추출하는 것이 목표** — "변경없음"이라고만 쓰지 말고, 문서에 기재된 현행 수치·기준을 그대로 추출
3. 변경고시/열람공고인 경우: 변경된 항목은 변경 후 값을, 변경 없는 항목은 문서에 기재된 현행 값을 추출
4. 결정조서 표가 있으면 반드시 해당 표에서 수치를 추출 (건폐율, 용적률, 높이, 용도 등)
5. 숫자와 날짜는 정확하게 추출, 한국어 원문 그대로 사용
6. 용적률은 기준/허용/상한 세 가지를 구분하여 추출
7. 용도별 비율(주거, 상업, 업무 등)이 명시된 경우 반드시 추출
8. 용적률 완화 조건(공개공지, 친환경 인증, 공공기여 등)이 있으면 반드시 추출
9. 문서에 전혀 언급되지 않은 정보만 null로 처리"""

    prompt = f"""다음 서울시 도시계획 고시/공고의 제목과 내용에서 정보를 추출하세요.

제목: {title}

내용:
{cn_content[:10000]}

아래 JSON 스키마에 따라 정보를 **빠짐없이** 추출하세요.
각 항목은 문서에서 찾을 수 있는 한 반드시 채워야 합니다:
{{
  "zone_name": "구역명 (예: 강남구 삼성동 제1종지구단위계획구역)",
  "sub_zone": "분석 대상 필지가 속한 세부 구역명 1개만 (제목의 [분석 대상: 필지: OO동 XX] 참조 → 결정조서 표에서 해당 지번 행을 찾아 그 행의 구역/획지/가구명 추출). 예: 제일은행 특별계획구역, A획지, 제1가구. 전체 목록이 아닌 해당 필지의 구역만. 없으면 null",
  "zone_type": "지구단위계획구역 | 정비구역 | 특별계획구역 | 재정비촉진지구 | 기타",
  "action_type": "결정 | 변경 | 지정 | 해제 | 열람 | 기타",
  "district": "해당 자치구 (예: 강남구)",
  "gazette_number": "고시번호 (예: 제2025-12호)",
  "announcement_date": "결정/고시일 (YYYY-MM-DD)",
  "effective_date": "효력발생일 (YYYY-MM-DD)",

  "building_coverage_ratio": "건폐율 (예: 60% 이하)",

  "base_floor_area_ratio": "기준용적률 (예: 200%)",
  "allowed_floor_area_ratio": "허용용적률 (예: 250%)",
  "max_floor_area_ratio": "상한용적률 (예: 300%)",
  "floor_area_ratio": "용적률 (기준/허용/상한 구분 없이 단일 값만 있는 경우)",
  "far_incentive_conditions": ["용적률 완화 조건 목록 (예: 공개공지 확보 시 +20%)"],

  "max_height_meters": null,
  "max_floors": null,
  "height_restrictions_detail": "높이 관련 상세 설명 (예: 가로변 20m 이하, 이면부 35m 이하 등)",

  "allowed_uses": ["허용 용도 목록"],
  "prohibited_uses": ["불허 용도 목록"],
  "use_ratios": ["용도별 비율 (예: 주거 60% 이상, 비주거 40% 이하 등)"],

  "development_restrictions": ["개발 관련 제한 사항 목록"],
  "construction_restrictions": ["건축 관련 제한 사항 목록"],
  "setback_requirements": "이격거리/건축선 설명 또는 null",

  "other_notes": ["위 항목에 해당하지 않는 기타 주요 사항 (조경, 주차, 공공시설 기부채납 등)"],
  "key_changes": ["이전 대비 주요 변경사항 (변경고시인 경우)"],

  "summary_korean": "300자 이내 핵심 요약 — 현재 적용 중인 건폐율·용적률·높이·주요 용도제한을 중심으로 작성. '변경없음'만 나열하지 말고 현행 수치 기재",
  "confidence": "high | medium | low"
}}

순수 JSON만 반환하세요."""
    return system, prompt


def build_analysis_prompt(title: str, cn_content: str) -> str:
    """분석 프롬프트 전문 반환 (Claude API 미호출, 사용자가 직접 Claude에 붙여넣기용)"""
    system, prompt = _build_prompt_parts(title, cn_content)
    return system + "\n\n---\n\n" + prompt


def analyze_announcement_with_claude(
    title: str,
    cn_content: str,
    api_key: str,
    model: str = "claude-sonnet-4-6",
) -> dict:
    """
    결정고시/열람공고 CN 내용을 Claude로 분석하여 건축 제한 정보 추출.
    CN이 짧아 건폐율/용적률 등 구체적 수치는 없을 수 있음 — 있는 정보만 추출.
    """
    try:
        import anthropic
    except ImportError:
        return {"error": "anthropic 미설치"}

    system, prompt = _build_prompt_parts(title, cn_content)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        system_cached = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        message = client.messages.create(
            model=model,
            max_tokens=2048,
            temperature=0,
            system=system_cached,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        import json
        result = json.loads(raw)
        logger.info(f"Claude 고시 분석 완료: {result.get('zone_name')}")
        return result
    except Exception as e:
        logger.warning(f"Claude 고시 분석 실패: {e}")
        return {"error": str(e), "confidence": "low"}


# ---------------------------------------------------------------------------
# 유틸리티
# ---------------------------------------------------------------------------

def _normalize_date(raw: str) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    if "T" in raw:
        return raw[:10]
    if len(raw) >= 8 and raw[4:5].isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw[:10]


def _detect_category(title: str) -> str:
    for cat in ["결정고시", "지정고시", "변경고시", "해제고시", "열람공고", "결정공고"]:
        if cat in title:
            return cat
    if "고시" in title:
        return "고시"
    if "공고" in title:
        return "공고"
    return ""


def _extract_district(text: str) -> str:
    for d in _DISTRICTS:
        if d in text:
            return d
    return ""


def _extract_zone_name(text: str, zone_names: list[str]) -> str:
    for zone in zone_names:
        if zone and zone in text:
            return zone
    return ""
