import os
import asyncio
import json
import re
import uuid
import time
import pdfplumber
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta

from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="RFP AI Analyzer", version="2.0.0")

_allowed = os.environ.get("ALLOWED_ORIGINS", "").strip()
_cors_origins = [o.strip() for o in _allowed.split(",") if o.strip()] if _allowed else []
if _cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=_cors_origins, allow_methods=["*"], allow_headers=["*"], allow_credentials=True)

_session_secret = os.environ.get("SESSION_SECRET", "rfp-dev-secret-change-me")
_session_https_only = os.environ.get("SESSION_HTTPS_ONLY", "1") == "1"
app.add_middleware(SessionMiddleware, secret_key=_session_secret, same_site="lax", https_only=_session_https_only, max_age=60*60*24*30)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

import db


# ─── Auth helpers ───

def current_user(request: Request):
    uid = request.session.get("uid")
    if not uid:
        return None
    return db.get_user_by_id(uid)


def require_user(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "로그인이 필요합니다.")
    return user


def require_admin(request: Request):
    user = require_user(request)
    if not user.get("is_admin"):
        raise HTTPException(403, "관리자 권한이 필요합니다.")
    return user


def check_rfp_access(request: Request, rfp_id: str):
    """Verify the current user can access this RFP. Returns user dict."""
    user = require_user(request)
    if user.get("is_admin"):
        return user
    owner = db.rfp_owner(rfp_id)
    if owner is not None and owner != user["id"]:
        raise HTTPException(403, "해당 RFP에 접근 권한이 없습니다.")
    return user

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)


class ConnectionManager:
    def __init__(self):
        self.active: list[dict] = []  # [{ws, username}]

    async def connect(self, ws: WebSocket, username: str):
        await ws.accept()
        self.active.append({"ws": ws, "username": username})
        await self.broadcast_users()

    def disconnect(self, ws: WebSocket):
        self.active = [c for c in self.active if c["ws"] != ws]

    async def broadcast_users(self):
        users = list({c["username"] for c in self.active})
        await self.broadcast({"type": "users", "users": users, "count": len(users)})

    async def broadcast(self, data: dict):
        dead = []
        for c in self.active:
            try:
                await c["ws"].send_json(data)
            except Exception:
                dead.append(c)
        for c in dead:
            self.active.remove(c)

    async def notify(self, action: str, detail: str, username: str = ""):
        await self.broadcast({
            "type": "activity",
            "action": action,
            "detail": detail,
            "username": username,
            "time": datetime.now().strftime("%H:%M:%S"),
        })


ws_manager = ConnectionManager()


def log_activity(action: str, detail: str = ""):
    db.insert_activity(datetime.now().strftime("%H:%M:%S"), action, detail)


def record_history(rfp_id: str, step: str, result: str):
    if not rfp_id:
        return
    ver = db.count_history_step(rfp_id, step) + 1
    db.insert_history(rfp_id, step, ver, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), result)


def get_anthropic_client():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if ANTHROPIC_AVAILABLE and api_key:
        return anthropic.Anthropic(api_key=api_key)
    return None


def call_ai(system_prompt: str, user_prompt: str, mock_type: str = "") -> str:
    client = get_anthropic_client()
    if client:
        try:
            message = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=8192,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return message.content[0].text
        except Exception as e:
            return f"[AI API 오류: {e}]\n\n" + generate_mock_response(mock_type)
    return generate_mock_response(mock_type)


def generate_mock_response(mock_type: str) -> str:
    if mock_type == "analyze":
        return json.dumps({
            "summary": "RFP 문서 자동 분석 결과",
            "requirements": [
                {"id": "REQ-001", "category": "기능", "description": "시스템 로그인 및 사용자 인증 기능", "priority": "필수", "risk": "낮음"},
                {"id": "REQ-002", "category": "기능", "description": "데이터 실시간 대시보드 구현", "priority": "필수", "risk": "중간"},
                {"id": "REQ-003", "category": "성능", "description": "동시 사용자 1,000명 이상 지원", "priority": "필수", "risk": "높음"},
                {"id": "REQ-004", "category": "보안", "description": "개인정보 암호화 저장 및 전송", "priority": "필수", "risk": "높음"},
                {"id": "REQ-005", "category": "UI/UX", "description": "반응형 웹 디자인 적용", "priority": "선택", "risk": "낮음"},
                {"id": "REQ-006", "category": "연동", "description": "기존 ERP 시스템과 API 연동", "priority": "필수", "risk": "높음"},
                {"id": "REQ-007", "category": "운영", "description": "시스템 운영 매뉴얼 및 교육 제공", "priority": "선택", "risk": "낮음"},
            ],
            "evaluation_criteria": [
                {"criteria": "기술 이해도", "weight": "30%", "description": "RFP 요구사항에 대한 기술적 이해와 해결 방안"},
                {"criteria": "수행 경험", "weight": "25%", "description": "유사 프로젝트 수행 실적 및 레퍼런스"},
                {"criteria": "가격 적정성", "weight": "20%", "description": "제안 금액의 적정성 및 비용 효율성"},
                {"criteria": "프로젝트 관리", "weight": "15%", "description": "일정 관리, 리스크 관리 방안"},
                {"criteria": "기술 지원", "weight": "10%", "description": "유지보수 및 기술 지원 계획"},
            ],
            "risks": [
                {"risk": "일정 지연 리스크", "level": "높음", "description": "ERP 연동 복잡도로 인한 일정 초과 가능성"},
                {"risk": "성능 리스크", "level": "중간", "description": "대규모 동시 접속 처리 시 병목 가능성"},
                {"risk": "보안 리스크", "level": "높음", "description": "개인정보 관련 규제 준수 필요"},
            ],
        }, ensure_ascii=False, indent=2)
    elif mock_type == "pattern":
        return json.dumps({
            "industry_analysis": "한국 공공/대기업 디지털 전환 시장은 연 12% 성장 중이며, 2024년 이후 MSA·클라우드 전환·데이터 통합 영역의 발주가 집중되고 있습니다. 평가는 기술이해도(30%), 수행경험(25%), 가격(20%) 비중이 지배적이며 보안 통제 항목이 매년 강화되는 추세입니다.",
            "customer_profile": {
                "type": "공공기관 (광역 지자체/공기업)",
                "decision_factors": ["기술 신뢰성", "유사 사업 수행 경험", "가격 적정성", "유지보수 체계"],
                "evaluation_weight_typical": {"기술 이해도": "30%", "수행 경험": "25%", "가격 적정성": "20%", "프로젝트 관리": "15%", "기술 지원": "10%"},
                "buying_signals": ["사전 영업 미팅 빈도 (3회 이상이면 우호적)", "RFP 내 특정 솔루션 키워드 언급", "기술 사전 데모 요청"],
                "common_objections": ["초기 도입 비용 부담", "내부 인력의 전환 학습 부담", "기존 시스템과의 호환성 우려"],
            },
            "winning_patterns": [
                {"pattern": "검증된 안정성 강조", "description": "동일 영역 다수 사업 수행 실적 + 정량 성과(가용성/만족도)를 전면 배치", "confidence": "높음", "example": "A공사 통합관제 수주 시 12건 레퍼런스 + 가용성 99.97% 강조로 기술평가 1위"},
                {"pattern": "단계적 전환 (Phased Approach)", "description": "Big Bang 대신 3단계 점진 전환으로 운영 리스크 최소화", "confidence": "높음", "example": "B금융 코어뱅킹 MSA 전환 시 무중단 단계적 전환으로 채택"},
                {"pattern": "TCO 절감 수치화", "description": "직접 견적 외 3년 누적 운영비 절감 효과를 정량 제시", "confidence": "높음", "example": "11.4억 TCO 절감 자료가 가격 평가 가산"},
                {"pattern": "특급 인력 풀투입", "description": "PM/Tech Lead 특급 인력 풀타임 명시", "confidence": "중간", "example": "공공기관 평가위원이 특히 선호"},
                {"pattern": "Win Theme 3개 부각", "description": "제안서 전반에 일관된 3개 핵심 메시지 반복", "confidence": "중간", "example": "Shipley 방법론 적용 시 일관성 향상"},
                {"pattern": "리스크 선제 대응", "description": "리스크 등록부 5개 이상 + 정량 대응 방안 제시", "confidence": "중간", "example": "프로젝트 관리 평가 가산 효과"},
                {"pattern": "사전 PoC 제공", "description": "제안 단계에서 핵심 기능 PoC 시연/영상 제공", "confidence": "높음", "example": "C기관 데이터 통합 사업 수주의 결정적 요인"},
            ],
            "win_themes": [
                {"theme": "검증된 안정성, 측정된 가치", "supporting_message": "동일 영역 12건 무중단 운영 — 가용성 99.95% 검증된 시스템", "evidence_required": "최근 3년 레퍼런스 표, 정량 성과"},
                {"theme": "단계적 위험 최소화 (Zero Downtime)", "supporting_message": "Big Bang이 아닌 3단계 점진 전환으로 운영 중단 Zero", "evidence_required": "Phase별 일정·검수 계획, 유사 사례 무중단 전환 영상"},
                {"theme": "총소유비용(TCO) 절감", "supporting_message": "Auto-Scaling으로 인프라 30%, 운영 인력 25% 절감", "evidence_required": "TCO 3년 시뮬레이션 자료"},
            ],
            "discriminators": [
                {"discriminator": "공공/금융 동일 분야 12건 수행", "proof_point": "최근 3년 누적 240억원 / 평균 만족도 4.7/5.0", "competitor_gap": "경쟁사 대비 2~3배 수행 실적"},
                {"discriminator": "자체 보유 통합 모니터링 자산", "proof_point": "200+ 운영 지표 자동 수집·알람 체계 (자체 개발 + Datadog)", "competitor_gap": "Day 1부터 운영 가시화 가능"},
                {"discriminator": "특급 PMP 인력 3명 풀타임 투입 보장", "proof_point": "PMP/정보관리기술사 보유, 평균 경력 18년", "competitor_gap": "통상 1~2명 수준의 경쟁사 대비 우위"},
                {"discriminator": "Shipley 방법론 기반 제안 표준화", "proof_point": "APMP 인증 인력 5명 보유", "competitor_gap": "제안 일관성/추적성 우수"},
            ],
            "proof_points": [
                {"category": "수행 실적", "claim": "동일 영역 12건 수행", "evidence": "최근 3년 누적 240억원, 평균 만족도 4.7/5.0"},
                {"category": "기술 역량", "claim": "정보관리기술사 3명 보유", "evidence": "사내 인력 명부 검증, 자격증 사본 제출 가능"},
                {"category": "운영 안정성", "claim": "가용성 99.95% 보장", "evidence": "최근 12건 평균 SLA 달성률 99.97%"},
                {"category": "고객 만족", "claim": "5년 연속 NPS 90+", "evidence": "외부 컨설팅 만족도 조사 결과"},
                {"category": "보안", "claim": "ISO 27001 + ISMS-P 인증", "evidence": "인증서 사본 제출 가능"},
                {"category": "방법론", "claim": "Shipley/APMP 인증 5명", "evidence": "사내 자격 보유자 명단"},
            ],
            "ghost_team": [
                {"competitor": "경쟁사 A (대형 SI)", "strengths": ["대형 레퍼런스 다수", "안정적 자금력"], "weaknesses": ["특정 기술 영역 외주 의존", "프로젝트 관리 표준화 약점"], "likely_positioning": "안정성 + 글로벌 레퍼런스 + 중상위 가격", "counter_strategy": "특급 인력 풀투입 보장 + 자체 모니터링 자산 차별화"},
                {"competitor": "경쟁사 B (전문 SI)", "strengths": ["가격 경쟁력", "민첩한 의사결정"], "weaknesses": ["수행 실적 부족", "유지보수 체계 약함"], "likely_positioning": "최저가 + 신속 납기", "counter_strategy": "TCO 절감 수치화 + 검증된 안정성 강조"},
                {"competitor": "경쟁사 C (외산 솔루션 파트너)", "strengths": ["솔루션 기술력"], "weaknesses": ["커스터마이징 한계", "한국 시장 이해 부족"], "likely_positioning": "솔루션 중심 + 고가", "counter_strategy": "한국형 커스터마이징 + 한국 레퍼런스 부각"},
            ],
            "evaluator_personas": [
                {"role": "기술 평가위원 (CIO/정보화책임관)", "background": "발주처 정보화 총괄. 신기술 도입 결정 권한", "key_concerns": ["기술 적합성", "안정성", "확장성"], "expected_questions": ["MSA 전환 시 운영 부담은?", "기존 ERP 연동 안정성은?"], "key_message_to_deliver": "검증된 12건의 무중단 전환 경험으로 위험을 최소화합니다"},
                {"role": "사업 평가위원 (구매/예산 담당)", "background": "예산 관리 + 계약 검토", "key_concerns": ["가격 적정성", "예산 일치", "리스크"], "expected_questions": ["시장 평균 대비 가격은?", "예산 초과 리스크는?"], "key_message_to_deliver": "시장 평균 -8%, TCO 3년 11.4억 절감으로 Value for Money 최고"},
                {"role": "사용자 대표 (현업 부서장)", "background": "실제 사용자 의견 대표", "key_concerns": ["사용 편의성", "운영 부담", "교육"], "expected_questions": ["기존 시스템 익숙한데 학습 부담은?"], "key_message_to_deliver": "동일 화면/조작 체계 유지 + 40시간 무상 교육 제공"},
            ],
            "price_to_win": {
                "market_average_range": "5억 8,000만원 ~ 6억 5,000만원",
                "recommended_position": "5억 6,000만원 (시장 평균 -8%)",
                "rationale": "공공 사업 가격 평가 가중치 20%, 최저가 입찰사 대비 5% 이내 차이 권장. -8%면 가격 평가 가산 + 마진 유지 가능",
                "price_weight": "20%",
                "low_price_threshold": "5억 2,000만원 (이하 시 적정성 의심 우려)",
                "high_price_threshold": "6억 8,000만원 (이상 시 가격 평가 1점 이상 손실)",
            },
            "style_recommendations": [
                "공공기관: 안정성·준법성·보안 인증 강조 (ISO 27001, ISMS-P, K-ISMS 등)",
                "능동 표현 + 정량 수치 우선 ('우수한' → '99.95% 검증된')",
                "Win Theme 3개를 제안서 전체에 일관되게 반복",
                "RFP 원문 어휘를 의도적으로 재인용하여 'RFP 이해도 높음' 인식",
                "표/리스트 활용으로 평가위원의 가독성 향상",
            ],
            "differentiation_tips": [
                "사전 영업 단계에서 핵심 기능 PoC 영상 전달 (수주의 50% 결정 요인)",
                "유사 프로젝트 고객 인터뷰 영상 또는 인용 자료 첨부",
                "제안서 첨부물에 자격증 사본·인증서 사본 일괄 제출",
                "발주처 명을 매 섹션 첫 줄에 의도적으로 호명",
                "경쟁사 대비 우위 비교 표를 가시적으로 배치",
            ],
            "risk_scenarios": [
                {"scenario": "기술 평가 열위", "probability": "중", "impact": "치명", "mitigation": "PoC 자료 사전 전달 + 시연 영상 + 기술 백서 첨부"},
                {"scenario": "최저가 경쟁사 출현", "probability": "상", "impact": "중", "mitigation": "TCO 3년 절감 자료 + Value for Money 표지 디자인"},
                {"scenario": "핵심 인력 의구심", "probability": "중", "impact": "중", "mitigation": "이력서 + 자격증 사본 첨부 + 인터뷰 사전 안내"},
            ],
            "action_plan": [
                {"action": "사전 영업 미팅 3회 추진", "owner": "영업 PL", "due": "제안 마감 D-21"},
                {"action": "PoC 데모 환경 구축 및 영상 제작", "owner": "Tech Lead", "due": "D-14"},
                {"action": "유사 프로젝트 고객 인용 자료 수집", "owner": "마케팅", "due": "D-10"},
                {"action": "TCO 3년 시뮬레이션 자료 작성", "owner": "재무 PL", "due": "D-7"},
                {"action": "제안서 내부 모의 평가 (Red Team Review)", "owner": "제안 PM", "due": "D-3"},
            ],
        }, ensure_ascii=False, indent=2)
    elif mock_type == "proposal":
        return json.dumps({
            "title": "디지털 전환 통합 플랫폼 구축 사업 제안서",
            "executive_summary": "본 제안은 귀 기관의 레거시 환경 한계를 극복하기 위해, 클라우드 네이티브 기반 마이크로서비스 아키텍처와 데이터 통합 허브를 결합한 차세대 통합 플랫폼을 6개월 내 단계적으로 구축하는 방안입니다. 당사는 동일 영역에서 12건의 수행 실적, 평균 가용성 99.95%, 운영비 30% 절감의 정량 성과를 보유하고 있어, 본 사업을 안정적·효율적으로 완수할 최적 파트너임을 확신합니다.",
            "win_themes": [
                {"theme": "검증된 안정성", "message": "동일 규모 12건 무중단 운영 — 가용성 99.95% 달성"},
                {"theme": "단계적 위험 최소화", "message": "Big Bang이 아닌 3단계 점진 전환으로 운영 중단 Zero"},
                {"theme": "총소유비용(TCO) 절감", "message": "Auto-Scaling으로 인프라 비용 30%, 운영 인력 25% 절감"},
            ],
            "discriminators": [
                {"point": "공공/금융 동일 분야 12건 수행", "proof": "최근 3년 누적 240억원 규모, 평균 만족도 4.7/5.0"},
                {"point": "자체 보유 통합 모니터링 자산", "proof": "200+ 운영 지표 자동 수집/알람 (Datadog/Prometheus 연계)"},
                {"point": "특급 PMP 인력 3명 풀타임 투입", "proof": "PMP/정보관리기술사 보유, 평균 경력 18년"},
            ],
            "evaluation_mapping": [
                {"criteria": "기술 이해도", "weight": "30%", "section_ref": "3. 제안 솔루션", "key_message": "RFP 요구사항 7개 전건 충족, 아키텍처 도식 및 기술 선정 근거 명시"},
                {"criteria": "수행 경험", "weight": "25%", "section_ref": "5. 수행 실적 및 투입 인력", "key_message": "동일 영역 12건 수행, 정량 성과 제시"},
                {"criteria": "가격 적정성", "weight": "20%", "section_ref": "7. 투자 비용 개요", "key_message": "시장 평균 대비 8% 절감, Value for Money 강조"},
                {"criteria": "프로젝트 관리", "weight": "15%", "section_ref": "4. 수행 방안", "key_message": "PMBOK 기반 WBS, 리스크 등록부, 변경관리 프로세스"},
                {"criteria": "기술 지원", "weight": "10%", "section_ref": "6. 유지보수 및 기술 지원", "key_message": "무상 1년 + 유상 SLA 99.9%, 24/7 콜센터"},
            ],
            "table_of_contents": [
                "1. 제안 개요", "  1.1 제안 배경 및 목적", "  1.2 프로젝트 범위 및 기대효과", "  1.3 본 제안의 차별점",
                "2. 현황 분석", "  2.1 고객 환경 분석 (AS-IS)", "  2.2 개선 방향 (TO-BE)", "  2.3 Gap 분석 및 핵심 과제",
                "3. 제안 솔루션", "  3.1 전체 시스템 아키텍처", "  3.2 핵심 기능 상세", "  3.3 기술 스택 및 선정 근거", "  3.4 데이터 및 시스템 연계 방안",
                "4. 수행 방안", "  4.1 프로젝트 추진 체계", "  4.2 단계별 일정 계획 (WBS)", "  4.3 품질 관리 방안", "  4.4 리스크 관리 방안", "  4.5 변경/형상/이슈 관리",
                "5. 수행 실적 및 투입 인력", "  5.1 유사 프로젝트 레퍼런스", "  5.2 핵심 투입 인력 프로필",
                "6. 유지보수 및 기술 지원", "  6.1 하자보수 범위 및 기간", "  6.2 SLA 및 기술 지원 체계",
                "7. 투자 비용 개요", "  7.1 비용 구성", "  7.2 Value for Money",
            ],
            "sections": {
                "1. 제안 개요": "1.1 제안 배경 및 목적\n귀 기관은 사용자 증가(연 15%) 및 외부 연계 시스템 확장으로 기존 모놀리식 환경의 확장성·운영 비용·장애 대응 한계가 누적되고 있습니다. 본 제안은 클라우드 네이티브 마이크로서비스 아키텍처로의 단계적 전환을 통해, 시스템 안정성(가용성 99.95%), 운영 효율(인력 25% 절감), 신규 서비스 출시 속도(평균 3개월 → 4주)를 동시에 확보하는 것을 목적으로 합니다.\n\n1.2 프로젝트 범위 및 기대효과\n총 6개월간 12개 핵심 업무 모듈을 Kubernetes 기반 컨테이너 환경으로 전환하며, API Gateway·메시지 큐·통합 모니터링 체계를 구축합니다. 기대효과로는 ① 인프라 운영비 30% 절감, ② 장애 복구 시간(MTTR) 60% 단축, ③ 신규 기능 배포 주기 12배 단축이 예상됩니다.\n\n1.3 본 제안의 차별점\n① 동일 영역 12건 수행 — 평균 만족도 4.7/5.0  ② 특급 인력 3명 풀타임 투입(PMP/기술사)  ③ 자체 보유 통합 모니터링 자산으로 Day 1부터 200+ 지표 실시간 가시화.",
                "2. 현황 분석": "2.1 AS-IS — 현재 환경의 한계\n① 모놀리식 구조: 단일 배포 단위로 작은 변경에도 전체 빌드 30분+ 소요  ② Vertical Scaling 한계: 트래픽 피크 시 응답시간 5초+ 지연 발생  ③ 수동 운영: 장애 감지 평균 18분, 복구 평균 47분 (업계 평균 대비 2배)  ④ 사일로화된 데이터: 부서별 분산 DB로 통합 분석 불가\n\n2.2 TO-BE — 차세대 청사진\n• 마이크로서비스(Domain별 12개) + Kubernetes 자동 확장 → 트래픽 피크 시 응답시간 200ms 이내 유지\n• 통합 데이터 허브 + Event-Driven 연계 → 실시간 데이터 통합/분석\n• Observability 표준화(메트릭/로그/트레이스) → MTTR 60% 단축\n\n2.3 Gap 분석 및 핵심 과제\n우선순위 1) 핵심 거래 모듈 컨테이너화 + Blue/Green 배포  2) 데이터 통합 허브 구축 및 사일로 해소  3) 운영 자동화/모니터링 표준화. 본 사업은 위 3개 핵심 과제를 6개월 내 동시 해결합니다.",
                "3. 제안 솔루션": "3.1 전체 아키텍처\n5계층 구조: ① 사용자 채널(Web/App)  ② API Gateway(인증/Rate Limit/라우팅)  ③ 마이크로서비스(12개 도메인, Spring Boot 3 / Node.js 20)  ④ 데이터 플랫폼(PostgreSQL HA, Redis 캐시, Kafka 메시지 버스)  ⑤ Observability(Prometheus/Grafana/Loki/Tempo + Datadog APM).\n\n3.2 핵심 기능 (성능 지표 포함)\n• 통합 사용자 인증/SSO — TPS 3,000 / 응답 < 50ms\n• 실시간 데이터 통합 — Kafka 50,000 msg/sec, 지연 < 200ms\n• 자동 스케일링 정책 — CPU 70% / 메모리 75% 임계 시 30초 내 확장\n• 통합 모니터링 대시보드 — 200+ 지표 실시간, SLO 자동 알람\n• 무중단 배포 — Blue/Green + Canary, 평균 배포 시간 8분\n• 데이터 백업/복구 — RPO 5분 / RTO 15분\n\n3.3 기술 스택 선정 근거\n| 분류 | 스택 | 선정 사유 |\n|---|---|---|\n| Container Orchestration | Kubernetes 1.30 | CNCF Graduated, 12건 수행 검증 |\n| API Gateway | Kong Gateway | 플러그인 생태계, HA 검증 |\n| Service Mesh | Istio | mTLS/Observability 통합 |\n| Message Broker | Kafka 3.7 | 50,000 msg/sec 처리 검증 |\n| Database | PostgreSQL 16 HA | 표준 호환, 비용 효율 |\n| Observability | Datadog + OpenTelemetry | APM/Log/Trace 통합 |\n\n3.4 데이터 및 연계\n• 기존 ERP/HR/CRM 시스템과 REST/SOAP/SFTP 연계 표준 채택  • 데이터 무결성 보장: Saga 패턴 + Outbox 패턴 적용  • 외부 연동 30종, 평균 응답 200ms 이내 SLA 설계.",
                "4. 수행 방안": "4.1 추진 체계\n• 총괄 PM(특급, PMP) 1명 + Tech Lead(특급) 2명 + 도메인 개발자 8명 + QA 2명 + DevOps 2명, 총 15명 풀타임\n• 거버넌스: 주간 진척 회의(매주 수), 월간 운영위 보고, 분기 평가\n\n4.2 단계별 일정 (WBS 요약)\nPhase 1 (1~2개월) — 기반 구축: 인프라/K8s 구축, CI/CD 파이프라인, 첫 2개 서비스 마이그레이션. 산출물: 아키텍처설계서, 인프라 구축 완료보고서. 마일스톤: M1(K8s 클러스터 OK), M2(첫 서비스 Live)\nPhase 2 (3~5개월) — 핵심 개발: 나머지 10개 도메인 서비스 개발/마이그레이션, 데이터 허브 구축, 통합 테스트. 마일스톤: M3(8개 서비스 OK), M4(전 서비스 통합 OK)\nPhase 3 (6개월) — 안정화/이관: 부하/장애 테스트, 운영팀 인계, 무상 하자보수 시작. 마일스톤: M5(검수 완료)\n\n4.3 품질 관리\n• CMMI Level 3 기반 프로세스, 단위 테스트 커버리지 80%+ 강제\n• Static Analysis(SonarQube) + 보안 점검(OWASP Top 10) 자동화\n• 코드 리뷰 100%, 주간 품질 보고서\n\n4.4 리스크 관리 (Top 5)\n① ERP 연동 복잡도(High×High) → 사전 PoC 2주, 전문 파트너 합류\n② 인력 이탈(Medium×Medium) → 핵심 인력 인센티브, 백업 인력 2명 확보\n③ 일정 지연(Medium×High) → 버퍼 10% 확보, 주간 EVM 추적\n④ 데이터 마이그레이션 오류(Low×High) → Shadow Run 4주, Rollback 시나리오\n⑤ 운영 인계 미흡(Medium×Medium) → 인계 교육 40시간, 운영 매뉴얼 200P\n\n4.5 변경/형상/이슈 관리\nJira + Confluence 표준, 변경 요청은 CCB 승인 후 반영, Git Flow 표준 적용.",
                "5. 수행 실적 및 투입 인력": "5.1 유사 프로젝트 레퍼런스 (최근 3년 12건 중 대표 3건)\n① A공사 통합관제시스템 (2024.03~2024.12, 38억원, PM/15명) — 가용성 99.97%, 운영비 32% 절감, 만족도 4.8/5.0\n② B금융 차세대 코어뱅킹 MSA 전환 (2023.06~2024.05, 65억원, 25명) — TPS 2,800→9,500, 장애시간 75% 감소\n③ C기관 데이터 통합 플랫폼 (2023.01~2023.11, 22억원, 12명) — 30개 시스템 연계, 처리 지연 80% 감소\n\n5.2 핵심 투입 인력 (총 15명 중 5명 발췌)\n| 역할 | 등급 | 경력 | 자격 | 대표 이력 |\n|---|---|---|---|---|\n| PM | 특급 | 22년 | PMP, 정보관리기술사 | A공사 등 동일 영역 PM 8건 |\n| Tech Lead 1 | 특급 | 18년 | 정보처리기술사, CKA | MSA 전환 5건 리드 |\n| Tech Lead 2 | 특급 | 16년 | AWS SA Pro | 클라우드 전환 7건 |\n| DBA | 고급 | 14년 | OCP, PostgreSQL Certified | HA 구축 12건 |\n| 보안 PL | 고급 | 12년 | CISSP, CISA | 금융권 보안 인증 다수 |",
                "6. 유지보수 및 기술 지원": "6.1 하자보수\n• 무상 하자보수 12개월 (검수 완료일부터)\n• 범위: 인도된 모든 산출물의 버그 수정, 환경 변화 대응, 보안 패치\n• 응답 SLA: Critical 1시간 / High 4시간 / Medium 8 영업시간 / Low 3 영업일\n\n6.2 유상 SLA (선택, 연 단위 갱신)\n• 가용성 99.9% 보장 (월간 다운타임 43분 이내)\n• 24/7 콜센터 + 원격 지원, 분기별 정기 점검 4회 + 현장 출동 2회 (연)\n• 월간 KPI 리포트: 가용성/장애 건수/MTTR/티켓 처리율\n• 분기 운영 검토회 + 연 1회 기술 로드맵 협의\n\n6.3 추가 지원\n• 무상 교육 40시간 (운영팀/개발팀) + 매뉴얼 200P 인도\n• 핫라인 보안 사고 대응 1시간 이내 도착",
                "7. 투자 비용 개요": "7.1 비용 구성 (총액 5억 2,000만원, VAT 별도)\n• 인건비 76% (3억 9,600만원) — 15명 × 평균 6개월\n• SW 라이선스/클라우드 10% (5,400만원) — AWS, Datadog, Kong\n• HW/인프라 8% (4,000만원) — 개발/스테이징 환경\n• 기타 경비 6% (3,000만원) — 출장, 교육, 문서화\n\n7.2 Value for Money\n• 시장 평균(6억 1,000만원) 대비 8% 절감\n• 도입 후 3년 누적 운영비 절감 기대치: 11.4억원 (인프라 30% + 인력 25% 절감 환산)\n• 신규 서비스 출시 속도 12배 향상에 따른 기회 비용 회수 추가",
            },
            "references_summary": [
                {"customer": "A공사", "project": "통합관제시스템 구축", "period": "2024.03~2024.12", "scale": "38억원 / 15명", "outcome": "가용성 99.97%, 운영비 32% 절감"},
                {"customer": "B금융", "project": "코어뱅킹 MSA 전환", "period": "2023.06~2024.05", "scale": "65억원 / 25명", "outcome": "TPS 2,800→9,500, 장애시간 75% 감소"},
                {"customer": "C기관", "project": "데이터 통합 플랫폼", "period": "2023.01~2023.11", "scale": "22억원 / 12명", "outcome": "30개 시스템 연계, 지연 80% 감소"},
            ],
            "key_personnel": [
                {"role": "PM", "grade": "특급", "years": "22년", "certs": "PMP, 정보관리기술사", "highlight": "A공사 등 동일 영역 PM 8건"},
                {"role": "Tech Lead", "grade": "특급", "years": "18년", "certs": "정보처리기술사, CKA", "highlight": "MSA 전환 5건 리드"},
                {"role": "Cloud Architect", "grade": "특급", "years": "16년", "certs": "AWS SA Pro", "highlight": "클라우드 전환 7건"},
                {"role": "DBA", "grade": "고급", "years": "14년", "certs": "OCP, PostgreSQL Certified", "highlight": "HA 구축 12건"},
                {"role": "보안 PL", "grade": "고급", "years": "12년", "certs": "CISSP, CISA", "highlight": "금융권 보안 인증 다수"},
            ],
            "risk_register": [
                {"risk": "ERP 연동 복잡도", "impact": "상", "likelihood": "상", "mitigation": "사전 PoC 2주, 전문 파트너 합류"},
                {"risk": "핵심 인력 이탈", "impact": "중", "likelihood": "중", "mitigation": "인센티브 + 백업 인력 2명 확보"},
                {"risk": "일정 지연", "impact": "상", "likelihood": "중", "mitigation": "버퍼 10%, 주간 EVM 추적"},
                {"risk": "데이터 마이그레이션 오류", "impact": "상", "likelihood": "하", "mitigation": "Shadow Run 4주, Rollback 시나리오"},
                {"risk": "운영 인계 미흡", "impact": "중", "likelihood": "중", "mitigation": "인계 교육 40시간, 매뉴얼 200P"},
            ],
        }, ensure_ascii=False, indent=2)
    elif mock_type == "review":
        return json.dumps({
            "overall_score": 78,
            "weighted_score": 76.8,
            "grade": "B+",
            "win_probability": "65%",
            "summary": "RFP 요구사항 7건 중 6건 충족·구체적 일정/인력 강점. 단, ERP 연동·DR 방안 누락 + 차별화 부족이 주요 약점.",
            "review_items": [
                {"category": "RFP 요구사항 충족도", "score": 85, "max": 100, "status": "양호", "comment": "7건 중 6건 충족, REQ-006 ERP 연동 상세 방안 누락"},
                {"category": "기술적 타당성", "score": 80, "max": 100, "status": "양호", "comment": "MSA·Kubernetes 선택 적절, 보안 통제 항목 일부 누락"},
                {"category": "수행 방안의 명확성", "score": 75, "max": 100, "status": "보통", "comment": "WBS 상세하나 크리티컬 패스 식별 부재"},
                {"category": "차별화 요소", "score": 65, "max": 100, "status": "보완필요", "comment": "경쟁사 대비 차별점 산만, Win Theme 3개 부각 필요"},
                {"category": "수행 실적/레퍼런스", "score": 82, "max": 100, "status": "양호", "comment": "정량 성과 제시 우수, 최근 3년 12건"},
                {"category": "투입 인력 적정성", "score": 78, "max": 100, "status": "양호", "comment": "핵심 인력 보유, 백업 인력 명시 보강 필요"},
                {"category": "가격 경쟁력", "score": 75, "max": 100, "status": "보통", "comment": "시장 대비 8% 절감, Value for Money 설명 추가 권장"},
                {"category": "유지보수/지원 체계", "score": 80, "max": 100, "status": "양호", "comment": "SLA 위반 페널티 명시 필요"},
                {"category": "문서 완성도", "score": 82, "max": 100, "status": "양호", "comment": "구성 체계적, 다이어그램 보강 권장"},
            ],
            "evaluator_perspectives": [
                {"persona": "기술 평가위원", "score": 78, "key_concern": "기술 스택 대안 비교표 부재", "key_strength": "아키텍처 5계층 구조 도식이 구체적"},
                {"persona": "사업 평가위원", "score": 74, "key_concern": "리스크 대응 비용 명시 부족, 변경관리 비용 누락 가능", "key_strength": "Phase별 일정 현실적"},
                {"persona": "운영 평가위원", "score": 81, "key_concern": "SLA 위반 시 페널티 조항 미명시", "key_strength": "24/7 콜센터 + 핫라인 1시간 대응"},
            ],
            "requirements_traceability": [
                {"req_id": "REQ-001", "title": "사용자 인증/SSO", "status": "충족", "section_ref": "3.2", "evidence": "TPS 3,000 / <50ms 성능 지표 명시"},
                {"req_id": "REQ-002", "title": "실시간 대시보드", "status": "부분충족", "section_ref": "3.2", "gap": "동시 사용자 1,000명 처리 검증 자료 미제시"},
                {"req_id": "REQ-003", "title": "동시 사용자 1,000명", "status": "부분충족", "section_ref": "3.1", "gap": "수평 확장 정책은 명시되나 부하 테스트 결과 누락"},
                {"req_id": "REQ-004", "title": "개인정보 암호화", "status": "충족", "section_ref": "3.2/3.3", "evidence": "전송/저장 모두 AES-256 명시"},
                {"req_id": "REQ-005", "title": "반응형 UI", "status": "충족", "section_ref": "3.2", "evidence": "Tailwind/CSS Grid 기반 명시"},
                {"req_id": "REQ-006", "title": "ERP 연동", "status": "누락", "section_ref": "-", "gap": "ERP 인터페이스 명세·전문 정의 누락 — 평가위원 1차 감점 가능"},
                {"req_id": "REQ-007", "title": "운영 매뉴얼/교육", "status": "충족", "section_ref": "6.3", "evidence": "40시간 교육 + 200P 매뉴얼"},
            ],
            "section_analysis": [
                {"section": "1. 제안 개요", "strengths": ["기대효과 정량 수치 제시", "차별점 3가지 명확"], "weaknesses": ["발주처 명시적 호명/Pain Point 직접 인용 부족"]},
                {"section": "3. 제안 솔루션", "strengths": ["5계층 아키텍처 도식", "기술 스택 선정 사유 표 형식"], "weaknesses": ["대안 비교 표 없음", "보안 통제(접근통제/감사로그) 항목 누락"]},
                {"section": "4. 수행 방안", "strengths": ["Phase별 WBS·산출물 상세", "리스크 등록부 5건"], "weaknesses": ["크리티컬 패스 식별 부재", "변경관리 비용 미산정"]},
                {"section": "5. 수행 실적 및 투입 인력", "strengths": ["정량 성과 제시", "핵심 인력 자격증 명시"], "weaknesses": ["백업 인력 가용성 명시 부족"]},
                {"section": "7. 투자 비용 개요", "strengths": ["시장 평균 비교"], "weaknesses": ["TCO 3년 절감 계산 산출 근거 미흡"]},
            ],
            "missing_requirements": [
                {"req_id": "REQ-006", "description": "ERP 연동 상세 인터페이스 명세 (전문/필드/주기)", "severity": "치명적"},
                {"req_id": "REQ-009", "description": "재해복구(DR) 방안 누락 - RPO/RTO/이중화 구조", "severity": "높음"},
                {"req_id": "REQ-011", "description": "데이터 마이그레이션 절차 및 검증 계획 미정의", "severity": "높음"},
            ],
            "logic_issues": [
                {"issue": "3.2절 '6개월 완료' vs 4.2절 '6개월 일정 + 안정화'  마감 정의 모호", "where": "3.2 ↔ 4.2", "fix": "검수일 vs 안정화 종료일 명확 구분"},
                {"issue": "투입 인력 15명 vs 유사 프로젝트 25명 사례 괴리", "where": "4.1 ↔ 5.1", "fix": "축소 운영 근거(자동화 도구 활용) 또는 외부 파트너 합류 명시"},
            ],
            "improvement_suggestions": [
                {"priority": "P0", "effort": "1일", "suggestion": "ERP 연동 아키텍처 다이어그램 + 인터페이스 표 추가", "impact": "치명적 누락 보완 → 점수 8점 상승 예상"},
                {"priority": "P0", "effort": "0.5일", "suggestion": "DR 방안 절 추가 (RPO 5분/RTO 15분/이중화 구조)", "impact": "리스크 평가 가산 가능"},
                {"priority": "P1", "effort": "1일", "suggestion": "Phase별 마일스톤·산출물·검수기준 표로 명확화", "impact": "프로젝트 관리 평가 5점 상승"},
                {"priority": "P1", "effort": "0.5일", "suggestion": "경쟁사 대비 기술 우위 비교 표 작성", "impact": "차별화 평가 강화"},
                {"priority": "P2", "effort": "1일", "suggestion": "고객 인터뷰 기반 Pain Point 해결 사례 추가", "impact": "공감 가능성 상승"},
            ],
            "wording_improvements": [
                {"before": "안정적인 시스템을 제공합니다", "after": "동일 규모 12건 무중단 운영 — 가용성 99.95% 검증된 시스템을 제공합니다", "why": "정량 수치로 신뢰도 강화"},
                {"before": "신속한 대응이 가능합니다", "after": "Critical 1시간 / High 4시간 SLA를 24/7 보장합니다", "why": "구체적 SLA 명시"},
                {"before": "우수한 기술력을 보유하고 있습니다", "after": "정보관리기술사 3명, PMP 5명, 클라우드 자격 12명 보유", "why": "자격증 수치로 객관화"},
            ],
            "competitor_gaps": [
                {"area": "가격", "our_position": "중상위(시장 평균 -8%)", "risk": "최저가 입찰사 대비 가격 평가 1~2점 열위 가능", "counter": "TCO 3년 11.4억 절감 효과로 Value for Money 강조"},
                {"area": "수행 실적", "our_position": "우위(12건)", "risk": "-", "counter": "정량 성과 표 전면 배치"},
                {"area": "기술", "our_position": "동등", "risk": "차별점 부족", "counter": "자체 모니터링 자산·통합 운영 노하우 부각"},
            ],
        }, ensure_ascii=False, indent=2)
    elif mock_type == "strategy":
        return json.dumps({
            "recommendation": "GO",
            "confidence": "75%",
            "analysis": {
                "market_fit": {"score": 80, "comment": "당사 핵심 역량과 높은 부합도"},
                "competition": {"score": 65, "comment": "3~4개 경쟁사 예상, 중상위 경쟁 강도"},
                "profitability": {"score": 70, "comment": "예상 마진율 15~20%, 양호"},
                "strategic_value": {"score": 85, "comment": "해당 산업군 레퍼런스 확보 시 후속 사업 기회"},
                "resource_availability": {"score": 75, "comment": "핵심 인력 확보 가능, 일부 외부 충원 필요"},
            },
            "key_factors": [
                "유사 프로젝트 3건 수행 실적 보유",
                "해당 고객사와 기존 유지보수 관계",
                "제안 마감까지 3주, 촉박한 일정",
                "핵심 PM 다른 프로젝트 투입 중",
                "요구 기술(SAP 연동) 내부 경험 부족",
            ],
            "win_strategy": [
                "기존 고객 관계 활용하여 사전 미팅 추진",
                "SAP 연동 전문 파트너사 확보",
                "PoC 제안으로 기술 역량 증명",
                "공격적 가격 전략 (초기 수익보다 레퍼런스 확보 우선)",
            ],
        }, ensure_ascii=False, indent=2)
    elif mock_type == "knowledge":
        return json.dumps({
            "saved": True,
            "message": "지식 자산이 성공적으로 저장되었습니다.",
            "categories": ["기술 제안", "프로젝트 관리", "보안/인증"],
            "reusable_count": 5,
        }, ensure_ascii=False, indent=2)
    elif mock_type == "estimate":
        return json.dumps({
            "project_name": "디지털 전환 통합 플랫폼 구축",
            "total_cost": "5억 6,160만원 (VAT 별도)",
            "total_cost_number": 561600000,
            "total_cost_with_vat": "6억 1,776만원 (VAT 포함)",
            "total_cost_with_vat_number": 617760000,
            "duration_months": 6,
            "summary": "KOSA 2024년 SW기술자 노임단가 기준 인건비 산정 + AWS 종량제 클라우드 인프라 임대 기준 6개월 견적. 시장 평균 대비 8% 절감 포지션.",
            "labor_rate_basis": {
                "source": "KOSA(한국소프트웨어산업협회) SW기술자 평균임금 2024년 공표",
                "rates": [
                    {"grade": "특급", "annual": "9,400만원/년", "monthly": "약 450만원/월 (실투입 기준)"},
                    {"grade": "고급", "annual": "8,000만원/년", "monthly": "약 380만원/월"},
                    {"grade": "중급", "annual": "6,500만원/년", "monthly": "약 310만원/월"},
                    {"grade": "초급", "annual": "5,000만원/년", "monthly": "약 240만원/월"},
                ],
            },
            "categories": [
                {"name": "인건비", "subtotal": "3억 9,600만원", "subtotal_number": 396000000, "ratio": "76%", "items": [
                    {"role": "PM", "grade": "특급", "count": 1, "months": 6, "unit_cost": "450만원", "cost": "2,700만원", "reason": "프로젝트 총괄 관리"},
                    {"role": "아키텍트", "grade": "특급", "count": 1, "months": 6, "unit_cost": "450만원", "cost": "2,700만원", "reason": "시스템 아키텍처 설계"},
                    {"role": "백엔드 개발", "grade": "고급", "count": 3, "months": 6, "unit_cost": "380만원", "cost": "6,840만원", "reason": "API 및 핵심 로직 개발"},
                    {"role": "프론트엔드 개발", "grade": "고급", "count": 2, "months": 6, "unit_cost": "380만원", "cost": "4,560만원", "reason": "UI/UX 구현"},
                    {"role": "QA", "grade": "중급", "count": 2, "months": 4, "unit_cost": "310만원", "cost": "2,480만원", "reason": "통합 테스트"},
                ]},
                {"name": "SW 라이선스", "subtotal": "5,400만원", "subtotal_number": 54000000, "ratio": "10%", "items": [
                    {"item": "클라우드 서비스 (AWS)", "cost": "3,600만원", "reason": "EKS, RDS, ElastiCache 등 운영 인프라"},
                    {"item": "모니터링 (Datadog)", "cost": "1,800만원", "reason": "APM, 로그 관리, 알림"},
                ]},
                {"name": "HW/인프라", "subtotal": "4,000만원", "subtotal_number": 40000000, "ratio": "8%", "items": [
                    {"item": "개발/스테이징 서버", "cost": "2,400만원", "reason": "개발 및 테스트 환경 구축"},
                    {"item": "네트워크/보안 장비", "cost": "1,600만원", "reason": "방화벽, VPN, SSL 인증서"},
                ]},
                {"name": "기타 경비", "subtotal": "3,000만원", "subtotal_number": 30000000, "ratio": "6%", "items": [
                    {"item": "프로젝트 관리비", "cost": "2,000만원", "reason": "회의, 출장, 문서화"},
                    {"item": "교육/인수인계", "cost": "1,000만원", "reason": "운영팀 교육 및 매뉴얼 작성"},
                ]},
            ],
            "phase_breakdown": [
                {"phase": "Phase 1: 기반 구축", "months": "1~2", "cost": "1억 4,000만원", "cost_number": 140000000, "deliverables": "아키텍처 설계서, 인프라 구축 완료보고서, 첫 2개 서비스 마이그레이션"},
                {"phase": "Phase 2: 핵심 개발", "months": "3~5", "cost": "2억 8,000만원", "cost_number": 280000000, "deliverables": "12개 서비스 개발/마이그레이션, 데이터 허브, 통합 테스트"},
                {"phase": "Phase 3: 안정화/이관", "months": "6", "cost": "1억 4,160만원", "cost_number": 141600000, "deliverables": "부하/장애 테스트, 운영 인계서, 매뉴얼"},
            ],
            "cost_structure": {
                "direct_cost": "4억 9,000만원",
                "direct_cost_number": 490000000,
                "general_admin": {"label": "일반관리비 (8%)", "amount": "3,920만원", "amount_number": 39200000},
                "profit": {"label": "이윤 (10%)", "amount": "5,292만원", "amount_number": 52920000},
                "contingency": {"label": "예비비 (5%)", "amount": "2,948만원", "amount_number": 29480000},
                "vat": {"label": "부가세 (10%)", "amount": "5,616만원", "amount_number": 56160000},
            },
            "pricing_options": [
                {"option": "Base (권장)", "total": "6억 1,776만원", "rationale": "표준 노임단가 + 정상 마진 10% + 시장 평균 -8%", "win_probability": "60%"},
                {"option": "Aggressive (저가)", "total": "5억 4,800만원", "rationale": "마진 5%, 예비비 축소. 레퍼런스 확보 우선 시", "win_probability": "75%"},
                {"option": "Premium (고가)", "total": "6억 8,400만원", "rationale": "특급 인력 풀투입 + 강화된 SLA(99.95%). 안정성 평가 가중치 높을 때", "win_probability": "45%"},
            ],
            "payment_milestones": [
                {"milestone": "착수금", "ratio": "20%", "amount": "1억 2,355만원", "trigger": "계약 체결 후 7일 이내"},
                {"milestone": "중간 검수", "ratio": "40%", "amount": "2억 4,710만원", "trigger": "Phase 2 완료 + 중간 보고서 승인"},
                {"milestone": "최종 검수", "ratio": "40%", "amount": "2억 4,710만원", "trigger": "최종 검수 합격 + 인계 완료"},
            ],
            "market_comparison": {
                "market_average": "6억 1,000만원",
                "market_average_number": 610000000,
                "our_total": "5억 6,160만원",
                "delta_pct": "-8%",
                "interpretation": "시장 평균 대비 8% 절감 — 가격 평가 1~2점 가산 기대. TCO 3년 11.4억 절감 효과 추가 강조 권장.",
            },
            "risks": [
                {"risk": "요구사항 변경 (Scope Creep)", "impact": "직접비 10~15% 증가 가능", "mitigation": "변경관리 프로세스 + CCB 승인 절차"},
                {"risk": "특급 인력 수급 지연", "impact": "1~2개월 일정 지연", "mitigation": "사전 인력 확정 + 백업 인력 2명 확보"},
                {"risk": "클라우드 환율 변동", "impact": "인프라 비용 5~8% 변동", "mitigation": "환율 헤지 또는 환차 보존 조항"},
            ],
            "assumptions": [
                "노임단가는 KOSA 2024년 공표 기준 적용",
                "클라우드 서비스는 종량제, 1년 약정 기준",
                "6개월 고정 기간 산정 (연장 시 추가 협의)",
                "출장/숙박 등 실비는 별도 정산",
                "부가세 별도, 결제는 마일스톤 기준",
            ],
            "notes": "본 견적은 RFP 기반 추정치이며 요구사항 확정 후 ±10% 조정 가능. 결제는 마일스톤 기준이며 사전 조정 가능합니다.",
        }, ensure_ascii=False, indent=2)
    return "분석이 완료되었습니다."


def clean_text(text: str) -> str:
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)


def extract_pdf_text(filepath: str) -> str:
    text = ""
    try:
        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
    except Exception as e:
        text = f"[PDF 파싱 오류: {e}]"
    return clean_text(text)


def extract_docx_text(filepath: str) -> str:
    try:
        from docx import Document as DocxDocument
        doc = DocxDocument(filepath)
        parts = [p.text for p in doc.paragraphs if p.text]
        for table in doc.tables:
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    parts.append(row_text)
        return clean_text("\n".join(parts))
    except Exception as e:
        return f"[DOCX 파싱 오류: {e}]"


def extract_document_text(filepath: str, filename: str) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        return extract_pdf_text(filepath)
    if name.endswith(".docx"):
        return extract_docx_text(filepath)
    if name.endswith(".txt") or name.endswith(".md"):
        try:
            return clean_text(Path(filepath).read_text(encoding="utf-8", errors="ignore"))
        except Exception as e:
            return f"[텍스트 읽기 오류: {e}]"
    return ""


# ─── Routes ───

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ─── Auth API ───

@app.post("/api/auth/register")
async def auth_register(request: Request, username: str = Form(...), password: str = Form(...)):
    username = username.strip()
    if len(username) < 2 or len(password) < 4:
        raise HTTPException(400, "아이디는 2자 이상, 비밀번호는 4자 이상이어야 합니다.")
    if db.get_user_by_username(username):
        raise HTTPException(409, "이미 존재하는 아이디입니다.")
    uid = db.create_user(username, password, is_admin=False)
    request.session["uid"] = uid
    user = db.get_user_by_id(uid)
    log_activity("회원가입", username)
    return JSONResponse({"user": user})


@app.post("/api/auth/login")
async def auth_login(request: Request, username: str = Form(...), password: str = Form(...)):
    user = db.verify_user(username.strip(), password)
    if not user:
        raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다.")
    request.session["uid"] = user["id"]
    log_activity("로그인", user["username"])
    return JSONResponse({"user": user})


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return JSONResponse({"ok": True})


@app.get("/api/auth/me")
async def auth_me(request: Request):
    user = current_user(request)
    return JSONResponse({"user": user})


# ─── Admin API ───

@app.get("/api/admin/users")
async def admin_list_users(request: Request):
    require_admin(request)
    return JSONResponse({"users": db.list_users()})


@app.post("/api/admin/delete-user")
async def admin_delete_user(request: Request, user_id: int = Form(...)):
    admin = require_admin(request)
    if user_id == admin["id"]:
        raise HTTPException(400, "본인 계정은 삭제할 수 없습니다.")
    ok = db.delete_user(user_id)
    if not ok:
        raise HTTPException(400, "삭제할 수 없는 사용자입니다.")
    log_activity("사용자 삭제", f"user_id={user_id}")
    return JSONResponse({"ok": True})


# ─── Dashboard API ───

@app.get("/api/dashboard")
async def dashboard(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"rfp_count": 0, "proposal_count": 0, "knowledge_count": 0, "pipeline_count": 0, "recent_rfps": [], "activity_log": [], "pipeline_status": {}})
    owner_id = None if user.get("is_admin") else user["id"]
    rfps = db.list_rfps(owner_id)
    pipelines = db.list_pipelines()
    visible_ids = {r["id"] for r in rfps}
    scoped_pipelines = {k: v for k, v in pipelines.items() if owner_id is None or k in visible_ids}
    return JSONResponse({
        "rfp_count": len(rfps),
        "proposal_count": db.count_proposals(),
        "knowledge_count": db.count_knowledge(),
        "pipeline_count": len(scoped_pipelines),
        "recent_rfps": [
            {"id": v["id"], "filename": v["filename"], "text_length": v["text_length"]}
            for v in rfps[:5]
        ],
        "activity_log": db.get_recent_activities(10),
        "pipeline_status": {
            rfp_id: {
                "steps": list(data.get("completed_steps", {}).keys()),
                "total": len(data.get("completed_steps", {})),
            }
            for rfp_id, data in scoped_pipelines.items()
        },
    })


# ─── Upload ───

MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))  # 100MB
ALLOWED_DOC_EXTS = {".pdf", ".docx", ".txt", ".md"}


def _safe_filename(name: str) -> str:
    name = Path(name).name  # strip any path components
    name = re.sub(r"[^A-Za-z0-9._\-가-힣 ]", "_", name)
    return name[:200] or "upload.pdf"


@app.post("/api/upload-rfp")
async def upload_rfp(request: Request, file: UploadFile = File(...)):
    user = require_user(request)
    safe_name = _safe_filename(file.filename or "")
    if not safe_name.lower().endswith(".pdf"):
        raise HTTPException(400, "PDF 파일만 업로드 가능합니다.")

    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"파일 크기가 너무 큽니다. (최대 {MAX_UPLOAD_BYTES//(1024*1024)}MB)")
    if not content.startswith(b"%PDF-"):
        raise HTTPException(400, "유효한 PDF 파일이 아닙니다.")

    rfp_id = str(uuid.uuid4())[:8]
    filepath = UPLOAD_DIR / f"{rfp_id}_{safe_name}"
    with open(filepath, "wb") as f:
        f.write(content)

    text = extract_pdf_text(str(filepath))
    db.insert_rfp(rfp_id, safe_name, str(filepath), len(text), datetime.now().isoformat(), owner_id=user["id"])
    db.upsert_pipeline(rfp_id, {}, {})
    log_activity("RFP 업로드", f"{safe_name} ({len(text):,}자)")
    return {"rfp_id": rfp_id, "filename": safe_name, "text_length": len(text), "preview": text[:500]}


# ─── Document Upload (제안서, 레퍼런스 등) ───

@app.post("/api/document/upload")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    rfp_id: str = Form(None),
    doc_type: str = Form("proposal"),
):
    user = require_user(request)
    if rfp_id:
        check_rfp_access(request, rfp_id)

    safe_name = _safe_filename(file.filename or "")
    ext = Path(safe_name).suffix.lower()
    if ext not in ALLOWED_DOC_EXTS:
        raise HTTPException(400, f"허용 확장자: {', '.join(sorted(ALLOWED_DOC_EXTS))}")

    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"파일 크기가 너무 큽니다. (최대 {MAX_UPLOAD_BYTES//(1024*1024)}MB)")
    # Magic bytes check for binary types
    if ext == ".pdf" and not content.startswith(b"%PDF-"):
        raise HTTPException(400, "유효한 PDF 파일이 아닙니다.")
    if ext == ".docx" and not content[:4] == b"PK\x03\x04":
        raise HTTPException(400, "유효한 DOCX 파일이 아닙니다.")

    doc_id = str(uuid.uuid4())[:8]
    filepath = UPLOAD_DIR / f"doc_{doc_id}_{safe_name}"
    with open(filepath, "wb") as f:
        f.write(content)

    text = extract_document_text(str(filepath), safe_name)
    db.insert_document(doc_id, rfp_id, user["id"], doc_type, safe_name, str(filepath), text, datetime.now().isoformat())
    log_activity("문서 업로드", f"{safe_name} ({len(text):,}자)")
    return {
        "doc_id": doc_id,
        "filename": safe_name,
        "doc_type": doc_type,
        "rfp_id": rfp_id,
        "text_length": len(text),
        "preview": text[:500],
        "content_text": text,
    }


@app.get("/api/document/list")
async def list_documents_api(request: Request, rfp_id: str = None):
    user = require_user(request)
    if rfp_id:
        check_rfp_access(request, rfp_id)
        items = db.list_documents(rfp_id=rfp_id)
    else:
        owner_id = None if user.get("is_admin") else user["id"]
        items = db.list_documents(owner_id=owner_id)
    return JSONResponse({"documents": items})


@app.get("/api/document/{doc_id}")
async def get_document_api(request: Request, doc_id: str):
    user = require_user(request)
    doc = db.get_document(doc_id)
    if not doc:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    if not user.get("is_admin") and doc.get("owner_id") not in (None, user["id"]):
        raise HTTPException(403, "해당 문서에 접근 권한이 없습니다.")
    return JSONResponse({"document": doc})


@app.delete("/api/document/{doc_id}")
async def delete_document_api(request: Request, doc_id: str):
    user = require_user(request)
    doc = db.get_document(doc_id)
    if not doc:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    if not user.get("is_admin") and doc.get("owner_id") not in (None, user["id"]):
        raise HTTPException(403, "해당 문서에 접근 권한이 없습니다.")
    row = db.delete_document(doc_id)
    if row and row[0] and Path(row[0]).exists():
        Path(row[0]).unlink(missing_ok=True)
    log_activity("문서 삭제", doc.get("filename", ""))
    return JSONResponse({"ok": True})


# ─── 1. RFP 자동 구조화 ───

@app.post("/api/analyze-rfp")
async def analyze_rfp(request: Request, rfp_id: str = Form(...)):
    check_rfp_access(request, rfp_id)
    rfp = db.get_rfp_meta(rfp_id)
    if not rfp:
        raise HTTPException(404, "RFP를 찾을 수 없습니다.")
    rfp["text"] = extract_pdf_text(rfp["filepath"]) if rfp.get("filepath") and Path(rfp["filepath"]).exists() else ""

    system = """당신은 RFP(제안요청서) 분석 전문가입니다.
RFP 원문에 실제로 기재된 내용만 추출하세요. 원문에 없는 내용을 임의로 만들지 마세요.

중요 규칙:
- id: RFP 원문에 요구사항 번호가 있으면 그대로 사용하고, 없으면 순번(1, 2, 3...)으로 표기
- description: 반드시 RFP 원문의 문장을 인용하거나 요약. 원문에 없는 요구사항을 절대 만들지 말 것
- evaluation_criteria: RFP에 평가 기준이 명시되어 있을 때만 추출. 없으면 빈 배열
- risks: RFP 내용에서 추론 가능한 리스크만 표기

반드시 아래 JSON 형식으로만 반환. 마크다운 코드블록 없이 순수 JSON만 출력:
{"summary":"RFP 핵심 요약","requirements":[{"id":"원문번호 또는 순번","category":"분류","description":"원문 기반 설명","priority":"필수/선택","risk":"높음/중간/낮음"}],"evaluation_criteria":[{"criteria":"기준","weight":"배점","description":"설명"}],"risks":[{"risk":"리스크명","level":"높음/중간/낮음","description":"설명"}]}"""

    result = call_ai(system, f"다음 RFP를 구조화해주세요:\n\n{rfp['text'][:8000]}", mock_type="analyze")
    db.update_pipeline_step(rfp_id, "analyze", result)
    record_history(rfp_id, "analyze", result)
    log_activity("RFP 구조화", rfp["filename"])
    await ws_manager.notify("RFP 구조화 완료", rfp["filename"])
    return JSONResponse({"rfp_id": rfp_id, "analysis": result})


# ─── 2. Winning 패턴 ───

@app.post("/api/winning-pattern")
async def winning_pattern(
    request: Request,
    rfp_id: str = Form(None),
    industry: str = Form("IT"),
    customer_type: str = Form("대기업"),
):
    require_user(request)
    if rfp_id:
        check_rfp_access(request, rfp_id)
    rfp_text = ""
    if rfp_id and db.rfp_exists(rfp_id):
        meta = db.get_rfp_meta(rfp_id)
        rfp_text = extract_pdf_text(meta["filepath"])[:4000] if meta and Path(meta["filepath"]).exists() else ""

    system = """당신은 한국 IT 제안서 수주 전략 전문가(Shipley/APMP 인증 보유)입니다.
고객/산업/RFP를 분석하여 **실제 수주 전략 워크북 수준**의 패턴 분석을 산출하세요.

## 분석 표준
1. **산업/고객 특성 정량 분석**: 평가기준 가중치 분포(공공/금융/대기업/중견), 평균 수주가격 포지션, 의사결정자 구성, 평가 위원 페르소나
2. **Winning Patterns**: 해당 산업/고객에서 통계적으로 자주 성공한 패턴 5~7개 — 각 패턴의 신뢰도(높음/중간/낮음)와 적용 사례
3. **Win Themes**: 본 RFP에서 가장 효과적일 3~5개 핵심 메시지 — 발주처 호명 + 핵심 가치 + 차별 메시지
4. **Discriminators**: 경쟁사 대비 당사만의 차별점 후보 3~5개 — 각 차별점의 Proof Point(수치/사례/인증) 필수
5. **Proof Points**: 사용 가능한 정량 근거(수치/레퍼런스/인증) 5개 이상
6. **Ghost Team (경쟁사 분석)**: 예상 경쟁사 3개 — 강점/약점/예상 제안 포지션
7. **Evaluator Personas**: 평가위원 3명 가상 — 역할/관심사/예상 질문/대응 메시지
8. **Price-to-Win**: 산업 평균 가격대 + 권장 입찰 포지션 + 가격 평가 가중치
9. **Risk Scenarios**: 수주 실패 시나리오 3개 + 사전 대응

## 출력 JSON 스키마 (마크다운 코드블록 금지, 순수 JSON만)
{
  "industry_analysis": "산업 분석 200자 — 시장 트렌드, 발주 패턴, 주요 이슈",
  "customer_profile": {
    "type": "공공기관/대기업/금융권/중견기업 중 하나",
    "decision_factors": ["1순위 의사결정 요인", "2순위", "3순위"],
    "evaluation_weight_typical": {"기술": "30%", "수행경험": "25%", "가격": "20%", "프로젝트관리": "15%", "기술지원": "10%"},
    "buying_signals": ["수주 가능성을 높이는 발주 신호 (예: 사전 영업 미팅 빈도)"],
    "common_objections": ["흔한 반대 의견 (예: 비용 부담)"]
  },
  "winning_patterns": [
    {"pattern": "패턴명 (예: 검증된 안정성 강조)", "description": "어떻게 작동하는지", "confidence": "높음/중간/낮음", "example": "이 패턴이 통한 실제 사례 한 줄"}
  ],
  "win_themes": [
    {"theme": "핵심 메시지 1 (예: '검증된 안정성, 측정된 가치')", "supporting_message": "이 테마를 뒷받침할 한 줄", "evidence_required": "필요한 증거 (수치/사례)"}
  ],
  "discriminators": [
    {"discriminator": "당사만의 차별점", "proof_point": "이를 입증하는 수치/사례/인증", "competitor_gap": "경쟁사 대비 우위 정도"}
  ],
  "proof_points": [
    {"category": "수행 실적", "claim": "동일 영역 12건 수행", "evidence": "최근 3년 누적 240억원 / 평균 만족도 4.7"},
    {"category": "기술 역량", "claim": "정보관리기술사 보유 3명", "evidence": "사내 인력 명부 검증"},
    {"category": "고객 만족", "claim": "5년 연속 NPS 90+", "evidence": "외부 컨설팅 조사 결과"}
  ],
  "ghost_team": [
    {"competitor": "경쟁사 A (예상)", "strengths": ["강점 1", "강점 2"], "weaknesses": ["약점 1"], "likely_positioning": "예상 제안 포지션 (예: 최저가 + 신속 납기)", "counter_strategy": "당사 대응 전략"},
    {"competitor": "경쟁사 B (예상)", "strengths": [], "weaknesses": [], "likely_positioning": "", "counter_strategy": ""},
    {"competitor": "경쟁사 C (예상)", "strengths": [], "weaknesses": [], "likely_positioning": "", "counter_strategy": ""}
  ],
  "evaluator_personas": [
    {"role": "기술 평가위원", "background": "예: 정보화책임관/CIO", "key_concerns": ["기술 적합성", "안정성"], "expected_questions": ["예상 질문 1", "예상 질문 2"], "key_message_to_deliver": "전달해야 할 핵심 메시지"},
    {"role": "사업 평가위원", "background": "구매/계약 담당자", "key_concerns": ["가격", "리스크"], "expected_questions": [], "key_message_to_deliver": ""},
    {"role": "사용자 대표", "background": "현업 부서장", "key_concerns": ["편의성", "운영 부담"], "expected_questions": [], "key_message_to_deliver": ""}
  ],
  "price_to_win": {
    "market_average_range": "예상 시장 평균 가격대 (예: 5억~7억원)",
    "recommended_position": "시장 평균 -8% 또는 평균가 권장",
    "rationale": "왜 이 포지션인지",
    "price_weight": "RFP 가격 평가 가중치",
    "low_price_threshold": "최저가 입찰 예상 금액",
    "high_price_threshold": "고가 입찰 예상 금액"
  },
  "style_recommendations": [
    "고객 유형별 작성 톤 추천 1 (예: 공공기관 - 안정성/준법성/보안 인증 강조)",
    "주요 표현 가이드 (수동태 지양, 정량 수치 우선 등)"
  ],
  "differentiation_tips": [
    "구체적 차별화 액션 1",
    "PoC 제안으로 기술 입증",
    "고객 인터뷰 기반 Pain Point 자료 첨부"
  ],
  "risk_scenarios": [
    {"scenario": "기술 평가 열위 시나리오", "probability": "중", "impact": "치명", "mitigation": "PoC 자료 사전 전달 + 시연 영상"},
    {"scenario": "최저가 경쟁 시나리오", "probability": "상", "impact": "중", "mitigation": "TCO 절감 자료로 Value for Money 강조"},
    {"scenario": "수행 인력 의구심 시나리오", "probability": "중", "impact": "중", "mitigation": "핵심 인력 이력서 + 자격증 사본 첨부"}
  ],
  "action_plan": [
    {"action": "사전 영업 미팅 추진", "owner": "영업 PL", "due": "제안 마감 D-21"},
    {"action": "PoC 데모 환경 구축", "owner": "Tech Lead", "due": "D-14"},
    {"action": "유사 프로젝트 인터뷰 영상 제작", "owner": "마케팅", "due": "D-7"}
  ]
}"""

    user_msg = f"산업: {industry}\n고객 유형: {customer_type}"
    if rfp_text:
        user_msg += f"\n\nRFP 내용:\n{rfp_text}"

    result = call_ai(system, user_msg, mock_type="pattern")
    if rfp_id:
        db.update_pipeline_step(rfp_id, "pattern", result)
    record_history(rfp_id, "pattern", result)
    log_activity("패턴 분석", f"{industry} / {customer_type}")
    return JSONResponse({"analysis": result})


# ─── 3. 제안서 초안 ───

@app.post("/api/generate-proposal")
async def generate_proposal(
    request: Request,
    rfp_id: str = Form(None),
    company_info: str = Form(""),
    references: str = Form(""),
):
    require_user(request)
    if rfp_id:
        check_rfp_access(request, rfp_id)
    rfp_text = ""
    if rfp_id and db.rfp_exists(rfp_id):
        meta = db.get_rfp_meta(rfp_id)
        rfp_text = extract_pdf_text(meta["filepath"])[:6000] if meta and Path(meta["filepath"]).exists() else ""

    system = """당신은 공공/대기업 IT 프로젝트 수주 경력 20년차 제안 PM 겸 컨설턴트입니다.
RFP 원문, 회사 정보, 레퍼런스를 분석하여 **실제 평가위원이 평가표로 채점 가능한 수준의 제안서 초안**을 작성하세요.

## 핵심 작성 원칙
1. **각 메인 섹션 최소 600자 이상**. 모호한 일반론 금지. 구체적 기술명/모델명/수치/일정/금액을 명시
2. **RFP 평가기준에 1:1 매핑**: 각 평가항목별로 어떤 섹션이 어떻게 대응하는지 명확히 서술
3. **정량 수치를 풍부하게**: 처리량(TPS), 응답시간(ms), 가용성(%), 비용절감률(%), 일정(개월/주), 인원(M/M), 만족도(점) 등을 적극 활용
4. **고객 Pain Point → 솔루션 → 효과 → 증거** 4단 구조로 서술
5. **차별화 포인트(Discriminators)** 3개 이상을 각 섹션에 자연스럽게 녹임
6. **Proof Points**: "유사 프로젝트 OO에서 △△% 개선" 형태의 구체적 사례 인용
7. **한국어 비즈니스 공식 문체** (존칭, 경어체), 표·리스트 형식의 구조화된 서술 권장 (예: "1) ... 2) ... 3) ...")
8. **반드시 RFP 원문 어휘를 의도적으로 재인용**하여 평가위원이 "RFP 이해도 높음"으로 인지하게 작성

## 섹션별 작성 가이드 (모두 600자 이상)
- **1. 제안 개요**: 제안 배경(고객 환경/이슈) + 제안 목적 + 본 제안의 차별점 3가지(Discriminators) + 정량적 기대효과(예: 운영비 30% 절감)
- **2. 현황 분석**: AS-IS(고객의 현재 시스템·프로세스 한계 4가지 이상) → TO-BE(제안 시스템 도입 후 모습) → Gap 분석 → 우선 해결 과제 정의
- **3. 제안 솔루션**: 전체 아키텍처(레이어/컴포넌트 명시), 핵심 기능 5~7개 상세(기능명/입력/처리/출력/성능지표), 기술 스택 표(스택/버전/선정사유/대안 비교), 데이터/통합 흐름
- **4. 수행 방안**: 추진 체계도(역할/책임/소통구조), WBS 기반 단계별 일정(Phase 1~3, 산출물/마일스톤/검수기준), 품질관리(ISO/CMMI 적용, 코드리뷰/테스트 전략), 리스크 관리표(상위 5개, 영향도×발생가능성×대응방안), 형상/이슈/변경 관리
- **5. 수행 실적 및 투입 인력**: 유사 프로젝트 3건 이상(고객사/기간/규모/역할/성과 수치), 핵심 인력 5명 이상(이름가명/직급/경력연수/주요 자격/투입 M/M/주요 수행 사례)
- **6. 유지보수 및 기술 지원**: 무상 하자보수(기간/범위), 유상 SLA(가용성 99.9% 등), 기술지원 체계(24/7 콜센터/원격/현장), 패치 정책, KPI/리포팅 주기
- **7. 투자 비용**: 비용 구성 요약(인건비/SW/HW/기타), 본 제안의 Value for Money 강조 (직접 비용 외 절감효과 포함)

## 출력 JSON 스키마 (반드시 모든 필드 채워서 출력. 마크다운 코드블록 금지, 순수 JSON만)
{
  "title": "구체적 제안서 제목 (예: 'OO공사 차세대 통합관제시스템 구축 사업 제안서')",
  "executive_summary": "Executive Summary 300~500자: 발주처 핵심 니즈 + 본 제안의 차별성 3가지 + 기대 효과 정량 수치",
  "win_themes": [
    {"theme": "Win Theme 1 (예: 검증된 안정성)", "message": "발주처에 전달할 핵심 메시지 한 줄"},
    {"theme": "Win Theme 2", "message": "..."},
    {"theme": "Win Theme 3", "message": "..."}
  ],
  "discriminators": [
    {"point": "차별화 포인트 1", "proof": "이를 뒷받침하는 구체 근거(수치/사례/인증)"},
    {"point": "차별화 포인트 2", "proof": "..."},
    {"point": "차별화 포인트 3", "proof": "..."}
  ],
  "evaluation_mapping": [
    {"criteria": "RFP 평가항목명 (예: 기술 이해도)", "weight": "30%", "section_ref": "3. 제안 솔루션", "key_message": "이 평가항목을 어떻게 만족시키는지 한 줄 요약"}
  ],
  "table_of_contents": ["1. 제안 개요","  1.1 제안 배경 및 목적","  1.2 프로젝트 범위 및 기대효과","  1.3 본 제안의 차별점","2. 현황 분석","  2.1 고객 환경 분석 (AS-IS)","  2.2 개선 방향 (TO-BE)","  2.3 Gap 분석 및 핵심 과제","3. 제안 솔루션","  3.1 전체 시스템 아키텍처","  3.2 핵심 기능 상세","  3.3 기술 스택 및 선정 근거","  3.4 데이터 및 시스템 연계 방안","4. 수행 방안","  4.1 프로젝트 추진 체계","  4.2 단계별 일정 계획 (WBS)","  4.3 품질 관리 방안","  4.4 리스크 관리 방안","  4.5 변경/형상/이슈 관리","5. 수행 실적 및 투입 인력","  5.1 유사 프로젝트 레퍼런스","  5.2 핵심 투입 인력 프로필","6. 유지보수 및 기술 지원","  6.1 하자보수 범위 및 기간","  6.2 SLA 및 기술 지원 체계","7. 투자 비용 개요","  7.1 비용 구성","  7.2 Value for Money"],
  "sections": {
    "1. 제안 개요": "600자 이상의 본문. 1.1~1.3 소제목별 단락 구성. 정량 수치 3개 이상 포함",
    "2. 현황 분석": "600자 이상. AS-IS 한계 4개 이상 + TO-BE 청사진 + Gap 분석 + 우선 과제",
    "3. 제안 솔루션": "800자 이상. 아키텍처(레이어/컴포넌트), 핵심 기능 5~7개(기능명/성능지표), 기술 스택 표 형식, 연계 방안",
    "4. 수행 방안": "800자 이상. 추진 체계, Phase별 일정/산출물/마일스톤, 품질/리스크/변경 관리",
    "5. 수행 실적 및 투입 인력": "600자 이상. 유사 프로젝트 3건(고객/기간/규모/성과), 핵심 인력 5명 이상(직급/경력/자격)",
    "6. 유지보수 및 기술 지원": "500자 이상. 하자보수, SLA, 기술지원 체계, KPI",
    "7. 투자 비용 개요": "400자 이상. 비용 구성 요약, Value for Money"
  },
  "references_summary": [
    {"customer": "고객사", "project": "프로젝트명", "period": "수행 기간", "scale": "규모(금액/인원)", "outcome": "정량 성과"}
  ],
  "key_personnel": [
    {"role": "역할 (예: PM)", "grade": "특급/고급/중급", "years": "경력 연수", "certs": "주요 자격증", "highlight": "대표 수행 이력 한 줄"}
  ],
  "risk_register": [
    {"risk": "리스크", "impact": "영향(상/중/하)", "likelihood": "발생가능성(상/중/하)", "mitigation": "대응 방안"}
  ]
}"""

    user_msg = f"""## 회사 정보
{company_info or '(제안사 정보를 기반으로 합리적으로 추정하여 작성하세요)'}

## 레퍼런스
{references or '(유사 프로젝트 경험을 합리적으로 추정하여 작성하세요)'}"""
    if rfp_text:
        user_msg += f"\n\n## RFP 원문\n{rfp_text}"

    result = call_ai(system, user_msg, mock_type="proposal")
    proposal_id = str(uuid.uuid4())[:8]
    db.insert_proposal(proposal_id, rfp_id, result)
    if rfp_id:
        db.update_pipeline_step(rfp_id, "proposal", result)
    record_history(rfp_id, "proposal", result)
    log_activity("제안서 생성", f"Proposal #{proposal_id}")
    await ws_manager.notify("제안서 초안 생성", f"Proposal #{proposal_id}")
    return JSONResponse({"proposal_id": proposal_id, "proposal": result})


# ─── 4. 리뷰 AI ───

@app.post("/api/review-proposal")
async def review_proposal(
    request: Request,
    rfp_id: str = Form(None),
    proposal_text: str = Form(""),
):
    require_user(request)
    if rfp_id:
        check_rfp_access(request, rfp_id)
    rfp_text = ""
    if rfp_id and db.rfp_exists(rfp_id):
        meta = db.get_rfp_meta(rfp_id)
        rfp_text = extract_pdf_text(meta["filepath"])[:4000] if meta and Path(meta["filepath"]).exists() else ""

    system = """당신은 발주처 평가위원 출신의 제안서 레드팀 리뷰 책임자입니다.
RFP 원문과 제안서 본문을 대조하여 **실제 평가표 채점 수준의 상세 리뷰**를 수행하세요.

## 리뷰 원칙
1. **요구사항 추적성(Requirements Traceability)**: RFP 모든 요구사항(REQ-XXX)에 대해 충족/부분충족/누락을 명시
2. **평가위원 페르소나 3종**: 기술평가(아키텍처/기술 스택), 사업평가(가격/일정/위험), 운영평가(유지보수/지원) 관점에서 별도 점수 산출
3. **섹션별 강점/약점**: 각 메인 섹션별 강점 1개·약점 1개 이상
4. **정량 진단**: 점수는 단순 평균이 아니라 가중 평균(평가기준 가중치 적용)
5. **개선 제안의 우선순위·작업량 표기**: 즉시(P0)·1주이내(P1)·여유(P2)
6. **wording 개선 예시**: 약한 표현 → 강한 표현 변환 예시 3개 이상

## 출력 JSON 스키마 (마크다운 금지, 순수 JSON만)
{
  "overall_score": 78,
  "weighted_score": 76.8,
  "grade": "B+",
  "win_probability": "65%",
  "summary": "제안서 종합 한 줄 평 (강점·약점 균형)",
  "review_items": [
    {"category": "RFP 요구사항 충족도", "score": 85, "max": 100, "status": "양호", "comment": "구체적 코멘트"},
    {"category": "기술적 타당성", "score": 80, "max": 100, "status": "양호", "comment": "..."},
    {"category": "수행 방안의 명확성", "score": 75, "max": 100, "status": "보통", "comment": "..."},
    {"category": "차별화 요소", "score": 65, "max": 100, "status": "보완필요", "comment": "..."},
    {"category": "수행 실적/레퍼런스", "score": 82, "max": 100, "status": "양호", "comment": "..."},
    {"category": "투입 인력 적정성", "score": 78, "max": 100, "status": "양호", "comment": "..."},
    {"category": "가격 경쟁력", "score": 75, "max": 100, "status": "보통", "comment": "..."},
    {"category": "유지보수/지원 체계", "score": 80, "max": 100, "status": "양호", "comment": "..."},
    {"category": "문서 완성도", "score": 82, "max": 100, "status": "양호", "comment": "..."}
  ],
  "evaluator_perspectives": [
    {"persona": "기술 평가위원", "score": 78, "key_concern": "기술 스택 선정 사유는 명확하나, 대안 비교 부족", "key_strength": "아키텍처 도식이 구체적"},
    {"persona": "사업 평가위원", "score": 74, "key_concern": "가격 산정 근거 일부 미흡, 리스크 대응 비용 누락 가능", "key_strength": "일정이 현실적"},
    {"persona": "운영 평가위원", "score": 81, "key_concern": "SLA 위반 시 페널티 조항 명시 부족", "key_strength": "24/7 지원 체계 우수"}
  ],
  "requirements_traceability": [
    {"req_id": "REQ-001", "title": "사용자 인증/SSO", "status": "충족", "section_ref": "3.2 핵심 기능", "evidence": "구현 방안과 성능 지표 명시"},
    {"req_id": "REQ-002", "title": "실시간 대시보드", "status": "부분충족", "section_ref": "3.2", "gap": "동시 사용자 1,000명 처리 검증 자료 미제시"},
    {"req_id": "REQ-006", "title": "ERP 연동", "status": "누락", "section_ref": "-", "gap": "ERP 인터페이스 명세·전문 정의 누락"}
  ],
  "section_analysis": [
    {"section": "1. 제안 개요", "strengths": ["기대효과 정량 수치 제시", "차별점 명확"], "weaknesses": ["발주처 명시적 호명 부족"]},
    {"section": "3. 제안 솔루션", "strengths": ["아키텍처 구체적"], "weaknesses": ["기술 스택 대안 비교 표 없음", "보안 통제 항목 누락"]},
    {"section": "4. 수행 방안", "strengths": ["WBS 상세"], "weaknesses": ["크리티컬 패스 식별 부재"]}
  ],
  "missing_requirements": [
    {"req_id": "REQ-006", "description": "ERP 연동 상세 인터페이스 명세", "severity": "치명적"},
    {"req_id": "REQ-009", "description": "재해복구(DR) 방안 누락", "severity": "높음"},
    {"req_id": "REQ-011", "description": "데이터 마이그레이션 절차 미정의", "severity": "높음"}
  ],
  "logic_issues": [
    {"issue": "3.2절 '6개월 완료' vs 4.2절 '8개월 일정표' 불일치", "where": "3.2 ↔ 4.2", "fix": "일정 통일 또는 단계별 출시 명시"},
    {"issue": "투입 인력 10명 제안 vs 유사 프로젝트 15명 사례 괴리", "where": "4.1 ↔ 5.2", "fix": "축소 운영 근거 또는 인원 보강 제시"}
  ],
  "improvement_suggestions": [
    {"priority": "P0", "effort": "1일", "suggestion": "ERP 연동 아키텍처 다이어그램 + 인터페이스 표 추가", "impact": "치명적 누락 보완 → 점수 8점 상승 예상"},
    {"priority": "P0", "effort": "0.5일", "suggestion": "DR 방안 절 추가 (RPO/RTO/이중화 구조)", "impact": "리스크 평가 가산 가능"},
    {"priority": "P1", "effort": "1일", "suggestion": "Phase별 마일스톤·산출물·검수기준 명확화", "impact": "프로젝트 관리 평가 5점 상승"},
    {"priority": "P1", "effort": "0.5일", "suggestion": "경쟁사 대비 기술 우위 비교 표 작성", "impact": "차별화 평가 강화"},
    {"priority": "P2", "effort": "1일", "suggestion": "고객 인터뷰 기반 Pain Point 해결 사례 추가", "impact": "공감 가능성 상승"}
  ],
  "wording_improvements": [
    {"before": "안정적인 시스템을 제공합니다", "after": "동일 규모 12건 무중단 운영 — 가용성 99.95% 검증된 시스템을 제공합니다", "why": "정량 수치로 신뢰도 강화"},
    {"before": "신속한 대응이 가능합니다", "after": "Critical 1시간 / High 4시간 SLA를 24/7 보장합니다", "why": "구체적 SLA 명시"},
    {"before": "우수한 기술력을 보유하고 있습니다", "after": "정보관리기술사 3명, PMP 5명, 클라우드 자격 12명 보유", "why": "자격증 수치로 객관화"}
  ],
  "competitor_gaps": [
    {"area": "가격", "our_position": "중상위(8% 절감)", "risk": "최저가 입찰사 대비 가격 평가 1~2점 열위 가능", "counter": "TCO 절감 효과로 Value for Money 강조"},
    {"area": "수행 실적", "our_position": "우위(12건)", "risk": "-", "counter": "정량 성과 표 전면 배치"}
  ]
}"""

    user_msg = f"제안서 내용:\n{proposal_text[:6000]}"
    if rfp_text:
        user_msg += f"\n\nRFP 원문:\n{rfp_text}"

    result = call_ai(system, user_msg, mock_type="review")
    if rfp_id:
        db.update_pipeline_step(rfp_id, "review", result)
    record_history(rfp_id, "review", result)
    log_activity("레드팀 리뷰", "제안서 검토 완료")
    return JSONResponse({"review": result})


# ─── 5. Go/No-Go ───

@app.post("/api/strategy")
async def strategy(
    request: Request,
    rfp_id: str = Form(None),
    company_strengths: str = Form(""),
    market_context: str = Form(""),
):
    require_user(request)
    if rfp_id:
        check_rfp_access(request, rfp_id)
    rfp_text = ""
    if rfp_id and db.rfp_exists(rfp_id):
        meta = db.get_rfp_meta(rfp_id)
        rfp_text = extract_pdf_text(meta["filepath"])[:4000] if meta and Path(meta["filepath"]).exists() else ""

    system = """당신은 RFP Go/No-Go 의사결정 전문가입니다.
아래 5개 항목을 반드시 모두 채점하고, 정해진 판정 기준에 따라 GO 또는 NO-GO를 결정하세요.

■ 채점 항목 (각 0~100점):
1. market_fit (시장 적합성): 당사 핵심 역량과 RFP 요구사항의 일치도
   - 80점 이상: 핵심 기술 스택이 일치하고 관련 경험 풍부
   - 60~79점: 부분 일치, 일부 기술 보완 필요
   - 60점 미만: 핵심 역량과 거리가 있음
2. competition (경쟁 환경): 예상 경쟁 강도와 수주 가능성
   - 80점 이상: 경쟁사 적거나 당사 우위 명확
   - 60~79점: 3~5개 경쟁사, 당사 중상위 경쟁력
   - 60점 미만: 강력한 경쟁사 다수, 열위
3. profitability (수익성): 예상 마진율과 비용 대비 수익
   - 80점 이상: 마진 20% 이상 예상
   - 60~79점: 마진 10~20% 예상
   - 60점 미만: 마진 10% 미만 또는 적자 위험
4. strategic_value (전략적 가치): 레퍼런스, 시장 확대, 장기 가치
   - 80점 이상: 핵심 산업군 진출 또는 대형 레퍼런스 확보
   - 60~79점: 일정 수준의 전략적 의미
   - 60점 미만: 전략적 가치 낮음
5. resource_availability (자원 가용성): 투입 인력/기술 확보 가능성
   - 80점 이상: 핵심 인력 즉시 투입 가능
   - 60~79점: 일부 충원/파트너 필요
   - 60점 미만: 핵심 자원 확보 어려움

■ GO/NO-GO 판정 기준 (반드시 준수):
- 5개 항목의 가중 평균 = market_fit×25% + competition×20% + profitability×20% + strategic_value×20% + resource_availability×15%
- 가중 평균 70점 이상 → GO
- 가중 평균 50~69점 → CONDITIONAL GO (조건부 참여)
- 가중 평균 50점 미만 → NO-GO
- confidence(신뢰도)는 입력 정보의 충분성에 따라 산정: 정보 충분→80~95%, 보통→60~79%, 부족→40~59%

■ 중요:
- 동일한 RFP와 입력에 대해서는 반드시 일관된 결과를 내야 함
- 회사 강점이나 시장 상황이 미입력이면 해당 항목을 보수적(50~60점)으로 채점하고, confidence를 낮게(40~60%) 설정
- 감으로 판단하지 말고, 위 기준을 기계적으로 적용하여 채점

반드시 아래 JSON 형식으로만 반환. 마크다운 코드블록 없이 순수 JSON만 출력:
{"recommendation":"GO 또는 CONDITIONAL GO 또는 NO-GO","confidence":"가중평균 기반 신뢰도%","weighted_score":72.5,"analysis":{"market_fit":{"score":80,"weight":"25%","comment":"RFP 원문 근거 기반 설명"},"competition":{"score":65,"weight":"20%","comment":"근거 기반 설명"},"profitability":{"score":70,"weight":"20%","comment":"근거 기반 설명"},"strategic_value":{"score":85,"weight":"20%","comment":"근거 기반 설명"},"resource_availability":{"score":75,"weight":"15%","comment":"근거 기반 설명"}},"key_factors":["RFP 원문에서 도출한 핵심 요인"],"win_strategy":["구체적 수주 전략"],"risks":["참여 시 리스크"]}"""

    user_msg = f"회사 강점: {company_strengths or '(미입력 — 보수적으로 채점)'}\n시장 상황: {market_context or '(미입력 — 보수적으로 채점)'}"
    if rfp_text:
        user_msg += f"\n\nRFP 내용:\n{rfp_text}"

    result = call_ai(system, user_msg, mock_type="strategy")
    if rfp_id:
        db.update_pipeline_step(rfp_id, "strategy", result)
    record_history(rfp_id, "strategy", result)
    log_activity("Go/No-Go 분석", "전략 분석 완료")
    return JSONResponse({"strategy": result})


# ─── 6. 지식 자산화 ───

@app.post("/api/knowledge/save")
async def save_knowledge(
    request: Request,
    category: str = Form(...),
    title: str = Form(...),
    content: str = Form(...),
    tags: str = Form(""),
):
    require_user(request)
    item = {
        "id": str(uuid.uuid4())[:8],
        "category": category,
        "title": title,
        "content": content,
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
        "created_at": datetime.now().isoformat(),
    }
    db.insert_knowledge(item)
    log_activity("지식 저장", title)
    return JSONResponse({"saved": True, "item": item, "total": db.count_knowledge()})


@app.get("/api/knowledge/list")
async def list_knowledge_api(request: Request, category: Optional[str] = None):
    require_user(request)
    items = db.list_knowledge(category)
    return JSONResponse({"items": items, "total": len(items)})


@app.post("/api/knowledge/recommend")
async def recommend_knowledge(
    request: Request,
    rfp_id: str = Form(None),
    query: str = Form(""),
):
    require_user(request)
    if rfp_id:
        check_rfp_access(request, rfp_id)
    system = "당신은 지식 자산 관리 전문가입니다. 관련 지식 자산을 추천하고, 재사용 가능한 문장과 템플릿을 JSON으로 제안하세요."
    rfp_text = ""
    if rfp_id and db.rfp_exists(rfp_id):
        meta = db.get_rfp_meta(rfp_id)
        rfp_text = extract_pdf_text(meta["filepath"])[:3000] if meta and Path(meta["filepath"]).exists() else ""
    knowledge_items = db.list_knowledge()
    user_msg = f"검색 쿼리: {query}\n저장된 지식: {json.dumps(knowledge_items, ensure_ascii=False, default=str)[:3000]}"
    if rfp_text:
        user_msg += f"\nRFP: {rfp_text}"
    result = call_ai(system, user_msg, mock_type="knowledge")
    return JSONResponse({"recommendations": result})


# ─── 7. 제안서 Export (Markdown) ───

@app.post("/api/export-proposal")
async def export_proposal(request: Request, proposal_text: str = Form("")):
    require_user(request)
    system = """당신은 문서 변환 전문가입니다.
제안서 내용을 깔끔한 Markdown 문서로 변환하세요.
목차, 섹션 제목, 본문을 포함한 완성된 문서를 만들어주세요.
마크다운 코드블록 감싸기 없이 순수 마크다운으로 출력하세요."""

    result = call_ai(system, f"다음 제안서를 Markdown으로 변환:\n\n{proposal_text[:8000]}", mock_type="")
    if not result or result == "분석이 완료되었습니다.":
        result = f"# 제안서\n\n{proposal_text}"
    log_activity("제안서 Export", "Markdown 변환")
    return JSONResponse({"markdown": result})


@app.post("/api/export-docx")
async def export_docx(request: Request, proposal_text: str = Form("")):
    require_user(request)
    import io
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    parsed = None
    try:
        stripped = proposal_text.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(stripped)
    except Exception:
        match = re.search(r'\{[\s\S]*\}', stripped)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except Exception:
                pass

    doc = Document()

    # Style defaults
    style = doc.styles['Normal']
    style.font.name = 'Malgun Gothic'
    style.font.size = Pt(11)

    if parsed:
        # Title
        title = parsed.get("title", "제안서")
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(title)
        run.bold = True
        run.font.size = Pt(22)
        run.font.color.rgb = RGBColor(30, 58, 138)
        doc.add_paragraph()

        # TOC
        toc = parsed.get("table_of_contents", [])
        if toc:
            h = doc.add_heading("목차", level=1)
            h.runs[0].font.color.rgb = RGBColor(30, 58, 138)
            for item in toc:
                level = 1 if item.startswith("  ") else 0
                p = doc.add_paragraph(item.strip(), style='List Bullet' if level else 'List Number')
            doc.add_page_break()

        # Sections
        sections = parsed.get("sections", {})
        for sec_title, sec_content in sections.items():
            h = doc.add_heading(sec_title, level=2)
            h.runs[0].font.color.rgb = RGBColor(30, 58, 138)
            doc.add_paragraph(sec_content)
            doc.add_paragraph()
    else:
        # Plain text fallback
        doc.add_heading("제안서", level=1)
        for line in proposal_text.split("\n"):
            line = line.strip()
            if not line:
                doc.add_paragraph()
            elif line.startswith("#"):
                level = min(line.count("#"), 4)
                doc.add_heading(line.lstrip("# "), level=level)
            else:
                doc.add_paragraph(line)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)

    log_activity("제안서 Export", "DOCX 다운로드")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": "attachment; filename=proposal.docx"},
    )


@app.post("/api/export-pptx")
async def export_pptx(request: Request, proposal_text: str = Form("")):
    require_user(request)
    import io, math
    from pptx import Presentation
    from pptx.util import Inches, Pt, Emu
    from pptx.dml.color import RGBColor as PptRGB
    from pptx.enum.text import PP_ALIGN
    from pptx.enum.shapes import MSO_SHAPE

    parsed = None
    try:
        stripped = proposal_text.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(stripped)
    except Exception:
        match = re.search(r'\{[\s\S]*\}', stripped)
        if match:
            try: parsed = json.loads(match.group(0))
            except Exception: pass

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    # Colors
    WHITE = PptRGB(255, 255, 255)
    GRAY = PptRGB(148, 163, 184)
    DARK_BG = PptRGB(15, 23, 42)
    CARD_BG = PptRGB(30, 41, 59)
    ACCENT = PptRGB(6, 182, 212)
    LIGHT_BLUE = PptRGB(99, 102, 241)
    GREEN = PptRGB(16, 185, 129)
    AMBER = PptRGB(245, 158, 11)
    PURPLE = PptRGB(139, 92, 246)
    CARD_COLORS = [LIGHT_BLUE, ACCENT, GREEN, AMBER, PURPLE, PptRGB(236, 72, 153)]

    def add_bg(slide):
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = DARK_BG

    def rect(slide, l, t, w, h, color, radius=False):
        shp_type = MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE
        s = slide.shapes.add_shape(shp_type, l, t, w, h)
        s.fill.solid(); s.fill.fore_color.rgb = color; s.line.fill.background()
        return s

    def oval(slide, l, t, w, h, color):
        s = slide.shapes.add_shape(MSO_SHAPE.OVAL, l, t, w, h)
        s.fill.solid(); s.fill.fore_color.rgb = color; s.line.fill.background()
        return s

    def arrow(slide, l, t, w, h, color):
        s = slide.shapes.add_shape(MSO_SHAPE.RIGHT_ARROW, l, t, w, h)
        s.fill.solid(); s.fill.fore_color.rgb = color; s.line.fill.background()
        return s

    def chevron(slide, l, t, w, h, color):
        s = slide.shapes.add_shape(MSO_SHAPE.CHEVRON, l, t, w, h)
        s.fill.solid(); s.fill.fore_color.rgb = color; s.line.fill.background()
        return s

    def tbox(slide, l, t, w, h, text, sz=18, color=WHITE, bold=False, align=PP_ALIGN.LEFT):
        tb = slide.shapes.add_textbox(l, t, w, h)
        tf = tb.text_frame; tf.word_wrap = True
        p = tf.paragraphs[0]; p.text = text; p.font.size = Pt(sz)
        p.font.color.rgb = color; p.font.bold = bold; p.alignment = align
        return tb

    def shaped_text(shape, text, sz=12, color=WHITE, bold=False, align=PP_ALIGN.CENTER):
        tf = shape.text_frame; tf.word_wrap = True
        tf.paragraphs[0].alignment = align
        p = tf.paragraphs[0]; p.text = text; p.font.size = Pt(sz)
        p.font.color.rgb = color; p.font.bold = bold

    def slide_header(slide, title):
        rect(slide, Inches(0), Inches(0), Inches(0.12), Inches(7.5), LIGHT_BLUE)
        tbox(slide, Inches(0.7), Inches(0.4), Inches(11), Inches(0.8), title, sz=28, bold=True)
        rect(slide, Inches(0.7), Inches(1.15), Inches(1.5), Inches(0.05), ACCENT)

    # Max chars per slide content area (~13pt, 11 inch wide) ≈ 380 chars
    MAX_CHARS = 350

    def split_text(text, limit=MAX_CHARS):
        """Split text into chunks that fit one slide."""
        sentences = re.split(r'(?<=[.!?。])\s+', text)
        chunks, cur = [], ""
        for s in sentences:
            if len(cur) + len(s) + 1 > limit and cur:
                chunks.append(cur.strip())
                cur = s
            else:
                cur = cur + " " + s if cur else s
        if cur.strip(): chunks.append(cur.strip())
        return chunks if chunks else [text[:limit]]

    # Detect if content is suitable for diagram
    def detect_visual_type(title, content):
        tl = (title + " " + content).lower()
        if any(k in tl for k in ["아키텍처", "architecture", "시스템 구성", "구조", "플랫폼"]):
            return "architecture"
        if any(k in tl for k in ["일정", "schedule", "단계", "phase", "로드맵", "마일스톤", "timeline"]):
            return "timeline"
        if any(k in tl for k in ["프로세스", "절차", "workflow", "흐름", "방법론", "접근"]):
            return "process"
        if any(k in tl for k in ["장점", "강점", "특징", "핵심", "차별", "benefit", "advantage"]):
            return "cards"
        if any(k in tl for k in ["조직", "인력", "체계", "팀", "역할"]):
            return "org"
        return "text"

    def extract_items(content):
        """Extract bullet-like items from text."""
        items = []
        for line in content.replace(".", ".\n").split("\n"):
            line = line.strip().lstrip("-•·▶▷◆ ")
            if len(line) > 5: items.append(line)
        return items[:8]  # max 8 items for visual

    def add_architecture_slide(slide, title, content):
        slide_header(slide, title)
        items = extract_items(content)
        if len(items) < 3: items = [content[i:i+40] for i in range(0, min(len(content), 200), 40)]
        # Draw layered architecture
        layers = items[:5]
        y_start = Inches(1.6)
        for i, layer in enumerate(layers):
            c = CARD_COLORS[i % len(CARD_COLORS)]
            bg_c = PptRGB(c.red // 4 + 10, c.green // 4 + 10, c.blue // 4 + 10) if hasattr(c, 'red') else CARD_BG
            s = rect(slide, Inches(1.5), y_start + Inches(i * 1.05), Inches(10), Inches(0.9), CARD_BG, radius=True)
            # Color accent bar on left
            rect(slide, Inches(1.5), y_start + Inches(i * 1.05), Inches(0.12), Inches(0.9), c)
            tbox(slide, Inches(1.9), y_start + Inches(i * 1.05) + Inches(0.15), Inches(9), Inches(0.6), layer[:80], sz=14, color=WHITE, bold=True if i == 0 else False)
        # Side label
        tbox(slide, Inches(0.7), Inches(6.5), Inches(5), Inches(0.4), f"{len(layers)}개 계층 아키텍처", sz=11, color=GRAY)

    def add_timeline_slide(slide, title, content):
        slide_header(slide, title)
        items = extract_items(content)
        if len(items) < 2: items = content.split(",")
        items = [it.strip() for it in items if it.strip()][:6]
        n = len(items)
        # Horizontal timeline
        y_line = Inches(3.5)
        rect(slide, Inches(1), y_line, Inches(11), Inches(0.06), PptRGB(51, 65, 85))
        for i, item in enumerate(n and items or ["Phase 1"]):
            x = Inches(1.2 + i * (10.5 / max(n - 1, 1))) if n > 1 else Inches(6)
            c = CARD_COLORS[i % len(CARD_COLORS)]
            # Circle node
            o = oval(slide, x - Inches(0.2), y_line - Inches(0.17), Inches(0.4), Inches(0.4), c)
            shaped_text(o, str(i + 1), sz=11, bold=True)
            # Label above
            tbox(slide, x - Inches(1.2), y_line - Inches(1.3), Inches(2.4), Inches(1.0), item[:50], sz=12, color=WHITE, bold=True, align=PP_ALIGN.CENTER)
            # Phase label below
            tbox(slide, x - Inches(0.8), y_line + Inches(0.5), Inches(1.6), Inches(0.4), f"Phase {i + 1}", sz=10, color=GRAY, align=PP_ALIGN.CENTER)

    def add_process_slide(slide, title, content):
        slide_header(slide, title)
        items = extract_items(content)[:5]
        if len(items) < 2: items = ["분석", "설계", "구현", "테스트", "배포"]
        n = len(items)
        # Chevron process flow
        chev_w = Inches(min(2.2, 11 / n))
        gap = Inches(0.1)
        total_w = n * (chev_w + gap)
        start_x = (Inches(13.333) - total_w) // 2 + Inches(0.3)
        for i, item in enumerate(items):
            x = start_x + i * (chev_w + gap)
            c = CARD_COLORS[i % len(CARD_COLORS)]
            s = chevron(slide, x, Inches(2.8), chev_w, Inches(1.6), c)
            shaped_text(s, item[:20], sz=13, bold=True)
            # Description below
            tbox(slide, x, Inches(4.6), chev_w, Inches(1.2), "", sz=10, color=GRAY, align=PP_ALIGN.CENTER)

    def add_cards_slide(slide, title, content):
        slide_header(slide, title)
        items = extract_items(content)[:6]
        if len(items) < 2: items = [content[:60]]
        n = len(items)
        cols = min(n, 3)
        rows = math.ceil(n / cols)
        card_w = Inches(3.4)
        card_h = Inches(2.0)
        gap_x = Inches(0.4)
        gap_y = Inches(0.3)
        total_w = cols * card_w + (cols - 1) * gap_x
        start_x = (Inches(13.333) - total_w) // 2
        for i, item in enumerate(items):
            col = i % cols
            row = i // cols
            x = start_x + col * (card_w + gap_x)
            y = Inches(1.6) + row * (card_h + gap_y)
            c = CARD_COLORS[i % len(CARD_COLORS)]
            # Card bg
            card = rect(slide, x, y, card_w, card_h, CARD_BG, radius=True)
            # Top accent
            rect(slide, x, y, card_w, Inches(0.06), c)
            # Number circle
            num_s = oval(slide, x + Inches(0.2), y + Inches(0.3), Inches(0.45), Inches(0.45), c)
            shaped_text(num_s, str(i + 1), sz=14, bold=True)
            # Text
            tbox(slide, x + Inches(0.85), y + Inches(0.3), card_w - Inches(1.1), card_h - Inches(0.5), item[:80], sz=13, color=WHITE)

    def add_org_slide(slide, title, content):
        slide_header(slide, title)
        items = extract_items(content)[:6]
        if not items: items = ["프로젝트 관리"]
        # Top box (PM)
        pm_w = Inches(3)
        pm_x = (Inches(13.333) - pm_w) // 2
        s = rect(slide, pm_x, Inches(1.6), pm_w, Inches(0.9), LIGHT_BLUE, radius=True)
        shaped_text(s, items[0][:30], sz=14, bold=True)
        # Lines + sub boxes
        subs = items[1:] if len(items) > 1 else ["기술팀", "기획팀", "QA팀"]
        n = len(subs)
        sub_w = Inches(2.4)
        gap = Inches(0.3)
        total = n * sub_w + (n - 1) * gap
        start_x = (Inches(13.333) - total) // 2
        center_x = pm_x + pm_w // 2
        rect(slide, center_x - Inches(0.02), Inches(2.5), Inches(0.04), Inches(0.6), PptRGB(51, 65, 85))
        for i, sub in enumerate(subs):
            x = start_x + i * (sub_w + gap)
            c = CARD_COLORS[(i + 1) % len(CARD_COLORS)]
            s = rect(slide, x, Inches(3.3), sub_w, Inches(0.8), c, radius=True)
            shaped_text(s, sub[:25], sz=13, bold=True)
            # Connect line
            cx = x + sub_w // 2
            rect(slide, cx - Inches(0.02), Inches(3.1), Inches(0.04), Inches(0.2), PptRGB(51, 65, 85))

    def add_text_slide(slide, title, text_chunk):
        slide_header(slide, title)
        card = rect(slide, Inches(0.5), Inches(1.5), Inches(12.333), Inches(5.5), CARD_BG, radius=True)
        tb = slide.shapes.add_textbox(Inches(0.9), Inches(1.8), Inches(11.5), Inches(4.9))
        tf = tb.text_frame; tf.word_wrap = True
        for j, line in enumerate(text_chunk.split("\n")):
            line = line.strip()
            if not line: continue
            p = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
            p.text = line[:120]  # hard limit per line
            p.font.size = Pt(14); p.font.color.rgb = PptRGB(203, 213, 225)
            p.space_after = Pt(8); p.line_spacing = Pt(22)

    # ─── Build slides ───
    title_text = parsed.get("title", "제안서") if parsed else "제안서"
    toc_items = parsed.get("table_of_contents", []) if parsed else []
    sections = parsed.get("sections", {}) if parsed else {}

    # Slide 1: Title
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    add_bg(slide)
    rect(slide, Inches(0), Inches(0), Inches(0.12), Inches(7.5), LIGHT_BLUE)
    oval(slide, Inches(9.5), Inches(-0.5), Inches(4.5), Inches(4.5), PptRGB(30, 41, 59))
    oval(slide, Inches(10), Inches(4.5), Inches(3), Inches(3), PptRGB(25, 35, 52))
    tbox(slide, Inches(1), Inches(2.2), Inches(9), Inches(1.5), title_text, sz=40, bold=True)
    rect(slide, Inches(1), Inches(3.8), Inches(2), Inches(0.06), ACCENT)
    from datetime import datetime as dt
    tbox(slide, Inches(1), Inches(4.1), Inches(8), Inches(0.8), "AI-Powered Proposal | PARAIX Hackathon 2026", sz=16, color=GRAY)
    tbox(slide, Inches(1), Inches(5.5), Inches(5), Inches(0.5), dt.now().strftime("%Y년 %m월 %d일"), sz=14, color=GRAY)

    # Slide 2: TOC
    if toc_items:
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        add_bg(slide); slide_header(slide, "목차")
        y = Inches(1.6)
        for i, item in enumerate(toc_items):
            is_sub = item.startswith("  ")
            left = Inches(1.4) if is_sub else Inches(0.7)
            sz = 13 if is_sub else 17
            clr = GRAY if is_sub else WHITE
            tbox(slide, left, y, Inches(10), Inches(0.42), item.strip(), sz=sz, color=clr, bold=not is_sub)
            y += Inches(0.35) if is_sub else Inches(0.42)
            if y > Inches(6.6):
                slide = prs.slides.add_slide(prs.slide_layouts[6])
                add_bg(slide); slide_header(slide, "목차 (계속)")
                y = Inches(1.6)

    # Section slides with auto visual detection
    for sec_title, sec_content in sections.items():
        content = sec_content if isinstance(sec_content, str) else str(sec_content)
        vis_type = detect_visual_type(sec_title, content)

        if vis_type == "architecture":
            slide = prs.slides.add_slide(prs.slide_layouts[6])
            add_bg(slide); add_architecture_slide(slide, sec_title, content)
        elif vis_type == "timeline":
            slide = prs.slides.add_slide(prs.slide_layouts[6])
            add_bg(slide); add_timeline_slide(slide, sec_title, content)
        elif vis_type == "process":
            slide = prs.slides.add_slide(prs.slide_layouts[6])
            add_bg(slide); add_process_slide(slide, sec_title, content)
        elif vis_type == "cards":
            slide = prs.slides.add_slide(prs.slide_layouts[6])
            add_bg(slide); add_cards_slide(slide, sec_title, content)
        elif vis_type == "org":
            slide = prs.slides.add_slide(prs.slide_layouts[6])
            add_bg(slide); add_org_slide(slide, sec_title, content)
        else:
            # Text with auto-split across slides
            chunks = split_text(content)
            for ci, chunk in enumerate(chunks):
                slide = prs.slides.add_slide(prs.slide_layouts[6])
                add_bg(slide)
                label = sec_title if ci == 0 else f"{sec_title} (계속)"
                add_text_slide(slide, label, chunk)

    # Last slide: Thank You
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    add_bg(slide)
    oval(slide, Inches(4), Inches(0.5), Inches(5.333), Inches(5.333), PptRGB(20, 30, 48))
    tbox(slide, Inches(0), Inches(2.5), Inches(13.333), Inches(1.2), "Thank You", sz=48, bold=True, align=PP_ALIGN.CENTER)
    tbox(slide, Inches(0), Inches(3.8), Inches(13.333), Inches(0.6), "AI-Powered by RFP AI Analyzer", sz=18, color=GRAY, align=PP_ALIGN.CENTER)
    rect(slide, Inches(5.5), Inches(4.5), Inches(2.333), Inches(0.05), ACCENT)

    buf = io.BytesIO()
    prs.save(buf); buf.seek(0)
    log_activity("제안서 Export", "PPTX 다운로드")
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={"Content-Disposition": "attachment; filename=proposal.pptx"})


# ─── 8. 사업 견적 산출 ───

@app.post("/api/estimate")
async def estimate_cost(request: Request, rfp_id: str = Form(None), additional_info: str = Form("")):
    require_user(request)
    if rfp_id:
        check_rfp_access(request, rfp_id)
    rfp_text = ""
    if rfp_id and db.rfp_exists(rfp_id):
        meta = db.get_rfp_meta(rfp_id)
        rfp_text = extract_pdf_text(meta["filepath"])[:6000] if meta and Path(meta["filepath"]).exists() else ""

    system = """당신은 공공/대기업 IT 사업 견적 전문 PM 겸 가격 전략가입니다.
RFP를 분석하여 **수주 가능한 가격대**의 상세 사업 견적을 산출하세요.

## 산출 표준 (반드시 준수)
1. **노임단가 기준**: 한국소프트웨어산업협회(KOSA) "SW기술자 평균임금" 2024년 공표 단가 적용
   - 기술사/특급: 약 9,400만원/년 (월 환산 약 783만원, 실 투입가 약 450만원/월)
   - 고급: 약 8,000만원/년 (실 투입가 약 380만원/월)
   - 중급: 약 6,500만원/년 (실 투입가 약 310만원/월)
   - 초급: 약 5,000만원/년 (실 투입가 약 240만원/월)
   - **단가 산출 근거에 노임단가 표를 반드시 명시**

2. **인건비 구성**: PM(1)/PL(1~2)/아키텍트(1)/개발자(다수)/QA(1~2)/디자이너(0~1) — RFP 복잡도에 따라 조정. 인력별 투입 M/M는 Phase별 단계 일정에 맞춰 산정 (전 기간 100% 투입 가정 금지)

3. **인프라/HW**: 클라우드(AWS/Azure/GCP) 우선. 온프레미스는 RFP 명시 시에만.
   - 클라우드 운영비 산정 기준: t3/m5 small=월 5만, medium=15만, large=30만, xlarge=60만, 2xlarge=120만
   - RDS db.m5.large=80만/월, ElastiCache 30만, ALB 5만, S3+CloudFront 20만 등
   - GPU: AWS p4d.24xlarge 월 3,000~4,000만 / 온프 H100 3~5억, A100 1~2억
   - 네트워크/보안: 방화벽 1,000~3,000만, L4 스위치 500~1,500만

4. **SW 라이선스**: Datadog $15/host/월, GitHub Enterprise $21/user/월, Atlassian, MS Office 등 실제 거래가 반영

5. **부가세·마진·예비비 분리 표기**: 본 견적의 직접비, 일반관리비(7~10%), 이윤(8~12%), 예비비(5%), 부가세(10%) 명시. **부가세 별도** 원칙.

6. **수주 가격 전략 옵션**: Base(권장), Aggressive(-8~12%, 마진 최소화), Premium(+10%, 안정성 강화) 3안 제시

7. **분기/마일스톤 결제 일정**: 일반적으로 착수금 20% / 중간보고 40% / 검수완료 40% (조정 가능)

8. **시장 평균 비교**: 동일 규모 사업의 시장 평균 견적 추정값을 제시하고 본 견적의 절감률/프리미엄을 명시

## 금액 검증 규칙
- 단일 카테고리가 전체의 80% 초과 금지 (인건비는 통상 60~75%)
- HW/인프라가 40% 초과 시 클라우드 대안 반드시 제시
- 비현실적 단가(특급 600만원 이상, 일반 서버 5천만 이상)는 근거 명시

## 출력 JSON 스키마 (모든 필드 채워서 출력. 마크다운 코드블록 금지, 순수 JSON만)
{
  "project_name": "구체적 프로젝트명",
  "total_cost": "총 5억 6,160만원 (VAT 별도)",
  "total_cost_number": 561600000,
  "total_cost_with_vat": "6억 1,776만원 (VAT 포함)",
  "total_cost_with_vat_number": 617760000,
  "duration_months": 6,
  "summary": "견적 요약 2~3문장 — 산정 근거(노임단가, 클라우드 임대 기준)와 가격 포지션 명시",
  "labor_rate_basis": {
    "source": "KOSA SW기술자 평균임금 2024년 공표",
    "rates": [
      {"grade": "특급", "annual": "9,400만원/년", "monthly": "약 450만원/월(실투입 기준)"},
      {"grade": "고급", "annual": "8,000만원/년", "monthly": "약 380만원/월"},
      {"grade": "중급", "annual": "6,500만원/년", "monthly": "약 310만원/월"},
      {"grade": "초급", "annual": "5,000만원/년", "monthly": "약 240만원/월"}
    ]
  },
  "categories": [
    {
      "name": "인건비",
      "subtotal": "금액 표기",
      "subtotal_number": 0,
      "ratio": "비율 %",
      "items": [
        {"role": "PM", "grade": "특급", "count": 1, "months": 6, "unit_cost": "450만원", "cost": "2,700만원", "reason": "프로젝트 총괄 (PMP 보유 권장)"}
      ]
    },
    {"name": "SW 라이선스", "subtotal": "...", "subtotal_number": 0, "ratio": "...", "items": [{"item": "...", "cost": "...", "reason": "..."}]},
    {"name": "HW/인프라", "subtotal": "...", "subtotal_number": 0, "ratio": "...", "items": [{"item": "...", "cost": "...", "reason": "..."}]},
    {"name": "기타 경비", "subtotal": "...", "subtotal_number": 0, "ratio": "...", "items": [{"item": "...", "cost": "...", "reason": "..."}]}
  ],
  "phase_breakdown": [
    {"phase": "Phase 1: 기반 구축", "months": "1~2", "cost": "1억 3,000만원", "cost_number": 130000000, "deliverables": "아키텍처 설계서, 인프라 구축 완료보고서"},
    {"phase": "Phase 2: 핵심 개발", "months": "3~5", "cost": "2억 8,000만원", "cost_number": 280000000, "deliverables": "기능 개발 완료, 통합 테스트 결과"},
    {"phase": "Phase 3: 안정화/이관", "months": "6", "cost": "1억 5,160만원", "cost_number": 151600000, "deliverables": "검수 완료, 운영 인계서"}
  ],
  "cost_structure": {
    "direct_cost": "4억 9,000만원",
    "direct_cost_number": 490000000,
    "general_admin": {"label": "일반관리비 (8%)", "amount": "3,920만원", "amount_number": 39200000},
    "profit": {"label": "이윤 (10%)", "amount": "5,292만원", "amount_number": 52920000},
    "contingency": {"label": "예비비 (5%)", "amount": "2,910만원", "amount_number": 29100000},
    "vat": {"label": "부가세 (10%)", "amount": "5,612만원", "amount_number": 56120000}
  },
  "pricing_options": [
    {"option": "Base (권장)", "total": "6억 1,776만원", "rationale": "표준 노임단가 + 정상 마진 10% + 시장 평균 -8%", "win_probability": "60%"},
    {"option": "Aggressive (저가)", "total": "5억 5,000만원", "rationale": "마진 5%, 예비비 축소. 레퍼런스 확보 우선 시", "win_probability": "75%"},
    {"option": "Premium (고가)", "total": "6억 8,000만원", "rationale": "프리미엄 인력 풀투입 + 강화된 SLA. 품질·안정성 평가 가중치 높을 때", "win_probability": "45%"}
  ],
  "payment_milestones": [
    {"milestone": "착수금", "ratio": "20%", "amount": "1억 2,355만원", "trigger": "계약 체결 후 7일 이내"},
    {"milestone": "중간 검수", "ratio": "40%", "amount": "2억 4,710만원", "trigger": "Phase 2 완료 + 중간 보고서 승인"},
    {"milestone": "최종 검수", "ratio": "40%", "amount": "2억 4,710만원", "trigger": "최종 검수 합격 + 인계 완료"}
  ],
  "market_comparison": {
    "market_average": "6억 1,000만원",
    "market_average_number": 610000000,
    "our_total": "5억 6,160만원",
    "delta_pct": "-8%",
    "interpretation": "시장 평균 대비 8% 절감. 가격 평가 가산 기대"
  },
  "risks": [
    {"risk": "요구사항 변경 (Scope Creep)", "impact": "직접비 10~15% 증가 가능", "mitigation": "변경관리 프로세스 + CCB 승인 절차"},
    {"risk": "특급 인력 수급 지연", "impact": "1~2개월 일정 지연", "mitigation": "사전 인력 확정 + 백업 인력 2명 확보"},
    {"risk": "클라우드 환율 변동", "impact": "인프라 비용 5~8% 변동", "mitigation": "환율 헤지 또는 환차 보존 조항"}
  ],
  "assumptions": [
    "노임단가는 KOSA 2024년 공표 기준 적용",
    "클라우드 서비스는 종량제, 1년 약정 기준",
    "6개월 고정 기간 산정 (연장 시 추가 협의)",
    "출장/숙박 등 실비는 별도 정산"
  ],
  "notes": "본 견적은 RFP 기반 추정치이며, 요구사항 확정 후 ±10% 조정 가능. 부가세 별도. 결제는 마일스톤 기준."
}"""

    user_msg = "다음 RFP를 분석하여 상세 사업 견적을 산출해주세요."
    if rfp_text:
        user_msg += f"\n\nRFP 내용:\n{rfp_text}"
    if additional_info:
        user_msg += f"\n\n추가 정보:\n{additional_info}"

    result = call_ai(system, user_msg, mock_type="estimate")
    if rfp_id:
        db.update_pipeline_step(rfp_id, "estimate", result)
    record_history(rfp_id or "", "estimate", result)
    meta = db.get_rfp_meta(rfp_id) if rfp_id else None
    log_activity("견적 산출", meta["filename"] if meta else "")
    return JSONResponse({"estimate": result})


# ─── 9. 원클릭 파이프라인 ───

@app.post("/api/pipeline/run")
async def run_pipeline(
    request: Request,
    rfp_id: str = Form(...),
    company_info: str = Form(""),
    industry: str = Form("IT"),
    customer_type: str = Form("대기업"),
):
    check_rfp_access(request, rfp_id)
    rfp = db.get_rfp_meta(rfp_id)
    if not rfp:
        raise HTTPException(404, "RFP를 찾을 수 없습니다.")
    rfp_text = extract_pdf_text(rfp["filepath"])[:6000] if rfp.get("filepath") and Path(rfp["filepath"]).exists() else ""
    results = {}
    steps_done = []

    # Step 1: Analyze
    system1 = """RFP를 분석하여 반드시 JSON으로만 반환하세요. 마크다운 코드블록 없이 순수 JSON만:
{"summary":"요약","requirements":[{"id":"REQ-001","category":"분류","description":"설명","priority":"필수","risk":"높음"}],"evaluation_criteria":[{"criteria":"기준","weight":"30%","description":"설명"}],"risks":[{"risk":"리스크","level":"높음","description":"설명"}]}"""
    results["analyze"] = call_ai(system1, rfp_text, mock_type="analyze")
    steps_done.append("analyze")

    # Step 2: Pattern
    system2 = """Winning Proposal 패턴을 JSON으로만 반환. 마크다운 코드블록 없이:
{"industry_analysis":"분석","winning_patterns":[{"pattern":"패턴","description":"설명","confidence":"높음"}],"style_recommendations":["추천"],"differentiation_tips":["전략"]}"""
    results["pattern"] = call_ai(system2, f"산업:{industry} 고객:{customer_type}\nRFP:{rfp_text[:3000]}", mock_type="pattern")
    steps_done.append("pattern")

    # Step 3: Proposal
    system3 = """제출 가능한 수준의 제안서 초안을 작성하세요. 각 섹션 200자 이상, 구체적 수치 포함, 한국어 비즈니스 문체.
필수 섹션: 제안개요, 현황분석, 제안솔루션, 수행방안, 수행실적, 유지보수, 투자비용.
JSON으로만 반환. 마크다운 코드블록 없이:
{"title":"제목","table_of_contents":["1. 제안 개요","2. 현황 분석","3. 제안 솔루션","4. 수행 방안","5. 수행 실적","6. 유지보수","7. 투자 비용"],"sections":{"1. 제안 개요":"내용","2. 현황 분석":"내용","3. 제안 솔루션":"내용","4. 수행 방안":"내용","5. 수행 실적":"내용","6. 유지보수":"내용","7. 투자 비용":"내용"}}"""
    results["proposal"] = call_ai(system3, f"회사:{company_info}\nRFP:{rfp_text}", mock_type="proposal")
    steps_done.append("proposal")

    # Step 4: Review
    system4 = """제안서를 리뷰하여 JSON으로만 반환. 마크다운 코드블록 없이:
{"overall_score":78,"grade":"B+","review_items":[{"category":"항목","score":80,"status":"양호","comment":"코멘트"}],"missing_requirements":["누락"],"logic_issues":["불일치"],"improvement_suggestions":["개선"]}"""
    results["review"] = call_ai(system4, f"제안서:{results['proposal'][:4000]}\nRFP:{rfp_text[:3000]}", mock_type="review")
    steps_done.append("review")

    # Step 5: Strategy
    system5 = """Go/No-Go를 JSON으로만 반환. 마크다운 코드블록 없이:
{"recommendation":"GO","confidence":"75%","analysis":{"market_fit":{"score":80,"comment":"설명"}},"key_factors":["요인"],"win_strategy":["전략"]}"""
    results["strategy"] = call_ai(system5, f"회사:{company_info}\nRFP:{rfp_text[:3000]}", mock_type="strategy")
    steps_done.append("strategy")

    db.upsert_pipeline(rfp_id, {s: True for s in steps_done}, results)
    for step in steps_done:
        record_history(rfp_id, step, results[step])
    log_activity("파이프라인 완료", f"{rfp['filename']} - {len(steps_done)}단계")
    await ws_manager.notify("파이프라인 완료", f"{rfp['filename']} - {len(steps_done)}단계")

    return JSONResponse({"rfp_id": rfp_id, "steps_completed": steps_done, "results": results})


# ─── 9. AI 경쟁력 채점 ───

@app.post("/api/score-proposal")
async def score_proposal(request: Request, proposal_text: str = Form("")):
    require_user(request)
    system = """당신은 제안서 경쟁력 채점 AI입니다.
제안서를 분석하고 반드시 아래 JSON 형식으로만 반환하세요. 마크다운 코드블록 없이 순수 JSON만:
{"total_score":82,"max_score":100,"grade":"A","categories":[{"name":"기술 이해도","score":85,"max":100,"feedback":"피드백"},{"name":"실현 가능성","score":78,"max":100,"feedback":"피드백"},{"name":"차별화","score":70,"max":100,"feedback":"피드백"},{"name":"문서 완성도","score":80,"max":100,"feedback":"피드백"},{"name":"가격 경쟁력","score":75,"max":100,"feedback":"피드백"}],"strengths":["강점1"],"weaknesses":["약점1"],"win_probability":"65%"}"""

    result = call_ai(system, f"다음 제안서의 경쟁력을 채점해주세요:\n\n{proposal_text[:6000]}", mock_type="")
    if not result or "분석이 완료" in result:
        result = json.dumps({
            "total_score": 82, "max_score": 100, "grade": "A-",
            "categories": [
                {"name": "기술 이해도", "score": 85, "max": 100, "feedback": "요구사항에 대한 기술적 이해가 높음"},
                {"name": "실현 가능성", "score": 78, "max": 100, "feedback": "일정 계획이 다소 낙관적"},
                {"name": "차별화", "score": 70, "max": 100, "feedback": "경쟁사 대비 뚜렷한 차별점 보강 필요"},
                {"name": "문서 완성도", "score": 88, "max": 100, "feedback": "체계적 구성, 시각자료 추가 권장"},
                {"name": "가격 경쟁력", "score": 75, "max": 100, "feedback": "시장 평균 수준, 가치 설명 보강 필요"},
            ],
            "strengths": ["기술 역량 충분", "체계적 문서 구성", "풍부한 레퍼런스"],
            "weaknesses": ["차별화 포인트 부족", "일정 리스크", "가격 근거 불충분"],
            "win_probability": "65%",
        }, ensure_ascii=False, indent=2)
    log_activity("경쟁력 채점", "AI 채점 완료")
    return JSONResponse({"score": result})


# ─── 결과 조회 API ───

@app.get("/api/results/{rfp_id}")
async def get_results(request: Request, rfp_id: str):
    check_rfp_access(request, rfp_id)
    data = db.get_pipeline(rfp_id)
    return JSONResponse({
        "results": data.get("results", {}),
        "steps": list(data.get("completed_steps", {}).keys()),
    })


# ─── 10. 제안서 버전 관리 ───

@app.post("/api/version/save")
async def save_version(
    request: Request,
    rfp_id: str = Form(...),
    content: str = Form(...),
    score: int = Form(0),
    note: str = Form(""),
):
    check_rfp_access(request, rfp_id)
    ver_num = db.count_versions(rfp_id) + 1
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    db.insert_version(rfp_id, ver_num, content, score, note, created_at)
    version = {"version": ver_num, "content": content, "score": score, "note": note, "created_at": created_at}
    log_activity("버전 저장", f"v{ver_num} (점수:{score})")
    return JSONResponse({"saved": True, "version": version, "total": ver_num})


@app.get("/api/version/list/{rfp_id}")
async def list_versions_api(request: Request, rfp_id: str):
    check_rfp_access(request, rfp_id)
    versions = db.list_versions(rfp_id)
    return JSONResponse({"versions": versions})


# ─── 11. 팀 협업 ───

@app.post("/api/team/init")
async def team_init(request: Request, rfp_id: str = Form(...)):
    check_rfp_access(request, rfp_id)
    return JSONResponse(db.get_team(rfp_id))


@app.post("/api/team/add-member")
async def add_member(request: Request, rfp_id: str = Form(...), name: str = Form(...), role: str = Form("")):
    check_rfp_access(request, rfp_id)
    member_id = str(uuid.uuid4())[:6]
    db.add_team_member(rfp_id, member_id, name, role)
    log_activity("팀원 추가", f"{name} ({role})")
    await ws_manager.notify("팀원 추가", f"{name} ({role})")
    return JSONResponse({"members": db.get_team_members(rfp_id)})


@app.post("/api/team/remove-member")
async def remove_member(request: Request, rfp_id: str = Form(...), member_id: str = Form(...)):
    check_rfp_access(request, rfp_id)
    existed = db.remove_team_member(rfp_id, member_id)
    if existed:
        log_activity("팀원 삭제", f"ID: {member_id}")
        await ws_manager.notify("팀원 삭제", member_id)
    return JSONResponse({"members": db.get_team_members(rfp_id)})


@app.post("/api/team/add-section")
async def add_section(
    request: Request,
    rfp_id: str = Form(...),
    title: str = Form(...),
    assignee: str = Form(""),
):
    check_rfp_access(request, rfp_id)
    section_id = str(uuid.uuid4())[:6]
    db.add_team_section(rfp_id, section_id, title, assignee)
    team = db.get_team(rfp_id)
    return JSONResponse({"sections": team["sections"]})


@app.post("/api/team/update-assignee")
async def update_assignee(
    request: Request,
    rfp_id: str = Form(...),
    section_id: str = Form(...),
    assignee: str = Form(...),
):
    check_rfp_access(request, rfp_id)
    db.update_section_assignee(section_id, assignee)
    return JSONResponse({"ok": True})


@app.post("/api/team/auto-assign")
async def auto_assign(
    request: Request,
    rfp_id: str = Form(...),
):
    check_rfp_access(request, rfp_id)
    if not db.rfp_exists(rfp_id):
        raise HTTPException(404, "RFP를 찾을 수 없습니다.")
    meta = db.get_rfp_meta(rfp_id)
    rfp_text = extract_pdf_text(meta["filepath"])[:6000] if meta and Path(meta["filepath"]).exists() else ""
    members = db.get_team_members(rfp_id)
    member_info = ", ".join([f"{m['name']}({m['role']})" for m in members]) if members else "(팀원 미등록 — 역할명으로 배정)"

    system = f"""당신은 프로젝트 매니저입니다.
RFP를 분석하여 제안서 작성에 필요한 섹션과 담당자를 자동 배정하세요.

등록된 팀원: {member_info}

규칙:
1. RFP 요구사항을 기반으로 제안서에 필요한 섹션(7~12개)을 도출
2. 각 섹션에 가장 적합한 팀원(또는 역할)을 배정
3. 팀원이 없으면 "PM", "기술리드", "아키텍트", "기획자", "디자이너" 등 역할명으로 배정

반드시 아래 JSON 형식으로만 반환. 마크다운 코드블록 없이 순수 JSON:
{{"sections":[{{"title":"섹션명","assignee":"담당자명","reason":"배정 사유"}}]}}"""

    result = call_ai(system, rfp_text, mock_type="")
    if not result or "분석이 완료" in result:
        result = json.dumps({"sections": [
            {"title": "1. 제안 개요", "assignee": "PM", "reason": "전체 방향 설정"},
            {"title": "2. 현황 분석", "assignee": "기획자", "reason": "고객 환경 분석"},
            {"title": "3. 시스템 아키텍처", "assignee": "아키텍트", "reason": "기술 설계"},
            {"title": "4. 핵심 기능 상세", "assignee": "기술리드", "reason": "기능 구현 방안"},
            {"title": "5. 수행 방안", "assignee": "PM", "reason": "일정/품질 관리"},
            {"title": "6. 수행 실적", "assignee": "기획자", "reason": "레퍼런스 정리"},
            {"title": "7. 투입 인력", "assignee": "PM", "reason": "인력 계획"},
            {"title": "8. 유지보수", "assignee": "기술리드", "reason": "기술 지원 계획"},
            {"title": "9. 투자 비용", "assignee": "PM", "reason": "비용 산정"},
        ]}, ensure_ascii=False)

    # Parse and apply
    parsed = None
    try:
        stripped = result.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(stripped)
    except Exception:
        match = re.search(r'\{[\s\S]*\}', result)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except Exception:
                pass

    new_sections = []
    if parsed and "sections" in parsed:
        for s in parsed["sections"]:
            new_sections.append({
                "id": str(uuid.uuid4())[:6],
                "title": s.get("title", ""),
                "assignee": s.get("assignee", ""),
                "reason": s.get("reason", ""),
                "status": "대기",
                "comments": [],
            })
        db.replace_team_sections(rfp_id, new_sections)

    log_activity("AI 자동 배정", f"{len(new_sections)}개 섹션")
    return JSONResponse({"sections": new_sections, "raw": result})


@app.post("/api/team/update-status")
async def update_section_status(
    request: Request,
    rfp_id: str = Form(...),
    section_id: str = Form(...),
    status: str = Form(...),
):
    check_rfp_access(request, rfp_id)
    title = db.update_section_status(section_id, status)
    if title:
        await ws_manager.notify("섹션 상태 변경", f"{title} → {status}")
    return JSONResponse({"ok": True})


@app.post("/api/team/add-comment")
async def add_comment(
    request: Request,
    rfp_id: str = Form(...),
    section_id: str = Form(...),
    author: str = Form(...),
    text: str = Form(...),
):
    check_rfp_access(request, rfp_id)
    db.add_section_comment(section_id, author, text, datetime.now().strftime("%H:%M"))
    await ws_manager.notify("코멘트 추가", f"{author}: {text[:30]}")
    return JSONResponse({"ok": True})


@app.get("/api/team/{rfp_id}")
async def get_team_api(request: Request, rfp_id: str):
    check_rfp_access(request, rfp_id)
    return JSONResponse(db.get_team(rfp_id))


# ─── 12. 자동 일정 생성 ───

@app.post("/api/schedule/generate")
async def generate_schedule(
    request: Request,
    deadline: str = Form(...),
    rfp_id: str = Form(None),
):
    require_user(request)
    if rfp_id:
        check_rfp_access(request, rfp_id)
    try:
        dl = datetime.strptime(deadline, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "날짜 형식: YYYY-MM-DD")

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    total_days = (dl - today).days
    if total_days < 1:
        raise HTTPException(400, "마감일은 오늘 이후여야 합니다.")

    phases = [
        {"name": "RFP 분석 및 전략 수립", "ratio": 0.15, "tasks": ["RFP 업로드/구조화 분석", "Go/No-Go 의사결정", "Winning 패턴 분석", "팀 구성 및 역할 배정"]},
        {"name": "제안서 초안 작성", "ratio": 0.35, "tasks": ["AI 초안 생성", "섹션별 담당자 작성", "기술 솔루션 상세화", "레퍼런스/사례 정리"]},
        {"name": "내부 리뷰 및 수정", "ratio": 0.25, "tasks": ["Red Team AI 리뷰", "요구사항 누락 체크", "논리 일관성 검토", "경쟁력 채점 및 보완"]},
        {"name": "최종 검수 및 제출", "ratio": 0.15, "tasks": ["디자인/레이아웃 최종화", "PDF/DOCX 생성", "경영진 승인", "제출"]},
        {"name": "버퍼 (예비)", "ratio": 0.10, "tasks": ["긴급 수정 대응", "최종 검토"]},
    ]

    schedule = []
    current = today
    for phase in phases:
        days = max(1, round(total_days * phase["ratio"]))
        end = min(current + timedelta(days=days), dl)
        schedule.append({
            "phase": phase["name"],
            "start": current.strftime("%Y-%m-%d"),
            "end": end.strftime("%Y-%m-%d"),
            "days": (end - current).days,
            "tasks": phase["tasks"],
        })
        current = end

    log_activity("일정 생성", f"마감: {deadline} ({total_days}일)")
    return JSONResponse({
        "deadline": deadline,
        "total_days": total_days,
        "schedule": schedule,
    })


# ─── 13. PDF 제안서 생성 ───

@app.post("/api/export-pdf")
async def export_pdf(request: Request, proposal_text: str = Form("")):
    require_user(request)
    import io
    from fpdf import FPDF

    parsed = None
    try:
        stripped = proposal_text.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(stripped)
    except Exception:
        match = re.search(r'\{[\s\S]*\}', proposal_text)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except Exception:
                pass

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)

    # Load Korean font
    font_path = Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf")
    has_korean_font = font_path.exists()
    if has_korean_font:
        pdf.add_font("Nanum", "", str(font_path), uni=True)
        pdf.add_font("Nanum", "B", str(font_path.parent / "NanumGothicBold.ttf"), uni=True)
        body_font, bold_font = "Nanum", "Nanum"
    else:
        body_font, bold_font = "Helvetica", "Helvetica"

    # Cover page
    pdf.add_page()
    pdf.set_fill_color(30, 58, 138)
    pdf.rect(0, 0, 210, 100, 'F')
    pdf.set_y(30)
    pdf.set_font(bold_font, "B", 28)
    pdf.set_text_color(255, 255, 255)
    title = parsed.get("title", "Proposal") if parsed else "Proposal"
    pdf.cell(0, 15, title, ln=True, align="C")
    pdf.set_font(body_font, "", 14)
    pdf.cell(0, 10, datetime.now().strftime("%Y-%m-%d"), ln=True, align="C")
    pdf.set_y(110)
    pdf.set_text_color(60, 60, 60)
    pdf.set_font(body_font, "", 11)
    pdf.cell(0, 8, "Generated by RFP AI Analyzer", ln=True, align="C")

    if parsed:
        # TOC page
        toc = parsed.get("table_of_contents", [])
        if toc:
            pdf.add_page()
            pdf.set_font(bold_font, "B", 18)
            pdf.set_text_color(30, 58, 138)
            pdf.cell(0, 12, "Table of Contents", ln=True)
            pdf.ln(4)
            pdf.set_font(body_font, "", 12)
            pdf.set_text_color(50, 50, 50)
            for item in toc:
                prefix = "    " if item.startswith("  ") else ""
                pdf.cell(0, 8, f"{prefix}{item.strip()}", ln=True)

        # Sections
        sections = parsed.get("sections", {})
        for sec_title, sec_content in sections.items():
            pdf.add_page()
            pdf.set_font(bold_font, "B", 16)
            pdf.set_text_color(30, 58, 138)
            pdf.cell(0, 12, sec_title, ln=True)
            pdf.ln(2)
            pdf.set_draw_color(30, 58, 138)
            pdf.line(10, pdf.get_y(), 200, pdf.get_y())
            pdf.ln(6)
            pdf.set_font(body_font, "", 11)
            pdf.set_text_color(50, 50, 50)
            pdf.multi_cell(0, 7, sec_content)
    else:
        pdf.add_page()
        pdf.set_font(body_font, "", 11)
        pdf.set_text_color(50, 50, 50)
        pdf.multi_cell(0, 7, proposal_text[:5000])

    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)

    log_activity("PDF Export", "제안서 PDF 생성")
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=proposal.pdf"},
    )


@app.get("/api/history/{rfp_id}")
async def get_history_api(request: Request, rfp_id: str, step: str = None):
    check_rfp_access(request, rfp_id)
    items = db.get_history(rfp_id, step)
    return JSONResponse({"history": items})


@app.get("/api/rfp-list")
async def rfp_list_api(request: Request):
    user = require_user(request)
    owner_id = None if user.get("is_admin") else user["id"]
    rfps = db.list_rfps(owner_id)
    pipelines = db.list_pipelines()
    items = []
    for v in rfps:
        steps = list(pipelines.get(v["id"], {}).get("completed_steps", {}).keys())
        items.append({"id": v["id"], "filename": v["filename"], "text_length": v["text_length"],
                       "uploaded_at": v.get("uploaded_at", ""), "steps_done": len(steps)})
    return JSONResponse({"rfps": items})


@app.delete("/api/rfp/{rfp_id}")
async def delete_rfp(request: Request, rfp_id: str):
    check_rfp_access(request, rfp_id)
    meta = db.get_rfp_meta(rfp_id)
    fname = meta["filename"] if meta else ""
    filepath = db.delete_rfp(rfp_id)
    if filepath and Path(filepath).exists():
        Path(filepath).unlink(missing_ok=True)
    log_activity("RFP 삭제", fname)
    return JSONResponse({"ok": True})


@app.get("/api/rfp-detail/{rfp_id}")
async def rfp_detail(request: Request, rfp_id: str):
    check_rfp_access(request, rfp_id)
    rfp = db.get_rfp_meta(rfp_id)
    if not rfp:
        raise HTTPException(404, "RFP를 찾을 수 없습니다.")
    pipeline = db.get_pipeline(rfp_id)
    return JSONResponse({
        "rfp": {"id": rfp["id"], "filename": rfp["filename"], "text_length": rfp["text_length"], "uploaded_at": rfp.get("uploaded_at", "")},
        "steps_done": list(pipeline.get("completed_steps", {}).keys()),
        "results": pipeline.get("results", {}),
        "versions": db.list_versions(rfp_id),
        "team": db.get_team(rfp_id),
        "proposals": db.list_proposals_by_rfp(rfp_id),
    })


@app.websocket("/ws/{username}")
async def websocket_endpoint(ws: WebSocket, username: str):
    uid = ws.session.get("uid") if hasattr(ws, "session") else None
    if not uid:
        await ws.close(code=1008)
        return
    user = db.get_user_by_id(uid)
    if not user:
        await ws.close(code=1008)
        return
    # Force username to match the authenticated user — ignore URL spoofing
    safe_username = user["username"]
    await ws_manager.connect(ws, safe_username)
    try:
        while True:
            await ws.receive_text()  # keep alive
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)
        await ws_manager.broadcast_users()


@app.on_event("startup")
async def startup():
    db.init_db()
    print(f"[Startup] DB ready. {db.count_rfps()} RFPs, {db.count_proposals()} proposals, {db.count_knowledge()} knowledge items")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
