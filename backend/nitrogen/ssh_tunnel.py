"""
可选：启动 SSH 本地端口转发，把本机 NitroGen FastAPI 地址映射到远端。

凭据：NITROGEN_SSH_PASSWORD（.env）或 NITROGEN_SSH_KEY；有密码时用 paramiko（Windows 友好）。
"""

from __future__ import annotations

import atexit
import logging
import select
import socket
import socketserver
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_tunnel_proc: Optional[subprocess.Popen] = None
_paramiko_tunnel: Optional["_ParamikoTunnel"] = None
_we_started_tunnel = False


@dataclass
class SshTunnelConfig:
    enabled: bool = False
    host: str = "connect.bjb1.seetacloud.com"
    port: int = 18037
    user: str = "root"
    password: str = ""
    local_port: int = 8000
    remote_port: int = 8000
    identity_file: str = ""


class _ParamikoTunnel:
    """paramiko 本地端口转发，支持密码登录。"""

    def __init__(self, cfg: SshTunnelConfig):
        self._cfg = cfg
        self._client = None
        self._server: Optional[socketserver.ThreadingTCPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        import paramiko

        cfg = self._cfg
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kw: dict = {
            "hostname": cfg.host,
            "port": cfg.port,
            "username": cfg.user,
            "timeout": 15.0,
            "allow_agent": not bool(cfg.password),
            "look_for_keys": not bool(cfg.password),
        }
        if cfg.password:
            connect_kw["password"] = cfg.password
        if cfg.identity_file:
            connect_kw["key_filename"] = cfg.identity_file
        client.connect(**connect_kw)
        transport = client.get_transport()
        if transport is None:
            raise RuntimeError("SSH transport unavailable")
        transport.set_keepalive(30)

        remote_host = "127.0.0.1"
        remote_port = cfg.remote_port
        local_port = cfg.local_port

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    chan = transport.open_channel(
                        "direct-tcpip",
                        (remote_host, remote_port),
                        self.request.getpeername(),
                    )
                except Exception:
                    return
                if chan is None:
                    return
                try:
                    while True:
                        r, _, _ = select.select([self.request, chan], [], [], 1.0)
                        if self.request in r:
                            data = self.request.recv(1024)
                            if not data:
                                break
                            chan.sendall(data)
                        if chan in r:
                            data = chan.recv(1024)
                            if not data:
                                break
                            self.request.sendall(data)
                finally:
                    chan.close()
                    self.request.close()

        class _Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._server = _Server(("127.0.0.1", local_port), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="ssh-tunnel-paramiko",
        )
        self._thread.start()
        self._client = client

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._client is not None:
            self._client.close()
            self._client = None


def local_port_from_url(fast_api_url: str) -> int:
    parsed = urlparse(fast_api_url)
    if parsed.port:
        return parsed.port
    if parsed.scheme == "https":
        return 443
    return 80


def port_is_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(host: str, port: int, total_timeout: float = 15.0) -> bool:
    deadline = time.time() + total_timeout
    while time.time() < deadline:
        if port_is_open(host, port):
            return True
        time.sleep(0.3)
    return False


def stop_ssh_tunnel() -> None:
    global _tunnel_proc, _paramiko_tunnel, _we_started_tunnel
    if _paramiko_tunnel is not None:
        _paramiko_tunnel.stop()
        logger.info("SSH tunnel (paramiko) closed")
        _paramiko_tunnel = None
    if _tunnel_proc is not None and _tunnel_proc.poll() is None:
        _tunnel_proc.terminate()
        try:
            _tunnel_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _tunnel_proc.kill()
        logger.info("SSH tunnel (ssh) closed")
    _tunnel_proc = None
    _we_started_tunnel = False


def _start_subprocess_tunnel(cfg: SshTunnelConfig) -> subprocess.Popen:
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-p", str(cfg.port),
        "-N",
        "-L", f"{cfg.local_port}:localhost:{cfg.remote_port}",
    ]
    if cfg.identity_file:
        cmd[1:1] = ["-i", cfg.identity_file]
    cmd.append(f"{cfg.user}@{cfg.host}")
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def start_ssh_tunnel(cfg: SshTunnelConfig) -> bool:
    global _tunnel_proc, _paramiko_tunnel, _we_started_tunnel

    if not cfg.enabled:
        return False

    if port_is_open("127.0.0.1", cfg.local_port):
        logger.info(
            "SSH tunnel: localhost:%s already open — reusing",
            cfg.local_port,
        )
        return True

    logger.info(
        "SSH tunnel: launching %s@%s:%s → localhost:%s",
        cfg.user, cfg.host, cfg.port, cfg.local_port,
    )

    if cfg.password:
        tunnel = _ParamikoTunnel(cfg)
        tunnel.start()
        if not wait_for_port("127.0.0.1", cfg.local_port, total_timeout=15.0):
            tunnel.stop()
            raise RuntimeError(
                f"SSH tunnel (password) did not open localhost:{cfg.local_port} within 15s."
            )
        _paramiko_tunnel = tunnel
    else:
        proc = _start_subprocess_tunnel(cfg)
        if not wait_for_port("127.0.0.1", cfg.local_port, total_timeout=15.0):
            proc.terminate()
            raise RuntimeError(
                f"SSH tunnel did not open localhost:{cfg.local_port} within 15s. "
                f"Set NITROGEN_SSH_PASSWORD in .env or configure SSH keys."
            )
        _tunnel_proc = proc

    _we_started_tunnel = True
    atexit.register(stop_ssh_tunnel)
    logger.info("SSH tunnel: up on localhost:%s", cfg.local_port)
    return True


def ssh_tunnel_config_from_env(fast_api_url: str) -> SshTunnelConfig:
    import os

    enabled = os.getenv("NITROGEN_SSH_TUNNEL", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    remote_port = int(os.getenv("NITROGEN_SSH_REMOTE_PORT", "8000"))
    return SshTunnelConfig(
        enabled=enabled,
        host=os.getenv("NITROGEN_SSH_HOST", "connect.bjb1.seetacloud.com"),
        port=int(os.getenv("NITROGEN_SSH_PORT", "18037")),
        user=os.getenv("NITROGEN_SSH_USER", "root"),
        password=os.getenv("NITROGEN_SSH_PASSWORD", ""),
        local_port=local_port_from_url(fast_api_url),
        remote_port=remote_port,
        identity_file=os.getenv("NITROGEN_SSH_KEY", "").strip(),
    )


def ensure_nitrogen_ssh_tunnel(fast_api_url: str) -> bool:
    cfg = ssh_tunnel_config_from_env(fast_api_url)
    if not cfg.enabled:
        return False
    return start_ssh_tunnel(cfg)
