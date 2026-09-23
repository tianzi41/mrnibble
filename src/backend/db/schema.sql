-- 啃书先生（MrNibble）SQLite 全量 DDL（架构文档 §4.2）
-- 约定：
--   * 全部时间戳为 UTC ISO8601 字符串（如 2026-09-11T08:30:00Z）。
--   * 主键为 uuid4().hex（32 位）。
--   * 外键 ON DELETE CASCADE；向量为 float32 小端 numpy.tobytes() 存 BLOB。
--   * 本脚本幂等（CREATE ... IF NOT EXISTS），可重复执行。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- ── 资料库 ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS documents (
  id            TEXT PRIMARY KEY,
  title         TEXT NOT NULL,
  source_type   TEXT NOT NULL DEFAULT 'file',    -- file | url
  fmt           TEXT NOT NULL,                   -- pdf|docx|pptx|md|txt|html
  original_path TEXT,                            -- data/files 下相对路径（source_type=file）
  source_url    TEXT,                            -- source_type=url
  file_hash     TEXT,                            -- sha256，用于去重
  size_bytes    INTEGER,
  page_count    INTEGER DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'pending', -- pending|parsing|ready|failed
  error         TEXT,
  warning       TEXT,                            -- 解析告警（如扫描版 PDF 无文本层）
  collection    TEXT,                            -- 预留分组（单一库 + 可选标签，见 §14/B）
  tags          TEXT,                            -- JSON 数组
  meta          TEXT,                            -- JSON 扩展
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_collection ON documents(collection);
CREATE INDEX IF NOT EXISTS idx_documents_status     ON documents(status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_hash ON documents(file_hash) WHERE file_hash IS NOT NULL;

-- ── 切片（含向量 BLOB；rowid 供 FTS 外部内容映射）──────
CREATE TABLE IF NOT EXISTS chunks (
  rowid           INTEGER PRIMARY KEY,           -- 隐式整数主键
  id              TEXT NOT NULL UNIQUE,
  document_id     TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ordinal         INTEGER NOT NULL,
  text            TEXT NOT NULL,
  text_seg        TEXT NOT NULL,                 -- 逐字 CJK 索引串（见 utils/textutil.build_index_text）
  token_count     INTEGER,
  page_no         INTEGER,                       -- 页码/幻灯片序号：引用的唯一来源
  section         TEXT,                          -- 章节/标题路径
  anchor          TEXT,                          -- JSON：{char_start,char_end,slide,heading}
  embedding       BLOB,                          -- float32 小端
  embedding_dim   INTEGER,
  embedding_model TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_document     ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_doc_page     ON chunks(document_id, page_no);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text_seg,
  content='chunks',
  content_rowid='rowid',
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS trg_chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, text_seg) VALUES (new.rowid, new.text_seg);
END;
CREATE TRIGGER IF NOT EXISTS trg_chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text_seg) VALUES ('delete', old.rowid, old.text_seg);
END;
CREATE TRIGGER IF NOT EXISTS trg_chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text_seg) VALUES ('delete', old.rowid, old.text_seg);
  INSERT INTO chunks_fts(rowid, text_seg) VALUES (new.rowid, new.text_seg);
END;

-- ── 会话与消息 ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS conversations (
  id           TEXT PRIMARY KEY,
  title        TEXT,
  mode         TEXT NOT NULL DEFAULT 'normal',   -- normal | guided
  document_ids TEXT,                             -- JSON 数组；NULL=全库
  collection   TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_updated ON conversations(updated_at);

CREATE TABLE IF NOT EXISTS messages (
  id              TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role            TEXT NOT NULL,                 -- user | assistant | system
  content         TEXT NOT NULL,                 -- 展示用 Markdown（引用已替换为 [n]）
  content_json    TEXT,                          -- 结构化载荷（引导式字段等）
  citations       TEXT,                          -- JSON 数组，见 §8
  grounded        INTEGER DEFAULT 1,             -- 0=含材料外内容
  model           TEXT,
  tokens          INTEGER,
  status          TEXT DEFAULT 'ok',
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, created_at);

-- ── 长期记忆（硬删除；召回 JOIN 主表，见 §9）──────────
CREATE TABLE IF NOT EXISTS memories (
  rowid             INTEGER PRIMARY KEY,
  id                TEXT NOT NULL UNIQUE,
  type              TEXT NOT NULL,               -- preference|progress|knowledge_gap|fact
  content           TEXT NOT NULL,
  content_seg       TEXT NOT NULL,               -- 逐字 CJK 索引串
  source            TEXT NOT NULL DEFAULT 'auto',-- auto|manual
  confidence        REAL DEFAULT 0.5,
  document_id       TEXT,
  conversation_id   TEXT,
  collection        TEXT,
  embedding         BLOB,
  embedding_dim     INTEGER,
  embedding_model   TEXT,
  recall_count      INTEGER DEFAULT 0,
  last_referenced_at TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type);
CREATE INDEX IF NOT EXISTS idx_memories_collection ON memories(collection);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  content_seg,
  content='memories',
  content_rowid='rowid',
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS trg_mem_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, content_seg) VALUES (new.rowid, new.content_seg);
END;
CREATE TRIGGER IF NOT EXISTS trg_mem_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content_seg) VALUES ('delete', old.rowid, old.content_seg);
END;
CREATE TRIGGER IF NOT EXISTS trg_mem_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content_seg) VALUES ('delete', old.rowid, old.content_seg);
  INSERT INTO memories_fts(rowid, content_seg) VALUES (new.rowid, new.content_seg);
END;

-- ── 生成产物 ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS generations (
  id           TEXT PRIMARY KEY,
  type         TEXT NOT NULL,                    -- cheatsheet|notes|mindmap|quiz|flashcard
  title        TEXT,
  document_ids TEXT,                             -- JSON 数组
  params       TEXT,                             -- JSON {length,count,...}
  content_md   TEXT,                             -- 速查表/笔记/导图（Markdown）
  content_json TEXT,                             -- quiz:[...]/flashcard:[...]/mindmap tree
  collection   TEXT,
  status       TEXT DEFAULT 'running',           -- running|ready|failed
  error        TEXT,
  model        TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_generations_type ON generations(type);

CREATE TABLE IF NOT EXISTS flashcards (
  id               TEXT PRIMARY KEY,
  generation_id    TEXT REFERENCES generations(id) ON DELETE CASCADE,
  document_id      TEXT,
  question         TEXT NOT NULL,
  answer           TEXT NOT NULL,
  tags             TEXT,                         -- JSON 数组
  sr_state         TEXT,                         -- JSON {ease,interval,due,reps,lapses}
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  last_reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_flashcards_generation ON flashcards(generation_id);

-- ── 配置（密钥加密存储）───────────────────────────────
CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT,
  is_secret  INTEGER DEFAULT 0,
  updated_at TEXT NOT NULL
);

-- ── 课程域（P0：课程生成 → 白板讲义 → 随堂练习 → 进度）──
-- 说明：课程结构按「课程 → 单元 → 讲次」三级落库，讲次是学习的最小单位；
-- 练习题与作答分别落 practice_questions / practice_attempts，保证可复算进度。
CREATE TABLE IF NOT EXISTS courses (
  id          TEXT PRIMARY KEY,
  title       TEXT NOT NULL,
  goal        TEXT NOT NULL DEFAULT '',           -- 学习目标（用户原话）
  level       TEXT NOT NULL DEFAULT 'beginner',   -- beginner|intermediate|advanced
  depth       TEXT NOT NULL DEFAULT 'standard',   -- brief|standard|detailed
  unit_count  INTEGER NOT NULL DEFAULT 3,
  language    TEXT NOT NULL DEFAULT 'zh',
  hands_on    INTEGER NOT NULL DEFAULT 1,        -- 是否包含实操环节（0=纯理论课，不出 hands_on 题）
  summary     TEXT,
  outline_json TEXT,                              -- 生成/编辑后的完整大纲快照
  status      TEXT NOT NULL DEFAULT 'drafting',   -- drafting|ready|failed
  error       TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_courses_updated ON courses(updated_at);

CREATE TABLE IF NOT EXISTS course_documents (
  course_id   TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  PRIMARY KEY (course_id, document_id)
);

CREATE TABLE IF NOT EXISTS course_units (
  id          TEXT PRIMARY KEY,
  course_id   TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  ordinal     INTEGER NOT NULL,
  title       TEXT NOT NULL,
  summary     TEXT NOT NULL DEFAULT '',
  status      TEXT NOT NULL DEFAULT 'pending',    -- pending|active|done
  summary_json TEXT,                              -- 单元总结（P1：学完后的回顾与薄弱点）
  summary_md  TEXT,
  summary_status TEXT DEFAULT 'pending',          -- pending|running|ready|failed
  summary_error  TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_units_course ON course_units(course_id, ordinal);

CREATE TABLE IF NOT EXISTS course_lessons (
  id             TEXT PRIMARY KEY,
  course_id      TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  unit_id        TEXT NOT NULL REFERENCES course_units(id) ON DELETE CASCADE,
  ordinal        INTEGER NOT NULL,                -- 单元内序号
  global_ordinal INTEGER NOT NULL,                -- 全课序号（决定学习顺序）
  kind           TEXT NOT NULL DEFAULT 'lecture',-- lecture|practice|project
  title          TEXT NOT NULL,
  objective      TEXT NOT NULL DEFAULT '',
  depth          TEXT NOT NULL DEFAULT 'standard',
  status         TEXT NOT NULL DEFAULT 'pending', -- pending|lecture_ready|practicing|done
  board_json     TEXT,                            -- 讲义白板（cards/outline/keypoints/summary，兼容旧数据）
  board_md       TEXT,                            -- 白板渲染后的 Markdown
  slides_json    TEXT,                            -- JSON：学生课件页（面向展示，短内容）
  script_json    TEXT,                            -- JSON：讲师讲稿（面向朗读，按 slide_id 关联课件）
  board_marks    TEXT,                            -- JSON：材料标注（高亮/圈注/连线旁注，见 P1）
  citations      TEXT,                            -- JSON 数组（同 §9 结构）
  conversation_id TEXT,                           -- 课堂对话
  model          TEXT,
  error          TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lessons_course ON course_lessons(course_id, global_ordinal);
CREATE INDEX IF NOT EXISTS idx_lessons_unit   ON course_lessons(unit_id, ordinal);

CREATE TABLE IF NOT EXISTS practice_questions (
  id            TEXT PRIMARY KEY,
  lesson_id     TEXT NOT NULL REFERENCES course_lessons(id) ON DELETE CASCADE,
  ordinal       INTEGER NOT NULL,
  type          TEXT NOT NULL,                    -- single|boolean|fill_in|hands_on|open
  stem          TEXT NOT NULL,
  options       TEXT,                             -- JSON 数组（single/boolean）
  answer        TEXT NOT NULL,                    -- JSON：single/boolean=下标；fill_in=可接受答案数组；open=参考答案
  explanation   TEXT NOT NULL DEFAULT '',
  image         TEXT,                             -- JSON：{document_id,page_no}（P1 图片题）
  source        TEXT,                             -- JSON：{page_no,document_title,...}
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pq_lesson ON practice_questions(lesson_id, ordinal);

CREATE TABLE IF NOT EXISTS practice_attempts (
  id         TEXT PRIMARY KEY,
  lesson_id  TEXT NOT NULL REFERENCES course_lessons(id) ON DELETE CASCADE,
  question_id TEXT NOT NULL REFERENCES practice_questions(id) ON DELETE CASCADE,
  attempt_no INTEGER NOT NULL DEFAULT 1,          -- 第几次作答（同一讲次可重做）
  answer     TEXT NOT NULL,                       -- JSON：下标或文本
  correct    INTEGER NOT NULL,
  score      REAL NOT NULL DEFAULT 0,
  feedback   TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pa_lesson ON practice_attempts(lesson_id);

CREATE TABLE IF NOT EXISTS course_errors (
  id         TEXT PRIMARY KEY,
  course_id  TEXT REFERENCES courses(id) ON DELETE CASCADE,
  lesson_id  TEXT REFERENCES course_lessons(id) ON DELETE CASCADE,
  question   TEXT NOT NULL DEFAULT '',
  detail     TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cerr_course ON course_errors(course_id);

CREATE TABLE IF NOT EXISTS course_jobs (
  id         TEXT PRIMARY KEY,
  course_id  TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  lesson_id  TEXT,
  kind       TEXT NOT NULL,                       -- outline|lecture|practice
  stage      TEXT NOT NULL DEFAULT 'queued',      -- 生成阶段文案
  status     TEXT NOT NULL DEFAULT 'running',     -- running|ready|failed
  error      TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cjobs_course ON course_jobs(course_id, created_at);

-- ── 运行事件（可观测，R-F04）───────────────────────────
CREATE TABLE IF NOT EXISTS events (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      TEXT NOT NULL,
  kind    TEXT NOT NULL,                         -- guided_state|retrieval|llm_call
  payload TEXT                                   -- JSON（已脱敏）
);
CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events(kind, ts);
