"""
从 astrbot_plugin_livingmemory 导入记忆到本插件

使用方法：
    python scripts/import_from_livingmemory.py \\
        --source ~/.astrobot/plugin_data/astrbot_plugin_livingmemory/livingmemory.db \\
        --target ~/.astrobot/plugin_data/astrbot_plugin_memory/memory.db \\
        --data-dir ~/.astrobot/plugin_data/astrbot_plugin_memory
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from models.memory_atom import MemoryAtom, AtomType, AtomStatus


# ── 字段映射 ──

# livingmemory 的 atom_type → 我们的 atom_type
TYPE_MAP = {
    "episodic": "episodic",
    "factual": "factual",
    "preference": "preference",
    "planned": "planned",
    "relational": "relational",
    "unknown": "unknown",
}

# livingmemory 的 status → 我们的 status
STATUS_MAP = {
    "active": "active",
    "dormant": "dormant",
    "expired": "archived",
    "forgotten": "forgotten",
    "superseded": "archived",
}


def connect_db(db_path: str) -> sqlite3.Connection:
    """连接 SQLite 数据库"""
    if not os.path.exists(db_path):
        print(f"❌ 找不到数据库: {db_path}")
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def read_livingmemory_atoms(conn: sqlite3.Connection) -> list[dict]:
    """读取 livingmemory 的 atoms"""
    rows = conn.execute("SELECT * FROM memory_atoms ORDER BY created_at ASC").fetchall()
    print(f"📖 从 livingmemory 读取到 {len(rows)} 条原子")
    return [dict(r) for r in rows]


def convert_atom(row: dict, default_user: str = "") -> MemoryAtom:
    """将 livingmemory 的原子行转换为我们的 MemoryAtom"""

    # 类型映射
    raw_type = row.get("atom_type", "unknown")
    atom_type = TYPE_MAP.get(raw_type, "unknown")

    # 状态映射
    raw_status = row.get("status", "active")
    status = STATUS_MAP.get(raw_status, "active")

    # 时间处理
    created_at = row.get("created_at", time.time())

    # 提取 user_id（从 persona_id 或 session_id）
    user_id = row.get("persona_id") or ""
    if not user_id and row.get("session_id"):
        # 尝试从 session_id 提取用户标识
        session_id = row["session_id"]
        if session_id and session_id != "default":
            if "_" in session_id:
                user_id = session_id.split("_")[0]
            else:
                user_id = session_id
    if not user_id:
        # fallback: 使用指定的默认用户
        user_id = default_user if default_user else "unknown"

    # 解析 entities
    entities = []
    raw_entities = row.get("entities")
    if raw_entities:
        try:
            entities = json.loads(raw_entities) if isinstance(raw_entities, str) else raw_entities
        except (json.JSONDecodeError, TypeError):
            entities = [str(raw_entities)] if raw_entities else []

    # 解析 metadata
    metadata = {}
    raw_meta = row.get("metadata")
    if raw_meta:
        try:
            metadata = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
        except (json.JSONDecodeError, TypeError):
            metadata = {}

    # 从 content 或 metadata 尝试提取日期
    diary_date = metadata.get("date") or ""
    if not diary_date and created_at:
        diary_date = datetime.fromtimestamp(created_at).strftime("%Y-%m-%d")

    # 构建原子
    atom = MemoryAtom(
        user_id=user_id,
        diary_date=diary_date,
        content=row.get("content", ""),
        atom_type=AtomType(atom_type),
        entities=[e for e in entities if isinstance(e, str)],
        importance=float(row.get("importance", 0.5)),
        confidence=float(row.get("confidence", 0.7)),
        access_count=int(row.get("reinforcement_count", 0)),
        created_at=created_at,
        last_accessed_at=row.get("last_accessed_at"),
        ttl_days=float(row.get("ttl_days", 30)),
        status=AtomStatus(status),
        session_id=row.get("session_id"),
        # 老数据没有 diary_snippet，留空
        diary_snippet="",
        metadata=metadata,
    )

    return atom


def write_to_our_db(atoms: list[MemoryAtom], db_path: str):
    """写入我们的 atom_store 数据库"""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    inserted = 0
    skipped = 0

    for atom in atoms:
        # 检查是否已存在（按用户+内容去重）
        existing = cursor.execute(
            "SELECT id FROM memory_atoms WHERE user_id = ? AND content = ? AND diary_date = ?",
            (atom.user_id, atom.content, atom.diary_date),
        ).fetchone()

        if existing:
            skipped += 1
            continue

        cursor.execute("""
            INSERT INTO memory_atoms
            (user_id, diary_date, atom_type, content, entities,
             importance, confidence, access_count, created_at,
             last_accessed_at, ttl_days, status, session_id, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            atom.user_id, atom.diary_date, atom.atom_type.value, atom.content,
            json.dumps(atom.entities, ensure_ascii=False),
            atom.importance, atom.confidence, atom.access_count,
            atom.created_at, atom.last_accessed_at, atom.ttl_days,
            atom.status.value, atom.session_id,
            json.dumps(atom.metadata, ensure_ascii=False),
        ))

        atom_id = cursor.lastrowid

        # 写入 FTS 索引
        try:
            cursor.execute(
                "INSERT INTO memory_atoms_fts (atom_id, content, user_id) VALUES (?, ?, ?)",
                (atom_id, atom.content, atom.user_id),
            )
        except sqlite3.OperationalError:
            pass  # FTS 表可能不存在

        inserted += 1

    conn.commit()
    conn.close()

    print(f"✅ 写入 {inserted} 条新原子，跳过 {skipped} 条重复")


def generate_diary_files(atoms: list[MemoryAtom], data_dir: str):
    """根据原子生成日记 .md 文件"""
    # 按用户+日期分组
    diary_map: dict[tuple[str, str], list[MemoryAtom]] = {}
    for atom in atoms:
        key = (atom.user_id, atom.diary_date)
        if key not in diary_map:
            diary_map[key] = []
        diary_map[key].append(atom)

    written = 0
    for (user_id, date_str), atom_list in diary_map.items():
        if not date_str:
            continue

        # 构建日记路径
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue

        diary_path = Path(data_dir) / "diaries" / user_id / str(dt.year) / f"{dt.month:02d}" / f"{dt.day:02d}.md"
        diary_path.parent.mkdir(parents=True, exist_ok=True)

        # 如果日记文件已存在，跳过
        if diary_path.exists():
            continue

        # 生成日记内容
        lines = [
            f"---",
            f"date: {date_str}",
            f"user_id: {user_id}",
            f"imported_from: livingmemory",
            f"---",
            f"",
            f"# {date_str} 的记忆",
            f"",
            f"从之前的记忆系统导入：",
            f"",
        ]

        for atom in atom_list:
            type_emoji = {
                "episodic": "📖", "factual": "📌", "preference": "💝",
                "planned": "📅", "relational": "🔗", "unknown": "📝",
            }.get(atom.atom_type.value, "📝")
            lines.append(f"- {type_emoji} [{atom.atom_type.value}] {atom.content}")

        lines.append("")

        with open(diary_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        written += 1

    print(f"📔 生成了 {written} 篇日记文件")


def main():
    parser = argparse.ArgumentParser(description="从 astrbot_plugin_livingmemory 导入记忆")
    parser.add_argument("--source", required=True, help="livingmemory 的数据库路径")
    parser.add_argument("--target", required=True, help="本插件的数据库路径")
    parser.add_argument("--data-dir", required=True, help="本插件的数据目录")
    parser.add_argument("--no-diary", action="store_true", help="不生成日记文件")
    parser.add_argument("--default-user", default="", help="当无法确定用户ID时使用的默认用户")
    args = parser.parse_args()

    # 检查我们的数据库是否存在，不存在则先初始化
    target_db = Path(args.target)
    if not target_db.exists():
        print(f"⚠️ 目标数据库不存在，需要先运行一次插件让它初始化")
        print(f"   请先加载插件并触发一次初始化，然后再运行此脚本")
        return

    print("🔍 连接 livingmemory 数据库...")
    source_conn = connect_db(args.source)

    print("📖 读取记忆原子...")
    rows = read_livingmemory_atoms(source_conn)

    if not rows:
        print("❌ livingmemory 中没有数据")
        return

    print("🔄 转换数据格式...")
    atoms = [convert_atom(r, args.default_user) for r in rows]

    # 统计
    user_ids = set(a.user_id for a in atoms)
    types = {}
    for a in atoms:
        types[a.atom_type.value] = types.get(a.atom_type.value, 0) + 1

    print(f"\n📊 转换统计:")
    print(f"  用户数: {len(user_ids)}")
    print(f"  原子数: {len(atoms)}")
    print(f"  类型分布: {types}")
    print(f"  用户: {', '.join(user_ids)}\n")

    print("💾 写入目标数据库...")
    write_to_our_db(atoms, args.target)

    if not args.no_diary:
        print("📔 生成日记文件...")
        generate_diary_files(atoms, args.data_dir)

    source_conn.close()

    print("\n🎉 迁移完成！")
    print(f"   目标数据库: {args.target}")
    print(f"   数据目录: {args.data_dir}")
    print("\n💡 重启 AstrBot 或重载插件即可看到导入的记忆。")


if __name__ == "__main__":
    main()
