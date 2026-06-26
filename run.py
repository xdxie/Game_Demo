"""
快速启动脚本。在 demo/ 根目录执行：
  python run.py

等价于：
  cd demo && uvicorn backend.main:app --host 0.0.0.0 --port 8000

前置条件：
  1. pip install -r requirements.txt
  2. 在 .env 填写 ANTHROPIC_API_KEY
  3. 在 GPU 机器上启动 NitroGen serve：
       python scripts/serve.py /path/to/ng.pt --port 5555
  4. 如果 NitroGen 在远程，修改 .env 中 NITROGEN_SERVER=tcp://<ip>:5555
"""
import logging

import uvicorn
from dotenv import load_dotenv
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT / ".env")

logger = logging.getLogger(__name__)


def _websocket_stack_ready() -> bool:
    try:
        import websockets  # noqa: F401
        return True
    except ImportError:
        try:
            import wsproto  # noqa: F401
            return True
        except ImportError:
            return False


if __name__ == "__main__":
    from backend.config import reload_config_from_env
    from backend.nitrogen.factory import nitrogen_mock_enabled, nitrogen_mode_label

    cfg = reload_config_from_env()
    backend = nitrogen_mode_label(cfg)
    if backend == "mock":
        logger.info("NitroGen: mock 模式（仅前端闭环）。实机请设 NITROGEN_MOCK=0")
    elif backend == "fast_api":
        from backend.nitrogen.ssh_tunnel import (
            ensure_nitrogen_ssh_tunnel,
            local_port_from_url,
        )
        nitro_port = local_port_from_url(cfg.nitrogen_fast_api_url)
        if nitro_port == 8000:
            logger.warning(
                "NITROGEN_FAST_API_URL 指向 localhost:8000，与陪玩服务端口冲突。"
                "请改为 http://localhost:18000，并设 NITROGEN_SSH_REMOTE_PORT=8000"
            )
        try:
            if ensure_nitrogen_ssh_tunnel(cfg.nitrogen_fast_api_url):
                logger.info(
                    "NitroGen: fast_api → %s（SSH 隧道已自动建立）",
                    cfg.nitrogen_fast_api_url,
                )
            else:
                logger.info(
                    "NitroGen: fast_api → %s（未设 NITROGEN_SSH_TUNNEL=1 时请手动开隧道）",
                    cfg.nitrogen_fast_api_url,
                )
        except Exception as e:
            logger.error("NitroGen SSH tunnel failed: %s", e)
            raise
    else:
        logger.info("NitroGen: ZMQ → %s", cfg.nitrogen_server)
    from backend.slow.vlm_factory import vlm_provider
    logger.info(
        "VLM: %s / %s（无 Key 时为 mock；真模型请设 VLM_MOCK=0 + VLM_API_KEY）",
        vlm_provider(cfg),
        cfg.vlm_model,
    )
    if not _websocket_stack_ready():
        logger.warning(
            "未检测到 websockets/wsproto：/ws 将返回 404。"
            "请执行: pip install \"uvicorn[standard]\" websockets"
        )
    print("\n" + "=" * 50)
    print("  请在浏览器打开: http://localhost:8000")
    print("=" * 50 + "\n")
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
        ws_ping_interval=30.0,
        ws_ping_timeout=120.0,
    )
