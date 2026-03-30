# RFP AI Analyzer

AI 기반 제안서(RFP) 분석 및 제안 업무 자동화 플랫폼

## 주요 기능

- **RFP 분석**: PDF/DOCX 형식의 RFP 문서를 업로드하면 AI가 핵심 요구사항, 평가 기준, 일정 등을 자동 분석
- **제안서 생성**: 분석된 RFP 기반으로 AI가 맞춤형 제안서 초안 자동 생성
- **수주 전략**: 경쟁사 대비 차별화 전략, 가격 전략, 기술 전략 등 수주 전략 수립
- **낙찰 패턴 분석**: 과거 데이터 기반 낙찰 패턴 분석 및 성공 요인 도출
- **제안서 리뷰 & 점수**: AI가 제안서를 평가 기준별로 채점하고 개선점 제안
- **견적 산출**: RFP 요구사항 기반 견적 자동 산출
- **파이프라인**: 분석 → 전략 → 생성 → 리뷰 전 과정 자동화
- **버전 관리**: 제안서 버전별 저장 및 비교
- **팀 협업**: 실시간 WebSocket 기반 다중 사용자 협업 (섹션 배정, 상태 관리, 코멘트)
- **지식 베이스**: 과거 제안서, 회사 역량, 기술 자료 등 지식 축적 및 AI 추천
- **Export**: Markdown, DOCX, PPTX 형식으로 제안서 내보내기

## 기술 스택

| 구분 | 기술 |
|------|------|
| Backend | FastAPI (Python 3.12) |
| AI | Claude API (Anthropic) |
| Database | PostgreSQL 16 |
| Frontend | Vanilla JS + Custom CSS |
| Infra | Docker Compose |

## 빠른 시작

### 사전 요구사항

- Docker & Docker Compose
- Anthropic API Key

### 설치 및 실행

```bash
# 1. 저장소 클론
git clone https://github.com/hisosic/ai-rfp.git
cd ai-rfp

# 2. 환경 변수 설정
cp .env.example .env
# .env 파일에서 ANTHROPIC_API_KEY 설정

# 3. 실행
docker compose up -d

# 4. 접속
# http://localhost:9000
```

### 환경 변수

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `ANTHROPIC_API_KEY` | Anthropic API 키 (필수) | - |
| `POSTGRES_USER` | DB 사용자명 | `rfp` |
| `POSTGRES_PASSWORD` | DB 비밀번호 | `rfp1234` |
| `POSTGRES_DB` | DB 이름 | `rfpdb` |

## 프로젝트 구조

```
.
├── app.py              # FastAPI 메인 애플리케이션
├── db.py               # PostgreSQL 데이터베이스 모듈
├── templates/
│   └── index.html      # SPA 메인 페이지
├── static/
│   ├── css/style.css   # 스타일시트
│   └── js/app.js       # 프론트엔드 로직
├── docker-compose.yml  # Docker Compose 설정
├── Dockerfile          # 앱 컨테이너 빌드
├── requirements.txt    # Python 의존성
└── .env.example        # 환경 변수 템플릿
```

## API 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| GET | `/` | 메인 페이지 |
| GET | `/api/dashboard` | 대시보드 통계 |
| POST | `/api/upload-rfp` | RFP 문서 업로드 |
| POST | `/api/analyze-rfp` | RFP 분석 |
| POST | `/api/winning-pattern` | 낙찰 패턴 분석 |
| POST | `/api/generate-proposal` | 제안서 생성 |
| POST | `/api/review-proposal` | 제안서 리뷰 |
| POST | `/api/strategy` | 수주 전략 수립 |
| POST | `/api/estimate` | 견적 산출 |
| POST | `/api/score-proposal` | 제안서 AI 점수 |
| POST | `/api/pipeline/run` | 파이프라인 실행 |
| POST | `/api/export-proposal` | Markdown 내보내기 |
| POST | `/api/export-docx` | DOCX 내보내기 |
| POST | `/api/export-pptx` | PPTX 내보내기 |
| WS | `/ws/{username}` | 실시간 협업 WebSocket |

## License

MIT
