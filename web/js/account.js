/* 账号页：查看个人信息 + 修改密码（改完强制退出重新登录） */
const Account = {
  init() {
    document.getElementById('btn-goto-password').addEventListener('click', () => {
      document.getElementById('account-password-panel').classList.remove('hidden');
      document.getElementById('account-password-panel').scrollIntoView({ behavior: 'smooth' });
    });
    document.getElementById('btn-close-password').addEventListener('click', () => {
      document.getElementById('account-password-panel').classList.add('hidden');
    });
    document.getElementById('btn-logout-account').addEventListener('click', () => App.doLogout());
    document.getElementById('account-password-form').addEventListener('submit', (e) => {
      e.preventDefault();
      this.changePassword();
    });
    // 页面加载即预取账号信息：即使标签切换逻辑异常，点开「账号」也不会卡在“加载中”
    this.load();
  },

  fmt(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  },

  async load() {
    const loading = document.getElementById('account-loading');
    const body = document.getElementById('account-body');
    const setLoadingText = (text, failed) => {
      loading.classList.toggle('failed', !!failed);
      const span = loading.querySelector('.loading-text');
      if (span) span.textContent = text;
      else loading.textContent = text;
    };
    try {
      const resp = await fetch('/api/auth/me');
      if (resp.status === 401) { location.href = '/login'; return; }
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || ('HTTP ' + resp.status));
      const u = data.user || {};
      const name = u.username || '—';
      document.getElementById('acc-username').textContent = name;
      document.getElementById('acc-hero-name').textContent = name;
      document.getElementById('acc-avatar').textContent = String(name).slice(0, 1) || '?';
      document.getElementById('acc-id').textContent = u.id ?? '—';
      document.getElementById('acc-created').textContent = this.fmt(u.created_at);
      document.getElementById('acc-last-login').textContent = this.fmt(u.last_login_at);
      document.getElementById('acc-sessions').textContent = `${u.session_count ?? 0} 个历史会话`;
      document.getElementById('acc-servers').textContent = `${u.server_count ?? 0} 台`;
      document.getElementById('acc-active').textContent = `${u.active_sessions ?? 1} 个（当前浏览器 + 其他已登录设备）`;
      loading.classList.add('hidden');
      body.classList.remove('hidden');
    } catch (e) {
      setLoadingText('加载失败: ' + e.message + '（可刷新页面重试）', true);
    }
  },

  async changePassword() {
    const form = document.getElementById('account-password-form');
    const resultEl = document.getElementById('account-password-result');
    const oldPw = form.elements.old_password.value;
    const newPw = form.elements.new_password.value;
    const newPw2 = form.elements.new_password2.value;

    resultEl.className = 'result-text';
    if (!oldPw || !newPw) { resultEl.className = 'result-text err'; resultEl.textContent = '请填写当前密码与新密码'; return; }
    if (newPw.length < 8) { resultEl.className = 'result-text err'; resultEl.textContent = '新密码至少 8 位'; return; }
    if (newPw !== newPw2) { resultEl.className = 'result-text err'; resultEl.textContent = '两次输入的新密码不一致'; return; }

    try {
      const resp = await fetch('/api/auth/password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ old_password: oldPw, new_password: newPw }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        resultEl.className = 'result-text err';
        resultEl.textContent = data.error || '修改失败';
        return;
      }
      // 服务端已注销全部会话并清了 Cookie；稍作停留提示后跳登录页
      resultEl.className = 'result-text ok';
      resultEl.textContent = '✓ 密码已修改，正在跳转登录页…';
      form.reset();
      setTimeout(() => { location.href = '/login'; }, 1200);
    } catch (e) {
      resultEl.className = 'result-text err';
      resultEl.textContent = '修改失败: ' + e.message;
    }
  },
};
