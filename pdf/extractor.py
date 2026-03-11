"""
pdfplumber를 사용한 PDF 텍스트 추출 (한국어 지원)
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_IMAGE_PDF_THRESHOLD = 100  # 이 글자 수 미만이면 이미지 기반 PDF로 간주


def extract_text(pdf_path: Path) -> tuple[str, int, int]:
    """
    PDF에서 텍스트 추출.

    Returns:
        (text, page_count, char_count)
        text가 비어있으면 이미지 기반 PDF일 가능성 높음
    """
    try:
        import pdfplumber
    except ImportError:
        logger.error("pdfplumber 미설치: pip install pdfplumber")
        return "", 0, 0

    pages_text = []
    page_count = 0

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            page_count = len(pdf.pages)
            for i, page in enumerate(pdf.pages, 1):
                try:
                    text = page.extract_text(x_tolerance=3, y_tolerance=3)
                    if text:
                        pages_text.append(f"--- {i}페이지 ---\n{text}")
                except Exception as e:
                    logger.warning(f"페이지 {i} 텍스트 추출 실패: {e}")
    except Exception as e:
        logger.error(f"PDF 열기 실패 ({pdf_path.name}): {e}")
        return "", 0, 0

    full_text = "\n\n".join(pages_text)
    char_count = len(full_text)

    if char_count < _IMAGE_PDF_THRESHOLD:
        logger.info(f"이미지 기반 PDF 의심: {pdf_path.name} ({char_count}자)")
    else:
        logger.info(f"텍스트 추출 완료: {pdf_path.name} ({page_count}페이지, {char_count:,}자)")

    return full_text, page_count, char_count


def is_image_pdf(char_count: int) -> bool:
    return char_count < _IMAGE_PDF_THRESHOLD


def truncate_for_claude(text: str, max_chars: int = 100_000) -> str:
    """
    Claude API 전송용 텍스트 트리밍.
    너무 길면 앞 80% + 뒤 20% 유지
    """
    if len(text) <= max_chars:
        return text
    front = int(max_chars * 0.8)
    back = max_chars - front
    omitted = len(text) - front - back
    return (
        text[:front]
        + f"\n\n[... 중간 내용 약 {omitted:,}자 생략 ...]\n\n"
        + text[-back:]
    )
