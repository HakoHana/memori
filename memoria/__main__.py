"""Memoria HTTP 服务入口 — python -m memoria

用法:
    python -m memoria                          # 默认 0.0.0.0:8765
    python -m memoria --port 8080 --host 127.0.0.1
    python -m memoria --data-dir /path/to/data
"""

from __future__ import annotations

import argparse
import sys

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Memoria Memory API Server")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument("--data-dir", default=None, help="数据目录")
    parser.add_argument("--reload", action="store_true", help="热重载（开发用）")
    args = parser.parse_args()

    # 通过环境变量传参给 create_app
    if args.data_dir:
        import os
        os.environ["MEMORIA_DATA_DIR"] = args.data_dir

    print(f"🧠 Memoria API 服务启动: http://{args.host}:{args.port}")
    print(f"   文档: http://{args.host}:{args.port}/docs")
    print(f"   健康检查: http://{args.host}:{args.port}/health")
    print()

    uvicorn.run(
        "memoria.api:create_app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        factory=True,
    )


if __name__ == "__main__":
    main()
