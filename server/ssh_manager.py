"""SSH 连接管理：paramiko 封装，连接复用、命令执行、主机指纹校验（TOFU）。"""
import os
import threading
import time
from typing import Any, Optional

import paramiko

import config
from session import get_server as get_server_raw  # 按 id 取凭据（server_id 全局唯一；归属校验在路由/Agent 层做）

class SSHError(Exception):
    pass


def _smart_decode(data: bytes) -> str:
    """智能解码：UTF-8 优先，失败依次尝试 GBK / Big5（Windows 中文控制台默认 GBK）。"""
    if not data:
        return ""
    for enc in ("utf-8", "gbk", "cp950"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


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
        # 本机模式：不经过 SSH，直接在本机用子进程执行
        server = get_server_raw(server_id)
        if server and server.get("conn_type") == "local":
            return self._exec_local(server, cmd, timeout)

        last_err: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                client = self._get_client(server_id)
                start = time.time()
                stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
                out = _smart_decode(stdout.read())
                err = _smart_decode(stderr.read())
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

    def _exec_local(self, server: dict[str, Any], cmd: str, timeout: int) -> dict[str, Any]:
        """本机模式执行：Windows 经 PowerShell，Linux 经 bash/sh。安全分级照常生效。"""
        import shutil
        import subprocess

        is_win = (str(server.get("os") or "").lower() == "windows")
        if is_win:
            pw = shutil.which("powershell") or shutil.which("pwsh")
            if pw:
                argv = [pw, "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-Command", cmd]
            elif os.name == "nt":
                argv = ["cmd.exe", "/d", "/s", "/c", cmd]
            else:
                raise SSHError("本机未找到 PowerShell，无法以 Windows 模式执行本机命令")
        else:
            argv = ["/bin/bash", "-lc", cmd] if os.path.exists("/bin/bash") else ["/bin/sh", "-c", cmd]

        start = time.time()
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=max(1, int(timeout)))
            out = _smart_decode(proc.stdout)
            err = _smart_decode(proc.stderr)
            code = proc.returncode
            if code != 0 and not err.strip() and not out.strip():
                err = f"命令退出码 {code}"
        except subprocess.TimeoutExpired as e:
            out = _smart_decode(e.stdout or b"")
            err = _smart_decode(e.stderr or b"")
            if not err.strip():
                err = f"命令超时（{timeout}s），已终止"
            code = 124
        except Exception as e:
            raise SSHError(f"本机命令执行失败: {e}") from e
        return {
            "exit_code": code,
            "stdout": out,
            "stderr": err,
            "duration_ms": int((time.time() - start) * 1000),
        }

    def test_connection(self, server_id: str) -> dict[str, Any]:
        """测试连接；SSH 目标若未标注系统类型，同时探测并返回识别到的 os。"""
        try:
            server = get_server_raw(server_id) or {}
            conn_local = server.get("conn_type") == "local"
            os_name = str(server.get("os") or "").strip().lower()
            if conn_local:
                # 本机模式：系统类型在保存时已按宿主机写死，直接用对应 shell 探活
                if os_name == "windows":
                    probe = "Write-Output ok; Write-Output $env:COMPUTERNAME"
                else:
                    probe = "echo ok && hostname && uname -a"
            elif os_name == "windows":
                probe = ('powershell -NoProfile -Command '
                         '"Write-Output ok; Write-Output $env:COMPUTERNAME"')
            elif os_name == "linux":
                probe = "echo ok && hostname && uname -a"
            else:
                # 未标注系统：Linux 会打印 Linux；Windows(cmd) 里 uname 不存在转而执行 ver
                probe = "uname -s || ver"

            result = self.exec_command(server_id, probe, timeout=15)
            out = result["stdout"]
            detected = None
            if not conn_local and not os_name:
                low = out.lower()
                if "microsoft windows" in low or "windows po" in low:
                    detected = "windows"
                elif "linux" in low or "darwin" in low:
                    detected = "linux"
            return {"success": True, "output": out, "os": os_name or detected}
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
