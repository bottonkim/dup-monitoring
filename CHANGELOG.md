# DUP Monitoring Changelog

## 2025-03-15: 정비구역 AI 결정조서 연결 + DFL03 타겟 보정

### 배경 (문제)

노고산동 57-33 조회 시 AI가 **청년안심주택 합본 고시**(2024-05-09)를 결정고시 타겟으로 잡고,
**2025-3호 결정조서**(신촌동 주민센터 복합화)를 분석에 사용.
실제 최신 결정고시는 **2023-195호** "마포2 도시정비형 재개발구역 변경 및 지구단위계획 결정(변경)".

숭인동 1383은 AI가 **2021-194호**(280번지 도로/건축한계선 소규모 변경)를 잡고 있었으나,
종합 결정조서는 **2007-4호**에만 존재.

### 커밋 내역

#### `f992a41` — 정비구역 AI 결정조서 연결 (3건)

**Fix 1: spcfWtnnc propel enrichment** (`lookup/urban_seoul.py`)
- 기존: dstplanWtnnc gazette_history만 propel 파일 enrichment 적용
- 변경: spcfWtnnc gazette_history도 dstplanWtnnc propel 데이터로 보강
  - 정비구역 결정이 지구단위계획 결정을 겸하는 경우 propel에 포함됨
  - combined_gh로 한 번의 API 호출로 양쪽 모두 보강
- spcfWtnnc 자체 propel 시도를 폴백으로 추가 (미보강 항목 대상)
- `_wtnnc_sn` 필드를 각 notification에 추가하여 원본 wtnnc_sn 추적

**Fix 2: 정비구역 AI 타겟 오버라이드** (`api/routes.py`)
- sub_zone에 "정비구역" 포함 시, spcfWtnnc의 최신 DFL03 보유 gazette_history 엔트리를 AI 타겟으로 오버라이드
- 노고산동 57-33 결과: 청년안심주택 → 마포2 정비구역(2023-195) 결정조서

**Fix 3: DFL03 booster premature break 수정** (`api/routes.py`)
- 2차 검색에서 `len(pdf_urls) > 0` 조건이 기존 URL 때문에 항상 true → `_found_dfl03` 플래그로 수정
- 숭인동 1383 결과: 2007-4호의 `숭인_결정조서(서고2007-4).pdf` 정상 추가

#### `c659f1f` — DFL03 기반 AI 타겟 보정 + URL percent-encoding 수정

**DFL03 타겟 보정** (`api/routes.py`)
- 선택된 결정고시 날짜에 DFL03이 없으면, DFL03 보유 gazette_history 엔트리로 자동 보정
- 숭인동 1383: 2021-194(소규모 변경) → 2007-4(종합 결정조서)로 보정

**URL percent-encoding 수정**
- propel URL이 `quote()`로 인코딩되어 `"결정조서" in url` 검색 실패
- `unquote()` 적용으로 정상 매칭 (import 추가: `from urllib.parse import unquote`)
- 영향 범위: `_already_has_dfl03` 체크 + DFL03 booster 2차 검색

#### `f5ef077` — AI 분석 배지 분리 표시

- 기존: `AI 분석 (결정고시)` 단일 빨간 배지
- 변경: `AI 분석`(빨간 #ef4444) + `결정고시`(앰버 #d97706) 또는 `열람공고`(파란 #2563eb) 2개 배지
- 파일: `result.html`, `style.css`

### 검증된 테스트 주소

| 주소 | 변경 전 | 변경 후 |
|------|---------|---------|
| 마포구 노고산동 57-33 | 청년안심주택 + 2025-3 결정조서 | 마포2 정비구역 + 2023-195 결정조서 |
| 종로구 숭인동 1383 | 2021-194 (소규모 변경) | 2007-4 (종합 결정조서) |
| 성동구 도선동 39-2 | 2016-220 (정상) | 변경 없음 (회귀 없음) |

### 기술 상세

**데이터 흐름 (정비구역 케이스)**
```
spcfWtnnc gazette_history (propel enrichment 전):
  2023-195 → desc_detail="" | docs=0

dstplanWtnnc propel API (getPropelList.json):
  noticeNo에 2023-195 포함 (정비구역+지구단위 겸용 고시)

combined_gh enrichment 후:
  2023-195 → desc_detail="마포2..." | DFL01+DFL02×9+DFL03+DFL06 = 12건

AI 타겟 오버라이드:
  spcfWtnnc 2023-195의 DFL03+DFL01 → gyeoljeong_ann.pdf_urls
```

**DFL03 타겟 보정 로직**
```
1. gyeoljeong_ann의 published_at 날짜로 gazette_history에서 매칭
2. 매칭된 엔트리에 DFL03이 있으면 → 보정 불필요
3. DFL03 없으면 → gazette_history에서 DFL03 보유 + primary_kw 매칭 엔트리 탐색
4. 찾으면 gyeoljeong_ann을 해당 엔트리로 교체
5. 정비구역 오버라이드 등으로 이미 DFL03이 있으면 스킵 (unquote 체크)
```

---

## 2025-03-14: 정비구역 겹침 주소 조회 개선

### 커밋: `16aba3d`
- 정비구역과 지구단위계획구역이 겹치는 주소에서 구역명 보완
- sub_zone 연동: 정비구역명을 sub_zone으로 표시
- 고시공고 검색 확장: 정비구역 키워드 추가
- merge 라우팅: 겹치는 구역 결과 병합

### 커밋: `050ebff`
- 지번 필터에서 본번 비교로 수정 (부번 무시)
- 합본 고시 면제: 여러 구역을 합친 고시는 지번 필터 스킵

---

## 2025-03-13: AI 타겟 하이라이트 + 연혁 통합

### 커밋: `66a7ae6`
- AI 분석 대상 고시를 연혁에서 하이라이트 표시
- PDF URL 기반 매칭으로 정확도 향상
- 구보 내부 고시 제목 추출

### 커밋: `25d2903`
- AI 분석 대상 고시 하이라이트 (빨간 배지 + 펄스 애니메이션)
- 안내문구 교정, AI섹션 위로 이동

### 커밋: `570b11a`
- 연혁 + 고시공고 통합: 고시번호 기준 병합
- 출처 배지 (UPIS/DB/서울시/구청), 카드 목록 제거

---

## 2025-03-12: 연혁 보강 + 지번 필터링

### 커밋: `159152e` — 연혁 desc_detail 보강 + 지번 필터 본문 체크
### 커밋: `3cb6b87` — 고시 매칭 시 지번 필터링 추가
### 커밋: `f7839f5` — 연혁 desc_detail: generic 제목도 표시
### 커밋: `ef888dd` — UPIS 구역 조회 시 필지 중심점 포함 여부 필터링

---

## 2025-03-11: UI 개선

### 커밋: `6469fe6` — 구역 현황 10건 초과 시 접기/펼치기
### 커밋: `708be04` — 연혁 접기 display 복원을 block으로 수정
### 커밋: `7a3ae7a` — 연혁 목록 5건 초과 시 접기/펼치기

---

## 핵심 아키텍처 참고

### AI 타겟 선택 우선순위 (routes.py)

```
1. 정비구역 오버라이드: sub_zone에 "정비구역" → spcfWtnnc DFL03 엔트리
2. DFL03 타겟 보정: 선택된 고시 날짜에 DFL03 없으면 → DFL03 보유 엔트리로 교체
3. 고시공고 매칭: g_cf(확인) > g_cc(충족) > g_kw(키워드) > g_fb(폴백)
4. DFL03 booster: 1차(날짜 매칭) → 2차(키워드 매칭)
```

### propel enrichment 흐름 (urban_seoul.py)

```
1. getCUq161.json: wtnnc_sn → presentSn
2. getPropelList.json: presentSn → 고시별 fileList (DFL01~DFL06)
3. gazette_history in-place 수정: drawing_documents + desc_detail
4. combined_gh: dstplanWtnnc + spcfWtnnc gazette_history 합쳐서 한 번에 보강
5. spcfWtnnc 자체 propel 폴백: 미보강 항목에 대해 spcfWtnnc wtnnc_sn으로 재시도
```

### URL encoding 주의사항

propel에서 생성된 download_url은 `quote(fileName)`으로 인코딩됨.
한글 키워드("결정조서") 검색 시 반드시 `unquote(url)` 후 비교해야 함.
