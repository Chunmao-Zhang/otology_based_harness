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
  };

  // ── Utilities ───────────────────────────────────────────────────────────

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function sanitizeDisplayText(value) {
    return String(value == null ? '' : value)
      .replace(/(^|[\s`"'(])\/(?:home|Users|tmp|var|mnt|opt|workspace|runs)\b[^\s`"'<>|)]*/g, '$1[hidden path]')
      .replace(/(^|[\s`"'(])runs\/ontology_workspace_runs\/[^\s`"'<>|)]*/g, '$1[hidden path]')
      .replace(/\b[A-Za-z]:\\[^\s`"'<>|)]+/g, '[hidden path]');
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

  function scrollToLatestMessage() {
    window.requestAnimationFrame(() => {
      const last = el.messages.lastElementChild;
      if (last) {
        last.scrollIntoView({ block: 'end', behavior: 'smooth' });
      } else {
        window.scrollTo({ top: document.documentElement.scrollHeight, behavior: 'smooth' });
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
    ws.addEventListener('open', () => { state.wsReady = true; });
    ws.addEventListener('close', () => { state.wsReady = false; });
    ws.addEventListener('error', () => { state.wsReady = false; });
    ws.addEventListener('message', (event) => {
      let payload = null;
      try { payload = JSON.parse(event.data); } catch (err) { return; }
      handleWsEvent(payload);
    });
  }

  function handleWsEvent(payload) {
    if (payload.type === 'history') {
      const session = payload.session || {};
      state.sessionId = session.id || state.sessionId;
      state.messages = session.messages || [];
      state.stages = session.stages || [];
      renderMessages();
      renderStageStrip();
      return;
    }
    if (payload.type === 'message') {
      state.messages.push(payload.message);
      renderMessages();
      renderSessionRail();
      return;
    }
    if (payload.type === 'run_start') {
      setRunning(true, 'The agent is working on your request…');
      return;
    }
    if (payload.type === 'stage') {
      if (payload.stages) state.stages = payload.stages;
      renderStageStrip();
      renderProgressTab();
      renderMessages();
      if (payload.status === 'running' && payload.detail) {
        el.runDetail.textContent = payload.detail;
      }
      return;
    }
    if (payload.type === 'activity') {
      state.messages.push(payload.message);
      renderMessages();
      return;
    }
    if (payload.type === 'assistant_final') {
      state.messages.push(payload.message);
      if (payload.stages) state.stages = payload.stages;
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
      setRunning(false);
      renderMessages();
      renderProgressTab();
    }
  }

  function setRunning(running, detail) {
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
    renderMessages();
    renderSessionRail();
    return content;
  }

  async function sendViaHttp(payload) {
    const optimisticContent = pushOptimisticUserMessage(payload);
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
    if (state.wsReady) {
      state.ws.send(JSON.stringify(payload));
    } else {
      sendViaHttp(payload);
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

  function renderAssistantContent(message) {
    if (message.clarification) {
      const actions = gateActions(message);
      return renderClarificationCard(message.clarification, actions);
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
      tools: [],
    };
  }

  function buildStageCards(events) {
    const cards = new Map();
    let activeStageId = inferActiveStageId();
    (events || []).forEach((message) => {
      if (!message || message.kind === 'run_start') return;
      if (message.kind === 'stage' && message.stage) {
        activeStageId = message.stage;
        if (!cards.has(activeStageId)) cards.set(activeStageId, makeStageCard(activeStageId, message.title));
        const card = cards.get(activeStageId);
        card.title = message.title || card.title;
        card.status = (stageById(activeStageId) || {}).status || message.status || card.status;
        card.thinking = normalizeActivityText(message.content || card.thinking);
        return;
      }
      if (message.kind === 'tool') {
        const stageId = activeStageId || inferActiveStageId();
        if (!stageId) return;
        if (!cards.has(stageId)) cards.set(stageId, makeStageCard(stageId, stageById(stageId)?.label));
        const card = cards.get(stageId);
        const content = normalizeActivityText(message.content || 'Running an internal tool.');
        if (content && !card.tools.includes(content)) card.tools.push(content);
      }
    });
    if (!cards.size) {
      (state.stages || []).filter((stage) => stage.status !== 'pending').forEach((stage) => {
        cards.set(stage.id, makeStageCard(stage.id, stage.label));
      });
    }
    return (state.stages || [])
      .filter((stage) => cards.has(stage.id))
      .map((stage) => {
        const card = cards.get(stage.id);
        card.status = stage.status || card.status;
        card.title = stage.label || card.title;
        return card;
      });
  }

  function renderStageCard(card) {
    const isDone = card.status === 'done';
    const expanded = !isDone || state.expandedStageCards.has(card.id);
    const statusLabel = isDone && !expanded ? '' : stageStatusText(card.status);
    const toolsHtml = card.tools.length
      ? `
        <div class="stage-card-section">
          <span>Tool activity</span>
          <ul>${card.tools.map((tool) => `<li>${formatInline(tool)}</li>`).join('')}</ul>
        </div>
      `
      : '';
    const thinkingHtml = card.thinking && expanded
      ? `
        <div class="stage-card-section">
          <span>Current model progress</span>
          <p>${formatInline(card.thinking)}</p>
        </div>
      `
      : '';
    return `
      <section class="stage-card ${escapeHtml(card.status)} ${expanded ? 'expanded' : 'collapsed'}">
        <button class="stage-card-head" type="button" data-stage-card="${escapeHtml(card.id)}" aria-expanded="${expanded ? 'true' : 'false'}">
          <span class="stage-card-icon">${stageCardIcon(card.status)}</span>
          <strong>${escapeHtml(card.title)}</strong>
          ${statusLabel ? `<small>${statusLabel}</small>` : ''}
        </button>
        ${expanded ? `<div class="stage-card-body">${thinkingHtml}${toolsHtml || '<div class="stage-card-section muted">No tool activity to show for this step yet.</div>'}</div>` : ''}
      </section>
    `;
  }

  function renderStagePipeline(cards) {
    if (!cards.length) return '';
    return `
      <article class="message event stage-pipeline-message">
        <div class="avatar activity-avatar">O</div>
        <div class="stage-card-list">
          ${cards.map(renderStageCard).join('')}
        </div>
      </article>
    `;
  }

  function buildMessageTimeline(messages) {
    const items = [];
    let eventBuffer = [];
    const flushEvents = () => {
      if (!eventBuffer.length) return;
      const cards = buildStageCards(eventBuffer);
      if (cards.length) items.push({ role: 'pipeline', cards });
      eventBuffer = [];
    };
    messages.forEach((message) => {
      if (message.role === 'event') {
        eventBuffer.push(message);
        return;
      }
      flushEvents();
      items.push(message);
    });
    flushEvents();
    if (!items.some((item) => item.role === 'pipeline') && (state.stages || []).some((stage) => stage.status !== 'pending')) {
      items.push({ role: 'pipeline', cards: buildStageCards([]) });
    }
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
    if (state.wsReady) {
      state.ws.send(JSON.stringify(payload));
    } else {
      sendViaHttp(payload);
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

  function renderMessages() {
    const visible = state.messages.filter((message) => ['user', 'assistant', 'system', 'event'].includes(message.role));
    el.hero.style.display = visible.length ? 'none' : '';
    el.messages.classList.toggle('active', visible.length > 0);
    el.messages.innerHTML = buildMessageTimeline(visible).map((message) => {
      if (message.role === 'pipeline') {
        return renderStagePipeline(message.cards || []);
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
            <div class="avatar">O</div>
            <div class="bubble">${renderAssistantContent(message)}</div>
          </article>
        `;
      }
      return `<article class="message system"><div class="bubble">${escapeHtml(sanitizeDisplayText(message.content || ''))}</div></article>`;
    }).join('');
    el.messages.querySelectorAll('[data-stage-card]').forEach((button) => {
      button.addEventListener('click', () => {
        const stageId = button.getAttribute('data-stage-card');
        if (!stageId) return;
        if (state.expandedStageCards.has(stageId)) state.expandedStageCards.delete(stageId);
        else state.expandedStageCards.add(stageId);
        renderMessages();
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
      button.addEventListener('click', () => openPanel('schema'));
    });
    if (window.Prism) window.Prism.highlightAllUnder(el.messages);
    el.messages.scrollTop = el.messages.scrollHeight;
    scrollToLatestMessage();
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
    const entityRows = entities.map((item, index) => `
      <tr>
        <td><input class="onto-cell-input" data-kind="entity" data-index="${index}" data-field="name" value="${escapeHtml(item.name)}"></td>
        <td><input class="onto-cell-input" data-kind="entity" data-index="${index}" data-field="entity_type" value="${escapeHtml(item.entity_type || '')}"></td>
        <td>${escapeHtml(item.value_type || 'str')}</td>
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
          <td><input class="onto-cell-input" data-kind="relation" data-index="${index}" data-field="relation" value="${escapeHtml(item.relation)}"></td>
          <td>${escapeHtml(item.tail_entity)}</td>
          <td>${escapeHtml(tail.entity_type || '')}</td>
          <td>${escapeHtml(tail.value_type || 'str')}</td>
        </tr>
      `;
    }).join('');
    const editable = schema.status === 'draft';
    el.schemaContent.innerHTML = `
      <div class="onto-section">
        <div class="onto-section-head"><h3>Ontology Schema</h3>${schemaStatusBadge(schema.status)}</div>
        <p class="onto-section-hint">${editable ? 'Edit entity and relation names directly, apply changes, then confirm.' : 'Schema confirmed and in use for data extraction and solving.'}</p>
        <h4 class="onto-subhead">Entity Definitions</h4>
        <div class="md-table-wrap"><table class="md-table onto-schema-table">
          <thead><tr><th>Entity</th><th>Entity Type</th><th>Data Type</th></tr></thead>
          <tbody>${entityRows || '<tr><td colspan="3">None</td></tr>'}</tbody>
        </table></div>
        <h4 class="onto-subhead">Schema Table</h4>
        <div class="md-table-wrap"><table class="md-table onto-schema-table">
          <thead><tr><th>Head Entity</th><th>Head Entity Type</th><th>Head Entity Data Type</th><th>Relation Name</th><th>Tail Entity</th><th>Tail Entity Type</th><th>Tail Entity Data Type</th></tr></thead>
          <tbody>${relationRows || '<tr><td colspan="7">None</td></tr>'}</tbody>
        </table></div>
        ${editable ? `
          <div class="onto-schema-actions">
            <button class="onto-btn secondary" id="schema-apply" ${state.schemaDirty ? '' : 'disabled'}>Apply changes</button>
            <button class="onto-btn primary" id="schema-confirm">Confirm Schema</button>
          </div>
          <div class="onto-schema-errors" id="schema-errors"></div>
        ` : ''}
      </div>
      <div class="onto-section">
        <div class="onto-section-head"><h3>Python View</h3></div>
        <pre class="onto-code"><code class="language-python">${escapeHtml(sanitizeDisplayText(schema.schema_text))}</code></pre>
      </div>
    `;
    if (window.Prism) window.Prism.highlightAllUnder(el.schemaContent);
    el.schemaContent.querySelectorAll('.onto-cell-input').forEach((input) => {
      input.addEventListener('input', () => {
        const kind = input.getAttribute('data-kind');
        const index = Number(input.getAttribute('data-index'));
        const field = input.getAttribute('data-field');
        const items = state.schemaForm.filter((item) => item.type === kind);
        if (items[index]) {
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
          const apply = el.schemaContent.querySelector('#schema-apply');
          if (apply) apply.disabled = false;
        }
      });
    });
    const applyBtn = el.schemaContent.querySelector('#schema-apply');
    if (applyBtn) {
      applyBtn.addEventListener('click', async () => {
        const errorsBox = el.schemaContent.querySelector('#schema-errors');
        try {
          const data = await api('/api/schema/form', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ run_id: schema.run_id, form: state.schemaForm }),
          });
          if (!data.ok) {
            errorsBox.textContent = (data.errors || []).join('; ') || 'Changes failed validation';
            return;
          }
          state.schema = data;
          state.schemaForm = JSON.parse(JSON.stringify(data.form || []));
          state.schemaDirty = false;
          renderSchemaTab();
        } catch (err) {
          errorsBox.textContent = `Apply failed: ${err.message}`;
        }
      });
    }
    const confirmBtn = el.schemaContent.querySelector('#schema-confirm');
    if (confirmBtn) {
      confirmBtn.addEventListener('click', async () => {
        const errorsBox = el.schemaContent.querySelector('#schema-errors');
        try {
          const data = await api('/api/schema/confirm', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ run_id: schema.run_id }),
          });
          if (!data.ok) {
            errorsBox.textContent = (data.errors || []).join('; ') || 'Confirmation failed';
            return;
          }
          state.schema = data;
          state.schemaForm = JSON.parse(JSON.stringify(data.form || []));
          renderSchemaTab();
          sendQuickReply('I have confirmed the schema in Schema Studio. Please continue.');
        } catch (err) {
          errorsBox.textContent = `Confirmation failed: ${err.message}`;
        }
      });
    }
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
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && el.confirmOverlay && !el.confirmOverlay.hidden) closeConfirm(false);
      if (event.key === 'Escape' && el.clarifyOverlay && !el.clarifyOverlay.hidden) closeClarificationModal();
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
  }

  init();
})();
