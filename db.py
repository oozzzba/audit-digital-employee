"""数据库持久化层（SQLite，标准库 sqlite3，零外部依赖）。

P0 目标：把每个会话的 Orchestrator 项目上下文（ctx）整体快照落盘，进程重启后恢复，
项目可继续推进；六阶段产出以「文档注册表」单独落一张表，便于按阶段/类型查询与留痕；
交互式全流程游标（idx/task）与会话对话流水也一并持久化。

表（与 schema.sql 的关系）：
  audit_project   每会话一行：state_json 快照 + 反范式查询字段（project_name / audit_type /
                  current_stage / current_role / project_status）
  session_doc     六阶段产出文档注册表（doc_type + stage + payload_json）
  pipeline_state  交互式全流程游标（idx / task）
  chat_message    会话对话流水（粗粒度审计轨迹）

说明：SQLite 仅作 P0「重启不丢状态」的落地形态；多实例/上生产时按 schema.sql 切换 MySQL，
本模块对外接口保持不变。
"""
import json
from difflib import SequenceMatcher
import os
import sqlite3
import threading
from datetime import datetime

DB_PATH = os.getenv("AUDIT_DB_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "audit.db"
)
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_project (
    session_id     TEXT PRIMARY KEY,
    project_name   TEXT DEFAULT '',
    audit_type     TEXT DEFAULT '',
    current_stage  TEXT DEFAULT '',
    current_role   TEXT DEFAULT '',
    project_status TEXT DEFAULT 'draft',
    state_json     TEXT NOT NULL,
    created_at     TEXT,
    updated_at     TEXT
);
CREATE TABLE IF NOT EXISTS session_doc (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    doc_type     TEXT NOT NULL,
    stage        TEXT DEFAULT '',
    payload_json TEXT,
    updated_at   TEXT,
    UNIQUE(session_id, doc_type)
);
CREATE INDEX IF NOT EXISTS idx_doc_session ON session_doc(session_id);
CREATE TABLE IF NOT EXISTS pipeline_state (
    session_id TEXT PRIMARY KEY,
    idx        INTEGER DEFAULT 1,
    task       TEXT DEFAULT '',
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS chat_message (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT DEFAULT '',
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_message(session_id, seq);
CREATE TABLE IF NOT EXISTS approval_record (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    condition    TEXT NOT NULL,
    role         TEXT NOT NULL,
    actor_id     TEXT NOT NULL,
    decision     TEXT NOT NULL,
    opinion      TEXT DEFAULT '',
    created_at   TEXT NOT NULL,
    UNIQUE(session_id, condition, role, decision)
);
CREATE INDEX IF NOT EXISTS idx_approval_session ON approval_record(session_id, created_at);
CREATE TABLE IF NOT EXISTS admin_agent_config (
    agent_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    stage TEXT NOT NULL,
    capability TEXT DEFAULT '',
    knowledge_base TEXT DEFAULT '',
    model TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'enabled',
    version INTEGER NOT NULL DEFAULT 1,
    config_json TEXT DEFAULT '{}',
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_asset (
    asset_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    source TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    version INTEGER NOT NULL DEFAULT 1,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_asset_status ON knowledge_asset(status);
CREATE TABLE IF NOT EXISTS audit_event (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_event_resource ON audit_event(resource_type, resource_id, created_at);
CREATE TABLE IF NOT EXISTS prompt_config (
    prompt_id TEXT PRIMARY KEY,
    prompt_key TEXT NOT NULL,
    name TEXT NOT NULL,
    agent_key TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    version INTEGER NOT NULL DEFAULT 1,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prompt_key ON prompt_config(prompt_key, version);
CREATE TABLE IF NOT EXISTS draft_artifact (
    artifact_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, artifact_type TEXT NOT NULL,
    stage TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'draft',
    payload_json TEXT NOT NULL, input_hash TEXT DEFAULT '', prompt_version TEXT DEFAULT '',
    created_by TEXT DEFAULT 'system', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_draft_session ON draft_artifact(session_id, stage, artifact_type);
CREATE TABLE IF NOT EXISTS agent_run (
    run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, stage TEXT NOT NULL, agent_id TEXT NOT NULL,
    trace_id TEXT NOT NULL, prompt_version TEXT DEFAULT '', input_hash TEXT DEFAULT '',
    output_hash TEXT DEFAULT '', status TEXT NOT NULL, duration_ms INTEGER DEFAULT 0,
    error_code TEXT DEFAULT '', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_run_session ON agent_run(session_id, created_at);
CREATE TABLE IF NOT EXISTS tool_call (
    call_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, trace_id TEXT NOT NULL, tool_name TEXT NOT NULL,
    request_hash TEXT DEFAULT '', result_status TEXT NOT NULL, duration_ms INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS field_change_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, artifact_id TEXT DEFAULT '',
    field_name TEXT NOT NULL, old_value TEXT, new_value TEXT, source_type TEXT DEFAULT '',
    actor_id TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _connect():
    # busy_timeout：多连接/多进程并发写时，等锁而非立即报「database is locked」
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _lock:
        conn = _connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()


# 模块直接被工具/测试导入时也自动执行轻量 schema migration。
init_db()


# ---- 序列化：ctx 含 set（approved）与各类 JSON 值 ----

def serialize_ctx(ctx):
    """把 ctx 转成可落盘的 JSON 字符串（approved 的 set → 有序 list）。"""
    state = dict(ctx)
    if isinstance(state.get("approved"), set):
        state["approved"] = sorted(state["approved"])
    return json.dumps(state, ensure_ascii=False, default=str)


def deserialize_ctx(raw):
    """从 JSON 字符串还原 ctx（approved 的 list → set）。"""
    state = json.loads(raw)
    state["approved"] = set(state.get("approved") or [])
    state.setdefault("notices", [])
    return state


# ---- 项目快照 ----

def save_project(session_id, *, ctx, current_role, current_stage, project_status,
                 project_name="", audit_type=""):
    now = _now()
    state_json = serialize_ctx(ctx)
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO audit_project"
                " (session_id, project_name, audit_type, current_stage, current_role,"
                "  project_status, state_json, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                " project_name=excluded.project_name, audit_type=excluded.audit_type,"
                " current_stage=excluded.current_stage, current_role=excluded.current_role,"
                " project_status=excluded.project_status, state_json=excluded.state_json,"
                " updated_at=excluded.updated_at",
                (session_id, project_name, audit_type, current_stage, current_role,
                 project_status, state_json, now, now),
            )
            conn.commit()
        finally:
            conn.close()


def load_project(session_id):
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT session_id, project_name, audit_type, current_stage, current_role,"
                " project_status, state_json, created_at, updated_at"
                " FROM audit_project WHERE session_id=?", (session_id,)).fetchone()
        finally:
            conn.close()
    return dict(row) if row else None


def count_projects():
    with _lock:
        conn = _connect()
        try:
            return conn.execute("SELECT COUNT(*) AS c FROM audit_project").fetchone()["c"]
        finally:
            conn.close()


def find_duplicate_projects(project_name, project_year, audit_object):
    """代码侧精确重复校验，不交给模型判断。"""
    name, year, obj = (str(project_name or '').strip(), str(project_year or '').strip(), str(audit_object or '').strip())
    if not name or not year or not obj or any(x in (name, year, obj) for x in ('待补充', '待补全')):
        return []
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT session_id, state_json FROM audit_project").fetchall()
        finally:
            conn.close()
    matches = []
    for row in rows:
        try:
            state = json.loads(row['state_json'] or '{}')
            form = state.get('info_form') or {}
            parsed = json.loads(state.get('parsed') or '{}') if isinstance(state.get('parsed'), str) else (state.get('parsed') or {})
            existing_obj = str(form.get('被审计单位名称') or parsed.get('审计对象') or '').strip()
            if (str(form.get('项目名称') or '').strip() == name and
                    str(form.get('项目年度') or '').strip() == year and existing_obj == obj):
                matches.append({'session_id': row['session_id'], 'project_name': name, 'project_year': year, 'audit_object': obj})
        except Exception:
            continue
    return matches


def load_latest_initiation_project(exclude_session_id=None):
    """返回项目台中最近更新的立项项目及其快照，用于复制配置生成新项目草稿。"""
    with _lock:
        conn = _connect()
        try:
            query = "SELECT session_id, project_name, state_json, updated_at FROM audit_project"
            params = []
            if exclude_session_id:
                query += " WHERE session_id<>?"
                params.append(exclude_session_id)
            query += " ORDER BY datetime(updated_at) DESC"
            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()
    for row in rows:
        try:
            state = json.loads(row["state_json"] or "{}")
        except Exception:
            state = {}
        form = state.get("info_form") or {}
        project_name = str(form.get("项目名称") or row["project_name"] or "").strip()
        if project_name and project_name not in ("待补全", "未明确"):
            return {"session_id": row["session_id"], "project_name": project_name, "updated_at": row["updated_at"], "state": state}
    return None


def find_latest_plan_demand(project_name="", exclude_session_id=None):
    """检索系统已保存的审计计划需求表，供年度计划立项快捷指令优先复用。"""
    query = "SELECT session_id, state_json, updated_at FROM audit_project"
    params = []
    if exclude_session_id:
        query += " WHERE session_id<>?"
        params.append(exclude_session_id)
    query += " ORDER BY datetime(updated_at) DESC"
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()
    target = ''.join(ch for ch in str(project_name or '') if ch.isalnum() or '\u4e00' <= ch <= '\u9fff')
    for row in rows:
        try:
            state = json.loads(row['state_json'] or '{}')
        except Exception:
            continue
        fields = state.get('plan_demand_fields') or {}
        if not fields:
            continue
        name = str(fields.get('计划名称') or fields.get('项目名称') or '').strip()
        compact = ''.join(ch for ch in name if ch.isalnum() or '\u4e00' <= ch <= '\u9fff')
        if target and compact and target not in compact and compact not in target:
            continue
        return {'session_id': row['session_id'], 'fields': fields, 'updated_at': row['updated_at']}
    return None


def find_similar_projects(project_name, project_year=None, audit_object=None, *, limit=10, exclude_session_id=None):
    """按项目名称预检已建项目，返回同名及名称相似项目详情。

    这是创建项目之前的代码侧校验；不依赖模型推理，也不修改任何项目数据。
    """
    name = str(project_name or '').strip()
    year = str(project_year or '').strip()
    obj = str(audit_object or '').strip()
    if not name or name in ('待补充', '待补全', '未明确'):
        return []
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT session_id, project_name, current_stage, project_status, state_json, updated_at FROM audit_project").fetchall()
        finally:
            conn.close()
    def compact(value):
        return ''.join(ch for ch in str(value or '').lower() if ch.isalnum() or '\u4e00' <= ch <= '\u9fff')
    query = compact(name)
    results = []
    for row in rows:
        if exclude_session_id and row['session_id'] == exclude_session_id:
            continue
        try:
            state = json.loads(row['state_json'] or '{}')
        except Exception:
            state = {}
        form = state.get('info_form') or {}
        existing_name = str(form.get('项目名称') or row['project_name'] or '').strip()
        existing_year = str(form.get('项目年度') or '').strip()
        existing_obj = str(form.get('被审计单位名称') or '').strip()
        candidate = compact(existing_name)
        if not candidate:
            continue
        same_name = candidate == query
        same_year = bool(year and existing_year and year == existing_year)
        ratio = SequenceMatcher(None, query, candidate).ratio()
        # 同名必返回；同年度且名称有明显重合，或编辑距离相近，也列为相似项目。
        shared = len(set(query) & set(candidate)) / max(1, len(set(query) | set(candidate)))
        if not (same_name or (same_year and (ratio >= 0.42 or shared >= 0.45)) or ratio >= 0.72):
            continue
        phase = row['current_stage'] or state.get('phase') or 'initiation'
        results.append({
            'session_id': row['session_id'], 'project_name': existing_name,
            'project_year': existing_year or '未明确', 'audit_object': existing_obj or '未明确',
            'current_stage': phase, 'project_status': row['project_status'] or '草稿',
            'project_code': form.get('项目编号') or state.get('project_code') or '编号待生成',
            'project_leader': form.get('项目组长') or form.get('项目负责人') or '未明确',
            'updated_at': row['updated_at'], 'match_type': '同名项目' if same_name else '相似项目',
            'match_score': round(1.0 if same_name else ratio, 2), 'form': form,
        })
    results.sort(key=lambda item: (item['match_type'] != '同名项目', -item['match_score']))
    return results[:limit]


def save_draft_artifact(session_id, artifact_id, artifact_type, stage, payload, *, version=1,
                        input_hash="", prompt_version="", created_by="system"):
    now = _now()
    with _lock:
        conn = _connect()
        try:
            conn.execute("""INSERT INTO draft_artifact
                (artifact_id,session_id,artifact_type,stage,version,status,payload_json,input_hash,prompt_version,created_by,created_at,updated_at)
                VALUES (?,?,?,?,?,'draft',?,?,?,?,?,?)
                ON CONFLICT(artifact_id) DO UPDATE SET version=excluded.version,payload_json=excluded.payload_json,
                input_hash=excluded.input_hash,prompt_version=excluded.prompt_version,updated_at=excluded.updated_at""",
                (artifact_id, session_id, artifact_type, stage, version, json.dumps(payload, ensure_ascii=False, default=str), input_hash, prompt_version, created_by, now, now))
            conn.commit()
        finally:
            conn.close()


def append_agent_run(run_id, session_id, stage, agent_id, trace_id, status, *, input_hash="", output_hash="", duration_ms=0, error_code=""):
    with _lock:
        conn = _connect()
        try:
            conn.execute("INSERT OR REPLACE INTO agent_run(run_id,session_id,stage,agent_id,trace_id,input_hash,output_hash,status,duration_ms,error_code,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (run_id, session_id, stage, agent_id, trace_id, input_hash, output_hash, status, duration_ms, error_code, _now()))
            conn.commit()
        finally:
            conn.close()


def append_field_change(session_id, field_name, old_value, new_value, source_type, actor_id, artifact_id=""):
    with _lock:
        conn = _connect()
        try:
            conn.execute("INSERT INTO field_change_log(session_id,artifact_id,field_name,old_value,new_value,source_type,actor_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
                         (session_id, artifact_id, field_name, str(old_value or ''), str(new_value or ''), source_type, actor_id, _now()))
            conn.commit()
        finally:
            conn.close()


# ---- 六阶段产出文档注册 ----

def save_docs(session_id, docs):
    """docs: list[(doc_type, stage, payload)]，payload 为 JSON 可序列化值。"""
    if not docs:
        return
    now = _now()
    with _lock:
        conn = _connect()
        try:
            for doc_type, stage, payload in docs:
                conn.execute(
                    "INSERT INTO session_doc(session_id, doc_type, stage, payload_json, updated_at)"
                    " VALUES (?,?,?,?,?)"
                    " ON CONFLICT(session_id, doc_type) DO UPDATE SET"
                    " stage=excluded.stage, payload_json=excluded.payload_json, updated_at=excluded.updated_at",
                    (session_id, doc_type, stage,
                     json.dumps(payload, ensure_ascii=False, default=str), now),
                )
            conn.commit()
        finally:
            conn.close()


# ---- 交互式全流程游标 ----

def save_pipeline(session_id, idx, task):
    now = _now()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO pipeline_state(session_id, idx, task, updated_at) VALUES (?,?,?,?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                " idx=excluded.idx, task=excluded.task, updated_at=excluded.updated_at",
                (session_id, idx, task, now),
            )
            conn.commit()
        finally:
            conn.close()


def load_pipeline(session_id):
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT idx, task FROM pipeline_state WHERE session_id=?", (session_id,)).fetchone()
        finally:
            conn.close()
    return {"idx": row["idx"], "task": row["task"]} if row else None


# ---- 会话对话流水（粗粒度审计轨迹） ----

def append_chat(session_id, role, content):
    now = _now()
    with _lock:
        conn = _connect()
        try:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM chat_message WHERE session_id=?",
                (session_id,)).fetchone()["s"]
            conn.execute(
                "INSERT INTO chat_message(session_id, seq, role, content, created_at) VALUES (?,?,?,?,?)",
                (session_id, seq, role, content, now),
            )
            conn.commit()
        finally:
            conn.close()


def load_chat(session_id, limit=200):
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT seq, role, content, created_at FROM chat_message"
                " WHERE session_id=? ORDER BY seq DESC LIMIT ?", (session_id, limit)).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in reversed(rows)]


def save_approval(session_id, condition, role, actor_id, decision, opinion=""):
    """持久化一次会签动作；同一会话/条件/角色/决定幂等。"""
    now = _now()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO approval_record"
                " (session_id, condition, role, actor_id, decision, opinion, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (session_id, condition, role, actor_id, decision, opinion, now),
            )
            conn.commit()
        finally:
            conn.close()


def load_approvals(session_id):
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT condition, role, actor_id, decision, opinion, created_at"
                " FROM approval_record WHERE session_id=? ORDER BY created_at, id",
                (session_id,),
            ).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def upsert_agent_config(item):
    now = _now()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO admin_agent_config(agent_id,name,stage,capability,knowledge_base,model,status,version,config_json,updated_by,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(agent_id) DO UPDATE SET"
                " name=excluded.name,stage=excluded.stage,capability=excluded.capability,knowledge_base=excluded.knowledge_base,model=excluded.model,status=excluded.status,version=admin_agent_config.version+1,config_json=excluded.config_json,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                (item["agent_id"], item["name"], item["stage"], item.get("capability", ""), item.get("knowledge_base", ""), item.get("model", ""), item.get("status", "enabled"), item.get("version", 1), item.get("config_json", "{}"), item["updated_by"], now),
            )
            conn.commit()
        finally:
            conn.close()


def load_agent_configs():
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM admin_agent_config ORDER BY stage, agent_id").fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def upsert_knowledge_asset(item):
    now = _now()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO knowledge_asset(asset_id,title,category,source,content,status,version,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(asset_id) DO UPDATE SET title=excluded.title,category=excluded.category,source=excluded.source,content=excluded.content,status=excluded.status,version=knowledge_asset.version+1,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                (item["asset_id"], item["title"], item["category"], item["source"], item["content"], item.get("status", "pending"), item.get("version", 1), item["updated_by"], now),
            )
            conn.commit()
        finally:
            conn.close()


def load_knowledge_assets():
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM knowledge_asset ORDER BY updated_at DESC").fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def delete_knowledge_asset(asset_id):
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM knowledge_asset WHERE asset_id=?", (asset_id,))
            conn.commit()
        finally:
            conn.close()


def append_audit_event(actor_id, action, resource_type, resource_id, detail=None):
    now = _now()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO audit_event(actor_id,action,resource_type,resource_id,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                (actor_id, action, resource_type, resource_id, json.dumps(detail or {}, ensure_ascii=False), now),
            )
            conn.commit()
        finally:
            conn.close()


def load_audit_events(limit=200):
    limit = min(max(int(limit), 1), 1000)
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM audit_event ORDER BY event_id DESC LIMIT ?", (limit,)).fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]


def upsert_prompt(item):
    now = _now()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO prompt_config(prompt_id,prompt_key,name,agent_key,content,status,version,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(prompt_id) DO UPDATE SET content=excluded.content,status=excluded.status,version=prompt_config.version+1,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                (item["prompt_id"], item["prompt_key"], item["name"], item["agent_key"], item["content"], item.get("status", "draft"), item.get("version", 1), item["updated_by"], now),
            )
            conn.commit()
        finally:
            conn.close()


def load_prompts():
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM prompt_config ORDER BY prompt_key, version DESC").fetchall()
        finally:
            conn.close()
    return [dict(r) for r in rows]
