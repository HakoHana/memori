"""画像文件存储 — Markdown 文件操作"""

from __future__ import annotations

from pathlib import Path


class PersonaStore:
    """画像存储：每个用户一个 Markdown 文件"""

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir) / "personas"

    def _file_path(self, user_id: str) -> Path:
        return self.base_dir / f"{user_id}.md"

    async def read(self, user_id: str) -> str | None:
        """读取用户画像"""
        path = self._file_path(user_id)
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            return f.read()

    async def write(self, user_id: str, content: str):
        """写入/覆盖用户画像"""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        path = self._file_path(user_id)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content.strip())
            f.write("\n")

    async def delete(self, user_id: str) -> bool:
        """删除用户画像"""
        path = self._file_path(user_id)
        if path.exists():
            path.unlink()
            return True
        return False

    async def exists(self, user_id: str) -> bool:
        """检查是否有画像"""
        return self._file_path(user_id).exists()
