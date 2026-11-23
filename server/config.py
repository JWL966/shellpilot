"""配置管理：读写 config.json（LLM 设置 + 服务器列表 + 认证/SSH 安全选项）。"""
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import threading
import uuid
from typing import Any, Optional
from urllib.parse import urlsplit

import session as session_store

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
    # 登录账号存于 PostgreSQL 的 users 表（见 server/session.py）；此处仅控制
    # 是否开放注册。旧版 auth.username / auth.password_hash 首次启动时自动迁移。
    "auth": {
        "allow_registration": True,
    },
    # SSH 主机指纹校验策略：
    #   tofu   = 首次连接记录指纹，之后指纹变化即拒绝（推荐）
    #   strict = 只连已知指纹的主机
    #   auto   = 不校验（不安全，保留兼容旧行为）
    "ssh": {
        "host_key_policy": "tofu",
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
    # 旧版访问令牌字段 → 已改为登录账号体系
    auth_sec = data.get("auth")
    if isinstance(auth_sec, dict):
        auth_sec.pop("token", None)
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


# ---------- 登录账号 / SSH 安全选项 ----------

_PBKDF2_ITERATIONS = 200_000


def _hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def check_password(stored: str, password: str) -> bool:
    """用 PBKDF2 哈希校验明文密码（常数时间比较）。"""
    if not stored:
        return False
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def admin_username() -> str:
    """管理员用户名：兼容读取旧版 config 字段，缺省 admin。"""
    with _lock:
        name = str((_load().get("auth") or {}).get("username") or "").strip()
    return name or "admin"


def registration_allowed() -> bool:
    with _lock:
        val = (_load().get("auth") or {}).get("allow_registration")
    return val if isinstance(val, bool) else True


def _cleanup_legacy_auth_fields() -> None:
    """旧版单用户字段（auth.username / auth.password_hash）已迁移进 users 表后清除。"""
    with _lock:
        data = _load()
        auth = data.get("auth") or {}
        if "username" in auth or "password_hash" in auth:
            auth.pop("username", None)
            auth.pop("password_hash", None)
            data["auth"] = auth
            _save(data)


def ensure_admin_user() -> tuple[str, str]:
    """users 表为空时创建管理员账号。

    初始密码优先级：SHELLPILOT_PASSWORD 环境变量 > 旧版 config 的
    password_hash（无缝迁移，明文不可知）> 随机生成（启动日志打印一次）。
    返回 (明文密码或空串, "created" | "migrated" | "exists")。
    """
    env_pw = os.environ.get("SHELLPILOT_PASSWORD", "").strip()
    if session_store.count_users() == 0:
        username = admin_username()
        with _lock:
            legacy_hash = str(
                (_load().get("auth") or {}).get("password_hash") or "").strip()
        if not env_pw and legacy_hash:
            if session_store.create_user(username, legacy_hash) is not None:
                _cleanup_legacy_auth_fields()
                return "", "migrated"
        pw = env_pw or secrets.token_urlsafe(12)
        if session_store.create_user(username, _hash_password(pw)) is not None:
            _cleanup_legacy_auth_fields()
            return pw, "created"
    else:
        _cleanup_legacy_auth_fields()
    return "", "exists"


def verify_credentials(username: str, password: str) -> Optional[int]:
    """校验登录凭据（查 users 表），成功返回用户 id，失败返回 None。"""
    if not username or not password:
        return None
    user = session_store.get_user_by_name(str(username).strip())
    if not user or not check_password(str(user["password_hash"]), password):
        return None
    return int(user["id"])


def change_password(user_id: int, old: str, new: str) -> None:
    """修改用户密码（需验证原密码）。失败抛 ValueError。"""
    row = session_store.get_user_by_id(user_id)
    if not row or not check_password(str(row["password_hash"]), str(old or "")):
        raise ValueError("原密码不正确")
    new = str(new or "")
    if not (8 <= len(new) <= 128):
        raise ValueError("新密码长度需在 8~128 位之间")
    session_store.update_user_password(user_id, _hash_password(new))


def reset_password() -> str:
    """命令行重置管理员密码：不存在则创建，存在则覆盖。返回明文（打印给用户）。"""
    pw = secrets.token_urlsafe(12)
    username = admin_username()
    user = session_store.get_user_by_name(username)
    if user:
        session_store.update_user_password(int(user["id"]), _hash_password(pw))
    else:
        session_store.create_user(username, _hash_password(pw))
    return pw


def get_host_key_policy() -> str:
    """SSH 主机指纹校验策略：tofu（默认）/ strict / auto。环境变量可覆盖。"""
    raw = os.environ.get("SHELLPILOT_HOST_KEY_POLICY", "").strip().lower()
    if not raw:
        with _lock:
            raw = str((_load().get("ssh") or {}).get("host_key_policy") or "tofu").strip().lower()
    return raw if raw in ("tofu", "strict", "auto") else "tofu"


# ---------- LLM 配置校验 / 安全告警 ----------

def _is_loopback_host(hostname: str) -> bool:
    n = (hostname or "").lower().strip("[]")
    if n == "localhost":
        return True
    try:
        return ipaddress.ip_address(n).is_loopback
    except ValueError:
        return False


def base_url_problem(url: Any) -> str:
    """校验 LLM base_url 的硬错误（保存时拒绝），返回问题描述；合法返回空串。"""
    url = str(url or "").strip()
    if not url:
        return ""
    try:
        u = urlsplit(url)
    except ValueError:
        return "base_url 不是合法 URL"
    if u.scheme not in ("http", "https"):
        return "base_url 必须以 http:// 或 https:// 开头"
    if not u.hostname:
        return "base_url 缺少主机名"
    if u.username or u.password:
        return "base_url 不能包含用户名/密码（防止密钥拼进 URL）"
    return ""


def base_url_caution(url: Any) -> str:
    """非硬错误但值得提示的风险（保存成功但附告警）。"""
    url = str(url or "").strip()
    if not url:
        return ""
    try:
        u = urlsplit(url)
    except ValueError:
        return ""
    if u.scheme == "http" and u.hostname and not _is_loopback_host(u.hostname):
        return "base_url 使用明文 HTTP，API Key 在网络路径上可见（建议改用 HTTPS 或自建隧道）"
    return ""


def validate_llm_updates(updates: dict[str, Any]) -> list[str]:
    """PUT /api/config/llm 的入口校验，返回错误列表（空列表 = 通过）。"""
    errs: list[str] = []
    if "base_url" in updates:
        err = base_url_problem(updates.get("base_url"))
        if err:
            errs.append(err)
    if "model" in updates and len(str(updates.get("model") or "")) > 200:
        errs.append("model 名称过长")
    if "timeout" in updates:
        try:
            t = int(updates["timeout"])
            if not (10 <= t <= 600):
                errs.append("timeout 需在 10~600 秒之间")
        except (TypeError, ValueError):
            errs.append("timeout 必须是数字")
    if "history_budget_chars" in updates:
        try:
            n = int(updates["history_budget_chars"])
            if n < 0:
                errs.append("history_budget_chars 不能为负")
        except (TypeError, ValueError):
            errs.append("history_budget_chars 必须是数字")
    if "models" in updates and not isinstance(updates.get("models"), list):
        errs.append("models 必须是列表")
    return errs


def llm_warnings() -> list[str]:
    """配置健康告警：不打断使用，但提示有泄密/不可用风险。"""
    warns: list[str] = []
    with _lock:
        cfg = _load()["llm"]
    if not cfg.get("api_key"):
        warns.append("尚未配置 API Key，对话不可用")
    err = base_url_problem(cfg.get("base_url"))
    if err:
        warns.append(err)
    else:
        caution = base_url_caution(cfg.get("base_url"))
        if caution:
            warns.append(caution)
    if get_host_key_policy() == "auto":
        warns.append("SSH 主机指纹不校验（host_key_policy=auto），连接可能被中间人窃取密码")
    return warns


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
