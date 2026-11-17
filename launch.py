from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
WEB_URL = "http://127.0.0.1:3001"
API_URL = "http://127.0.0.1:8001"


def available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(.3)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def ready(url: str, processes: list[subprocess.Popen], timeout: float = 40) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if any(proc.poll() is not None for proc in processes):
            return False
        try:
            with opener.open(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(.25)
    return False


def already_running() -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{WEB_URL}/contest-api/health", timeout=2) as response:
            return json.loads(response.read(4096)).get("service") == "evidence-desk-contest"
    except (OSError, ValueError, AttributeError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="启动独立参赛版")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--production", action="store_true", help="使用已执行 npm run build 的前端")
    args = parser.parse_args()
    missing = [module for module in ("fastapi", "httpx", "dotenv", "uvicorn") if importlib.util.find_spec(module) is None]
    if missing:
        print("缺少后端依赖，请先运行：python -m pip install -r requirements.txt")
        return 1
    node = shutil.which("node")
    next_cli = WEB / "node_modules" / "next" / "dist" / "bin" / "next"
    if not node or not next_cli.is_file():
        print("需要 Node.js 和前端依赖。请进入 web 运行 npm ci。")
        return 1
    build_dir = ".next-seat" if os.environ.get("CONTEST_WEB_BUILD_DIR") == ".next-seat" else ".next"
    if args.production and not (WEB / build_dir / "BUILD_ID").is_file():
        print("生产构建不存在。请先在 web 中运行 npm run build。")
        return 1
    if not all(available(port) for port in (3001, 8001)):
        if already_running():
            print(f"智慧交易助手已运行：{WEB_URL}")
            if not args.no_browser:
                webbrowser.open(WEB_URL)
            return 0
        print("参赛版端口 3001 或 8001 已被占用，请使用已启动的实例或关闭占用进程。不会自动终止其他进程。")
        return 1

    log_dir = ROOT / "runlogs"
    log_dir.mkdir(exist_ok=True)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    processes: list[subprocess.Popen] = []
    with (log_dir / "api.log").open("a", encoding="utf-8") as api_log, (log_dir / "web.log").open("a", encoding="utf-8") as web_log:
        try:
            processes.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "server.main:app", "--host", "127.0.0.1", "--port", "8001"], cwd=ROOT, stdout=api_log, stderr=subprocess.STDOUT, creationflags=flags))
            if not ready(f"{API_URL}/health", processes):
                print(f"后端启动失败，请查看 {log_dir / 'api.log'}")
                return 1
            processes.append(subprocess.Popen([node, str(next_cli), "start" if args.production else "dev", "--hostname", "127.0.0.1", "--port", "3001"], cwd=WEB, stdout=web_log, stderr=subprocess.STDOUT, creationflags=flags))
            if not ready(f"{WEB_URL}/contest-api/health", processes):
                print(f"前端启动失败，请查看 {log_dir / 'web.log'}")
                return 1
            print(f"智慧交易助手参赛版已启动：{WEB_URL}（Ctrl+C 结束）", flush=True)
            if not args.no_browser:
                webbrowser.open(WEB_URL)
            while all(proc.poll() is None for proc in processes):
                time.sleep(.5)
            print("服务已退出，详细信息见 runlogs。")
            return 1
        except KeyboardInterrupt:
            return 0
        finally:
            for proc in reversed(processes):
                if proc.poll() is None:
                    proc.terminate()
            for proc in processes:
                try:
                    proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
