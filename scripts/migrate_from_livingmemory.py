"""
完整迁移：清空记忆数据 → 从 livingmemory 导入并映射到用户 Hako

用法：
    python scripts/migrate_from_livingmemory.py
"""

import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

SOURCE_DB = "/home/hako/data/plugin_data/astrbot_plugin_livingmemory/livingmemory.db"
TARGET_DB = "/home/hako/data/plugin_data/Memory/memory.db"
DATA_DIR  = "/home/hako/data/plugin_data/Memory"
USER_NAME = "Hako"
USER_UID  = "u_hako_main"

print("=" * 60)
print("  livingmemory → Memory 插件 完整迁移")
print("  目标用户:", USER_NAME)
print("  用户 UID:", USER_UID)
print("=" * 60)

# ── 1. 检查源数据库 ──
if not os.path.exists(SOURCE_DB):
    print(f"❌ 找不到 livingmemory 数据库: {SOURCE_DB}")
    sys.exit(1)

source = sqlite3.connect(SOURCE_DB)
source.row_factory = sqlite3.Row

# ── 2. 备份目标数据库 ──
if os.path.exists(TARGET_DB):
    backup_path = TARGET_DB + ".bak." + datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(TARGET_DB, backup_path)
    print(f"📦 已备份当前数据库 → {backup_path}")

target = sqlite3.connect(TARGET_DB)
target.execute("PRAGMA journal_mode=WAL")
target.execute("PRAGMA synchronous=NORMAL")
target_c = target.cursor()

# ── 补齐缺失表 ──
MISSING_TABLES = {
    "canonical_users": """
        CREATE TABLE IF NOT EXISTS canonical_users (
            uid TEXT PRIMARY KEY,
            primary_name TEXT,
            identity_confidence REAL DEFAULT 0.3,
            created_at REAL,
            updated_at REAL
        )""",
    "user_identities": """
        CREATE TABLE IF NOT EXISTS user_identities (
            platform_id TEXT PRIMARY KEY,
            uid TEXT NOT NULL,
            platform TEXT NOT NULL,
            display_name TEXT,
            first_seen REAL,
            last_seen REAL,
            verified INTEGER DEFAULT 0,
            source TEXT DEFAULT 'auto'
        )""",
    "user_persona": """
        CREATE TABLE IF NOT EXISTS user_persona (
            uid TEXT PRIMARY KEY,
            summary TEXT,
            full_markdown TEXT,
            known_ids TEXT DEFAULT '[]',
            primary_name TEXT,
            identity_confidence REAL DEFAULT 0.3,
            tier TEXT DEFAULT 'new',
            version INTEGER DEFAULT 1,
            last_full_update REAL,
            last_incremental_update REAL,
            incremental_count INTEGER DEFAULT 0,
            diary_count_since_full INTEGER DEFAULT 0,
            created_at REAL,
            updated_at REAL
        )""",
}
existing_tables = set(
    r[0] for r in target_c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
)
for tname, ddl in MISSING_TABLES.items():
    if tname not in existing_tables:
        target_c.execute(ddl)
        print(f"  ✅ 创建缺失表 {tname}")

# ── 补齐缺失列 ──
for col_def in [
    "ALTER TABLE user_persona ADD COLUMN tags TEXT DEFAULT '[]'",
    "ALTER TABLE user_persona ADD COLUMN tier TEXT DEFAULT 'new'",
]:
    try: target_c.execute(col_def)
    except: pass

# 补齐索引
for idx_def in [
    "CREATE INDEX IF NOT EXISTS idx_identity_uid ON user_identities(uid)",
]:
    try: target_c.execute(idx_def)
    except: pass

# ── 3. 清理现有数据（保留表结构） ──
print("\n🧹 清理现有数据...")
for table in [
    "memory_atoms", "memory_atoms_fts",
    "diary_entries", "diary_fts",
    "graph_nodes", "graph_edges", "entity_cooccur",
    "atomic_facts", "diary_fact_links",
    "write_ops", "consolidation_state",
    "user_persona",
]:
    try:
        target_c.execute(f"DELETE FROM {table}")
        print(f"  ✅ 清空 {table}")
    except sqlite3.OperationalError as e:
        print(f"  ⚠️  {table}: {e}")
target.commit()

# ── 4. 建立用户身份 ──
print(f"\n👤 建立用户 '{USER_NAME}' (UID: {USER_UID})...")
now = time.time()

# canonical_users
target_c.execute("""
    INSERT OR REPLACE INTO canonical_users
    (uid, primary_name, identity_confidence, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?)
""", (USER_UID, USER_NAME, 1.0, now, now))

# user_registry
target_c.execute("""
    INSERT OR REPLACE INTO user_registry
    (user_id, user_name, first_seen_at, last_seen_at, name_updated_at)
    VALUES (?, ?, ?, ?, ?)
""", (USER_NAME, USER_NAME, now, now, now))

# user_identities
target_c.execute("""
    INSERT OR REPLACE INTO user_identities
    (platform_id, uid, platform, display_name, first_seen, last_seen, verified, source)
    VALUES (?, ?, ?, ?, ?, ?, 1, 'migration')
""", (f"migrate:{USER_NAME}", USER_UID, "import", USER_NAME, now, now))

# user_persona
target_c.execute("""
    INSERT OR REPLACE INTO user_persona
    (uid, summary, tags, tier, version, created_at, updated_at)
    VALUES (?, ?, ?, ?, 1, ?, ?)
""", (USER_UID, "从 livingmemory 导入的用户画像", json.dumps(["imported"], ensure_ascii=False), "known", now, now))

target.commit()
print(f"  ✅ 用户 {USER_NAME} ({USER_UID}) 已就绪")

# ── 5. 读取 livingmemory 原子 ──
print("\n📖 读取 livingmemory 原子...")
atoms = source.execute("SELECT * FROM memory_atoms ORDER BY created_at ASC").fetchall()
print(f"  共 {len(atoms)} 条原子")

# ── 6. 转换并写入原子 ──
TYPE_MAP = {
    "episodic": "episodic", "factual": "factual", "preference": "preference",
    "planned": "planned", "relational": "relational", "unknown": "unknown",
}
STATUS_MAP = {
    "active": "active", "dormant": "dormant", "expired": "archived",
    "forgotten": "forgotten", "superseded": "archived",
}

def parse_entities(raw):
    if not raw: return []
    try: return json.loads(raw) if isinstance(raw, str) else raw
    except: return [str(raw)] if raw else []

def parse_meta(raw):
    if not raw: return {}
    try: return json.loads(raw) if isinstance(raw, str) else raw
    except: return {}

print("\n🔄 转换并写入原子...")
inserted = 0
skipped = 0

for row in atoms:
    row = dict(row)
    created_at = float(row.get("created_at", time.time()))
    atom_type = TYPE_MAP.get(row.get("atom_type", "unknown"), "unknown")
    status = STATUS_MAP.get(row.get("status", "active"), "active")

    # 从 content 或 created_at 推导日期
    meta = parse_meta(row.get("metadata"))
    diary_date = meta.get("date", "") or datetime.fromtimestamp(created_at).strftime("%Y-%m-%d")

    # 检查重复
    existing = target_c.execute(
        "SELECT id FROM memory_atoms WHERE user_id=? AND content=? AND diary_date=?",
        (USER_NAME, row.get("content", ""), diary_date),
    ).fetchone()

    if existing:
        skipped += 1
        continue

    # 写入原子
    entities = parse_entities(row.get("entities"))
    target_c.execute("""
        INSERT INTO memory_atoms
        (user_id, diary_date, atom_type, content, entities,
         importance, confidence, access_count, created_at,
         last_accessed_at, ttl_days, expires_at, decay_type, status,
         session_id, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        USER_NAME, diary_date, atom_type, row.get("content", ""),
        json.dumps(entities, ensure_ascii=False),
        float(row.get("importance", 0.5)),
        float(row.get("confidence", 0.7)),
        int(row.get("reinforcement_count", 0)),
        created_at,
        row.get("last_accessed_at"),
        float(row.get("ttl_days", 30)),
        float(row.get("expires_at", 0)),
        row.get("decay_type", "exponential"),
        status,
        row.get("session_id"),
        json.dumps(meta, ensure_ascii=False),
    ))
    atom_id = target_c.lastrowid

    # FTS 索引
    try:
        target_c.execute(
            "INSERT INTO memory_atoms_fts (atom_id, content, user_id) VALUES (?, ?, ?)",
            (atom_id, row.get("content", ""), USER_NAME),
        )
    except sqlite3.OperationalError:
        pass

    inserted += 1

target.commit()
print(f"  ✅ 写入 {inserted} 条，跳过 {skipped} 条重复")

# ── 7. 导入文档作为日记条目 ──
print("\n📔 导入文档作为日记条目...")
docs = source.execute("""
    SELECT d.*, json_extract(d.metadata, '$.session_id') as session
    FROM documents d ORDER BY d.created_at ASC
""").fetchall()
diary_inserted = 0

for doc in docs:
    doc = dict(doc)
    text = doc.get("text", "")
    if not text.strip():
        continue

    created_at = doc.get("created_at", time.time())
    if isinstance(created_at, str):
        try: created_at = datetime.fromisoformat(created_at).timestamp()
        except: created_at = time.time()
    else:
        created_at = float(created_at)
    date_str = datetime.fromtimestamp(created_at).strftime("%Y-%m-%d")

    # 构建日记内容（带 frontmatter）
    diary_content = (
        f"---\n"
        f"date: {date_str}\n"
        f"user_id: {USER_NAME}\n"
        f"mood: 日常\n"
        f"importance: 0.5\n"
        f"source: livingmemory_import\n"
        f"---\n\n"
        f"{text}\n"
    )

    # 直接插入，每条文档作为一篇独立的日记
    target_c.execute("""
        INSERT INTO diary_entries
        (user_id, date, content, created_at, updated_at, status)
        VALUES (?, ?, ?, ?, ?, 'active')
    """, (USER_NAME, date_str, diary_content, created_at, created_at))

    diary_inserted += 1

target.commit()
print(f"  ✅ 导入 {diary_inserted} 篇日记")

# ── 8. 导入图谱数据 ──
print("\n🔗 导入知识图谱...")

# 节点
gnodes = source.execute("SELECT * FROM graph_nodes").fetchall()
node_map = {}  # old_id → new_id
gnode_inserted = 0
for node in gnodes:
    node = dict(node)
    key = node.get("node_key", "")
    existing = target_c.execute(
        "SELECT id FROM graph_nodes WHERE node_key=?", (key,)
    ).fetchone()
    if existing:
        node_map[node["id"]] = existing[0]
        gnode_inserted += 1
        continue

    now_str = datetime.fromtimestamp(time.time()).isoformat()
    target_c.execute("""
        INSERT INTO graph_nodes
        (node_key, node_type, value, canonical_value, metadata, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        key,
        node.get("node_type", "topic"),
        node.get("value", ""),
        node.get("canonical_value", ""),
        json.dumps(parse_meta(node.get("metadata"))),
        node.get("created_at", now_str) or now_str,
        node.get("updated_at", now_str) or now_str,
    ))
    node_map[node["id"]] = target_c.lastrowid
    gnode_inserted += 1

target.commit()
print(f"  ✅ 导入 {gnode_inserted} 个节点")

# 边
gedges = source.execute("SELECT * FROM graph_edges ORDER BY id").fetchall()
gedge_inserted = 0
for edge in gedges:
    edge = dict(edge)
    src_id = node_map.get(edge.get("source_node_id"))
    tgt_id = node_map.get(edge.get("target_node_id"))
    if not src_id or not tgt_id:
        continue

    src_key = f"node:{src_id}"
    tgt_key = f"node:{tgt_id}"
    ekey = f"{src_key}|{edge.get('relation_type','')}|{tgt_key}|{edge.get('source_memory_id',0)}"

    existing = target_c.execute(
        "SELECT id FROM graph_edges WHERE edge_key=?", (ekey,)
    ).fetchone()
    if existing:
        continue

    now_str = datetime.fromtimestamp(time.time()).isoformat()
    target_c.execute("""
        INSERT INTO graph_edges
        (edge_key, semantic_key, source_node_id, target_node_id,
         relation_type, source_memory_id, weight, confidence, status,
         metadata, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
    """, (
        ekey,
        f"{src_key}|{edge.get('relation_type','')}|{tgt_key}",
        src_id, tgt_id,
        edge.get("relation_type", "co_occur"),
        edge.get("source_memory_id", 0),
        float(edge.get("weight", 1.0)),
        float(edge.get("confidence", 0.8)),
        json.dumps(parse_meta(edge.get("metadata"))),
        edge.get("created_at", now_str) or now_str,
        edge.get("updated_at", now_str) or now_str,
    ))
    gedge_inserted += 1

target.commit()
print(f"  ✅ 导入 {gedge_inserted} 条边")

# ── 9. 写入 consolidation_state ──
target_c.execute("""
    INSERT OR REPLACE INTO consolidation_state
    (user_id, msg_count, warmup_threshold, last_consolidated_at,
     last_diary_date, diary_count, diary_count_since_persona, l1_retry_count)
    VALUES (?, 0, 0, ?, ?, ?, 0, 0)
""", (USER_NAME, now, datetime.now().strftime("%Y-%m-%d"), diary_inserted))
target.commit()

# ── 10. 统计 ──
print("\n" + "=" * 60)
print("  📊 迁移统计")
print("=" * 60)
print(f"  用户: {USER_NAME} ({USER_UID})")
print(f"  记忆原子: {inserted}")
print(f"  日记条目: {diary_inserted}")
print(f"  图谱节点: {gnode_inserted}")
print(f"  图谱边:   {gedge_inserted}")

# 验证
for tbl in ["memory_atoms", "diary_entries", "graph_nodes", "graph_edges"]:
    cnt = target_c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    print(f"  {tbl}: {cnt}")

target.commit()
target.close()
source.close()

print("\n🎉 迁移完成！重启 AstrBot 或重载插件即可生效。")
