// ─── State ───
let currentRfpId = null;
let rfpList = [];
let cachedResults = {};
let activeControllers = {};
let currentUsername = '';
let ws = null;

// ─── Username & WebSocket ───
function confirmUsername() {
    const input = el('usernameInput');
    const name = input.value.trim();
    if (!name) { input.focus(); return; }
    currentUsername = name;
    localStorage.setItem('rfp_username', name);
    el('usernameModal').classList.remove('show');
    connectWebSocket(name);
}

let wsRetryCount = 0;
function connectWebSocket(username) {
    try {
        const proto = location.protocol === 'https:' ? 'wss' : 'ws';
        const basePath = location.pathname.endsWith('/') ? location.pathname : location.pathname + '/';
        ws = new WebSocket(`${proto}://${location.host}${basePath}ws/${encodeURIComponent(username)}`);
        ws.onopen = () => { wsRetryCount = 0; };
        ws.onmessage = (e) => {
            try {
                const data = JSON.parse(e.data);
                if (data.type === 'users') {
                    el('onlineCount').textContent = data.count;
                    const container = el('onlineUsers');
                    const avatars = data.users.slice(0, 5).map(u =>
                        `<span title="${u}" style="width:24px;height:24px;border-radius:50%;background:var(--primary);color:white;display:inline-flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;margin-left:-4px;border:2px solid var(--bg)">${u.charAt(0)}</span>`
                    ).join('');
                    const extra = data.count > 5 ? `<span style="font-size:11px;color:var(--text-muted)">+${data.count - 5}</span>` : '';
                    container.innerHTML = `<span style="width:8px;height:8px;border-radius:50%;background:var(--success);animation:pulse 2s infinite"></span>${avatars}${extra}<span style="font-size:12px;color:var(--text-muted)">${data.count}명</span>`;
                } else if (data.type === 'activity') {
                    if (data.username !== currentUsername) {
                        showToast(`${data.username || ''} ${data.action}: ${data.detail}`, 'warning');
                    }
                    refreshDashboard();
                }
            } catch {}
        };
        ws.onerror = () => {};
        ws.onclose = () => {
            wsRetryCount++;
            const delay = Math.min(wsRetryCount * 5000, 30000);
            setTimeout(() => connectWebSocket(username), delay);
        };
    } catch {
        // WebSocket not supported or blocked — app works fine without it
    }
}

// ─── Mobile Sidebar ───
function toggleSidebar() {
    document.querySelector('.sidebar').classList.toggle('open');
    document.getElementById('sidebarOverlay').classList.toggle('show');
}

// ─── Navigation ───
function switchTab(tabId) {
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
    document.getElementById(tabId).classList.add('active');
    const navBtn = document.querySelector(`[data-tab="${tabId}"]`);
    if (navBtn) navBtn.classList.add('active');
    document.querySelector('.topbar-title').textContent = navBtn?.querySelector('.nav-text')?.textContent || '';
    if (tabId === 'tab-dashboard') refreshDashboard();
    const tabMap = { 'tab-analyze': 'analyze', 'tab-pattern': 'pattern', 'tab-proposal': 'proposal', 'tab-review': 'review', 'tab-strategy': 'strategy', 'tab-estimate': 'estimate' };
    const cacheKey = tabMap[tabId];
    if (cacheKey && cachedResults[cacheKey]) showCachedResult(cacheKey);
    if (cacheKey) refreshHistoryBar(cacheKey);
    document.querySelector('.sidebar').classList.remove('open');
    document.getElementById('sidebarOverlay')?.classList.remove('show');
}

function showCachedResult(key) {
    const data = cachedResults[key];
    if (!data) return;
    const renderers = {
        analyze: () => { const p = parseJsonSafe(data); if (p) renderAnalysis(p); else el('analyzeResult').innerHTML = pre(data); el('analyzeResult').classList.add('visible'); },
        pattern: () => { const p = parseJsonSafe(data); if (p) renderPattern(p); else el('patternResult').innerHTML = pre(data); el('patternResult').classList.add('visible'); },
        proposal: () => { const p = parseJsonSafe(data); renderProposal(p, data); el('proposalResult').classList.add('visible'); },
        review: () => { const p = parseJsonSafe(data); if (p) renderReview(p); else el('reviewResult').innerHTML = pre(data); el('reviewResult').classList.add('visible'); },
        strategy: () => { const p = parseJsonSafe(data); if (p) renderStrategy(p); else el('strategyResult').innerHTML = pre(data); el('strategyResult').classList.add('visible'); },
    };
    if (renderers[key]) renderers[key]();
}
function el(id) { return document.getElementById(id); }
function pre(text) { return `<pre style="white-space:pre-wrap;color:var(--text-dim)">${text}</pre>`; }


// ─── Toast ───
function showToast(msg, type = 'success') {
    const t = el('toast'); t.textContent = msg;
    const c = { success:'linear-gradient(135deg,#10b981,#059669)', error:'linear-gradient(135deg,#ef4444,#dc2626)', warning:'linear-gradient(135deg,#f59e0b,#d97706)' };
    t.style.background = c[type] || c.success; t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 3000);
}

// ─── Loading ───
function showLoading(id) { const e = el(id); if (e) e.classList.add('active'); }
function hideLoading(id) { const e = el(id); if (e) e.classList.remove('active'); }

// ─── Button state: action → cancel → action ───
function setBtnLoading(btnId, cancelKey) {
    const btn = el(btnId);
    if (!btn) return;
    btn._origHTML = btn.innerHTML;
    btn._origClass = btn.className;
    btn.innerHTML = '&#9724; 취소';
    btn.className = 'btn btn-danger';
    btn.onclick = () => cancelRequest(cancelKey, btnId);
}
function setBtnDone(btnId) {
    const btn = el(btnId);
    if (!btn) return;
    btn.innerHTML = btn._origHTML || btn.innerHTML;
    btn.className = btn._origClass || btn.className;
    btn.onclick = null; // will be re-assigned by next line based on original
    // Re-assign original handlers
    const handlers = {
        'btnAnalyze': () => analyzeRfp(),
        'btnPattern': () => analyzePattern(),
        'btnProposal': () => generateProposal(),
        'btnReview': () => reviewProposal(),
        'btnStrategy': () => analyzeStrategy(),
        'pipelineBtn': () => runPipeline(),
    };
    if (handlers[btnId]) btn.onclick = handlers[btnId];
}
function cancelRequest(key, btnId) {
    if (activeControllers[key]) {
        activeControllers[key].abort();
        delete activeControllers[key];
        showToast('요청이 취소되었습니다.', 'warning');
    }
    if (btnId) setBtnDone(btnId);
}

// ─── RFP Selectors ───
function updateRfpSelectors() {
    document.querySelectorAll('.rfp-select').forEach(sel => {
        const val = sel.value;
        sel.innerHTML = '<option value="">-- RFP 선택 --</option>';
        rfpList.forEach(r => { sel.innerHTML += `<option value="${r.id}" ${r.id===currentRfpId?'selected':''}>${r.filename} (${r.text_length.toLocaleString()}자)</option>`; });
        if (val) sel.value = val;
    });
}

// ─── API Helper ───
async function apiPost(url, formData, abortKey = null) {
    const ctrl = new AbortController();
    if (abortKey) { if (activeControllers[abortKey]) activeControllers[abortKey].abort(); activeControllers[abortKey] = ctrl; }
    const resp = await fetch(url, { method: 'POST', body: formData, signal: ctrl.signal });
    if (abortKey) delete activeControllers[abortKey];
    if (!resp.ok) { const err = await resp.json().catch(() => ({ detail: '서버 오류' })); throw new Error(err.detail || '요청 실패'); }
    return resp.json();
}

function parseJsonSafe(text) {
    if (typeof text === 'object') return text;
    try { return JSON.parse(text); } catch {}
    const s = text.replace(/```json\s*/g, '').replace(/```\s*/g, '').trim();
    try { return JSON.parse(s); } catch {}
    const m = s.match(/\{[\s\S]*\}/);
    if (m) { try { return JSON.parse(m[0]); } catch {} }
    return null;
}

// ─── Load cached results from server ───
async function loadServerResults(rfpId) {
    if (!rfpId) return;
    try {
        const resp = await fetch(`/api/rfp-detail/${rfpId}`);
        const data = await resp.json();
        if (data.results) Object.entries(data.results).forEach(([k, v]) => { cachedResults[k] = v; });
        // Auto-fill review tab with proposal text if available
        if (cachedResults.proposal) {
            const reviewBox = el('proposalTextForReview');
            if (reviewBox && !reviewBox.value) {
                const p = parseJsonSafe(cachedResults.proposal);
                if (p && p.sections) {
                    reviewBox.value = Object.entries(p.sections).map(([t, c]) => `[${t}]\n${c}`).join('\n\n');
                } else {
                    reviewBox.value = cachedResults.proposal.substring(0, 3000);
                }
            }
        }
    } catch {}
}

async function selectRfp(rfpId) {
    currentRfpId = rfpId;
    cachedResults = {};
    updateRfpSelectors();
    await loadServerResults(rfpId);
    // Show cached on current visible tab
    const activeTab = document.querySelector('.tab-content.active')?.id;
    const tabMap = { 'tab-analyze': 'analyze', 'tab-pattern': 'pattern', 'tab-proposal': 'proposal', 'tab-review': 'review', 'tab-strategy': 'strategy', 'tab-estimate': 'estimate' };
    if (tabMap[activeTab] && cachedResults[tabMap[activeTab]]) showCachedResult(tabMap[activeTab]);
    refreshDashboard();
    showToast('RFP 선택됨');
}

async function deleteRfp(rfpId) {
    if (!confirm('이 RFP와 관련된 모든 데이터(제안서, 버전, 팀, 파이프라인)가 삭제됩니다. 계속하시겠습니까?')) return;
    try {
        await fetch(`/api/rfp/${rfpId}`, { method: 'DELETE' });
        if (currentRfpId === rfpId) { currentRfpId = null; cachedResults = {}; }
        rfpList = rfpList.filter(r => r.id !== rfpId);
        updateRfpSelectors();
        refreshDashboard();
        showToast('RFP 삭제 완료');
    } catch (e) { showToast('삭제 실패', 'error'); }
}

// ─── Dashboard ───
async function refreshDashboard() {
    try {
        const resp = await fetch('api/dashboard');
        const data = await resp.json();
        el('statRfp').textContent = data.rfp_count;
        el('statProposal').textContent = data.proposal_count;
        el('statKnowledge').textContent = data.knowledge_count;
        el('statPipeline').textContent = data.pipeline_count;
        const logEl = el('activityLog');
        if (data.activity_log.length === 0) {
            logEl.innerHTML = '<p style="color:var(--text-muted);text-align:center;padding:20px">아직 활동이 없습니다. RFP를 업로드하여 시작하세요.</p>';
        } else {
            logEl.innerHTML = data.activity_log.map(a => `<div class="activity-item"><span class="activity-time">${a.time}</span><span class="activity-dot"></span><span class="activity-text"><strong>${a.action}</strong> ${a.detail}</span></div>`).join('');
        }
        updatePipelineVisual(data.pipeline_status);

        // Auto-select first RFP if none selected
        if (!currentRfpId && data.recent_rfps?.length) {
            currentRfpId = data.recent_rfps[data.recent_rfps.length - 1].id;
            rfpList = data.recent_rfps;
            updateRfpSelectors();
            loadServerResults(currentRfpId);
        }

        // RFP Management list
        renderRfpList();
    } catch {}
}

async function renderRfpList() {
    const resp = await fetch('api/rfp-list');
    const data = await resp.json();
    rfpList = data.rfps;
    updateRfpSelectors();
    const listEl = el('rfpManageList');
    if (!listEl) return;
    if (!data.rfps.length) { listEl.innerHTML = '<p style="color:var(--text-muted);text-align:center;padding:16px">등록된 RFP가 없습니다.</p>'; return; }
    const stepLabels = { 0: '미분석', 1: '분석중', 2: '분석중', 3: '분석중', 4: '분석중', 5: '완료' };
    listEl.innerHTML = data.rfps.map(r => {
        const isActive = r.id === currentRfpId;
        const border = isActive ? 'border-left:3px solid var(--primary)' : 'border-left:3px solid var(--border)';
        const stepsText = r.steps_done >= 5 ? '<span style="color:#10b981">5/5 완료</span>' : `<span style="color:#f59e0b">${r.steps_done}/5</span>`;
        return `<div class="knowledge-item" style="${border};display:flex;align-items:center;justify-content:space-between;gap:12px;cursor:pointer" onclick="selectRfp('${r.id}')">
            <div style="flex:1;min-width:0">
                <div style="font-weight:600;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${isActive ? '&#9654; ' : ''}${r.filename}</div>
                <div style="font-size:12px;color:var(--text-muted)">${r.text_length.toLocaleString()}자 | 파이프라인: ${stepsText}</div>
            </div>
            <div style="display:flex;gap:6px;flex-shrink:0">
                <button class="btn btn-outline" style="font-size:11px;padding:3px 10px" onclick="event.stopPropagation();selectRfp('${r.id}');switchTab('tab-pipeline')">분석</button>
                <button class="btn btn-danger" style="font-size:11px;padding:3px 10px" onclick="event.stopPropagation();deleteRfp('${r.id}')">삭제</button>
            </div>
        </div>`;
    }).join('');
}

function updatePipelineVisual(status) {
    const steps = ['analyze', 'pattern', 'proposal', 'review', 'strategy'];
    const done = currentRfpId && status[currentRfpId] ? status[currentRfpId].steps : [];
    steps.forEach((s, i) => {
        const e = el(`pipe-${s}`); if (e) e.className = 'pipe-step' + (done.includes(s) ? ' done' : '');
        if (i < steps.length - 1) { const c = el(`conn-${i}`); if (c) c.className = 'pipe-connector' + (done.includes(s) ? ' done' : ''); }
    });
}

// ─── Upload ───
function initUpload() {
    const area = el('uploadArea'), input = el('fileInput');
    area.addEventListener('click', () => input.click());
    area.addEventListener('dragover', e => { e.preventDefault(); area.classList.add('dragover'); });
    area.addEventListener('dragleave', () => area.classList.remove('dragover'));
    area.addEventListener('drop', e => { e.preventDefault(); area.classList.remove('dragover'); if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]); });
    input.addEventListener('change', () => { if (input.files.length) handleFile(input.files[0]); });
}

async function handleFile(file) {
    if (!file.name.toLowerCase().endsWith('.pdf')) { showToast('PDF 파일만 업로드 가능합니다.', 'error'); return; }
    document.querySelector('.upload-filename').textContent = file.name;
    showLoading('uploadSpinner');
    const fd = new FormData(); fd.append('file', file);
    try {
        const data = await apiPost('api/upload-rfp', fd);
        currentRfpId = data.rfp_id;
        rfpList.push({ id: data.rfp_id, filename: data.filename, text_length: data.text_length });
        updateRfpSelectors(); cachedResults = {};
        el('uploadResult').classList.add('visible');
        el('uploadPreview').textContent = data.preview || '(텍스트 없음)';
        el('uploadInfo').innerHTML = `<strong>파일:</strong> ${data.filename} | <strong>텍스트:</strong> ${data.text_length.toLocaleString()}자 | <strong>ID:</strong> ${data.rfp_id}`;
        showToast('RFP 업로드 완료!');
    } catch (e) { showToast(e.message, 'error'); }
    finally { hideLoading('uploadSpinner'); }
}

// ─── Analyze ───
async function analyzeRfp() {
    const rfpId = el('analyzeRfpSelect')?.value || currentRfpId;
    if (!rfpId) { showToast('RFP를 먼저 업로드해주세요.', 'warning'); return; }
    showLoading('analyzeSpinner'); setBtnLoading('btnAnalyze', 'analyze');
    el('analyzeResult').classList.remove('visible');
    const fd = new FormData(); fd.append('rfp_id', rfpId);
    try {
        const data = await apiPost('api/analyze-rfp', fd, 'analyze');
        cachedResults.analyze = data.analysis;
        const a = parseJsonSafe(data.analysis);
        if (a) renderAnalysis(a); else el('analyzeResult').innerHTML = pre(data.analysis);
        el('analyzeResult').classList.add('visible');
        showToast('RFP 구조화 분석 완료!'); refreshDashboard();
    } catch (e) { if (e.name !== 'AbortError') showToast(e.message, 'error'); }
    finally { hideLoading('analyzeSpinner'); setBtnDone('btnAnalyze'); refreshHistoryBar('analyze'); }
}

function renderAnalysis(data) {
    const e = el('analyzeResult'); let h = '';
    if (data.summary) h += `<div class="card"><p style="color:var(--text-dim)">${data.summary}</p></div>`;
    if (data.requirements?.length) {
        h += `<div class="section-title">요구사항 목록 (${data.requirements.length}건)</div><table class="data-table"><thead><tr><th>ID</th><th>분류</th><th>요구사항</th><th>구분</th><th>리스크</th></tr></thead><tbody>`;
        data.requirements.forEach(r => { h += `<tr><td style="color:var(--accent)">${r.id}</td><td>${r.category}</td><td>${r.description}</td><td><span class="badge ${r.priority==='필수'?'badge-required':'badge-optional'}">${r.priority}</span></td><td><span class="badge ${r.risk==='높음'?'badge-high':r.risk==='중간'?'badge-medium':'badge-low'}">${r.risk}</span></td></tr>`; });
        h += '</tbody></table>';
    }
    if (data.evaluation_criteria?.length) {
        h += `<div class="section-title">평가 기준</div><table class="data-table"><thead><tr><th>기준</th><th>배점</th><th>설명</th></tr></thead><tbody>`;
        data.evaluation_criteria.forEach(c => h += `<tr><td><strong>${c.criteria}</strong></td><td style="color:var(--accent)">${c.weight}</td><td style="color:var(--text-dim)">${c.description}</td></tr>`);
        h += '</tbody></table>';
    }
    if (data.risks?.length) {
        h += `<div class="section-title">리스크 항목</div>`;
        data.risks.forEach(r => { const b = r.level==='높음'?'badge-high':r.level==='중간'?'badge-medium':'badge-low'; h += `<div class="factor-item" style="border-left-color:${r.level==='높음'?'#ef4444':r.level==='중간'?'#f59e0b':'#10b981'}"><span class="badge ${b}">${r.level}</span> <strong>${r.risk}</strong> — ${r.description}</div>`; });
    }
    e.innerHTML = h;
}

// ─── Pattern ───
async function analyzePattern() {
    const rfpId = el('patternRfpSelect')?.value || currentRfpId;
    showLoading('patternSpinner'); setBtnLoading('btnPattern', 'pattern');
    el('patternResult').classList.remove('visible');
    const fd = new FormData();
    if (rfpId) fd.append('rfp_id', rfpId);
    fd.append('industry', el('patternIndustry').value); fd.append('customer_type', el('patternCustomer').value);
    try {
        const data = await apiPost('api/winning-pattern', fd, 'pattern');
        cachedResults.pattern = data.analysis;
        const a = parseJsonSafe(data.analysis);
        if (a) renderPattern(a); else el('patternResult').innerHTML = pre(data.analysis);
        el('patternResult').classList.add('visible'); showToast('패턴 분석 완료!');
    } catch (e) { if (e.name !== 'AbortError') showToast(e.message, 'error'); }
    finally { hideLoading('patternSpinner'); setBtnDone('btnPattern'); refreshHistoryBar('pattern'); }
}

function renderPattern(data) {
    const e = el('patternResult'); let h = '';
    if (data.industry_analysis) h += `<div class="card"><strong style="color:var(--accent)">산업 분석:</strong> <span style="color:var(--text-dim)">${data.industry_analysis}</span></div>`;
    if (data.winning_patterns?.length) { h += `<div class="section-title">Winning 패턴</div>`; data.winning_patterns.forEach(p => { const b = p.confidence==='높음'?'badge-low':p.confidence==='중간'?'badge-medium':'badge-high'; h += `<div class="factor-item" style="border-left-color:var(--primary)"><strong>${p.pattern}</strong> <span class="badge ${b}">${p.confidence}</span><br><span style="color:var(--text-muted);font-size:13px">${p.description}</span></div>`; }); }
    if (data.style_recommendations?.length) { h += `<div class="section-title">스타일 추천</div>`; data.style_recommendations.forEach(s => h += `<div class="factor-item" style="border-left-color:var(--accent)">${s}</div>`); }
    if (data.differentiation_tips?.length) { h += `<div class="section-title">차별화 전략</div>`; data.differentiation_tips.forEach(t => h += `<div class="factor-item" style="border-left-color:var(--success)">${t}</div>`); }
    e.innerHTML = h;
}

// ─── Proposal ───
function saveProposalInputs() {
    localStorage.setItem('rfp_companyInfo', el('companyInfo')?.value || '');
    localStorage.setItem('rfp_references', el('references')?.value || '');
}
function loadProposalInputs() {
    const ci = el('companyInfo'), rf = el('references');
    if (ci && !ci.value) ci.value = localStorage.getItem('rfp_companyInfo') || '';
    if (rf && !rf.value) rf.value = localStorage.getItem('rfp_references') || '';
}

async function generateProposal() {
    const rfpId = el('proposalRfpSelect')?.value || currentRfpId;
    saveProposalInputs();
    showLoading('proposalSpinner'); setBtnLoading('btnProposal', 'proposal');
    el('proposalResult').classList.remove('visible');
    const fd = new FormData();
    if (rfpId) fd.append('rfp_id', rfpId);
    fd.append('company_info', el('companyInfo').value); fd.append('references', el('references').value);
    try {
        const data = await apiPost('api/generate-proposal', fd, 'proposal');
        cachedResults.proposal = data.proposal;
        const p = parseJsonSafe(data.proposal);
        renderProposal(p, data.proposal);
        el('proposalResult').classList.add('visible'); showToast('제안서 초안 생성 완료!');
        // Auto-fill review tab
        const reviewBox = el('proposalTextForReview');
        if (reviewBox && p && p.sections) {
            reviewBox.value = Object.entries(p.sections).map(([t, c]) => `[${t}]\n${c}`).join('\n\n');
        } else if (reviewBox) { reviewBox.value = data.proposal.substring(0, 3000); }
        // Auto-fill version tab
        const verContent = el('versionContent');
        if (verContent) verContent.value = data.proposal.substring(0, 5000);
    } catch (e) { if (e.name !== 'AbortError') showToast(e.message, 'error'); }
    finally { hideLoading('proposalSpinner'); setBtnDone('btnProposal'); refreshHistoryBar('proposal'); }
}

// Store raw for download buttons
let lastProposalRaw = '';

function renderProposal(data, rawText) {
    lastProposalRaw = rawText || '';
    const e = el('proposalResult');
    let h = '';

    // Download buttons (always show)
    h += `<div class="btn-group" style="margin-bottom:20px;justify-content:flex-end;flex-wrap:wrap">
        <button class="btn btn-primary" onclick="downloadPptx(encodeURIComponent(lastProposalRaw))">PPT 다운로드</button>
        <button class="btn btn-success" onclick="downloadDocx(encodeURIComponent(lastProposalRaw))">DOCX 다운로드</button>
        <button class="btn btn-warning" onclick="downloadPdf(encodeURIComponent(lastProposalRaw))">PDF 다운로드</button>
        <button class="btn btn-outline" onclick="exportProposal(encodeURIComponent(lastProposalRaw))">Markdown</button>
        <button class="btn btn-outline" onclick="scoreProposal(encodeURIComponent(lastProposalRaw))">AI 경쟁력 채점</button>
    </div>`;

    if (data && data.title) {
        // ── Parsed JSON: rich view ──
        // Title
        h += `<div style="text-align:center;padding:32px 20px;background:linear-gradient(135deg,rgba(99,102,241,0.15),rgba(6,182,212,0.1));border-radius:16px;margin-bottom:24px">
            <h2 style="color:var(--primary-light);font-size:22px;margin-bottom:8px">${data.title}</h2>
            <p style="color:var(--text-muted);font-size:13px">${new Date().toLocaleDateString('ko-KR')} | AI 자동 생성</p>
        </div>`;

        // TOC
        if (data.table_of_contents?.length) {
            h += `<div class="card" style="margin-bottom:20px"><div class="card-title" style="font-size:16px">목차</div>`;
            let tocNum = 0;
            data.table_of_contents.forEach(t => {
                const isSub = t.startsWith('  ');
                if (!isSub) tocNum++;
                const style = isSub
                    ? 'padding:5px 0 5px 28px;color:var(--text-muted);font-size:13px;border-left:2px solid var(--border);margin-left:12px'
                    : 'padding:8px 0;font-weight:600;font-size:14px;color:var(--text)';
                h += `<div style="${style}">${t.trim()}</div>`;
            });
            h += '</div>';
        }

        // Sections
        if (data.sections) {
            const sectionColors = ['#6366f1','#06b6d4','#10b981','#f59e0b','#ef4444','#8b5cf6','#ec4899'];
            let secIdx = 0;
            Object.entries(data.sections).forEach(([title, content]) => {
                const color = sectionColors[secIdx % sectionColors.length];
                // Split content into paragraphs
                const paragraphs = content.split('\n').filter(p => p.trim());
                h += `<div style="margin-bottom:20px;border:1px solid var(--border);border-radius:14px;overflow:hidden">
                    <div style="background:${color}18;padding:14px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px">
                        <span style="width:8px;height:8px;border-radius:50%;background:${color};flex-shrink:0"></span>
                        <h4 style="color:${color};font-size:15px;margin:0">${title}</h4>
                    </div>
                    <div style="padding:18px 20px">`;
                paragraphs.forEach(p => {
                    h += `<p style="color:var(--text-dim);font-size:14px;line-height:1.9;margin-bottom:12px">${p.trim()}</p>`;
                });
                h += `</div></div>`;
                secIdx++;
            });
        }
    } else {
        // ── Raw text fallback: still render nicely ──
        const text = rawText || '';
        // Try to render markdown-like content
        const lines = text.replace(/```json/g,'').replace(/```/g,'').split('\n');
        h += `<div class="card"><div class="card-title">제안서 초안</div>`;
        lines.forEach(line => {
            const trimmed = line.trim();
            if (!trimmed) { h += '<br>'; return; }
            if (trimmed.startsWith('# ')) h += `<h2 style="color:var(--primary-light);margin:16px 0 8px">${trimmed.slice(2)}</h2>`;
            else if (trimmed.startsWith('## ')) h += `<h3 style="color:var(--accent);margin:14px 0 6px">${trimmed.slice(3)}</h3>`;
            else if (trimmed.startsWith('### ')) h += `<h4 style="color:var(--text);margin:12px 0 4px">${trimmed.slice(4)}</h4>`;
            else if (trimmed.startsWith('- ')) h += `<div style="padding:4px 0 4px 16px;color:var(--text-dim);font-size:14px">${trimmed}</div>`;
            else h += `<p style="color:var(--text-dim);font-size:14px;line-height:1.8;margin-bottom:8px">${trimmed}</p>`;
        });
        h += '</div>';
    }

    e.innerHTML = h;
}

// ─── Estimate ───
async function runEstimate() {
    const rfpId = el('estimateRfpSelect')?.value || currentRfpId;
    if (!rfpId) { showToast('RFP를 먼저 업로드해주세요.', 'warning'); return; }
    showLoading('estimateSpinner'); el('estimateResult').classList.remove('visible');
    setBtnLoading('btnEstimate');
    const fd = new FormData();
    fd.append('rfp_id', rfpId);
    fd.append('additional_info', el('estimateInfo')?.value || '');
    try {
        const data = await apiPost('api/estimate', fd);
        const est = parseJsonSafe(data.estimate);
        if (est) renderEstimate(est); else el('estimateResult').innerHTML = pre(data.estimate);
        el('estimateResult').classList.add('visible');
        showToast('견적 산출 완료!');
        refreshDashboard();
    } catch (e) { showToast(e.message, 'error'); }
    finally { hideLoading('estimateSpinner'); setBtnDone('btnEstimate'); refreshHistoryBar('estimate'); }
}

function renderEstimate(data) {
    const r = el('estimateResult');
    let h = '';

    // Summary header
    h += `<div style="text-align:center;margin-bottom:24px">
        <div style="font-size:14px;color:var(--text-muted)">${data.project_name || ''}</div>
        <div style="font-size:42px;font-weight:800;color:var(--accent);margin:8px 0">${data.total_cost || '산출 중'}</div>
        <div style="font-size:14px;color:var(--text-muted)">예상 기간: ${data.duration_months || '-'}개월</div>
        <div style="font-size:13px;color:var(--text-dim);margin-top:8px;max-width:600px;margin-left:auto;margin-right:auto">${data.summary || ''}</div>
    </div>`;

    // Cost breakdown bar chart
    if (data.categories?.length) {
        const colors = ['#6366f1', '#06b6d4', '#10b981', '#f59e0b', '#ef4444', '#ec4899'];
        const total = data.total_cost_number || data.categories.reduce((s, c) => s + (c.subtotal_number || 0), 0) || 1;

        // Horizontal proportion bar
        h += `<div style="display:flex;height:32px;border-radius:8px;overflow:hidden;margin-bottom:24px">`;
        data.categories.forEach((cat, i) => {
            const pct = total > 0 ? ((cat.subtotal_number || 0) / total * 100) : 25;
            h += `<div style="width:${pct}%;background:${colors[i % colors.length]};display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:600;color:white;min-width:40px" title="${cat.name}: ${cat.subtotal}">${cat.ratio || Math.round(pct) + '%'}</div>`;
        });
        h += `</div>`;

        // Legend
        h += `<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:24px;justify-content:center">`;
        data.categories.forEach((cat, i) => {
            h += `<div style="display:flex;align-items:center;gap:6px;font-size:13px">
                <span style="width:12px;height:12px;border-radius:3px;background:${colors[i % colors.length]}"></span>
                <span style="color:var(--text-dim)">${cat.name}</span>
                <span style="color:var(--text);font-weight:600">${cat.subtotal}</span>
            </div>`;
        });
        h += `</div>`;

        // Detailed tables per category
        data.categories.forEach((cat, ci) => {
            h += `<div class="section-title" style="border-color:${colors[ci % colors.length]}">${cat.name} — ${cat.subtotal} (${cat.ratio})</div>`;
            h += `<table class="data-table"><thead><tr>`;

            if (cat.name === '인건비') {
                h += `<th>역할</th><th>등급</th><th>인원</th><th>기간</th><th>월 단가</th><th>금액</th><th>사유</th>`;
                h += `</tr></thead><tbody>`;
                (cat.items || []).forEach(item => {
                    h += `<tr>
                        <td><strong>${item.role || item.item || ''}</strong></td>
                        <td><span class="badge badge-optional">${item.grade || '-'}</span></td>
                        <td style="text-align:center">${item.count || '-'}</td>
                        <td style="text-align:center">${item.months || '-'}개월</td>
                        <td style="color:var(--accent)">${item.unit_cost || '-'}</td>
                        <td style="color:var(--text);font-weight:600">${item.cost || '-'}</td>
                        <td style="font-size:12px;color:var(--text-muted);max-width:250px">${item.reason || ''}</td>
                    </tr>`;
                });
            } else {
                h += `<th>항목</th><th>금액</th><th>필요 사유</th>`;
                h += `</tr></thead><tbody>`;
                (cat.items || []).forEach(item => {
                    h += `<tr>
                        <td><strong>${item.item || item.role || ''}</strong></td>
                        <td style="color:var(--accent);font-weight:600">${item.cost || '-'}</td>
                        <td style="font-size:12px;color:var(--text-muted)">${item.reason || ''}</td>
                    </tr>`;
                });
            }
            h += `</tbody></table>`;
        });
    }

    // Risks
    if (data.risks?.length) {
        h += `<div class="section-title" style="border-color:#ef4444">비용 리스크</div>`;
        data.risks.forEach(r => {
            h += `<div class="factor-item" style="border-left-color:#ef4444">
                <strong>${r.risk}</strong> — <span style="color:#fca5a5">${r.impact}</span>
                <br><span style="font-size:12px;color:var(--text-muted)">대응: ${r.mitigation}</span>
            </div>`;
        });
    }

    // Assumptions
    if (data.assumptions?.length) {
        h += `<div class="section-title">전제 조건</div>`;
        data.assumptions.forEach(a => h += `<div class="factor-item" style="border-left-color:var(--accent)">${a}</div>`);
    }

    // Notes
    if (data.notes) {
        h += `<div style="margin-top:16px;padding:14px;background:rgba(99,102,241,0.08);border-radius:10px;font-size:13px;color:var(--text-dim)">${data.notes}</div>`;
    }

    r.innerHTML = h;
}

// ─── Export / Score ───
async function downloadPptx(enc) {
    showToast('PPT 생성 중...', 'warning');
    const fd = new FormData(); fd.append('proposal_text', decodeURIComponent(enc));
    try {
        const resp = await fetch('api/export-pptx', { method: 'POST', body: fd });
        if (!resp.ok) throw new Error('PPT 생성 실패');
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a'); a.href = url; a.download = 'proposal.pptx'; a.click();
        URL.revokeObjectURL(url);
        showToast('PPT 다운로드 완료!');
    } catch (e) { showToast(e.message, 'error'); }
}

async function downloadDocx(enc) {
    showToast('DOCX 생성 중...', 'warning');
    const fd = new FormData(); fd.append('proposal_text', decodeURIComponent(enc));
    try {
        const resp = await fetch('api/export-docx', { method: 'POST', body: fd });
        if (!resp.ok) throw new Error('DOCX 생성 실패');
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a'); a.href = url; a.download = 'proposal.docx'; a.click();
        URL.revokeObjectURL(url);
        showToast('DOCX 다운로드 완료!');
    } catch (e) { showToast(e.message, 'error'); }
}

async function exportProposal(enc) { const fd = new FormData(); fd.append('proposal_text', decodeURIComponent(enc)); try { const d = await apiPost('api/export-proposal', fd); el('exportContent').textContent = d.markdown; el('exportModal').classList.add('show'); } catch (e) { showToast(e.message, 'error'); } }
function closeModal() { el('exportModal').classList.remove('show'); }
function copyExport() { navigator.clipboard.writeText(el('exportContent').textContent).then(() => showToast('클립보드에 복사!')); }
function downloadExport() { const b = new Blob([el('exportContent').textContent], {type:'text/markdown'}); const u = URL.createObjectURL(b); const a = document.createElement('a'); a.href = u; a.download = 'proposal.md'; a.click(); URL.revokeObjectURL(u); showToast('다운로드 완료!'); }

async function scoreProposal(enc) {
    showToast('AI 채점 중...', 'warning');
    const fd = new FormData(); fd.append('proposal_text', decodeURIComponent(enc));
    try { const d = await apiPost('api/score-proposal', fd); const s = parseJsonSafe(d.score); if (s) renderScore(s); showToast('채점 완료!'); } catch (e) { showToast(e.message, 'error'); }
}

function renderScore(data) {
    const color = data.total_score >= 80 ? '#10b981' : data.total_score >= 60 ? '#f59e0b' : '#ef4444';
    let h = `<div style="text-align:center;margin-bottom:24px"><div class="score-circle" style="border:4px solid ${color}"><span class="number" style="color:${color}">${data.total_score}</span><span class="label">${data.grade}</span></div><p style="color:var(--text-muted)">수주 확률: <strong style="color:var(--accent)">${data.win_probability}</strong></p></div>`;
    if (data.categories?.length) { h += '<div style="margin-bottom:20px">'; data.categories.forEach(c => { const bc = c.score>=80?'#10b981':c.score>=60?'#f59e0b':'#ef4444'; h += `<div style="margin-bottom:12px"><div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:4px"><span>${c.name}</span><span style="color:${bc};font-weight:700">${c.score}/${c.max}</span></div><div class="progress-bar"><div class="fill" style="width:${c.score}%;background:${bc}"></div></div><div style="font-size:12px;color:var(--text-muted);margin-top:2px">${c.feedback}</div></div>`; }); h += '</div>'; }
    if (data.strengths?.length) { h += '<div class="section-title">강점</div>'; data.strengths.forEach(s => h += `<div class="factor-item" style="border-left-color:#10b981">${s}</div>`); }
    if (data.weaknesses?.length) { h += '<div class="section-title">약점</div>'; data.weaknesses.forEach(w => h += `<div class="factor-item" style="border-left-color:#ef4444">${w}</div>`); }
    el('exportContent').innerHTML = h; el('exportModal').classList.add('show');
}

// ─── Review ───
async function reviewProposal() {
    const rfpId = el('reviewRfpSelect')?.value || currentRfpId;
    const pt = el('proposalTextForReview').value;
    if (!pt.trim()) { showToast('제안서 내용을 입력해주세요.', 'warning'); return; }
    showLoading('reviewSpinner'); setBtnLoading('btnReview', 'review');
    el('reviewResult').classList.remove('visible');
    const fd = new FormData(); if (rfpId) fd.append('rfp_id', rfpId); fd.append('proposal_text', pt);
    try {
        const data = await apiPost('api/review-proposal', fd, 'review');
        cachedResults.review = data.review;
        const r = parseJsonSafe(data.review);
        if (r) renderReview(r); else el('reviewResult').innerHTML = pre(data.review);
        el('reviewResult').classList.add('visible'); showToast('리뷰 완료!');
    } catch (e) { if (e.name !== 'AbortError') showToast(e.message, 'error'); }
    finally { hideLoading('reviewSpinner'); setBtnDone('btnReview'); refreshHistoryBar('review'); }
}

function renderReview(data) {
    const e = el('reviewResult'); let h = '';
    const score = data.overall_score || 0, color = score>=80?'#10b981':score>=60?'#f59e0b':'#ef4444';
    h += `<div class="score-circle" style="border:4px solid ${color}"><span class="number" style="color:${color}">${score}</span><span class="label">${data.grade||''}</span></div>`;
    if (data.review_items?.length) {
        h += `<div class="section-title">항목별 평가</div><table class="data-table"><thead><tr><th>항목</th><th>점수</th><th>상태</th><th>코멘트</th></tr></thead><tbody>`;
        data.review_items.forEach(r => { const bc = r.score>=80?'#10b981':r.score>=60?'#f59e0b':'#ef4444'; const sb = r.status==='양호'?'badge-low':r.status==='보통'?'badge-medium':'badge-high'; h += `<tr><td><strong>${r.category}</strong></td><td><div class="progress-bar" style="width:80px;display:inline-block;vertical-align:middle"><div class="fill" style="width:${r.score}%;background:${bc}"></div></div> <span style="color:${bc}">${r.score}</span></td><td><span class="badge ${sb}">${r.status}</span></td><td style="font-size:13px;color:var(--text-dim)">${r.comment}</td></tr>`; });
        h += '</tbody></table>';
    }
    if (data.missing_requirements?.length) { h += `<div class="section-title">누락된 요구사항</div>`; data.missing_requirements.forEach(m => h += `<div class="factor-item" style="border-left-color:#ef4444">${m}</div>`); }
    if (data.logic_issues?.length) { h += `<div class="section-title">논리 불일치</div>`; data.logic_issues.forEach(l => h += `<div class="factor-item" style="border-left-color:#f59e0b">${l}</div>`); }
    if (data.improvement_suggestions?.length) { h += `<div class="section-title">개선 제안</div>`; data.improvement_suggestions.forEach(s => h += `<div class="factor-item" style="border-left-color:var(--primary)">${s}</div>`); }
    e.innerHTML = h;
}

// ─── Strategy ───
async function analyzeStrategy() {
    const rfpId = el('strategyRfpSelect')?.value || currentRfpId;
    showLoading('strategySpinner'); setBtnLoading('btnStrategy', 'strategy');
    el('strategyResult').classList.remove('visible');
    const fd = new FormData(); if (rfpId) fd.append('rfp_id', rfpId);
    fd.append('company_strengths', el('companyStrengths').value); fd.append('market_context', el('marketContext').value);
    try {
        const data = await apiPost('api/strategy', fd, 'strategy');
        cachedResults.strategy = data.strategy;
        const s = parseJsonSafe(data.strategy);
        if (s) renderStrategy(s); else el('strategyResult').innerHTML = pre(data.strategy);
        el('strategyResult').classList.add('visible'); showToast('전략 분석 완료!');
    } catch (e) { if (e.name !== 'AbortError') showToast(e.message, 'error'); }
    finally { hideLoading('strategySpinner'); setBtnDone('btnStrategy'); refreshHistoryBar('strategy'); }
}

function renderStrategy(data) {
    const e = el('strategyResult'); let h = '';
    const rec = data.recommendation || '';
    const isGo = rec === 'GO';
    const isCond = rec.includes('CONDITIONAL');
    const boxClass = isGo ? 'go' : isCond ? 'conditional' : 'nogo';
    const boxColor = isGo ? 'var(--success)' : isCond ? 'var(--warning)' : 'var(--danger)';

    h += `<div class="decision-box" style="background:${isGo?'rgba(16,185,129,0.1)':isCond?'rgba(245,158,11,0.1)':'rgba(239,68,68,0.1)'};border:2px solid ${boxColor}">
        <div class="decision-text" style="color:${boxColor}">${rec}</div>
        <div class="confidence">신뢰도: ${data.confidence}</div>
        ${data.weighted_score ? `<div style="margin-top:8px;font-size:14px;color:var(--text-dim)">가중 평균 점수: <strong style="color:${boxColor}">${data.weighted_score}점</strong> / 100</div>` : ''}
    </div>`;

    // Scoring criteria explanation
    h += `<div style="background:var(--bg-card2);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:20px;font-size:12px;color:var(--text-muted)">
        <strong>판정 기준:</strong> 가중평균 70점 이상 → GO | 50~69점 → CONDITIONAL GO | 50점 미만 → NO-GO<br>
        <strong>가중치:</strong> 시장적합성 25% + 경쟁환경 20% + 수익성 20% + 전략가치 20% + 자원가용성 15%
    </div>`;

    if (data.analysis) {
        const lb = {market_fit:'시장 적합성 (25%)',competition:'경쟁 환경 (20%)',profitability:'수익성 (20%)',strategic_value:'전략적 가치 (20%)',resource_availability:'자원 가용성 (15%)'};
        h += `<div class="section-title">항목별 채점</div><div class="grid-2">`;
        Object.entries(data.analysis).forEach(([k,v]) => {
            if (!v||typeof v!=='object') return;
            const bc = v.score>=80?'#10b981':v.score>=60?'#f59e0b':'#ef4444';
            const grade = v.score>=80?'우수':v.score>=60?'보통':'미흡';
            h += `<div class="card"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <strong>${lb[k]||k}</strong>
                <span style="color:${bc};font-weight:700;font-size:18px">${v.score}점 <span style="font-size:12px">(${grade})</span></span>
            </div>
            <div class="progress-bar" style="margin-bottom:8px"><div class="fill" style="width:${v.score}%;background:${bc}"></div></div>
            <p style="font-size:13px;color:var(--text-muted)">${v.comment}</p></div>`;
        });
        h += '</div>';
    }
    if (data.key_factors?.length) { h += `<div class="section-title">핵심 요인</div>`; data.key_factors.forEach(f => h += `<div class="factor-item">${f}</div>`); }
    if (data.risks?.length) { h += `<div class="section-title">참여 리스크</div>`; data.risks.forEach(r => h += `<div class="factor-item" style="border-left-color:#ef4444">${r}</div>`); }
    if (data.win_strategy?.length) { h += `<div class="section-title">수주 전략</div>`; data.win_strategy.forEach((s,i) => h += `<div class="factor-item" style="border-left-color:var(--success)"><strong>${i+1}.</strong> ${s}</div>`); }
    e.innerHTML = h;
}

// ─── Knowledge ───
async function saveKnowledge() {
    const t = el('knowledgeTitle').value, c = el('knowledgeContent').value;
    if (!t.trim() || !c.trim()) { showToast('제목과 내용을 입력해주세요.', 'warning'); return; }
    const fd = new FormData(); fd.append('category', el('knowledgeCategory').value); fd.append('title', t); fd.append('content', c); fd.append('tags', el('knowledgeTags').value);
    try { await apiPost('api/knowledge/save', fd); showToast('지식 자산 저장!'); el('knowledgeTitle').value = ''; el('knowledgeContent').value = ''; el('knowledgeTags').value = ''; loadKnowledge(); } catch (e) { showToast(e.message, 'error'); }
}
async function loadKnowledge() {
    try { const r = await fetch('api/knowledge/list'); const d = await r.json(); const e = el('knowledgeList');
    if (!d.items.length) { e.innerHTML = '<p style="color:var(--text-muted);text-align:center;padding:24px">저장된 지식 자산이 없습니다.</p>'; return; }
    e.innerHTML = d.items.map(i => { const tags = i.tags.map(t => `<span class="ki-tag">${t}</span>`).join(''); return `<div class="knowledge-item"><div class="ki-header"><span class="ki-title">${i.title}</span><div class="ki-tags"><span class="badge badge-optional">${i.category}</span>${tags}</div></div><div class="ki-content">${i.content.substring(0,200)}${i.content.length>200?'...':''}</div></div>`; }).join(''); } catch {}
}

// ─── Pipeline ───
async function runPipeline() {
    const rfpId = el('pipelineRfpSelect')?.value || currentRfpId;
    if (!rfpId) { showToast('RFP를 먼저 업로드해주세요.', 'warning'); return; }
    setBtnLoading('pipelineBtn', 'pipeline');
    showLoading('pipelineSpinner'); el('pipelineResult').classList.remove('visible');
    const fd = new FormData(); fd.append('rfp_id', rfpId);
    fd.append('company_info', el('pipelineCompany')?.value || '');
    fd.append('industry', el('pipelineIndustry')?.value || 'IT');
    fd.append('customer_type', el('pipelineCustomer')?.value || '대기업');
    try {
        const data = await apiPost('api/pipeline/run', fd, 'pipeline');
        if (data.results) Object.entries(data.results).forEach(([k,v]) => { cachedResults[k] = v; });
        renderPipelineResult(data);
        el('pipelineResult').classList.add('visible');
        showToast(`파이프라인 완료! ${data.steps_completed.length}단계`); refreshDashboard();
    } catch (e) { if (e.name !== 'AbortError') showToast(e.message, 'error'); }
    finally { hideLoading('pipelineSpinner'); setBtnDone('pipelineBtn'); }
}

function renderPipelineResult(data) {
    const e = el('pipelineResult');
    const lb = {analyze:'RFP 구조화',pattern:'Winning 패턴',proposal:'제안서 초안',review:'리뷰 AI',strategy:'Go/No-Go'};
    const tabs = {analyze:'tab-analyze',pattern:'tab-pattern',proposal:'tab-proposal',review:'tab-review',strategy:'tab-strategy'};
    let h = `<div style="text-align:center;margin-bottom:20px;color:var(--success);font-weight:700;font-size:18px">${data.steps_completed.length}단계 분석 완료</div>`;
    h += `<p style="text-align:center;color:var(--text-muted);margin-bottom:20px;font-size:14px">각 메뉴 탭에서 상세 결과를 확인할 수 있습니다.</p>`;
    data.steps_completed.forEach(step => {
        const parsed = parseJsonSafe(data.results[step]);
        h += `<details style="margin-bottom:12px"><summary style="cursor:pointer;padding:12px;background:var(--bg-card2);border-radius:10px;font-weight:600;color:var(--primary-light);display:flex;align-items:center;justify-content:space-between"><span>${lb[step]||step} &#10003;</span><button class="btn btn-outline" style="font-size:12px;padding:4px 12px" onclick="event.stopPropagation();switchTab('${tabs[step]}')">상세 보기</button></summary>`;
        h += `<div style="padding:16px;border:1px solid var(--border);border-radius:0 0 10px 10px;margin-top:-4px"><pre style="white-space:pre-wrap;font-size:13px;color:var(--text-dim);max-height:200px;overflow-y:auto">${parsed ? JSON.stringify(parsed,null,2) : data.results[step]}</pre></div></details>`;
    });
    e.innerHTML = h;
}

// ─── PDF Download ───
async function downloadPdf(enc) {
    showToast('PDF 생성 중...', 'warning');
    const fd = new FormData(); fd.append('proposal_text', decodeURIComponent(enc));
    try {
        const resp = await fetch('api/export-pdf', { method: 'POST', body: fd });
        if (!resp.ok) throw new Error('PDF 생성 실패');
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a'); a.href = url; a.download = 'proposal.pdf'; a.click();
        URL.revokeObjectURL(url); showToast('PDF 다운로드 완료!');
    } catch (e) { showToast(e.message, 'error'); }
}

// ─── In-tab History Bar ───
async function refreshHistoryBar(step) {
    const rfpId = currentRfpId;
    const bar = el(`historyBar-${step}`);
    if (!bar) return;
    if (!rfpId) { bar.innerHTML = ''; return; }
    try {
        const resp = await fetch(`/api/history/${rfpId}?step=${step}`);
        const data = await resp.json();
        if (!data.history?.length) { bar.innerHTML = ''; return; }
        const sorted = data.history.slice().sort((a, b) => a.version - b.version);
        let h = `<span class="history-bar-label">이전 버전:</span>`;
        sorted.forEach(v => {
            const shortDate = v.timestamp.split(' ')[0].slice(5); // MM-DD
            const shortTime = v.timestamp.split(' ')[1]?.slice(0, 5) || '';
            h += `<span class="history-chip" onclick="loadHistoryVersion('${step}',${v.version})" title="${v.timestamp}">v${v.version} (${shortDate} ${shortTime})</span>`;
        });
        h += `<span class="history-chip active" onclick="showCachedResult('${step}')">최신</span>`;
        bar.innerHTML = h;
    } catch { bar.innerHTML = ''; }
}

async function loadHistoryVersion(step, version) {
    const rfpId = currentRfpId;
    if (!rfpId) return;
    try {
        const resp = await fetch(`/api/history/${rfpId}?step=${step}`);
        const data = await resp.json();
        const item = data.history.find(h => h.version === version);
        if (!item) return;
        // Render selected version
        const resultMap = { analyze: 'analyzeResult', pattern: 'patternResult', proposal: 'proposalResult', review: 'reviewResult', strategy: 'strategyResult', estimate: 'estimateResult' };
        const renderers = {
            analyze: (d) => { const p = parseJsonSafe(d); if (p) renderAnalysis(p); else el('analyzeResult').innerHTML = pre(d); },
            pattern: (d) => { const p = parseJsonSafe(d); if (p) renderPattern(p); else el('patternResult').innerHTML = pre(d); },
            proposal: (d) => { const p = parseJsonSafe(d); renderProposal(p, d); },
            review: (d) => { const p = parseJsonSafe(d); if (p) renderReview(p); else el('reviewResult').innerHTML = pre(d); },
            strategy: (d) => { const p = parseJsonSafe(d); if (p) renderStrategy(p); else el('strategyResult').innerHTML = pre(d); },
            estimate: (d) => { const p = parseJsonSafe(d); if (p) renderEstimate(p); else el('estimateResult').innerHTML = pre(d); },
        };
        if (renderers[step]) renderers[step](item.result);
        el(resultMap[step]).classList.add('visible');
        // Highlight active chip
        const bar = el(`historyBar-${step}`);
        bar.querySelectorAll('.history-chip').forEach(c => c.classList.remove('active'));
        const chips = bar.querySelectorAll('.history-chip');
        chips.forEach(c => { if (c.textContent.includes(`v${version}`)) c.classList.add('active'); });
        showToast(`v${version} (${item.timestamp}) 결과를 표시합니다.`);
    } catch {}
}

// ─── Analysis History ───
async function loadHistory(step) {
    const rfpId = el('historyRfpSelect')?.value || currentRfpId;
    if (!rfpId) { showToast('RFP를 선택해주세요.', 'warning'); return; }
    const url = step ? `/api/history/${rfpId}?step=${step}` : `/api/history/${rfpId}`;
    try {
        const resp = await fetch(url);
        const data = await resp.json();
        renderHistory(data.history, rfpId);
    } catch {}
}

function renderHistory(items, rfpId) {
    const list = el('historyList');
    if (!items.length) { list.innerHTML = '<p style="color:var(--text-muted);text-align:center;padding:24px">분석 이력이 없습니다. 분석을 실행하면 자동으로 기록됩니다.</p>'; return; }
    const stepLabels = { analyze: '구조화 분석', pattern: 'Winning 패턴', proposal: '제안서 초안', review: '리뷰 AI', strategy: 'Go/No-Go' };
    const stepColors = { analyze: '#6366f1', pattern: '#06b6d4', proposal: '#10b981', review: '#f59e0b', strategy: '#ef4444' };
    // Sort by timestamp desc
    const sorted = items.slice().sort((a, b) => b.timestamp.localeCompare(a.timestamp));
    list.innerHTML = sorted.map((h, i) => {
        const color = stepColors[h.step] || 'var(--primary)';
        const label = stepLabels[h.step] || h.step;
        const parsed = parseJsonSafe(h.result);
        const preview = parsed ? JSON.stringify(parsed, null, 2).substring(0, 300) : (h.result || '').substring(0, 300);
        return `<details style="margin-bottom:10px" ${i === 0 ? 'open' : ''}>
            <summary style="cursor:pointer;padding:12px;background:var(--bg-card2);border-radius:10px;display:flex;align-items:center;justify-content:space-between;gap:12px">
                <div style="display:flex;align-items:center;gap:10px;flex:1">
                    <span style="width:10px;height:10px;border-radius:50%;background:${color};flex-shrink:0"></span>
                    <span style="font-weight:600;color:var(--text)">${label}</span>
                    <span class="badge" style="background:${color}22;color:${color}">v${h.version}</span>
                </div>
                <span style="font-size:12px;color:var(--text-muted);flex-shrink:0">${h.timestamp}</span>
            </summary>
            <div style="padding:16px;border:1px solid var(--border);border-radius:0 0 10px 10px;margin-top:-2px">
                <div class="btn-group" style="margin-bottom:10px">
                    <button class="btn btn-outline" style="font-size:12px;padding:4px 12px" onclick="applyHistory('${rfpId}','${h.step}',${items.indexOf(h)})">이 버전 적용</button>
                    <button class="btn btn-outline" style="font-size:12px;padding:4px 12px" onclick="copyHistoryResult(this)">복사</button>
                </div>
                <pre class="history-pre" style="white-space:pre-wrap;font-size:12px;color:var(--text-dim);max-height:300px;overflow-y:auto;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px">${preview}${(h.result||'').length > 300 ? '\n...(더보기)' : ''}</pre>
            </div>
        </details>`;
    }).join('');
}

async function applyHistory(rfpId, step, idx) {
    const resp = await fetch(`/api/history/${rfpId}?step=${step}`);
    const data = await resp.json();
    const items = data.history.sort((a, b) => b.timestamp.localeCompare(a.timestamp));
    if (!items[idx]) return;
    cachedResults[step] = items[idx].result;
    showCachedResult(step);
    const tabMap = { analyze: 'tab-analyze', pattern: 'tab-pattern', proposal: 'tab-proposal', review: 'tab-review', strategy: 'tab-strategy' };
    if (tabMap[step]) switchTab(tabMap[step]);
    showToast(`v${items[idx].version} 결과가 적용되었습니다.`);
}

function copyHistoryResult(btn) {
    const pre = btn.closest('details').querySelector('.history-pre');
    if (pre) navigator.clipboard.writeText(pre.textContent).then(() => showToast('복사 완료!'));
}

// ─── Version Control ───
async function saveVersion() {
    const rfpId = el('versionRfpSelect')?.value || currentRfpId;
    if (!rfpId) { showToast('RFP를 선택해주세요.', 'warning'); return; }
    const content = el('versionContent').value;
    if (!content.trim()) { showToast('제안서 내용을 입력해주세요.', 'warning'); return; }
    const fd = new FormData();
    fd.append('rfp_id', rfpId);
    fd.append('content', content);
    fd.append('score', el('versionScore').value || '0');
    fd.append('note', el('versionNote').value);
    try {
        const d = await apiPost('api/version/save', fd);
        showToast(`v${d.version.version} 저장 완료!`);
        el('versionNote').value = '';
        loadVersions(rfpId);
    } catch (e) { showToast(e.message, 'error'); }
}

async function loadVersions(rfpId) {
    rfpId = rfpId || el('versionRfpSelect')?.value || currentRfpId;
    if (!rfpId) return;
    try {
        const resp = await fetch(`/api/version/list/${rfpId}`);
        const data = await resp.json();
        renderVersions(data.versions);
    } catch {}
}

function renderVersions(versions) {
    // Score chart
    const chart = el('versionScoreChart');
    if (versions.length) {
        const max = 100;
        chart.innerHTML = versions.map((v, i) => {
            const h = Math.max(5, (v.score / max) * 70);
            const color = v.score >= 80 ? '#10b981' : v.score >= 60 ? '#f59e0b' : '#ef4444';
            return `<div style="display:flex;flex-direction:column;align-items:center;flex:1;max-width:60px">
                <span style="font-size:11px;color:${color};font-weight:700">${v.score}</span>
                <div style="width:100%;height:${h}px;background:${color};border-radius:4px 4px 0 0;min-width:20px"></div>
                <span style="font-size:10px;color:var(--text-muted);margin-top:4px">v${v.version}</span></div>`;
        }).join('');
    } else { chart.innerHTML = ''; }

    const list = el('versionList');
    if (!versions.length) { list.innerHTML = '<p style="color:var(--text-muted);text-align:center;padding:20px">저장된 버전이 없습니다.</p>'; return; }
    list.innerHTML = versions.slice().reverse().map(v => {
        const color = v.score >= 80 ? '#10b981' : v.score >= 60 ? '#f59e0b' : '#ef4444';
        return `<div class="knowledge-item"><div class="ki-header">
            <span class="ki-title">v${v.version} <span style="color:${color};font-weight:700">${v.score}점</span></span>
            <span style="font-size:12px;color:var(--text-muted)">${v.created_at}</span></div>
            <div style="color:var(--text-dim);font-size:13px">${v.note || '(메모 없음)'}</div>
            <div class="ki-content" style="margin-top:6px;max-height:80px;overflow:hidden">${v.content.substring(0, 200)}${v.content.length > 200 ? '...' : ''}</div></div>`;
    }).join('');
}

// ─── Team Collaboration ───
let teamMembers = [];

async function addMember() {
    const rfpId = el('teamRfpSelect')?.value || currentRfpId;
    if (!rfpId) { showToast('RFP를 선택해주세요.', 'warning'); return; }
    const name = el('memberName').value.trim();
    if (!name) { showToast('이름을 입력해주세요.', 'warning'); return; }
    const fd = new FormData();
    fd.append('rfp_id', rfpId); fd.append('name', name); fd.append('role', el('memberRole').value);
    try {
        await apiPost('api/team/add-member', fd);
        el('memberName').value = ''; el('memberRole').value = '';
        showToast(`${name} 추가!`);
        loadTeam();
    } catch (e) { showToast(e.message, 'error'); }
}

async function removeMember(memberId) {
    const rfpId = el('teamRfpSelect')?.value || currentRfpId;
    if (!rfpId) return;
    const fd = new FormData();
    fd.append('rfp_id', rfpId); fd.append('member_id', memberId);
    try {
        await apiPost('api/team/remove-member', fd);
        showToast('팀원 삭제 완료');
        loadTeam();
    } catch (e) { showToast(e.message, 'error'); }
}

async function autoAssign() {
    const rfpId = el('teamRfpSelect')?.value || currentRfpId;
    if (!rfpId) { showToast('RFP를 선택해주세요.', 'warning'); return; }
    const existing = el('sectionList')?.querySelectorAll('.knowledge-item')?.length || 0;
    if (existing > 0 && !confirm('기존 배정을 초기화하고 다시 AI 자동 배정하시겠습니까?')) return;
    setBtnLoading('btnAutoAssign', 'autoAssign');
    showLoading('autoAssignSpinner');
    const fd = new FormData(); fd.append('rfp_id', rfpId);
    try {
        const d = await apiPost('api/team/auto-assign', fd, 'autoAssign');
        renderSections(d.sections, rfpId);
        showToast(`AI가 ${d.sections.length}개 섹션을 자동 배정했습니다!`);
    } catch (e) { if (e.name !== 'AbortError') showToast(e.message, 'error'); }
    finally { hideLoading('autoAssignSpinner'); setBtnDone('btnAutoAssign'); }
}

function addSectionManual() {
    const rfpId = el('teamRfpSelect')?.value || currentRfpId;
    if (!rfpId) { showToast('RFP를 선택해주세요.', 'warning'); return; }
    const title = prompt('섹션명을 입력하세요:');
    if (!title) return;
    const fd = new FormData();
    fd.append('rfp_id', rfpId); fd.append('title', title); fd.append('assignee', '');
    apiPost('api/team/add-section', fd).then(() => loadTeam()).catch(e => showToast(e.message, 'error'));
}

async function loadTeam() {
    const rfpId = el('teamRfpSelect')?.value || currentRfpId;
    if (!rfpId) return;
    try {
        const resp = await fetch(`/api/team/${rfpId}`);
        const data = await resp.json();
        teamMembers = data.members;
        renderMembers(data.members);
        renderSections(data.sections, rfpId);
    } catch {}
}

function renderMembers(members) {
    const list = el('memberList');
    if (!members.length) { list.innerHTML = '<p style="color:var(--text-muted);font-size:13px">팀원을 등록하면 AI가 역할에 맞게 자동 배정합니다.</p>'; return; }
    list.innerHTML = '<div style="display:flex;flex-wrap:wrap;gap:8px">' + members.map(m =>
        `<span style="background:var(--bg-card2);border:1px solid var(--border);border-radius:8px;padding:6px 12px;font-size:13px;display:inline-flex;align-items:center;gap:8px">
            <strong>${m.name}</strong> <span style="color:var(--text-muted)">${m.role}</span>
            <span onclick="removeMember('${m.id}')" style="cursor:pointer;color:var(--text-muted);font-size:16px;line-height:1" title="삭제">&times;</span>
        </span>`
    ).join('') + '</div>';
}

async function updateSectionStatus(rfpId, sectionId, status) {
    const fd = new FormData();
    fd.append('rfp_id', rfpId); fd.append('section_id', sectionId); fd.append('status', status);
    try { await apiPost('api/team/update-status', fd); } catch {}
}

async function updateAssignee(rfpId, sectionId, assignee) {
    const fd = new FormData();
    fd.append('rfp_id', rfpId); fd.append('section_id', sectionId); fd.append('assignee', assignee);
    try { await apiPost('api/team/update-assignee', fd); } catch {}
}

async function addCommentToSection(rfpId, sectionId) {
    const input = document.querySelector(`#comment-${sectionId}`);
    if (!input || !input.value.trim()) return;
    const fd = new FormData();
    fd.append('rfp_id', rfpId); fd.append('section_id', sectionId);
    fd.append('author', currentUsername || 'Me'); fd.append('text', input.value);
    try { await apiPost('api/team/add-comment', fd); input.value = ''; loadTeam(); } catch {}
}

function renderSections(sections, rfpId) {
    const list = el('sectionList');
    if (!sections.length) { list.innerHTML = '<p style="color:var(--text-muted);text-align:center;padding:20px">AI 자동 배정 버튼을 클릭하거나 수동으로 섹션을 추가하세요.</p>'; return; }
    const statusColors = { '대기': 'var(--text-muted)', '진행중': '#f59e0b', '리뷰중': '#6366f1', '완료': '#10b981' };
    // Build assignee options
    const memberOpts = teamMembers.map(m => m.name);
    list.innerHTML = sections.map(s => {
        const sc = statusColors[s.status] || 'var(--text-muted)';
        const reason = s.reason ? `<div style="font-size:12px;color:var(--text-muted);margin-top:2px">배정 사유: ${s.reason}</div>` : '';
        const comments = (s.comments || []).map(c => `<div style="font-size:12px;color:var(--text-dim);padding:4px 0;border-bottom:1px solid var(--border)"><strong>${c.author}</strong> <span style="color:var(--text-muted)">${c.time}</span><br>${c.text}</div>`).join('');

        // Assignee dropdown (editable)
        let assigneeOpts = '<option value="">미배정</option>';
        const allNames = [...new Set([...memberOpts, s.assignee].filter(Boolean))];
        allNames.forEach(n => { assigneeOpts += `<option ${n===s.assignee?'selected':''}>${n}</option>`; });

        return `<div class="knowledge-item" style="border-left:3px solid ${sc}">
            <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:6px">
                <span class="ki-title" style="flex:1">${s.title}</span>
                <div style="display:flex;gap:6px;align-items:center">
                    <select class="form-control" style="width:auto;padding:4px 8px;font-size:12px" onchange="updateAssignee('${rfpId}','${s.id}',this.value)" title="담당자 변경">${assigneeOpts}</select>
                    <select class="form-control" style="width:auto;padding:4px 8px;font-size:12px" onchange="updateSectionStatus('${rfpId}','${s.id}',this.value)">
                        <option ${s.status==='대기'?'selected':''}>대기</option><option ${s.status==='진행중'?'selected':''}>진행중</option><option ${s.status==='리뷰중'?'selected':''}>리뷰중</option><option ${s.status==='완료'?'selected':''}>완료</option>
                    </select>
                </div>
            </div>
            ${reason}${comments}
            <div style="display:flex;gap:8px;margin-top:8px"><input class="form-control" id="comment-${s.id}" placeholder="코멘트..." style="flex:1;padding:6px 10px;font-size:12px">
                <button class="btn btn-outline" style="font-size:12px;padding:4px 12px" onclick="addCommentToSection('${rfpId}','${s.id}')">등록</button></div>
        </div>`;
    }).join('');
}

// ─── Schedule ───
async function generateSchedule() {
    const deadline = el('scheduleDeadline').value;
    if (!deadline) { showToast('마감일을 선택해주세요.', 'warning'); return; }
    const rfpId = el('scheduleRfpSelect')?.value || currentRfpId;
    setBtnLoading('btnSchedule', 'schedule');
    const fd = new FormData();
    fd.append('deadline', deadline);
    if (rfpId) fd.append('rfp_id', rfpId);
    try {
        const data = await apiPost('api/schedule/generate', fd);
        renderSchedule(data);
        el('scheduleResult').classList.add('visible');
        showToast('일정 생성 완료!');
    } catch (e) { showToast(e.message, 'error'); }
    finally { setBtnDone('btnSchedule'); }
}

function renderSchedule(data) {
    const e = el('scheduleResult');
    let h = `<div style="text-align:center;margin-bottom:20px"><span style="font-size:24px;font-weight:800;color:var(--primary-light)">${data.total_days}일</span><span style="color:var(--text-muted);margin-left:8px">마감까지</span></div>`;

    // Gantt-like timeline
    h += '<div style="margin-bottom:24px">';
    const colors = ['#6366f1', '#06b6d4', '#f59e0b', '#10b981', '#94a3b8'];
    data.schedule.forEach((phase, i) => {
        const pct = Math.max(5, (phase.days / data.total_days) * 100);
        h += `<div style="display:flex;align-items:center;margin-bottom:6px">
            <div style="width:160px;font-size:13px;font-weight:600;color:var(--text-dim);flex-shrink:0">${phase.phase}</div>
            <div style="flex:1;display:flex;align-items:center;gap:8px">
                <div style="width:${pct}%;height:28px;background:${colors[i % 5]};border-radius:6px;display:flex;align-items:center;padding:0 10px;min-width:40px">
                    <span style="font-size:11px;color:white;font-weight:600">${phase.days}일</span></div>
                <span style="font-size:11px;color:var(--text-muted)">${phase.start} ~ ${phase.end}</span></div></div>`;
    });
    h += '</div>';

    // Detail tasks
    data.schedule.forEach((phase, i) => {
        h += `<details style="margin-bottom:10px" ${i === 0 ? 'open' : ''}>
            <summary style="cursor:pointer;padding:10px;background:var(--bg-card2);border-radius:8px;font-weight:600;color:${colors[i % 5]}">${phase.phase} (${phase.start} ~ ${phase.end}, ${phase.days}일)</summary>
            <div style="padding:12px;border:1px solid var(--border);border-radius:0 0 8px 8px;margin-top:-2px">`;
        phase.tasks.forEach(t => { h += `<div class="factor-item" style="border-left-color:${colors[i % 5]};font-size:13px">${t}</div>`; });
        h += '</div></details>';
    });
    e.innerHTML = h;
}

// ─── Init ───
document.addEventListener('DOMContentLoaded', () => {
    initUpload(); loadKnowledge(); refreshDashboard(); loadProposalInputs();
    // Auto-save proposal inputs on typing
    ['companyInfo', 'references'].forEach(id => {
        const e = el(id);
        if (e) e.addEventListener('input', saveProposalInputs);
    });
    document.querySelectorAll('.nav-item[data-tab]').forEach(btn => { btn.addEventListener('click', () => switchTab(btn.dataset.tab)); });
    document.addEventListener('change', e => {
        if (e.target.classList.contains('rfp-select') && e.target.value) selectRfp(e.target.value);
    });

    // Restore username from localStorage
    const saved = localStorage.getItem('rfp_username');
    if (saved) {
        currentUsername = saved;
        el('usernameModal').classList.remove('show');
        connectWebSocket(saved);
    } else {
        el('usernameInput').focus();
    }
});
