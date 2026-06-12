(() => {
  const config = window.STUDIO_CONFIG || {};
  const state = {
    sessions: [],
    session: null,
    ws: null,
    streaming: false,
    health: null,
    activePanel: 'schema',
    activeStreamId: null,
    reconnectTimer: null,
    importingGraph: false,
    importResult: null,
    importError: '',
    selectedGraphFileName: '',
    selectedGraphId: localStorage.getItem('kbqa_graph_scope_id') || '',
    graphSummary: null,
    summaryLoading: false,
    summaryError: '',
    deletingGraphId: '',
    deletingSessionId: '',
    confirmingDeleteSessionId: '',
    creatingSession: false,
    isComposing: false,
    compositionEndedAt: 0,
    suppressNextEnter: false,
    compositionGuardTimer: null,
    liveToolKey: '',
    activeModelDeltaKey: '',
    suppressModelOutputSync: false,
    resetModelOutputOnNextDelta: false,
    activeReasoning: '',
    activeReasoningKey: '',
    resetReasoningOnNextDelta: false,
    activeExampleFormat: localStorage.getItem('kbqa_example_format') || 'txt',
    previewViewMode: localStorage.getItem('kbqa_preview_view_mode') || 'visual',
    graphPollingTimer: null,
    pollingGraphIds: new Set(),
    graphProgress: {},
    warmPollingTimer: null,
    warmPollingGraphId: '',
    warmPollingToken: 0,
    warmTrickleTimer: null,
  };

  const $ = (selector) => document.querySelector(selector);
  const els = {
    brandHome: $('#brand-home'),
    status: $('#status-pill'),
    model: $('#model-chip'),
    historyBtn: $('#history-btn'),
    themeToggle: $('#theme-toggle'),
    newChat: $('#new-chat'),
    hero: $('#hero'),
    heroUpload: $('#hero-upload-graph'),
    capabilitySection: $('#capability-section'),
    exampleSection: $('#example-section'),
    promptGrid: $('#prompt-grid'),
    messages: $('#messages'),
    input: $('#message-input'),
    send: $('#send-button'),
    inputSection: $('#input-section'),
    runIndicator: $('#run-indicator'),
    runDetail: $('#run-detail'),
    resetRun: $('#reset-run'),
    warmProgress: $('#scope-warm-progress'),
    warmProgressLabel: $('#scope-warm-progress-label'),
    warmProgressPercent: $('#scope-warm-progress-percent'),
    warmProgressTrack: $('#scope-warm-progress-track'),
    warmProgressFill: $('#scope-warm-progress-fill'),
    warmProgressSub: $('#scope-warm-progress-sub'),
    graphScopeSelect: $('#graph-scope-select'),
    graphScopeHint: $('#graph-scope-hint'),
    kbScopeTrigger: $('#kb-scope-trigger'),
    kbScopeCurrent: $('#kb-scope-current'),
    kbScopeMenu: $('#kb-scope-menu'),
    fabContainer: $('#fab-container'),
    fabMain: $('#fab-main'),
    fabSchema: $('#fab-schema'),
    fabImport: $('#fab-import'),
    panel: $('#activities-panel'),
    closePanel: $('#close-panel'),
    tabs: document.querySelectorAll('.panel-tab'),
    schemaContent: $('#schema-content'),
    importContent: $('#import-content'),
    skillCount: $('#skill-count'),
    toolCount: $('#tool-count'),
    historyOverlay: $('#history-overlay'),
    historyModal: $('#history-modal'),
    closeHistory: $('#close-history'),
    historyNewChat: $('#history-new-chat'),
    sessionCount: $('#session-count'),
    sessions: $('#session-list'),
    scrollTop: null,
  };

  document.addEventListener('DOMContentLoaded', init);

  async function init() {
    if ('scrollRestoration' in window.history) {
      window.history.scrollRestoration = 'manual';
    }
    applySavedTheme();
    bindEvents();
    // Fire health + sessions in parallel; await the (fast) graph catalog so
    // both the landing page and the chat view can render the KB selector with
    // real options on the first paint.
    void loadHealth();
    await loadGraphCatalog();
    await loadSessions();
    // Kick off a background prewarm of the (auto-picked or restored) KB scope
    // so the first user question is fast.
    prewarmSelectedGraph();
    showLandingPage({ scrollTop: true });
    document.body.style.opacity = '1';
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    if (!response.ok) {
      throw new Error(await response.text());
    }
    return response.json();
  }

  async function loadHealth() {
    try {
      // /api/health does not include the graph catalog — preserve the one
      // loadGraphCatalog() put on state so a run_done event doesn't wipe the
      // KB selector and reset the user's selected knowledge base.
      const previousCatalog = (state.health && state.health.graph_catalog) || null;
      state.health = await api('/api/health');
      if (previousCatalog && Array.isArray(previousCatalog)) {
        state.health.graph_catalog = previousCatalog;
        state.health.custom_graph_imports = previousCatalog;
      }
      normalizeSelectedGraph();
      const model = state.health.model || 'Model';
      els.model.textContent = shortenModel(model);
      els.status.classList.toggle('ready', Boolean(state.health.api_key_present));
      els.status.classList.toggle('warn', !state.health.api_key_present);
      els.status.querySelector('span:last-child').textContent = state.health.api_key_present
        ? 'Runtime ready'
        : 'Missing API key';
      renderHeroMetrics();
    } catch (error) {
      state.health = null;
      els.status.classList.add('warn');
      els.status.querySelector('span:last-child').textContent = 'Runtime unavailable';
    }
    await loadGraphSummary(state.selectedGraphId || '', { silent: true });
    renderSchemaPanel();
    renderImportPanel();
    renderGraphScopeSelector();
    // Resume polling for any graphs still in "processing" state (e.g. after page refresh)
    for (const graph of graphCatalog()) {
      if (graph.status === 'processing' && graph.id) {
        startGraphPolling(graph.id);
      }
    }
  }

  // Lightweight graph catalog loader — fetches the imports list independently from
  // /api/health so the landing page renders fast and the KB selector populates as
  // soon as this returns (typically < 100 ms for the lightweight endpoint).
  async function loadGraphCatalog() {
    if (state._graphCatalogPromise) return state._graphCatalogPromise;
    state._graphCatalogPromise = (async () => {
      try {
        const data = await api('/api/graphs/imports?light=1');
        const imports = Array.isArray(data && data.imports) ? data.imports : [];
        if (!state.health) state.health = {};
        state.health.graph_catalog = imports;
        state.health.custom_graph_imports = imports;
        normalizeSelectedGraph();
        renderImportPanel();
        renderGraphScopeSelector();
        // Resume polling for any still-processing graphs.
        for (const graph of graphCatalog()) {
          if (graph.status === 'processing' && graph.id) {
            startGraphPolling(graph.id);
          }
        }
      } catch (error) {
        // Silent — health endpoint will surface the error if it persists.
      } finally {
        state._graphCatalogPromise = null;
      }
    })();
    return state._graphCatalogPromise;
  }

  async function loadGraphSummary(graphId = state.selectedGraphId || '', options = {}) {
    state.summaryLoading = true;
    state.summaryError = '';
    if (!options.silent) renderSchemaPanel();
    try {
      const query = graphId ? `?graph_id=${encodeURIComponent(graphId)}` : '';
      state.graphSummary = await api(`/api/graphs/summary${query}`);
    } catch (error) {
      state.graphSummary = null;
      state.summaryError = String(error.message || error);
    } finally {
      state.summaryLoading = false;
      if (!options.silent) renderSchemaPanel();
    }
  }

  async function loadSessions() {
    const data = await api('/api/sessions');
    state.sessions = data.sessions || [];
    renderSessionList();
  }

  function graphCatalog() {
    const health = state.health || {};
    if (Array.isArray(health.graph_catalog)) return health.graph_catalog;
    if (Array.isArray(health.custom_graph_imports)) return health.custom_graph_imports;
    return [];
  }

  function renderHeroMetrics() {
    const project = (state.health && state.health.project) || {};
    const skillCount = (project.skills || []).length || 3;
    const toolCount = (project.tools || []).length || 6;
    if (els.skillCount) els.skillCount.textContent = `${skillCount} reasoning skills`;
    if (els.toolCount) els.toolCount.textContent = `${toolCount} graph tools`;
  }

  function selectedGraph() {
    return graphCatalog().find((item) => item.id === state.selectedGraphId) || null;
  }

  function normalizeSelectedGraph() {
    if (!state.selectedGraphId) {
      // No KB selected yet — auto-pick the first ready graph so the user has
      // a sensible default scope to chat against instead of seeing
      // "Select a knowledge base" on first load.
      const graphs = graphCatalog();
      const firstReady = graphs.find((item) => item && item.id && item.status === 'ready');
      if (firstReady) {
        state.selectedGraphId = firstReady.id;
        localStorage.setItem('kbqa_graph_scope_id', firstReady.id);
      }
      return;
    }
    if (!selectedGraph()) {
      state.selectedGraphId = '';
      localStorage.setItem('kbqa_graph_scope_id', '');
    }
  }

  // Kick off a background build of the scope runtime for the given graph so
  // the user's first chat question on that scope doesn't pay the cost of
  // building the vector index / SQLite / ChromaDB from scratch. While the
  // build is in flight, a progress bar is shown above the chat input and the
  // input itself is dimmed/disabled — the user can still switch KBs.
  // Skips graphs that are still processing — the backend is already
  // finalising them via background_finalize.
  function prewarmSelectedGraph(graphId = state.selectedGraphId) {
    if (!graphId) return Promise.resolve();
    const graph = graphCatalog().find((item) => item && item.id === graphId);
    if (!graph || graph.status !== 'ready') return Promise.resolve();
    // Cancel any in-flight poll for a different graph (or the same one being
    // re-warmed) before kicking off the new request — the token check below
    // makes stale polls ignore their results.
    const token = ++state.warmPollingToken;
    state.warmPollingGraphId = graphId;
    showWarmProgress({ state: 'warming', graph_id: graphId, progress: 0,
                       message: 'Preparing knowledge base…', show_progress: true,
                       stage: 'queued' });
    return api(`/api/graphs/imports/${encodeURIComponent(graphId)}/warm`, {
      method: 'POST',
    })
      .then(() => {
        // If the user has since switched KBs, drop this result on the floor.
        if (token !== state.warmPollingToken) return;
        // Always poll — the status endpoint will return "ready" immediately
        // if the scope was already on disk.
        startWarmProgressPolling(graphId, token);
      })
      .catch((error) => {
        if (token !== state.warmPollingToken) return;
        // Silent — the chat will warm the scope synchronously on first use
        // and surface any real error there. Hide the bar so we're not stuck.
        hideWarmProgress({ animate: true });
      });
  }

  function startWarmProgressPolling(graphId, token) {
    if (state.warmPollingTimer) {
      window.clearInterval(state.warmPollingTimer);
      state.warmPollingTimer = null;
    }
    const tick = () => {
      // Stale poll (user switched KB) — drop.
      if (token !== state.warmPollingToken) return;
      api(`/api/graphs/imports/${encodeURIComponent(graphId)}/warm/status`)
        .then((status) => {
          if (token !== state.warmPollingToken) return;
          if (!status || !status.state) return;
          showWarmProgress({ ...status, graph_id: graphId });
          if (status.state === 'ready') {
            stopWarmProgressPolling();
            scheduleWarmProgressHide(graphId);
          } else if (status.state === 'error') {
            stopWarmProgressPolling();
            scheduleWarmProgressHide(graphId, { delay: 2400, keepError: true });
          }
        })
        .catch(() => {
          // Network blip — keep polling, the next tick will retry.
        });
    };
    // Immediate first tick so the bar reflects reality, then every 500ms.
    tick();
    state.warmPollingTimer = window.setInterval(tick, 500);
  }

  function stopWarmProgressPolling() {
    if (state.warmPollingTimer) {
      window.clearInterval(state.warmPollingTimer);
      state.warmPollingTimer = null;
    }
    stopWarmTrickle();
  }

  // Smoothly grow the bar between backend progress updates so the user
  // always sees motion. The trickle is capped by stage so we never run
  // past the next stage boundary. For stages that already have sub-progress
  // (preparing_kg, writing_artifacts, building_vector_index) we run a tiny
  // trickle — the real updates arrive every ~500ms so the override is the
  // dominant signal, the trickle just fills the gap.
  function updateWarmTrickle(percent, stateName, stage) {
    if (state.warmTrickleTimer) {
      window.clearInterval(state.warmTrickleTimer);
      state.warmTrickleTimer = null;
    }
    if (stateName === 'ready' || stateName === 'error') return;
    let cap = 99;
    let increment = 0.4; // % per tick
    switch (stage) {
      case 'queued':                 cap = Math.max(percent + 1, 4);  increment = 0.2; break;
      case 'preparing_kg':
      case 'parsing_records':
      case 'indexing_triples':       cap = 25; increment = 0.4; break;
      case 'writing_artifacts':      cap = 45; increment = 0.5; break;
      case 'loading_model':          cap = 50; increment = 0.3; break;
      case 'building_vector_index':
      case 'encoding_entities':
      case 'encoding_relations':     cap = 95; increment = 0.4; break;
      case 'activating_scope':       cap = 99; increment = 0.2; break;
      default:                       cap = 99; increment = 0.4;
    }
    const tick = () => {
      if (!els.warmProgressFill) return;
      const current = parseFloat(els.warmProgressFill.style.width) || percent;
      if (current < cap) {
        const next = Math.min(cap, current + increment);
        els.warmProgressFill.style.width = `${next}%`;
        if (els.warmProgressTrack) {
          els.warmProgressTrack.setAttribute('aria-valuenow', String(Math.round(next)));
        }
        if (els.warmProgressPercent && !els.warmProgress.classList.contains('is-done')
            && !els.warmProgress.classList.contains('is-error')) {
          // Only show the trickled % while the real progress hasn't jumped
          // past it. When the next real update arrives, showWarmProgress
          // re-asserts the authoritative value.
          els.warmProgressPercent.textContent = `${Math.round(next)}%`;
        }
      } else {
        // Reached the cap — stop the trickle, the next real update will
        // restart it with a fresh cap.
        window.clearInterval(state.warmTrickleTimer);
        state.warmTrickleTimer = null;
      }
    };
    state.warmTrickleTimer = window.setInterval(tick, 200);
  }

  function stopWarmTrickle() {
    if (state.warmTrickleTimer) {
      window.clearInterval(state.warmTrickleTimer);
      state.warmTrickleTimer = null;
    }
  }

  function showWarmProgress(payload) {
    if (!els.warmProgress) return;
    const state_name = payload.state || 'warming';
    const progress = Math.max(0, Math.min(1, Number(payload.progress || 0)));
    const percent = Math.round(progress * 100);
    const label = warmProgressLabelFor(payload);
    const sub = warmProgressSubTextFor(payload);

    els.warmProgress.hidden = false;
    els.warmProgress.classList.remove('is-active', 'is-done', 'is-error');
    if (state_name === 'ready') {
      els.warmProgress.classList.add('is-done');
    } else if (state_name === 'error') {
      els.warmProgress.classList.add('is-error');
    } else {
      els.warmProgress.classList.add('is-active');
    }
    if (els.warmProgressLabel) els.warmProgressLabel.textContent = label;
    if (els.warmProgressPercent) els.warmProgressPercent.textContent = `${percent}%`;
    if (els.warmProgressFill) els.warmProgressFill.style.width = `${percent}%`;
    if (els.warmProgressTrack) {
      els.warmProgressTrack.setAttribute('aria-valuenow', String(percent));
    }
    if (els.warmProgressSub) els.warmProgressSub.textContent = sub;

    // The CSS already smooths the bar between updates (transition: 0.32s).
    // For stages with no sub-progress (currently just loading_model), we also
    // run a small JS trickle that creeps the bar forward between polls so
    // the user sees continuous motion instead of a frozen fill.
    updateWarmTrickle(percent, state_name, String(payload.stage || ''));

    // Block the input while the selected KB is still warming. Use a
    // data-attribute on the input-section so CSS can dim/disable the
    // .input-container.
    if (els.inputSection) {
      const blockInput = state_name !== 'ready';
      els.inputSection.classList.toggle('is-warming', blockInput);
    }
    // Hard-disable the textarea + send button so keyboard input is also
    // blocked (pointer-events:none alone would still allow tab+type).
    if (els.input) {
      if (state_name !== 'ready') {
        els.input.setAttribute('disabled', 'disabled');
        els.input.setAttribute('aria-disabled', 'true');
      } else {
        els.input.removeAttribute('disabled');
        els.input.removeAttribute('aria-disabled');
      }
    }
    if (els.send) {
      if (state_name !== 'ready') {
        els.send.setAttribute('disabled', 'disabled');
      } else {
        els.send.removeAttribute('disabled');
      }
    }
  }

  function hideWarmProgress(options = {}) {
    if (!els.warmProgress) return;
    if (options.animate) {
      els.warmProgress.classList.remove('is-active', 'is-done', 'is-error');
    }
    els.warmProgress.hidden = true;
    if (els.inputSection) els.inputSection.classList.remove('is-warming');
    if (els.input) {
      els.input.removeAttribute('disabled');
      els.input.removeAttribute('aria-disabled');
    }
    if (els.send) {
      els.send.removeAttribute('disabled');
    }
  }

  function scheduleWarmProgressHide(graphId, options = {}) {
    const delay = options.delay ?? 1400;
    const keepError = Boolean(options.keepError);
    window.setTimeout(() => {
      // If the user has since switched KBs, hide immediately.
      if (state.warmPollingGraphId && state.warmPollingGraphId !== graphId) {
        hideWarmProgress({ animate: true });
        return;
      }
      if (!keepError) {
        hideWarmProgress({ animate: true });
      } else {
        // For errors, just unblock the input so the user can try again,
        // but keep the bar visible until the user dismisses / switches.
        if (els.inputSection) els.inputSection.classList.remove('is-warming');
      }
    }, delay);
  }

  function warmProgressLabelFor(payload) {
    if (payload.state === 'ready') {
      return 'Knowledge base is ready';
    }
    if (payload.state === 'error') {
      return 'Failed to prepare knowledge base';
    }
    const stage = String(payload.stage || '');
    switch (stage) {
      case 'preparing_kg':
        return 'Preparing knowledge base…';
      case 'parsing_records':
        return 'Parsing ledger records…';
      case 'indexing_triples':
        return 'Indexing triples…';
      case 'writing_artifacts':
        return 'Writing graph artifacts…';
      case 'loading_model':
        return 'Loading embedding model…';
      case 'building_vector_index':
      case 'encoding_entities':
        return 'Encoding entities…';
      case 'encoding_relations':
        return 'Encoding relations…';
      case 'activating_scope':
        return 'Activating knowledge base…';
      case 'queued':
        return 'Waiting for the previous build to finish…';
      default:
        return 'Building knowledge base…';
    }
  }

  function warmProgressSubTextFor(payload) {
    const msg = String(payload.message || '').trim();
    if (!msg) return '';
    if (msg === String(payload.stage || '')) return '';
    return msg;
  }

  async function renderGraphScopeSelector() {
    if (!els.graphScopeSelect) return;
    // Ensure the catalog has been fetched (awaited on init, but if a render was
    // triggered by some code path before the catalog loaded, fetch it now).
    if (!Array.isArray((state.health || {}).graph_catalog) || (state.health.graph_catalog || []).length === 0) {
      try { await loadGraphCatalog(); } catch (_) { /* fall through to empty render */ }
    }
    const graphs = graphCatalog();
    const current = selectedGraph();
    const options = graphs.map((graph) => {
      const stats = graph.stats || {};
      // Accept both `graph.stats.triple_count` (heavy import path) and
      // `graph.triple_count` (light path used by the landing page loader).
      const tripleCount = stats.triple_count != null
        ? stats.triple_count
        : (graph.triple_count != null ? graph.triple_count : 0);
      const label = `${graph.name || graph.id || 'Knowledge base'} · ${formatTripleCount(tripleCount)} triples`;
      return `<option value="${escapeHtml(graph.id || '')}">${escapeHtml(label)}</option>`;
    });
    els.graphScopeSelect.innerHTML = options.join('');
    if (current) {
      els.graphScopeSelect.value = current.id;
    }
    if (els.kbScopeCurrent) {
      els.kbScopeCurrent.textContent = current
        ? current.name || current.id || 'Selected knowledge base'
        : graphs.length
          ? 'Select a knowledge base'
          : 'No knowledge bases yet';
    }
    renderKbScopeMenu(graphs, current);
    if (els.graphScopeHint) {
      els.graphScopeHint.textContent = current
        ? `Chat is scoped to ${current.name || current.id}`
        : graphs.length
          ? 'Select a knowledge base above to scope chat.'
          : 'No knowledge bases yet — upload one in the KB panel.';
    }
  }

  function renderKbScopeMenu(graphs, current) {
    if (!els.kbScopeMenu) return;
    if (!graphs.length) {
      els.kbScopeMenu.innerHTML = '<div class="kb-scope-menu-empty">No knowledge bases yet — upload one in the KB panel.</div>';
      return;
    }
    els.kbScopeMenu.innerHTML = graphs.map((graph) => {
      const stats = graph.stats || {};
      const isSelected = current && current.id === graph.id;
      const meta = `${formatTripleCount(stats.triple_count || 0)} triples`;
      return `<div class="kb-scope-menu-item${isSelected ? ' is-selected' : ''}" role="option" aria-selected="${isSelected ? 'true' : 'false'}" data-graph-id="${escapeHtml(graph.id || '')}">
        <span class="kb-scope-menu-item-dot"></span>
        <span class="kb-scope-menu-item-name">${escapeHtml(graph.name || graph.id || 'Knowledge base')}</span>
        <span class="kb-scope-menu-item-meta">${escapeHtml(meta)}</span>
      </div>`;
    }).join('');
    els.kbScopeMenu.querySelectorAll('.kb-scope-menu-item').forEach((item) => {
      item.addEventListener('click', () => {
        const id = item.getAttribute('data-graph-id') || '';
        if (id) {
          state.selectedGraphId = id;
          localStorage.setItem('kbqa_graph_scope_id', id);
          prewarmSelectedGraph(id);
        }
        closeKbScopeMenu();
        renderGraphScopeSelector();
        loadGraphSummary(state.selectedGraphId || '', { silent: true });
        renderSchemaPanel();
      });
    });
  }

  function positionKbScopeMenu() {
    if (!els.kbScopeMenu || !els.kbScopeTrigger) return;
    const rect = els.kbScopeTrigger.getBoundingClientRect();
    const margin = 10;
    const menuMinWidth = 280;
    const menuMaxWidth = 360;
    const menuWidth = Math.min(menuMaxWidth, Math.max(menuMinWidth, rect.width + 60));
    const left = Math.min(window.innerWidth - menuWidth - 12, Math.max(12, rect.left - 12));
    const top = rect.top - margin;
    els.kbScopeMenu.style.left = `${left}px`;
    els.kbScopeMenu.style.top = `${top}px`;
    els.kbScopeMenu.style.width = `${menuWidth}px`;
  }

  function openKbScopeMenu() {
    if (!els.kbScopeMenu || !els.kbScopeTrigger) return;
    els.kbScopeMenu.hidden = false;
    positionKbScopeMenu();
    requestAnimationFrame(() => {
      els.kbScopeMenu.setAttribute('data-open', 'true');
    });
    els.kbScopeTrigger.setAttribute('aria-expanded', 'true');
  }

  function closeKbScopeMenu() {
    if (!els.kbScopeMenu || !els.kbScopeTrigger) return;
    els.kbScopeMenu.removeAttribute('data-open');
    els.kbScopeTrigger.setAttribute('aria-expanded', 'false');
    setTimeout(() => {
      if (els.kbScopeMenu && els.kbScopeMenu.getAttribute('data-open') !== 'true') {
        els.kbScopeMenu.hidden = true;
      }
    }, 200);
  }

  function toggleKbScopeMenu() {
    if (!els.kbScopeMenu) return;
    if (els.kbScopeMenu.getAttribute('data-open') === 'true') {
      closeKbScopeMenu();
    } else {
      openKbScopeMenu();
    }
  }

  function formatTripleCount(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
    return String(n);
  }

  async function createSession() {
    const data = await api('/api/sessions', { method: 'POST' });
    await loadSessions();
    await openSession(data.session.id);
    closeHistory();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  async function ensureActiveSession() {
    if (!state.session) {
      state.creatingSession = true;
      updateSendState();
      try {
        const data = await api('/api/sessions', { method: 'POST' });
        await loadSessions();
        await openSession(data.session.id);
      } finally {
        state.creatingSession = false;
        updateSendState();
      }
    }
    if (!isSocketReady()) {
      await waitForSocketReady();
    }
  }

  function showLandingPage(options = {}) {
    disconnectSocket();
    state.session = null;
    state.activeStreamId = null;
    state.liveToolKey = '';
    state.activeModelDeltaKey = '';
    state.suppressModelOutputSync = false;
    state.resetModelOutputOnNextDelta = false;
    state.activeReasoning = '';
    state.activeReasoningKey = '';
    state.resetReasoningOnNextDelta = false;
    resetRunUi();
    renderAll();
    if (options.scrollTop) {
      requestAnimationFrame(() => window.scrollTo({ top: 0, left: 0, behavior: 'auto' }));
    }
  }

  async function openSession(sessionId) {
    const data = await api(`/api/sessions/${encodeURIComponent(sessionId)}`);
    state.session = data.session;
    state.activeStreamId = null;
    state.liveToolKey = '';
    resetRunUi();
    connect(sessionId);
    renderAll();
  }

  function connect(sessionId) {
    disconnectSocket();
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws/${sessionId}`);
    ws.onopen = updateSendState;
    ws.onmessage = (event) => handleSocketMessage(JSON.parse(event.data));
    ws.onclose = () => {
      if (state.ws !== ws) return;
      state.ws = null;
      updateSendState();
      if (state.session && state.session.id === sessionId) {
        resetRunUi();
        state.reconnectTimer = window.setTimeout(() => {
          if (!state.ws && state.session && state.session.id === sessionId) {
            connect(sessionId);
          }
        }, 1600);
      }
    };
    ws.onerror = updateSendState;
    state.ws = ws;
  }

  function disconnectSocket() {
    clearReconnectTimer();
    if (!state.ws) return;
    const oldSocket = state.ws;
    state.ws = null;
    oldSocket.close();
  }

  function handleSocketMessage(message) {
    if (message.type === 'history') {
      state.session = message.session;
      state.activeStreamId = null;
      state.activeModelDeltaKey = '';
      state.suppressModelOutputSync = false;
      resetRunUi();
      renderAll();
      return;
    }

    if (message.type === 'message') {
      upsertMessage(message.message);
      return;
    }

    if (message.type === 'run_start') {
      state.streaming = true;
      state.activeStreamId = message.stream_id || `stream-${message.run_id}`;
      state.liveToolKey = '';
      state.activeModelDeltaKey = '';
      state.suppressModelOutputSync = false;
      state.resetModelOutputOnNextDelta = false;
      state.activeReasoning = '';
      state.activeReasoningKey = '';
      state.resetReasoningOnNextDelta = false;
      els.runIndicator.classList.add('active');
      els.runDetail.textContent = '';
      updateSendState();
      renderMessages();
      renderSchemaPanel();
      return;
    }

    if (message.type === 'assistant_delta') {
      appendAssistantDelta(message);
      return;
    }

    if (message.type === 'reasoning_delta') {
      appendReasoningDelta(message);
      return;
    }

    if (message.type === 'reasoning_reset') {
      resetActiveReasoning();
      renderMessages();
      return;
    }

    if (message.type === 'event') {
      if (message.message && message.message.kind === 'model_output') {
        if (!state.suppressModelOutputSync) {
          syncActiveModelOutput(message.message);
        }
      } else {
        // Tool-only model turns should not blank the last text the model streamed.
        // The next assistant_delta/model_output will replace this preserved text.
        state.resetModelOutputOnNextDelta = true;
        state.resetReasoningOnNextDelta = true;
      }
      upsertMessage(message.message);
      renderSchemaPanel();
      return;
    }

    if (message.type === 'assistant_final') {
      const finalMessage = { ...message.message, streaming: false };
      state.activeStreamId = null;
      state.liveToolKey = '';
      state.activeModelDeltaKey = '';
      state.suppressModelOutputSync = false;
      state.resetModelOutputOnNextDelta = false;
      state.activeReasoning = '';
      state.activeReasoningKey = '';
      state.resetReasoningOnNextDelta = false;
      appendFinalAssistantMessage(finalMessage);
      return;
    }

    if (message.type === 'error') {
      upsertMessage(message.message);
      state.streaming = false;
      state.activeStreamId = null;
      state.activeModelDeltaKey = '';
      state.suppressModelOutputSync = false;
      state.resetModelOutputOnNextDelta = false;
      state.activeReasoning = '';
      state.activeReasoningKey = '';
      state.resetReasoningOnNextDelta = false;
      els.runIndicator.classList.remove('active');
      updateSendState();
      renderSchemaPanel();
      return;
    }

    if (message.type === 'run_done') {
      state.streaming = false;
      state.activeStreamId = null;
      state.liveToolKey = '';
      state.activeModelDeltaKey = '';
      state.suppressModelOutputSync = false;
      state.resetModelOutputOnNextDelta = false;
      state.activeReasoning = '';
      state.activeReasoningKey = '';
      state.resetReasoningOnNextDelta = false;
      els.runIndicator.classList.remove('active');
      updateSendState();
      loadSessions();
      loadHealth();
      renderSchemaPanel();
    }
  }

  function appendAssistantDelta(message) {
    if (!state.session || !message.delta) return;
    state.suppressModelOutputSync = false;
    const id = message.id || state.activeStreamId || 'stream-current';
    const modelDeltaKey = message.model_message_id || message.message_id || '';
    if (message.replace) {
      state.activeModelDeltaKey = modelDeltaKey;
      state.resetModelOutputOnNextDelta = true;
    } else if (modelDeltaKey && modelDeltaKey !== state.activeModelDeltaKey) {
      state.activeModelDeltaKey = modelDeltaKey;
      state.resetModelOutputOnNextDelta = true;
    }
    const existing = state.session.messages.find((item) => item.id === id);
    if (existing) {
      existing.content = state.resetModelOutputOnNextDelta
        ? message.delta
        : `${existing.content || ''}${message.delta}`;
      existing.streaming = true;
      existing.agent = message.agent || existing.agent || config.agentId;
      existing.timestamp = new Date().toISOString();
    } else {
      state.session.messages.push({
        id,
        role: 'assistant',
        content: message.delta,
        timestamp: new Date().toISOString(),
        streaming: true,
        agent: message.agent || config.agentId,
      });
    }
    state.resetModelOutputOnNextDelta = false;
    renderMessages();
    scrollToBottom();
  }

  function appendReasoningDelta(message) {
    if (!state.session || !message.delta) return;
    const reasoningKey = message.reasoning_epoch !== undefined && message.reasoning_epoch !== null
      ? `epoch:${message.reasoning_epoch}`
      : (message.model_message_id || message.message_id || '');
    if (
      message.replace
      || state.resetReasoningOnNextDelta
      || (reasoningKey && reasoningKey !== state.activeReasoningKey)
    ) {
      state.activeReasoning = '';
      state.activeReasoningKey = reasoningKey || '';
      state.resetReasoningOnNextDelta = false;
    }
    state.activeReasoning = `${state.activeReasoning || ''}${message.delta}`;
    renderMessages();
    scrollToBottom();
  }

  function resetActiveReasoning() {
    state.activeReasoning = '';
    state.activeReasoningKey = '';
    state.resetReasoningOnNextDelta = true;
  }

  function syncActiveModelOutput(message) {
    if (!state.session || !state.activeStreamId || !message || !message.content) return;
    const modelDeltaKey = message.model_message_id || message.message_id || '';
    if (modelDeltaKey) state.activeModelDeltaKey = modelDeltaKey;
    const existing = state.session.messages.find((item) => item.id === state.activeStreamId);
    if (existing) {
      existing.content = message.content;
      existing.streaming = true;
      existing.agent = message.agent || existing.agent || config.agentId;
      existing.timestamp = message.timestamp || new Date().toISOString();
    } else {
      state.session.messages.push({
        id: state.activeStreamId,
        role: 'assistant',
        content: message.content,
        timestamp: message.timestamp || new Date().toISOString(),
        streaming: true,
        agent: message.agent || config.agentId,
      });
    }
    // A complete model output just landed — any subsequent assistant_delta
    // belongs to a NEW model generation and must replace, not append.
    state.resetModelOutputOnNextDelta = true;
  }

  function clearActiveModelDraft() {
    if (!state.session || !state.activeStreamId) return;
    const existing = state.session.messages.find((item) => item.id === state.activeStreamId && item.role === 'assistant');
    if (existing) {
      existing.content = '';
      existing.streaming = true;
      existing.timestamp = new Date().toISOString();
      return;
    }
    state.session.messages.push({
      id: state.activeStreamId,
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      streaming: true,
      agent: config.agentId,
    });
  }

  function upsertMessage(message, forceReplace = false) {
    if (!state.session || !message) return;
    const index = state.session.messages.findIndex((item) => item.id === message.id);
    if (index >= 0) {
      if (forceReplace || state.session.messages[index].content !== message.content) {
        state.session.messages[index] = { ...state.session.messages[index], ...message };
      }
    } else {
      state.session.messages.push(message);
    }
    renderMessages();
    renderTitleVisibility();
    scrollToBottom();
  }

  function appendFinalAssistantMessage(message) {
    if (!state.session || !message) return;
    state.session.messages = (state.session.messages || []).filter((item) => item.id !== message.id);
    state.session.messages.push(message);
    renderMessages();
    renderTitleVisibility();
    scrollToBottom();
  }

  function bindEvents() {
    els.brandHome.addEventListener('click', () => {
      showLandingPage({ scrollTop: true });
    });
    els.newChat.addEventListener('click', createSession);
    els.historyBtn.addEventListener('click', openHistory);
    els.closeHistory.addEventListener('click', closeHistory);
    els.historyOverlay.addEventListener('click', closeHistory);
    if (els.historyNewChat) {
      els.historyNewChat.addEventListener('click', createSession);
    }
    els.themeToggle.addEventListener('click', toggleTheme);
    els.send.addEventListener('click', sendCurrentMessage);
    els.resetRun.addEventListener('click', reconnectCurrentSession);
    if (els.heroUpload) {
      els.heroUpload.addEventListener('click', () => openPanel('import'));
    }
    document.querySelectorAll('.capability-card').forEach((button) => {
      button.addEventListener('click', () => {
        const panel = button.dataset.panel || '';
        if (panel) {
          openPanel(panel);
          return;
        }
        els.input.focus();
        window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
      });
    });
    if (els.graphScopeSelect) {
      els.graphScopeSelect.addEventListener('change', async () => {
        state.selectedGraphId = els.graphScopeSelect.value || '';
        localStorage.setItem('kbqa_graph_scope_id', state.selectedGraphId);
        renderGraphScopeSelector();
        await loadGraphSummary(state.selectedGraphId || '');
        renderImportPanel();
        prewarmSelectedGraph();
      });
    }
    if (els.kbScopeTrigger) {
      els.kbScopeTrigger.addEventListener('click', (e) => {
        e.stopPropagation();
        toggleKbScopeMenu();
      });
    }
    document.addEventListener('click', (e) => {
      if (!els.kbScopeMenu) return;
      const menu = els.kbScopeMenu;
      const trigger = els.kbScopeTrigger;
      if (menu.contains(e.target) || (trigger && trigger.contains(e.target))) return;
      if (menu.getAttribute('data-open') === 'true') {
        closeKbScopeMenu();
      }
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && els.kbScopeMenu && els.kbScopeMenu.getAttribute('data-open') === 'true') {
        closeKbScopeMenu();
        if (els.kbScopeTrigger) els.kbScopeTrigger.focus();
      }
    });
    els.input.addEventListener('input', () => {
      autoResize();
      updateSendState();
    });
    els.input.addEventListener('compositionstart', () => {
      state.isComposing = true;
      clearCompositionGuard();
    });
    els.input.addEventListener('compositionend', () => {
      state.isComposing = false;
      state.compositionEndedAt = Date.now();
      state.suppressNextEnter = true;
      if (state.compositionGuardTimer) window.clearTimeout(state.compositionGuardTimer);
      state.compositionGuardTimer = window.setTimeout(clearCompositionGuard, 180);
      autoResize();
      updateSendState();
    });
    els.input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        if (isComposingInput(event)) {
          if (state.suppressNextEnter && !state.isComposing && !event.isComposing && event.keyCode !== 229) {
            event.preventDefault();
            clearCompositionGuard();
          }
          return;
        }
        event.preventDefault();
        sendCurrentMessage();
      }
    });
    if (els.promptGrid) {
      els.promptGrid.querySelectorAll('.question-card').forEach((button) => {
        button.addEventListener('click', () => {
          els.input.value = button.dataset.prompt || '';
          autoResize();
          updateSendState();
          sendCurrentMessage();
        });
      });
    }
    setFabOpen(false);
    els.fabMain.addEventListener('click', () => {
      setFabOpen(!els.fabContainer.classList.contains('open'));
    });
    if (els.fabSchema) {
      els.fabSchema.addEventListener('click', () => openPanel('schema'));
    }
    if (els.fabImport) {
      els.fabImport.addEventListener('click', () => openPanel('import'));
    }
    els.closePanel.addEventListener('click', closePanel);
    els.tabs.forEach((tab) => {
      tab.addEventListener('click', () => openPanel(tab.dataset.tab || 'schema'));
    });

    document.addEventListener('keydown', (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'n') {
        event.preventDefault();
        createSession();
      }
      if (event.key === 'Escape') {
        closeHistory();
        closePanel();
        setFabOpen(false, true);
      }
    });
  }

  async function sendCurrentMessage() {
    const content = els.input.value.trim();
    if (!content || state.streaming || state.creatingSession) return;
    if (els.inputSection && els.inputSection.classList.contains('is-warming')) return;
    try {
      await ensureActiveSession();
      if (!isSocketReady()) {
        throw new Error('Conversation channel is not ready yet.');
      }
      state.ws.send(JSON.stringify({ type: 'chat', content, graph_id: state.selectedGraphId || '' }));
      els.input.value = '';
      autoResize();
      updateSendState();
    } catch (error) {
      window.alert(`Unable to start the conversation: ${String(error.message || error)}`);
      updateSendState();
    }
  }

  async function renderAll() {
    renderHeroMetrics();
    renderTitleVisibility();
    renderSessionList();
    renderMessages();
    renderSchemaPanel();
    renderImportPanel();
    await renderGraphScopeSelector();
    updateSendState();
  }

  function renderTitleVisibility() {
    const messages = state.session ? state.session.messages || [] : [];
    const hasMessages = messages.length > 0;
    if (els.hero) els.hero.classList.toggle('hidden', hasMessages);
    if (els.capabilitySection) els.capabilitySection.classList.toggle('hidden', hasMessages);
    if (els.exampleSection) els.exampleSection.classList.toggle('hidden', hasMessages);
    els.messages.classList.toggle('active', hasMessages);
  }

  function renderSessionList() {
    if (els.sessionCount) {
      els.sessionCount.textContent = `${state.sessions.length} saved`;
    }
    els.sessions.innerHTML = '';
    if (!state.sessions.length) {
      els.sessions.innerHTML = `
        <div class="history-empty">
          <span class="history-empty-icon">○</span>
          <strong>No saved conversations yet</strong>
          <p>Start from the homepage and your graph questions will appear here.</p>
          <button class="history-empty-action" type="button">Start a new chat</button>
        </div>
      `;
      const action = els.sessions.querySelector('.history-empty-action');
      if (action) action.addEventListener('click', createSession);
      return;
    }
    for (const session of state.sessions) {
      const card = document.createElement('div');
      const isOpen = Boolean(state.session && session.id === state.session.id);
      const isDeleting = state.deletingSessionId === session.id;
      const isConfirmingDelete = state.confirmingDeleteSessionId === session.id;
      const messageCount = Number(session.message_count || 0);
      card.className = `history-card${isOpen ? ' active' : ''}`;
      card.innerHTML = `
        <button class="history-load" type="button" data-session-open="${escapeHtml(session.id)}">
          <span class="history-orb"><span class="history-dot"></span></span>
          <span class="history-copy">
            <span class="history-name">${escapeHtml(session.title || 'New conversation')}</span>
            <span class="history-meta">
              <span>${formatMessageCount(messageCount)}</span>
              <span>${formatDate(session.updated_at)}</span>
            </span>
          </span>
          <span class="history-status">${isOpen ? 'Current' : 'Open'}</span>
        </button>
        <button class="history-delete${isConfirmingDelete ? ' confirming' : ''}" type="button" data-session-delete="${escapeHtml(session.id)}" aria-label="${isConfirmingDelete ? 'Confirm delete conversation' : 'Delete conversation'}" title="${isConfirmingDelete ? 'Click again to delete' : 'Delete conversation'}" ${isDeleting ? 'disabled' : ''}>
          ${isDeleting ? '<span class="delete-spinner"></span><span>Deleting</span>' : isConfirmingDelete ? '<span class="delete-icon">!</span><span>Confirm</span>' : '<span class="delete-icon">×</span><span>Delete</span>'}
        </button>
      `;
      card.querySelector('[data-session-open]').addEventListener('click', async () => {
        state.confirmingDeleteSessionId = '';
        await openSession(session.id);
        closeHistory();
      });
      card.querySelector('[data-session-delete]').addEventListener('click', async (event) => {
        event.stopPropagation();
        if (state.confirmingDeleteSessionId !== session.id) {
          state.confirmingDeleteSessionId = session.id;
          renderSessionList();
          return;
        }
        await deleteSession(session.id);
      });
      els.sessions.appendChild(card);
    }
  }

  async function deleteSession(sessionId) {
    if (!sessionId || state.deletingSessionId) return;
    state.deletingSessionId = sessionId;
    state.confirmingDeleteSessionId = '';
    renderSessionList();
    try {
      const deletingCurrent = Boolean(state.session && state.session.id === sessionId);
      await api(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' });
      await loadSessions();
      if (deletingCurrent) {
        showLandingPage({ scrollTop: true });
      } else {
        renderSessionList();
      }
    } catch (error) {
      window.alert(`Failed to delete conversation: ${String(error.message || error)}`);
    } finally {
      state.deletingSessionId = '';
      state.confirmingDeleteSessionId = '';
      renderSessionList();
    }
  }

  function renderMessages() {
    if (!state.session) {
      els.messages.innerHTML = '';
      renderTitleVisibility();
      return;
    }
    const messages = state.session.messages || [];
    const visibleMessages = buildTimelineItems(messages);
    renderTitleVisibility();
    els.messages.innerHTML = visibleMessages.map(renderMessage).join('');
    els.messages.querySelectorAll('.event-head').forEach((head) => {
      head.addEventListener('click', () => head.closest('.event-card').classList.toggle('open'));
    });
  }

  function isRenderableMessage(message) {
    return message.role !== 'event' && !isActiveAssistantDraft(message);
  }

  function isVisibleEvent(message) {
    return message.role === 'event' && message.kind !== 'model_output';
  }

  function buildTimelineItems(messages) {
    const liveRun = buildLiveRun(messages);
    const items = [];
    let insertedLiveRun = false;
    messages.forEach((message, index) => {
      if (isRenderableMessage(message)) {
        items.push(message);
      }
      if (liveRun && index === liveRun.afterIndex) {
        items.push(liveRun);
        insertedLiveRun = true;
      }
    });
    if (liveRun && !insertedLiveRun) {
      items.push(liveRun);
    }
    return items;
  }

  function buildLiveRun(messages) {
    if (!state.activeStreamId) return null;
    const assistantIndex = messages.findIndex((item) => item.id === state.activeStreamId);
    const afterIndex = findLastUserIndex(messages, assistantIndex >= 0 ? assistantIndex : messages.length);
    const runItems = messages.slice(afterIndex + 1);
    const draft = messages.find((item) => item.id === state.activeStreamId && item.role === 'assistant') || null;
    const latestModelOutput = findLast(runItems, (item) => item.role === 'event' && item.kind === 'model_output');
    const events = runItems.filter(isVisibleEvent);
    const currentTool = findLast(events, (item) => item.role === 'event');
    const toolKey = currentTool ? `${currentTool.id || ''}:${currentTool.kind || ''}:${currentTool.title || ''}` : '';
    const toolSwapped = Boolean(toolKey && toolKey !== state.liveToolKey);
    if (toolKey) state.liveToolKey = toolKey;
    return {
      id: `${state.activeStreamId}-live`,
      role: 'run_progress',
      agent: (draft && draft.agent) || (currentTool && currentTool.agent) || config.agentId,
      content: draft ? (draft.content || '') : ((latestModelOutput && latestModelOutput.content) || ''),
      reasoning: state.activeReasoning || '',
      currentTool,
      eventCount: events.length,
      toolSwapped,
      afterIndex,
    };
  }

  function isActiveAssistantDraft(message) {
    return Boolean(state.activeStreamId && message.role === 'assistant' && message.id === state.activeStreamId);
  }

  function findLastUserIndex(messages, beforeIndex) {
    for (let index = Math.min(beforeIndex - 1, messages.length - 1); index >= 0; index -= 1) {
      if (messages[index].role === 'user') return index;
    }
    return -1;
  }

  function findLast(items, predicate) {
    for (let index = items.length - 1; index >= 0; index -= 1) {
      if (predicate(items[index])) return items[index];
    }
    return null;
  }

  function isComposingInput(event) {
    return state.isComposing || event.isComposing || event.keyCode === 229 || state.suppressNextEnter;
  }

  function clearCompositionGuard() {
    state.suppressNextEnter = false;
    if (state.compositionGuardTimer) {
      window.clearTimeout(state.compositionGuardTimer);
      state.compositionGuardTimer = null;
    }
  }

  function renderMessage(message) {
    const streamingClass = message.streaming ? ' streaming' : '';
    if (message.role === 'user') {
      return `<article class="message user"><div class="bubble">${escapeHtml(message.content)}</div></article>`;
    }
    if (message.role === 'run_progress') {
      return renderLiveRun(message);
    }
    if (message.role === 'assistant') {
      return renderAssistantResult(message, streamingClass);
    }
    if (message.role === 'event') {
      const kind = message.kind || 'event';
      return `
        <article class="message event">
          <div class="event-card ${escapeHtml(kind)}">
            <div class="event-head">
              <div class="event-title"><span class="event-dot"></span><span>${escapeHtml(message.title || kind)}</span></div>
              <span class="event-agent">${escapeHtml(message.agent || '')}</span>
            </div>
            <div class="event-body"><pre>${escapeHtml(message.content || '')}</pre></div>
          </div>
        </article>
      `;
    }
    return `<article class="message system"><div class="bubble">${escapeHtml(message.content || '')}</div></article>`;
  }

  function renderLiveRun(run) {
    const hasReasoning = Boolean(run.reasoning);
    const modelOutput = run.content
      ? formatMarkdown(run.content)
      : (hasReasoning
          ? '<span class="live-placeholder">Model is thinking… reasoning streaming below.</span>'
          : '<span class="live-placeholder">Waiting for the model to stream text...</span>');
    const reasoningPane = hasReasoning
      ? `
          <div class="run-reasoning-pane">
            <span class="run-section-label">Model thinking</span>
            <div class="run-reasoning-output">${escapeHtml(reasoningTail(run.reasoning))}</div>
          </div>
        `
      : '';
    return `
      <article class="message run-progress">
        <div class="avatar">${avatarLetter(run.agent)}</div>
        <div class="run-card">
          <div class="run-card-head">
            <div class="run-title"><span class="run-pulse"></span><span>Agent is working</span></div>
            <span class="run-count">${run.eventCount} tool updates</span>
          </div>
          <div class="run-tool-pane">
            <span class="run-section-label">Tool activity</span>
            ${renderCurrentTool(run.currentTool, run.toolSwapped, run.eventCount)}
          </div>
          ${reasoningPane}
          <div class="run-model-pane">
            <span class="run-section-label">Model output</span>
            <div class="run-model-output">${modelOutput}</div>
          </div>
        </div>
      </article>
    `;
  }

  function reasoningTail(text, limit = 1400) {
    const value = String(text || '');
    return value.length > limit ? `…${value.slice(-limit)}` : value;
  }

  // Extract the complete execute_code context: class definitions + context setup + LLM code + output.
  function tidyCodeForDisplay(text) {
    return String(text || '')
      .replace(/\r\n/g, '\n')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  }

  function getLastExecuteCodeData(beforeMessageId) {
    const messages = (state.session && state.session.messages) || [];
    const endIdx = beforeMessageId
      ? messages.findIndex((m) => m.id === beforeMessageId)
      : messages.length;
    const slice = endIdx >= 0 ? messages.slice(0, endIdx) : messages;

    // Find last execute_code tool_call
    let callIdx = -1;
    for (let i = slice.length - 1; i >= 0; i--) {
      const m = slice[i];
      if (m.role === 'event' && m.kind === 'tool_call' && m.tool === 'execute_code') {
        callIdx = i;
        break;
      }
    }
    if (callIdx < 0) return null;

    // Parse LLM code_lines from tool_call args
    let llmCode = null;
    try {
      const args = JSON.parse(slice[callIdx].content);
      const lines = Array.isArray(args.code_lines) ? args.code_lines : [];
      if (lines.length) llmCode = lines.join('\n');
    } catch (_) { /* ignore */ }

    // Extract ontology class definitions from the nearest preceding build_subgraph_schema tool_end
    let classCode = null;
    for (let i = callIdx - 1; i >= 0; i--) {
      const m = slice[i];
      if (m.role === 'event' && m.kind === 'tool_end' && m.tool === 'build_subgraph_schema') {
        const raw = m.content || '';
        // Class code sits between the two ======== separator lines
        const match = raw.match(/={6,}\n([\s\S]*?)\n={6,}/);
        if (match && match[1].trim()) classCode = match[1].trim();
        break;
      }
    }

    // Standard context block that explains the pre-initialised variables
    const contextBlock = [
      '# Runtime context (auto-initialized)',
      '# entities        : all graph entity instances',
      '# entities_by_name: { entity_name -> entity object }',
      '# mid_name_map    : { entity_name -> display name }',
      '# result_dict     : { "direct_results": [], "detailed_results": {} }',
      '# get_name(entity): returns the display name for an entity',
    ].join('\n');

    // Assemble full script for display
    const sections = [];
    if (classCode) sections.push('# Ontology class definitions\n' + classCode);
    sections.push(contextBlock);
    if (llmCode)   sections.push('# Model-generated inference code\n' + llmCode);
    const codeText = sections.length ? tidyCodeForDisplay(sections.join('\n\n')) : null;

    // Find the matching execute_code tool_end for execution output + full_code
    let outputText = null;
    let fullCode = null;
    for (let i = callIdx + 1; i < slice.length; i++) {
      const m = slice[i];
      if (m.role === 'event' && m.kind === 'tool_end' && m.tool === 'execute_code') {
        outputText = (m.content || '').trim() || null;
        fullCode   = m.full_code || null;   // backend-provided fully-rendered script
        break;
      }
    }

    // Prefer the fully-rendered script from the backend; fall back to frontend reconstruction.
    const finalCodeText = tidyCodeForDisplay(fullCode || codeText || '');
    if (!finalCodeText && !outputText) return null;
    return { codeText: finalCodeText || null, outputText };
  }

  function renderAssistantResult(message, streamingClass = '') {
    const output = message.content
      ? formatMarkdown(message.content)
      : '<span class="live-placeholder">No final answer was returned.</span>';

    const codeData = getLastExecuteCodeData(message.id);
    const lineCount = codeData && codeData.codeText
      ? codeData.codeText.split('\n').length
      : 0;

    const codePane = codeData ? `
      <div class="run-code-pane">
        <div class="run-code-header">
          <div class="run-code-header-left">
            <span class="run-section-label">Reasoning code</span>
            ${lineCount ? `<span class="run-code-meta">${lineCount} lines · Python</span>` : ''}
          </div>
          <button class="code-toggle-btn" onclick="this.closest('.run-code-pane').classList.toggle('collapsed')" type="button">
            <svg class="code-toggle-icon" width="12" height="12" viewBox="0 0 12 12" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M2 4.5L6 8L10 4.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            <span class="toggle-open">Hide code</span>
            <span class="toggle-closed">Show code</span>
          </button>
        </div>

        <div class="run-code-collapsed-hint">
          <span class="run-code-hint-icon">⚡</span>
          <span>Reasoning code was executed to derive the answer below. Click <strong>Show code</strong> to inspect the inference steps.</span>
        </div>

        <div class="run-code-body">
          ${codeData.codeText ? `
          <div class="run-code-section">
            <div class="run-code-section-bar">
              <span class="run-code-lang-tag">Python</span>
              <span class="run-code-section-title">Inference code</span>
            </div>
            <pre class="run-code-block python-highlight"><code>${highlightPython(codeData.codeText)}</code></pre>
          </div>
          ` : ''}
          ${codeData.outputText ? `
          <div class="run-code-section run-code-section-output">
            <div class="run-code-section-bar">
              <span class="run-code-lang-tag output">Output</span>
              <span class="run-code-section-title">Execution result</span>
            </div>
            <pre class="run-code-block run-output-block"><code>${escapeHtml(codeData.outputText)}</code></pre>
          </div>
          ` : ''}
        </div>
      </div>
    ` : '';

    return `
      <article class="message assistant run-result${streamingClass}">
        <div class="avatar">${avatarLetter(message.agent)}</div>
        <div class="run-card final-card">
          <div class="run-card-head">
            <div class="run-title"><span class="run-check">✓</span><span>Task complete</span></div>
            <span class="run-count">final answer</span>
          </div>
          <div class="run-tool-pane">
            <span class="run-section-label">Tool activity</span>
            <div class="current-tool-card complete">
              <div class="current-tool-topline">
                <span class="current-tool-status">Done</span>
              </div>
              <div class="current-tool-main">
                <strong>Analysis complete</strong>
                <span>Answer derived from graph evidence — see final answer below</span>
              </div>
            </div>
          </div>
          ${codePane}
          <div class="run-model-pane final-answer">
            <span class="run-section-label">Final answer</span>
            <div class="run-model-output">${output}</div>
          </div>
        </div>
      </article>
    `;
  }

  const KBQA_TOOL_LABELS = {
    search_entity: {
      doing: 'Searching the knowledge graph',
      doingDetail: 'Locating candidate entities by name and semantic similarity',
      done: 'Entities identified',
      doneDetail: 'Candidate nodes ranked and ready for reasoning',
    },
    search_predicate: {
      doing: 'Exploring graph relations',
      doingDetail: 'Retrieving matching predicates across the knowledge base',
      done: 'Relations retrieved',
      doneDetail: 'Relevant predicates fetched from the graph',
    },
    search_entity_by_predicate: {
      doing: 'Traversing graph edges',
      doingDetail: 'Finding connected entities via the selected relation',
      done: 'Traversal complete',
      doneDetail: 'Connected entities discovered along the predicate path',
    },
    list_predicates_by_entity: {
      doing: 'Mapping entity connections',
      doingDetail: 'Enumerating all relations for the target entity',
      done: 'Connections mapped',
      doneDetail: 'Entity relation profile assembled',
    },
    build_subgraph_schema: {
      doing: 'Building evidence subgraph',
      doingDetail: 'Extracting and structuring local graph evidence',
      done: 'Subgraph ready',
      doneDetail: 'Local evidence materialized for logical inference',
    },
    execute_code: {
      doing: 'Running logical inference',
      doingDetail: 'Executing reasoning code over the extracted subgraph',
      done: 'Inference complete',
      doneDetail: 'Answer derived from graph evidence',
    },
  };

  function renderCurrentTool(tool, swapped, stepCount) {
    if (!tool) {
      return `
        <div class="current-tool-card empty">
          <div class="current-tool-main">
            <strong>Preparing next step</strong>
            <span>The agent is planning its next action</span>
          </div>
        </div>
      `;
    }
    const toolName    = tool.tool || '';
    const isRunning   = tool.kind === 'tool_call';
    const isDone      = tool.kind === 'tool_end';
    const meta        = KBQA_TOOL_LABELS[toolName] || {};
    const statusClass = isDone ? 'done' : isRunning ? 'calling' : 'context';
    const label       = isDone ? 'Done' : isRunning ? 'Working' : 'Processing';

    let friendlyText, detail;
    if (isRunning) {
      friendlyText = (meta.doing   || 'Processing') + '...';
      detail       = meta.doingDetail || '';
    } else if (isDone) {
      friendlyText = meta.done     || meta.doing || 'Step complete';
      detail       = meta.doneDetail || '';
    } else if (tool.kind === 'graph_wait') {
      friendlyText = 'Initializing knowledge base...';
      detail       = 'Preparing the graph runtime for this session';
    } else if (tool.kind === 'graph_scope') {
      friendlyText = 'Knowledge base ready';
      detail       = 'Knowledge base scope activated successfully';
    } else {
      friendlyText = 'Processing';
      detail       = '';
    }

    const stepLabel = stepCount > 0 ? `Step ${stepCount}` : '';
    return `
      <div class="current-tool-card ${escapeHtml(statusClass)}${swapped ? ' tool-swapping' : ''}">
        <div class="current-tool-topline">
          <span class="current-tool-status">${escapeHtml(label)}</span>
          ${stepLabel ? `<span class="tool-step-label">${escapeHtml(stepLabel)}</span>` : ''}
          ${isRunning ? '<span class="tool-dots"><span></span><span></span><span></span></span>' : ''}
        </div>
        <div class="current-tool-main">
          <strong>${escapeHtml(friendlyText)}</strong>
          ${detail ? `<span>${escapeHtml(detail)}</span>` : ''}
        </div>
      </div>
    `;
  }

  function renderSchemaPanel() {
    if (!els.schemaContent) return;
    const health = state.health || {};
    const project = health.project || {};
    els.schemaContent.innerHTML = renderKbqaSchema(project, health.graph_runtime || {});
    const explorerSelect = $('#graph-explorer-select');
    if (explorerSelect) {
      explorerSelect.addEventListener('change', async () => {
        state.selectedGraphId = explorerSelect.value || '';
        localStorage.setItem('kbqa_graph_scope_id', state.selectedGraphId);
        renderGraphScopeSelector();
        renderImportPanel();
        await loadGraphSummary(state.selectedGraphId || '');
        prewarmSelectedGraph();
      });
    }
    const refresh = $('#graph-explorer-refresh');
    if (refresh) refresh.addEventListener('click', () => loadGraphSummary(state.selectedGraphId || ''));
    const manage = $('#graph-explorer-manage');
    if (manage) manage.addEventListener('click', () => openPanel('import'));
  }

  function renderImportPanel() {
    if (!els.importContent) return;
    const imports = graphCatalog();
    const result = state.importResult && state.importResult.import ? state.importResult.import : null;
    const latest = result ? [result, ...imports.filter((item) => item.id !== result.id)] : imports;
    const fileName = state.selectedGraphFileName || '';
    const needsFile = Boolean(state.importError && state.importError.includes('choose a graph file') && !fileName && !state.importingGraph);
    els.importContent.innerHTML = `
      <div class="schema-hero kbqa-hero import-hero refined-import-hero management-hero">
        <div>
          <span class="schema-kicker">KB Management</span>
          <h2>Add, Select, and Manage Knowledge Bases</h2>
          <p>Upload a typed KB file, inspect the knowledge-base catalog, choose the scope used by chat, or delete datasets you no longer need.</p>
        </div>
        <div class="import-steps" aria-label="KB import steps">
          <span><strong>1</strong>Upload typed triples</span>
          <span><strong>2</strong>Select chat scope</span>
          <span><strong>3</strong>Explore or delete</span>
        </div>
      </div>
      <form class="import-card refined-import-card" id="graph-import-form">
        <div class="import-field graph-name-field">
          <label class="field-label" for="graph-name">
            <span>KB name</span>
            <small>A short display name used in the scope selector and knowledge-base catalog.</small>
          </label>
          <div class="graph-name-input-wrap">
            <span class="graph-name-icon">KB</span>
            <input id="graph-name" name="dataset_name" type="text" placeholder="qa-test-products">
          </div>
        </div>
        <div class="import-field">
          <div class="field-label">
            <span>KB file</span>
            <small>TXT/TSV/CSV, JSON/JSONL, or Excel with head_name, head_type, relation, tail_name, tail_type.</small>
          </div>
          <label class="file-dropzone ${fileName ? 'has-file' : ''}" for="graph-file">
            <input id="graph-file" class="file-picker-input" name="file" type="file" accept=".txt,.tsv,.csv,.json,.jsonl,.ndjson,.xlsx,.xlsm,text/plain,application/json">
            <span class="file-dropzone-icon">T-KB</span>
            <span class="file-dropzone-copy">
              <strong id="graph-file-name">${escapeHtml(fileName || (state.importingGraph ? 'Uploading selected file...' : 'Drop or choose a knowledge-base file'))}</strong>
              <small id="graph-file-hint">${escapeHtml(fileName ? 'Ready to append to the knowledge-base ledger.' : 'Click to browse. Required: head_name, relation, tail_name. Optional: head_type, tail_type (default to "Entity" when omitted).')}</small>
            </span>
            <span id="graph-file-action" class="file-dropzone-action">${fileName ? 'Change file' : 'Browse file'}</span>
          </label>
        </div>
        <div class="format-example format-switcher-card">
          <div class="format-preview">
            ${renderFormatPreview()}
          </div>
          <div class="format-side-rail" aria-label="Choose example format">
            ${renderFormatTabs()}
          </div>
        </div>
        <div class="example-downloads" aria-label="Download typed KB examples">
          <span class="download-label">Examples</span>
          <a class="download-pill txt" href="/static/examples/typed-kg-example.txt" download><span>TXT</span><small>table file</small></a>
          <a class="download-pill excel" href="/static/examples/typed-kg-example.xlsx" download><span>Excel</span><small>workbook</small></a>
          <a class="download-pill json" href="/static/examples/typed-kg-example.json" download><span>JSON</span><small>triples</small></a>
        </div>
        <button class="import-submit primary-import-submit ${state.importingGraph ? 'loading' : ''} ${needsFile ? 'needs-file' : ''}" type="submit" ${state.importingGraph ? 'disabled' : ''}>
          <span class="submit-icon">${state.importingGraph ? '…' : needsFile ? '!' : '↑'}</span>
          <span class="submit-copy">
            <strong>${state.importingGraph ? 'Uploading and indexing...' : needsFile ? 'Choose a file first' : 'Upload knowledge base'}</strong>
            <small>${state.importingGraph ? escapeHtml(fileName || 'Processing file') : needsFile ? 'Select TXT, JSON, or Excel above before uploading' : 'Append knowledge base and select it for chat'}</small>
          </span>
        </button>
        ${state.importError ? `<div class="import-message error">${escapeHtml(state.importError)}</div>` : ''}
        ${result ? `<div class="import-message success">Knowledge base <strong>${escapeHtml(result.name || result.id)}</strong> (${formatDatasetStats(result.stats)}) uploaded. Indexing in progress — check the progress bar below in Managed Knowledge Bases.</div>` : ''}
      </form>
      <div class="schema-section-title"><h3>Managed Knowledge Bases</h3><small>${latest.length} uploaded knowledge bases</small></div>
      <div class="import-list">
        ${latest.length ? latest.map(renderImportItem).join('') : '<div class="empty-state">No uploaded knowledge bases yet. Upload a typed TXT, JSON, or Excel file to start.</div>'}
      </div>
    `;
    const form = $('#graph-import-form');
    if (form) form.addEventListener('submit', handleGraphImport);
    const fileInput = $('#graph-file');
    if (fileInput) fileInput.addEventListener('change', handleGraphFileChange);
    bindFormatTabs();
    els.importContent.querySelectorAll('[data-graph-select]').forEach((button) => {
      button.addEventListener('click', async () => {
        state.selectedGraphId = button.dataset.graphSelect || '';
        localStorage.setItem('kbqa_graph_scope_id', state.selectedGraphId);
        renderGraphScopeSelector();
        renderImportPanel();
        await loadGraphSummary(state.selectedGraphId || '');
        prewarmSelectedGraph();
      });
    });
    els.importContent.querySelectorAll('[data-graph-explore]').forEach((button) => {
      button.addEventListener('click', async () => {
        state.selectedGraphId = button.dataset.graphExplore || '';
        localStorage.setItem('kbqa_graph_scope_id', state.selectedGraphId);
        renderGraphScopeSelector();
        await loadGraphSummary(state.selectedGraphId || '', { silent: true });
        openPanel('schema');
        prewarmSelectedGraph();
      });
    });
    els.importContent.querySelectorAll('[data-graph-delete]').forEach((button) => {
      button.addEventListener('click', () => handleGraphDelete(button.dataset.graphDelete || '', button.dataset.graphName || 'this graph'));
    });
  }

  function exampleFormats() {
    return [
      {
        id: 'txt',
        label: 'TXT / TSV / CSV',
        kicker: 'Plain table (3-col or 5-col)',
        chip: 'fast paste',
        code: `# 3-col (head|relation|tail) — types default to "Entity"
Kismet|directed_by|William Dieterle

# 5-col (head|head_type|relation|tail|tail_type)
Kismet|film|written_by|Edward Knoblock|person`,
      },
      {
        id: 'json',
        label: 'JSON',
        kicker: 'Structured triples',
        chip: 'nested',
        code: `{
  "triples": [
    {
      "head_name": "Kismet",
      "relation": "directed_by",
      "tail_name": "William Dieterle"
    },
    {
      "head_name": "Kismet",
      "head_type": "film",
      "relation": "written_by",
      "tail_name": "Edward Knoblock",
      "tail_type": "person"
    }
  ]
}`,
      },
      {
        id: 'excel',
        label: 'Excel',
        kicker: 'Sheet columns',
        chip: 'xlsx',
        code: `Required columns
head_name, relation, tail_name
Optional columns
head_type, tail_type  (default to "Entity" when omitted)

Example
Kismet | film | directed_by | William Dieterle | person`,
      },
    ];
  }

  function activeExampleFormat() {
    return exampleFormats().find((item) => item.id === state.activeExampleFormat) || exampleFormats()[0];
  }

  function renderFormatPreview() {
    const format = activeExampleFormat();
    const isTabular = format.id === 'txt' || format.id === 'excel';

    let previewContent = '';
    if (isTabular) {
      // Show a 3-column preview (untyped) plus a 5-column variant when types
      // are supported. Encourages the simpler untyped form by default.
      previewContent = `
        <div class="format-preview-table-wrapper">
          <table class="format-preview-table">
            <thead>
              <tr>
                <th>head_name</th>
                <th>relation</th>
                <th>tail_name</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>Kismet</td>
                <td>directed_by</td>
                <td>William Dieterle</td>
              </tr>
              <tr>
                <td>Kismet</td>
                <td>written_by</td>
                <td>Edward Knoblock</td>
              </tr>
            </tbody>
          </table>
          <div class="format-preview-note">head_type / tail_type are optional. Add 2 more columns if you want typed KBs; otherwise entities default to "Entity".</div>
        </div>
      `;
    } else {
      previewContent = `<pre class="format-preview-raw">${escapeHtml(format.code)}</pre>`;
    }

    return `
      <div class="format-preview-topline">
        <div>
          <span>${escapeHtml(format.kicker)}</span>
          <strong>${escapeHtml(format.label)}</strong>
        </div>
      </div>
      <div class="format-preview-body">
        ${previewContent}
      </div>
    `;
  }

  function renderFormatTabs() {
    const icons = { txt: 'T', json: '{ }', excel: 'XL' };
    return exampleFormats().map((format) => `
      <button class="format-tab ${format.id === activeExampleFormat().id ? 'active' : ''}" type="button" data-example-format="${escapeHtml(format.id)}">
        <span>${escapeHtml(icons[format.id] || format.label.slice(0, 2))}</span>
        <strong>${escapeHtml(format.label.replace(' / TSV / CSV', ''))}</strong>
      </button>
    `).join('');
  }

  function bindFormatTabs() {
    if (!els.importContent) return;
    els.importContent.querySelectorAll('[data-example-format]').forEach((button) => {
      button.addEventListener('click', () => {
        state.activeExampleFormat = button.dataset.exampleFormat || 'txt';
        localStorage.setItem('kbqa_example_format', state.activeExampleFormat);
        updateFormatExampleUi();
      });
    });
  }

  function updateFormatExampleUi() {
    if (!els.importContent) return;
    const preview = els.importContent.querySelector('.format-preview');
    const rail = els.importContent.querySelector('.format-side-rail');
    if (preview) preview.innerHTML = renderFormatPreview();
    if (rail) rail.innerHTML = renderFormatTabs();
    bindFormatTabs();
  }

  function renderImportItem(item) {
    const stats = item.stats || {};
    const files = item.files || [];
    const active = item.id && item.id === state.selectedGraphId;
    const deleting = item.id && item.id === state.deletingGraphId;
    const isProcessing = item.status === 'processing';
    const isFailed = item.status === 'failed';
    const progress = state.graphProgress[item.id] || item.progress || null;
    const progressMsg = progress ? progress.message || '' : '';
    // show_progress: only show bar during encoding_entities / encoding_relations
    const showProgress = progress && progress.show_progress === true;
    // Extract real encoding percent from message like "Encoding entities 1792/254970 (1%)"
    let encodingPct = 0;
    if (showProgress && progressMsg) {
      const m = progressMsg.match(/\((\d+)%\)/);
      if (m) encodingPct = parseInt(m[1], 10);
    }
    return `
      <div class="import-item graph-management-item ${active ? 'active' : ''} ${isProcessing ? 'graph-processing' : ''}">
        <div class="graph-management-main">
          <div>
            <div class="graph-management-title-row">
              <strong>${escapeHtml(item.name || item.id || 'custom graph')}</strong>
              ${isProcessing ? '<span class="processing-graph-badge">Processing</span>' : ''}
              ${isFailed ? '<span class="failed-graph-badge">Failed</span>' : ''}
              ${active && !isProcessing && !isFailed ? '<span class="active-graph-badge">Active scope</span>' : ''}
            </div>
            <p>${isProcessing ? (progressMsg || 'Building indexes, please wait...') : isFailed ? (progressMsg || 'Processing failed. You can delete and retry.') : formatDatasetStats(stats)}</p>
            ${isProcessing && showProgress ? `
              <div class="graph-progress-bar-wrap">
                <div class="graph-progress-track">
                  <div class="graph-progress-bar" style="width: ${encodingPct}%"></div>
                </div>
                <span class="graph-progress-pct">${encodingPct}%</span>
              </div>
            ` : ''}
            <code>${escapeHtml(item.id || '')}</code>
          </div>
          <span class="graph-management-date">${escapeHtml(formatDate(item.created_at))}</span>
        </div>
        <div class="graph-management-actions">
          <button class="report-action graph-action" type="button" data-graph-select="${escapeHtml(item.id || '')}" ${active || isProcessing || isFailed ? 'disabled' : ''}>${active ? 'Selected' : 'Use in chat'}</button>
          <button class="report-action graph-action" type="button" data-graph-explore="${escapeHtml(item.id || '')}" ${isProcessing || isFailed ? 'disabled' : ''}>Explore</button>
          <button class="report-action graph-action danger" type="button" data-graph-delete="${escapeHtml(item.id || '')}" data-graph-name="${escapeHtml(item.name || item.id || 'this graph')}" ${deleting ? 'disabled' : ''}>${deleting ? 'Removing...' : (isProcessing ? 'Stop & Delete' : 'Delete')}</button>
        </div>
        ${!isProcessing ? `<details>
          <summary>Source and generated metadata</summary>
          <pre>${escapeHtml([
            `raw_path: ${item.raw_path || ''}`,
            `processed_dir: ${item.processed_dir || ''}`,
            ...(files.length ? files.slice(0, 24) : ['No file list available']),
          ].join('\n'))}</pre>
        </details>` : ''}
      </div>
    `;
  }

  function handleGraphFileChange(event) {
    const input = event.currentTarget;
    const file = input && input.files ? input.files[0] : null;
    state.selectedGraphFileName = file ? file.name : '';
    updateFilePickerLabel('graph-file-name', 'graph-file-hint', input.closest('.file-dropzone'), state.selectedGraphFileName);
  }

  async function handleGraphImport(event) {
    event.preventDefault();
    if (state.importingGraph) return;
    const fileInput = $('#graph-file');
    const nameInput = $('#graph-name');
    const file = fileInput && fileInput.files ? fileInput.files[0] : null;
    if (!file) {
      state.importError = 'Please choose a graph file before uploading.';
      renderImportPanel();
      return;
    }
    state.selectedGraphFileName = file.name;
    const form = new FormData();
    form.append('file', file);
    form.append('dataset_name', nameInput ? nameInput.value : '');
    state.importingGraph = true;
    state.importError = '';
    state.importResult = null;
    renderImportPanel();
    try {
      const response = await fetch('/api/graphs/imports', { method: 'POST', body: form });
      if (!response.ok) throw new Error(await response.text());
      state.importResult = await response.json();
      const imported = state.importResult && state.importResult.import ? state.importResult.import : null;
      if (imported && imported.id) {
        state.selectedGraphId = imported.id;
        localStorage.setItem('kbqa_graph_scope_id', state.selectedGraphId);
      }
      state.selectedGraphFileName = '';
      state.importError = '';
      // Refresh catalog so the processing card appears immediately
      state.health = await api('/api/health');
      normalizeSelectedGraph();
      await loadGraphSummary(state.selectedGraphId || '', { silent: true });
      // Start polling for graph processing status
      if (imported && imported.status === 'processing') {
        startGraphPolling(imported.id);
      }
    } catch (error) {
      state.selectedGraphFileName = '';
      state.importError = String(error.message || error);
    } finally {
      state.importingGraph = false;
      renderGraphScopeSelector();
      renderImportPanel();
      renderSchemaPanel();
    }
  }

  function startGraphPolling(graphId) {
    state.pollingGraphIds.add(graphId);
    if (state.graphPollingTimer) return; // already polling
    state.graphPollingTimer = window.setInterval(async () => {
      const ids = [...state.pollingGraphIds];
      if (!ids.length) {
        stopGraphPolling();
        return;
      }
      let anyChanged = false;
      let anyProgressUpdate = false;
      for (const gid of ids) {
        try {
          const data = await api(`/api/graphs/imports/${encodeURIComponent(gid)}/status`);
          if (data.status === 'ready') {
            state.pollingGraphIds.delete(gid);
            delete state.graphProgress[gid];
            // 同步更新 importResult.import 里的状态，避免 renderImportPanel 一直显示"Building indexes"
            if (state.importResult && state.importResult.import && state.importResult.import.id === gid) {
              state.importResult.import.status = 'ready';
              state.importResult.import.stats = data.stats || state.importResult.import.stats;
              state.importResult.import.progress = null;
            }
            // Kick off a background scope prewarm for the newly-ready graph
            // (if it's the active selection) so the user can ask their first
            // question on it without waiting for the vector index to build.
            if (gid === state.selectedGraphId) {
              prewarmSelectedGraph(gid);
            }
            anyChanged = true;
          } else if (data.status === 'failed') {
            state.pollingGraphIds.delete(gid);
            state.graphProgress[gid] = data.progress || { stage: 'failed', progress: 1, message: 'Processing failed.' };
            if (state.importResult && state.importResult.import && state.importResult.import.id === gid) {
              state.importResult.import.status = 'failed';
              state.importResult.import.progress = state.graphProgress[gid];
            }
            anyChanged = true;
          } else if (data.progress) {
            const prev = state.graphProgress[gid];
            if (!prev || prev.stage !== data.progress.stage || prev.progress !== data.progress.progress) {
              anyProgressUpdate = true;
            }
            state.graphProgress[gid] = data.progress;
          }
        } catch (error) {
          state.pollingGraphIds.delete(gid);
          delete state.graphProgress[gid];
          anyChanged = true;
        }
      }
      if (anyChanged) {
        // /api/health does not include the graph catalog — preserve the
        // one loadGraphCatalog() put on state so a polling tick doesn't
        // wipe the KB selector.
        const previousCatalog = (state.health && state.health.graph_catalog) || null;
        state.health = await api('/api/health');
        if (previousCatalog && Array.isArray(previousCatalog)) {
          state.health.graph_catalog = previousCatalog;
          state.health.custom_graph_imports = previousCatalog;
        }
        normalizeSelectedGraph();
        await loadGraphSummary(state.selectedGraphId || '', { silent: true });
      }
      if (anyChanged || anyProgressUpdate) {
        renderGraphScopeSelector();
        renderImportPanel();
        renderSchemaPanel();
      }
      if (!state.pollingGraphIds.size) {
        stopGraphPolling();
      }
    }, 3000);
  }

  function stopGraphPolling() {
    if (state.graphPollingTimer) {
      window.clearInterval(state.graphPollingTimer);
      state.graphPollingTimer = null;
    }
    state.pollingGraphIds.clear();
  }

  async function handleGraphDelete(graphId, graphName) {
    if (!graphId || state.deletingGraphId) return;
    // 看当前图谱是不是正在处理中（决定提示文案）
    const target = graphCatalog().find((g) => g.id === graphId);
    const isProcessing = target && target.status === 'processing';
    const confirmMsg = isProcessing
      ? `"${graphName}" is still processing.\n\nClick OK to stop the background task and delete the graph. Some index files may remain. Continue?`
      : `Delete "${graphName}"? This removes the uploaded graph and rebuilds the graph ledger.`;
    const confirmed = window.confirm(confirmMsg);
    if (!confirmed) return;

    state.deletingGraphId = graphId;
    state.importError = '';
    state.importResult = null;
    renderImportPanel();
    try {
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 90000);
      let response;
      try {
        response = await fetch(`/api/graphs/imports/${encodeURIComponent(graphId)}`, {
          method: 'DELETE',
          signal: controller.signal,
        });
      } finally {
        window.clearTimeout(timeout);
      }
      if (!response.ok && response.status !== 404) throw new Error(await response.text());
      // /api/health does not include the graph catalog — preserve the
      // current one so it isn't wiped by this refresh.
      const previousCatalog = (state.health && state.health.graph_catalog) || null;
      state.health = await api('/api/health');
      if (previousCatalog && Array.isArray(previousCatalog)) {
        state.health.graph_catalog = previousCatalog;
        state.health.custom_graph_imports = previousCatalog;
      }
      if (state.selectedGraphId === graphId) {
        state.selectedGraphId = '';
        localStorage.setItem('kbqa_graph_scope_id', '');
      }
      // Refresh the catalog so the deleted graph disappears from the
      // dropdown, and so a fresh default scope can be auto-picked.
      await loadGraphCatalog();
      normalizeSelectedGraph();
      await loadGraphSummary(state.selectedGraphId || '', { silent: true });
      // Prewarm the (possibly auto-picked replacement) scope so the user's
      // next question on it doesn't pay the cold-build cost.
      prewarmSelectedGraph();
      state.importError = response.status === 404 ? 'That graph was already removed; the catalog has been refreshed.' : '';
    } catch (error) {
      state.importError = error && error.name === 'AbortError'
        ? 'Delete is still taking too long. The UI was unlocked; refresh the graph catalog in a moment.'
        : String(error.message || error);
    } finally {
      state.deletingGraphId = '';
      renderGraphScopeSelector();
      renderImportPanel();
      renderSchemaPanel();
    }
  }

  function renderKbqaSchema(project, graphRuntime) {
    const graphs = graphCatalog();
    const summary = state.graphSummary || {};
    const stats = summary.stats || graphRuntime || project.dataset || {};
    const current = selectedGraph();
    const scopeName = current ? current.name || current.id : 'No knowledge base selected';
    const options = graphs.map((graph) => {
      const graphStats = graph.stats || {};
      const label = `${graph.name || graph.id || 'Knowledge base'} · ${graphStats.triple_count || 0} triples`;
      return `<option value="${escapeHtml(graph.id || '')}" ${graph.id === state.selectedGraphId ? 'selected' : ''}>${escapeHtml(label)}</option>`;
    }).join('');
    const loading = state.summaryLoading ? '<div class="empty-state">Loading graph summary...</div>' : '';
    const error = state.summaryError ? `<div class="import-message error">${escapeHtml(state.summaryError)}</div>` : '';
    const explorerBody = !state.summaryLoading ? `
      <div class="graph-overview-grid">
        ${graphStatCard('Entities', stats.entity_count || 0, 'Unique subjects and objects')}
        ${graphStatCard('Entity Types', stats.entity_type_count || 0, 'Distinct uploaded node types')}
        ${graphStatCard('Relations', stats.relation_count || 0, 'Distinct typed relation schemas')}
        ${graphStatCard('Triples', stats.triple_count || 0, 'Evidence statements')}
      </div>

      ${renderDenseGraphMap(summary.dense_subgraph || {})}

      <div class="schema-section-title"><h3>Graph Samples</h3><small>raw examples from the selected scope</small></div>
      <div class="explorer-sample-grid">
        <div class="explorer-card">
          <div class="explorer-card-head"><strong>Sample Nodes</strong><span>${(summary.sample_nodes || []).length}</span></div>
          ${renderSampleNodes(summary.sample_nodes || [])}
        </div>
        <div class="explorer-card">
          <div class="explorer-card-head"><strong>Sample Relations</strong><span>${(summary.sample_relations || []).length}</span></div>
          ${renderSampleRelations(summary.sample_relations || [])}
        </div>
        <div class="explorer-card wide">
          <div class="explorer-card-head"><strong>Sample Triples</strong><span>${(summary.sample_triples || []).length}</span></div>
          ${renderSampleTriples(summary.sample_triples || [])}
        </div>
      </div>

      <div class="schema-section-title"><h3>Frequent Items</h3><small>top 10 by occurrence</small></div>
      <div class="frequency-grid">
        <div class="explorer-card">
          <div class="explorer-card-head"><strong>Top Nodes</strong><span>10 max</span></div>
          ${renderFrequencyList(summary.top_nodes || [], 'node')}
        </div>
        <div class="explorer-card">
          <div class="explorer-card-head"><strong>Top Relations</strong><span>10 max</span></div>
          ${renderFrequencyList(summary.top_relations || [], 'relation')}
        </div>
      </div>
    ` : '';
    return `
      <div class="graph-explorer-hero">
        <div>
          <span class="schema-kicker">Graph Explorer</span>
          <h2>${escapeHtml(scopeName)}</h2>
          <p>Choose a graph, inspect representative nodes and triples, then use the same scope in chat for grounded code reasoning.</p>
        </div>
        <div class="graph-explorer-controls">
          <label for="graph-explorer-select">Graph to view</label>
          <select id="graph-explorer-select">${options}</select>
          <div class="graph-explorer-actions">
            <button class="report-action" id="graph-explorer-refresh" type="button">Refresh</button>
            <button class="report-action" id="graph-explorer-manage" type="button">Manage graphs</button>
          </div>
        </div>
      </div>
      ${error || loading}
      ${explorerBody}
    `;
  }

  function graphStatCard(label, value, body) {
    return `<div class="status-card explorer-stat"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><p>${escapeHtml(body)}</p></div>`;
  }

  function renderDenseGraphMap(graph) {
    const nodes = (graph.nodes || []).slice(0, 15);
    const edges = graph.edges || [];
    const info = graph.summary || {};
    if (!nodes.length) {
      return `
        <div class="graph-map-card empty-graph-map">
          <div class="graph-map-topline">
            <div>
              <span class="graph-map-kicker">Dense Graph Map</span>
              <h3>Top 15 Connected Nodes</h3>
              <p>No connected nodes were found in this graph scope yet.</p>
            </div>
          </div>
        </div>
      `;
    }

    const width = 980;
    const height = 560;
    const placedNodes = layoutDenseGraph(nodes, width, height);
    const nodeById = new Map(placedNodes.map((node) => [node.id, node]));
    const maxWeight = Math.max(1, ...placedNodes.map((node) => Number(node.weight || 1)));
    const visibleEdges = edges
      .filter((edge) => nodeById.has(edge.source) && nodeById.has(edge.target))
      .slice(0, 34);
    const edgeMarkup = visibleEdges.map((edge, index) => renderGraphEdge(edge, index, nodeById)).join('');
    const nodeMarkup = placedNodes.map((node, index) => renderGraphNode(node, index, maxWeight)).join('');
    const focus = info.focus_node || nodes[0].label || nodes[0].id || 'selected graph';
    const title = `Top 15 dense map around ${focus}`;

    return `
      <div class="graph-map-card">
        <div class="graph-map-topline">
          <div>
            <span class="graph-map-kicker">Dense Graph Map</span>
            <h3>Top 15 Connected Nodes</h3>
            <p>A real topology slice ranked by node degree and internal edge density. Larger circles mean more graph connections.</p>
          </div>
          <div class="graph-map-metrics" aria-label="Dense graph metrics">
            <span><strong>${escapeHtml(info.node_count || nodes.length)}</strong> nodes</span>
            <span><strong>${escapeHtml(info.edge_count || visibleEdges.length)}</strong> edges</span>
            <span><strong>${escapeHtml(info.density || 0)}</strong> density</span>
          </div>
        </div>
        <div class="graph-map-canvas">
          <svg class="kg-map-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeAttr(title)}">
            <defs>
              <radialGradient id="kg-node-focus" cx="36%" cy="28%" r="72%">
                <stop offset="0%" stop-color="#fef3c7"></stop>
                <stop offset="48%" stop-color="#38bdf8"></stop>
                <stop offset="100%" stop-color="#1d4ed8"></stop>
              </radialGradient>
              <radialGradient id="kg-node-core" cx="34%" cy="26%" r="76%">
                <stop offset="0%" stop-color="#ffffff"></stop>
                <stop offset="54%" stop-color="#93c5fd"></stop>
                <stop offset="100%" stop-color="#2563eb"></stop>
              </radialGradient>
              <radialGradient id="kg-node-neighbor" cx="34%" cy="26%" r="76%">
                <stop offset="0%" stop-color="#ecfeff"></stop>
                <stop offset="56%" stop-color="#67e8f9"></stop>
                <stop offset="100%" stop-color="#0f766e"></stop>
              </radialGradient>
              <linearGradient id="kg-edge-gradient" x1="0%" x2="100%" y1="0%" y2="0%">
                <stop offset="0%" stop-color="#0f172a" stop-opacity=".18"></stop>
                <stop offset="48%" stop-color="#2563eb" stop-opacity=".72"></stop>
                <stop offset="100%" stop-color="#06b6d4" stop-opacity=".52"></stop>
              </linearGradient>
              <marker id="kg-arrow" markerWidth="9" markerHeight="9" refX="7.5" refY="4.5" orient="auto" markerUnits="strokeWidth">
                <path d="M1,1 L8,4.5 L1,8 Z" fill="#2563eb" opacity=".72"></path>
              </marker>
              <filter id="kg-soft-glow" x="-40%" y="-40%" width="180%" height="180%">
                <feGaussianBlur stdDeviation="5" result="blur"></feGaussianBlur>
                <feMerge>
                  <feMergeNode in="blur"></feMergeNode>
                  <feMergeNode in="SourceGraphic"></feMergeNode>
                </feMerge>
              </filter>
            </defs>
            <rect class="kg-map-bg" x="18" y="18" width="944" height="524" rx="34"></rect>
            <circle class="kg-map-orbit orbit-one" cx="${width / 2}" cy="${height / 2}" r="178"></circle>
            <ellipse class="kg-map-orbit orbit-two" cx="${width / 2}" cy="${height / 2}" rx="386" ry="218"></ellipse>
            <g class="kg-map-edges">${edgeMarkup}</g>
            <g class="kg-map-nodes">${nodeMarkup}</g>
          </svg>
        </div>
      </div>
    `;
  }

  function layoutDenseGraph(nodes, width, height) {
    const centerX = width / 2;
    const centerY = height / 2;
    const focusIndex = Math.max(0, nodes.findIndex((node) => node.role === 'focus'));
    const ordered = focusIndex === 0 ? nodes : [nodes[focusIndex], ...nodes.filter((_, index) => index !== focusIndex)];
    const innerCount = Math.min(6, Math.max(0, ordered.length - 1));
    const outerCount = Math.max(0, ordered.length - 1 - innerCount);
    return ordered.map((node, index) => {
      if (index === 0) {
        return { ...node, x: centerX, y: centerY, layoutRole: 'focus' };
      }
      if (index <= innerCount) {
        const angle = -Math.PI / 2 + ((index - 1) / Math.max(1, innerCount)) * Math.PI * 2;
        return {
          ...node,
          x: centerX + Math.cos(angle) * 246,
          y: centerY + Math.sin(angle) * 144,
          layoutRole: 'core',
        };
      }
      const outerIndex = index - innerCount - 1;
      const angle = -Math.PI / 2 + (outerIndex / Math.max(1, outerCount)) * Math.PI * 2 + Math.PI / Math.max(5, outerCount);
      const ripple = outerIndex % 2 === 0 ? 1 : -1;
      return {
        ...node,
        x: centerX + Math.cos(angle) * (384 + ripple * 12),
        y: centerY + Math.sin(angle) * (214 - ripple * 8),
        layoutRole: 'neighbor',
      };
    });
  }

  function renderGraphEdge(edge, index, nodeById) {
    const source = nodeById.get(edge.source);
    const target = nodeById.get(edge.target);
    if (!source || !target) return '';
    const dx = target.x - source.x;
    const dy = target.y - source.y;
    const length = Math.max(1, Math.hypot(dx, dy));
    const curve = ((index % 2 === 0 ? 1 : -1) * Math.min(72, 22 + length * 0.08));
    const cx = (source.x + target.x) / 2 + (-dy / length) * curve;
    const cy = (source.y + target.y) / 2 + (dx / length) * curve;
    const label = shortGraphLabel(edge.label || edge.relation || 'relation', 22);
    const labelWidth = Math.max(64, Math.min(168, label.length * 7 + 22));
    let angle = Math.atan2(dy, dx) * 180 / Math.PI;
    if (angle > 92 || angle < -92) angle += 180;
    const strokeWidth = Math.min(5.2, 1.35 + Math.sqrt(Number(edge.weight || 1)) * 0.72);
    const path = `M ${source.x.toFixed(1)} ${source.y.toFixed(1)} Q ${cx.toFixed(1)} ${cy.toFixed(1)} ${target.x.toFixed(1)} ${target.y.toFixed(1)}`;
    return `
      <g class="kg-edge-group">
        <path class="kg-edge-shadow" d="${path}" stroke-width="${(strokeWidth + 7).toFixed(2)}"></path>
        <path class="kg-edge" d="${path}" stroke-width="${strokeWidth.toFixed(2)}" marker-end="url(#kg-arrow)"></path>
        <g class="kg-edge-label" transform="translate(${cx.toFixed(1)} ${cy.toFixed(1)}) rotate(${angle.toFixed(1)})">
          <rect x="${(-labelWidth / 2).toFixed(1)}" y="-13" width="${labelWidth}" height="26" rx="13"></rect>
          <text text-anchor="middle" dominant-baseline="central">${escapeHtml(label)}</text>
        </g>
      </g>
    `;
  }

  function renderGraphNode(node, index, maxWeight) {
    const weight = Number(node.weight || 1);
    const normalized = Math.sqrt(weight / Math.max(1, maxWeight));
    const radius = Math.round(20 + normalized * 18 + (index === 0 ? 7 : 0));
    const tone = index === 0 ? 'focus' : index < 7 ? 'core' : 'neighbor';
    const label = shortGraphLabel(node.label || node.id || 'node', tone === 'neighbor' ? 16 : 18);
    return `
      <g class="kg-node ${tone}" transform="translate(${node.x.toFixed(1)} ${node.y.toFixed(1)})">
        <circle class="kg-node-halo" r="${radius + 13}"></circle>
        <circle class="kg-node-ring" r="${radius + 5}"></circle>
        <circle class="kg-node-body" r="${radius}" filter="url(#kg-soft-glow)"></circle>
        <text class="kg-node-label" text-anchor="middle" dominant-baseline="central">${escapeHtml(label)}</text>
        <text class="kg-node-meta" y="${radius + 22}" text-anchor="middle">${escapeHtml(formatCompactNumber(weight))} links</text>
      </g>
    `;
  }

  function shortGraphLabel(value, limit) {
    return truncate(String(value || ''), limit || 18);
  }

  function formatCompactNumber(value) {
    const number = Number(value || 0);
    if (number >= 1000000) return `${(number / 1000000).toFixed(1)}m`;
    if (number >= 1000) return `${(number / 1000).toFixed(number >= 10000 ? 0 : 1)}k`;
    return String(number);
  }

  function renderSampleNodes(nodes) {
    if (!nodes.length) return '<div class="empty-state compact">No nodes found in this scope.</div>';
    return `<div class="node-chip-list">${nodes.map((node) => `<span>${escapeHtml(node.name || node)}${node.type ? ` · ${escapeHtml(node.type)}` : ''}</span>`).join('')}</div>`;
  }

  function renderSampleRelations(relations) {
    if (!relations.length) return '<div class="empty-state compact">No relations found in this scope.</div>';
    return `<div class="relation-sample-list">${relations.map((relation) => `
      <div class="relation-sample-row">
        <strong>${escapeHtml(relation.name || '')}</strong>
        <span>${escapeHtml(relation.count || 0)} triples</span>
        <small>${escapeHtml(formatRelationSchema(relation))} · ${escapeHtml(formatRelationExample(relation.example))}</small>
      </div>
    `).join('')}</div>`;
  }

  function renderSampleTriples(triples) {
    if (!triples.length) return '<div class="empty-state compact">No triples found in this scope.</div>';
    return `<div class="triple-list refined-triples">${triples.map((triple) => `
      <div class="triple-row refined-triple-row">
        <span class="triple-entity subject">${escapeHtml(triple.subject_name || triple.subject || '')}${triple.subject_type ? ` · ${escapeHtml(triple.subject_type)}` : ''}</span>
        <span class="triple-connector">
          <i></i>
          <strong>${escapeHtml(triple.relation_name || triple.relation || '')}</strong>
        </span>
        <span class="triple-entity object">${escapeHtml(triple.object_name || triple.object || '')}${triple.object_type ? ` · ${escapeHtml(triple.object_type)}` : ''}</span>
      </div>
    `).join('')}</div>`;
  }

  function renderFrequencyList(items, type) {
    if (!items.length) return `<div class="empty-state compact">No frequent ${type}s yet.</div>`;
    return `<div class="frequency-list">${items.slice(0, 10).map((item, index) => {
      const meta = type === 'node'
        ? `${item.type || 'Entity'} · ${item.count || 0} hits · out ${item.out || 0} · in ${item.in || 0}`
        : `${formatRelationSchema(item)} · ${item.count || 0} triples · ${formatRelationExample(item.example)}`;
      return `
        <div class="frequency-row">
          <span>${index + 1}</span>
          <strong>${escapeHtml(item.label || item.name || '')}</strong>
          <small>${escapeHtml(meta)}</small>
        </div>
      `;
    }).join('')}</div>`;
  }

  function formatRelationExample(example) {
    if (!example || (!example.subject && !example.object)) return 'No example triple available';
    return `${example.subject_name || example.subject || '?'} -> ${example.object_name || example.object || '?'}`;
  }

  function formatRelationSchema(relation) {
    const head = relation.head_type || (relation.example && relation.example.subject_type) || 'Entity';
    const name = relation.label || relation.relation_name || relation.name || 'relation';
    const tail = relation.tail_type || (relation.example && relation.example.object_type) || 'Entity';
    return `${head} -${name}-> ${tail}`;
  }

  function card(label, value, body) {
    return `<div class="status-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><p>${escapeHtml(body || '')}</p></div>`;
  }

  function openPanel(tab = 'schema') {
    if (!['schema', 'import'].includes(tab)) tab = 'schema';
    state.activePanel = tab;
    els.panel.classList.add('active');
    setFabOpen(false);
    els.tabs.forEach((item) => item.classList.toggle('active', item.dataset.tab === tab));
    const schemaSection = $('#schema-content');
    if (schemaSection) {
      schemaSection.classList.toggle('active', tab === 'schema');
    }
    const importSection = $('#import-content');
    if (importSection) {
      importSection.classList.toggle('active', tab === 'import');
    }
    if (tab === 'schema') renderSchemaPanel();
    if (tab === 'import') renderImportPanel();
  }

  function setFabOpen(open, restoreFocus = false) {
    if (!els.fabContainer || !els.fabMain) return;
    const actions = [els.fabSchema, els.fabImport].filter(Boolean);
    els.fabContainer.classList.toggle('open', open);
    els.fabMain.setAttribute('aria-expanded', open ? 'true' : 'false');
    actions.forEach((action) => {
      action.setAttribute('aria-hidden', open ? 'false' : 'true');
      action.tabIndex = open ? 0 : -1;
    });
    if (!open && restoreFocus && actions.includes(document.activeElement)) {
      els.fabMain.focus({ preventScroll: true });
    }
  }

  function closePanel() {
    els.panel.classList.remove('active');
  }

  function openHistory() {
    renderSessionList();
    els.historyOverlay.classList.add('active');
    els.historyModal.classList.add('active');
  }

  function closeHistory() {
    state.confirmingDeleteSessionId = '';
    els.historyOverlay.classList.remove('active');
    els.historyModal.classList.remove('active');
  }

  function updateSendState() {
    const waitingForSocket = Boolean(state.session && !isSocketReady());
    const warming = !!(els.inputSection && els.inputSection.classList.contains('is-warming'));
    els.send.disabled = warming || state.streaming || state.creatingSession || waitingForSocket || !els.input.value.trim();
  }

  function resetRunUi() {
    state.streaming = false;
    state.activeStreamId = null;
    state.liveToolKey = '';
    state.activeModelDeltaKey = '';
    state.suppressModelOutputSync = false;
    state.resetModelOutputOnNextDelta = false;
    state.activeReasoning = '';
    state.activeReasoningKey = '';
    state.resetReasoningOnNextDelta = false;
    els.runIndicator.classList.remove('active');
    els.runDetail.textContent = '';
    updateSendState();
  }

  function reconnectCurrentSession() {
    resetRunUi();
    if (!state.session) return;
    connect(state.session.id);
  }

  function isSocketReady() {
    return state.ws && state.ws.readyState === WebSocket.OPEN;
  }

  function waitForSocketReady(timeout = 2500) {
    if (isSocketReady()) return Promise.resolve(true);
    return new Promise((resolve) => {
      const started = Date.now();
      const timer = window.setInterval(() => {
        if (isSocketReady()) {
          window.clearInterval(timer);
          resolve(true);
          return;
        }
        if (Date.now() - started > timeout) {
          window.clearInterval(timer);
          resolve(false);
        }
      }, 50);
    });
  }

  function clearReconnectTimer() {
    if (state.reconnectTimer) {
      window.clearTimeout(state.reconnectTimer);
      state.reconnectTimer = null;
    }
  }

  function autoResize() {
    els.input.style.height = 'auto';
    const minHeight = 24;
    const maxHeight = 96;
    const nextHeight = Math.max(minHeight, Math.min(maxHeight, els.input.scrollHeight));
    els.input.style.height = `${nextHeight}px`;
    els.input.style.overflowY = els.input.scrollHeight > maxHeight ? 'auto' : 'hidden';
  }

  function scrollToBottom() {
    requestAnimationFrame(() => window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' }));
  }



  function applySavedTheme() {
    const theme = localStorage.getItem(`${config.agentId || 'studio'}-theme`) || 'light';
    document.body.classList.toggle('dark-theme', theme === 'dark');
    document.body.classList.toggle('light-theme', theme !== 'dark');
  }

  function toggleTheme() {
    const dark = !document.body.classList.contains('dark-theme');
    document.body.classList.toggle('dark-theme', dark);
    document.body.classList.toggle('light-theme', !dark);
    localStorage.setItem(`${config.agentId || 'studio'}-theme`, dark ? 'dark' : 'light');
  }

  function formatDatasetStats(stats) {
    if (!stats) return 'No dataset stats available.';
    const parts = [];
    if (stats.entity_count) parts.push(`${stats.entity_count} entities`);
    if (stats.relation_count) parts.push(`${stats.relation_count} relations`);
    if (stats.triple_count) parts.push(`${stats.triple_count} triples`);
    return parts.length ? parts.join(' · ') : 'File artifacts are present.';
  }

  function shortenModel(model) {
    const parts = String(model || '').split('/');
    return parts[parts.length - 1] || model;
  }

  function formatDate(value) {
    if (!value) return 'just now';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return 'just now';
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  function formatMessageCount(count) {
    return `${count} ${count === 1 ? 'message' : 'messages'}`;
  }

  function avatarLetter(agent) {
    const source = agent || config.brand || 'A';
    return escapeHtml(String(source).trim().charAt(0).toUpperCase() || 'A');
  }

  function truncate(value, limit) {
    const text = String(value || '').replace(/\s+/g, ' ').trim();
    return text.length <= limit ? text : `${text.slice(0, limit - 1)}…`;
  }

  function unique(values) {
    return [...new Set(values)];
  }

  function updateFilePickerLabel(nameId, hintId, dropzone, fileName) {
    const name = $(`#${nameId}`);
    const hint = $(`#${hintId}`);
    const action = $('#graph-file-action');
    if (name) name.textContent = fileName || 'Drop or choose a graph file';
    if (hint) hint.textContent = fileName ? 'Ready to append to the typed graph ledger.' : 'Click to browse. Required columns: head_name, head_type, relation, tail_name, tail_type.';
    if (action) action.textContent = fileName ? 'Change file' : 'Browse file';
    if (dropzone) dropzone.classList.toggle('has-file', Boolean(fileName));
  }

  function escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = value == null ? '' : String(value);
    return div.innerHTML;
  }

  function highlightPython(value) {
    const source = String(value == null ? '' : value);
    if (window.Prism && window.Prism.languages && window.Prism.languages.python) {
      return window.Prism.highlight(source, window.Prism.languages.python, 'python');
    }
    const keywords = new Set([
      'and', 'as', 'assert', 'async', 'await', 'break', 'class', 'continue',
      'def', 'del', 'elif', 'else', 'except', 'finally', 'for', 'from',
      'global', 'if', 'import', 'in', 'is', 'lambda', 'nonlocal', 'not',
      'or', 'pass', 'raise', 'return', 'try', 'while', 'with', 'yield',
    ]);
    const constants = new Set(['True', 'False', 'None', 'self']);
    const builtins = new Set([
      'dict', 'float', 'getattr', 'int', 'len', 'list', 'max', 'min', 'print',
      'range', 'set', 'sorted', 'str', 'sum', 'tuple', 'zip',
    ]);
    const tokenPattern = /("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|#.*|\b\d+(?:\.\d+)?\b|\b[A-Za-z_][A-Za-z0-9_]*\b)/g;
    let html = '';
    let lastIndex = 0;
    for (const match of source.matchAll(tokenPattern)) {
      const token = match[0];
      const index = match.index || 0;
      html += escapeHtml(source.slice(lastIndex, index));
      let cls = '';
      if (token.startsWith('#')) cls = 'tok-comment';
      else if (token.startsWith('"') || token.startsWith("'")) cls = 'tok-string';
      else if (/^\d/.test(token)) cls = 'tok-number';
      else if (keywords.has(token)) cls = 'tok-keyword';
      else if (constants.has(token)) cls = 'tok-constant';
      else if (builtins.has(token)) cls = 'tok-builtin';
      html += cls ? `<span class="${cls}">${escapeHtml(token)}</span>` : escapeHtml(token);
      lastIndex = index + token.length;
    }
    html += escapeHtml(source.slice(lastIndex));
    return html;
  }

  function escapeAttr(value) {
    return escapeHtml(value).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function formatMarkdown(value) {
    const lines = String(value || '').replace(/\r\n/g, '\n').split('\n');
    const blocks = [];
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      const trimmed = line.trim();
      if (!trimmed) {
        index += 1;
        continue;
      }
      const boxedAnswer = parseBoxedAnswer(trimmed);
      if (boxedAnswer) {
        blocks.push(renderBoxedAnswer(boxedAnswer));
        index += 1;
        continue;
      }
      if (trimmed.startsWith('```')) {
        const codeLines = [];
        index += 1;
        while (index < lines.length && !lines[index].trim().startsWith('```')) {
          codeLines.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) index += 1;
        blocks.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
        continue;
      }
      if (isMarkdownTableStart(lines, index)) {
        const header = parseMarkdownRow(lines[index]);
        index += 2;
        const rows = [];
        while (index < lines.length && isMarkdownRow(lines[index])) {
          rows.push(parseMarkdownRow(lines[index]));
          index += 1;
        }
        blocks.push(renderMarkdownTable(header, rows));
        continue;
      }
      const heading = trimmed.match(/^(#{1,3})\s+(.+)$/);
      if (heading) {
        const level = heading[1].length;
        blocks.push(`<h${level}>${formatInlineMarkdown(heading[2])}</h${level}>`);
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
          items.push(`<li>${formatInlineMarkdown(match[1])}</li>`);
          index += 1;
        }
        blocks.push(`<${listType}>${items.join('')}</${listType}>`);
        continue;
      }
      const paragraph = [];
      while (
        index < lines.length &&
        lines[index].trim() &&
        !lines[index].trim().startsWith('```') &&
        !isMarkdownTableStart(lines, index) &&
        !parseBoxedAnswer(lines[index].trim()) &&
        !/^(#{1,3})\s+/.test(lines[index].trim()) &&
        !/^[-*]\s+/.test(lines[index].trim()) &&
        !/^\d+\.\s+/.test(lines[index].trim())
      ) {
        paragraph.push(lines[index]);
        index += 1;
      }
      blocks.push(`<p>${paragraph.map((item) => formatInlineMarkdown(item)).join('<br>')}</p>`);
    }
    return blocks.join('');
  }

  function parseBoxedAnswer(value) {
    const text = String(value || '').trim();
    const prefixMatch = text.match(/^\\{1,2}boxed\s*\{/);
    if (!prefixMatch) return null;
    let depth = 0;
    let body = '';
    let foundOpen = false;
    for (let index = prefixMatch[0].length - 1; index < text.length; index += 1) {
      const char = text[index];
      if (char === '{') {
        if (foundOpen) body += char;
        depth += 1;
        foundOpen = true;
        continue;
      }
      if (char === '}') {
        depth -= 1;
        if (depth === 0) {
          const trailing = text.slice(index + 1).trim();
          return trailing ? null : normalizeBoxedAnswer(body);
        }
        body += char;
        continue;
      }
      if (foundOpen) body += char;
    }
    return null;
  }

  function normalizeBoxedAnswer(rawValue) {
    const raw = String(rawValue || '').trim();
    if (!raw) return { raw: '', values: [] };
    try {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        return { raw, values: parsed.map((item) => String(item)) };
      }
      return { raw, values: [String(parsed)] };
    } catch (error) {
      const quoted = [...raw.matchAll(/"([^"]+)"|'([^']+)'/g)].map((match) => match[1] || match[2]);
      if (quoted.length) return { raw, values: quoted };
      const cleaned = raw.replace(/^\[|\]$/g, '').trim();
      const values = cleaned
        ? cleaned.split(/\s*,\s*/).map((item) => item.replace(/^["']|["']$/g, '').trim()).filter(Boolean)
        : [];
      return { raw, values: values.length ? values : [raw] };
    }
  }

  function renderBoxedAnswer(answer) {
    const values = answer.values && answer.values.length ? answer.values : [answer.raw || ''];
    const chips = values.map((item) => `<strong>${formatInlineMarkdown(item)}</strong>`).join('');
    return `
      <div class="boxed-answer-card">
        <span class="boxed-answer-kicker">Final Answer</span>
        <div class="boxed-answer-values">${chips}</div>
      </div>
    `;
  }

  function formatInlineMarkdown(value) {
    let html = escapeHtml(value || '');
    html = html.replace(/\\{1,2}boxed\s*\{([^{}]+)\}/g, (_, answer) => renderBoxedAnswer(normalizeBoxedAnswer(answer)));
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    return html;
  }

  function isMarkdownRow(line) {
    const trimmed = String(line || '').trim();
    return trimmed.startsWith('|') && trimmed.endsWith('|') && parseMarkdownRow(trimmed).length > 1;
  }

  function isMarkdownTableStart(lines, index) {
    return index + 1 < lines.length && isMarkdownRow(lines[index]) && isMarkdownDivider(lines[index + 1]);
  }

  function isMarkdownDivider(line) {
    const cells = parseMarkdownRow(line);
    return cells.length > 1 && cells.every((cell) => /^:?-{3,}:?$/.test(cell.replace(/\s+/g, '')));
  }

  function parseMarkdownRow(line) {
    return String(line || '').trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
  }

  function renderMarkdownTable(header, rows) {
    const head = header.map((cell) => `<th>${formatInlineMarkdown(cell)}</th>`).join('');
    const body = rows.map((row) => `
      <tr>${header.map((_, index) => `<td>${formatInlineMarkdown(row[index] || '')}</td>`).join('')}</tr>
    `).join('');
    return `<div class="markdown-table-wrap"><table class="markdown-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  }

  function downloadText(filename, content) {
    const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }
})();
