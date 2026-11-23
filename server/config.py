"""配置管理：读写 config.json（LLM 设置 + 服务器列表）。"""
import json
import os
import threading
import uuid
from typing import Any, Optional

CONFIG_PATH = os.environ.get(
    "SHELLPILOT_CONFIG",
    os.environ.get(  # 兼容旧变量名
        "YCAI_CONFIG",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"),
    ),
)

_lock = threading.Lock()

DEFAULT_CONFIG: dict[str, Any] = {
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "models": [],       # 可选模型列表；为空时下拉框只显示默认 model
        "timeout": 120,
        # 回灌模型的历史字符上限；0 = 不裁剪（全量发送）。大上下文模型保持 0 即可。
        "history_budget_chars": 0,
    },
    "servers": [],
}

# LLM 配置允许的字段（其余一律丢弃，避免遗留旧字段）
LLM_FIELDS = ("base_url", "api_key", "model", "models", "timeout", "history_budget_chars")


def _load() -> dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    # 合并默认值（向后兼容新增字段）
    for key, val in DEFAULT_CONFIG.items():
        if key not in data:
            data[key] = json.loads(json.dumps(val))
        elif isinstance(val, dict):
            for k2, v2 in val.items():
                data[key].setdefault(k2, v2)
    # 清理旧版本遗留字段（agent 段、temperature、max_tokens 等）
    data.pop("agent", None)
    data["llm"] = {k: v for k, v in data.get("llm", {}).items() if k in LLM_FIELDS}
    for k, v in DEFAULT_CONFIG["llm"].items():
        data["llm"].setdefault(k, v)
    models = data["llm"].get("models")
    if not isinstance(models, list):
        data["llm"]["models"] = []
    else:
        # 去重、去空、保持顺序
        seen = set()
        clean = []
        for m in models:
            name = str(m).strip()
            if name and name not in seen:
                seen.add(name)
                clean.append(name)
        data["llm"]["models"] = clean
    return data


def _save(data: dict[str, Any]) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


# ---------- LLM 配置 ----------

def get_llm_config() -> dict[str, Any]:
    with _lock:
        raw = _load()["llm"]
        cfg = raw.copy()
    cfg["api_key"] = "*" * 8 if raw.get("api_key") else ""
    cfg["has_api_key"] = bool(raw.get("api_key"))
    models = list(raw.get("models") or [])
    default = raw.get("model")
    if default and default not in models:
        models.insert(0, default)
    cfg["models"] = models
    return cfg


def get_llm_config_raw() -> dict[str, Any]:
    with _lock:
        return _load()["llm"].copy()


def resolve_model(selected: Optional[str]) -> str:
    """根据前端选择返回可用模型；非法/空选择回退到默认模型。"""
    with _lock:
        cfg = _load()["llm"]
    default = str(cfg.get("model") or "").strip()
    models = [str(m).strip() for m in (cfg.get("models") or []) if str(m).strip()]
    if default and default not in models:
        models.insert(0, default)
    if selected:
        selected = str(selected).strip()
        if selected in models:
            return selected
    return default or (models[0] if models else "")


def update_llm_config(updates: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        data = _load()
        for k, v in updates.items():
            # api_key 三态：null=清除；空串/全星号=保持不变；其他=覆盖
            if k == "api_key":
                if v is None:
                    data["llm"]["api_key"] = ""
                elif not v or set(v) == {"*"}:
                    pass
                else:
                    data["llm"]["api_key"] = v
                continue
            if k in ("base_url", "model"):
                data["llm"][k] = v
            elif k == "models":
                if isinstance(v, list):
                    seen = set()
                    clean = []
                    for m in v:
                        name = str(m).strip()
                        if name and name not in seen:
                            seen.add(name)
                            clean.append(name)
                    data["llm"]["models"] = clean
            elif k == "timeout":
                try:
                    data["llm"][k] = max(10, min(int(v), 600))
                except (TypeError, ValueError):
                    continue
            elif k == "history_budget_chars":
                # 0 或负数 = 不裁剪（全量历史），适合大上下文模型
                try:
                    n = int(v)
                except (TypeError, ValueError):
                    continue
                data["llm"][k] = 0 if n <= 0 else min(n, 20_000_000)
        _save(data)
    return get_llm_config()


# ---------- 服务器管理 ----------

def list_servers() -> list[dict[str, Any]]:
    with _lock:
        servers = _load()["servers"]
    out = []
    for s in servers:
        item = {k: v for k, v in s.items() if k not in ("password", "passphrase", "key_content")}
        item["has_password"] = bool(s.get("password"))
        item["has_key"] = bool(s.get("key_content") or s.get("key_path"))
        out.append(item)
    return out


def get_server_raw(server_id: str) -> Optional[dict[str, Any]]:
    with _lock:
        for s in _load()["servers"]:
            if s["id"] == server_id:
                return json.loads(json.dumps(s))
    return None


def add_server(server: dict[str, Any]) -> dict[str, Any]:
    server = dict(server)
    server["id"] = server.get("id") or uuid.uuid4().hex[:12]
    with _lock:
        data = _load()
        data["servers"].append(server)
        _save(data)
    return {k: v for k, v in server.items() if k not in ("password", "passphrase", "key_content")} | {
        "has_password": bool(server.get("password")),
        "has_key": bool(server.get("key_content") or server.get("key_path")),
    }


def update_server(server_id: str, updates: dict[str, Any]) -> Optional[dict[str, Any]]:
    with _lock:
        data = _load()
        for s in data["servers"]:
            if s["id"] == server_id:
                for k, v in updates.items():
                    if k == "password" and v and set(v) == {"*"}:
                        continue
                    if k == "id":
                        continue
                    s[k] = v
                _save(data)
                item = {k2: v2 for k2, v2 in s.items() if k2 not in ("password", "passphrase", "key_content")}
                item["has_password"] = bool(s.get("password"))
                item["has_key"] = bool(s.get("key_content") or s.get("key_path"))
                return item
    return None


def delete_server(server_id: str) -> bool:
    with _lock:
        data = _load()
        before = len(data["servers"])
        data["servers"] = [s for s in data["servers"] if s["id"] != server_id]
        _save(data)
        return len(data["servers"]) < before
