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
│   ├── urban_seoul.py      # urban.seoul.go.kr ArcGIS (UPIS 구역명)
│   ├── announcements.py    # 고시공고 검색 + 벌크 임포트 + Claude 분석
│   ├── gazette_pdf.py      # 서울시보 PDF 다운로드 + Claude 분석
│   └── tojieum.py          # 토지이음 (Playwright 필요)
├── frontend/
│   ├── templates/          # Jinja2 (index.html, result.html)
│   └── static/style.css
├── scrapers/               # 백그라운드 스크래퍼 (4개 소스)
├── scheduler/jobs.py       # APScheduler 작업 정의
├── pdf/                    # PDF 다운로드/추출/Claude 분석
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
   - UPIS 구역명 (urban.seoul.go.kr)
   - 토지이음 (Playwright 필요)
3. 구역명 → 고시공고 DB 검색
4. AI 분석: 비동기 AJAX (/api/analyze-gazette)

## 환경변수 (.env)

필수:
- `JUSO_API_KEY`: juso.go.kr API 키 (IP 제한 주의)
- `VWORLD_API_KEY`: VWORLD 오픈 API 키
- `SEOUL_API_KEY`: 서울 열린데이터광장 API 키
- `ANTHROPIC_API_KEY`: Claude API 키

선택:
- `VWORLD_DOMAIN`: VWORLD API domain 파라미터 (기본: localhost)
- `PORT`: 서버 포트 (기본: 8000)

## 배포

- **로컬**: `python main.py --no-scheduler`
- **Render**: GitHub push → Manual Deploy, `runtime.txt`로 Python 3.11 고정
- **Oracle Cloud**: `deploy/` 폴더 참조 (현재 미사용)

## 주의사항

- VWORLD API `domain` 파라미터: `_VWORLD_DOMAIN` 환경변수로 설정 (vworld.py, address.py)
- 로딩 오버레이: CSS `display: none` 기본, `.active` 클래스로 `display: flex` 전환
- 자동완성: 항목 선택 시 주소만 채움, 폼 자동 제출 안 함
- Render free tier: 매 배포마다 DB 초기화, 15분 미사용 시 sleep
