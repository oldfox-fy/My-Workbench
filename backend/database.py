# backend/database.py
import os
import aiosqlite
from config_loader import config
from backend.bootstrap import logger


async def get_db():
    db = await aiosqlite.connect(f"{config.data_dir}/data/lumneo.db")
    db.row_factory = aiosqlite.Row
    # WAL 模式是数据库级设置，只要任一连接打开过就会持久化。
    # 这里做一层保险：哪怕 init_db 没跑过，每次 get_db 也确保 WAL 开启。
    await db.execute("PRAGMA journal_mode=WAL")
    return db

async def init_db():
    os.makedirs(os.path.dirname(f"{config.data_dir}/data/lumneo.db"), exist_ok=True)
    db = await get_db()

    # ── WAL 模式：防止意外关闭导致数据库损坏 ──
    # WAL (Write-Ahead Logging) 模式下写入先追加到 WAL 文件，
    # 崩溃后 SQLite 自动从 WAL 恢复，不会像 DELETE 模式那样损坏主 DB。
    await db.execute("PRAGMA journal_mode=WAL")
    # 同步模式设为 NORMAL（WAL 下的推荐值，兼顾性能和安全）
    await db.execute("PRAGMA synchronous=NORMAL")
    # 外键约束
    await db.execute("PRAGMA foreign_keys=ON")

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
    # RAG 增强：分块类型与页码
    await _ensure_column(db, "kb_chunks", "chunk_type", "TEXT NOT NULL DEFAULT 'text'")
    await _ensure_column(db, "kb_chunks", "page_number", "INTEGER DEFAULT NULL")
    await _ensure_column(db, "kb_chunks", "citation_id", "TEXT DEFAULT ''")
    # OCR 缓存
    await _ensure_column(db, "kb_index_meta", "ocr_text", "TEXT DEFAULT ''")
    await _ensure_column(db, "kb_index_meta", "image_description", "TEXT DEFAULT ''")

    # Feature 3: Agent 追踪
    await db.execute("""
        CREATE TABLE IF NOT EXISTS agent_traces (
            id TEXT PRIMARY KEY,
            message_id INTEGER NOT NULL,
            chat_id TEXT NOT NULL,
            status TEXT DEFAULT 'running',
            total_steps INTEGER DEFAULT 0,
            total_tool_calls INTEGER DEFAULT 0,
            total_time_ms INTEGER DEFAULT 0,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS agent_spans (
            id TEXT PRIMARY KEY,
            trace_id TEXT NOT NULL,
            parent_span_id TEXT,
            span_type TEXT NOT NULL DEFAULT 'tool_call',
            name TEXT NOT NULL,
            status TEXT DEFAULT 'running',
            start_time REAL NOT NULL,
            end_time REAL,
            duration_ms INTEGER,
            input_preview TEXT DEFAULT '',
            output_preview TEXT DEFAULT '',
            error_message TEXT,
            FOREIGN KEY (trace_id) REFERENCES agent_traces(id) ON DELETE CASCADE
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_spans_trace ON agent_spans(trace_id)"
    )
    await _ensure_column(db, "tool_calls", "span_id", "TEXT DEFAULT NULL")

    # Feature 4: Agent 计划持久化
    await db.execute("""
        CREATE TABLE IF NOT EXISTS agent_plans (
            id TEXT PRIMARY KEY,
            message_id INTEGER NOT NULL,
            chat_id TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS agent_plan_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            title TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            tool_span_id TEXT DEFAULT NULL,
            FOREIGN KEY (plan_id) REFERENCES agent_plans(id) ON DELETE CASCADE
        )
    """)

    # Feature 5: Crew 模板
    await db.execute("""
        CREATE TABLE IF NOT EXISTS crew_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            mode TEXT NOT NULL DEFAULT 'sequential',
            config TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ── 数据库完整性检测与自动修复 ──
    await _check_and_repair(db)

    await db.commit()
    await db.close()


async def _ensure_column(db, table: str, column: str, ddl: str):
    """若表缺少指定列则动态添加（兼容旧版数据库）。"""
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    existing = {row[1] for row in rows}
    if column not in existing:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


async def _check_and_repair(db):
    """
    启动时检测数据库完整性，自动修复最常见的两种损坏：
      1. kb_chunks_fts（FTS5 虚表）— 损坏后 DROP + 重建 + 从 kb_chunks 全量重建索引
      2. vec_chunks（sqlite-vec 虚表）— 损坏后 DROP（下次索引时自动重建）

    其它表损坏仅记录警告（自动修复风险太高，建议用户手动处理）。
    """
    try:
        ok = await db.execute("PRAGMA integrity_check")
        result = await ok.fetchone()
        if result and result[0] == "ok":
            logger.info("[DB] 数据库完整性检测通过")
            return
        # 有损坏 → 记录详情
        error_msg = result[0] if result else "未知错误"
        logger.warning(f"[DB] 数据库完整性检测发现问题: {error_msg}")
    except Exception as e:
        # integrity_check 本身抛异常说明损坏严重
        logger.warning(f"[DB] 数据库完整性检测失败（数据库可能严重损坏）: {e}")
        return

    # ── 逐表尝试修复 ──
    repaired = []

    # 1) FTS5 虚表：最常见的损坏来源（高频写入 + 触发器）
    if await _try_repair_fts(db):
        repaired.append("kb_chunks_fts")

    # 2) sqlite-vec 向量表：独立扩展管理，可能与主库不一致
    if await _try_repair_vec(db):
        repaired.append("vec_chunks")

    if repaired:
        logger.warning(
            f"[DB] 已自动修复 {len(repaired)} 个表: {', '.join(repaired)}。"
            f"知识库需要「全量重建索引」以恢复数据完整性。"
        )
    else:
        logger.warning(
            "[DB] 检测到数据库损坏但无法自动修复。"
            "建议手动执行: sqlite3 lumneo.db \"PRAGMA integrity_check\" 查看详情。"
            "如果仅向量检索不可用，可在知识库设置中「全量重建索引」。"
        )


async def _try_repair_fts(db) -> bool:
    """尝试修复 FTS5 虚表：DROP → 重建 → 从 kb_chunks 回填。"""
    try:
        # 先测试 FTS 表是否可读
        await db.execute("SELECT count(*) FROM kb_chunks_fts")
    except Exception:
        pass  # 不可读，进入修复
    else:
        return False  # 可读，不需要修复

    logger.warning("[DB] kb_chunks_fts 损坏，自动重建 FTS 索引...")
    try:
        # 先尝试 DROP（可能失败，取决于损坏程度）
        try:
            await db.execute("DROP TABLE IF EXISTS kb_chunks_fts")
        except Exception:
            pass

        # 重建虚表
        await db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks_fts USING fts5(
                content, heading_path, content='kb_chunks', content_rowid='id'
            )
        """)

        # 重建触发器
        await db.execute("DROP TRIGGER IF EXISTS kb_chunks_fts_ai")
        await db.execute("""
            CREATE TRIGGER kb_chunks_fts_ai AFTER INSERT ON kb_chunks BEGIN
                INSERT INTO kb_chunks_fts(rowid, content, heading_path)
                VALUES (new.id, new.content, new.heading_path);
            END
        """)
        await db.execute("DROP TRIGGER IF EXISTS kb_chunks_fts_ad")
        await db.execute("""
            CREATE TRIGGER kb_chunks_fts_ad AFTER DELETE ON kb_chunks BEGIN
                INSERT INTO kb_chunks_fts(kb_chunks_fts, rowid, content, heading_path)
                VALUES ('delete', old.id, old.content, old.heading_path);
            END
        """)
        await db.execute("DROP TRIGGER IF EXISTS kb_chunks_fts_au")
        await db.execute("""
            CREATE TRIGGER kb_chunks_fts_au AFTER UPDATE ON kb_chunks BEGIN
                INSERT INTO kb_chunks_fts(kb_chunks_fts, rowid, content, heading_path)
                VALUES ('delete', old.id, old.content, old.heading_path);
                INSERT INTO kb_chunks_fts(rowid, content, heading_path)
                VALUES (new.id, new.content, new.heading_path);
            END
        """)

        # 从 kb_chunks 全量重建 FTS 索引
        await db.execute(
            "INSERT INTO kb_chunks_fts(rowid, content, heading_path) "
            "SELECT id, content, heading_path FROM kb_chunks"
        )
        logger.info("[DB] kb_chunks_fts 重建完成")
        return True
    except Exception as e:
        logger.warning(f"[DB] kb_chunks_fts 自动修复失败: {e}")
        return False


async def _try_repair_vec(db) -> bool:
    """尝试修复 sqlite-vec 向量表：损坏则 DROP（下次索引时自动重建）。"""
    try:
        # 检查 vec_chunks 是否存在
        row = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vec_chunks'"
        )
        exists = await row.fetchone()
        if not exists:
            return False  # 表不存在，无需修复

        # 尝试读取
        await db.execute("SELECT count(*) FROM vec_chunks")
        return False  # 可读，无需修复
    except Exception:
        pass  # 损坏，进入修复

    logger.warning("[DB] vec_chunks 向量表损坏，自动清理（下次索引重建）...")
    try:
        await db.execute("DROP TABLE IF EXISTS vec_chunks")
        # 也清理 session_memories 向量表（共用扩展，可能同时受影响）
        try:
            await db.execute("DROP TABLE IF EXISTS vec_session_memories")
        except Exception:
            pass
        # 清除索引元数据，强制下次全量重建
        await db.execute("DELETE FROM kb_index_meta")
        logger.info("[DB] vec_chunks 已清理，请执行「全量重建索引」恢复")
        return True
    except Exception as e:
        logger.warning(f"[DB] vec_chunks 自动修复失败: {e}")
        return False