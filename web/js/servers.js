/* 服务器管理 */
const Servers = {
  init() {
    document.getElementById('btn-add-server').addEventListener('click', () => this.openModal());
    document.getElementById('btn-close-server-modal').addEventListener('click', () => this.closeModal());
    document.getElementById('server-modal').addEventListener('click', (e) => {
      if (e.target.id === 'server-modal') this.closeModal();
    });
    document.getElementById('auth-type').addEventListener('change', () => this.toggleAuth());
    document.getElementById('server-form').addEventListener('submit', (e) => {
      e.preventDefault();
      this.save();
    });
    document.getElementById('btn-test-server-form').addEventListener('click', () => this.testFromForm());
  },

  toggleAuth() {
    const type = document.getElementById('auth-type').value;
    document.getElementById('field-password').classList.toggle('hidden', type !== 'password');
    document.getElementById('field-key').classList.toggle('hidden', type !== 'key');
  },

  async render() {
    const servers = await App.loadServers();
    const grid = document.getElementById('server-list');
    grid.innerHTML = '';
    if (!servers.length) {
      grid.innerHTML = `<div style="color:#667085;font-size:13px">还没有服务器，点击右上角"添加服务器"开始。</div>`;
      return;
    }
    servers.forEach(s => {
      const card = document.createElement('div');
      card.className = 'server-card';
      card.innerHTML = `
        <div class="server-card-head">
          <span class="s-name"></span>
          <span class="s-badge"></span>
        </div>
        <div class="s-host"></div>
        <div class="s-note"></div>
        <div class="s-actions">
          <button class="btn btn-sm" data-act="test">测试连接</button>
          <button class="btn btn-sm" data-act="edit">编辑</button>
          <button class="btn btn-sm btn-ghost" data-act="hostkey" title="服务器重装系统后，删除旧的 SSH 主机指纹以便重新记录">重置指纹</button>
          <button class="btn btn-sm btn-ghost" data-act="del" style="color:#f85149">删除</button>
        </div>
      `;
      card.querySelector('.s-name').textContent = s.name || s.host;
      card.querySelector('.s-badge').textContent = s.auth_type === 'key' || s.has_key ? '密钥' : '密码';
      card.querySelector('.s-host').textContent = `${s.username}@${s.host}:${s.port || 22}`;
      card.querySelector('.s-note').textContent = s.note || '';

      card.querySelector('[data-act="test"]').addEventListener('click', (e) => this.test(s.id, e.target));
      card.querySelector('[data-act="edit"]').addEventListener('click', () => this.openModal(s));
      card.querySelector('[data-act="hostkey"]').addEventListener('click', () => this.resetHostKey(s));
      card.querySelector('[data-act="del"]').addEventListener('click', () => this.remove(s));

      grid.appendChild(card);
    });
  },

  openModal(server = null) {
    const modal = document.getElementById('server-modal');
    const form = document.getElementById('server-form');
    form.reset();
    document.getElementById('server-form-result').textContent = '';
    document.getElementById('server-modal-title').textContent = server ? '编辑服务器' : '添加服务器';

    if (server) {
      form.elements.id.value = server.id;
      form.elements.name.value = server.name || '';
      form.elements.host.value = server.host || '';
      form.elements.port.value = server.port || 22;
      form.elements.username.value = server.username || '';
      const authType = server.auth_type || (server.has_key ? 'key' : 'password');
      form.elements.auth_type.value = authType;
      form.elements.key_path.value = server.key_path || '';
      form.elements.note.value = server.note || '';
      if (server.has_password) form.elements.password.value = '********';
      if (server.has_key) form.elements.passphrase.value = '********';
    } else {
      form.elements.port.value = 22;
      form.elements.auth_type.value = 'password';
    }
    this.toggleAuth();
    modal.classList.remove('hidden');
    form.elements.name.focus();
  },

  closeModal() {
    document.getElementById('server-modal').classList.add('hidden');
  },

  collectForm() {
    const form = document.getElementById('server-form');
    const data = {
      name: form.elements.name.value.trim(),
      host: form.elements.host.value.trim(),
      port: parseInt(form.elements.port.value) || 22,
      username: form.elements.username.value.trim(),
      auth_type: form.elements.auth_type.value,
      note: form.elements.note.value.trim(),
    };
    const pwd = form.elements.password.value;
    if (pwd && pwd !== '********') data.password = pwd;

    if (data.auth_type === 'key') {
      const keyContent = form.elements.key_content.value.trim();
      const keyPath = form.elements.key_path.value.trim();
      if (keyContent) data.key_content = keyContent;
      if (keyPath) data.key_path = keyPath;
      const pass = form.elements.passphrase.value;
      if (pass && pass !== '********') data.passphrase = pass;
    }
    return data;
  },

  async save() {
    const data = this.collectForm();
    const resultEl = document.getElementById('server-form-result');
    if (!data.name || !data.host || !data.username) {
      resultEl.className = 'result-text err';
      resultEl.textContent = '请填写名称、主机和用户名';
      return;
    }
    if (data.auth_type === 'key' && !data.key_content && !data.key_path && !data.id) {
      resultEl.className = 'result-text err';
      resultEl.textContent = '请填写私钥内容或私钥路径';
      return;
    }
    const isEdit = !!data.id;
    const id = data.id;
    delete data.id;

    try {
      const url = isEdit ? `/api/servers/${id}` : '/api/servers';
      const resp = await fetch(url, {
        method: isEdit ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      });
      const json = await resp.json();
      if (!resp.ok) {
        resultEl.className = 'result-text err';
        resultEl.textContent = json.error || '保存失败';
        return;
      }
      this.closeModal();
      await this.render();
      await App.loadServers();
    } catch (e) {
      resultEl.className = 'result-text err';
      resultEl.textContent = '保存失败: ' + e.message;
    }
  },

  async testFromForm() {
    const data = this.collectForm();
    const resultEl = document.getElementById('server-form-result');

    if (data.id) {
      // 编辑现有服务器：直接测试
      resultEl.className = 'result-text';
      resultEl.textContent = '测试中…';
      try {
        const resp = await fetch(`/api/servers/${data.id}/test`, { method: 'POST' });
        const json = await resp.json();
        resultEl.className = 'result-text ' + (json.success ? 'ok' : 'err');
        resultEl.textContent = json.success ? '✓ 连接成功：' + (json.output || '').split('\n')[0] : '✗ ' + json.error;
      } catch (e) {
        resultEl.className = 'result-text err';
        resultEl.textContent = '测试失败: ' + e.message;
      }
      return;
    }

    // 新服务器：先保存再测试
    resultEl.className = 'result-text';
    resultEl.textContent = '保存并测试中…';
    try {
      const saveResp = await fetch('/api/servers', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      });
      const saved = await saveResp.json();
      if (!saveResp.ok) {
        resultEl.className = 'result-text err';
        resultEl.textContent = saved.error || '保存失败';
        return;
      }
      const newId = saved.server.id;
      const resp = await fetch(`/api/servers/${newId}/test`, { method: 'POST' });
      const json = await resp.json();
      resultEl.className = 'result-text ' + (json.success ? 'ok' : 'err');
      resultEl.textContent = json.success ? '✓ 连接成功，已保存' : '✗ ' + json.error + '（已保存配置）';
      await this.render();
      await App.loadServers();
      if (json.success) setTimeout(() => this.closeModal(), 1200);
    } catch (e) {
      resultEl.className = 'result-text err';
      resultEl.textContent = '测试失败: ' + e.message;
    }
  },

  async test(serverId, btn) {
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = '测试中…';
    try {
      const resp = await fetch(`/api/servers/${serverId}/test`, { method: 'POST' });
      const json = await resp.json();
      btn.textContent = json.success ? '✓ 连接正常' : '✗ 连接失败';
      btn.style.color = json.success ? '#3fb950' : '#f85149';
      if (!json.success) alert('连接失败：' + (json.error || '未知错误'));
    } catch (e) {
      btn.textContent = '✗ 失败';
      alert('测试失败: ' + e.message);
    } finally {
      setTimeout(() => { btn.textContent = old; btn.disabled = false; btn.style.color = ''; }, 2500);
    }
  },

  async resetHostKey(server) {
    const name = server.name || server.host;
    if (!confirm(`确定重置「${name}」的 SSH 主机指纹？\n\n仅在服务器确实重装过系统时重置；若你没有重装过，指纹变化可能是中间人攻击，请先核实网络环境。`)) return;
    try {
      const resp = await fetch(`/api/servers/${server.id}/hostkey`, { method: 'DELETE' });
      const json = await resp.json();
      alert(json.message || json.error || (json.success ? '已重置' : '操作失败'));
    } catch (e) {
      alert('操作失败: ' + e.message);
    }
  },

  async remove(server) {
    if (!confirm(`确定删除服务器「${server.name || server.host}」？`)) return;
    await fetch(`/api/servers/${server.id}`, { method: 'DELETE' });
    await this.render();
    await App.loadServers();
  },
};
