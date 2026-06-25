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
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
