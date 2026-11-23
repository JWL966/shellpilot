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
    # llm / servers 两段已改为按用户存数据库（server/session.py 的 user_llm、servers 表）。
    # config.json 里的这两段只在升级时作为一次性迁移来源，迁移完成后被清空；
    # 此处保留默认值用于新用户的初始 LLM 配置模板。
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "models": [],       # 可选模型列表；为空时下拉框只显示默认 model
        "timeout": 120,
        # 回灌模型的历史字符上限；0 = 不裁剪（全量发送）。大上下文模型保持 0 即可。
        "history_budget_chars": 0,
    },
    # 登录账号存于 PostgreSQL 的 users 表；此处仅控制是否开放注册。
    # 旧版 auth.username / auth.password_hash 首次启动时自动迁移。
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


def admin_user_id() -> Optional[int]:
    """管理员账号在 users 表里的 id；不存在返回 None。"""
    user = session_store.get_user_by_name(admin_username())
    return int(user["id"]) if user else None


def migrate_legacy_data() -> list[str]:
    """旧版单用户数据 → 按用户隔离的数据库表（管理员名下）。启动时调用一次。

    - config.json 的 servers 列表 → servers 表（仅当服务器表为空时）
    - config.json 的 llm 配置 → user_llm 表（仅当管理员还没有 LLM 配置时）
    - user_id 为空的历史会话 → 划归管理员
    迁移成功后清除 config.json 里的对应内容（敏感信息不再落盘文件）。
    """
    msgs: list[str] = []
    uid = admin_user_id()
    if uid is None:
        return msgs

    n = session_store.backfill_sessions_owner(uid)
    if n:
        msgs.append(f"已把 {n} 条历史会话划归管理员账号")

    if session_store.count_servers() == 0:
        with _lock:
            legacy = list(_load().get("servers") or [])
        if legacy:
            ok = 0
            for s in legacy:
                try:
                    session_store.create_server(uid, s)
                    ok += 1
                except Exception:
                    continue  # 单条失败（如 id 冲突）不阻断其余迁移
            if ok:
                with _lock:
                    data = _load()
                    data["servers"] = []
                    _save(data)
                msgs.append(f"已把旧版 config.json 里的 {ok} 台服务器迁入数据库（管理员名下），config.json 中已清除")

    if session_store.get_user_llm(uid) is None:
        with _lock:
            llm = dict(_load().get("llm") or {})
        if llm.get("api_key") or llm.get("base_url") or llm.get("model"):
            session_store.upsert_user_llm(uid, llm)
            with _lock:
                data = _load()
                data["llm"] = json.loads(json.dumps(DEFAULT_CONFIG["llm"]))
                _save(data)
            msgs.append("已把旧版 config.json 里的 LLM 配置迁入数据库（管理员名下），config.json 中已清除")
    return msgs


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


def llm_warnings(cfg: Optional[dict[str, Any]] = None) -> list[str]:
    """配置健康告警：不打断使用，但提示有泄密/不可用风险。
    cfg 缺省时按管理员的配置检查（用于启动日志）。"""
    if cfg is None:
        uid = admin_user_id()
        cfg = get_llm_config_raw(uid) if uid is not None else json.loads(json.dumps(DEFAULT_CONFIG["llm"]))
    warns: list[str] = []
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


# ---------- LLM 配置（按用户隔离，存 user_llm 表）----------

def get_llm_config_raw(user_id: int) -> dict[str, Any]:
    """某用户的完整 LLM 配置（含明文 api_key，仅服务端内部使用）。"""
    cfg = session_store.get_user_llm(user_id)
    if cfg is None:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG["llm"]))
        cfg["models"] = list(cfg.get("models") or [])
    return cfg


def get_llm_config(user_id: int) -> dict[str, Any]:
    """某用户的 LLM 配置（api_key 脱敏，供前端展示）。"""
    raw = get_llm_config_raw(user_id)
    cfg = raw.copy()
    cfg["api_key"] = "*" * 8 if raw.get("api_key") else ""
    cfg["has_api_key"] = bool(raw.get("api_key"))
    models = list(raw.get("models") or [])
    default = raw.get("model")
    if default and default not in models:
        models.insert(0, default)
    cfg["models"] = models
    return cfg


def resolve_model(user_id: int, selected: Optional[str]) -> str:
    """根据用户可选模型返回可用模型；非法/空选择回退到默认模型。"""
    cfg = get_llm_config_raw(user_id)
    default = str(cfg.get("model") or "").strip()
    models = [str(m).strip() for m in (cfg.get("models") or []) if str(m).strip()]
    if default and default not in models:
        models.insert(0, default)
    if selected:
        selected = str(selected).strip()
        if selected in models:
            return selected
    return default or (models[0] if models else "")


def update_llm_config(user_id: int, updates: dict[str, Any]) -> dict[str, Any]:
    cfg = get_llm_config_raw(user_id)
    for k, v in updates.items():
        # api_key 三态：null=清除；空串/全星号=保持不变；其他=覆盖
        if k == "api_key":
            if v is None:
                cfg["api_key"] = ""
            elif not v or set(v) == {"*"}:
                pass
            else:
                cfg["api_key"] = v
            continue
        if k in ("base_url", "model"):
            cfg[k] = v
        elif k == "models":
            if isinstance(v, list):
                seen = set()
                clean = []
                for m in v:
                    name = str(m).strip()
                    if name and name not in seen:
                        seen.add(name)
                        clean.append(name)
                cfg["models"] = clean
        elif k == "timeout":
            try:
                cfg[k] = max(10, min(int(v), 600))
            except (TypeError, ValueError):
                continue
        elif k == "history_budget_chars":
            # 0 或负数 = 不裁剪（全量历史），适合大上下文模型
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            cfg[k] = 0 if n <= 0 else min(n, 20_000_000)
    session_store.upsert_user_llm(user_id, cfg)
    return get_llm_config(user_id)


# ---------- 服务器管理（按用户隔离，存 servers 表）----------

def _server_view(raw: dict[str, Any]) -> dict[str, Any]:
    """对前端脱敏：剔除明文凭据，附 has_password / has_key 标志。"""
    view = {k: v for k, v in raw.items()
            if k not in ("password", "passphrase", "key_content", "user_id")}
    view["has_password"] = bool(raw.get("password"))
    view["has_key"] = bool(raw.get("key_content") or raw.get("key_path"))
    return view


def list_servers(user_id: int) -> list[dict[str, Any]]:
    return [_server_view(s) for s in session_store.list_servers(user_id)]


def get_server_raw(user_id: int, server_id: str) -> Optional[dict[str, Any]]:
    """按归属取服务器完整配置（含明文凭据）；非本人或不存在返回 None。"""
    return session_store.get_server(server_id, user_id)


def add_server(user_id: int, server: dict[str, Any]) -> dict[str, Any]:
    server = dict(server)
    server["id"] = server.get("id") or uuid.uuid4().hex[:12]
    created = session_store.create_server(user_id, server)
    return _server_view(created)


def update_server(user_id: int, server_id: str, updates: dict[str, Any]) -> Optional[dict[str, Any]]:
    updates = dict(updates)
    updates.pop("id", None)
    # password 为全星号 = 保持原密码不变
    if updates.get("password") and set(str(updates["password"])) == {"*"}:
        updates.pop("password", None)
    updated = session_store.update_server(user_id, server_id, updates)
    return _server_view(updated) if updated else None


def delete_server(user_id: int, server_id: str) -> bool:
    return session_store.delete_server(server_id, user_id)
