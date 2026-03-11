-- PDF AI 분석 결과 테이블
CREATE TABLE IF NOT EXISTS pdf_extractions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    pdf_attachment_id INTEGER NOT NULL REFERENCES pdf_attachments(id),
    extracted_at      TEXT NOT NULL,
    claude_model      TEXT NOT NULL,
    raw_text_chars    INTEGER,
    structured_json   TEXT NOT NULL,
    extraction_status TEXT DEFAULT 'done',
    error_message     TEXT
);
