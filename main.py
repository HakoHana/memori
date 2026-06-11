"""memori — HTTP 服务启动入口

用法:
    python main.py --port 8765
    python -m memori --port 8765

也可直接作为 AstrBot 插件加载（通过 metadata.yaml 注册的 Star 插件）。
"""

import sys
import warnings

# 弃用警告：main.py 不再包含 AstrBot Star 插件类
# AstrBot 插件已迁移至 adapters/astrbot/plugin.py
warnings.warn(
    "main.py 已精简为 HTTP 服务入口。AstrBot Star 插件已迁移至 "
    "adapters/astrbot/plugin.py，请更新 metadata.yaml 的 module 指向。",
    DeprecationWarning,
    stacklevel=2,
)

if __name__ == "__main__":
    from memori.__main__ import main
    sys.exit(main())
