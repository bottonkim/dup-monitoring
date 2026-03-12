# 서울시 도시계획 구역 조회 시스템

## 프로젝트 구조

```
├── main.py                 # 진입점 (--no-scheduler, --run-once, --host, --port)
├── api/routes.py           # FastAPI 라우트 (/lookup, /suggest, /api/analyze-gazette)
├── config/settings.py      # .env 환경변수 로드
├── db/                     # SQLite DB + 마이그레이션
├── lookup/                 # 외부 API 조회 모듈
│   ├── address.py          # juso.go.kr → PNU + VWORLD 지오코딩
│   ├── vworld.py           # VWORLD Data/WFS/NED API (도시계획구역, 지목, 면적)
│   ├── urban_seoul.py      # urban.seoul.go.kr ArcGIS (UPIS 구역명 + 연혁 + 도면)
│   ├── announcements.py    # 고시공고 검색 + 벌크 임포트 + Claude 분석
│   ├── gazette_pdf.py      # 서울시보 PDF 다운로드 + subprocess 격리 분석
│   ├── seoul_notice.py     # 서울시 고시공고 실시간 검색 (www.seoul.go.kr)
│   ├── gu_announce.py      # 구청 고시공고 실시간 조회 (9개 구청)
│   ├── gu_gazette.py       # 구청 구보 조회 (3개 구청)
│   ├── gu_planning.py      # 구청 지구단위계획 게시판 조회 (3개 구청)
│   ├── pdf_quick_analyze.py # 소형 첨부 PDF 즉시 분석 (1-30MB, subprocess 불필요)
│   └── tojieum.py          # 토지이음 (Playwright 필요)
├── frontend/
│   ├── templates/          # Jinja2 (index.html, result.html)
│   └── static/style.css
├── scrapers/               # 백그라운드 스크래퍼 (4개 소스)
├── scheduler/jobs.py       # APScheduler 작업 정의
├── pdf/                    # PDF 추출/Claude 분석
│   ├── extractor.py        # pdfplumber 텍스트 추출
│   └── claude_analyzer.py  # Claude API 구조화 분석 (8항목)
├── notifications/          # 이메일 발송
└── deploy/                 # 서버 배포 스크립트 (systemd, nginx)
```

## 조회 파이프라인 (api/routes.py)

1. 주소 → PNU + 좌표 (juso.go.kr + VWORLD geocoding)
2. **병렬 실행 1** (ThreadPoolExecutor 6개):
   - VWORLD Data API 11개 레이어 (용도지역, 지구단위 등)
   - VWORLD NED API (지목, 면적)
   - VWORLD 구역명 전용 레이어 3개
   - VWORLD WFS API 4개 레이어
   - UPIS 구역명 + 연혁 + 도면 (urban.seoul.go.kr)
   - 토지이음 (Playwright 필요)
3. 구역명 확보 → **병렬 실행 2** (ThreadPoolExecutor 4개, 해당 구청만 타겟):
   - 서울시 고시공고 검색 (www.seoul.go.kr)
   - 구청 고시공고 검색 (해당 구청 웹사이트)
   - 구청 구보 검색
   - 구청 지구단위계획 게시판 검색
4. 외부 검색 결과 + DB 고시공고 병합 (제목 중복 제거)
5. AI 분석: 비동기 AJAX (/api/analyze-gazette) — 6단계 폴백

## AI 분석 파이프라인 (/api/analyze-gazette, POST)

6단계 폴백 (세마포어로 동시 1건만 처리, 시보 PDF는 최후 수단):
1. **고시 상세** — ann_cn이 detailed (결정조서 키워드 2개 이상 포함)
2. **UPIS 상세** — upis_content가 detailed
3. **고시공고** — ann_cn이 summary 수준이라도 분석 시도
4. **첨부 PDF** — 서울시/구청 고시 첨부 PDF (1-30MB, subprocess 불필요, pdf_quick_analyze.py)
5. **시보 PDF** — 위에서 미확보 시에만 (subprocess 격리, 500MB 메모리 제한, 180초 타임아웃)
6. **UPIS summary 폴백** — 요약 수준 UPIS content

content_quality 판별: `_DETAIL_KEYWORDS` (건폐율, 용적률, 허용용도, 불허용도, 높이제한, 결정조서) 중 2개 이상 → "detailed"

캐시 계층: 분석 결과 JSON → 추출 텍스트 → PDF 파일 (한번 성공 시 재처리 불필요)
- 시보 캐시: `data/pdfs/sibo{번호}_{hash}_analysis.json`, `*_text.txt`
- 첨부 PDF 캐시: `data/pdfs/{url_hash}_analysis.json`
- 캐시 삭제 시 재추출됨 (코드 변경 후 기존 캐시 삭제 필요)

**시보 PDF 2단계 처리 (gazette_pdf.py)**:
- Phase A: 전체 PDF에서 구역명 키워드 매칭 페이지 스캔 (텍스트 미보관)
  - A-1: TOC 기반 탐색
  - A-2: 전체 페이지 키워드 스캔
  - A-3: 결정조서 키워드 전방 스캔 (건폐율/용적률/결정조서/허용용도/높이제한 등, 최대 40페이지)
- Phase B: 매칭 페이지만 소형 PDF로 분리 → 텍스트 추출 → Claude 분석

**시보 PDF 처리 선행 조건**:
- `gazette_ref`(고시 raw_content)에 시보 번호("제XXXX호" 형식)가 포함되어야 함
- 시보 번호 없으면 PDF 분석 스킵 → 고시공고/UPIS 폴백으로 진행
- `seoul_gazette` 소스 공고는 시보 번호 포함, `upis_api` 소스 공고는 미포함이 일반적

추출 항목 (pdf/claude_analyzer.py + lookup/announcements.py 동일 스키마):
1. 건폐율
2. 기준용적률 / 허용용적률 / 상한용적률
3. 용적률 완화 조건
4. 높이제한 (수치 + 상세)
5. 허용/불허 용도
6. 용도별 비율
7. 건축/개발 제한사항
8. 기타사항 (조경, 주차, 기부채납 등)

**중요**: `claude_analyzer.py`(PDF 분석)와 `announcements.py`(CN/UPIS 폴백 분석)의 프롬프트 스키마는 반드시 동일하게 유지

## 고시공고 매칭 로직

검색 우선순위 (폴백 계층):
1. **구체적 구역명** (UPIS/VWORLD/토지이음에서 가져온 구역명)
2. **동 이름** (emdNm) — 1번 결과 없을 때만
3. **자치구** (sggNm) — 2번도 없을 때만

DB 검색: `title`과 `zone_name` 필드만 LIKE 검색 (raw_content 제외 — 오매칭 방지)

## 외부 실시간 검색 (lookup/)

주소 입력 시 해당 구청만 타겟으로 실시간 검색 (백그라운드 스크래핑 아님):
- `seoul_notice.py`: 서울시 고시공고 검색 (bbsNo=277, 제목 검색 → 상세페이지 본문 + 첨부 PDF)
- `gu_announce.py`: 구청 고시공고 (9개 구청: 성동, 강남, 마포, 강동, 송파, 서초, 영등포, 용산, 종로)
- `gu_gazette.py`: 구청 구보 (3개 구청: 성동, 강남, 마포)
- `gu_planning.py`: 구청 지구단위계획 게시판 (3개 구청: 강남, 서초, 송파)

미설정 구청은 빈 리스트 반환 → 기존 폴백(DB + 시보 PDF)으로 처리.
구청 설정은 점진적 확대 가능 (GU_CONFIGS dict 구조).

## UPIS 연혁 데이터 (lookup/urban_seoul.py)

- `fetch_zone_data()`: PNU → 구역명 + 최신 고시 + 연혁 + 도면
- `_parse_gazette_history()`: tnNtfc.content에서 고시번호+날짜 파싱
- 도면 다운로드 URL: `https://urban.seoul.go.kr/{dImagePath}/{dImageName}`
- 고시 원문 URL: `https://urban.seoul.go.kr/view/html/PMNU4030100001?noticeCode={noticeCode}`

## 환경변수 (.env)

필수:
- `JUSO_API_KEY`: juso.go.kr API 키 (IP 제한: * 로 변경 완료)
- `VWORLD_API_KEY`: VWORLD 오픈 API 키
- `SEOUL_API_KEY`: 서울 열린데이터광장 API 키
- `ANTHROPIC_API_KEY`: Claude API 키

선택:
- `VWORLD_DOMAIN`: VWORLD API domain 파라미터 (기본: localhost)
- `PORT`: 서버 포트 (기본: 8000)

## 배포

### 로컬
```
python main.py --no-scheduler
```

### Oracle Cloud (운영 서버)
- **서울 리전**, VM.Standard.E2.1.Micro (1GB RAM + Swap 2GB, Always Free)
- IP: `168.107.53.76`
- SSH: `ssh -i ~/.ssh/oracle_cloud ubuntu@168.107.53.76`
- 프로젝트 경로: `/opt/dup-monitor`
- systemd 서비스: `dup-monitor.service`
- Nginx 리버스 프록시 (port 80 → 8000)
- 접속: `http://168.107.53.76`

**서버 업데이트 방법:**
```
# 로컬에서 코드 수정 후
git push
# 서버 반영 (PowerShell 한 줄)
ssh -i ~/.ssh/oracle_cloud ubuntu@168.107.53.76 "cd /opt/dup-monitor && git pull && sudo systemctl restart dup-monitor"
```

**서버 메모리 보호:**
- 시보 PDF 2단계 처리: Phase A(페이지 스캔, 번호만 기록) → Phase B(관련 페이지만 소형 PDF로 분리 → 텍스트 추출)
- Linux: subprocess 격리 (500MB 제한, OOM 시 자식만 종료, 180초 타임아웃)
- Windows: subprocess spawn 오버헤드 회피 → 직접 호출 (개발 환경용)
- 세마포어로 동시 PDF 분석 1건 제한
- 텍스트/분석 결과 캐시: 동일 시보+구역 재요청 시 PDF 미접근

**서버 스펙 업그레이드 권장:**
- 현재: VM.Standard.E2.1.Micro (1GB RAM + 2GB Swap, Always Free)
- **권장: VM.Standard.A1.Flex (ARM, 1 OCPU + 4GB RAM)** — Oracle Always Free 범위 내 무료
  - Always Free 한도: A1.Flex 최대 4 OCPU + 24GB RAM
  - 1 OCPU + 4GB 구성이면 PDF 처리에 충분
- 대안: 현재 서버에서 Swap 2GB → 4GB 확대 (`sudo fallocate -l 4G /swapfile`)

### Render (백업 / 한국 API 제한)
- Render 서버(미국)에서 VWORLD API 호출 불가 (Connection aborted / 502)
- 고시공고 + AI 분석만 작동, 용도지역/지목/면적 미표시
- 매 배포마다 DB 초기화 (ephemeral filesystem)

## 주의사항

- VWORLD API `domain` 파라미터: `VWORLD_DOMAIN` 환경변수로 설정 (vworld.py, address.py 모두)
- `routes.py`에서 `address_to_pnu()` 호출 시 반드시 `vworld_domain=settings.vworld_domain` 전달
- 로딩 오버레이: CSS `display: none` 기본, `.active` 클래스로 `display: flex` 전환. 폼 즉시 제출 + 서버 응답 시 자동 전환 (preventDefault 없음)
- 자동완성: 항목 선택 시 주소만 채움, 폼 자동 제출 안 함
- 한국 정부 API (VWORLD, juso.go.kr): 해외 서버에서 호출 불가 → 한국 리전 서버 필수
- 시보 PDF 분석: 2단계 처리(스캔→분리→추출) + subprocess 격리 + 텍스트/분석 캐시 (gazette_pdf.py)
- AI 분석 엔드포인트: POST 방식 (`/api/analyze-gazette`), 프론트엔드에서 JSON body로 요청 (zone_name, gazette_ref, ann_title, ann_cn, upis_content, content_quality, pdf_urls)
- AI 분석 결과에 `_gazette_source` 필드 포함 (사용된 소스 표시: "고시 상세", "UPIS 상세", "첨부 PDF", "시보 PDF" 등)
- Windows 로컬 개발: multiprocessing은 spawn 방식이라 subprocess 대신 직접 호출됨 (gazette_pdf.py의 sys.platform 분기)
- 시보 PDF 캐시 무효화: 추출 로직 변경 시 `data/pdfs/sibo*_text.txt`와 `*_analysis.json` 삭제 필요
