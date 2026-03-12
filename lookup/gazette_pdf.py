"""
서울시보 PDF 다운로드 → 관련 페이지 추출 → Claude 분석
시보 번호(예: 제4122호) + 구역명으로 결정고시 건축 제한 추출
"""
import hashlib
import logging
import re
import requests
from pathlib import Path

logger = logging.getLogger(__name__)

_SIBO_LIST_URL = "https://event.seoul.go.kr/seoulsibo/list.do"
_SIBO_DOWNLOAD_URL = "https://event.seoul.go.kr/seoulsibo/fileDownload.do"


def analyze_gazette_for_zone(
    gazette_ref: str,
    zone_name: str,
    anthropic_api_key: str,
    pdf_cache_dir: Path,
    claude_model: str = "claude-sonnet-4-6",
    max_gazette_mb: int = 200,
) -> dict | None:
    """
    시보 번호 + 구역명 → 시보 PDF에서 관련 페이지 추출 → Claude 분석.

    Args:
        gazette_ref: "제4122호 제2026-53호" 형식의 시보/고시 참조
        zone_name: "왕십리 광역중심 지구단위계획구역" 등
        anthropic_api_key: Claude API 키
        pdf_cache_dir: PDF 캐시 디렉토리
        claude_model: 사용할 Claude 모델
        max_gazette_mb: 최대 다운로드 크기 (MB)

    Returns:
        구조화 분석 dict 또는 None
    """
    # 1. 시보 번호 추출
    sibo_no = _extract_sibo_number(gazette_ref)
    if not sibo_no:
        logger.debug(f"시보 번호 추출 실패: {gazette_ref}")
        return None

    # 2. 시보 PDF 파일명 조회
    pdf_filename = _find_gazette_filename(sibo_no)
    if not pdf_filename:
        logger.debug(f"시보 PDF 파일명 조회 실패: 제{sibo_no}호")
        return None

    # 3. PDF 다운로드 (캐시)
    pdf_path = _download_gazette(pdf_filename, pdf_cache_dir, max_gazette_mb)
    if not pdf_path:
        return None

    # 4. 관련 페이지 추출
    # 검색 키워드: 구역명의 핵심 부분
    search_terms = _build_search_terms(zone_name)
    extracted_text = _extract_relevant_pages(pdf_path, search_terms)
    if not extracted_text:
        logger.debug(f"시보에서 관련 페이지 미발견: {zone_name}")
        return None

    # 5. Claude 분석
    from pdf.claude_analyzer import analyze_pdf
    result = analyze_pdf(
        extracted_text,
        metadata={
            "title": f"서울시보 제{sibo_no}호 - {zone_name} 결정고시",
            "source": f"서울시보 제{sibo_no}호",
            "published_at": "",
        },
        api_key=anthropic_api_key,
        model=claude_model,
    )

    if result and not result.get("error"):
        result["_gazette_source"] = f"서울시보 제{sibo_no}호"
        logger.info(f"시보 분석 완료: {zone_name} → confidence={result.get('confidence')}")
        return result

    return None


def _extract_sibo_number(gazette_ref: str) -> str | None:
    """'제4122호 제2026-53호' → '4122'"""
    m = re.search(r"제(\d{4,5})호", gazette_ref)
    return m.group(1) if m else None


def _find_gazette_filename(sibo_no: str) -> str | None:
    """시보 번호로 PDF 파일명 조회 (서울시보 목록 페이지에서)"""
    try:
        resp = requests.get(_SIBO_LIST_URL, timeout=15,
                            headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        html = resp.text

        # 시보 번호 근처의 PDF 파일명 찾기
        # HTML 구조: ...4122... seoulsibo_YYYYMMDDHHMMSS_XXXXX.pdf ...
        idx = html.find(sibo_no)
        if idx == -1:
            logger.debug(f"시보 목록에서 제{sibo_no}호 미발견 (최근 목록에 없음)")
            return None

        # 시보 번호 이후 가장 가까운 PDF 파일명
        after = html[idx:]
        m = re.search(r"(seoulsibo_\d{14}_\d{5}\.pdf)", after)
        if m:
            return m.group(1)

    except Exception as e:
        logger.warning(f"시보 목록 조회 실패: {e}")

    return None


def _download_gazette(filename: str, cache_dir: Path, max_mb: int) -> Path | None:
    """시보 PDF 다운로드 (캐시 사용)"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    local_path = cache_dir / filename

    if local_path.exists() and local_path.stat().st_size > 0:
        logger.debug(f"시보 PDF 캐시 히트: {filename}")
        return local_path

    url = f"{_SIBO_DOWNLOAD_URL}?fileName={filename}"
    logger.info(f"시보 PDF 다운로드: {filename}")

    try:
        resp = requests.get(url, timeout=120, stream=True,
                            headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()

        max_bytes = max_mb * 1024 * 1024
        downloaded = 0
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(8192):
                if chunk:
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        local_path.unlink(missing_ok=True)
                        logger.warning(f"시보 PDF 크기 초과: {downloaded/1024/1024:.0f}MB > {max_mb}MB")
                        return None
                    f.write(chunk)

        logger.info(f"시보 PDF 다운로드 완료: {filename} ({downloaded/1024/1024:.1f}MB)")
        return local_path

    except Exception as e:
        local_path.unlink(missing_ok=True)
        logger.warning(f"시보 PDF 다운로드 실패: {e}")
        return None


def _build_search_terms(zone_name: str) -> list[str]:
    """구역명에서 검색 키워드 추출"""
    # "왕십리 광역중심 지구단위계획구역" → ["왕십리", "광역중심"]
    generic = {"지구단위계획구역", "지구단위계획", "정비구역", "특별계획구역",
               "결정고시", "구역", "계획", "변경", "결정", "일대", "일원"}
    terms = []
    for part in zone_name.split():
        if part not in generic and len(part) >= 2:
            terms.append(part)
    # 최소 1개 키워드
    if not terms and zone_name:
        terms = [zone_name[:4]]
    return terms


def _extract_relevant_pages(
    pdf_path: Path,
    search_terms: list[str],
    context_pages: int = 12,
    max_chars: int = 50_000,
) -> str | None:
    """PDF에서 검색어가 포함된 페이지 클러스터 추출 (메모리 효율적, 페이지 단위 처리)"""
    # PyMuPDF(fitz) 우선 사용 — C 기반으로 메모리 효율적
    try:
        import fitz  # pymupdf
        return _extract_with_pymupdf(pdf_path, search_terms, context_pages, max_chars)
    except ImportError:
        logger.debug("pymupdf 미설치, pdfplumber 폴백")

    # 폴백: pdfplumber (메모리 많이 사용)
    try:
        import pdfplumber
    except ImportError:
        logger.error("pdfplumber/pymupdf 모두 미설치")
        return None

    return _extract_with_pdfplumber(pdf_path, search_terms, context_pages, max_chars)


def _extract_with_pymupdf(
    pdf_path: Path,
    search_terms: list[str],
    context_pages: int,
    max_chars: int,
) -> str | None:
    """PyMuPDF(fitz)로 페이지 단위 스트리밍 추출 — 대용량 PDF에 적합"""
    import fitz
    import gc

    doc = fitz.open(str(pdf_path))
    total = len(doc)
    logger.debug(f"시보 PDF (pymupdf): {total} 페이지, {pdf_path.stat().st_size / 1024 / 1024:.1f}MB")

    # 1단계: 키워드 포함 페이지 스캔 (페이지별로 처리 후 즉시 해제)
    matched_pages = []
    primary_term = search_terms[0] if search_terms else ""
    if not primary_term:
        doc.close()
        return None

    for i in range(total):
        page = doc.load_page(i)
        text = page.get_text("text") or ""
        if primary_term in text:
            matched_pages.append(i)
        # 명시적 메모리 해제
        del text
        if i % 50 == 0:
            gc.collect()

    if not matched_pages:
        doc.close()
        logger.debug(f"시보에서 '{primary_term}' 미발견")
        return None

    # 2단계: 클러스터링
    clusters = []
    cur_cluster = [matched_pages[0]]
    for p in matched_pages[1:]:
        if p - cur_cluster[-1] <= 5:
            cur_cluster.append(p)
        else:
            clusters.append(cur_cluster)
            cur_cluster = [p]
    clusters.append(cur_cluster)

    # 3단계: 본문 클러스터 선택 (목차 제외)
    best_cluster = None
    for cl in clusters:
        if min(cl) < 10 and len(cl) <= 2:
            continue
        if best_cluster is None or len(cl) > len(best_cluster):
            best_cluster = cl

    if not best_cluster:
        best_cluster = clusters[-1]

    # 4단계: 해당 페이지만 텍스트 추출
    start = max(0, min(best_cluster))
    end = min(total, max(best_cluster) + context_pages + 1)

    pages_text = []
    for i in range(start, end):
        page = doc.load_page(i)
        text = page.get_text("text") or ""
        if text.strip():
            pages_text.append(f"--- {i+1}페이지 ---\n{text}")
        del text

    doc.close()
    gc.collect()

    full_text = "\n\n".join(pages_text)
    if len(full_text) > max_chars:
        full_text = full_text[:max_chars] + "\n\n[... 이하 생략 ...]"

    logger.info(f"시보 관련 페이지 (pymupdf): {len(pages_text)}페이지 (p{start+1}-{end}), {len(full_text)}자")
    return full_text


def _extract_with_pdfplumber(
    pdf_path: Path,
    search_terms: list[str],
    context_pages: int,
    max_chars: int,
) -> str | None:
    """pdfplumber 폴백 (pymupdf 미설치 시)"""
    import pdfplumber

    matched_pages = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        total = len(pdf.pages)
        logger.debug(f"시보 PDF (pdfplumber): {total} 페이지")

        for i, page in enumerate(pdf.pages):
            try:
                text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
                if search_terms and search_terms[0] in text:
                    matched_pages.append(i)
            except Exception:
                pass

        if not matched_pages:
            return None

        clusters = []
        cur_cluster = [matched_pages[0]]
        for p in matched_pages[1:]:
            if p - cur_cluster[-1] <= 5:
                cur_cluster.append(p)
            else:
                clusters.append(cur_cluster)
                cur_cluster = [p]
        clusters.append(cur_cluster)

        best_cluster = None
        for cl in clusters:
            if min(cl) < 10 and len(cl) <= 2:
                continue
            if best_cluster is None or len(cl) > len(best_cluster):
                best_cluster = cl

        if not best_cluster:
            best_cluster = clusters[-1]

        start = max(0, min(best_cluster))
        end = min(total, max(best_cluster) + context_pages + 1)

        pages_text = []
        for i in range(start, end):
            try:
                text = pdf.pages[i].extract_text(x_tolerance=3, y_tolerance=3) or ""
                if text.strip():
                    pages_text.append(f"--- {i+1}페이지 ---\n{text}")
            except Exception:
                pass

    full_text = "\n\n".join(pages_text)
    if len(full_text) > max_chars:
        full_text = full_text[:max_chars] + "\n\n[... 이하 생략 ...]"

    logger.info(f"시보 관련 페이지 (pdfplumber): {len(pages_text)}페이지 (p{start+1}-{end}), {len(full_text)}자")
    return full_text
