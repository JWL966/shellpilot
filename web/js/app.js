/* 全局状态与工具函数 */
const App = {
  state: {
    view: 'chat',
    serverId: null,
    model: null,
    servers: [],
    models: [],
    wsReady: false,
  },

  init() {
    this.setupTheme();
    this.setupTabs();
    this.setupNewSession();
    this.setupLogout();
    this.loadServers();
    this.loadModels();
    Chat.init();
    Servers.init();
    Settings.init();
    Account.init();
  },

  setupLogout() {
    document.getElementById('btn-logout').addEventListener('click', async () => {
      try { await fetch('/api/logout', { method: 'POST' }); } catch (e) { /* 忽略 */ }
      location.href = '/login';
    });
  },

  /* 退出登录（供账号页等处复用） */
  async doLogout() {
    try { await fetch('/api/logout', { method: 'POST' }); } catch (e) { /* 忽略 */ }
    location.href = '/login';
  },

  setupTheme() {
    const saved = localStorage.getItem('ycai-theme') || 'dark';
    this.setTheme(saved);
    const btn = document.getElementById('btn-theme');
    btn.addEventListener('click', () => {
      const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      this.setTheme(next);
      localStorage.setItem('ycai-theme', next);
    });
  },

  setTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    const btn = document.getElementById('btn-theme');
    if (btn) btn.textContent = theme === 'dark' ? '☀️' : '🌙';
  },

  setupTabs() {
    document.querySelectorAll('.tab').forEach(tab => {
      tab.addEventListener('click', () => {
        const view = tab.dataset.view;
        document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t === tab));
        document.querySelectorAll('.view').forEach(v => {
          v.classList.toggle('active', v.id === `view-${view}`);
        });
        // 侧边栏只在对话视图显示
        document.getElementById('sidebar').classList.toggle('hidden', view !== 'chat');
        App.state.view = view;
        if (view === 'servers') Servers.render();
        if (view === 'settings') Settings.load();
        if (view === 'account') Account.load();
      });
    });
  },

  setupNewSession() {
    document.getElementById('btn-new-session').addEventListener('click', () => Chat.newSession());
  },

  async loadServers() {
    try {
      const resp = await fetch('/api/servers');
      if (resp.status === 401) { location.href = '/login'; return []; }  // 会话过期
      const data = await resp.json();
      this.state.servers = data.servers || [];
      this.renderServerSelect();
      return this.state.servers;
    } catch (e) {
      console.error('加载服务器失败', e);
      return [];
    }
  },

  renderServerSelect() {
    const select = document.getElementById('chat-server-select');
    const current = this.state.serverId || '';
    select.innerHTML = '<option value="">自动 / 按会话</option>';
    this.state.servers.forEach(s => {
      const opt = document.createElement('option');
      opt.value = s.id;
      opt.textContent = `${s.name || s.host} (${s.host})`;
      if (s.id === current) opt.selected = true;
      select.appendChild(opt);
    });
    if (!this.state.servers.length) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = '（请先添加服务器）';
      opt.disabled = true;
      select.appendChild(opt);
    }
    select.onchange = () => {
      App.state.serverId = select.value || null;
    };
  },

  async loadModels() {
    try {
      const resp = await fetch('/api/config/llm');
      const data = await resp.json();
      const llm = data.llm || {};
      const models = Array.isArray(llm.models) ? llm.models.filter(Boolean) : [];
      this.state.models = models;
      this.state.model = models[0] || llm.model || null;
      this.renderModelSelect();
    } catch (e) {
      console.error('加载模型列表失败', e);
    }
  },

  renderModelSelect() {
    const select = document.getElementById('chat-model-select');
    if (!select) return;
    const models = this.state.models.length ? this.state.models : (this.state.model ? [this.state.model] : []);
    select.innerHTML = '';
    if (!models.length) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = '（请先在设置中配置模型）';
      opt.disabled = true;
      select.appendChild(opt);
      return;
    }
    models.forEach((m) => {
      const opt = document.createElement('option');
      opt.value = m;
      opt.textContent = m;
      if (m === this.state.model) opt.selected = true;
      select.appendChild(opt);
    });
    select.onchange = () => { App.state.model = select.value || null; };
  },

  setStatus(ok, text) {
    const dot = document.getElementById('status-dot');
    const label = document.getElementById('status-text');
    dot.className = 'status-dot ' + (ok ? 'on' : 'off');
    label.textContent = text;
  },

  escapeHtml(str) {
    return String(str ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  },

  /* 轻量 Markdown 渲染（无外部依赖）：支持标题/列表/粗体/行内代码/代码块/分割线 */
  markdown(text) {
    const esc = App.escapeHtml(text);
    const blocks = [];
    // 先提取代码块
    let html = esc.replace(/```(\w*)\n([\s\S]*?)```/g, (m, lang, code) => {
      blocks.push(`<pre><code>${code.replace(/\n$/, '')}</code></pre>`);
      return `\u0000BLOCK${blocks.length - 1}\u0000`;
    });

    const lines = html.split('\n');
    const out = [];
    let listType = null;

    const closeList = () => {
      if (listType) { out.push(listType === 'ul' ? '</ul>' : '</ol>'); listType = null; }
    };

    for (let raw of lines) {
      const line = raw.replace(/\s+$/, '');
      if (/^\u0000BLOCK\d+\u0000\s*$/.test(line.trim())) {
        closeList();
        const idx = parseInt(line.trim().match(/\d+/)[0], 10);
        out.push(blocks[idx]);
        continue;
      }
      if (!line.trim()) { closeList(); continue; }

      const h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) { closeList(); out.push(`<h${h[1].length + 1}>${this.inline(h[2])}</h${h[1].length + 1}>`); continue; }

      if (/^(---+|\*\*\*+)$/.test(line.trim())) { closeList(); out.push('<hr>'); continue; }

      const ul = line.match(/^\s*[-*+]\s+(.*)$/);
      if (ul) {
        if (listType !== 'ul') { closeList(); out.push('<ul>'); listType = 'ul'; }
        out.push(`<li>${this.inline(ul[1])}</li>`);
        continue;
      }
      const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (ol) {
        if (listType !== 'ol') { closeList(); out.push('<ol>'); listType = 'ol'; }
        out.push(`<li>${this.inline(ol[1])}</li>`);
        continue;
      }
      closeList();
      out.push(`<p>${this.inline(line)}</p>`);
    }
    closeList();
    return out.join('\n');
  },

  inline(text) {
    return text
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  },
};
