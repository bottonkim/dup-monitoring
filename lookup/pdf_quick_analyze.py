"""
소형 PDF 즉시 분석 모듈
서울시 고시/구청 고시의 첨부 PDF(1-10MB)를 실시간 분석.
시보 PDF(100MB+)와 달리 subprocess 불필요 (메모리 부담 미미).
결과 캐시: data/pdfs/{hash}_analysis.json
"""
import hashlib
import json
import logging
import tempfile
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

_MAX_PDF_SIZE = 30 * 1024 * 1024  # 30MB 제한 (시보 제외)
_CACHE_DIR = Path("data/pdfs")


def analyze_small_pdf(
    pdf_url: str,
    title: str,
    api_key: str,
    model: str = "claude-sonnet-4-6",
    cache_dir: Path = _CACHE_DIR,
    zone_name: str = "",
) -> dict | None:
    """
    소형 PDF 다운로드 → 텍스트 추출 → Claude 분석.
    캐시된 결과가 있으면 재사용.

    반환: 구조화 분석 결과 dict 또는 None (실패 시)
    """
    if not pdf_url or not api_key:
        return None

    # 캐시 확인
    url_hash = hashlib.sha256(pdf_url.encode()).hexdigest()[:16]
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{url_hash}_analysis.json"

    from pdf.claude_analyzer import SCHEMA_VERSION
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached and not cached.get("error") and cached.get("_schema_version") == SCHEMA_VERSION:
                logger.info(f"PDF 분석 캐시 사용: {cache_path.name}")
                cached["_gazette_source"] = "첨부 PDF (캐시)"
                return cached
            elif cached and cached.get("_schema_version") != SCHEMA_VERSION:
                logger.info(f"PDF 분석 캐시 스키마 변경 → 재분석: {cache_path.name}")
        except Exception:
            pass

    # PDF 다운로드
    try:
        resp = requests.get(pdf_url, headers=_HEADERS, timeout=60, stream=True)
        resp.raise_for_status()

        # 크기 확인
        content_length = int(resp.headers.get("Content-Length", 0))
        if content_length > _MAX_PDF_SIZE:
            logger.info(f"PDF 크기 초과 ({content_length / 1024 / 1024:.1f}MB): {pdf_url[:80]}")
            return None

        # 임시 파일에 저장
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            total = 0
            for chunk in resp.iter_content(chunk_size=8192):
                total += len(chunk)
                if total > _MAX_PDF_SIZE:
                    logger.info(f"PDF 다운로드 중 크기 초과: {pdf_url[:80]}")
                    return None
                tmp.write(chunk)
            tmp_path = Path(tmp.name)

    except Exception as e:
        logger.warning(f"PDF 다운로드 실패: {e}")
        return None

    # 텍스트 추출 (PyMuPDF)
    try:
        text = _extract_text_fitz(tmp_path)
        text_len = len(text) if text else 0

        if text and text_len >= 100:
            # 텍스트 기반 PDF → Claude 텍스트 분석
            try:
                from pdf.claude_analyzer import analyze_pdf
                result = analyze_pdf(
                    pdf_text=text,
                    metadata={"title": title, "source": "첨부 PDF", "published_at": ""},
                    api_key=api_key,
                    model=model,
                    max_pdf_chars=50000,
                )
                if result and not result.get("error"):
                    result["_gazette_source"] = "첨부 PDF"
                    result["_schema_version"] = SCHEMA_VERSION
                    cache_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                    return result
            except Exception as e:
                logger.warning(f"PDF Claude 분석 실패: {e}")
        else:
            # 이미지 기반 PDF → Claude Vision API 폴백
            logger.info(f"PDF 텍스트 부족 ({text_len}자), Vision API 폴백: {pdf_url[:80]}")
            try:
                from pdf.claude_analyzer import analyze_image_pdf
                result = analyze_image_pdf(
                    pdf_path=tmp_path,
                    metadata={"title": title, "source": "첨부 PDF (이미지)", "published_at": ""},
                    api_key=api_key,
                    model=model,
                    max_pages=10,
                    zone_name=zone_name,
                )
                if result and not result.get("error"):
                    result["_gazette_source"] = "첨부 PDF (이미지)"
                    result["_schema_version"] = SCHEMA_VERSION
                    cache_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                    return result
            except Exception as e:
                logger.warning(f"이미지 PDF Vision 분석 실패: {e}")
    except Exception as e:
        logger.warning(f"PDF 텍스트 추출 실패: {e}")
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass

    return None


def _extract_text_fitz(pdf_path: Path, max_pages: int = 100) -> str:
    """PyMuPDF(fitz)로 PDF 텍스트 추출 (페이지 단위 스트리밍)"""
    import gc

    try:
        import fitz
    except ImportError:
        logger.error("PyMuPDF 미설치: pip install PyMuPDF")
        return ""

    texts = []
    doc = fitz.open(str(pdf_path))
    try:
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            texts.append(page.get_text())
            if i % 20 == 19:
                gc.collect()
    finally:
        doc.close()

    return "\n".join(texts)
