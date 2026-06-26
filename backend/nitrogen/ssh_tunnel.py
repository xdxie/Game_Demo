"""
可选：启动 SSH 本地端口转发，把本机 NitroGen FastAPI 地址映射到远端。

与 action_fast_system/run_inference.py 中逻辑一致；凭据通过环境变量配置，
不在代码里写密码（使用本机 SSH 密钥或交互式登录）。
"""

from __future__ import annotations

import atexit
import logging
import socket
import subprocess
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_tunnel_proc: Optional[subprocess.Popen] = None
_we_started_tunnel = False


@dataclass
class SshTunnelConfig:
    enabled: bool = False
    host: str = "connect.bjb1.seetacloud.com"
    port: int = 18037
    user: str = "root"
    local_port: int = 8000
    remote_port: int = 8000
    identity_file: str = ""


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
    global _tunnel_proc, _we_started_tunnel
    if _tunnel_proc is not None and _tunnel_proc.poll() is None:
        _tunnel_proc.terminate()
        try:
            _tunnel_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _tunnel_proc.kill()
        logger.info("SSH tunnel closed")
    _tunnel_proc = None
    _we_started_tunnel = False


def start_ssh_tunnel(cfg: SshTunnelConfig) -> bool:
    """
    若本地端口已通，则复用现有隧道；否则 spawn ``ssh -L`` 子进程。

    Returns:
        True 表示隧道可用（新建或复用）；False 表示未启用。
    """
    global _tunnel_proc, _we_started_tunnel

    if not cfg.enabled:
        return False

    if port_is_open("127.0.0.1", cfg.local_port):
        logger.info(
            "SSH tunnel: localhost:%s already open — reusing",
            cfg.local_port,
        )
        return True

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

    logger.info(
        "SSH tunnel: launching %s@%s:%s → localhost:%s",
        cfg.user, cfg.host, cfg.port, cfg.local_port,
    )
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if not wait_for_port("127.0.0.1", cfg.local_port, total_timeout=15.0):
        proc.terminate()
        raise RuntimeError(
            f"SSH tunnel did not open localhost:{cfg.local_port} within 15s. "
            f"Check SSH login to {cfg.user}@{cfg.host}:{cfg.port}."
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
        local_port=local_port_from_url(fast_api_url),
        remote_port=remote_port,
        identity_file=os.getenv("NITROGEN_SSH_KEY", "").strip(),
    )


def ensure_nitrogen_ssh_tunnel(fast_api_url: str) -> bool:
    """按环境变量尝试建立隧道；fast_api 后端启动前调用。"""
    cfg = ssh_tunnel_config_from_env(fast_api_url)
    if not cfg.enabled:
        return False
    return start_ssh_tunnel(cfg)
