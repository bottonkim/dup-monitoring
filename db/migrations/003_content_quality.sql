-- 고시 콘텐츠 품질 분류 (결정조서 상세 포함 여부)
-- 'detailed': 건폐율/용적률/허용용도 등 결정조서 핵심 키워드 2개 이상 포함
-- 'summary': 고시 요약 수준
-- 'minimal': 제목만 또는 10자 미만
ALTER TABLE announcements ADD COLUMN content_quality TEXT DEFAULT 'summary';

CREATE INDEX IF NOT EXISTS idx_ann_content_quality ON announcements(content_quality);
