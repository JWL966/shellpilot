"""SSH 连接管理：paramiko 封装，连接复用、命令执行。"""
import threading
import time
from typing import Any, Optional

import paramiko

from config import get_server_raw


class SSHError(Exception):
    pass


class SSHManager:
    """按 server_id 缓存 SSH 连接，执行命令前探活，失败自动重连一次。"""

    def __init__(self):
        self._clients: dict[str, paramiko.SSHClient] = {}
        self._lock = threading.Lock()

    def _connect(self, server: dict[str, Any]) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs: dict[str, Any] = {
            "hostname": server["host"],
            "port": int(server.get("port", 22)),
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


ssh_manager = SSHManager()
