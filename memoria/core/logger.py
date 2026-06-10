"""Logger 桥接模块

统一日志入口，隔离对 astrbot.api.logger 的直接依赖。
在其他平台可替换此模块的 backend 即可。
"""

import logging
import sys

logger = logging.getLogger("memory")
logger.setLevel(logging.DEBUG)

# 确保有 handler 输出到 stderr（与 AstrBot 日志流兼容）
if not logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        "[Memory] %(levelname)s %(message)s"
    ))
    logger.addHandler(handler)
    logger.propagate = False  # 不传播，避免重复
