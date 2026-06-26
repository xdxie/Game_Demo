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
    from backend.config import get_config
    from backend.nitrogen.factory import nitrogen_mock_enabled

    cfg = get_config()
    if nitrogen_mock_enabled(cfg):
        logger.info("NitroGen: mock 模式（仅前端闭环，无 ZMQ）。实机推理请设 NITROGEN_MOCK=0")
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
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
