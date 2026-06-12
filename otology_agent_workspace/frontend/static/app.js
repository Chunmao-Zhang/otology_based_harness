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
    historyBtn: document.getElementById('history-btn'),
    historyModal: document.getElementById('history-modal'),
    historyOverlay: document.getElementById('history-overlay'),
    historyList: document.getElementById('session-list'),
    historyCount: document.getElementById('session-count'),
    historyNewChat: document.getElementById('history-new-chat'),
    closeHistory: document.getElementById('close-history'),
    runIndicator: document.getElementById('run-indicator'),
    runDetail: document.getElementById('run-detail'),
    resetRun: document.getElementById('reset-run'),
    stageStrip: document.getElementById('stage-strip'),
    uploadMetric: document.getElementById('upload-metric'),
    heroUpload: document.getElementById('hero-upload'),
    uploadTrigger: document.getElementById('upload-trigger'),
    uploadCurrent: document.getElementById('upload-current'),
    uploadMenu: document.getElementById('upload-menu'),
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
  };

  // ── Utilities ───────────────────────────────────────────────────────────

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function formatInline(text) {
    let safe = escapeHtml(text);
    safe = safe.replace(/`([^`]+)`/g, '<code>$1</code>');
    safe = safe.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    return safe;
  }

  function isTableRow(line) {
    const trimmed = String(line || '').trim();
    return trimmed.startsWith('|') && trimmed.endsWith('|');
  }

  function parseRow(line) {
    return String(line).trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
  }

  function formatMarkdown(value) {
    const lines = String(value || '').replace(/\r\n/g, '\n').split('\n');
    const blocks = [];
    let index = 0;
    while (index < lines.length) {
      const trimmed = lines[index].trim();
      if (!trimmed) { index += 1; continue; }
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
      const listType = /^[-*]\s+/.test(trimmed) ? 'ul' : (/^\d+\.\s+/.test(trimmed) ? 'ol' : '');
      if (listType) {
        const items = [];
        while (index < lines.length) {
          const item = lines[index].trim();
          const match = listType === 'ul' ? item.match(/^[-*]\s+(.+)$/) : item.match(/^\d+\.\s+(.+)$/);
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
        && !/^\d+\.\s+/.test(lines[index].trim())) {
        paragraph.push(lines[index]);
        index += 1;
      }
      blocks.push(`<p>${paragraph.map(formatInline).join('<br>')}</p>`);
    }
    return blocks.join('');
  }

  async function api(path, options) {
    const response = await fetch(path, options);
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
    if (!sessionId) {
      const data = await api('/api/sessions', { method: 'POST' });
      sessionId = data.session.id;
    }
    state.sessionId = sessionId;
    localStorage.setItem('ontology-ui-session', sessionId);
    state.messages = [];
    state.stages = [];
    connectWs();
    renderMessages();
    renderStageStrip();
  }

  function connectWs() {
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${protocol}://${window.location.host}/ws/${state.sessionId}`);
    state.ws = ws;
    ws.addEventListener('open', () => { state.wsReady = true; });
    ws.addEventListener('close', () => { state.wsReady = false; });
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
      if (payload.status === 'running' && payload.detail) {
        el.runDetail.textContent = payload.detail;
      }
      return;
    }
    if (payload.type === 'assistant_final') {
      state.messages.push(payload.message);
      if (payload.stages) state.stages = payload.stages;
      renderMessages();
      renderStageStrip();
      refreshSidebarData();
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

  function sendMessage() {
    const content = el.input.value.trim();
    if (!content || state.running || !state.wsReady) return;
    const uploadIds = Array.from(state.selectedUploads);
    state.ws.send(JSON.stringify({ type: 'chat', content, upload_ids: uploadIds }));
    el.input.value = '';
    autoSizeInput();
  }

  function sendQuickReply(text) {
    if (state.running || !state.wsReady) return;
    state.ws.send(JSON.stringify({ type: 'chat', content: text, upload_ids: [] }));
  }

  // ── Chat rendering ──────────────────────────────────────────────────────

  function gateActions(message) {
    if (state.running) return '';
    const last = state.messages[state.messages.length - 1];
    if (!last || last.id !== message.id) return '';
    const waiting = (state.stages || []).find((stage) => stage.status === 'waiting');
    if (!waiting) return '';
    const schemaGate = waiting.id === 'confirm_schema';
    return `
      <div class="gate-actions">
        <button class="gate-confirm" data-action="confirm">Confirm &amp; continue</button>
        ${schemaGate ? '<button class="gate-open-schema" data-action="open-schema">Open Schema Studio</button>' : ''}
      </div>
    `;
  }

  function renderMessages() {
    const visible = state.messages.filter((message) => ['user', 'assistant', 'system'].includes(message.role));
    el.hero.style.display = visible.length ? 'none' : '';
    el.messages.classList.toggle('active', visible.length > 0);
    el.messages.innerHTML = visible.map((message) => {
      if (message.role === 'user') {
        const uploads = (message.uploads || []).length
          ? `<div class="bubble-uploads">${message.uploads.map((name) => `<span class="bubble-upload-chip">📎 ${escapeHtml(name)}</span>`).join('')}</div>`
          : '';
        return `<article class="message user"><div class="bubble">${escapeHtml(message.content)}${uploads}</div></article>`;
      }
      if (message.role === 'assistant') {
        return `
          <article class="message assistant">
            <div class="avatar">O</div>
            <div class="bubble">${formatMarkdown(message.content)}${gateActions(message)}</div>
          </article>
        `;
      }
      return `<article class="message system"><div class="bubble">${escapeHtml(message.content || '')}</div></article>`;
    }).join('');
    el.messages.querySelectorAll('[data-action="confirm"]').forEach((button) => {
      button.addEventListener('click', () => sendQuickReply('Confirm'));
    });
    el.messages.querySelectorAll('[data-action="open-schema"]').forEach((button) => {
      button.addEventListener('click', () => openPanel('schema'));
    });
    if (window.Prism) window.Prism.highlightAllUnder(el.messages);
    el.messages.scrollTop = el.messages.scrollHeight;
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

  // ── Upload selector (input bar) ─────────────────────────────────────────

  function renderUploadSelector() {
    const count = state.selectedUploads.size;
    el.uploadCurrent.textContent = count ? `${count} file(s) selected` : 'No files attached';
    el.uploadMenu.innerHTML = state.uploads.length
      ? state.uploads.map((upload) => {
        const selected = state.selectedUploads.has(upload.id);
        return `
          <button class="kb-scope-menu-item" role="option" aria-selected="${selected}" data-upload="${escapeHtml(upload.id)}">
            <span class="kb-scope-menu-item-dot"></span>
            <span class="kb-scope-menu-item-name">${escapeHtml(upload.name)}</span>
            <span class="kb-scope-menu-item-meta">${selected ? 'Selected' : upload.type.toUpperCase()}</span>
          </button>
        `;
      }).join('')
      : '<div class="kb-scope-menu-empty">No files uploaded yet. Upload one in "Files &amp; Evidence".</div>';
    el.uploadMenu.querySelectorAll('[data-upload]').forEach((button) => {
      button.addEventListener('click', () => {
        const id = button.getAttribute('data-upload');
        if (state.selectedUploads.has(id)) state.selectedUploads.delete(id);
        else state.selectedUploads.add(id);
        renderUploadSelector();
      });
    });
  }

  function toggleUploadMenu(open) {
    const shouldOpen = open != null ? open : el.uploadMenu.hidden;
    el.uploadMenu.hidden = !shouldOpen;
    el.uploadMenu.dataset.open = shouldOpen ? 'true' : 'false';
    el.uploadTrigger.setAttribute('aria-expanded', String(shouldOpen));
  }

  // ── Panel: evidence tab ─────────────────────────────────────────────────

  async function refreshUploads() {
    try {
      const data = await api('/api/uploads');
      state.uploads = data.uploads || [];
      state.selectedUploads.forEach((id) => {
        if (!state.uploads.some((upload) => upload.id === id)) state.selectedUploads.delete(id);
      });
    } catch (err) {
      state.uploads = [];
    }
    renderUploadSelector();
    if (el.uploadMetric) el.uploadMetric.textContent = `${state.uploads.length} uploaded file(s)`;
  }

  async function renderEvidenceTab() {
    let evidence = { sources: [], needs_web_search: false };
    try { evidence = await api('/api/evidence'); } catch (err) { /* ignore */ }
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
    const sourcesHtml = evidence.sources && evidence.sources.length
      ? evidence.sources.map((source) => `
          <div class="onto-evidence-row">
            <span class="onto-evidence-kind ${escapeHtml(source.source_kind || '')}">${source.source_kind === 'web' ? 'Web' : 'Upload'}</span>
            <div class="onto-file-info">
              <strong>${source.url ? `<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener">${escapeHtml(source.source_id)}</a>` : escapeHtml(source.source_id)}</strong>
              <small>${escapeHtml(source.reason || '')}</small>
            </div>
          </div>
        `).join('')
      : '<div class="onto-empty">No evidence manifest yet. Ask a question and confirm it to see the evidence sources used.</div>';
    el.evidenceContent.innerHTML = `
      <div class="onto-section">
        <div class="onto-section-head">
          <h3>Uploaded Files</h3>
          <label class="onto-upload-btn">
            <input type="file" id="file-input" accept=".csv,.txt,.md" hidden>
            <span>+ Upload file</span>
          </label>
        </div>
        <p class="onto-section-hint">Supports CSV / TXT / MD. After uploading, select files to attach from the left of the input box.</p>
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
        await api(`/api/uploads/${encodeURIComponent(button.getAttribute('data-delete'))}`, { method: 'DELETE' });
        await refreshUploads();
        renderEvidenceTab();
      });
    });
  }

  // ── Panel: schema tab ───────────────────────────────────────────────────

  async function refreshSchema() {
    try {
      state.schema = await api('/api/schema');
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
    const entityRows = entities.map((item, index) => `
      <tr>
        <td><input class="onto-cell-input" data-kind="entity" data-index="${index}" data-field="name" value="${escapeHtml(item.name)}"></td>
        <td><input class="onto-cell-input" data-kind="entity" data-index="${index}" data-field="entity_type" value="${escapeHtml(item.entity_type || '')}"></td>
      </tr>
    `).join('');
    const relationRows = relations.map((item, index) => `
      <tr>
        <td>${escapeHtml(item.head_entity)}</td>
        <td><input class="onto-cell-input" data-kind="relation" data-index="${index}" data-field="relation" value="${escapeHtml(item.relation)}"></td>
        <td>${escapeHtml(item.tail_entity)}</td>
        <td><span class="onto-rel-type">${escapeHtml(item.relation_type || '')}</span></td>
      </tr>
    `).join('');
    const editable = schema.status === 'draft';
    el.schemaContent.innerHTML = `
      <div class="onto-section">
        <div class="onto-section-head"><h3>Ontology Schema</h3>${schemaStatusBadge(schema.status)}</div>
        <p class="onto-section-hint">${editable ? 'Edit entity and relation names directly, apply changes, then confirm.' : 'Schema confirmed and in use for data extraction and solving.'}</p>
        <h4 class="onto-subhead">Entities</h4>
        <div class="md-table-wrap"><table class="md-table onto-schema-table">
          <thead><tr><th>Entity</th><th>Semantic Type</th></tr></thead>
          <tbody>${entityRows || '<tr><td colspan="2">None</td></tr>'}</tbody>
        </table></div>
        <h4 class="onto-subhead">Relations</h4>
        <div class="md-table-wrap"><table class="md-table onto-schema-table">
          <thead><tr><th>Head</th><th>Relation</th><th>Tail</th><th>Type</th></tr></thead>
          <tbody>${relationRows || '<tr><td colspan="4">None</td></tr>'}</tbody>
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
        <pre class="onto-code"><code class="language-python">${escapeHtml(schema.schema_text)}</code></pre>
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
    let results = { report: {}, answer_sources: [] };
    try { results = await api('/api/results'); } catch (err) { /* ignore */ }
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
    const report = results.report || {};
    const hasReport = report.total_instances != null;
    const reportHtml = hasReport
      ? `
        <div class="onto-stats-grid">
          <div class="onto-stat"><strong>${report.total_instances}</strong><span>Instances</span></div>
          <div class="onto-stat"><strong>${report.total_facts}</strong><span>Facts</span></div>
          <div class="onto-stat"><strong>${report.total_relations}</strong><span>Relations</span></div>
          <div class="onto-stat"><strong>${report.avg_confidence != null ? Number(report.avg_confidence).toFixed(2) : '-'}</strong><span>Avg confidence</span></div>
        </div>
        ${(report.relation_types_used || []).length ? `<p class="onto-section-hint">Relations used: ${report.relation_types_used.map(escapeHtml).join(', ')}</p>` : ''}
      `
      : '<div class="onto-empty">After data extraction, a summary of instances, facts and relations will appear here.</div>';
    const sourcesHtml = (results.answer_sources || []).length
      ? `<ul class="onto-source-list">${results.answer_sources.map((source) => `<li>${escapeHtml(source)}</li>`).join('')}</ul>`
      : '';
    el.progressContent.innerHTML = `
      <div class="onto-section">
        <div class="onto-section-head"><h3>Pipeline Progress</h3></div>
        <div class="onto-stage-list">${stageHtml}</div>
      </div>
      <div class="onto-section">
        <div class="onto-section-head"><h3>Extraction Summary</h3></div>
        ${reportHtml}
        ${sourcesHtml}
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

  // ── History modal ───────────────────────────────────────────────────────

  async function openHistory() {
    const data = await api('/api/sessions');
    const sessions = data.sessions || [];
    el.historyCount.textContent = `${sessions.length} record(s)`;
    el.historyList.innerHTML = sessions.length
      ? sessions.map((session) => `
          <div class="history-item" data-session="${escapeHtml(session.id)}">
            <div class="history-item-main">
              <strong>${escapeHtml(session.title || 'New chat')}</strong>
              <small>${escapeHtml(session.updated_at || '')} · ${session.message_count} message(s)</small>
            </div>
            <button class="history-delete" data-delete-session="${escapeHtml(session.id)}" title="Delete session">×</button>
          </div>
        `).join('')
      : '<div class="onto-empty">No previous sessions yet.</div>';
    el.historyList.querySelectorAll('[data-session]').forEach((item) => {
      item.addEventListener('click', (event) => {
        if (event.target.closest('[data-delete-session]')) return;
        closeHistory();
        startSession(item.getAttribute('data-session'));
      });
    });
    el.historyList.querySelectorAll('[data-delete-session]').forEach((button) => {
      button.addEventListener('click', async (event) => {
        event.stopPropagation();
        await api(`/api/sessions/${button.getAttribute('data-delete-session')}`, { method: 'DELETE' });
        openHistory();
      });
    });
    el.historyModal.classList.add('active');
    el.historyOverlay.classList.add('active');
  }

  function closeHistory() {
    el.historyModal.classList.remove('active');
    el.historyOverlay.classList.remove('active');
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
    if (el.historyNewChat) el.historyNewChat.addEventListener('click', () => { closeHistory(); startSession(''); });
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

    el.historyBtn.addEventListener('click', openHistory);
    el.closeHistory.addEventListener('click', closeHistory);
    el.historyOverlay.addEventListener('click', closeHistory);

    el.fabMain.addEventListener('click', toggleFab);
    el.fabEvidence.addEventListener('click', () => openPanel('evidence'));
    el.fabSchema.addEventListener('click', () => openPanel('schema'));
    el.fabProgress.addEventListener('click', () => openPanel('progress'));
    el.heroUpload.addEventListener('click', () => openPanel('evidence'));
    el.closePanel.addEventListener('click', () => el.panel.classList.remove('active'));
    el.panelTabs.forEach((button) => {
      button.addEventListener('click', () => openPanel(button.dataset.tab));
    });

    el.uploadTrigger.addEventListener('click', () => toggleUploadMenu());
    document.addEventListener('click', (event) => {
      if (!event.target.closest('#upload-segment')) toggleUploadMenu(false);
      if (!event.target.closest('#fab-container')) closeFab();
    });

    el.resetRun.addEventListener('click', () => setRunning(false));
  }

  async function init() {
    bindEvents();
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
