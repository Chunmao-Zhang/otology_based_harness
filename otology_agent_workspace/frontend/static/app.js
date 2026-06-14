(() => {
  const config = window.STUDIO_CONFIG || { agentId: 'ontology_coordinator', brand: 'Ontology QA Agent' };

  const el = {
    body: document.body,
    hero: document.getElementById('hero'),
    messages: document.getElementById('messages'),
    input: document.getElementById('message-input'),
    send: document.getElementById('send-button'),
    statusPill: document.getElementById('status-pill'),
    modelChip: document.getElementById('model-chip'),
    newChat: document.getElementById('new-chat'),
    brandHome: document.getElementById('brand-home'),
    themeToggle: document.getElementById('theme-toggle'),
    sideRail: document.getElementById('side-rail'),
    railToggle: document.getElementById('rail-toggle'),
    railCollapse: document.getElementById('rail-collapse'),
    railNewChat: document.getElementById('rail-new-chat'),
    sessionList: document.getElementById('session-list'),
    runIndicator: document.getElementById('run-indicator'),
    runDetail: document.getElementById('run-detail'),
    resetRun: document.getElementById('reset-run'),
    stageStrip: document.getElementById('stage-strip'),
    uploadMetric: document.getElementById('upload-metric'),
    heroUpload: document.getElementById('hero-upload'),
    uploadCurrent: document.getElementById('upload-current'),
    attachAdd: document.getElementById('attach-add'),
    chatFileInput: document.getElementById('chat-file-input'),
    attachChips: document.getElementById('attach-chips'),
    fabContainer: document.getElementById('fab-container'),
    fabMain: document.getElementById('fab-main'),
    fabEvidence: document.getElementById('fab-evidence'),
    fabSchema: document.getElementById('fab-schema'),
    fabProgress: document.getElementById('fab-progress'),
    panel: document.getElementById('activities-panel'),
    closePanel: document.getElementById('close-panel'),
    panelTabs: Array.from(document.querySelectorAll('.panel-tab')),
    evidenceContent: document.getElementById('evidence-content'),
    schemaContent: document.getElementById('schema-content'),
    progressContent: document.getElementById('progress-content'),
    confirmOverlay: document.getElementById('confirm-overlay'),
    confirmTitle: document.getElementById('confirm-title'),
    confirmText: document.getElementById('confirm-text'),
    confirmOk: document.getElementById('confirm-ok'),
    confirmCancel: document.getElementById('confirm-cancel'),
    clarifyOverlay: document.getElementById('clarify-overlay'),
    clarifyModalClose: document.getElementById('clarify-modal-close'),
    clarifyModalProblem: document.getElementById('clarify-modal-problem'),
    clarifyModalSteps: document.getElementById('clarify-modal-steps'),
    clarifyModalAddStep: document.getElementById('clarify-modal-add-step'),
    clarifyModalCancel: document.getElementById('clarify-modal-cancel'),
    clarifyModalSave: document.getElementById('clarify-modal-save'),
    schemaOverlay: document.getElementById('schema-overlay'),
    schemaModalBody: document.getElementById('schema-modal-body'),
    schemaModalClose: document.getElementById('schema-modal-close'),
  };

  const state = {
    sessionId: '',
    ws: null,
    wsReady: false,
    running: false,
    messages: [],
    stages: [],
    uploads: [],
    selectedUploads: new Set(),
    schema: null,
    schemaForm: [],
    schemaDirty: false,
    panelTab: 'evidence',
    isComposing: false,
    activeClarificationMessageId: '',
    expandedStageCards: new Set(),
    expandedGroups: new Set(),
    revealedGroups: new Set(),
    forceScroll: false,
    liveStream: {},
    toolKeys: {},
    runStartedAt: 0,
    pendingOptimistic: [],
    // WebSocket resilience: tunnels can drop a long-running socket. We auto-
    // reconnect, and while a run is still in flight we poll history so the run's
    // persisted progress/result is recovered even though the live stream lapsed.
    wsConnectedOnce: false,
    wsReconnectAttempts: 0,
    wsReconnectTimer: null,
    historyPollTimer: null,
  };

  // ── Agent identity: coordinator vs subagents ─────────────────────────────
  // The whole run is orchestrated by ONE coordinator LLM (deepseek-v4-flash)
  // that delegates each step to a specialised subagent via the `task` tool.
  // The UI makes that hierarchy explicit: a coordinator banner sits above the
  // per-step cards, and every step card is badged with the subagent that owns
  // it — so a viewer can always see which agent is doing which task.
  const COORDINATOR_LANE = '__coordinator__';
  const COORDINATOR_AGENT = { agent: 'ontology_coordinator', label: 'Coordinator', cn: '主控 Agent', icon: '◉' };
  const SUBAGENT_ORDER = ['clarify', 'evidence', 'schema_build', 'schema_judge', 'extract', 'solve'];
  const STAGE_AGENTS = {
    clarify:      { agent: 'problem_clarifier', label: 'Problem Clarifier', cn: '问题澄清', icon: '◇' },
    evidence:     { agent: 'evidence_collector', label: 'Evidence Collector', cn: '证据采集', icon: '◈' },
    schema_build: { agent: 'schema_builder',    label: 'Schema Builder',    cn: 'Schema 构建', icon: '▦' },
    schema_judge: { agent: 'schema_judger',     label: 'Schema Judger',     cn: 'Schema 评审', icon: '§' },
    extract:      { agent: 'data_extractor',    label: 'Data Extractor',    cn: '数据抽取',   icon: '⛏' },
    solve:        { agent: 'workspace_solver',  label: 'Workspace Solver',  cn: '代码求解',   icon: 'ƒ' },
  };
  function stageAgent(stageId) {
    return STAGE_AGENTS[stageId] || { agent: stageId, label: stageId || 'Subagent', cn: '', icon: '●' };
  }
  function agentBadgeHtml(stageId) {
    const a = stageAgent(stageId);
    return `<span class="agent-badge subagent" title="Subagent · ${escapeHtml(a.label)}">
        <span class="agent-badge-glyph">${escapeHtml(a.icon)}</span>
        <span class="agent-badge-text"><span class="agent-badge-kind">子Agent</span><span class="agent-badge-name">${escapeHtml(a.label)}</span></span>
      </span>`;
  }
  function coordinatorChipRow() {
    return `<div class="deleg-chip-row">${SUBAGENT_ORDER.map((id) => {
      const stage = stageById(id);
      const status = stage ? stage.status : 'pending';
      const a = stageAgent(id);
      const live = state.running && status === 'running';
      const cls = status === 'done' ? 'done' : (status === 'running' ? 'running' : (status === 'waiting' ? 'waiting' : 'pending'));
      const dot = status === 'done' ? '✓' : (status === 'running' ? '●' : '');
      return `<span class="deleg-chip ${cls}${live ? ' live' : ''}" title="${escapeHtml(a.label)} · ${escapeHtml(status)}">
          <span class="deleg-chip-glyph">${escapeHtml(a.icon)}</span>
          <span class="deleg-chip-name">${escapeHtml(a.label)}</span>
          <span class="deleg-chip-dot">${dot}</span>
        </span>`;
    }).join('<span class="deleg-arrow" aria-hidden="true">→</span>')}</div>`;
  }
  function coordinatorBannerHtml(isLatest) {
    const live = (state.liveStream || {})[COORDINATOR_LANE] || {};
    const active = inferActiveStageId();
    const activeAgent = active ? stageAgent(active) : null;
    const orchestrating = isLatest && state.running;
    const subtitle = orchestrating
      ? (activeAgent ? `正在委派 → 子Agent「${activeAgent.label}」` : '正在编排工作流…')
      : '已完成全流程编排';
    const narration = (live.output || live.thinking || '').trim();
    const narrHtml = (orchestrating && narration)
      ? `<div class="coordinator-narration"><span class="coordinator-narration-label">主控决策</span><div class="coordinator-narration-body">${formatMarkdown(narration)}</div></div>`
      : '';
    return `
      <div class="coordinator-banner${orchestrating ? ' active' : ''}">
        <div class="coordinator-head">
          <div class="coordinator-avatar">${escapeHtml(COORDINATOR_AGENT.icon)}</div>
          <div class="coordinator-meta">
            <div class="coordinator-title">
              <span class="coordinator-kind">主控 Agent</span>
              <span class="coordinator-name">Coordinator</span>
              ${orchestrating ? '<span class="coordinator-live-dot" title="Orchestrating"></span>' : '<span class="run-check">✓</span>'}
            </div>
            <div class="coordinator-subtitle">${escapeHtml(subtitle)}</div>
          </div>
          <div class="coordinator-tag">deepseek-v4-flash · task()</div>
        </div>
        ${coordinatorChipRow()}
        ${narrHtml}
      </div>`;
  }

  // ── Utilities ───────────────────────────────────────────────────────────

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function sanitizeDisplayText(value) {
    const text = String(value == null ? '' : value)
      .replace(/(^|[\s`"'(])\/(?:home|Users|tmp|var|mnt|opt|workspace|runs)\b[^\s`"'<>|)]*/g, '$1[hidden path]')
      .replace(/(^|[\s`"'(])runs\/ontology_workspace_runs\/[^\s`"'<>|)]*/g, '$1[hidden path]')
      .replace(/\b[A-Za-z]:\\[^\s`"'<>|)]+/g, '[hidden path]');
    return text.split('\n').filter((line) => {
      const lower = line.trim().toLowerCase();
      return !lower.startsWith('schema path:')
        && !lower.startsWith('**schema path**:')
        && !lower.startsWith('schema used:')
        && !lower.startsWith('**schema used**:')
        && !lower.startsWith('source files:')
        && !lower.startsWith('**source files**:')
        && !lower.startsWith('workspace path:')
        && !lower.startsWith('**workspace path**:');
    }).join('\n').trim();
  }

  const ACTIVITY_TEXT_TRANSLATIONS = new Map(Object.entries({
    '正在理解问题并整理可确认的任务描述。': 'Clarifying the problem statement and solution plan.',
    '已整理出问题澄清结果，等待你确认后继续。': 'Problem clarification is ready for your confirmation.',
    '正在整理本地文件和必要的公开证据。': 'Collecting uploaded and public evidence as needed.',
    '正在把证据转成可编辑的 ontology schema。': 'Building an editable ontology schema from the evidence.',
    '正在检查当前 schema 是否足够回答问题。': 'Checking whether the current schema can answer the question.',
    'schema 已准备好，等待你确认后再抽取数据。': 'Schema is ready and waiting for your confirmation.',
    '正在按确认后的 schema 抽取实例、属性和关系。': 'Extracting instances, attributes, and relations with the confirmed schema.',
    '正在进入 workspace，用代码基于抽取结果生成答案。': 'Solving the question from the extracted workspace data.',
    '正在读取并抽样分析上传文件。': 'Reading the provided evidence.',
    '正在从已整理证据中检索相关片段。': 'Retrieving relevant evidence snippets.',
    '正在补充必要的公开网页证据。': 'Looking up supplemental public evidence.',
    '正在校验 schema 的实体、字段和关系约束。': 'Validating schema entities, fields, and relations.',
    '正在生成 draft schema 文件。': 'Preparing the draft schema.',
    '正在保存证据清单。': 'Saving the evidence manifest.',
    '正在规划当前阶段的处理清单。': 'Planning the current processing step.',
    '正在调用专门子 agent 处理当前阶段。': 'Running the specialist worker for this step.',
    '正在从表格证据中抽取结构化实例。': 'Extracting structured records from tabular evidence.',
    '正在构建可执行的求解 workspace。': 'Preparing the executable answer workspace.',
    '正在执行 workspace 求解流程。': 'Running the answer workflow.',
    '正在运行求解代码并读取执行结果。': 'Executing answer code and reading the result.',
    '正在执行一个内部处理步骤。': 'Running an internal processing step.',
  }));

  function normalizeActivityText(value) {
    const text = sanitizeDisplayText(value);
    return ACTIVITY_TEXT_TRANSLATIONS.get(text) || text;
  }

  function formatInline(text) {
    let safe = escapeHtml(sanitizeDisplayText(text));
    safe = safe.replace(/`([^`]+)`/g, '<code>$1</code>');
    safe = safe.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    safe = safe.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    return safe;
  }

  function isNearBottom() {
    const threshold = 120;
    const scrollY = window.scrollY || document.documentElement.scrollTop;
    return window.innerHeight + scrollY >= document.documentElement.scrollHeight - threshold;
  }

  function scrollToLatestMessage(behavior = 'smooth') {
    window.requestAnimationFrame(() => {
      const last = el.messages.lastElementChild;
      if (last) {
        last.scrollIntoView({ block: 'end', behavior });
      } else {
        window.scrollTo({ top: document.documentElement.scrollHeight, behavior });
      }
    });
  }

  function isTableRow(line) {
    const trimmed = String(line || '').trim();
    return trimmed.startsWith('|') && trimmed.endsWith('|');
  }

  function parseRow(line) {
    return String(line).trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
  }

  function formatMarkdown(value) {
    const lines = sanitizeDisplayText(value).replace(/\r\n/g, '\n').split('\n');
    const blocks = [];
    let index = 0;
    while (index < lines.length) {
      const trimmed = lines[index].trim();
      if (!trimmed) { index += 1; continue; }
      if (/^---+$/.test(trimmed)) {
        blocks.push('<hr>');
        index += 1;
        continue;
      }
      if (trimmed.startsWith('```')) {
        const code = [];
        index += 1;
        while (index < lines.length && !lines[index].trim().startsWith('```')) {
          code.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) index += 1;
        blocks.push(`<pre><code class="language-python">${escapeHtml(code.join('\n'))}</code></pre>`);
        continue;
      }
      if (trimmed.startsWith('>')) {
        const quote = [];
        while (index < lines.length && lines[index].trim().startsWith('>')) {
          quote.push(lines[index].trim().replace(/^>\s?/, ''));
          index += 1;
        }
        blocks.push(`<blockquote>${quote.map(formatInline).join('<br>')}</blockquote>`);
        continue;
      }
      if (isTableRow(lines[index]) && index + 1 < lines.length && /^\|[\s\-|:]+\|$/.test(lines[index + 1].trim())) {
        const header = parseRow(lines[index]);
        index += 2;
        const rows = [];
        while (index < lines.length && isTableRow(lines[index])) {
          rows.push(parseRow(lines[index]));
          index += 1;
        }
        const head = `<tr>${header.map((cell) => `<th>${formatInline(cell)}</th>`).join('')}</tr>`;
        const body = rows.map((row) => `<tr>${row.map((cell) => `<td>${formatInline(cell)}</td>`).join('')}</tr>`).join('');
        blocks.push(`<div class="md-table-wrap"><table class="md-table"><thead>${head}</thead><tbody>${body}</tbody></table></div>`);
        continue;
      }
      const heading = trimmed.match(/^(#{1,3})\s+(.+)$/);
      if (heading) {
        blocks.push(`<h${heading[1].length}>${formatInline(heading[2])}</h${heading[1].length}>`);
        index += 1;
        continue;
      }
      const listType = /^[-*]\s+/.test(trimmed) ? 'ul' : (/^\d+[.\u3001)]\s+/.test(trimmed) ? 'ol' : '');
      if (listType) {
        const items = [];
        while (index < lines.length) {
          const item = lines[index].trim();
          const match = listType === 'ul' ? item.match(/^[-*]\s+(.+)$/) : item.match(/^\d+[.\u3001)]\s+(.+)$/);
          if (!match) break;
          items.push(`<li>${formatInline(match[1])}</li>`);
          index += 1;
        }
        blocks.push(`<${listType}>${items.join('')}</${listType}>`);
        continue;
      }
      const paragraph = [];
      while (index < lines.length && lines[index].trim()
        && !lines[index].trim().startsWith('```')
        && !isTableRow(lines[index])
        && !/^(#{1,3})\s+/.test(lines[index].trim())
        && !/^[-*]\s+/.test(lines[index].trim())
        && !/^\d+[.\u3001)]\s+/.test(lines[index].trim())) {
        paragraph.push(lines[index]);
        index += 1;
      }
      blocks.push(`<p>${paragraph.map(formatInline).join('<br>')}</p>`);
    }
    return blocks.join('');
  }

  function withSession(path) {
    if (!state.sessionId) return path;
    const sep = path.includes('?') ? '&' : '?';
    return `${path}${sep}session_id=${encodeURIComponent(state.sessionId)}`;
  }

  async function api(path, options) {
    const response = await fetch(new URL(path, window.location.origin), options);
    if (!response.ok) {
      let detail = `${response.status}`;
      try { detail = (await response.json()).detail || detail; } catch (err) { /* ignore */ }
      throw new Error(detail);
    }
    return response.json();
  }

  function formatBytes(size) {
    if (!size && size !== 0) return '';
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  }

  // ── Health ──────────────────────────────────────────────────────────────

  async function refreshHealth() {
    try {
      const data = await api('/api/health');
      el.statusPill.innerHTML = '<span class="status-dot online"></span><span>Service online</span>';
      el.modelChip.textContent = (data.model || 'Model').split('/').pop();
      if (el.uploadMetric) el.uploadMetric.textContent = `${data.upload_count || 0} uploaded file(s)`;
    } catch (err) {
      el.statusPill.innerHTML = '<span class="status-dot offline"></span><span>Service unavailable</span>';
    }
  }

  // ── Sessions & WebSocket ────────────────────────────────────────────────

  async function startSession(sessionId) {
    // Switching sessions: tear down any pending reconnect/poll for the old socket
    // and null it out so its close handler won't reconnect to the old session.
    stopHistoryPolling();
    if (state.wsReconnectTimer) { clearTimeout(state.wsReconnectTimer); state.wsReconnectTimer = null; }
    state.wsConnectedOnce = false;
    state.wsReconnectAttempts = 0;
    if (state.ws) {
      try { state.ws.close(); } catch (err) { /* ignore */ }
      state.ws = null;
    }
    state.wsReady = false;
    let session = null;
    if (!sessionId) {
      const data = await api('/api/sessions', { method: 'POST' });
      session = data.session || {};
      sessionId = session.id;
    } else {
      const data = await api(`/api/sessions/${sessionId}`);
      session = data.session || {};
    }
    state.sessionId = sessionId;
    localStorage.setItem('ontology-ui-session', sessionId);
    state.messages = session.messages || [];
    state.stages = session.stages || [];
    state.uploads = [];
    state.selectedUploads.clear();
    state.activeClarificationMessageId = '';
    state.schema = null;
    state.schemaForm = [];
    state.forceScroll = true;
    connectWs();
    renderMessages();
    renderStageStrip();
    renderAttachments();
    renderSessionRail();
    refreshSidebarData();
  }

  function connectWs() {
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${protocol}://${window.location.host}/ws/${state.sessionId}`);
    state.ws = ws;
    ws.addEventListener('open', () => {
      state.wsReady = true;
      state.wsReconnectAttempts = 0;
      // On a *reconnect* (not the first connect), recover anything we missed while
      // offline: the server replays history on connect, and if a run is still in
      // flight we poll history until it settles (gate opens or answer arrives).
      if (state.wsConnectedOnce && state.running) startHistoryPolling();
      state.wsConnectedOnce = true;
    });
    ws.addEventListener('close', () => {
      state.wsReady = false;
      // Only reconnect if this is still the active socket (not one we replaced
      // by switching sessions).
      if (state.ws === ws) scheduleReconnect();
    });
    ws.addEventListener('error', () => { state.wsReady = false; });
    ws.addEventListener('message', (event) => {
      let payload = null;
      try { payload = JSON.parse(event.data); } catch (err) { return; }
      handleWsEvent(payload);
    });
  }

  function scheduleReconnect() {
    if (state.wsReconnectTimer) return;
    const delay = Math.min(1000 * (2 ** (state.wsReconnectAttempts || 0)), 8000);
    state.wsReconnectAttempts = (state.wsReconnectAttempts || 0) + 1;
    state.wsReconnectTimer = setTimeout(() => {
      state.wsReconnectTimer = null;
      if (state.sessionId) connectWs();
    }, delay);
  }

  function startHistoryPolling() {
    if (state.historyPollTimer) return;
    state.historyPollTimer = setInterval(() => {
      if (!state.running) { stopHistoryPolling(); return; }
      if (state.wsReady && state.ws) {
        try { state.ws.send(JSON.stringify({ type: 'history' })); } catch (err) { /* ignore */ }
      }
    }, 4000);
  }

  function stopHistoryPolling() {
    if (state.historyPollTimer) { clearInterval(state.historyPollTimer); state.historyPollTimer = null; }
  }

  function cssEscapeId(value) {
    return (window.CSS && CSS.escape) ? CSS.escape(value) : String(value).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
  }

  // Incrementally update the live coordinator narration and the active subagent's
  // reasoning/output panes without rebuilding the whole message tree. Returns
  // false when the live group is not in the DOM yet so the caller can fall back
  // to a full render. Re-rendering everything (markdown + syntax highlighting)
  // on every streamed token is what exhausted the browser renderer on long runs.
  function updateLiveStreamDom() {
    const root = el.messages.querySelector('.stage-pipeline-message.orchestration');
    if (!root) return false;
    const live = state.liveStream || {};
    const coord = live[COORDINATOR_LANE] || {};
    const narration = (coord.output || coord.thinking || '').trim();
    const banner = root.querySelector('.coordinator-banner');
    if (banner) {
      let narrEl = banner.querySelector('.coordinator-narration');
      if (narration) {
        if (!narrEl) {
          narrEl = document.createElement('div');
          narrEl.className = 'coordinator-narration';
          narrEl.innerHTML = '<span class="coordinator-narration-label">主控决策</span><div class="coordinator-narration-body"></div>';
          banner.appendChild(narrEl);
        }
        const body = narrEl.querySelector('.coordinator-narration-body') || narrEl;
        body.innerHTML = formatMarkdown(narration);
      } else if (narrEl) {
        narrEl.remove();
      }
    }
    Object.keys(live).forEach((stage) => {
      if (stage === COORDINATOR_LANE) return;
      const node = root.querySelector(`.task-node[data-stage="${cssEscapeId(stage)}"]`);
      if (!node) return;
      const data = live[stage] || {};
      const rEl = node.querySelector('.run-reasoning-output');
      if (rEl) rEl.innerHTML = data.thinking ? escapeHtml(sanitizeDisplayText(data.thinking)) : '<span class="live-placeholder">Waiting for live model update…</span>';
      const oEl = node.querySelector('.run-model-output');
      if (oEl) oEl.innerHTML = data.output ? formatMarkdown(data.output) : '<span class="live-placeholder">Waiting for model output…</span>';
    });
    return true;
  }

  function handleWsEvent(payload) {
    if (payload.type === 'history') {
      const session = payload.session || {};
      state.sessionId = session.id || state.sessionId;
      state.messages = session.messages || [];
      state.stages = session.stages || [];
      // Recover run state after a reconnect: if no stage is still running, the
      // segment finished (a gate opened or the answer arrived) while we were
      // offline — settle the UI and stop polling. Otherwise keep showing work.
      const stages = state.stages || [];
      const anyRunning = stages.some((s) => s.status === 'running');
      if (state.running && !anyRunning) {
        setRunning(false);
        stopHistoryPolling();
      }
      renderMessages();
      renderStageStrip();
      // Reload the schema/uploads so a recovered schema gate renders fully.
      refreshSidebarData();
      return;
    }
    if (payload.type === 'message') {
      // Skip the server echo of a user message we already showed optimistically,
      // so it does not appear twice.
      const msg = payload.message || {};
      if (msg.role === 'user') {
        const idx = state.pendingOptimistic.indexOf(String(msg.content || '').trim());
        if (idx !== -1) {
          state.pendingOptimistic.splice(idx, 1);
          return;
        }
      }
      state.messages.push(payload.message);
      renderMessages();
      renderSessionRail();
      return;
    }
    if (payload.type === 'run_start') {
      state.liveStream = {};
      state.forceScroll = true;
      setRunning(true, 'The agent is working on your request…');
      // Render immediately so the coordinator banner shows up at once instead of
      // leaving the user staring at a blank screen for several seconds while the
      // main agent thinks before its first delegation.
      renderMessages();
      renderStageStrip();
      return;
    }
    if (payload.type === 'stage') {
      if (payload.stages) state.stages = payload.stages;
      if (payload.status === 'done' && payload.stage) delete state.liveStream[payload.stage];
      renderStageStrip();
      renderProgressTab();
      if (payload.status === 'running') {
        // A new business step started: bring the user to the live work.
        state.forceScroll = true;
        renderMessages();
      } else {
        renderMessages({ preserveScroll: true });
      }
      if (payload.status === 'running' && payload.detail) {
        el.runDetail.textContent = payload.detail;
      }
      return;
    }
    if (payload.type === 'stream') {
      if (payload.stage) {
        state.liveStream[payload.stage] = {
          thinking: payload.thinking || '',
          output: payload.output || '',
        };
        // Targeted in-place update; only fall back to a full render if the live
        // group is not on screen yet.
        if (!updateLiveStreamDom()) renderMessages({ preserveScroll: true });
      }
      return;
    }
    if (payload.type === 'activity') {
      state.messages.push(payload.message);
      renderMessages({ preserveScroll: true });
      return;
    }
    if (payload.type === 'assistant_final') {
      state.messages.push(payload.message);
      if (payload.stages) state.stages = payload.stages;
      // Surface the deliverable (a confirmation gate or the final answer).
      state.forceScroll = true;
      renderMessages();
      renderStageStrip();
      refreshSidebarData();
      renderSessionRail();
      return;
    }
    if (payload.type === 'error') {
      state.messages.push(payload.message);
      renderMessages();
      return;
    }
    if (payload.type === 'run_done') {
      state.liveStream = {};
      setRunning(false);
      stopHistoryPolling();
      renderMessages();
      renderProgressTab();
    }
  }

  // Each running step card anchors its timer to that step's own server-stamped
  // start (`stage.started_at`), so the clock measures the current subagent's
  // elapsed time and resets when the next subagent takes over. It falls back to
  // the segment start only if no per-step stamp is available.
  function formatElapsed(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    const ss = String(s % 60).padStart(2, '0');
    return `${m}m ${ss}s`;
  }

  // Update every live timer in place (text only) so the per-second tick never
  // triggers a full re-render — that would reset animations and the scroll
  // position the user is reading.
  function tickElapsed() {
    const now = Date.now();
    document.querySelectorAll('.work-elapsed[data-since]').forEach((node) => {
      const since = Number(node.getAttribute('data-since')) || now;
      node.textContent = formatElapsed(now - since);
    });
  }

  function setRunning(running, detail) {
    // Start the clock once, on the transition into a running segment, so it
    // counts continuously from start to end instead of resetting on each event.
    if (running && !state.running) state.runStartedAt = Date.now();
    state.running = running;
    el.runIndicator.classList.toggle('active', running);
    el.runDetail.textContent = running ? (detail || '') : '';
    el.send.disabled = running;
  }

  function pushClientError(message) {
    state.messages.push({
      id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      role: 'system',
      content: message,
      timestamp: new Date().toISOString(),
    });
    renderMessages();
  }

  function pushOptimisticUserMessage(payload) {
    if (!payload || payload.type !== 'chat') return '';
    const content = String(payload.content || '').trim();
    if (!content) return '';
    const uploadNames = (payload.upload_ids || [])
      .map((id) => (state.uploads.find((upload) => upload.id === id) || {}).name)
      .filter(Boolean);
    state.messages.push({
      id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      role: 'user',
      content,
      uploads: uploadNames,
      timestamp: new Date().toISOString(),
    });
    state.forceScroll = true;
    renderMessages();
    renderSessionRail();
    return content;
  }

  async function sendViaHttp(payload, { optimistic = true } = {}) {
    const optimisticContent = optimistic ? pushOptimisticUserMessage(payload) : '';
    let skippedOptimisticEcho = false;
    setRunning(true, 'The agent is working on your request…');
    try {
      const data = await api(`/api/chat/${state.sessionId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      (data.events || []).forEach((event) => {
        const message = event && event.message;
        if (
          optimisticContent
          && !skippedOptimisticEcho
          && event.type === 'message'
          && message
          && message.role === 'user'
          && message.content === optimisticContent
        ) {
          skippedOptimisticEcho = true;
          return;
        }
        handleWsEvent(event);
      });
    } catch (err) {
      pushClientError(`Send failed: ${err.message}`);
    } finally {
      setRunning(false);
    }
  }

  function sendMessage() {
    const content = el.input.value.trim();
    if (!content || state.running) return;
    const uploadIds = Array.from(state.selectedUploads);
    const payload = { type: 'chat', content, upload_ids: uploadIds };
    if (state.wsReady) {
      // Show the user's message and the working state at once — do not wait for
      // the server to echo it back, which is what made the UI look frozen for
      // several seconds after asking a question.
      const content2 = pushOptimisticUserMessage(payload);
      if (content2) state.pendingOptimistic.push(content2);
      setRunning(true, 'The agent is working on your request…');
      state.ws.send(JSON.stringify(payload));
    } else {
      sendViaHttp(payload);
    }
    el.input.value = '';
    state.selectedUploads.clear();
    renderAttachments();
    autoSizeInput();
  }

  function sendQuickReply(text) {
    if (state.running) return;
    const payload = { type: 'chat', content: text, upload_ids: [] };
    state.forceScroll = true;
    // A confirmation reply: the server posts its own localized acknowledgement
    // bubble, so don't show an optimistic one (it would duplicate). Still flip
    // to the working state at once so the click gives immediate feedback.
    if (state.wsReady) {
      setRunning(true, 'The agent is working on your request…');
      state.ws.send(JSON.stringify(payload));
    } else {
      sendViaHttp(payload, { optimistic: false });
    }
  }

  // ── Chat rendering ──────────────────────────────────────────────────────

  function gateActions(message) {
    if (state.running) return '';
    const last = state.messages[state.messages.length - 1];
    if (!last || last.id !== message.id) return '';
    const waiting = (state.stages || []).find((stage) => stage.status === 'waiting');
    if (!waiting) return '';
    if (waiting.id === 'confirm_problem' && message.clarification) {
      return `
        <div class="gate-actions clarification-actions">
          <button class="gate-confirm" data-action="confirm-clarification-direct" data-message-id="${escapeHtml(message.id)}">
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path fill="currentColor" d="M13.78 3.22a.75.75 0 0 1 0 1.06l-7.25 7.25a.75.75 0 0 1-1.06 0L2.22 8.28a.75.75 0 1 1 1.06-1.06L6 9.94l6.72-6.72a.75.75 0 0 1 1.06 0Z"/></svg>
            <span>Confirm &amp; Continue</span>
          </button>
          <button class="gate-edit" data-action="edit-clarification" data-message-id="${escapeHtml(message.id)}">
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path fill="currentColor" d="M11.54 1.74a2.1 2.1 0 0 1 2.97 2.97l-7.68 7.68a.75.75 0 0 1-.34.2l-3.43.9a.75.75 0 0 1-.92-.92l.9-3.43a.75.75 0 0 1 .2-.34l7.68-7.68Zm1.91 1.06a.6.6 0 0 0-.85 0l-.58.58.85.85.58-.58a.6.6 0 0 0 0-.85Zm-1.64 2.49-.85-.85-6.5 6.5-.36 1.37 1.37-.36 6.34-6.66Z"/></svg>
            <span>Edit</span>
          </button>
        </div>
      `;
    }
    const schemaGate = waiting.id === 'confirm_schema';
    return `
      <div class="gate-actions">
        <button class="gate-confirm" data-action="confirm">
          <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path fill="currentColor" d="M13.78 3.22a.75.75 0 0 1 0 1.06l-7.25 7.25a.75.75 0 0 1-1.06 0L2.22 8.28a.75.75 0 1 1 1.06-1.06L6 9.94l6.72-6.72a.75.75 0 0 1 1.06 0Z"/></svg>
          <span>Confirm &amp; Continue</span>
        </button>
        ${schemaGate ? '<button class="gate-open-schema" data-action="open-schema">Open Schema Studio</button>' : ''}
      </div>
    `;
  }

  function renderClarificationCard(clarification, actionsHtml = '') {
    const steps = (clarification.steps || [])
      .map((step, index) => `<li><span>${index + 1}</span><p>${formatInline(step)}</p></li>`)
      .join('');
    return `
      <section class="clarification-card">
        <div class="clarification-head">
          <span class="clarification-kicker">Problem clarification</span>
          <h3>Clarified question</h3>
        </div>
        <div class="clarification-problem">
          <span>Problem</span>
          <strong>${formatInline(clarification.problem || '')}</strong>
        </div>
        <div class="clarification-plan">
          <span>Plan</span>
          <ol>${steps}</ol>
        </div>
        ${actionsHtml}
      </section>
    `;
  }

  function analysisCompleteHtml(detail = 'Analysis complete. Review the confirmation card below before continuing.') {
    return `
      <section class="analysis-complete-card">
        <div class="analysis-complete-head">
          <span class="analysis-complete-icon">O</span>
          <div>
            <strong>Analysis complete</strong>
            <p>${escapeHtml(detail)}</p>
          </div>
        </div>
      </section>
    `;
  }

  function isSchemaReviewMessage(message) {
    const content = String(message.content || '');
    return content.includes('Schema path:')
      || content.includes('Draft schema')
      || content.includes('The draft schema is ready')
      || content.includes('Judgment:')
      || content.includes('**Relation Schema**')
      || content.includes('**Entity Definitions**');
  }

  function schemaReviewState() {
    const confirmStage = (state.stages || []).find((stage) => stage.id === 'confirm_schema');
    const waiting = !!(confirmStage && confirmStage.status === 'waiting');
    const confirmed = !!(state.schema && state.schema.status === 'confirmed');
    if (waiting) {
      return {
        statusClass: 'waiting', statusLabel: 'Waiting',
        heading: 'Review schema before continuing',
        copy: 'Confirm the entity definitions and relation schema below, or open Schema Studio to edit them.',
      };
    }
    if (confirmed) {
      return {
        statusClass: 'confirmed', statusLabel: 'Confirmed',
        heading: 'Confirmed ontology schema',
        copy: 'This schema was confirmed and used for data extraction and solving.',
      };
    }
    return {
      statusClass: 'draft', statusLabel: 'Draft',
      heading: 'Ontology schema draft',
      copy: 'Draft schema generated from the evidence.',
    };
  }

  // Human-readable list of an entity's primitive attributes (the columns the
  // data extractor will fill). Keeps the schema cards honest: a class is never
  // just a name + type, it carries its real fields.
  function attributesText(item) {
    const attrs = (item && item.attributes) || [];
    if (!attrs.length) return '';
    return attrs
      .map((a) => `${a.name}${a.optional ? '?' : ''}: ${a.value_type || 'str'}`)
      .join(', ');
  }

  function schemaPreviewTablesHtml() {
    const entities = state.schemaForm.filter((item) => item.type === 'entity');
    const relations = state.schemaForm.filter((item) => item.type === 'relation');
    const view = schemaReviewState();
    if (!entities.length && !relations.length) {
      return `
        <section class="schema-review-card">
          <div class="schema-review-head">
            <div>
              <span class="run-section-label">Schema confirmation</span>
              <h3>${escapeHtml(view.heading)}</h3>
            </div>
            <span class="schema-review-status ${view.statusClass}">${escapeHtml(view.statusLabel)}</span>
          </div>
          <div class="onto-empty">Schema preview is loading.</div>
        </section>
      `;
    }
    const entityMeta = new Map(entities.map((item) => [item.name, item]));
    const entityRows = entities.map((item) => `
      <tr>
        <td>${escapeHtml(item.name)}</td>
        <td>${escapeHtml(item.entity_type || '')}</td>
        <td>${escapeHtml(item.value_type || 'str')}</td>
        <td>${escapeHtml(attributesText(item)) || '<span class="onto-muted">—</span>'}</td>
      </tr>
    `).join('');
    const relationRows = relations.map((item) => {
      const head = entityMeta.get(item.head_entity) || {};
      const tail = entityMeta.get(item.tail_entity) || {};
      return `
        <tr>
          <td>${escapeHtml(item.head_entity)}</td>
          <td>${escapeHtml(head.entity_type || '')}</td>
          <td>${escapeHtml(head.value_type || 'str')}</td>
          <td>${escapeHtml(item.relation)}</td>
          <td>${escapeHtml(item.tail_entity)}</td>
          <td>${escapeHtml(tail.entity_type || '')}</td>
          <td>${escapeHtml(tail.value_type || 'str')}</td>
        </tr>
      `;
    }).join('');
    return `
      <section class="schema-review-card">
        <div class="schema-review-head">
          <div>
            <span class="run-section-label">Schema confirmation</span>
            <h3>${escapeHtml(view.heading)}</h3>
          </div>
          <span class="schema-review-status ${view.statusClass}">${escapeHtml(view.statusLabel)}</span>
        </div>
        <p class="schema-review-copy">${escapeHtml(view.copy)}</p>
        <div class="schema-download-row">
          <a href="/api/schema/download?session_id=${encodeURIComponent(state.sessionId)}&kind=python" target="_blank" rel="noopener">Download Python schema</a>
          <a href="/api/schema/download?session_id=${encodeURIComponent(state.sessionId)}&kind=entities" target="_blank" rel="noopener">Download entity table</a>
          <a href="/api/schema/download?session_id=${encodeURIComponent(state.sessionId)}&kind=relations" target="_blank" rel="noopener">Download relation table</a>
        </div>
        <div class="schema-preview-card">
          <h4>Entity Definitions</h4>
          <div class="md-table-wrap"><table class="md-table onto-schema-table schema-entity-table">
            <thead><tr><th>Entity</th><th>Entity Type</th><th>Entity Data Type</th><th>Attributes</th></tr></thead>
            <tbody>${entityRows || '<tr><td colspan="4">None</td></tr>'}</tbody>
          </table></div>
          <h4>Relation Schema</h4>
          <div class="md-table-wrap"><table class="md-table onto-schema-table schema-relation-table">
            <thead><tr><th>Head Entity</th><th>Head Entity Type</th><th>Head Entity Data Type</th><th>Relation Name</th><th>Tail Entity</th><th>Tail Entity Type</th><th>Tail Entity Data Type</th></tr></thead>
            <tbody>${relationRows || '<tr><td colspan="7">None</td></tr>'}</tbody>
          </table></div>
        </div>
      </section>
    `;
  }

  function schemaReviewActionsHtml() {
    if (state.running) return '';
    const waiting = (state.stages || []).find((stage) => stage.id === 'confirm_schema' && stage.status === 'waiting');
    const draftSchema = state.schema && state.schema.status === 'draft';
    if (!waiting && !draftSchema) return '';
    return `
      <div class="gate-actions schema-review-actions">
        <button class="gate-confirm" data-action="confirm">
          <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path fill="currentColor" d="M13.78 3.22a.75.75 0 0 1 0 1.06l-7.25 7.25a.75.75 0 0 1-1.06 0L2.22 8.28a.75.75 0 1 1 1.06-1.06L6 9.94l6.72-6.72a.75.75 0 0 1 1.06 0Z"/></svg>
          <span>Confirm &amp; Continue</span>
        </button>
        <button class="gate-open-schema" data-action="open-schema">Open Schema Studio</button>
      </div>
    `;
  }

  function renderAssistantContent(message) {
    if (message.clarification) {
      const actions = gateActions(message);
      const detail = actions
        ? 'Problem clarification is ready for confirmation.'
        : 'Problem clarification confirmed.';
      return `${analysisCompleteHtml(detail)}${renderClarificationCard(message.clarification, actions)}`;
    }
    if (isSchemaReviewMessage(message)) {
      const view = schemaReviewState();
      const detail = view.statusClass === 'waiting'
        ? 'Schema analysis is complete. Review the schema below before extraction starts.'
        : (view.statusClass === 'confirmed'
          ? 'Schema confirmed. It was used for data extraction and solving.'
          : 'Schema analysis is complete.');
      return `${analysisCompleteHtml(detail)}${schemaPreviewTablesHtml()}${schemaReviewActionsHtml()}`;
    }
    return `${formatMarkdown(message.content)}${gateActions(message)}`;
  }

  function stageById(stageId) {
    return (state.stages || []).find((stage) => stage.id === stageId) || null;
  }

  function stageStatusText(status) {
    return {
      pending: 'Pending',
      running: 'Working',
      waiting: 'Waiting for confirmation',
      done: 'Done',
    }[status] || 'Working';
  }

  function stageCardIcon(status) {
    if (status === 'done') return '✓';
    if (status === 'waiting') return '!';
    if (status === 'running') return '●';
    return '○';
  }

  function inferActiveStageId() {
    const active = (state.stages || []).find((stage) => ['running', 'waiting'].includes(stage.status));
    if (active) return active.id;
    const progressed = (state.stages || []).filter((stage) => stage.status !== 'pending');
    return progressed.length ? progressed[progressed.length - 1].id : '';
  }

  function makeStageCard(stageId, title) {
    const stage = stageById(stageId);
    return {
      id: stageId,
      title: (stage && stage.label) || title || 'Processing step',
      status: (stage && stage.status) || 'running',
      thinking: '',
      output: '',
      toolSteps: [],
    };
  }

  function buildStageCards(events) {
    const eventList = events || [];
    const hasEvents = eventList.length > 0;
    const cards = new Map();
    let activeStageId = hasEvents ? '' : inferActiveStageId();
    if (!hasEvents) {
      (state.stages || []).filter((stage) => stage.status !== 'pending').forEach((stage) => {
        cards.set(stage.id, makeStageCard(stage.id, stage.label));
      });
    }
    eventList.forEach((message) => {
      if (!message || message.kind === 'run_start') return;
      if (message.kind === 'stage' && message.stage) {
        activeStageId = message.stage;
        if (!cards.has(activeStageId)) cards.set(activeStageId, makeStageCard(activeStageId, message.title));
        const card = cards.get(activeStageId);
        card.title = message.title || card.title;
        card.status = (stageById(activeStageId) || {}).status || message.status || card.status;
        if (message.thinking) card.thinking = normalizeActivityText(message.thinking);
        if (message.output) card.output = normalizeActivityText(message.output);
        return;
      }
      if (message.kind === 'tool') {
        const stageId = activeStageId || inferActiveStageId();
        if (!stageId) return;
        if (!cards.has(stageId)) cards.set(stageId, makeStageCard(stageId, stageById(stageId)?.label));
        const card = cards.get(stageId);
        const label = normalizeActivityText(message.content || 'Running an internal tool.');
        const stepStatus = message.status === 'done' ? 'done' : 'running';
        const stepOutput = message.tool_output ? normalizeActivityText(message.tool_output) : '';
        const cid = message.tool_call_id || `${stageId}:${label}`;
        let step = card.toolSteps.find((item) => item.id === cid);
        if (!step) {
          step = { id: cid, label, status: stepStatus, output: stepOutput };
          card.toolSteps.push(step);
        } else {
          if (label) step.label = label;
          if (stepStatus === 'done') step.status = 'done';
          if (stepOutput) step.output = stepOutput;
        }
      }
    });
    // Cards come only from this group's own event buffer; we never inject the
    // global live-stream stages here (doing so leaked a later run's stages into
    // earlier run groups). liveStream is used purely to enrich existing cards.
    return (state.stages || [])
      .filter((stage) => stage.status !== 'pending' && cards.has(stage.id))
      .map((stage) => {
        const card = cards.get(stage.id);
        if (!hasEvents || stage.status === 'done' || stage.id === inferActiveStageId()) {
          card.status = stage.status || card.status;
        }
        card.title = stage.label || card.title;
        // Carry the server-stamped per-step start so the live timer measures the
        // current subagent's own elapsed time and resets when the step changes.
        if (stage.started_at) card.startedAt = stage.started_at;
        const live = (state.liveStream || {})[stage.id];
        if (live && stage.status !== 'done') {
          if (live.thinking) card.thinking = normalizeActivityText(live.thinking);
          if (live.output) card.output = normalizeActivityText(live.output);
        }
        return card;
      });
  }

  // Fixed single tool-activity bar that mirrors the reference KC-Agent UI:
  // it always shows the latest tool call and floats up only when the tool
  // actually changes, instead of growing an ever-longer list.
  function renderTaskToolActivity(card, status) {
    const running = status === 'running';
    const steps = card.toolSteps || [];
    const stepCount = steps.length;
    const latestStep = stepCount ? steps[stepCount - 1] : null;
    const latest = latestStep ? latestStep.label : '';
    const latestDone = latestStep ? latestStep.status === 'done' : false;

    let swapped = false;
    if (running && latestStep) {
      if (!state.toolKeys) state.toolKeys = {};
      const key = `${card.id}:${latestStep.id}:${latestStep.status}`;
      swapped = state.toolKeys[card.id] !== key;
      state.toolKeys[card.id] = key;
    }

    let statusClass = 'calling';
    let label = 'Working';
    let showDots = false;
    if (status === 'done') {
      statusClass = 'done';
      label = 'Done';
    } else if (status === 'waiting') {
      statusClass = 'complete';
      label = 'Ready';
    } else if (running && latestStep && latestDone) {
      // The current tool just finished; show its Done state until the next
      // tool starts, exactly like the reference UI's tool_end transition.
      statusClass = 'done';
      label = 'Done';
    } else if (running && latestStep) {
      statusClass = 'calling';
      label = 'Working';
      showDots = true;
    } else if (running) {
      statusClass = 'context';
      label = 'Processing';
      showDots = true;
    } else {
      statusClass = 'done';
      label = 'Done';
    }

    let title;
    let detail;
    if (latest) {
      // One fixed sentence bound to the tool name — never the raw tool output
      // or arguments, mirroring the reference KC-Agent tool bar.
      title = latest;
      detail = '';
    } else if (status === 'done' || status === 'waiting') {
      title = `${card.title || 'This step'} ready`;
      detail = '';
    } else {
      title = `Preparing ${card.title || 'the next step'}…`;
      detail = 'Planning the next action.';
    }

    const stepLabel = stepCount > 0 ? `Step ${stepCount}` : '';
    return `
      <div class="current-tool-card ${escapeHtml(statusClass)}${swapped ? ' tool-swapping' : ''}">
        <div class="current-tool-topline">
          <span class="current-tool-status">${escapeHtml(label)}</span>
          ${stepLabel ? `<span class="tool-step-label">${escapeHtml(stepLabel)}</span>` : ''}
          ${showDots ? '<span class="tool-dots"><span></span><span></span><span></span></span>' : ''}
        </div>
        <div class="current-tool-main">
          <strong>${escapeHtml(sanitizeDisplayText(title))}</strong>
          ${detail ? `<span>${escapeHtml(detail)}</span>` : ''}
        </div>
      </div>`;
  }

  // A stage card only stays "working" when it belongs to the current run and a
  // run is actually in progress. Earlier runs (above the latest user message)
  // and any stage left over after a run ends collapse to the completed format,
  // so "Agent is working" never lingers on a finished step.
  function resolveCardStatus(card, isLatest) {
    if (card.status === 'done') return 'done';
    if (card.status === 'pending') return 'pending';
    if (card.status === 'waiting') return (isLatest && !state.running) ? 'waiting' : 'done';
    return (isLatest && state.running) ? 'running' : 'done';
  }

  function renderTaskNode(card, isLatest) {
    const status = resolveCardStatus(card, isLatest);
    const isDone = status === 'done';
    const isPending = status === 'pending';
    const isWaiting = status === 'waiting';
    const isRunning = status === 'running';
    if (isPending) return '';
    const expanded = isRunning || isWaiting || state.expandedStageCards.has(card.id);
    const steps = card.toolSteps || [];
    const toolCount = Math.max(steps.length, 1);
    const thinking = card.thinking || '';
    const output = card.output || '';
    const detailHtml = `
      <div class="run-tool-pane">
        <span class="run-section-label">Tool activity</span>
        ${renderTaskToolActivity(card, status)}
      </div>
      <div class="run-reasoning-pane">
        <span class="run-section-label">Model thinking</span>
        <div class="run-reasoning-output">${thinking ? escapeHtml(sanitizeDisplayText(thinking)) : '<span class="live-placeholder">Waiting for live model update…</span>'}</div>
      </div>
      <div class="run-model-pane">
        <span class="run-section-label">Model output</span>
        <div class="run-model-output">${output ? formatMarkdown(output) : '<span class="live-placeholder">Waiting for model output…</span>'}</div>
      </div>
    `;
    if (isDone) {
      return `
        <section class="task-node done ${expanded ? 'expanded' : 'folded'}" data-stage="${escapeHtml(card.id)}">
          <div class="run-card ontology-task-card complete">
            <div class="run-card-head completed-task-head">
              <div class="task-node-title">
                <span class="run-check">✓</span>
                ${agentBadgeHtml(card.id)}
                <span class="task-node-stage">${escapeHtml(card.title)}</span>
              </div>
              <div class="task-node-actions">
                <span class="run-count">${toolCount} tool updates</span>
                <button class="task-toggle-button" type="button" data-stage-card="${escapeHtml(card.id)}" aria-expanded="${expanded ? 'true' : 'false'}">
                  ${expanded ? 'Hide' : 'Open'}
                </button>
              </div>
            </div>
            ${expanded ? detailHtml : ''}
          </div>
        </section>
      `;
    }
    const workingLabel = isWaiting ? '等待确认' : `${escapeHtml(stageAgent(card.id).label)} 执行中`;
    return `
      <section class="task-node ${escapeHtml(status)} expanded" data-stage="${escapeHtml(card.id)}">
        <div class="run-card ontology-task-card working">
          <div class="run-card-head">
            <div class="task-node-title">
              <span class="${isWaiting ? 'run-check' : 'run-pulse'}">${isWaiting ? '✓' : ''}</span>
              ${agentBadgeHtml(card.id)}
              <span class="task-node-stage">${escapeHtml(card.title)}</span>
              <span class="task-node-working">${workingLabel}</span>
              ${isRunning ? `<span class="work-elapsed" data-since="${card.startedAt || state.runStartedAt || Date.now()}" title="Elapsed time on the current step">${formatElapsed(Date.now() - (card.startedAt || state.runStartedAt || Date.now()))}</span>` : ''}
            </div>
            <span class="run-count">${toolCount} tool updates</span>
          </div>
          ${detailHtml}
        </div>
      </section>
    `;
  }

  // Horizontal timeline summarising a finished run as a connected line of step
  // nodes. `reveal` triggers the one-time staggered "switch" animation (each
  // node pops in and the connector segment fills) — it is gated by the caller
  // so it plays exactly once per group and never replays on re-render.
  function renderRunTimeline(cards, isLatest, reveal) {
    const nodes = cards.map((card, idx) => {
      const status = resolveCardStatus(card, isLatest);
      const cls = status === 'done' ? 'done' : (status === 'running' ? 'running' : (status === 'waiting' ? 'waiting' : 'pending'));
      const glyph = status === 'done' ? '✓' : (status === 'running' || status === 'waiting' ? '' : String(idx + 1));
      const style = reveal ? ` style="--ti:${idx}"` : '';
      return `
        <li class="run-timeline-step ${cls}"${style}>
          <span class="run-timeline-line" aria-hidden="true"></span>
          <span class="run-timeline-dot">${glyph}</span>
          <span class="run-timeline-label">${escapeHtml(sanitizeDisplayText(card.title || 'Step'))}</span>
        </li>`;
    }).join('');
    return `<ol class="run-timeline${reveal ? ' revealing' : ''}">${nodes}</ol>`;
  }

  function renderStagePipeline(cards, isLatest, groupKey) {
    if (!cards.length) {
      // A segment is running but its first task card has not landed yet: still
      // show the coordinator banner so feedback is immediate (the coordinator's
      // own reasoning streams on the __coordinator__ lane).
      if (isLatest && state.running) {
        return `
        <article class="message event stage-pipeline-message orchestration">
          ${coordinatorBannerHtml(true)}
        </article>`;
      }
      return '';
    }
    // The current run stays fully expanded with the live tool bar.
    if (isLatest) {
      return `
        <article class="message event stage-pipeline-message orchestration">
          ${coordinatorBannerHtml(true)}
          <div class="task-node-list subagent-lane">
            ${cards.map((card) => renderTaskNode(card, isLatest)).join('')}
          </div>
        </article>
      `;
    }
    // Earlier/finished runs collapse to a slim timeline so a completed phase
    // never competes for attention with the live one.
    const expanded = state.expandedGroups.has(groupKey);
    const stepWord = cards.length > 1 ? 'steps' : 'step';
    const reveal = !state.revealedGroups.has(groupKey);
    if (reveal) state.revealedGroups.add(groupKey);
    const barHead = `
      <div class="stage-group-bar-head">
        <span class="stage-group-summary-title">
          <span class="coordinator-pill"><span class="coordinator-pill-glyph">${escapeHtml(COORDINATOR_AGENT.icon)}</span>Coordinator</span>
          <span class="run-check">✓</span> 已编排 ${cards.length} ${stepWord}
        </span>
        <button class="task-toggle-button stage-group-toggle" type="button" data-stage-group="${escapeHtml(groupKey)}" aria-expanded="${expanded}">${expanded ? 'Hide' : 'Show details'}</button>
      </div>
    `;
    // Collapsed: a slim bar carrying the horizontal step timeline.
    if (!expanded) {
      const timeline = renderRunTimeline(cards, isLatest, reveal);
      return `<article class="message event stage-pipeline-message collapsed-group"><div class="stage-group-bar">${barHead}${timeline}</div></article>`;
    }
    // Expanded: one cohesive panel — header on top, the steps laid out as a
    // vertical timeline below. The horizontal timeline is dropped here because
    // it would just duplicate the per-step rows.
    return `
      <article class="message event stage-pipeline-message expanded-group">
        <div class="stage-group-wrap">
          <div class="stage-group-bar">${barHead}</div>
          <div class="task-node-list">
            ${cards.map((card) => renderTaskNode(card, isLatest)).join('')}
          </div>
        </div>
      </article>
    `;
  }

  function buildMessageTimeline(messages) {
    const items = [];
    let eventBuffer = [];
    const flushEvents = (allowEmpty = false) => {
      if (!eventBuffer.length) return;
      const cards = buildStageCards(eventBuffer);
      if (cards.length || (allowEmpty && state.running)) {
        const groupKey = cards.length ? cards.map((card) => card.id).join('-') : 'live';
        items.push({ role: 'pipeline', cards, groupKey });
      }
      eventBuffer = [];
    };
    messages.forEach((message) => {
      if (message.role === 'event') {
        // A run_start marks the boundary of a new run: flush the previous run's
        // events into their own group so stages never bleed across runs.
        if (message.kind === 'run_start' && eventBuffer.length) flushEvents();
        eventBuffer.push(message);
        return;
      }
      flushEvents();
      items.push(message);
    });
    flushEvents(true);
    if (!items.some((item) => item.role === 'pipeline') && (state.running || (state.stages || []).some((stage) => stage.status !== 'pending'))) {
      items.push({ role: 'pipeline', cards: buildStageCards([]), groupKey: 'live' });
    }
    // Only the final pipeline group (the current run) may show live "working"
    // state; everything above it renders as completed.
    const lastPipelineIndex = items.reduce((acc, item, idx) => (item.role === 'pipeline' ? idx : acc), -1);
    items.forEach((item, idx) => {
      if (item.role === 'pipeline') item.isLatest = idx === lastPipelineIndex;
    });
    return items;
  }

  function clarifyStepRowHtml(value) {
    return `
      <div class="clarify-step">
        <span class="clarify-step-no"></span>
        <input class="clarify-step-input" type="text" value="${escapeHtml(sanitizeDisplayText(value))}" placeholder="Describe this step">
        <button type="button" class="clarify-step-remove" title="Remove step" aria-label="Remove step">×</button>
      </div>
    `;
  }

  function clarifyFormHtml(clarification) {
    return `
      <div class="clarify-form">
        <div class="clarify-form-head">
          <h4>Review &amp; edit before continuing</h4>
          <p>Adjust the problem statement and solution steps below, then confirm.</p>
        </div>
        <label class="clarify-label">Problem</label>
        <textarea class="clarify-problem" rows="2">${escapeHtml(sanitizeDisplayText(clarification.problem || ''))}</textarea>
        <label class="clarify-label">Solution steps</label>
        <div class="clarify-steps">${(clarification.steps || []).map(clarifyStepRowHtml).join('')}</div>
        <button type="button" class="clarify-add-step">+ Add step</button>
        <div class="gate-actions">
          <button class="gate-confirm" data-action="confirm-clarification">Confirm &amp; Continue</button>
        </div>
      </div>
    `;
  }

  function sendClarification(clarification) {
    const problem = String(clarification.problem || '').trim();
    const steps = (clarification.steps || []).map((step) => String(step || '').trim()).filter(Boolean);
    if (!problem || !steps.length || state.running) return;
    const payload = { type: 'confirm_problem', problem, steps };
    state.forceScroll = true;
    if (state.wsReady) {
      setRunning(true, 'The agent is working on your request…');
      state.ws.send(JSON.stringify(payload));
    } else {
      sendViaHttp(payload, { optimistic: false });
    }
  }

  function renumberClarifyModalSteps() {
    if (!el.clarifyModalSteps) return;
    el.clarifyModalSteps.querySelectorAll('.clarify-step-no').forEach((badge, index) => {
      badge.textContent = index + 1;
    });
  }

  function bindClarifyModalRemove(row) {
    row.querySelector('.clarify-step-remove').addEventListener('click', () => {
      row.remove();
      renumberClarifyModalSteps();
    });
  }

  function renderClarifyModalSteps(steps) {
    el.clarifyModalSteps.innerHTML = (steps || []).map(clarifyStepRowHtml).join('');
    el.clarifyModalSteps.querySelectorAll('.clarify-step').forEach(bindClarifyModalRemove);
    renumberClarifyModalSteps();
  }

  function openClarificationModal(messageId) {
    const message = state.messages.find((item) => item.id === messageId);
    if (!message || !message.clarification || !el.clarifyOverlay) return;
    state.activeClarificationMessageId = messageId;
    el.clarifyModalProblem.value = sanitizeDisplayText(message.clarification.problem || '');
    renderClarifyModalSteps(message.clarification.steps || []);
    el.clarifyOverlay.hidden = false;
    el.body.classList.add('modal-open');
    window.requestAnimationFrame(() => el.clarifyModalProblem.focus());
  }

  function closeClarificationModal() {
    state.activeClarificationMessageId = '';
    if (el.clarifyOverlay) el.clarifyOverlay.hidden = true;
    el.body.classList.remove('modal-open');
  }

  function saveClarificationModal() {
    const problem = el.clarifyModalProblem.value.trim();
    const steps = Array.from(el.clarifyModalSteps.querySelectorAll('.clarify-step-input'))
      .map((input) => input.value.trim())
      .filter(Boolean);
    if (!problem || !steps.length) {
      alert('The problem statement and at least one step are required.');
      return;
    }
    closeClarificationModal();
    sendClarification({ problem, steps });
  }

  function bindClarifyForm(form) {
    const renumber = () => {
      form.querySelectorAll('.clarify-step-no').forEach((badge, index) => { badge.textContent = index + 1; });
    };
    renumber();
    form.querySelector('.clarify-add-step').addEventListener('click', () => {
      const steps = form.querySelector('.clarify-steps');
      steps.insertAdjacentHTML('beforeend', clarifyStepRowHtml(''));
      bindClarifyRemove(form, steps.lastElementChild, renumber);
      renumber();
      steps.lastElementChild.querySelector('.clarify-step-input').focus();
    });
    form.querySelectorAll('.clarify-step').forEach((row) => bindClarifyRemove(form, row, renumber));
    form.querySelector('[data-action="confirm-clarification"]').addEventListener('click', () => {
      const problem = form.querySelector('.clarify-problem').value.trim();
      const steps = Array.from(form.querySelectorAll('.clarify-step-input'))
        .map((input) => input.value.trim())
        .filter(Boolean);
      if (!problem || !steps.length) {
        alert('The problem statement and at least one step are required.');
        return;
      }
      sendClarification({ problem, steps });
    });
  }

  function bindClarifyRemove(form, row, renumber) {
    row.querySelector('.clarify-step-remove').addEventListener('click', () => {
      row.remove();
      renumber();
    });
  }

  function renderMessages(options = {}) {
    const visible = state.messages.filter((message) => ['user', 'assistant', 'system', 'event'].includes(message.role));
    // Scroll-to-bottom only happens for a genuinely new step (forceScroll is set
    // on new user messages, run start, and each new business stage). Every other
    // re-render preserves the reader's exact scroll position so history never
    // jumps around on its own.
    const shouldAutoScroll = !options.preserveScroll && state.forceScroll;
    state.forceScroll = false;
    const prevScrollY = window.scrollY || document.documentElement.scrollTop || 0;
    const prevMsgTop = el.messages.scrollTop || 0;
    el.hero.style.display = visible.length ? 'none' : '';
    el.messages.classList.toggle('active', visible.length > 0);
    el.messages.innerHTML = buildMessageTimeline(visible).map((message) => {
      if (message.role === 'pipeline') {
        return renderStagePipeline(message.cards || [], message.isLatest, message.groupKey);
      }
      if (message.role === 'user') {
        const uploads = (message.uploads || []).length
          ? `<div class="bubble-uploads">${message.uploads.map((name) => `<span class="bubble-upload-chip">📎 ${escapeHtml(name)}</span>`).join('')}</div>`
          : '';
        return `<article class="message user"><div class="bubble">${escapeHtml(sanitizeDisplayText(message.content))}${uploads}</div></article>`;
      }
      if (message.role === 'assistant') {
        return `
          <article class="message assistant">
            <div class="avatar coordinator-answer-avatar" title="Coordinator">${escapeHtml(COORDINATOR_AGENT.icon)}</div>
            <div class="bubble">
              <div class="coordinator-answer-tag"><span class="coordinator-pill"><span class="coordinator-pill-glyph">${escapeHtml(COORDINATOR_AGENT.icon)}</span>主控 Agent</span><span class="coordinator-answer-note">综合各子Agent结果给出最终答案</span></div>
              ${renderAssistantContent(message)}
            </div>
          </article>
        `;
      }
      return `<article class="message system"><div class="bubble">${escapeHtml(sanitizeDisplayText(message.content || ''))}</div></article>`;
    }).join('');
    el.messages.querySelectorAll('[data-stage-card]').forEach((button) => {
      button.addEventListener('click', () => {
        const stageId = button.getAttribute('data-stage-card');
        if (!stageId) return;
        const stageButtons = Array.from(el.messages.querySelectorAll('[data-stage-card]'))
          .filter((item) => item.getAttribute('data-stage-card') === stageId);
        const stageIndex = Math.max(0, stageButtons.indexOf(button));
        const anchor = button.closest('.task-node') || button;
        const anchorTop = anchor.getBoundingClientRect().top;
        if (state.expandedStageCards.has(stageId)) state.expandedStageCards.delete(stageId);
        else state.expandedStageCards.add(stageId);
        renderMessages({ preserveScroll: true });
        window.requestAnimationFrame(() => {
          const nextButtons = Array.from(el.messages.querySelectorAll('[data-stage-card]'))
            .filter((item) => item.getAttribute('data-stage-card') === stageId);
          const nextButton = nextButtons[Math.min(stageIndex, nextButtons.length - 1)];
          const nextAnchor = nextButton?.closest('.task-node') || nextButton;
          if (!nextAnchor) return;
          window.scrollBy(0, nextAnchor.getBoundingClientRect().top - anchorTop);
        });
      });
    });
    el.messages.querySelectorAll('[data-stage-group]').forEach((button) => {
      button.addEventListener('click', () => {
        const key = button.getAttribute('data-stage-group');
        if (!key) return;
        const anchor = button.closest('.stage-pipeline-message') || button;
        const anchorTop = anchor.getBoundingClientRect().top;
        if (state.expandedGroups.has(key)) state.expandedGroups.delete(key);
        else state.expandedGroups.add(key);
        renderMessages({ preserveScroll: true });
        window.requestAnimationFrame(() => {
          const nextButton = el.messages.querySelector(`[data-stage-group="${(window.CSS && CSS.escape) ? CSS.escape(key) : key}"]`);
          const nextAnchor = nextButton?.closest('.stage-pipeline-message') || nextButton;
          if (!nextAnchor) return;
          window.scrollBy(0, nextAnchor.getBoundingClientRect().top - anchorTop);
        });
      });
    });
    el.messages.querySelectorAll('[data-action="confirm"]').forEach((button) => {
      button.addEventListener('click', () => sendQuickReply('Confirm'));
    });
    el.messages.querySelectorAll('[data-action="confirm-clarification-direct"]').forEach((button) => {
      button.addEventListener('click', () => {
        const message = state.messages.find((item) => item.id === button.getAttribute('data-message-id'));
        if (message && message.clarification) sendClarification(message.clarification);
      });
    });
    el.messages.querySelectorAll('[data-action="edit-clarification"]').forEach((button) => {
      button.addEventListener('click', () => {
        openClarificationModal(button.getAttribute('data-message-id'));
      });
    });
    el.messages.querySelectorAll('[data-action="open-schema"]').forEach((button) => {
      button.addEventListener('click', () => openSchemaModal());
    });
    if (window.Prism) window.Prism.highlightAllUnder(el.messages);
    if (shouldAutoScroll) {
      el.messages.scrollTop = el.messages.scrollHeight;
      scrollToLatestMessage('smooth');
    } else {
      el.messages.scrollTop = prevMsgTop;
      window.scrollTo(0, prevScrollY);
    }
  }

  // ── Stage strip ─────────────────────────────────────────────────────────

  function stageIcon(status) {
    if (status === 'done') return '✓';
    if (status === 'running') return '●';
    if (status === 'waiting') return '✋';
    return '○';
  }

  function renderStageStrip() {
    const stages = state.stages || [];
    const active = stages.some((stage) => stage.status !== 'pending');
    el.stageStrip.hidden = !active;
    if (!active) return;
    el.stageStrip.innerHTML = stages.map((stage) => `
      <span class="onto-stage-chip ${escapeHtml(stage.status)}" title="${escapeHtml(stage.label)}">
        <i>${stageIcon(stage.status)}</i>${escapeHtml(stage.label)}
      </span>
    `).join('<span class="onto-stage-sep"></span>');
  }

  // ── Attachments (input bar) ─────────────────────────────────────────────

  function renderAttachments() {
    const attached = state.uploads.filter((upload) => state.selectedUploads.has(upload.id));
    el.uploadCurrent.textContent = attached.length ? `${attached.length} file(s) attached` : 'No files attached';
    el.attachChips.hidden = !attached.length;
    el.attachChips.innerHTML = attached.map((upload) => `
      <span class="attach-chip">
        📎 ${escapeHtml(upload.name)}
        <button type="button" class="attach-chip-remove" data-detach="${escapeHtml(upload.id)}" title="Remove attachment" aria-label="Remove attachment">×</button>
      </span>
    `).join('');
    el.attachChips.querySelectorAll('[data-detach]').forEach((button) => {
      button.addEventListener('click', () => {
        state.selectedUploads.delete(button.getAttribute('data-detach'));
        renderAttachments();
      });
    });
  }

  async function uploadAndAttachFiles(files) {
    for (const file of files) {
      const form = new FormData();
      form.append('file', file);
      form.append('session_id', state.sessionId);
      try {
        const data = await api('/api/uploads', { method: 'POST', body: form });
        if (data.upload && data.upload.id) state.selectedUploads.add(data.upload.id);
      } catch (err) {
        alert(`Upload failed: ${err.message}`);
      }
    }
    await refreshUploads();
  }

  // ── Panel: evidence tab ─────────────────────────────────────────────────

  async function refreshUploads() {
    try {
      const data = await api(withSession('/api/uploads'));
      state.uploads = data.uploads || [];
      state.selectedUploads.forEach((id) => {
        if (!state.uploads.some((upload) => upload.id === id)) state.selectedUploads.delete(id);
      });
    } catch (err) {
      state.uploads = [];
    }
    renderAttachments();
    if (el.uploadMetric) el.uploadMetric.textContent = `${state.uploads.length} uploaded file(s)`;
  }

  async function renderEvidenceTab() {
    let evidence = { sources: [], needs_web_search: false };
    try { evidence = await api(withSession('/api/evidence')); } catch (err) { /* ignore */ }
    const uploadsHtml = state.uploads.length
      ? state.uploads.map((upload) => `
          <div class="onto-file-row">
            <span class="onto-file-icon">${upload.type === 'csv' ? '📊' : '📄'}</span>
            <div class="onto-file-info">
              <strong>${escapeHtml(upload.name)}</strong>
              <small>${upload.type.toUpperCase()} · ${formatBytes(upload.size)} · ${escapeHtml(upload.uploaded_at || '')}</small>
            </div>
            <button class="onto-file-delete" data-delete="${escapeHtml(upload.id)}" title="Delete file">×</button>
          </div>
        `).join('')
      : '<div class="onto-empty">No files uploaded yet. Upload CSV, TXT or MD files as evidence for schema building and data extraction.</div>';
    const allSources = evidence.sources || [];
    const uploadSources = allSources.filter((source) => source.source_kind !== 'web');
    const webSources = allSources.filter((source) => source.source_kind === 'web');
    const sourceRow = (source) => `
      <div class="onto-evidence-row">
        <span class="onto-evidence-kind ${escapeHtml(source.source_kind || '')}">${source.source_kind === 'web' ? 'Web' : 'Upload'}</span>
        <div class="onto-file-info">
          <strong>${source.url ? `<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener">${escapeHtml(sanitizeDisplayText(source.title || source.source_id))}</a>` : escapeHtml(sanitizeDisplayText(source.title || source.source_id))}</strong>
          <small>${escapeHtml(sanitizeDisplayText(source.reason || ''))}</small>
        </div>
        ${source.source_kind === 'web' ? `<span class="onto-evidence-stage ${source.stage === 'extract' ? 'extract' : 'evidence'}">${source.stage === 'extract' ? 'Extraction' : 'Collection'}</span>` : ''}
      </div>
    `;
    const emptyManifest = '<div class="onto-empty">No evidence manifest yet. Ask a question and confirm it to see the evidence sources used.</div>';
    const sourcesHtml = allSources.length
      ? `
        <h4 class="onto-evidence-group">Uploaded files</h4>
        ${uploadSources.length ? uploadSources.map(sourceRow).join('') : '<div class="onto-empty">No uploaded files were used.</div>'}
        <h4 class="onto-evidence-group">Web sources</h4>
        ${webSources.length ? webSources.map(sourceRow).join('') : '<div class="onto-empty">No web search was needed.</div>'}
      `
      : emptyManifest;
    el.evidenceContent.innerHTML = `
      <div class="onto-section">
        <div class="onto-section-head">
          <h3>Uploaded Files</h3>
          <label class="onto-upload-btn">
            <input type="file" id="file-input" accept=".csv,.txt,.md" hidden>
            <span>+ Upload file</span>
          </label>
        </div>
        <p class="onto-section-hint">Supports CSV / TXT / MD. Use the + button next to the input box to attach files to a question.</p>
        <div class="onto-file-list">${uploadsHtml}</div>
      </div>
      <div class="onto-section">
        <div class="onto-section-head"><h3>Evidence Manifest</h3></div>
        <p class="onto-section-hint">${evidence.needs_web_search ? 'Web search was used to supplement the evidence.' : 'Only local uploaded files were used as evidence.'}</p>
        <div class="onto-file-list">${sourcesHtml}</div>
      </div>
    `;
    const fileInput = el.evidenceContent.querySelector('#file-input');
    if (fileInput) {
      fileInput.addEventListener('change', async () => {
        const file = fileInput.files && fileInput.files[0];
        if (!file) return;
        const form = new FormData();
        form.append('file', file);
        form.append('session_id', state.sessionId);
        try {
          await api('/api/uploads', { method: 'POST', body: form });
          await refreshUploads();
          renderEvidenceTab();
        } catch (err) {
          alert(`Upload failed: ${err.message}`);
        }
      });
    }
    el.evidenceContent.querySelectorAll('[data-delete]').forEach((button) => {
      button.addEventListener('click', async () => {
        await api(withSession(`/api/uploads/${encodeURIComponent(button.getAttribute('data-delete'))}`), { method: 'DELETE' });
        await refreshUploads();
        renderEvidenceTab();
      });
    });
  }

  // ── Panel: schema tab ───────────────────────────────────────────────────

  async function refreshSchema() {
    try {
      state.schema = await api(withSession('/api/schema'));
      state.schemaForm = JSON.parse(JSON.stringify(state.schema.form || []));
      state.schemaDirty = false;
    } catch (err) {
      state.schema = null;
      state.schemaForm = [];
    }
  }

  function schemaStatusBadge(status) {
    if (status === 'confirmed') return '<span class="onto-badge confirmed">Confirmed</span>';
    if (status === 'draft') return '<span class="onto-badge draft">Draft · Pending confirmation</span>';
    return '<span class="onto-badge none">None</span>';
  }

  function renderSchemaTab() {
    const schema = state.schema;
    if (!schema || schema.status === 'none' || !schema.schema_text) {
      el.schemaContent.innerHTML = `
        <div class="onto-section">
          <div class="onto-section-head"><h3>Ontology Schema</h3>${schemaStatusBadge('none')}</div>
          <div class="onto-empty">No schema yet. Ask a question and confirm it, and the agent will build a draft schema here for you to review, edit and confirm.</div>
        </div>
      `;
      return;
    }
    const entities = state.schemaForm.filter((item) => item.type === 'entity');
    const relations = state.schemaForm.filter((item) => item.type === 'relation');
    const entityMeta = new Map(entities.map((item) => [item.name, item]));
    const entityRows = entities.map((item) => `
      <tr>
        <td>${escapeHtml(item.name)}</td>
        <td>${escapeHtml(item.entity_type || '')}</td>
        <td>${escapeHtml(item.value_type || 'str')}</td>
        <td>${escapeHtml(attributesText(item)) || '<span class="onto-muted">—</span>'}</td>
      </tr>
    `).join('');
    const relationRows = relations.map((item) => {
      const head = entityMeta.get(item.head_entity) || {};
      const tail = entityMeta.get(item.tail_entity) || {};
      return `
        <tr>
          <td>${escapeHtml(item.head_entity)}</td>
          <td>${escapeHtml(head.entity_type || '')}</td>
          <td>${escapeHtml(head.value_type || 'str')}</td>
          <td>${escapeHtml(item.relation)}</td>
          <td>${escapeHtml(item.tail_entity)}</td>
          <td>${escapeHtml(tail.entity_type || '')}</td>
          <td>${escapeHtml(tail.value_type || 'str')}</td>
        </tr>
      `;
    }).join('');
    el.schemaContent.innerHTML = `
      <div class="onto-section">
        <div class="onto-section-head"><h3>Ontology Schema</h3>${schemaStatusBadge(schema.status)}</div>
        <p class="onto-section-hint">${schema.status === 'draft' ? 'Draft schema is shown here for read-only review. Use Open Schema Studio in the confirmation card to edit it.' : 'Schema confirmed and in use for data extraction and solving.'}</p>
        <div class="schema-download-row compact">
          <a href="/api/schema/download?session_id=${encodeURIComponent(state.sessionId)}&kind=python" target="_blank" rel="noopener">Download Python schema</a>
          <a href="/api/schema/download?session_id=${encodeURIComponent(state.sessionId)}&kind=entities" target="_blank" rel="noopener">Download entity table</a>
          <a href="/api/schema/download?session_id=${encodeURIComponent(state.sessionId)}&kind=relations" target="_blank" rel="noopener">Download relation table</a>
        </div>
        <h4 class="onto-subhead">Entity Definitions</h4>
        <div class="md-table-wrap"><table class="md-table onto-schema-table schema-entity-table">
          <thead><tr><th>Entity</th><th>Entity Type</th><th>Entity Data Type</th><th>Attributes</th></tr></thead>
          <tbody>${entityRows || '<tr><td colspan="4">None</td></tr>'}</tbody>
        </table></div>
        <h4 class="onto-subhead">Relation Schema</h4>
        <div class="md-table-wrap"><table class="md-table onto-schema-table schema-relation-table">
          <thead><tr><th>Head Entity</th><th>Head Entity Type</th><th>Head Entity Data Type</th><th>Relation Name</th><th>Tail Entity</th><th>Tail Entity Type</th><th>Tail Entity Data Type</th></tr></thead>
          <tbody>${relationRows || '<tr><td colspan="7">None</td></tr>'}</tbody>
        </table></div>
      </div>
      <div class="onto-section">
        <div class="onto-section-head"><h3>Python View</h3></div>
        <pre class="onto-code pretty-code"><code class="language-python">${escapeHtml(sanitizeDisplayText(schema.schema_text))}</code></pre>
      </div>
    `;
    if (window.Prism) window.Prism.highlightAllUnder(el.schemaContent);
  }

  function renderSchemaModal() {
    const schema = state.schema;
    if (!schema || schema.status === 'none' || !schema.schema_text) {
      el.schemaModalBody.innerHTML = '<div class="onto-empty">Schema preview is still loading.</div>';
      return;
    }
    const editable = schema.status === 'draft';
    const entities = state.schemaForm.filter((item) => item.type === 'entity');
    const relations = state.schemaForm.filter((item) => item.type === 'relation');
    const entityMeta = new Map(entities.map((item) => [item.name, item]));
    const entityRows = entities.map((item, index) => `
      <tr>
        <td>${editable ? `<input class="onto-cell-input" data-kind="entity" data-index="${index}" data-field="name" value="${escapeHtml(item.name)}">` : escapeHtml(item.name)}</td>
        <td>${editable ? `<input class="onto-cell-input" data-kind="entity" data-index="${index}" data-field="entity_type" value="${escapeHtml(item.entity_type || '')}">` : escapeHtml(item.entity_type || '')}</td>
        <td>${escapeHtml(item.value_type || 'str')}</td>
        <td>${escapeHtml(attributesText(item)) || '<span class="onto-muted">—</span>'}</td>
      </tr>
    `).join('');
    const relationRows = relations.map((item, index) => {
      const head = entityMeta.get(item.head_entity) || {};
      const tail = entityMeta.get(item.tail_entity) || {};
      return `
        <tr>
          <td>${escapeHtml(item.head_entity)}</td>
          <td>${escapeHtml(head.entity_type || '')}</td>
          <td>${escapeHtml(head.value_type || 'str')}</td>
          <td>${editable ? `<input class="onto-cell-input" data-kind="relation" data-index="${index}" data-field="relation" value="${escapeHtml(item.relation)}">` : escapeHtml(item.relation)}</td>
          <td>${escapeHtml(item.tail_entity)}</td>
          <td>${escapeHtml(tail.entity_type || '')}</td>
          <td>${escapeHtml(tail.value_type || 'str')}</td>
        </tr>
      `;
    }).join('');
    el.schemaModalBody.innerHTML = `
      <div class="schema-modal-grid">
        <section class="schema-preview-card modal-preview">
          <h4>Entity Definitions</h4>
          <div class="md-table-wrap"><table class="md-table onto-schema-table schema-entity-table">
            <thead><tr><th>Entity</th><th>Entity Type</th><th>Entity Data Type</th><th>Attributes</th></tr></thead>
            <tbody>${entityRows || '<tr><td colspan="4">None</td></tr>'}</tbody>
          </table></div>
          <h4>Relation Schema</h4>
          <div class="md-table-wrap"><table class="md-table onto-schema-table schema-relation-table">
            <thead><tr><th>Head Entity</th><th>Head Entity Type</th><th>Head Entity Data Type</th><th>Relation Name</th><th>Tail Entity</th><th>Tail Entity Type</th><th>Tail Entity Data Type</th></tr></thead>
            <tbody>${relationRows || '<tr><td colspan="7">None</td></tr>'}</tbody>
          </table></div>
          <div class="schema-download-row compact">
            <a href="/api/schema/download?session_id=${encodeURIComponent(state.sessionId)}&kind=python" target="_blank" rel="noopener">Download Python schema</a>
            <a href="/api/schema/download?session_id=${encodeURIComponent(state.sessionId)}&kind=entities" target="_blank" rel="noopener">Download entity table</a>
            <a href="/api/schema/download?session_id=${encodeURIComponent(state.sessionId)}&kind=relations" target="_blank" rel="noopener">Download relation table</a>
          </div>
          ${editable ? `
            <div class="onto-schema-actions">
              <button class="onto-btn secondary" id="schema-modal-apply" disabled>Apply changes</button>
              <button class="onto-btn primary" id="schema-modal-confirm">Confirm &amp; Continue</button>
            </div>
            <div class="onto-schema-errors" id="schema-modal-errors"></div>
          ` : ''}
        </section>
        <section class="schema-preview-card modal-code">
          <h4>Python View</h4>
          <pre class="onto-code pretty-code"><code class="language-python">${escapeHtml(sanitizeDisplayText(schema.schema_text))}</code></pre>
        </section>
      </div>
    `;
    if (window.Prism) window.Prism.highlightAllUnder(el.schemaModalBody);
    bindSchemaModalEditing();
  }

  function bindSchemaModalEditing() {
    if (!el.schemaModalBody) return;
    const apply = el.schemaModalBody.querySelector('#schema-modal-apply');
    const errorsBox = el.schemaModalBody.querySelector('#schema-modal-errors');
    el.schemaModalBody.querySelectorAll('.onto-cell-input').forEach((input) => {
      input.addEventListener('input', () => {
        const kind = input.getAttribute('data-kind');
        const index = Number(input.getAttribute('data-index'));
        const field = input.getAttribute('data-field');
        const items = state.schemaForm.filter((item) => item.type === kind);
        if (!items[index]) return;
        if (kind === 'entity' && field === 'name') {
          const oldName = items[index].name;
          items[index].name = input.value;
          state.schemaForm.forEach((item) => {
            if (item.type === 'relation') {
              if (item.head_entity === oldName) item.head_entity = input.value;
              if (item.tail_entity === oldName) item.tail_entity = input.value;
            }
          });
        } else {
          items[index][field] = input.value;
        }
        state.schemaDirty = true;
        if (apply) apply.disabled = false;
      });
    });
    if (apply) {
      apply.addEventListener('click', async () => {
        try {
          const data = await api('/api/schema/form', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ run_id: state.schema.run_id, form: state.schemaForm }),
          });
          if (!data.ok) {
            errorsBox.textContent = (data.errors || []).join('; ') || 'Changes failed validation';
            return;
          }
          state.schema = data;
          state.schemaForm = JSON.parse(JSON.stringify(data.form || []));
          state.schemaDirty = false;
          renderSchemaModal();
          renderSchemaTab();
          renderMessages();
        } catch (err) {
          errorsBox.textContent = `Apply failed: ${err.message}`;
        }
      });
    }
    const confirmBtn = el.schemaModalBody.querySelector('#schema-modal-confirm');
    if (confirmBtn) {
      confirmBtn.addEventListener('click', async () => {
        try {
          const data = await api('/api/schema/confirm', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ run_id: state.schema.run_id }),
          });
          if (!data.ok) {
            errorsBox.textContent = (data.errors || []).join('; ') || 'Confirmation failed';
            return;
          }
          state.schema = data;
          state.schemaForm = JSON.parse(JSON.stringify(data.form || []));
          closeSchemaModal();
          sendQuickReply('Confirm');
        } catch (err) {
          errorsBox.textContent = `Confirmation failed: ${err.message}`;
        }
      });
    }
  }

  async function openSchemaModal() {
    await refreshSchema();
    renderSchemaModal();
    el.schemaOverlay.hidden = false;
  }

  function closeSchemaModal() {
    if (el.schemaOverlay) el.schemaOverlay.hidden = true;
  }

  // ── Panel: progress tab ─────────────────────────────────────────────────

  async function renderProgressTab() {
    const stages = state.stages.length ? state.stages : [];
    const stageHtml = stages.length
      ? stages.map((stage) => `
          <div class="onto-stage-row ${escapeHtml(stage.status)}">
            <span class="onto-stage-dot"><i>${stageIcon(stage.status)}</i></span>
            <span class="onto-stage-label">${escapeHtml(stage.label)}</span>
            <span class="onto-stage-status">${{ pending: 'Pending', running: 'Running', waiting: 'Awaiting your confirmation', done: 'Done' }[stage.status] || ''}</span>
          </div>
        `).join('')
      : '<div class="onto-empty">No runs yet. Send a question to see pipeline progress here.</div>';
    el.progressContent.innerHTML = `
      <div class="onto-section">
        <div class="onto-section-head"><h3>Pipeline Progress</h3></div>
        <p class="onto-section-hint">This view only tracks the eight main ontology QA stages.</p>
        <div class="onto-stage-list">${stageHtml}</div>
      </div>
    `;
  }

  async function refreshSidebarData() {
    await refreshUploads();
    await refreshSchema();
    renderMessages();
    if (!el.panel.classList.contains('active')) return;
    if (state.panelTab === 'evidence') renderEvidenceTab();
    if (state.panelTab === 'schema') renderSchemaTab();
    if (state.panelTab === 'progress') renderProgressTab();
  }

  // ── Panel & FAB plumbing ────────────────────────────────────────────────

  function openPanel(tab) {
    state.panelTab = tab || state.panelTab;
    el.panel.classList.add('active');
    el.panelTabs.forEach((button) => {
      button.classList.toggle('active', button.dataset.tab === state.panelTab);
    });
    document.querySelectorAll('.panel-section').forEach((section) => {
      section.classList.toggle('active', section.id === `${state.panelTab}-content`);
    });
    closeFab();
    if (state.panelTab === 'evidence') refreshUploads().then(renderEvidenceTab);
    if (state.panelTab === 'schema') refreshSchema().then(renderSchemaTab);
    if (state.panelTab === 'progress') renderProgressTab();
  }

  function closeFab() {
    el.fabContainer.classList.remove('open');
    el.fabMain.setAttribute('aria-expanded', 'false');
    [el.fabEvidence, el.fabSchema, el.fabProgress].forEach((button) => {
      button.setAttribute('aria-hidden', 'true');
      button.tabIndex = -1;
    });
  }

  function toggleFab() {
    const open = !el.fabContainer.classList.contains('open');
    el.fabContainer.classList.toggle('open', open);
    el.fabMain.setAttribute('aria-expanded', String(open));
    [el.fabEvidence, el.fabSchema, el.fabProgress].forEach((button) => {
      button.setAttribute('aria-hidden', String(!open));
      button.tabIndex = open ? 0 : -1;
    });
  }

  // ── Session rail (left sidebar) ─────────────────────────────────────────

  async function renderSessionRail() {
    let sessions = [];
    try {
      const data = await api('/api/sessions');
      sessions = data.sessions || [];
    } catch (err) {
      sessions = [];
    }
    if (!el.sessionList) return;
    el.sessionList.innerHTML = sessions.length
      ? sessions.map((session) => `
          <div class="rail-item ${session.id === state.sessionId ? 'active' : ''}" data-session="${escapeHtml(session.id)}" title="${escapeHtml(session.title || 'New chat')}">
            <span class="rail-item-icon" aria-hidden="true">
              <svg viewBox="0 0 16 16" width="14" height="14"><path fill="currentColor" d="M8 1.5c3.6 0 6.5 2.46 6.5 5.5 0 3.04-2.9 5.5-6.5 5.5-.8 0-1.57-.12-2.27-.34l-3 1.5a.4.4 0 0 1-.57-.42l.5-2.66C1.6 10.06 1.5 8.8 1.5 7 1.5 3.96 4.4 1.5 8 1.5Z"/></svg>
            </span>
            <span class="rail-item-title">${escapeHtml(session.title || 'New chat')}</span>
            <button class="rail-item-delete" data-delete-session="${escapeHtml(session.id)}" title="Delete chat" aria-label="Delete chat">
              <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>
            </button>
          </div>
        `).join('')
      : '<div class="rail-empty">No chats yet. Start a new chat to begin.</div>';
    el.sessionList.querySelectorAll('[data-session]').forEach((item) => {
      item.addEventListener('click', (event) => {
        if (event.target.closest('[data-delete-session]')) return;
        const id = item.getAttribute('data-session');
        if (id === state.sessionId) return;
        startSession(id);
        if (window.matchMedia('(max-width: 900px)').matches) collapseRail(true);
      });
    });
    el.sessionList.querySelectorAll('[data-delete-session]').forEach((button) => {
      button.addEventListener('click', async (event) => {
        event.stopPropagation();
        const id = button.getAttribute('data-delete-session');
        const ok = await confirmDialog({
          title: 'Delete chat?',
          text: 'This conversation and its files, evidence and results will be permanently removed. This cannot be undone.',
          confirmLabel: 'Delete',
        });
        if (!ok) return;
        await api(`/api/sessions/${id}`, { method: 'DELETE' });
        if (id === state.sessionId) {
          let remaining = [];
          try {
            const data = await api('/api/sessions');
            remaining = (data.sessions || []).filter((s) => s.id !== id);
          } catch (err) { remaining = []; }
          await startSession(remaining.length ? remaining[0].id : '');
        } else {
          renderSessionRail();
        }
      });
    });
  }

  // ── Confirm dialog ──────────────────────────────────────────────────────

  let confirmResolver = null;

  function confirmDialog({ title, text, confirmLabel } = {}) {
    if (!el.confirmOverlay) {
      return Promise.resolve(window.confirm(text || 'Are you sure?'));
    }
    if (title) el.confirmTitle.textContent = title;
    if (text) el.confirmText.textContent = text;
    if (confirmLabel) el.confirmOk.textContent = confirmLabel;
    el.confirmOverlay.hidden = false;
    requestAnimationFrame(() => el.confirmOk.focus());
    return new Promise((resolve) => { confirmResolver = resolve; });
  }

  function closeConfirm(result) {
    if (!el.confirmOverlay) return;
    el.confirmOverlay.hidden = true;
    const resolve = confirmResolver;
    confirmResolver = null;
    if (resolve) resolve(result);
  }

  function collapseRail(collapsed) {
    el.body.classList.toggle('rail-collapsed', collapsed);
    localStorage.setItem('ontology-ui-rail', collapsed ? 'collapsed' : 'open');
  }

  function toggleRail() {
    collapseRail(!el.body.classList.contains('rail-collapsed'));
  }

  // ── Input plumbing ──────────────────────────────────────────────────────

  function autoSizeInput() {
    el.input.style.height = 'auto';
    el.input.style.height = `${Math.min(el.input.scrollHeight, 180)}px`;
  }

  function bindEvents() {
    el.send.addEventListener('click', sendMessage);
    el.input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !state.isComposing && !event.isComposing) {
        event.preventDefault();
        sendMessage();
      }
    });
    el.input.addEventListener('compositionstart', () => { state.isComposing = true; });
    el.input.addEventListener('compositionend', () => { state.isComposing = false; });
    el.input.addEventListener('input', autoSizeInput);

    el.newChat.addEventListener('click', () => startSession(''));
    if (el.railNewChat) el.railNewChat.addEventListener('click', () => startSession(''));
    el.brandHome.addEventListener('click', () => startSession(''));
    document.addEventListener('keydown', (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'n') {
        event.preventDefault();
        startSession('');
      }
    });

    el.themeToggle.addEventListener('click', () => {
      const dark = el.body.classList.toggle('dark-theme');
      el.body.classList.toggle('light-theme', !dark);
      localStorage.setItem('ontology-ui-theme', dark ? 'dark' : 'light');
    });
    const savedTheme = localStorage.getItem('ontology-ui-theme');
    if (savedTheme === 'dark') {
      el.body.classList.add('dark-theme');
      el.body.classList.remove('light-theme');
    }

    if (el.railToggle) el.railToggle.addEventListener('click', toggleRail);
    if (el.railCollapse) el.railCollapse.addEventListener('click', () => collapseRail(true));

    if (el.confirmCancel) el.confirmCancel.addEventListener('click', () => closeConfirm(false));
    if (el.confirmOk) el.confirmOk.addEventListener('click', () => closeConfirm(true));
    if (el.confirmOverlay) el.confirmOverlay.addEventListener('click', (event) => {
      if (event.target === el.confirmOverlay) closeConfirm(false);
    });
    if (el.clarifyModalClose) el.clarifyModalClose.addEventListener('click', closeClarificationModal);
    if (el.clarifyModalCancel) el.clarifyModalCancel.addEventListener('click', closeClarificationModal);
    if (el.clarifyModalSave) el.clarifyModalSave.addEventListener('click', saveClarificationModal);
    if (el.clarifyModalAddStep) {
      el.clarifyModalAddStep.addEventListener('click', () => {
        el.clarifyModalSteps.insertAdjacentHTML('beforeend', clarifyStepRowHtml(''));
        const row = el.clarifyModalSteps.lastElementChild;
        bindClarifyModalRemove(row);
        renumberClarifyModalSteps();
        row.querySelector('.clarify-step-input').focus();
      });
    }
    if (el.clarifyOverlay) {
      el.clarifyOverlay.addEventListener('click', (event) => {
        if (event.target === el.clarifyOverlay) closeClarificationModal();
      });
    }
    if (el.schemaModalClose) el.schemaModalClose.addEventListener('click', closeSchemaModal);
    if (el.schemaOverlay) {
      el.schemaOverlay.addEventListener('click', (event) => {
        if (event.target === el.schemaOverlay) closeSchemaModal();
      });
    }
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && el.confirmOverlay && !el.confirmOverlay.hidden) closeConfirm(false);
      if (event.key === 'Escape' && el.clarifyOverlay && !el.clarifyOverlay.hidden) closeClarificationModal();
      if (event.key === 'Escape' && el.schemaOverlay && !el.schemaOverlay.hidden) closeSchemaModal();
    });

    el.fabMain.addEventListener('click', toggleFab);
    el.fabEvidence.addEventListener('click', () => openPanel('evidence'));
    el.fabSchema.addEventListener('click', () => openPanel('schema'));
    el.fabProgress.addEventListener('click', () => openPanel('progress'));
    el.heroUpload.addEventListener('click', () => openPanel('evidence'));
    el.closePanel.addEventListener('click', () => el.panel.classList.remove('active'));
    el.panelTabs.forEach((button) => {
      button.addEventListener('click', () => openPanel(button.dataset.tab));
    });

    el.attachAdd.addEventListener('click', () => el.chatFileInput.click());
    el.chatFileInput.addEventListener('change', async () => {
      const files = Array.from(el.chatFileInput.files || []);
      el.chatFileInput.value = '';
      if (files.length) await uploadAndAttachFiles(files);
    });
    document.addEventListener('click', (event) => {
      if (!event.target.closest('#fab-container')) closeFab();
    });

    el.resetRun.addEventListener('click', () => setRunning(false));
  }

  async function init() {
    bindEvents();
    const savedRail = localStorage.getItem('ontology-ui-rail');
    const startCollapsed = savedRail === 'collapsed' || window.matchMedia('(max-width: 900px)').matches;
    el.body.classList.toggle('rail-collapsed', startCollapsed);
    await refreshHealth();
    await refreshUploads();
    await refreshSchema();
    const savedSession = localStorage.getItem('ontology-ui-session') || '';
    try {
      if (savedSession) await api(`/api/sessions/${savedSession}`);
      await startSession(savedSession);
    } catch (err) {
      await startSession('');
    }
    setInterval(refreshHealth, 30000);
    setInterval(tickElapsed, 1000);
  }

  init();
})();
