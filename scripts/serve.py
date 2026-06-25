"""
NitroGen 推理服务（serve.py）。

这是对 NitroGen 上游 scripts/serve.py 的适配版本。
原始代码见：https://github.com/MineDojo/NitroGen

启动方式（Linux + GPU）：
  python scripts/serve.py /path/to/ng.pt --port 5555 --ctx 1

注意：
- 需要在安装了 NitroGen 依赖的环境中运行
- 如果远程部署，修改 .env 中 NITROGEN_SERVER=tcp://<remote_ip>:5555
- 本机开发时，直接在本地跑此脚本，backend 通过 tcp://localhost:5555 连接

协议说明：
  客户端发送：pickle({ "type": "predict", "image": PIL.Image })
  服务端返回：pickle({ "pred": {
    "j_left":  ndarray (16, 2),
    "j_right": ndarray (16, 2),
    "buttons": ndarray (16, 21),
  }})
"""

import argparse
import pickle
import logging

import zmq

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [serve] %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def load_model(ckpt_path: str, ctx: int):
    """加载 NitroGen 模型（依赖 NitroGen 库）"""
    try:
        # NitroGen 上游导入路径（请根据实际安装路径调整）
        from nitrogen.model import NitroGenModel
        model = NitroGenModel.from_pretrained(ckpt_path, ctx=ctx)
        model.eval()
        logger.info("Model loaded: %s  (ctx=%d)", ckpt_path, ctx)
        return model
    except ImportError as e:
        logger.error("NitroGen 库未安装：%s", e)
        logger.error("请先安装 NitroGen：pip install -e /path/to/NitroGen")
        raise


def predict(model, image):
    """
    单帧推理。
    image: PIL.Image，256x256 RGB
    返回：{"j_left": (16,2), "j_right": (16,2), "buttons": (16,21)}
    """
    import torch
    import numpy as np

    with torch.no_grad():
        pred = model.predict(image)

    return {
        "j_left":  pred["j_left"].cpu().numpy().astype("float32"),
        "j_right": pred["j_right"].cpu().numpy().astype("float32"),
        "buttons": pred["buttons"].cpu().numpy().astype("float32"),
    }


def main():
    parser = argparse.ArgumentParser(description="NitroGen ZMQ 推理服务")
    parser.add_argument("ckpt",  type=str,          help="模型权重路径")
    parser.add_argument("--port", type=int, default=5555, help="ZMQ 监听端口")
    parser.add_argument("--ctx",  type=int, default=1,    help="上下文帧数（NitroGen 参数）")
    args = parser.parse_args()

    model = load_model(args.ckpt, args.ctx)

    ctx    = zmq.Context()
    socket = ctx.socket(zmq.REP)
    socket.bind(f"tcp://*:{args.port}")
    logger.info("Listening on tcp://*:%d", args.port)

    while True:
        try:
            raw     = socket.recv()
            request = pickle.loads(raw)

            if request.get("type") == "predict":
                image = request["image"]
                pred  = predict(model, image)
                socket.send(pickle.dumps({"pred": pred}))
            else:
                socket.send(pickle.dumps({"error": "unknown type"}))

        except KeyboardInterrupt:
            logger.info("Shutting down")
            break
        except Exception as e:
            logger.error("Inference error: %s", e)
            try:
                socket.send(pickle.dumps({"error": str(e)}))
            except Exception:
                pass

    socket.close()
    ctx.term()


if __name__ == "__main__":
    main()
