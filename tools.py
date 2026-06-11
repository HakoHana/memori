"""AstrBot Agent Tools（兼容入口）

此文件已迁移至 adapters/astrbot/tools.py。
保留此文件以保持向后兼容。
"""

import warnings

warnings.warn(
    "tools.py 已迁移至 adapters/astrbot/tools.py，请更新导入路径。",
    DeprecationWarning,
    stacklevel=2,
)

from .adapters.astrbot.tools import RecallTool, MemorizeTool

__all__ = ["RecallTool", "MemorizeTool"]
