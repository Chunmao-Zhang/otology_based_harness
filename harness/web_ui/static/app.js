/**
 * Agent Harness - Frontend Application
 *
 * 功能：
 * - WebSocket 流式对话
 * - 多会话管理（切换不丢失上下文）
 * - Agent 切换
 * - Tool Call 可视化
 */

(function () {
  'use strict';

  // ─── State ──────────────────────────────────────────────────────────────────
  const state = {
    ws: null,
    sessionId: null,
    agents: [],
    sessions: {},       // sessionId -> { messages: [], agentId, agentName, title }
    currentAgentId: '',
    isStreaming: false,
    sidebarOpen: true,
  };

  // ─── DOM Elements ───────────────────────────────────────────────────────────
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  const els = {
    sidebar: $('#sidebar'),
    sessionsList: $('#sessions-list'),
    agentSelect: $('#agent-select'),
    chatContainer: $('#chat-container'),
    welcomeScreen: $('#welcome-screen'),
    messages: $('#messages'),
    messageInput: $('#message-input'),
    btnSend: $('#btn-send'),
    btnNewChat: $('#btn-new-chat'),
    btnToggleSidebar: $('#btn-toggle-sidebar'),
    topbarTitle: $('#topbar-title'),
    topbarAgent: $('#topbar-agent'),
    quickActions: $('#quick-actions'),
  };

  // ─── Init ───────────────────────────────────────────────────────────────────
  async function init() {
    await loadAgents();
    setupEventListeners();
    createNewSession();
  }

  // ─── API ────────────────────────────────────────────────────────────────────
  async function loadAgents() {
    try {
      const res = await fetch('/api/agents');
      const data = await res.json();
      state.agents = data.agents;
      renderAgentSelect();
    } catch (e) {
      console.error('Failed to load agents:', e);
    }
  }

  // ─── WebSocket ──────────────────────────────────────────────────────────────
  function connectWebSocket(sessionId) {
    if (state.ws) {
      state.ws.close();
    }

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${protocol}//${location.host}/ws/${sessionId}`;
    const ws = new WebSocket(url);

    ws.onopen = () => {
      // 请求历史消息
      ws.send(JSON.stringify({ type: 'get_history' }));
    };

    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      handleWSMessage(msg);
    };

    ws.onclose = () => {
      // 自动重连
      setTimeout(() => {
        if (state.sessionId === sessionId) {
          connectWebSocket(sessionId);
        }
      }, 3000);
    };

    ws.onerror = (e) => {
      console.error('WebSocket error:', e);
    };

    state.ws = ws;
  }

  function handleWSMessage(msg) {
    switch (msg.type) {
      case 'history':
        handleHistory(msg);
        break;
      case 'start':
        handleStreamStart();
        break;
      case 'assistant_message':
        handleAssistantMessage(msg);
        break;
      case 'tool_call':
        handleToolCall(msg);
        break;
      case 'tool_result':
        handleToolResult(msg);
        break;
      case 'done':
        handleStreamDone();
        break;
      case 'error':
        handleError(msg);
        break;
      case 'agent_switched':
        handleAgentSwitched(msg);
        break;
    }
  }

  // ─── Message Handlers ───────────────────────────────────────────────────────
  function handleHistory(msg) {
    const session = state.sessions[state.sessionId];
    if (!session) return;

    session.messages = msg.messages || [];
    session.agentId = msg.agent_id;
    session.agentName = msg.agent_name;

    state.currentAgentId = msg.agent_id;
    els.agentSelect.value = msg.agent_id;

    renderMessages();
    updateTopbar();
  }

  function handleStreamStart() {
    state.isStreaming = true;
    els.btnSend.disabled = true;
    showLoading();
  }

  function handleAssistantMessage(msg) {
    hideLoading();
    const session = state.sessions[state.sessionId];
    if (!session) return;

    // 更新或添加最新的 assistant 消息
    const lastMsg = session.messages[session.messages.length - 1];
    if (lastMsg && lastMsg.role === 'assistant' && lastMsg._streaming) {
      lastMsg.content = msg.content;
      lastMsg.agent = msg.agent;
    } else {
      session.messages.push({
        role: 'assistant',
        content: msg.content,
        agent: msg.agent,
        _streaming: true,
      });
    }

    renderMessages();
    scrollToBottom();
  }

  function handleToolCall(msg) {
    hideLoading();
    const session = state.sessions[state.sessionId];
    if (!session) return;

    session.messages.push({
      role: 'tool_call',
      tool: msg.tool,
      agent: msg.agent,
      args: msg.args,
      callId: msg.call_id,
    });

    renderMessages();
    scrollToBottom();
  }

  function handleToolResult(msg) {
    const session = state.sessions[state.sessionId];
    if (!session) return;

    session.messages.push({
      role: 'tool_result',
      tool: msg.tool,
      agent: msg.agent,
      output: msg.output,
    });

    renderMessages();
    scrollToBottom();
  }

  function handleStreamDone() {
    state.isStreaming = false;
    els.btnSend.disabled = false;
    hideLoading();

    // 标记最后一条 assistant 消息为非 streaming
    const session = state.sessions[state.sessionId];
    if (session) {
      const lastMsg = session.messages[session.messages.length - 1];
      if (lastMsg && lastMsg._streaming) {
        delete lastMsg._streaming;
      }
    }

    updateSessionsList();
  }

  function handleError(msg) {
    state.isStreaming = false;
    els.btnSend.disabled = false;
    hideLoading();

    const session = state.sessions[state.sessionId];
    if (session) {
      session.messages.push({
        role: 'system',
        content: `❌ ${msg.content}`,
      });
      renderMessages();
      scrollToBottom();
    }
  }

  function handleAgentSwitched(msg) {
    const session = state.sessions[state.sessionId];
    if (session) {
      session.agentId = msg.agent_id;
      session.agentName = msg.agent_name;
      session.messages.push({
        role: 'system',
        content: `已切换到 ${msg.agent_name}`,
      });
      renderMessages();
    }
    state.currentAgentId = msg.agent_id;
    updateTopbar();
  }

  // ─── Actions ────────────────────────────────────────────────────────────────
  function sendMessage(content) {
    if (!content.trim() || state.isStreaming || !state.ws) return;

    const session = state.sessions[state.sessionId];
    if (!session) return;

    // 添加用户消息
    session.messages.push({
      role: 'user',
      content: content.trim(),
      timestamp: new Date().toISOString(),
    });

    // 更新标题
    if (session.messages.filter(m => m.role === 'user').length === 1) {
      session.title = content.trim().slice(0, 20) + (content.length > 20 ? '...' : '');
    }

    // 隐藏欢迎页
    els.welcomeScreen.classList.add('hidden');
    els.messages.classList.remove('hidden');

    renderMessages();
    scrollToBottom();
    updateSessionsList();

    // 发送到 WebSocket
    state.ws.send(JSON.stringify({
      type: 'chat',
      content: content.trim(),
    }));

    // 清空输入
    els.messageInput.value = '';
    autoResizeInput();
  }

  function switchAgent(agentId) {
    if (agentId === state.currentAgentId || state.isStreaming) return;

    state.currentAgentId = agentId;

    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify({
        type: 'switch_agent',
        agent_id: agentId,
      }));
    }
  }

  function createNewSession() {
    const sessionId = generateId();
    const defaultAgent = state.agents.find(a => a.default) || state.agents[0];
    const agentId = defaultAgent ? defaultAgent.id : '';
    const agentName = defaultAgent ? defaultAgent.name : '';

    state.sessions[sessionId] = {
      messages: [],
      agentId: agentId,
      agentName: agentName,
      title: '新对话',
      createdAt: new Date().toISOString(),
    };

    switchToSession(sessionId);
    updateSessionsList();
  }

  function switchToSession(sessionId) {
    state.sessionId = sessionId;
    const session = state.sessions[sessionId];

    if (session) {
      state.currentAgentId = session.agentId;
      els.agentSelect.value = session.agentId;
    }

    connectWebSocket(sessionId);
    renderMessages();
    updateTopbar();
    updateSessionsList();
  }

  function deleteSession(sessionId) {
    delete state.sessions[sessionId];

    if (state.sessionId === sessionId) {
      const remaining = Object.keys(state.sessions);
      if (remaining.length > 0) {
        switchToSession(remaining[0]);
      } else {
        createNewSession();
      }
    }

    updateSessionsList();
  }

  // ─── Rendering ──────────────────────────────────────────────────────────────
  function renderAgentSelect() {
    els.agentSelect.innerHTML = state.agents.map(a =>
      `<option value="${a.id}" ${a.default ? 'selected' : ''}>${a.name}</option>`
    ).join('');

    const defaultAgent = state.agents.find(a => a.default) || state.agents[0];
    if (defaultAgent) {
      state.currentAgentId = defaultAgent.id;
    }

    renderQuickActions();
  }

  function renderQuickActions() {
    const actions = [
      { icon: '🚀', text: '执行完整竞品攻克流程', msg: '请执行完整的竞品攻克销售流程，从选定待攻克商家开始，依次完成所有11个环节。输入数据文件：/workspaces/jingping-coordinator/data/00_raw_shops.json' },
      { icon: '💰', text: '筛选价格敏感商家', msg: '请执行到筛选价格敏感商家为止。输入数据文件：/workspaces/jingping-coordinator/data/00_raw_shops.json' },
      { icon: '🎯', text: '仅选定待攻克商家', msg: '请执行步骤01：选定待攻克商家。输入文件：/workspaces/jingping-coordinator/data/00_raw_shops.json' },
      { icon: '📂', text: '查看当前数据状态', msg: '请列出 /workspaces/jingping-coordinator/data/ 目录下的所有文件，并简要说明每个文件的内容。' },
    ];

    els.quickActions.innerHTML = actions.map(a => `
      <div class="quick-action" data-message="${escapeAttr(a.msg)}">
        <span class="quick-action-icon">${a.icon}</span>
        <span class="quick-action-text">${a.text}</span>
      </div>
    `).join('');

    // 绑定点击事件
    els.quickActions.querySelectorAll('.quick-action').forEach(el => {
      el.addEventListener('click', () => {
        const msg = el.dataset.message;
        sendMessage(msg);
      });
    });
  }

  function renderMessages() {
    const session = state.sessions[state.sessionId];
    if (!session) return;

    const messages = session.messages;

    if (messages.length === 0) {
      els.welcomeScreen.classList.remove('hidden');
      els.messages.classList.add('hidden');
      return;
    }

    els.welcomeScreen.classList.add('hidden');
    els.messages.classList.remove('hidden');

    let html = '';
    for (const msg of messages) {
      html += renderSingleMessage(msg);
    }

    els.messages.innerHTML = html;

    // 绑定 tool step 折叠事件
    els.messages.querySelectorAll('.tool-step-header').forEach(header => {
      header.addEventListener('click', () => {
        header.parentElement.classList.toggle('expanded');
      });
    });
  }

  function renderSingleMessage(msg) {
    switch (msg.role) {
      case 'user':
        return `
          <div class="message message-user">
            <div class="message-bubble">${escapeHtml(msg.content)}</div>
          </div>`;

      case 'assistant':
        return `
          <div class="message message-assistant">
            <div class="message-avatar">🤖</div>
            <div class="message-body">
              ${msg.agent ? `<div class="message-agent-name">${escapeHtml(msg.agent)}</div>` : ''}
              <div class="message-content">${formatContent(msg.content)}</div>
            </div>
          </div>`;

      case 'tool_call':
        const isSubagent = msg.tool === 'task';
        const typeClass = isSubagent ? 'type-subagent' : 'type-tool';
        const icon = isSubagent ? '🔄' : '🔧';
        const displayName = isSubagent ? `SubAgent: ${getSubagentName(msg.args)}` : msg.tool;

        return `
          <div class="message">
            <div class="tool-step ${typeClass}">
              <div class="tool-step-header">
                <span class="tool-step-icon">${icon}</span>
                <span class="tool-step-name">${escapeHtml(displayName)}</span>
                ${msg.agent ? `<span class="tool-step-agent">${escapeHtml(msg.agent)}</span>` : ''}
                <svg class="tool-step-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>
              </div>
              <div class="tool-step-body">
                <div class="tool-step-section">
                  <div class="tool-step-section-label">参数</div>
                  <div class="tool-step-section-content">${escapeHtml(formatArgs(msg.args))}</div>
                </div>
              </div>
            </div>
          </div>`;

      case 'tool_result':
        return `
          <div class="message">
            <div class="tool-step type-result">
              <div class="tool-step-header">
                <span class="tool-step-icon">📋</span>
                <span class="tool-step-name">${escapeHtml(msg.tool || '结果')}</span>
                ${msg.agent ? `<span class="tool-step-agent">${escapeHtml(msg.agent)}</span>` : ''}
                <svg class="tool-step-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>
              </div>
              <div class="tool-step-body">
                <div class="tool-step-section">
                  <div class="tool-step-section-label">输出</div>
                  <div class="tool-step-section-content">${escapeHtml(msg.output || '')}</div>
                </div>
              </div>
            </div>
          </div>`;

      case 'system':
        return `
          <div class="message message-system">
            <span class="system-text">${escapeHtml(msg.content)}</span>
          </div>`;

      default:
        return '';
    }
  }

  function renderSessionsList() {
    updateSessionsList();
  }

  function updateSessionsList() {
    const entries = Object.entries(state.sessions).sort((a, b) =>
      (b[1].createdAt || '').localeCompare(a[1].createdAt || '')
    );

    els.sessionsList.innerHTML = entries.map(([id, session]) => `
      <div class="session-item ${id === state.sessionId ? 'active' : ''}" data-id="${id}">
        <svg class="session-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/>
        </svg>
        <span class="session-title">${escapeHtml(session.title || '新对话')}</span>
        <button class="session-delete" data-id="${id}" title="删除">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M18 6L6 18M6 6l12 12" stroke-linecap="round"/>
          </svg>
        </button>
      </div>
    `).join('');

    // 绑定事件
    els.sessionsList.querySelectorAll('.session-item').forEach(el => {
      el.addEventListener('click', (e) => {
        if (e.target.closest('.session-delete')) return;
        switchToSession(el.dataset.id);
      });
    });

    els.sessionsList.querySelectorAll('.session-delete').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        deleteSession(btn.dataset.id);
      });
    });
  }

  function updateTopbar() {
    const session = state.sessions[state.sessionId];
    if (session) {
      els.topbarTitle.textContent = session.title || '新对话';
      const agent = state.agents.find(a => a.id === session.agentId);
      els.topbarAgent.textContent = agent ? agent.name : session.agentId;
    }
  }

  // ─── Loading ────────────────────────────────────────────────────────────────
  function showLoading() {
    if (els.messages.querySelector('.loading-indicator')) return;
    const loadingEl = document.createElement('div');
    loadingEl.className = 'loading-indicator';
    loadingEl.innerHTML = `
      <div class="loading-dots">
        <span></span><span></span><span></span>
      </div>
      <span>思考中...</span>
    `;
    els.messages.appendChild(loadingEl);
    scrollToBottom();
  }

  function hideLoading() {
    const el = els.messages.querySelector('.loading-indicator');
    if (el) el.remove();
  }

  // ─── Event Listeners ────────────────────────────────────────────────────────
  function setupEventListeners() {
    // Send message
    els.btnSend.addEventListener('click', () => {
      sendMessage(els.messageInput.value);
    });

    els.messageInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage(els.messageInput.value);
      }
    });

    els.messageInput.addEventListener('input', () => {
      autoResizeInput();
      els.btnSend.disabled = !els.messageInput.value.trim() || state.isStreaming;
    });

    // New chat
    els.btnNewChat.addEventListener('click', createNewSession);

    // Toggle sidebar
    els.btnToggleSidebar.addEventListener('click', () => {
      state.sidebarOpen = !state.sidebarOpen;
      els.sidebar.classList.toggle('collapsed', !state.sidebarOpen);
    });

    // Agent select
    els.agentSelect.addEventListener('change', (e) => {
      switchAgent(e.target.value);
    });
  }

  // ─── Utilities ──────────────────────────────────────────────────────────────
  function autoResizeInput() {
    const el = els.messageInput;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 200) + 'px';
  }

  function scrollToBottom() {
    requestAnimationFrame(() => {
      els.chatContainer.scrollTop = els.chatContainer.scrollHeight;
    });
  }

  function generateId() {
    return Math.random().toString(36).slice(2, 14);
  }

  function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  function escapeAttr(str) {
    return str.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function formatContent(content) {
    if (!content) return '';
    // 简单的 markdown 格式化
    let html = escapeHtml(content);
    // 代码块
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
    // 行内代码
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    // 粗体
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    // 换行
    html = html.replace(/\n/g, '<br>');
    return html;
  }

  function formatArgs(argsStr) {
    if (!argsStr) return '';
    try {
      const obj = typeof argsStr === 'string' ? JSON.parse(argsStr) : argsStr;
      return JSON.stringify(obj, null, 2);
    } catch {
      return argsStr;
    }
  }

  function getSubagentName(argsStr) {
    try {
      const obj = typeof argsStr === 'string' ? JSON.parse(argsStr) : argsStr;
      return obj.subagent_type || 'unknown';
    } catch {
      return 'unknown';
    }
  }

  // ─── Start ──────────────────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', init);
})();
