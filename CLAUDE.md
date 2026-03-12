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
2. **병렬 실행** (ThreadPoolExecutor 6개):
   - VWORLD Data API 11개 레이어 (용도지역, 지구단위 등)
   - VWORLD NED API (지목, 면적)
   - VWORLD 구역명 전용 레이어 3개
   - VWORLD WFS API 4개 레이어
   - UPIS 구역명 + 연혁 + 도면 (urban.seoul.go.kr)
   - 토지이음 (Playwright 필요)
3. 구역명 → 고시공고 DB 검색 (title + zone_name만 검색, raw_content 제외)
4. AI 분석: 비동기 AJAX (/api/analyze-gazette) — 3단계 폴백

## AI 분석 파이프라인 (/api/analyze-gazette)

3단계 폴백:
1. **시보 PDF** (subprocess 격리, 300MB 메모리 제한, 120초 타임아웃)
2. **고시공고 DB content** (raw_content 10자 이상)
3. **UPIS 고시 content** (upis_content 10자 이상)

추출 항목 (pdf/claude_analyzer.py):
1. 건폐율
2. 기준용적률 / 허용용적률 / 상한용적률
3. 용적률 완화 조건
4. 높이제한 (수치 + 상세)
5. 허용/불허 용도
6. 용도별 비율
7. 건축/개발 제한사항
8. 기타사항 (조경, 주차, 기부채납 등)

## 고시공고 매칭 로직

검색 우선순위 (폴백 계층):
1. **구체적 구역명** (UPIS/VWORLD/토지이음에서 가져온 구역명)
2. **동 이름** (emdNm) — 1번 결과 없을 때만
3. **자치구** (sggNm) — 2번도 없을 때만

DB 검색: `title`과 `zone_name` 필드만 LIKE 검색 (raw_content 제외 — 오매칭 방지)

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
- 1GB RAM + 2GB Swap
- 시보 PDF(100MB+) 처리: subprocess 격리 (300MB 제한, OOM 시 자식만 종료)
- PyMuPDF(fitz) 페이지 단위 스트리밍 + gc.collect()

### Render (백업 / 한국 API 제한)
- Render 서버(미국)에서 VWORLD API 호출 불가 (Connection aborted / 502)
- 고시공고 + AI 분석만 작동, 용도지역/지목/면적 미표시
- 매 배포마다 DB 초기화 (ephemeral filesystem)

## 주의사항

- VWORLD API `domain` 파라미터: `VWORLD_DOMAIN` 환경변수로 설정 (vworld.py, address.py 모두)
- `routes.py`에서 `address_to_pnu()` 호출 시 반드시 `vworld_domain=settings.vworld_domain` 전달
- 로딩 오버레이: CSS `display: none` 기본, `.active` 클래스로 `display: flex` 전환
- 자동완성: 항목 선택 시 주소만 채움, 폼 자동 제출 안 함
- 한국 정부 API (VWORLD, juso.go.kr): 해외 서버에서 호출 불가 → 한국 리전 서버 필수
- 시보 PDF 분석: subprocess로 격리, 메인 서버 보호 (gazette_pdf.py `_extract_in_subprocess`)
