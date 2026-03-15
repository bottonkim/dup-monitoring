"""
서울시보 PDF 다운로드 → 관련 페이지 추출 → Claude 분석
시보 번호(예: 제4122호) + 구역명으로 결정고시 건축 제한 추출

2단계 처리 전략 (대용량 PDF 메모리 최적화):
  Phase A: 전체 PDF에서 키워드 매칭 페이지 번호만 스캔 (텍스트 미보관)
  Phase B: 매칭 페이지만 소형 PDF로 분리 → 텍스트 추출
캐시 계층: 분석 결과 JSON > 추출 텍스트 > PDF 파일
"""
import gc
import hashlib
import json
import logging
import re
import requests
from pathlib import Path

logger = logging.getLogger(__name__)

_SIBO_LIST_URL = "https://event.seoul.go.kr/seoulsibo/list.do"
_SIBO_DOWNLOAD_URL = "https://event.seoul.go.kr/seoulsibo/fileDownload.do"


def _cache_key(sibo_no: str, zone_name: str) -> str:
    """시보 번호 + 구역명 → 캐시 파일명 프리픽스"""
    h = hashlib.md5(zone_name.encode()).hexdigest()[:10]
    return f"sibo{sibo_no}_{h}"


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
    캐시 계층: 분석 결과 JSON → 추출 텍스트 → PDF 파일
    """
    sibo_no = _extract_sibo_number(gazette_ref)
    if not sibo_no:
        logger.debug(f"시보 번호 추출 실패: {gazette_ref}")
        return None

    pdf_cache_dir.mkdir(parents=True, exist_ok=True)
    ck = _cache_key(sibo_no, zone_name)

    # ── 캐시 1: Claude 분석 결과 ──
    from pdf.claude_analyzer import SCHEMA_VERSION
    analysis_cache = pdf_cache_dir / f"{ck}_analysis.json"
    if analysis_cache.exists():
        try:
            cached = json.loads(analysis_cache.read_text(encoding="utf-8"))
            if cached and not cached.get("error") and cached.get("_schema_version") == SCHEMA_VERSION:
                logger.info(f"시보 분석 캐시 히트: {zone_name}")
                return cached
            elif cached and cached.get("_schema_version") != SCHEMA_VERSION:
                logger.info(f"시보 분석 캐시 스키마 변경 → 재분석: {zone_name}")
        except Exception:
            pass

    # ── 캐시 2: 추출 텍스트 ──
    text_cache = pdf_cache_dir / f"{ck}_text.txt"
    extracted_text = None
    if text_cache.exists() and text_cache.stat().st_size > 0:
        extracted_text = text_cache.read_text(encoding="utf-8")
        logger.info(f"시보 텍스트 캐시 히트: {zone_name} ({len(extracted_text)}자)")

    # ── PDF 처리 (캐시 미스) ──
    if not extracted_text:
        pdf_filename = _find_gazette_filename(sibo_no)
        if not pdf_filename:
            logger.debug(f"시보 PDF 파일명 조회 실패: 제{sibo_no}호")
            return None

        pdf_path = _download_gazette(pdf_filename, pdf_cache_dir, max_gazette_mb)
        if not pdf_path:
            return None

        search_terms = _build_search_terms(zone_name)
        extracted_text = _extract_in_subprocess(pdf_path, search_terms)
        if not extracted_text:
            logger.debug(f"시보에서 관련 페이지 미발견 또는 추출 실패: {zone_name}")
            return None

        # 텍스트 캐시 저장
        try:
            text_cache.write_text(extracted_text, encoding="utf-8")
            logger.debug(f"텍스트 캐시 저장: {text_cache.name} ({len(extracted_text)}자)")
        except Exception as e:
            logger.debug(f"텍스트 캐시 저장 실패: {e}")

    # ── Claude 분석 ──
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
        result["_schema_version"] = SCHEMA_VERSION
        logger.info(f"시보 분석 완료: {zone_name} → confidence={result.get('confidence')}")
        # 분석 결과 캐시 저장
        try:
            analysis_cache.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.debug(f"분석 캐시 저장 실패: {e}")
        return result

    return None


# ─────────────────────────────────────────────
# 시보 번호 / PDF 파일명 / 다운로드
# ─────────────────────────────────────────────

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

        idx = html.find(sibo_no)
        if idx == -1:
            logger.debug(f"시보 목록에서 제{sibo_no}호 미발견 (최근 목록에 없음)")
            return None

        after = html[idx:]
        m = re.search(r"(seoulsibo_\d{14}_\d{5}\.pdf)", after)
        if m:
            return m.group(1)

    except Exception as e:
        logger.warning(f"시보 목록 조회 실패: {e}")

    return None


def _download_gazette(filename: str, cache_dir: Path, max_mb: int) -> Path | None:
    """시보 PDF 다운로드 (캐시 사용, 스트리밍)"""
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
    generic = {"지구단위계획구역", "지구단위계획", "정비구역", "특별계획구역",
               "결정고시", "구역", "계획", "변경", "결정", "일대", "일원"}
    terms = []
    for part in zone_name.split():
        if part not in generic and len(part) >= 2:
            terms.append(part)
    if not terms and zone_name:
        terms = [zone_name[:4]]
    return terms


# ─────────────────────────────────────────────
# 2단계 PDF 처리 (subprocess 격리)
# ─────────────────────────────────────────────

def _subprocess_worker(q, path_str, terms, ctx_pages, mx_chars):
    """subprocess에서 실행되는 PDF 추출 워커 (모듈 레벨 — Windows pickle 호환)"""
    try:
        import resource
        mem_limit = 1536 * 1024 * 1024  # 1.5GB (fork 시 부모 VSZ 상속분 고려)
        resource.setrlimit(resource.RLIMIT_AS, (mem_limit, mem_limit))
    except (ImportError, ValueError, OSError):
        pass  # Windows 또는 제한 불가 환경
    try:
        text = _phased_extract(Path(path_str), terms, ctx_pages, mx_chars)
        q.put(("ok", text))
    except Exception as e:
        q.put(("error", str(e)))


def _extract_in_subprocess(
    pdf_path: Path,
    search_terms: list[str],
    context_pages: int = 12,
    max_chars: int = 50_000,
    timeout: int = 180,
) -> str | None:
    """
    자식 프로세스에서 2단계 PDF 처리 — OOM 시 메인 서버 보호.
    타임아웃 180초 (대용량 PDF 스캔+분리+추출 고려).
    Windows에서는 subprocess spawn이 느리므로 직접 호출.
    """
    import sys
    if sys.platform == "win32":
        # Windows: subprocess spawn 오버헤드 회피 → 직접 호출
        try:
            return _phased_extract(pdf_path, search_terms, context_pages, max_chars)
        except Exception as e:
            logger.warning(f"PDF 추출 오류 (direct): {e}")
            return None

    import multiprocessing

    q = multiprocessing.Queue()

    p = multiprocessing.Process(
        target=_subprocess_worker,
        args=(q, str(pdf_path), search_terms, context_pages, max_chars),
    )
    p.start()
    p.join(timeout)

    if p.is_alive():
        p.kill()
        p.join(5)
        logger.warning(f"PDF 추출 타임아웃 ({timeout}초): {pdf_path.name}")
        return None

    if p.exitcode != 0:
        logger.warning(f"PDF 추출 프로세스 비정상 종료 (exit={p.exitcode}): {pdf_path.name}")

    if q.empty():
        logger.warning(f"PDF 추출 결과 없음 (OOM 또는 크래시): {pdf_path.name}")
        return None

    try:
        status, result = q.get_nowait()
    except Exception:
        return None

    if status == "ok":
        return result
    logger.warning(f"PDF 추출 오류: {result}")
    return None


# ─────────────────────────────────────────────
# Phase A → B: 페이지 스캔 → 분리 → 추출
# ─────────────────────────────────────────────

def _phased_extract(
    pdf_path: Path,
    search_terms: list[str],
    context_pages: int = 15,
    max_chars: int = 80_000,
) -> str | None:
    """
    2단계 PDF 처리 (메모리 효율):
      Phase A: TOC 또는 키워드 스캔으로 페이지 번호만 확보
             + 결정조서 키워드 전방 스캔으로 범위 확장
             → 전체 PDF 해제
      Phase B: 관련 페이지만 소형 PDF로 분리 → 텍스트 추출
    """
    try:
        import fitz
    except ImportError:
        logger.debug("pymupdf 미설치, pdfplumber 폴백")
        return _extract_with_pdfplumber(pdf_path, search_terms, context_pages, max_chars)

    primary_term = search_terms[0] if search_terms else ""
    if not primary_term:
        return None

    file_mb = pdf_path.stat().st_size / 1024 / 1024

    # ════ Phase A: 페이지 번호만 스캔 (텍스트 미보관) ════
    doc = fitz.open(str(pdf_path))
    total = len(doc)
    logger.debug(f"시보 PDF: {total}페이지, {file_mb:.1f}MB")

    matched_pages = []

    # A-1: TOC(목차/북마크) 기반 탐색 — 메모리 최소
    try:
        toc = doc.get_toc()
        if toc:
            for _level, title, page_num in toc:
                if primary_term in title:
                    matched_pages.append(max(0, page_num - 1))
            if matched_pages:
                logger.debug(f"TOC에서 '{primary_term}' 발견: {len(matched_pages)}건")
    except Exception:
        pass

    # A-2: TOC 실패 시 전체 페이지 키워드 스캔
    if not matched_pages:
        for i in range(total):
            page = doc.load_page(i)
            text = page.get_text("text") or ""
            if primary_term in text:
                matched_pages.append(i)
            del text
            if i % 10 == 0:
                gc.collect()

    if not matched_pages:
        doc.close()
        del doc
        gc.collect()
        logger.debug(f"시보에서 '{primary_term}' 미발견")
        return None

    # 클러스터링 → 1차 페이지 범위
    start, preliminary_end = _select_page_range(matched_pages, context_pages, total)

    # A-3: 결정조서 키워드 전방 스캔 — 고시문 뒤의 결정조서/건축제한 내용 포착
    # 서울시보 구조: 고시문(구역명) → 결정조서(건폐율/용적률/높이 상세표)
    # 결정조서에는 구역명이 매 페이지에 없을 수 있으므로 별도 키워드로 스캔
    _REGULATION_KEYWORDS = ("건폐율", "용적률", "결정조서", "허용용도", "높이제한",
                            "허용 용도", "불허 용도", "용도별", "건축물")
    regulation_end = preliminary_end
    scan_limit = min(total, preliminary_end + 40)  # 최대 40페이지 더 스캔
    for i in range(preliminary_end, scan_limit):
        page = doc.load_page(i)
        text = page.get_text("text") or ""
        has_regulation = any(kw in text for kw in _REGULATION_KEYWORDS)
        del text
        if has_regulation:
            regulation_end = i + 1
        elif i > regulation_end + 3:
            # 결정조서 키워드 없는 페이지가 3페이지 연속이면 중단
            break
        if i % 10 == 0:
            gc.collect()

    end = min(total, max(preliminary_end, regulation_end + 2))

    # Phase A 완료 — 전체 PDF 메모리 해제
    doc.close()
    del doc
    gc.collect()

    page_count = end - start
    logger.debug(f"대상 페이지 범위: p{start+1}-{end} ({page_count}페이지)"
                 f"{' (결정조서 확장)' if end > preliminary_end else ''}")

    # ════ Phase B: 텍스트 추출 ════
    # 대용량 PDF(>30MB): 관련 페이지만 소형 PDF로 분리 후 추출
    if file_mb > 30:
        text = _extract_via_small_pdf(pdf_path, start, end, max_chars)
        if text:
            return text
        logger.debug("소형 PDF 분리 실패, 직접 추출로 폴백")

    # 소규모 PDF 또는 폴백: 직접 추출
    return _direct_extract_pymupdf(pdf_path, start, end, max_chars)


def _select_page_range(
    matched_pages: list[int], context_pages: int, total: int,
) -> tuple[int, int]:
    """매칭된 페이지들을 클러스터링하여 최적 페이지 범위 반환"""
    # 클러스터링 (5페이지 이내 간격)
    clusters = []
    cur_cluster = [matched_pages[0]]
    for p in matched_pages[1:]:
        if p - cur_cluster[-1] <= 5:
            cur_cluster.append(p)
        else:
            clusters.append(cur_cluster)
            cur_cluster = [p]
    clusters.append(cur_cluster)

    # 본문 클러스터 선택 (목차=앞 10페이지 & 2페이지 이하 클러스터 제외)
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
    return start, end


def _extract_via_small_pdf(
    pdf_path: Path, start: int, end: int, max_chars: int,
) -> str | None:
    """
    관련 페이지만 소형 PDF로 분리 후 텍스트 추출.
    원본 200MB → 소형 5-10MB로 메모리 부담 대폭 감소.
    """
    import fitz

    small_path = pdf_path.parent / f"_temp_{pdf_path.stem}_p{start}-{end}.pdf"
    try:
        # 원본에서 관련 페이지만 복사
        doc = fitz.open(str(pdf_path))
        small_doc = fitz.open()
        small_doc.insert_pdf(doc, from_page=start, to_page=end - 1)
        small_doc.save(str(small_path))
        small_doc.close()
        doc.close()
        del doc, small_doc
        gc.collect()

        # 소형 PDF에서 텍스트 추출
        small_doc = fitz.open(str(small_path))
        pages_text = []
        for i in range(len(small_doc)):
            page = small_doc.load_page(i)
            text = page.get_text("text") or ""
            if text.strip():
                pages_text.append(f"--- {start + i + 1}페이지 ---\n{text}")
            del text
        small_doc.close()
        gc.collect()

        full_text = "\n\n".join(pages_text)
        if len(full_text) > max_chars:
            full_text = full_text[:max_chars] + "\n\n[... 이하 생략 ...]"

        logger.info(
            f"시보 관련 페이지 (소형PDF): {len(pages_text)}페이지 "
            f"(p{start+1}-{end}), {len(full_text)}자"
        )
        return full_text

    except Exception as e:
        logger.warning(f"소형 PDF 처리 실패: {e}")
        return None
    finally:
        small_path.unlink(missing_ok=True)


def _direct_extract_pymupdf(
    pdf_path: Path, start: int, end: int, max_chars: int,
) -> str | None:
    """원본 PDF에서 직접 텍스트 추출 (소규모 PDF 또는 폴백)"""
    import fitz

    doc = fitz.open(str(pdf_path))
    pages_text = []
    for i in range(start, end):
        page = doc.load_page(i)
        text = page.get_text("text") or ""
        if text.strip():
            pages_text.append(f"--- {i + 1}페이지 ---\n{text}")
        del text
    doc.close()
    gc.collect()

    full_text = "\n\n".join(pages_text)
    if len(full_text) > max_chars:
        full_text = full_text[:max_chars] + "\n\n[... 이하 생략 ...]"

    logger.info(
        f"시보 관련 페이지 (직접추출): {len(pages_text)}페이지 "
        f"(p{start+1}-{end}), {len(full_text)}자"
    )
    return full_text


# ─────────────────────────────────────────────
# pdfplumber 폴백 (pymupdf 미설치 시)
# ─────────────────────────────────────────────

def _extract_with_pdfplumber(
    pdf_path: Path,
    search_terms: list[str],
    context_pages: int,
    max_chars: int,
) -> str | None:
    """pdfplumber 폴백 (pymupdf 미설치 시)"""
    try:
        import pdfplumber
    except ImportError:
        logger.error("pdfplumber/pymupdf 모두 미설치")
        return None

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

        start, end = _select_page_range(matched_pages, context_pages, total)

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
