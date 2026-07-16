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
      1. kb_chunks_fts（FTS5 虚表）— 损坏后 DROP + 重建 + 从 kb_chunks 回填
      2. vec_chunks（sqlite-vec 虚表）— 损坏后 DROP（下次索引时自动重建）

    重要：integrity_check 不检查 FTS5 影子表，必须独立用写操作探测。
    其它表损坏仅记录警告（自动修复风险太高，建议用户手动处理）。
    """
    # ── 第一步：主库完整性检查（仅诊断用，不影响修复流程）──
    integrity_ok = True
    integrity_msg = ""
    try:
        ok = await db.execute("PRAGMA integrity_check")
        result = await ok.fetchone()
        if result and result[0] == "ok":
            logger.info("[DB] 主库 B-tree 完整性检测通过")
        else:
            integrity_ok = False
            integrity_msg = result[0] if result else "未知错误"
            logger.warning(f"[DB] 主库 B-tree 完整性检测发现问题: {integrity_msg}")
    except Exception as e:
        integrity_ok = False
        integrity_msg = str(e)
        logger.warning(f"[DB] 主库完整性检测失败: {e}")
        # 不 return——继续尝试修复 FTS/vec

    # ── 第二步：kb_chunks 主表探活（先于 FTS，因为 FTS 依赖它）──
    kb_broken = await _probe_kb_chunks(db)
    if kb_broken:
        logger.warning("[DB] kb_chunks 主表访问/写入探测失败，触发自动重建...")
    else:
        logger.info("[DB] kb_chunks 主表探测通过")

    # ── 第三步：FTS5 虚表写操作探活 ──
    # integrity_check 不检查 FTS5 的影子表（_content/_idx/_docsize/_config），
    # 必须用 INSERT + DELETE 来验证虚表是否真的能正常工作。
    fts_broken = False if kb_broken else await _probe_fts_write(db)
    if fts_broken:
        logger.warning("[DB] FTS5 虚表写入探测失败，触发自动修复...")
    elif not kb_broken:
        logger.info("[DB] FTS5 虚表写入探测通过")

    # ── 第四步：vec_chunks 表探活 ──
    vec_broken = await _probe_vec_access(db)
    if vec_broken:
        logger.warning("[DB] 向量表访问探测失败，触发自动清理...")
    else:
        logger.info("[DB] 向量表访问探测通过（或表不存在，跳过）")

    # ── 第五步：执行修复 ──
    repaired = []

    if kb_broken and await _try_repair_kb_tables(db):
        repaired.append("kb_chunks + kb_index_meta + kb_chunks_fts")
        fts_broken = False  # kb_chunks 重建时已一并重建了 FTS

    if fts_broken and await _try_repair_fts(db):
        repaired.append("kb_chunks_fts")

    if vec_broken and await _try_repair_vec(db):
        repaired.append("vec_chunks")

    if repaired:
        logger.warning(
            f"[DB] 已自动修复 {len(repaired)} 个表: {', '.join(repaired)}。"
            f"知识库需要「全量重建索引」以恢复数据完整性。\n"
            f"修复明细: FTS={'已修复' if 'kb_chunks_fts' in repaired else '正常'} "
            f"向量={'已清理' if 'vec_chunks' in repaired else '正常'}"
        )
    elif not integrity_ok:
        logger.warning(
            f"[DB] 主库 B-tree 损坏但 FTS/vec 正常。"
            f"建议手动执行修复: sqlite3 lumneo.db \"pragma integrity_check\""
        )
    else:
        logger.info("[DB] 所有检测通过，数据库健康")


async def _probe_fts_write(db) -> bool:
    """
    FTS5 写操作探活：INSERT 一条傀儡行 → DELETE 它。
    如果这过程中抛异常，说明 FTS5 影子表已损坏，需要修复。

    返回 True 表示损坏，False 表示正常。
    """
    try:
        # 先确认 kb_chunks_fts 表存在
        row = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='kb_chunks_fts'"
        )
        if not await row.fetchone():
            return False  # 表不存在（全新数据库），无需修复

        # 用一个不存在的 rowid 做 INSERT + DELETE 压力测试
        # 这会在 FTS5 影子表上产生真实的写入
        await db.execute(
            "INSERT INTO kb_chunks_fts(kb_chunks_fts, rowid, content, heading_path) "
            "VALUES ('delete', -99999, '__probe__', '')"
        )
        return False  # 写入成功，表健康
    except Exception:
        return True  # 损坏


async def _probe_vec_access(db) -> bool:
    """
    向量表访问探活：检查 vec_chunks 是否存在且可读写。
    返回 True 表示损坏，False 表示正常或不存在。
    """
    try:
        row = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vec_chunks'"
        )
        if not await row.fetchone():
            return False  # 表不存在（从未建过索引），无需修复

        # 尝试读取
        await db.execute("SELECT count(*) FROM vec_chunks")
        return False
    except Exception:
        return True  # 损坏


async def _probe_kb_chunks(db) -> bool:
    """
    kb_chunks 主表探活：先 SELECT 再尝试临时表写入。
    如果连 SELECT 都失败，说明主 B-tree 已损坏，需要 DROP + 重建。
    返回 True 表示损坏，False 表示正常。
    """
    try:
        # 检查表是否存在
        row = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='kb_chunks'"
        )
        if not await row.fetchone():
            return False  # 全新数据库

        # SELECT 探活
        await db.execute("SELECT count(*) FROM kb_chunks")

        # 更进一步：尝试创建一个临时表然后立即删除，
        # 验证 SQLite 的写入路径没有损坏
        await db.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _db_probe (x INTEGER)"
        )
        await db.execute("INSERT INTO _db_probe VALUES (1)")
        await db.execute("DROP TABLE _db_probe")
        return False
    except Exception:
        return True


async def _try_repair_kb_tables(db) -> bool:
    """
    重建 KB 相关表：kb_chunks + kb_index_meta + kb_chunks_fts。
    这是最彻底的修复——DROP 所有知识库表 → 重新 CREATE。
    数据会丢失，但全量重建索引可以恢复。

    返回 True 表示已修复，False 表示修复失败。
    """
    logger.warning("[DB] kb_chunks 主表损坏，重建 KB 表结构...")
    try:
        # 先尝试 DROP（可能失败，用 try 逐个处理）
        for tbl in ("kb_chunks_fts", "kb_chunks", "kb_index_meta"):
            try:
                # FTS5 用 DROP TABLE；普通表也可以用
                await db.execute(f"DROP TABLE IF EXISTS {tbl}")
            except Exception:
                # 极端损坏：DROP 也失败 → 尝试直接删 sqlite_master 记录
                try:
                    await db.execute(
                        "DELETE FROM sqlite_master WHERE type='table' AND name=?",
                        (tbl,),
                    )
                except Exception:
                    pass

        # 重建触发器（先删后建，避免残留）
        for trig in ("kb_chunks_fts_ai", "kb_chunks_fts_ad", "kb_chunks_fts_au"):
            try:
                await db.execute(f"DROP TRIGGER IF EXISTS {trig}")
            except Exception:
                pass

        # 重建 kb_chunks
        await db.execute("""
            CREATE TABLE IF NOT EXISTS kb_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                heading_path TEXT DEFAULT '',
                content TEXT NOT NULL,
                file_hash TEXT NOT NULL,
                model_name TEXT NOT NULL,
                chunk_type TEXT NOT NULL DEFAULT 'text',
                page_number INTEGER DEFAULT NULL,
                citation_id TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_kb_chunks_file ON kb_chunks(file_path)"
        )

        # 重建 kb_index_meta
        await db.execute("""
            CREATE TABLE IF NOT EXISTS kb_index_meta (
                file_path TEXT PRIMARY KEY,
                file_hash TEXT NOT NULL,
                mtime REAL NOT NULL,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                ocr_text TEXT DEFAULT '',
                image_description TEXT DEFAULT '',
                indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 重建 kb_chunks_fts
        await db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks_fts USING fts5(
                content, heading_path, content='kb_chunks', content_rowid='id'
            )
        """)
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS kb_chunks_fts_ai AFTER INSERT ON kb_chunks BEGIN
                INSERT INTO kb_chunks_fts(rowid, content, heading_path)
                VALUES (new.id, new.content, new.heading_path);
            END
        """)
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS kb_chunks_fts_ad AFTER DELETE ON kb_chunks BEGIN
                INSERT INTO kb_chunks_fts(kb_chunks_fts, rowid, content, heading_path)
                VALUES ('delete', old.id, old.content, old.heading_path);
            END
        """)
        await db.execute("""
            CREATE TRIGGER IF NOT EXISTS kb_chunks_fts_au AFTER UPDATE ON kb_chunks BEGIN
                INSERT INTO kb_chunks_fts(kb_chunks_fts, rowid, content, heading_path)
                VALUES ('delete', old.id, old.content, old.heading_path);
                INSERT INTO kb_chunks_fts(rowid, content, heading_path)
                VALUES (new.id, new.content, new.heading_path);
            END
        """)

        logger.info("[DB] KB 表结构重建完成（kb_chunks + kb_index_meta + kb_chunks_fts）")
        return True
    except Exception as e:
        logger.error(f"[DB] KB 表结构重建失败: {e}")
        return False


async def _try_repair_fts(db) -> bool:
    """
    修复 FTS5 虚表：DROP 所有相关对象 → 重建虚表 + 触发器 → 从 kb_chunks 回填。
    返回 True 表示已修复，False 表示修复失败。
    """
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
    """
    修复 sqlite-vec 向量表：DROP vec_chunks + vec_session_memories，
    清除 kb_index_meta，下次全量重建索引时自动恢复。
    返回 True 表示已清理，False 表示修复失败。
    """
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