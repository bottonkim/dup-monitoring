"""
Claude API를 사용한 한국어 도시계획 PDF 구조화 분석
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """당신은 서울시 도시계획 문서를 전문으로 분석하는 한국어 도시계획 문서 분석가입니다.
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
9. 문서에 전혀 언급되지 않은 정보만 null로 처리

주요 인식 용어:
- 결정고시/지정고시/변경고시: 공식 결정 또는 변경 알림
- 열람공고: 주민 열람 공고
- 건폐율: 대지면적 대비 건축면적 비율
- 기준용적률: 해당 용도지역에 적용되는 기본 용적률
- 허용용적률: 인센티브 항목 적용 시 완화 가능한 용적률
- 상한용적률: 공공기여 등을 통해 도달 가능한 최대 용적률
- 허용용도/불허용도: 해당 구역 건물 사용 가능/불가 용도
- 개발행위허가: 개발 활동에 대한 허가 요건
"""

_USER_PROMPT_TEMPLATE = """다음은 서울시 도시계획 공문서에서 추출한 텍스트입니다.

## 공문서 정보
- 제목: {title}
- 출처: {source}
- 공고일: {published_at}

## PDF 본문
{pdf_text}

## 추출 요청

아래 JSON 스키마에 따라 정보를 **빠짐없이** 추출하세요.
각 항목은 문서에서 찾을 수 있는 한 반드시 채워야 합니다:

{{
  "zone_name": "구역명 (예: 강남구 삼성동 제1종지구단위계획구역)",
  "sub_zone": "해당 필지/지번이 속한 세부 구역명 — 결정조서 표에서 해당 지번 행의 구역 구분을 확인 (예: 제일은행 특별계획구역, A획지, 제1가구 등). 세부 구역 구분이 없으면 null",
  "zone_type": "지구단위계획구역 | 정비구역 | 특별계획구역 | 재정비촉진지구 | 기타",
  "action_type": "결정 | 변경 | 지정 | 해제 | 기타",
  "district": "해당 자치구 (예: 강남구)",
  "gazette_number": "고시번호 (예: 제2025-12호)",
  "announcement_date": "결정/고시일 (YYYY-MM-DD)",
  "effective_date": "효력발생일 (YYYY-MM-DD)",

  "building_coverage_ratio": "건폐율 (예: 60% 이하)",

  "base_floor_area_ratio": "기준용적률 (예: 200%)",
  "allowed_floor_area_ratio": "허용용적률 (예: 250%)",
  "max_floor_area_ratio": "상한용적률 (예: 300%)",
  "floor_area_ratio": "용적률 (기준/허용/상한 구분 없이 단일 값만 있는 경우)",
  "far_incentive_conditions": ["용적률 완화 조건 목록 (예: 공개공지 확보 시 +20%, 친환경 건축물 인증 시 +10%)"],

  "max_height_meters": 숫자 또는 null,
  "max_floors": 숫자 또는 null,
  "height_restrictions_detail": "높이 관련 상세 설명 (예: 가로변 20m 이하, 이면부 35m 이하 등)",

  "allowed_uses": ["허용 용도 목록"],
  "prohibited_uses": ["불허 용도 목록"],
  "use_ratios": ["용도별 비율 (예: 주거 60% 이상, 비주거 40% 이하, 판매시설 10% 이하 등)"],

  "development_restrictions": ["개발 관련 제한 사항 목록"],
  "construction_restrictions": ["건축 관련 제한 사항 목록"],
  "setback_requirements": "이격거리/건축선 설명 또는 null",

  "other_notes": ["위 항목에 해당하지 않는 기타 주요 사항 (조경, 주차, 공공시설 기부채납 등)"],
  "key_changes": ["이전 대비 주요 변경사항 (변경고시인 경우)"],

  "summary_korean": "300자 이내 핵심 요약 — 현재 적용 중인 건폐율·용적률·높이·주요 용도제한을 중심으로 작성. '변경없음'만 나열하지 말고 현행 수치 기재",
  "confidence": "high | medium | low"
}}

순수 JSON만 반환하세요."""


def _repair_truncated_json(raw: str) -> dict | None:
    """max_tokens로 잘린 JSON 복구 시도: 열린 괄호/따옴표를 닫아줌."""
    s = raw.rstrip()
    # 열린 문자열 닫기
    in_str = False
    prev = ''
    for ch in s:
        if ch == '"' and prev != '\\':
            in_str = not in_str
        prev = ch
    if in_str:
        s += '"'
    # 끝에 쉼표가 남아있으면 제거
    s = s.rstrip()
    while s.endswith(','):
        s = s[:-1].rstrip()
    # 열린 괄호 닫기
    for _ in range(s.count('[') - s.count(']')):
        s += ']'
    for _ in range(s.count('{') - s.count('}')):
        s += '}'
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def analyze_pdf(
    pdf_text: str,
    metadata: dict,
    api_key: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 8192,
    max_pdf_chars: int = 100_000,
) -> dict:
    """
    PDF 텍스트 + 메타데이터 → 구조화 JSON 추출 (Claude API)

    Args:
        pdf_text: pdfplumber로 추출한 텍스트
        metadata: {"title": ..., "source": ..., "published_at": ...}
        api_key: Anthropic API 키
        model: Claude 모델 ID
        max_tokens: 최대 출력 토큰 수
        max_pdf_chars: Claude에 전송할 최대 텍스트 길이

    Returns:
        구조화된 dict (실패 시 error 키 포함)
    """
    try:
        import anthropic
    except ImportError:
        logger.error("anthropic 미설치: pip install anthropic")
        return {"error": "anthropic 패키지 미설치", "confidence": "low"}

    from pdf.extractor import truncate_for_claude
    truncated = truncate_for_claude(pdf_text, max_pdf_chars)

    user_msg = _USER_PROMPT_TEMPLATE.format(
        title=metadata.get("title", ""),
        source=metadata.get("source", ""),
        published_at=metadata.get("published_at", ""),
        pdf_text=truncated,
    )

    client = anthropic.Anthropic(api_key=api_key)

    try:
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=0,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = message.content[0].text.strip()

        # max_tokens 초과로 잘린 경우 경고
        if message.stop_reason == "max_tokens":
            logger.warning(f"Claude 응답이 max_tokens에서 잘림 (model={model})")

        # JSON 파싱
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        # 잘린 JSON 복구 시도
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            repaired = _repair_truncated_json(raw)
            if repaired:
                result = repaired
                result["_truncated"] = True
                logger.warning("잘린 JSON 복구 성공")
            else:
                raise

        logger.info(f"Claude 분석 완료: zone={result.get('zone_name')}, confidence={result.get('confidence')}")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"Claude 응답 JSON 파싱 실패: {e}\n응답: {raw[:200]}")
        return {"error": f"JSON 파싱 실패: {e}", "raw_response": raw[:500], "confidence": "low"}
    except Exception as e:
        logger.error(f"Claude API 호출 실패: {e}")
        return {"error": str(e), "confidence": "low"}


def analyze_image_pdf(
    pdf_path: Path,
    metadata: dict,
    api_key: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 8192,
    max_pages: int = 5,
    zone_name: str = "",
) -> dict:
    """
    이미지 기반 PDF용: 페이지를 이미지로 변환 후 Claude Vision API 사용.
    zone_name이 있고 PDF가 긴 경우 → 2단계 TOC 스캔 (목차→해당 페이지만 분석).
    pdf2image + poppler 필요.
    """
    try:
        from pdf2image import convert_from_path
        import base64
        from io import BytesIO
    except ImportError:
        logger.warning("pdf2image 미설치 - 이미지 PDF 분석 불가")
        return {"error": "pdf2image 미설치", "confidence": "low"}

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except Exception as e:
        return {"error": str(e), "confidence": "low"}

    # PDF 총 페이지 수 확인
    try:
        from pdf2image.pdf2image import pdfinfo_from_path
        info = pdfinfo_from_path(str(pdf_path))
        total_pages = info.get("Pages", 0)
    except Exception:
        total_pages = 0

    # 긴 PDF + zone_name 있으면 → 2단계 TOC 스캔
    target_pages = None
    if zone_name and total_pages > 6:
        target_pages = _find_pages_via_toc(
            pdf_path, zone_name, total_pages, client, model, convert_from_path
        )

    if target_pages:
        # 2단계: TOC에서 찾은 페이지만 분석
        logger.info(f"TOC 스캔 결과 → 페이지 {target_pages} 분석")
        pages_to_analyze = target_pages[:max(max_pages, 12)]
    else:
        # 기본: 첫 N페이지 분석
        pages_to_analyze = list(range(1, min(max_pages, total_pages or max_pages) + 1))

    # 대상 페이지 이미지 변환
    try:
        images_with_pages = []
        for pg in pages_to_analyze:
            imgs = convert_from_path(str(pdf_path), dpi=150, first_page=pg, last_page=pg)
            if imgs:
                images_with_pages.append((pg, imgs[0]))
    except Exception as e:
        logger.error(f"PDF→이미지 변환 실패: {e}")
        return {"error": str(e), "confidence": "low"}

    if not images_with_pages:
        return {"error": "변환된 이미지 없음", "confidence": "low"}

    content = []
    for pg, img in images_with_pages:
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        b64 = base64.standard_b64encode(buf.getvalue()).decode()
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}
        })
        content.append({"type": "text", "text": f"위는 PDF {pg}페이지입니다."})

    content.append({"type": "text", "text": _USER_PROMPT_TEMPLATE.format(
        title=metadata.get("title", ""),
        source=metadata.get("source", ""),
        published_at=metadata.get("published_at", ""),
        pdf_text="(이미지 기반 PDF - 위 이미지에서 직접 추출)",
    )})

    try:
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=0,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception as e:
        logger.error(f"이미지 PDF Claude 분석 실패: {e}")
        return {"error": str(e), "confidence": "low"}


def _find_pages_via_toc(
    pdf_path: Path,
    zone_name: str,
    total_pages: int,
    client,
    model: str,
    convert_from_path,
) -> list[int] | None:
    """
    구보/시보 등 긴 PDF의 목차(1-2페이지)를 Vision으로 스캔하여
    zone_name 관련 페이지 번호를 찾는다.
    """
    import base64
    from io import BytesIO

    try:
        toc_images = convert_from_path(str(pdf_path), dpi=150, first_page=1, last_page=min(2, total_pages))
    except Exception as e:
        logger.warning(f"TOC 이미지 변환 실패: {e}")
        return None

    content = []
    for i, img in enumerate(toc_images, 1):
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        b64 = base64.standard_b64encode(buf.getvalue()).decode()
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}
        })
        content.append({"type": "text", "text": f"위는 PDF {i}페이지(목차)입니다."})

    content.append({"type": "text", "text": f"""이 PDF는 총 {total_pages}페이지의 구보(구청 관보)입니다.
위 목차 이미지에서 "{zone_name}" 관련 내용이 포함된 페이지 번호를 모두 찾아주세요.

중요: 같은 구역에 대해 여러 섹션이 있을 수 있습니다:
- 열람공고/결정고시 본문 (변경 개요)
- 결정조서 (건폐율, 용적률, 높이제한 등 상세 수치가 있는 표)
- 도면/부록
이 모든 섹션의 페이지를 포함해주세요.

규칙:
1. 반드시 JSON 배열만 반환 (예: [15, 16, 17, 25, 26])
2. 관련 페이지가 여러 개면 모두 포함 (최대 15개)
3. 목차에서 찾을 수 없으면 빈 배열 [] 반환
4. 구역명이 정확히 일치하지 않더라도 유사한 항목 포함
5. 순수 JSON 배열만 반환, 설명 없이"""})

    try:
        message = client.messages.create(
            model=model,
            max_tokens=256,
            temperature=0,
            messages=[{"role": "user", "content": content}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        pages = json.loads(raw)
        if isinstance(pages, list) and pages:
            # 유효한 페이지 번호만 필터
            valid = [p for p in pages if isinstance(p, int) and 1 <= p <= total_pages]
            if valid:
                # 찾은 페이지 뒤 3페이지도 포함 (결정조서가 바로 뒤에 있을 수 있음)
                extended = set(valid)
                max_found = max(valid)
                for extra in range(max_found + 1, min(max_found + 4, total_pages + 1)):
                    extended.add(extra)
                valid = sorted(extended)
                logger.info(f"TOC에서 '{zone_name}' 관련 페이지 발견 (확장): {valid}")
                return valid
        logger.info(f"TOC에서 '{zone_name}' 관련 페이지 미발견")
        return None
    except Exception as e:
        logger.warning(f"TOC 스캔 실패: {e}")
        return None
