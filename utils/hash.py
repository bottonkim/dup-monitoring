"""변경 감지용 해시 유틸리티"""
import hashlib


def content_hash(title: str, content: str = "") -> str:
    """제목+내용의 SHA-256 해시 반환 (변경 감지용)"""
    combined = f"{title.strip().lower()}|{content.strip().lower()}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()
