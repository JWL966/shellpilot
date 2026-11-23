/* 聊天界面与 WebSocket 通信 */
const Chat = {
  ws: null,
  sessionId: null,
  running: false,
  reconnectTimer: null,
  currentAssistantEl: null,
  currentAssistantText: '',
  toolCards: {},       // tool_call_id -> element
  pendingConfirms: {}, // tool_call_id -> {card, timer}
  sessions: [],
  respondedThisTurn: false, // 本轮是否已有 AI 内容/工具事件，用于等待指示器

  init() {
    this.input = document.getElementById('chat-input');
    this.sendBtn = document.getElementById('btn-send');
    this.stopBtn = document.getElementById('btn-stop');
    this.messagesEl = document.getElementById('messages');
    this.scrollEl = document.getElementById('chat-scroll');
    this.welcomeEl = document.getElementById('welcome');

    this.sendBtn.addEventListener('click', () => this.send());
    this.stopBtn.addEventListener('click', () => this.stop());
    this.input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this.send(); }
    });
    this.input.addEventListener('input', () => this.autoResize());

    document.querySelectorAll('.example-card').forEach(card => {
      card.addEventListener('click', () => {
        this.input.value = card.dataset.text;
        this.autoResize(); this.input.focus();
      });
    });

    this.connect();
    this.loadSessions();
  },

  autoResize() {
    this.input.style.height = 'auto';
    this.input.style.height = Math.min(this.input.scrollHeight, 190) + 'px';
  },

  /* ---------- WebSocket ---------- */
  connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    this.ws = new WebSocket(`${proto}://${location.host}/ws`);
    this.ws.onopen = () => {
      App.state.wsReady = true; App.setStatus(true, '已连接');
      if (this.reconnectTimer) { clearTimeout(this.reconnectTimer); this.reconnectTimer = null; }
    };
    this.ws.onclose = () => {
      App.state.wsReady = false; App.setStatus(false, '连接断开，重连中…');
      this.reconnectTimer = setTimeout(() => this.connect(), 2000);
    };
    this.ws.onerror = () => App.setStatus(false, '连接错误');
    this.ws.onmessage = (ev) => {
      let data; try { data = JSON.parse(ev.data); } catch { return; }
      this.handleEvent(data);
    };
  },

  handleEvent(ev) {
    switch (ev.type) {
      case 'session_start':
        if (ev.session_id && ev.session_id !== this.sessionId) {
          this.sessionId = ev.session_id; this.loadSessions();
        }
        break;
      case 'assistant_thinking':
        this.removeTyping();
        this.appendAssistantDelta(ev.content);
        break;
      case 'tool_call_start':
        this.removeStreamingAssistant();
        this.removeTyping();
        this.startToolCard(ev);
        break;
      case 'confirm_required':
        this.removeStreamingAssistant();
        this.removeTyping();
        this.showConfirm(ev);
        break;
      case 'tool_call_end':
        this.endToolCard(ev);
        break;
      case 'final_answer':
        this.removeTyping();
        this.finishAssistant(ev.content);
        break;
      case 'error':
        this.removeStreamingAssistant();
        this.removeTyping();
        this.appendError(ev.message);
        break;
      case 'cancelled':
        this.removeStreamingAssistant();
        this.removeTyping();
        this.appendError(ev.message || '已取消');
        break;
      case 'agent_done':
        this.removeStreamingAssistant();
        this.removeTyping();
        this.setRunning(false);
        this.clearPendingConfirms();
        break;
    }
  },

  clearPendingConfirms() {
    Object.values(this.pendingConfirms).forEach(({ card, timer }) => {
      clearInterval(timer);
      if (!card.classList.contains('resolved')) {
        card.classList.add('resolved');
        card.querySelectorAll('button').forEach(b => b.disabled = true);
        const cd = card.querySelector('.confirm-countdown');
        if (cd) cd.textContent = '（本轮已结束）';
      }
    });
    this.pendingConfirms = {};
  },

  sendRaw(obj) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      this.appendError('WebSocket 未连接，请稍候重试'); return false;
    }
    this.ws.send(JSON.stringify(obj)); return true;
  },

  /* ---------- 会话管理 ---------- */
  async loadSessions() {
    try {
      const resp = await fetch('/api/sessions');
      this.sessions = (await resp.json()).sessions || [];
      this.renderSessions();
    } catch (e) { /* 忽略 */ }
  },

  renderSessions() {
    const ul = document.getElementById('session-list');
    ul.innerHTML = '';
    this.sessions.forEach(s => {
      const li = document.createElement('li');
      li.className = 'session-item' + (s.id === this.sessionId ? ' active' : '');
      const title = document.createElement('span');
      title.className = 's-title'; title.textContent = s.title || '未命名'; title.title = s.title || '';
      const del = document.createElement('button');
      del.className = 's-del'; del.textContent = '✕'; del.title = '删除会话';
      del.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (!confirm('删除这个会话？')) return;
        await fetch(`/api/sessions/${s.id}`, { method: 'DELETE' });
        if (s.id === this.sessionId) this.newSession();
        this.loadSessions();
      });
      li.appendChild(title); li.appendChild(del);
      li.addEventListener('click', () => this.openSession(s.id));
      ul.appendChild(li);
    });
  },

  newSession() {
    if (this.running) { this.appendError('当前会话正在执行中，请先停止'); return; }
    this.sessionId = null;
    this.messagesEl.innerHTML = '';
    this.welcomeEl.classList.remove('hidden');
    this.toolCards = {}; this.pendingConfirms = {};
    this.renderSessions(); this.input.focus();
  },

  async openSession(id) {
    if (this.running) { this.appendError('当前会话正在执行中，请先停止'); return; }
    try {
      const resp = await fetch(`/api/sessions/${id}`);
      if (!resp.ok) { this.appendError('会话不存在'); return; }
      this.sessionId = id;
      this.renderHistory((await resp.json()).session);
      this.renderSessions();
    } catch (e) { this.appendError('加载会话失败: ' + e.message); }
  },

  renderHistory(session) {
    this.messagesEl.innerHTML = '';
    this.toolCards = {}; this.pendingConfirms = {};
    const msgs = session.messages || [];

    for (const m of msgs) {
      if (m.role === 'user') {
        this.addUserMessage(m.content);
      } else if (m.role === 'assistant') {
        if (m.tool_calls && m.tool_calls.length) {
          for (const tc of m.tool_calls) {
            let args = {}; try { args = JSON.parse(tc.function.arguments || '{}'); } catch {}
            this.startToolCard({ tool_call_id: tc.id, name: tc.function.name, arguments: args, risk: 'safe' });
          }
        }
        if (m.content) this.addAssistantMessage(m.content);
      } else if (m.role === 'tool') {
        let result = {}; try { result = JSON.parse(m.content || '{}'); } catch {}
        const el = this.toolCards[m.tool_call_id];
        if (el) { this.fillToolResult(el, result); this.markToolDone(el, result); }
      }
    }
    this.welcomeEl.classList.add('hidden');
    this.scrollToBottom(true);
  },

  /* ---------- 发送消息 ---------- */
  send() {
    const text = this.input.value.trim();
    if (!text) return;
    if (this.running) return; // 第六点：AI 未回复完不允许再发

    const ok = this.sendRaw({
      type: 'user_message', session_id: this.sessionId,
      content: text, server_id: App.state.serverId || null,
      model: App.state.model || null,
    });
    if (!ok) return;

    this.welcomeEl.classList.add('hidden');
    this.addUserMessage(text);
    this.input.value = ''; this.autoResize();
    this.setRunning(true);
    this.removeStreamingAssistant();
    this.showTyping(); // 第五点：等待交互效果
  },

  stop() {
    if (!this.sessionId) return;
    this.sendRaw({ type: 'cancel', session_id: this.sessionId });
    this.removeTyping();
    this.setRunning(false);
  },

  setRunning(running) {
    this.running = running;
    this.sendBtn.classList.toggle('hidden', running);
    this.stopBtn.classList.toggle('hidden', !running);
    this.input.disabled = running;
    if (!running) this.input.focus();
  },

  /* ---------- 等待指示器 ---------- */
  showTyping() {
    if (this.typingEl) return;
    const el = document.createElement('div');
    el.className = 'msg assistant';
    el.innerHTML = `<div class="avatar">AI</div><div class="bubble"><span class="typing-dots"><span></span><span></span><span></span></span></div>`;
    this.messagesEl.appendChild(el);
    this.typingEl = el;
    this.scrollToBottom();
  },
  removeTyping() {
    if (this.typingEl) { this.typingEl.remove(); this.typingEl = null; }
  },

  /* ---------- 消息渲染 ---------- */
  addUserMessage(text) {
    const el = document.createElement('div');
    el.className = 'msg user';
    el.innerHTML = `<div class="avatar">你</div><div class="bubble"></div>`;
    el.querySelector('.bubble').textContent = text;
    this.messagesEl.appendChild(el);
    this.scrollToBottom(true);
  },

  createAssistantEl() {
    const el = document.createElement('div');
    el.className = 'msg assistant';
    el.innerHTML = `<div class="avatar">AI</div><div class="bubble"></div>`;
    this.messagesEl.appendChild(el);
    return el;
  },

  addAssistantMessage(text) {
    const el = this.createAssistantEl();
    el.querySelector('.bubble').innerHTML = App.markdown(text);
    return el;
  },

  appendAssistantDelta(delta) {
    // 这里只是临时流式气泡：一旦出现工具卡片或最终回复会被移除，
    // 保证最终回复永远是新追加在命令记录之后的一条消息。
    if (!this.currentAssistantEl) {
      this.currentAssistantEl = this.createAssistantEl();
      this.currentAssistantText = '';
    }
    this.currentAssistantText += delta;
    this.currentAssistantEl.querySelector('.bubble').innerHTML = App.markdown(this.currentAssistantText);
    this.scrollToBottom();
  },

  removeStreamingAssistant() {
    if (this.currentAssistantEl) {
      this.currentAssistantEl.remove();
      this.currentAssistantEl = null;
      this.currentAssistantText = '';
    }
  },

  finishAssistant(text) {
    // 第七点：AI 最终回复一定追加在所有工具卡片之后（按时间顺序）
    this.removeStreamingAssistant();
    if (!text) return;
    this.addAssistantMessage(text);
    this.scrollToBottom(true);
  },

  appendError(message) {
    this.removeTyping();
    const el = document.createElement('div');
    el.className = 'msg assistant';
    el.innerHTML = `<div class="avatar" style="background:var(--red);color:#fff">!</div><div class="bubble" style="color:var(--red)"></div>`;
    el.querySelector('.bubble').textContent = message;
    this.messagesEl.appendChild(el);
    this.scrollToBottom(true);
  },

  /* ---------- 工具卡片（第七点：按事件顺序追加，天然位于 AI 最终回复之前） ---------- */
  startToolCard(ev) {
    const card = document.createElement('div');
    const risk = ev.risk || 'safe';
    card.className = 'tool-card ' + risk;
    card.dataset.id = ev.tool_call_id;
    const cmdText = ev.arguments?.cmd || ev.arguments?.path || ev.arguments?.service || ev.arguments?.port || '';
    const label = ev.name === 'run_command' ? '$ ' + cmdText : `${ev.name} ${cmdText}`;

    card.innerHTML = `
      <div class="tool-head">
        <span class="tool-icon">${ev.name === 'run_command' ? '❯' : '🛠'}</span>
        <span class="tool-name">${App.escapeHtml(ev.name)}</span>
        <span class="tool-cmd"></span>
        <span class="badge running">运行中</span>
      </div>
      <div class="tool-body hidden"></div>`;
    card.querySelector('.tool-cmd').textContent = label;
    card.querySelector('.tool-head').addEventListener('click', () => {
      card.querySelector('.tool-body').classList.toggle('hidden');
    });

    // 工具卡片作为独立消息项追加（左侧），保持时间顺序
    const wrap = document.createElement('div');
    wrap.className = 'msg assistant tool-msg';
    wrap.appendChild(card);
    this.messagesEl.appendChild(wrap);
    this.toolCards[ev.tool_call_id] = { card, wrap };
    this.scrollToBottom();
  },

  endToolCard(ev) {
    const item = this.toolCards[ev.tool_call_id];
    if (!item) return;
    this.fillToolResult(item.card, ev);
    this.markToolDone(item.card, ev);
  },

  fillToolResult(card, result) {
    const body = card.querySelector('.tool-body');
    const text = result.result || result.output || result.error || '';
    if (card.dataset.filled) return;
    card.dataset.filled = '1';
    const pre = document.createElement('pre');
    pre.textContent = text;
    body.appendChild(pre);
    const meta = document.createElement('div');
    meta.className = 'tool-meta';
    const parts = [];
    if (result.exit_code !== undefined) parts.push(`exit=${result.exit_code}`);
    if (result.success === false) parts.push('失败');
    meta.textContent = parts.join(' · ');
    if (parts.length) body.appendChild(meta);
    if (text.length > 400) body.classList.add('hidden');
    else body.classList.remove('hidden');
  },

  markToolDone(card, ev) {
    const badge = card.querySelector('.badge');
    if (!badge) return;
    const success = ev.success !== false && !ev.error;
    badge.className = 'badge ' + (success ? 'ok' : 'err');
    badge.textContent = success ? '完成' : '失败';
  },

  /* ---------- 危险操作确认 ---------- */
  showConfirm(ev) {
    const card = document.createElement('div');
    card.className = 'confirm-card';
    card.dataset.id = ev.tool_call_id;
    card.innerHTML = `
      <div class="confirm-title"><span>⚠️</span> 需要你确认后执行</div>
      <div class="confirm-cmd"></div>
      <div class="confirm-reason"></div>
      <div class="confirm-actions">
        <button class="btn btn-danger btn-sm" data-act="approve">确认执行</button>
        <button class="btn btn-sm" data-act="reject">取消</button>
        <span class="confirm-countdown"></span>
      </div>`;
    card.querySelector('.confirm-cmd').textContent = ev.command || '';
    card.querySelector('.confirm-reason').textContent = ev.reason || '危险操作';

    const countdownEl = card.querySelector('.confirm-countdown');
    let remain = 300;
    const tick = () => {
      countdownEl.textContent = remain > 0 ? `（${remain}s 后自动取消）` : '（已超时）';
      if (remain <= 0) { clearInterval(timer); resolve(false); }
      remain--;
    };
    const timer = setInterval(tick, 1000);
    tick();
    const resolve = (approved) => {
      clearInterval(timer);
      card.classList.add('resolved');
      card.querySelectorAll('button').forEach(b => b.disabled = true);
      card.querySelector('.confirm-title').innerHTML = approved
        ? '<span>✅</span> 已确认执行' : '<span>⛔</span> 已取消';
      countdownEl.textContent = '';
      this.sendRaw({ type: 'confirm', tool_call_id: ev.tool_call_id, approved });
    };
    card.querySelector('[data-act="approve"]').addEventListener('click', () => resolve(true));
    card.querySelector('[data-act="reject"]').addEventListener('click', () => resolve(false));

    const wrap = document.createElement('div');
    wrap.className = 'msg assistant tool-msg';
    wrap.appendChild(card);
    this.messagesEl.appendChild(wrap);
    this.pendingConfirms[ev.tool_call_id] = { card, timer };
    this.scrollToBottom();
  },

  /* 第二点：只在用户接近底部时自动滚动，方便上拉查看历史 */
  scrollToBottom(force = false) {
    if (!force) {
      const distance = this.scrollEl.scrollHeight - this.scrollEl.scrollTop - this.scrollEl.clientHeight;
      if (distance > 120) return; // 用户正在上方阅读，不打扰
    }
    requestAnimationFrame(() => { this.scrollEl.scrollTop = this.scrollEl.scrollHeight; });
  },
};
