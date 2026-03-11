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
2. 문서에 없는 정보는 null로 처리, 절대 추측하지 않음
3. 숫자와 날짜는 정확하게 추출
4. 한국어 원문 그대로 사용 (번역 불필요)

주요 인식 용어:
- 결정고시/지정고시/변경고시: 공식 결정 또는 변경 알림
- 열람공고: 주민 열람 공고
- 건폐율: 대지면적 대비 건축면적 비율
- 용적률(FAR): 대지면적 대비 연면적 비율
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

아래 JSON 스키마로 정보를 추출하세요:

{{
  "zone_name": "구역명 (예: 강남구 삼성동 제1종지구단위계획구역)",
  "zone_type": "지구단위계획구역 | 정비구역 | 특별계획구역 | 재정비촉진지구 | 기타",
  "action_type": "결정 | 변경 | 지정 | 해제 | 기타",
  "district": "해당 자치구 (예: 강남구)",
  "gazette_number": "고시번호 (예: 제2025-12호)",
  "announcement_date": "결정/고시일 (YYYY-MM-DD)",
  "effective_date": "효력발생일 (YYYY-MM-DD)",
  "building_coverage_ratio": "건폐율 (예: 60%)",
  "floor_area_ratio": "용적률 (예: 400%)",
  "max_height_meters": 숫자 또는 null,
  "max_floors": 숫자 또는 null,
  "setback_requirements": "이격거리/건축선 설명 또는 null",
  "allowed_uses": ["허용 용도 목록"],
  "prohibited_uses": ["불허 용도 목록"],
  "development_restrictions": ["개발 관련 제한 사항 목록"],
  "construction_restrictions": ["건축 관련 제한 사항 목록"],
  "key_changes": ["이전 대비 주요 변경사항 (변경고시인 경우)"],
  "summary_korean": "200자 이내 핵심 요약",
  "confidence": "high | medium | low"
}}

순수 JSON만 반환하세요."""


def analyze_pdf(
    pdf_text: str,
    metadata: dict,
    api_key: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 4096,
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

        # JSON 파싱
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
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
    max_tokens: int = 4096,
    max_pages: int = 5,
) -> dict:
    """
    이미지 기반 PDF용: 페이지를 이미지로 변환 후 Claude Vision API 사용
    pdf2image + poppler 필요
    """
    try:
        from pdf2image import convert_from_path
        import base64
        from io import BytesIO
    except ImportError:
        logger.warning("pdf2image 미설치 - 이미지 PDF 분석 불가")
        return {"error": "pdf2image 미설치", "confidence": "low"}

    try:
        images = convert_from_path(str(pdf_path), dpi=150, first_page=1, last_page=max_pages)
    except Exception as e:
        logger.error(f"PDF→이미지 변환 실패: {e}")
        return {"error": str(e), "confidence": "low"}

    content = []
    for i, img in enumerate(images, 1):
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        b64 = base64.standard_b64encode(buf.getvalue()).decode()
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}
        })
        content.append({"type": "text", "text": f"위는 PDF {i}페이지입니다."})

    content.append({"type": "text", "text": _USER_PROMPT_TEMPLATE.format(
        title=metadata.get("title", ""),
        source=metadata.get("source", ""),
        published_at=metadata.get("published_at", ""),
        pdf_text="(이미지 기반 PDF - 위 이미지에서 직접 추출)",
    )})

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
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
