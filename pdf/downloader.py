"""
PDF 다운로드 + 로컬 캐시 관리
"""
import hashlib
import logging
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_MAX_PDF_BYTES_DEFAULT = 50 * 1024 * 1024  # 50MB


def download_pdf(
    url: str,
    cache_dir: Path,
    max_bytes: int = _MAX_PDF_BYTES_DEFAULT,
    timeout: int = 30,
) -> Path:
    """
    PDF URL 다운로드 후 로컬 경로 반환.
    이미 캐시된 경우 즉시 반환.

    Raises:
        ValueError: 파일 크기 초과 또는 PDF 아닌 경우
        requests.RequestException: 다운로드 실패
    """
    cache_dir.mkdir(parents=True, exist_ok=True)

    # URL 해시로 파일명 결정 (중복 다운로드 방지)
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
    # 원본 파일명 추출
    raw_name = url.split("?")[0].rstrip("/").split("/")[-1]
    if not raw_name.lower().endswith(".pdf"):
        raw_name = raw_name + ".pdf"
    filename = f"{url_hash}_{raw_name}"
    local_path = cache_dir / filename

    if local_path.exists() and local_path.stat().st_size > 0:
        logger.debug(f"PDF 캐시 히트: {local_path.name}")
        return local_path

    logger.info(f"PDF 다운로드: {url}")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/pdf,*/*",
    }

    with requests.get(url, headers=headers, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")
        # 일부 정부 사이트는 application/octet-stream 으로 반환
        if "html" in content_type.lower():
            raise ValueError(f"PDF 가 아닌 HTML 응답: {url}")

        downloaded = 0
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        local_path.unlink(missing_ok=True)
                        raise ValueError(
                            f"PDF 크기 초과 ({downloaded / 1024 / 1024:.1f}MB > "
                            f"{max_bytes / 1024 / 1024:.0f}MB): {url}"
                        )
                    f.write(chunk)

    logger.info(f"PDF 다운로드 완료: {local_path.name} ({downloaded / 1024:.1f}KB)")
    return local_path
