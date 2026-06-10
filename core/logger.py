"""Logger 桥接模块

统一日志入口，框架无关。
在其他平台可替换此模块的 backend 即可。
"""

import logging
import sys

logger = logging.getLogger("memori")
logger.setLevel(logging.DEBUG)

# 确保有 handler 输出到 stderr
if not logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        "[Memoria] %(levelname)s %(message)s"
    ))
    logger.addHandler(handler)
    logger.propagate = False
