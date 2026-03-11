-- 고시공고 테이블
CREATE TABLE IF NOT EXISTS announcements (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    title        TEXT NOT NULL,
    category     TEXT,
    district     TEXT,
    zone_name    TEXT,
    published_at TEXT,
    fetched_at   TEXT NOT NULL,
    url          TEXT,
    content_hash TEXT NOT NULL,
    raw_content  TEXT,
    is_new       INTEGER DEFAULT 1,
    notified_at  TEXT,
    UNIQUE(source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_ann_published ON announcements(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_ann_source ON announcements(source, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_ann_new ON announcements(is_new, notified_at);
CREATE INDEX IF NOT EXISTS idx_ann_zone ON announcements(zone_name);

-- PDF 첨부파일 테이블
CREATE TABLE IF NOT EXISTS pdf_attachments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    announcement_id INTEGER NOT NULL REFERENCES announcements(id),
    pdf_url         TEXT NOT NULL UNIQUE,
    filename        TEXT,
    local_path      TEXT,
    download_status TEXT DEFAULT 'pending',
    downloaded_at   TEXT,
    file_size_bytes INTEGER
);

-- 스크래퍼 실행 이력
CREATE TABLE IF NOT EXISTS scraper_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scraper_name  TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT,
    items_found   INTEGER DEFAULT 0,
    items_new     INTEGER DEFAULT 0,
    error_message TEXT
);

-- 지번 조회 이력
CREATE TABLE IF NOT EXISTS lookup_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    queried_at  TEXT NOT NULL,
    address     TEXT NOT NULL,
    pnu         TEXT,
    zone_names  TEXT,
    result_json TEXT
);

-- 이메일 발송 이력
CREATE TABLE IF NOT EXISTS email_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at          TEXT NOT NULL,
    recipient        TEXT NOT NULL,
    subject          TEXT NOT NULL,
    email_type       TEXT,
    announcement_ids TEXT
);
