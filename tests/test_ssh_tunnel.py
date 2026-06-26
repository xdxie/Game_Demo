"""SSH 隧道辅助"""

from unittest.mock import MagicMock, patch

from backend.nitrogen.ssh_tunnel import (
    SshTunnelConfig,
    local_port_from_url,
    ssh_tunnel_config_from_env,
    start_ssh_tunnel,
)


def test_local_port_from_url():
    assert local_port_from_url("http://localhost:8000") == 8000
    assert local_port_from_url("http://127.0.0.1:9001/predict") == 9001


def test_ssh_tunnel_config_from_env():
    with patch.dict(
        "os.environ",
        {
            "NITROGEN_SSH_TUNNEL": "1",
            "NITROGEN_SSH_HOST": "example.com",
            "NITROGEN_SSH_PORT": "22",
            "NITROGEN_SSH_USER": "ubuntu",
        },
        clear=False,
    ):
        cfg = ssh_tunnel_config_from_env("http://localhost:8000")
        assert cfg.enabled is True
        assert cfg.host == "example.com"
        assert cfg.port == 22
        assert cfg.user == "ubuntu"
        assert cfg.local_port == 8000


def test_start_ssh_tunnel_reuses_open_port():
    cfg = SshTunnelConfig(enabled=True, local_port=8000)
    with patch("backend.nitrogen.ssh_tunnel.port_is_open", return_value=True):
        assert start_ssh_tunnel(cfg) is True


def test_start_ssh_tunnel_with_password_uses_paramiko():
    cfg = SshTunnelConfig(
        enabled=True, local_port=18000, password="secret",
    )
    mock_tunnel = MagicMock()
    with patch("backend.nitrogen.ssh_tunnel.port_is_open", return_value=False), \
         patch("backend.nitrogen.ssh_tunnel._ParamikoTunnel", return_value=mock_tunnel), \
         patch("backend.nitrogen.ssh_tunnel.wait_for_port", return_value=True):
        assert start_ssh_tunnel(cfg) is True
        mock_tunnel.start.assert_called_once()
