"""SSH 连接管理：paramiko 封装，连接复用、命令执行、主机指纹校验（TOFU）。"""
import os
import threading
import time
from typing import Any, Optional

import paramiko

import config
from config import get_server_raw

class SSHError(Exception):
    pass


# TOFU 指纹库路径：环境变量 SHELLPILOT_KNOWN_HOSTS 可覆盖，默认 ~/.shellpilot/known_hosts
KNOWN_HOSTS_PATH = os.environ.get(
    "SHELLPILOT_KNOWN_HOSTS",
    os.path.join(os.path.expanduser("~"), ".shellpilot", "known_hosts"),
)

_kh_lock = threading.Lock()  # 指纹库专用锁（与连接池锁分开，避免嵌套持锁）


def _hostkey_entry(host: str, port: int) -> str:
    """paramiko 的 known_hosts 条目名：22 端口用裸主机名，其余用 [host]:port。"""
    return host if int(port or 22) == 22 else f"[{host}]:{int(port)}"


class _TofuPolicy(paramiko.MissingHostKeyPolicy):
    """首次连接记录主机指纹到本地指纹库；之后由 paramiko 对照校验，
    指纹不一致会在 connect 阶段抛 BadHostKeyException。"""

    def __init__(self, path: str, port: int):
        self._path = path
        self._port = int(port or 22)

    def missing_host_key(self, client, hostname, key):
        entry = _hostkey_entry(hostname, self._port)
        with _kh_lock:
            keys = paramiko.HostKeys()
            if os.path.exists(self._path):
                try:
                    keys.load(self._path)
                except Exception:
                    pass  # 指纹库损坏时按空库处理，重新记录
            keys.add(entry, key.get_name(), key)
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            keys.save(self._path)


class SSHManager:
    """按 server_id 缓存 SSH 连接，执行命令前探活，失败自动重连一次。"""

    def __init__(self):
        self._clients: dict[str, paramiko.SSHClient] = {}
        self._lock = threading.Lock()

    def _connect(self, server: dict[str, Any]) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        port = int(server.get("port", 22))
        # 预加载本地指纹库（TOFU 的 O：已记录的指纹在这里比对）
        if os.path.exists(KNOWN_HOSTS_PATH):
            try:
                client.load_host_keys(KNOWN_HOSTS_PATH)
            except Exception:
                pass
        policy = config.get_host_key_policy()
        if policy == "auto":
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        elif policy == "strict":
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:  # tofu（默认）
            client.set_missing_host_key_policy(_TofuPolicy(KNOWN_HOSTS_PATH, port))
        kwargs: dict[str, Any] = {
            "hostname": server["host"],
            "port": port,
            "username": server["username"],
            "timeout": 10,
        }
        if server.get("password"):
            kwargs["password"] = server["password"]
        key_content = server.get("key_content")
        key_path = server.get("key_path")
        passphrase = server.get("passphrase") or None
        try:
            if key_content:
                import io
                key_file = io.StringIO(key_content)
                pkey = self._load_pkey(key_file, passphrase)
                kwargs["pkey"] = pkey
            elif key_path:
                pkey = self._load_pkey(open(key_path, "r", encoding="utf-8"), passphrase)
                kwargs["pkey"] = pkey
            client.connect(**kwargs)
        except paramiko.BadHostKeyException as e:
            raise SSHError(
                f"主机 {e.hostname} 的指纹与首次记录不一致，连接已拒绝（可能被中间人劫持，"
                f"或对方重装过系统）。确认无误后可在「服务器管理」里重置该主机指纹，"
                f"或删除文件 {KNOWN_HOSTS_PATH} 中对应条目后重试。"
            ) from e
        except Exception as e:
            raise SSHError(f"SSH 连接失败: {e}") from e
        return client

    @staticmethod
    def _load_pkey(key_file, passphrase):
        import io
        data = key_file.read() if hasattr(key_file, "read") else key_file
        for key_cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
            try:
                return key_cls.from_private_key(io.StringIO(data), password=passphrase)
            except Exception:
                continue
        raise SSHError("无法解析私钥（支持 Ed25519/ECDSA/RSA，需 PEM/OpenSSH 格式）")

    def _get_client(self, server_id: str) -> paramiko.SSHClient:
        with self._lock:
            client = self._clients.get(server_id)
            if client:
                try:
                    transport = client.get_transport()
                    if transport and transport.is_active():
                        # 探活
                        transport.send_ignore()
                        return client
                except Exception:
                    pass
                self._clients.pop(server_id, None)

            server = get_server_raw(server_id)
            if not server:
                raise SSHError(f"服务器不存在: {server_id}")
            client = self._connect(server)
            self._clients[server_id] = client
            return client

    def exec_command(self, server_id: str, cmd: str, timeout: int = 30) -> dict[str, Any]:
        """执行命令，返回 {exit_code, stdout, stderr, duration_ms}。失败自动重连一次。"""
        last_err: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                client = self._get_client(server_id)
                start = time.time()
                stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
                out = stdout.read().decode("utf-8", errors="replace")
                err = stderr.read().decode("utf-8", errors="replace")
                code = stdout.channel.recv_exit_status()
                duration = int((time.time() - start) * 1000)
                return {
                    "exit_code": code,
                    "stdout": out,
                    "stderr": err,
                    "duration_ms": duration,
                }
            except Exception as e:
                last_err = e
                with self._lock:
                    self._clients.pop(server_id, None)
                if attempt == 2:
                    break
        raise SSHError(f"命令执行失败: {last_err}")

    def test_connection(self, server_id: str) -> dict[str, Any]:
        try:
            result = self.exec_command(server_id, "echo ok && hostname && uname -a", timeout=10)
            return {"success": True, "output": result["stdout"]}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def close(self, server_id: str) -> None:
        with self._lock:
            client = self._clients.pop(server_id, None)
        if client:
            try:
                client.close()
            except Exception:
                pass

    def close_all(self) -> None:
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            try:
                client.close()
            except Exception:
                pass

    def forget_host_key(self, host: str, port: int = 22) -> bool:
        """删除某主机的已记录指纹（对方重装系统后用户主动重置用）。"""
        if not os.path.exists(KNOWN_HOSTS_PATH):
            return False
        entry = _hostkey_entry(host, port)
        with _kh_lock:
            keys = paramiko.HostKeys()
            try:
                keys.load(KNOWN_HOSTS_PATH)
            except Exception:
                return False
            if keys.lookup(entry) is None:
                return False
            del keys[entry]
            keys.save(KNOWN_HOSTS_PATH)
        return True


ssh_manager = SSHManager()
