# backend/database.py
import os
import aiosqlite
from config_loader import config


async def get_db():
    db = await aiosqlite.connect(f"{config.data_dir}/data/lumneo.db")
    db.row_factory = aiosqlite.Row
    return db

async def init_db():
    os.makedirs(os.path.dirname(f"{config.data_dir}/data/lumneo.db"), exist_ok=True)
    db = await get_db()

    await db.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id TEXT PRIMARY KEY,
            title TEXT DEFAULT '新对话',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            file_ref TEXT DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            tools TEXT NOT NULL DEFAULT '[]',
            profile_prompt TEXT DEFAULT '',
            temperature REAL DEFAULT 1.0,
            top_p REAL DEFAULT 1.0,
            top_k INTEGER DEFAULT 40,
            frequency_penalty REAL DEFAULT 0.0,
            presence_penalty REAL DEFAULT 0.0,
            skills TEXT NOT NULL DEFAULT '[]'
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS models (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            modelName TEXT NOT NULL,
            baseUrl TEXT NOT NULL,
            apiKey TEXT NOT NULL
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS tool_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            call_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            arguments TEXT DEFAULT NULL,
            result TEXT DEFAULT NULL,
            status TEXT DEFAULT 'calling',
            execution_time INTEGER DEFAULT NULL,
            error_message TEXT DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
        )
    """)

    # 通用键值配置表（当前用于存储知识库 embedding 配置等应用级设置）
    await db.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 知识库 RAG：分片元数据（内容 + 来源文件 + 标题路径 + 文件指纹）
    await db.execute("""
        CREATE TABLE IF NOT EXISTS kb_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            heading_path TEXT DEFAULT '',
            content TEXT NOT NULL,
            file_hash TEXT NOT NULL,
            model_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_kb_chunks_file ON kb_chunks(file_path)"
    )

    # FTS5 全文搜索（知识库关键词检索）
    await db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks_fts USING fts5(
            content, heading_path, content='kb_chunks', content_rowid='id'
        )
    """)
    # 触发器：INSERT 时自动同步 FTS 索引
    await db.execute("""
        CREATE TRIGGER IF NOT EXISTS kb_chunks_fts_ai AFTER INSERT ON kb_chunks BEGIN
            INSERT INTO kb_chunks_fts(rowid, content, heading_path)
            VALUES (new.id, new.content, new.heading_path);
        END
    """)
    # 触发器：DELETE 时自动移除 FTS 条目
    await db.execute("""
        CREATE TRIGGER IF NOT EXISTS kb_chunks_fts_ad AFTER DELETE ON kb_chunks BEGIN
            INSERT INTO kb_chunks_fts(kb_chunks_fts, rowid, content, heading_path)
            VALUES ('delete', old.id, old.content, old.heading_path);
        END
    """)
    # 触发器：UPDATE 时自动同步
    await db.execute("""
        CREATE TRIGGER IF NOT EXISTS kb_chunks_fts_au AFTER UPDATE ON kb_chunks BEGIN
            INSERT INTO kb_chunks_fts(kb_chunks_fts, rowid, content, heading_path)
            VALUES ('delete', old.id, old.content, old.heading_path);
            INSERT INTO kb_chunks_fts(rowid, content, heading_path)
            VALUES (new.id, new.content, new.heading_path);
        END
    """)

    # 知识库 RAG：索引状态（增量索引依据 file_hash / mtime）
    await db.execute("""
        CREATE TABLE IF NOT EXISTS kb_index_meta (
            file_path TEXT PRIMARY KEY,
            file_hash TEXT NOT NULL,
            mtime REAL NOT NULL,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 自定义技能（Skill）：前台可视化注册，动态加载，无需重启
    #   skill_type: 'prompt'（提示词 + 工具白名单，人人可建）
    #             | 'code'（可执行 Python 脚本，仅管理员可建/编辑）
    await db.execute("""
        CREATE TABLE IF NOT EXISTS skills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            skill_type TEXT NOT NULL DEFAULT 'prompt',
            enabled INTEGER NOT NULL DEFAULT 1,
            instruction TEXT DEFAULT '',
            tools TEXT NOT NULL DEFAULT '[]',
            code TEXT DEFAULT '',
            parameters TEXT NOT NULL DEFAULT '{}',
            isolated INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 会话记忆：存储 AI 助手的重要回复，用于跨对话语义检索
    await db.execute("""
        CREATE TABLE IF NOT EXISTS session_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE,
            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
        )
    """)

    # 知识库标签系统
    await db.execute("""
        CREATE TABLE IF NOT EXISTS kb_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            color TEXT DEFAULT '#6366f1',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS kb_file_tags (
            file_path TEXT NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (file_path, tag_id),
            FOREIGN KEY (tag_id) REFERENCES kb_tags(id) ON DELETE CASCADE
        )
    """)

    # ---- 轻量迁移：为旧版数据库补齐新增列 ----
    await _ensure_column(db, "profiles", "skills", "TEXT NOT NULL DEFAULT '[]'")
    # 对话分叉
    await _ensure_column(db, "chats", "parent_chat_id", "TEXT DEFAULT NULL")
    await _ensure_column(db, "chats", "branched_at_message_id", "INTEGER DEFAULT NULL")
    # 模型角色（自动切换）
    await _ensure_column(db, "models", "role", "TEXT NOT NULL DEFAULT 'default'")

    await db.commit()
    await db.close()


async def _ensure_column(db, table: str, column: str, ddl: str):
    """若表缺少指定列则动态添加（兼容旧版数据库）。"""
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    existing = {row[1] for row in rows}
    if column not in existing:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")