/* LLM 设置 */
const Settings = {
  init() {
    document.getElementById('llm-form').addEventListener('submit', (e) => {
      e.preventDefault();
      this.saveLLM();
    });
    document.getElementById('btn-test-llm').addEventListener('click', () => this.testLLM());
  },

  renderWarnings(warnings) {
    const box = document.getElementById('llm-warnings');
    const list = Array.isArray(warnings) ? warnings.filter(Boolean) : [];
    box.classList.toggle('hidden', !list.length);
    box.innerHTML = '';
    list.forEach(w => {
      const div = document.createElement('div');
      div.textContent = '⚠ ' + w;
      box.appendChild(div);
    });
  },

  async load() {
    try {
      const resp = await fetch('/api/config/llm');
      const data = await resp.json();
      const llm = data.llm || {};
      const form = document.getElementById('llm-form');
      form.elements.base_url.value = llm.base_url || '';
      form.elements.api_key.value = llm.has_api_key ? '********' : '';
      form.elements.model.value = llm.model || '';
      const models = Array.isArray(llm.models) ? llm.models : [];
      form.elements.models.value = models.join('\n');
      form.elements.timeout.value = llm.timeout ?? 120;
      form.elements.history_budget_chars.value = llm.history_budget_chars ?? 0;
      this.renderWarnings(data.warnings);
    } catch (e) {
      console.error(e);
    }
  },

  async saveLLM() {
    const form = document.getElementById('llm-form');
    const resultEl = document.getElementById('llm-result');
    const models = form.elements.models.value.split('\n').map(m => m.trim()).filter(Boolean);
    const payload = {
      base_url: form.elements.base_url.value.trim(),
      api_key: form.elements.api_key.value.trim(),
      model: form.elements.model.value.trim(),
      models,
      timeout: parseInt(form.elements.timeout.value) || 120,
      history_budget_chars: parseInt(form.elements.history_budget_chars.value) || 0,
    };
    try {
      const resp = await fetch('/api/config/llm', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await resp.json();
      if (!resp.ok) {
        resultEl.className = 'result-text err';
        resultEl.textContent = data.error || '保存失败';
        return;
      }
      this.renderWarnings(data.warnings);
      resultEl.className = 'result-text ok';
      resultEl.textContent = '✓ 已保存';
      setTimeout(() => { resultEl.textContent = ''; }, 3000);
      await App.loadModels(); // 保存后立即刷新对话页模型下拉框
    } catch (e) {
      resultEl.className = 'result-text err';
      resultEl.textContent = '保存失败: ' + e.message;
    }
  },

  async testLLM() {
    const resultEl = document.getElementById('llm-result');
    const form = document.getElementById('llm-form');
    resultEl.className = 'result-text';
    resultEl.textContent = '测试中…';
    try {
      const resp = await fetch('/api/config/llm/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: form.elements.model.value.trim() || null }),
      });
      const data = await resp.json();
      resultEl.className = 'result-text ' + (data.success ? 'ok' : 'err');
      resultEl.textContent = data.success ? '✓ LLM 连接正常' : '✗ ' + (data.error || '连接失败');
    } catch (e) {
      resultEl.className = 'result-text err';
      resultEl.textContent = '测试失败: ' + e.message;
    }
  },
};
