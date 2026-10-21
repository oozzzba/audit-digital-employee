"""审计数字员工 · Web 服务层（FastAPI）

把命令行入口（main.py）之外，再开一扇浏览器窗口：
  - 登录 → 审计任务工作台（六阶段对话流 + 人工闸门 + 结果溯源）
  - 项目看板 / 问题清单 / 审计报告 / 整改督办 四个结果页，读同一份项目上下文

会话隔离：每个浏览器会话一个 Orchestrator（独立 ctx），互不串台。

启动：
    python3 app.py
    然后浏览器打开 http://127.0.0.1:8000
"""
import json
import logging
import os
import re
import uuid
import base64
import hashlib
import hmac
import time

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from orchestrator import (
    Orchestrator, STAGE_ORDER, STAGE_NAMES, STAGE_OUTPUT_KEY, FLOW, STAGE_PRECONDITIONS, FINAL_GATE,
    _cond_name, _cond_role,
)
from roles import ROLES, DEFAULT_ROLE, role_name
from generator import (LLMError, TASK_PARSE_SYSTEM, PROJECT_FORM_SYSTEM, RISK_SYSTEM, WORKPAPER_SYSTEM,
                       RECTIFICATION_SYSTEM, INITIATION_SYSTEM, AUDIT_PLAN_SYSTEM, AUDIT_NOTICE_SYSTEM,
                       ARCHIVE_SYSTEM, PRE_SURVEY_SYSTEM, SEEK_OPINION_SYSTEM, OVERDUE_SUPERVISE_SYSTEM,
                       FILE_MATERIALS_SYSTEM, STAGE_DETECT_SYSTEM)
from generator import INITIATION_TRACE_SYSTEM, PREAUDIT_RISK_SYSTEM
import db
import replies

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("audit")
from docs import render_info_form, render_plan_demand_form
from data_tool import DATA_SOURCES
from ledger_data import load_ledger, summarize, filter_rows, category_options
from problem_data import (
    load_problems,
    summarize as problems_summarize,
    filter_options as problems_options,
    filter_problems,
)
from function_tools import FUNCTION_HANDLERS, guard_function_stage
from agent_spec import AGENT_SPEC
from retriever import knowledge_manifest
from tool_contract import validate, envelope, SCHEMA_VERSION
from tool_catalog import dispatch as tool_dispatch, TOOL_SPECS, tool_families, reload_retriever

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="审计数字员工", docs_url=None, redoc_url=None)


def _demo_mode():
    """仅显式 DEMO_MODE=true 才允许演示放行；默认按生产闸门执行。"""
    return os.getenv("DEMO_MODE", "false").lower() == "true"


def _requested_project_name(text):
    """从创建项目指令中提取名称，用于创建前的代码侧同名/相似预检。"""
    value = str(text or "").strip()
    named = re.search(r"项目名称\s*(?:是|为|：|:)\s*[“\"「]?([^”\"」,，。\n]+)", value)
    if named:
        return named.group(1).strip()
    value = re.sub(r"^(?:帮我|请|我要|希望|需要)?\s*(?:创建|新建|发起|完成|进行|开展|做|建立)\s*(?:一个新的?|一项)?\s*", "", value)
    value = re.sub(r"\s*(?:审计)?项目?立项\s*$", "", value)
    return value.strip(" ：:，,。")


def _creation_precheck(text, session_id):
    """创建前预检：同名或相似项目命中即暂停新建，交给人工选择。"""
    name = _requested_project_name(text)
    year_match = re.search(r"(?:19|20)\d{2}", str(text or ""))
    year = year_match.group(0) if year_match else ""
    object_match = re.search(r"(?:被审计单位|审计对象)\s*(?:是|为|：|:)\s*([^，,；;。\n]+)", str(text or ""))
    audit_object = object_match.group(1).strip() if object_match else ""
    return db.find_similar_projects(name, year, audit_object, exclude_session_id=session_id)


def actor(request: Request):
    """统一读取当前操作人；生产环境应由 SSO/JWT 中间件注入，不信任前端角色切换。"""
    token = _token_payload(request)
    return {
        "actorId": str((token or {}).get("sub") or request.headers.get("x-actor-id") or "demo-user"),
        "actorRole": str((token or {}).get("role") or request.headers.get("x-actor-role") or "project_lead"),
        "name": str((token or {}).get("name") or request.headers.get("x-actor-name") or ""),
        "department": str((token or {}).get("department") or request.headers.get("x-actor-department") or os.getenv("DEFAULT_ORG", "未明确")),
    }


async def _write_admin_event(request: Request, action: str, resource_id: str, detail: dict):
    """管理写操作先复用会话流水留痕；生产迁移时应写入独立 audit_event 表。"""
    a = actor(request)
    db.append_audit_event(a["actorId"], action, "ADMIN", resource_id, detail)
    db.append_chat("admin", "system", json.dumps({"action": action, "resource_id": resource_id, "actor": a["actorId"], "detail": detail}, ensure_ascii=False))


def _token_sign(payload):
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    secret = os.getenv("AUTH_SECRET", "dev-only-change-this-secret").encode()
    sig = hmac.new(secret, raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def _token_payload(request):
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return None
    try:
        raw, sig = auth[7:].split(".", 1)
        secret = os.getenv("AUTH_SECRET", "dev-only-change-this-secret").encode()
        expected = hmac.new(secret, raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def require_admin(request: Request):
    payload = _token_payload(request)
    if payload and payload.get("role") == "system_admin":
        return payload
    if _demo_mode() and request.headers.get("x-demo-admin") == "true":
        return {"sub": "demo-admin", "role": "system_admin"}
    raise HTTPException(status_code=401, detail="请先以系统管理员身份登录")


@app.exception_handler(LLMError)
async def llm_error_handler(request: Request, exc: LLMError):
    """模型调用重试耗尽后，统一转成友好错误（503），避免裸 500 暴露内部细节。"""
    logger.error("LLM 调用失败：%s", exc)
    return JSONResponse({"err": "模型调用失败，请稍后重试（DeepSeek 服务暂不可用）。"}, status_code=503)

# 会话隔离：session_id -> Orchestrator（每个会话独立项目上下文）
SESSIONS = {}
# 交互式全流程进度：session_id -> {"idx": int, "task": str}
PIPELINE = {}

db.init_db()


def _seed_prompt_configs():
    """将代码中实际使用的 Prompt 首次登记到后台；已存在记录不覆盖。"""
    existing_rows = {p["prompt_id"]: p for p in db.load_prompts()}
    prompts = [
        ("task-parse", "任务解析系统指令", "调度中心", TASK_PARSE_SYSTEM),
        ("project-form", "项目表单抽取指令", "立项 Agent", PROJECT_FORM_SYSTEM),
        ("initiation-trace", "立项风险溯源指令", "立项 Agent", INITIATION_TRACE_SYSTEM),
        ("preaudit-risk", "审前风险提示指令", "审前 Agent", PREAUDIT_RISK_SYSTEM),
        ("risk", "风险分析系统指令", "现场实施 Agent", RISK_SYSTEM),
        ("workpaper", "底稿生成系统指令", "现场实施 Agent", WORKPAPER_SYSTEM),
        ("rectification", "整改督办系统指令", "整改 Agent", RECTIFICATION_SYSTEM),
        ("initiation", "立项生成系统指令", "立项 Agent", INITIATION_SYSTEM),
        ("audit-plan", "审计方案系统指令", "审前 Agent", AUDIT_PLAN_SYSTEM),
        ("audit-notice", "审计通知系统指令", "审前 Agent", AUDIT_NOTICE_SYSTEM),
        ("archive", "归档系统指令", "归档 Agent", ARCHIVE_SYSTEM),
        ("pre-survey", "审前调查系统指令", "审前 Agent", PRE_SURVEY_SYSTEM),
        ("seek-opinion", "征求意见系统指令", "报告 Agent", SEEK_OPINION_SYSTEM),
        ("overdue-supervise", "逾期督办系统指令", "整改 Agent", OVERDUE_SUPERVISE_SYSTEM),
        ("file-materials", "资料归档系统指令", "归档 Agent", FILE_MATERIALS_SYSTEM),
        ("stage-detect", "阶段识别系统指令", "调度中心", STAGE_DETECT_SYSTEM),
    ]
    for key, name, agent, content in prompts:
        prompt_id = "builtin-" + key
        # 仅同步系统托管的内置 Prompt；管理员自定义/编辑内容不被启动过程覆盖。
        if prompt_id in existing_rows and existing_rows[prompt_id].get("updated_by") != "system":
            continue
        db.upsert_prompt({"prompt_id": prompt_id, "prompt_key": key, "name": name, "agent_key": agent, "content": content, "status": "published", "updated_by": "system"})


_seed_prompt_configs()


def _get_orch(session_id):
    """取会话编排器：内存命中直接返回；否则从 SQLite 恢复，再否则新建。

    这样进程重启后，只要 session_id 仍在库里，项目上下文（ctx）与当前角色即可无缝续上。
    """
    if session_id in SESSIONS:
        return SESSIONS[session_id]
    orch = Orchestrator()
    row = db.load_project(session_id)
    if row and row.get("state_json"):
        try:
            orch.ctx = db.deserialize_ctx(row["state_json"])
            orch.current_role = row.get("current_role") or DEFAULT_ROLE
        except Exception:
            pass  # 快照损坏则退回全新上下文，不阻断服务
    SESSIONS[session_id] = orch
    return orch


def _safe_json(text):
    if isinstance(text, dict):
        return text
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {}


def _project_meta(orch):
    """反范式字段：项目名称 / 审计类型，仅供列表页展示，完整状态在 state_json。"""
    parsed = _safe_json(orch.ctx.get("parsed"))
    info = orch.ctx.get("info_form") or {}
    if not isinstance(info, dict):
        info = {}
    project_name = info.get("项目名称") or parsed.get("项目名称") or parsed.get("审计对象") or ""
    audit_type = parsed.get("审计类型") or ""
    return str(project_name).strip(), str(audit_type).strip()


def _project_status(orch):
    ctx = orch.ctx
    approved = ctx.get("approved", set())
    if "项目终止" in approved:
        return "terminated"
    if ctx.get("archive") and "归档审批" in approved:
        return "archived"
    if ctx.get("task"):
        return "running"
    return "draft"


def _persist(session_id, orch):
    """把 Orchestrator 状态整体落盘；失败仅告警，不阻断审计流程（内存仍可用）。"""
    try:
        name, audit_type = _project_meta(orch)
        db.save_project(
            session_id, ctx=orch.ctx, current_role=orch.current_role,
            current_stage=orch.completed() or "initiation",
            project_status=_project_status(orch),
            project_name=name, audit_type=audit_type,
        )
        # AI 产物以草稿快照持久化，避免仅驻留进程内存；正式成果仍由会签接口落库。
        for artifact_type, stage, payload in [(key, st, orch.ctx.get(key)) for st, key in STAGE_OUTPUT_KEY.items() if orch.ctx.get(key) is not None]:
            if payload in (None, "", [], {}):
                continue
            aid = f"{session_id}:{artifact_type}"
            db.save_draft_artifact(session_id, aid, artifact_type, stage, payload, created_by="system")
        docs = [
            (key, stage, orch.ctx.get(key))
            for stage, key in STAGE_OUTPUT_KEY.items()
            if orch.ctx.get(key)
        ]
        db.save_docs(session_id, docs)
    except Exception as e:
        print(f"[db] 持久化失败 session={session_id}: {e}")


def _get_pipeline(session_id):
    if session_id in PIPELINE:
        return PIPELINE[session_id]
    row = db.load_pipeline(session_id)
    if row:
        PIPELINE[session_id] = row
        return row
    return None


def _save_pipeline(session_id, state):
    PIPELINE[session_id] = state
    try:
        db.save_pipeline(session_id, state.get("idx", 1), state.get("task", ""))
    except Exception as e:
        print(f"[db] 持久化 pipeline 失败 session={session_id}: {e}")


def _log_chat(session_id, role, content):
    try:
        db.append_chat(session_id, role, content)
    except Exception:
        pass


def _is_info_msg(err):
    """保留兼容判断：回复层改造后，resolve 返回的均为友好状态提示，统一走 info 通道。"""
    return True


# ==================== 渲染层：把 ctx 转成前端可渲染的块 ====================

_STAGE_GATE = {
    "initiation": "立项完成 —— 待「立项审批通过」",
    "preaudit": "审前准备完成 —— 待「方案审批 + 通知签发」",
    "field": "现场实施完成 —— 待「问题定性确认」",
    "report": "审计报告编制完成 —— 待「报告审签发布」",
    "rectification": "问题整改完成 —— 待「整改闭环销号」（或项目终止）",
    "archive": "归档完成 —— 待「归档审批」",
}

_STAGE_NEXT = {
    "initiation": "生成审计方案（审前准备）",
    "preaudit": "继续推进（现场实施）",
    "field": "生成报告（报告编制）",
    "report": "生成整改台账（问题整改）",
    "rectification": "归档（项目归档）",
    "archive": "归档审批（项目闭环）",
}

# 单产出阶段：stage -> (ctx key, 展示标题)
_SINGLE_OUTPUT = {
    "initiation": ("initiation", "审计立项申请"),
    "report": ("report", "审计报告初稿"),
    "rectification": ("rectification", "整改台账与督办建议"),
    "archive": ("archive", "项目归档清单"),
}


def _pretty_json(text):
    """把任务解析的 JSON 文本美化，容忍 markdown 代码块"""
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.dumps(json.loads(text[start:end + 1]), ensure_ascii=False, indent=2)
            except Exception:
                return text
        return text


def _field_blocks(ctx):
    blocks = []
    blocks.append({"type": "json", "label": "任务解析", "content": _pretty_json(ctx.get("parsed", ""))})
    blocks.append({"type": "markdown", "label": "取数筛查", "content": ctx.get("anomalies_text", "")})
    retrieved = [
        {"source": r.get("source", ""), "score": r.get("score", 0), "text": r.get("text", "")}
        for r in ctx.get("retrieved", [])
    ]
    blocks.append({"type": "retrieved", "label": "法规检索", "items": retrieved})
    blocks.append({"type": "markdown", "label": "风险判断", "content": ctx.get("risks", "")})
    blocks.append({"type": "markdown", "label": "审计事项清单（三级结构）", "content": ctx.get("audit_items", "")})
    blocks.append({"type": "markdown", "label": "审计工作底稿", "content": ctx.get("workpaper", "")})
    return blocks


def _preaudit_blocks(ctx):
    blocks = []
    blocks.append({"type": "markdown", "label": "审计实施方案", "content": ctx.get("audit_plan", "")})
    if ctx.get("data_portrait"):
        blocks.append({"type": "markdown", "label": "数据画像（真实历史问题分布）", "content": ctx.get("data_portrait", "")})
    if ctx.get("audit_notice"):
        blocks.append({"type": "markdown", "label": "审计通知书（草稿）", "content": ctx.get("audit_notice", "")})
    return blocks


def _trace(ctx):
    return {
        "data_source": ctx.get("data_source"),
        "anomalies": ctx.get("anomalies", []),
        "retrieved": [
            {"source": r.get("source", ""), "score": r.get("score", 0)}
            for r in ctx.get("retrieved", [])
        ],
        "initiation_intent_retrieved": [
            {"source": r.get("source", ""), "score": r.get("score", 0), "library": r.get("library", "")}
            for r in ctx.get("initiation_intent_retrieved", [])
        ],
    }


def _reference_cards(refs, allowed=None):
    """把底层检索片段整理成用户可读的知识卡片：按来源去重、合并、截断，不暴露内部分数。"""
    grouped = {}
    for item in refs or []:
        source = str(item.get("source") or "").strip()
        if not source or (allowed and source not in allowed):
            continue
        grouped.setdefault(source, [])
        text = str(item.get("text") or "").strip()
        if text and text not in grouped[source]:
            grouped[source].append(text)
    cards = []
    for source, chunks in grouped.items():
        content = "\n".join(chunks[:3])
        cards.append({"source": source, "text": content[:520], "reference_type": "规则/样本参考"})
    return cards


def _blocks_for(stage, ctx):
    """按阶段返回前端渲染块（含各阶段新增的结构化/LLM 文档）"""
    if stage == "initiation":
        blocks = []
        parsed = _safe_json(ctx.get("parsed"))
        scope = parsed.get("审计范围") or "未识别到明确审计范围"
        audit_type = parsed.get("审计类型") or "其他审计"
        process_text = (
            f"我已将你的输入识别为“审计项目立项”，提取到的审计范围为“{scope}”，并给出“{audit_type}”的初步类型建议。\n\n"
            "随后，我使用标准立项表单结构生成了项目草稿：项目编号、年度、实施单位、负责人及业务字段均由系统基于当前输入和授权资料预填；"
            "所有字段均标记为待人工审核，用户可直接修正，不需要重新编制表单。\n\n"
            "当前尚未创建正式项目。提交表单后，系统才会进行字段校验、项目重复校验和权限校验；全部通过并经人工审核后，项目才会流转至审前准备。"
        )
        blocks.append({"type": "markdown", "label": "思考过程", "content": process_text})
        if ctx.get("info_form"):
            blocks.append({"type": "markdown", "label": "项目立项表", "content": render_info_form(ctx.get("info_form"), ctx.get("info_form_sources"))})
        if ctx.get("initiation_risks"):
            blocks.append({"type": "json", "label": "立项风险提示（待人工复核）", "content": ctx.get("initiation_risks")})
        if ctx.get("initiation_trace"):
            trace = ctx.get("initiation_trace") or {}
            blocks.append({"type": "markdown", "label": "立项风险溯源与变更说明", "content":
                           "## 风险分析\n" + str(trace.get("risk_analysis", "")) +
                           "\n\n## 历史问题总结\n" + str(trace.get("historical_summary", "")) +
                           "\n\n## AI变更日志（可读）\n" + str(trace.get("readable_change_log", ""))})
        if ctx.get("demand_form"):
            blocks.append({"type": "markdown", "label": "2026 审计计划需求表", "content": ctx.get("demand_form")})
        blocks.append({"type": "markdown", "label": "审计立项申请", "content": ctx.get("initiation", "")})
        return blocks
    if stage == "preaudit":
        blocks = _preaudit_blocks(ctx)
        if ctx.get("preaudit_workload"):
            blocks.append({"type": "json", "label": "工时与人员分工建议（待组长确认）", "content": ctx.get("preaudit_workload")})
        if ctx.get("preaudit_risk_prompts"):
            blocks.append({"type": "json", "label": "审前风险提示（待人工复核）", "content": ctx.get("preaudit_risk_prompts")})
        blocks.append({"type": "markdown", "label": "审前资料清单", "content": ctx.get("preaudit_materials", "")})
        blocks.append({"type": "markdown", "label": "审前调查表单", "content": ctx.get("pre_survey_form", "")})
        blocks.append({"type": "markdown", "label": "审计单位承诺书", "content": ctx.get("commitment", "")})
        blocks.append({"type": "markdown", "label": "审计事项清单（三级结构）", "content": ctx.get("audit_items", "")})
        return blocks
    if stage == "field":
        blocks = _field_blocks(ctx)
        blocks.append({"type": "markdown", "label": "现场审计会议通知与议程", "content": ctx.get("meeting_agenda", "")})
        blocks.append({"type": "markdown", "label": "实施资料清单与调查表", "content": ctx.get("field_materials", "")})
        return blocks
    if stage == "report":
        return [
            {"type": "markdown", "label": "审计问题清单", "content": ctx.get("problem_list", "")},
            {"type": "markdown", "label": "审计报告初稿", "content": ctx.get("report", "")},
            {"type": "markdown", "label": "征求意见稿", "content": ctx.get("seek_opinion", "")},
        ]
    if stage == "rectification":
        return [
            {"type": "markdown", "label": "整改台账与督办建议", "content": ctx.get("rectification", "")},
            {"type": "markdown", "label": "整改成果复核", "content": ctx.get("rectification_review", "")},
        ]
    if stage == "archive":
        return [
            {"type": "markdown", "label": "项目归档清单", "content": ctx.get("archive", "")},
            {"type": "markdown", "label": "归档三清单（问题/建议/追责）", "content": ctx.get("archive_three", "")},
        ]
    return []


def render_stage(stage, ctx):
    """把某个阶段的产出转成前端结构：{stage, title, blocks, trace, gate, next_hint}"""
    return {
        "stage": stage,
        "title": STAGE_NAMES.get(stage, stage),
        "blocks": _blocks_for(stage, ctx),
        "trace": _trace(ctx),
        "gate": _STAGE_GATE.get(stage, ""),
        "next_hint": _STAGE_NEXT.get(stage, ""),
    }


# 六阶段进度：ctx 里出现哪些产出，就判定哪些阶段已完成
_PHASE_KEYS = {
    "initiation": "initiation",
    "preaudit": "audit_plan",
    "field": "workpaper",
    "report": "report",
    "rectification": "rectification",
    "archive": "archive",
}


def phase_progress(ctx):
    done = sum(1 for s in STAGE_ORDER if ctx.get(_PHASE_KEYS[s]))
    phases = []
    for i, s in enumerate(STAGE_ORDER):
        if ctx.get(_PHASE_KEYS[s]):
            status = "done"
        elif i == done:
            status = "active"
        else:
            status = "pending"
        phases.append({"name": STAGE_NAMES[s], "status": status})
    return phases, done


def state_summary(ctx):
    phases, done = phase_progress(ctx)
    return {
        "task": ctx.get("task", ""),
        "phases": phases,
        "done": done,
        "stats": {
            "stages_done": done,
            "anomalies": len(ctx.get("anomalies", [])),
            "retrieved": len(ctx.get("retrieved", [])),
            "knowledge_docs": len(knowledge_manifest().get("docs", [])),
            "data_sources": len(DATA_SOURCES),
        },
        "anomalies": ctx.get("anomalies", []),
        "anomaly_summary": ctx.get("anomaly_summary", {}),
        "data_source": ctx.get("data_source"),
        "risks": ctx.get("risks", ""),
        "risks_list": ctx.get("risks_list", []),
        "workpaper": ctx.get("workpaper", ""),
        "workpaper_locked": ctx.get("workpaper_locked", False),
        "report": ctx.get("report", ""),
        "rectification": ctx.get("rectification", ""),
        "info_form": ctx.get("info_form"),
        "info_form_sources": ctx.get("info_form_sources", {}),
        "initiation_risks": ctx.get("initiation_risks", []),
        "initiation_trace": ctx.get("initiation_trace", {}),
        "initiation_hard_facts": ctx.get("initiation_hard_facts", []),
        "initiation_status": ctx.get("initiation_status", ""),
        "demand_status": ctx.get("demand_status", ""),
        "preaudit_retrieved": [{"source": r.get("source"), "score": r.get("score", 0)} for r in ctx.get("preaudit_retrieved", [])],
        "preaudit_workload": ctx.get("preaudit_workload", {}),
        "preaudit_risk_prompts": ctx.get("preaudit_risk_prompts", {}),
        "notices": ctx.get("notices", []),
    }


# ==================== 工作台待办（六阶段快速切换） ====================

# 每个阶段的子任务待办（label, ctx 产出键, 负责角色）；完成态由 ctx 实时推导，不臆造。
_STAGE_TODOS = {
    "initiation": [
        ("下达审计任务", "task", "project_lead"),
        ("生成项目立项表", "info_form", "project_lead"),
        ("生成立项申请", "initiation", "project_lead"),
    ],
    "preaudit": [
        ("编制审计实施方案", "audit_plan", "chief_auditor"),
        ("生成数据画像", "data_portrait", "chief_auditor"),
        ("起草审查通知书", "audit_notice", "chief_auditor"),
        ("审前资料清单", "preaudit_materials", "respondent"),
        ("审前调查表单", "pre_survey_form", "respondent"),
        ("签署审计单位承诺书", "commitment", "respondent"),
    ],
    "field": [
        ("解析审计任务", "parsed", "auditor"),
        ("取数筛查异常", "anomalies", "auditor"),
        ("检索法规依据", "retrieved", "auditor"),
        ("风险判断", "risks", "auditor"),
        ("生成审计工作底稿", "workpaper", "auditor"),
        ("会议通知与议程", "meeting_agenda", "group_lead"),
        ("实施资料清单与调查表", "field_materials", "group_lead"),
    ],
    "report": [
        ("审计问题清单", "problem_list", "chief_auditor"),
        ("生成审计报告初稿", "report", "chief_auditor"),
        ("征求意见稿", "seek_opinion", "respondent"),
    ],
    "rectification": [
        ("生成整改台账与督办建议", "rectification", "respondent"),
        ("整改成果复核", "rectification_review", "chief_auditor"),
    ],
    "archive": [
        ("生成项目归档清单", "archive", "chief_auditor"),
        ("归档三清单", "archive_three", "chief_auditor"),
    ],
}


def _stage_exit_gate(stage):
    """某阶段的出口闸门（流转条件）= 进入下一阶段的前置条件；归档为最终闸门。"""
    idx = STAGE_ORDER.index(stage)
    if idx + 1 < len(STAGE_ORDER):
        return STAGE_PRECONDITIONS[STAGE_ORDER[idx + 1]]
    return FINAL_GATE


def _is_done(val):
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, (list, tuple, dict, set)):
        return len(val) > 0
    if isinstance(val, str):
        return len(val.strip()) > 0
    return bool(val)


def _cond_ok(ctx, cond):
    approved = ctx.get("approved", set())
    name = _cond_name(cond)
    if name == "整改成果复核通过":
        return name in approved or "项目终止" in approved
    return name in approved


def _stage_todos(ctx, stage, role=None):
    """某阶段的待办列表：子任务 + 人工闸门；每项标记负责角色与「是否当前角色」。"""
    todos = []
    for label, key, r in _STAGE_TODOS.get(stage, []):
        todos.append({"label": label, "key": key, "done": _is_done(ctx.get(key)), "gate": False, "role": r})
    for cond in _stage_exit_gate(stage):
        todos.append({"label": _cond_name(cond), "done": _cond_ok(ctx, cond), "gate": True, "role": _cond_role(cond)})
    if role:
        for t in todos:
            t["mine"] = t["role"] == role
            t["role_name"] = role_name(t["role"]) if t["role"] else "系统"
    return todos


# ==================== API ====================

@app.get("/api/session")
def new_session():
    session_id = uuid.uuid4().hex[:12]
    orch = _get_orch(session_id)
    _persist(session_id, orch)
    return {"session_id": session_id}


@app.get("/api/state")
def state(session_id: str):
    orch = _get_orch(session_id)
    summary = state_summary(orch.ctx)
    summary["current_role"] = orch.current_role
    summary["current_role_name"] = role_name(orch.current_role)
    return summary


@app.get("/api/todos")
def todos(project_id: str, stage: str | None = None, role: str | None = None):
    """六阶段快速切换的待办列表：携带 project_id 返回对应阶段待办（子任务 + 人工闸门），
    并标记每项的负责角色；未指定 stage 默认定位到「下一个待办阶段」。
    """
    orch = _get_orch(project_id)
    ctx = orch.ctx
    if not stage:
        stage = orch.next_pending() or STAGE_ORDER[-1]
    r = role or orch.current_role
    return {
        "project_id": project_id,
        "task": ctx.get("task", ""),
        "progress_desc": orch.progress_desc(),
        "next_pending": orch.next_pending(),
        "phases": phase_progress(ctx)[0],
        "selected": stage,
        "selected_name": STAGE_NAMES.get(stage, stage),
        "todos": _stage_todos(ctx, stage, r),
        "current_role": r,
        "current_role_name": role_name(r),
        "pending_approvals": [
            {"label": _cond_name(c), "role": _cond_role(c),
             "role_name": role_name(_cond_role(c)) if _cond_role(c) else "系统"}
            for c in orch.pending_approvals()
        ],
    }


@app.get("/api/roles")
def roles(session_id: str | None = None):
    """角色注册表 + 当前角色（供顶栏角色切换器）"""
    current = None
    if session_id:
        current = _get_orch(session_id).current_role
    return {
        "current": current,
        "current_name": role_name(current) if current else "",
        "roles": [{"key": k, "name": v} for k, v in ROLES.items()],
    }


@app.post("/api/roles/switch")
async def switch_role(req: Request):
    """切换当前会话角色（角色感知会签：确认时只满足该角色负责的会签项）"""
    body = await req.json()
    session_id = body.get("session_id", "")
    role = body.get("role", "")
    if role not in ROLES:
        return JSONResponse({"err": f"未知角色：{role}"}, status_code=400)
    orch = _get_orch(session_id)
    orch.set_role(role)
    _persist(session_id, orch)
    return {
        "current_role": orch.current_role,
        "current_role_name": role_name(orch.current_role),
    }


@app.get("/api/approvals")
def approval_list(session_id: str):
    """返回项目已落库的会签记录及当前待办条件。"""
    orch = _get_orch(session_id)
    return {
        "session_id": session_id,
        "items": db.load_approvals(session_id),
        "pending": [
            {"condition": _cond_name(c), "role": _cond_role(c),
             "role_name": role_name(_cond_role(c)) if _cond_role(c) else "系统"}
            for c in orch.pending_approvals()
        ],
    }


@app.post("/api/approvals")
async def approval_create(req: Request):
    """生产会签入口：只允许条件对应角色确认，逐项落库后才可推进。"""
    body = await req.json()
    session_id = str(body.get("session_id") or "")
    condition = str(body.get("condition") or "").strip()
    opinion = str(body.get("opinion") or "").strip()[:1000]
    if not session_id or not condition:
        return JSONResponse({"err": "session_id 和 condition 必填"}, status_code=400)
    orch = _get_orch(session_id)
    actor_info = actor(req)
    # 审批角色以服务端会话/身份为准，不接受请求体伪造角色；前端需先切换到对应角色。
    role = orch.current_role or actor_info["actorRole"]
    # 只允许确认当前待办会签，禁止提前确认后续阶段条件。
    pending = orch.pending_approvals()
    matched = next((c for c in pending if _cond_name(c) == condition), None)
    if not matched:
        return JSONResponse({"err": "该条件不是当前待确认项，不能提前审批"}, status_code=409)
    required_role = _cond_role(matched)
    if required_role and role != required_role:
        return JSONResponse({"err": f"该会签项仅允许{role_name(required_role)}确认"}, status_code=403)
    if condition == "整改成果复核通过":
        statuses = orch.ctx.get("rectification_statuses") or {}
        if not statuses or any(v not in ("通过销号", "结转持续整改") for v in statuses.values()):
            return JSONResponse({"err": "整改成果尚未逐项复核：必须先提交每个问题的三态结论，且不通过项不得进入归档"}, status_code=409)
    orch._satisfy(matched)
    if condition in ("现场实施资料组长审核通过", "现场实施资料主审审核通过"):
        required = {"现场实施资料组长审核通过", "现场实施资料主审审核通过"}
        if required.issubset(orch.ctx.get("approved", set())):
            from roles import notify
            notify(orch.ctx, "respondent", "现场会议通知、实施资料清单及调查表已完成组长/主审审核，请迎审人员反馈资料并确认。")
    if condition == "报告审签发布":
        # 只有正式签发会签通过才锁定底稿；报告初稿/征求意见稿阶段仍允许修订。
        orch.ctx["workpaper_locked"] = True
    db.save_approval(session_id, condition, role, actor_info["actorId"], "APPROVED", opinion)
    _persist(session_id, orch)
    return {"ok": True, "condition": condition, "pending": [
        {"condition": _cond_name(c), "role": _cond_role(c)} for c in orch.pending_approvals()
    ]}


@app.post("/api/rectification/review")
async def rectification_review(req: Request):
    """整改逐问题复核：三态值由服务端白名单约束，人工最终确认后才可归档。"""
    body = await req.json()
    session_id = str(body.get("session_id") or "")
    problem_id = str(body.get("problem_id") or "").strip()
    status = str(body.get("status") or "").strip()
    if not session_id or not problem_id or status not in ("通过销号", "不通过退回重改", "结转持续整改"):
        return JSONResponse({"err": "session_id、problem_id 必填；status 只能为：通过销号 / 不通过退回重改 / 结转持续整改"}, status_code=400)
    orch = _get_orch(session_id)
    if not orch.ctx.get("rectification"):
        return JSONResponse({"err": "尚未生成整改台账，不能提交整改复核"}, status_code=409)
    statuses = orch.ctx.setdefault("rectification_statuses", {})
    statuses[problem_id] = status
    orch.ctx.setdefault("rectification_review_log", []).append({
        "problem_id": problem_id, "status": status, "actor": actor(req)["actorId"],
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    _persist(session_id, orch)
    await _write_admin_event(req, "RECTIFICATION_REVIEW_UPDATED", problem_id, {"status": status, "session_id": session_id})
    return {"ok": True, "problem_id": problem_id, "status": status,
            "statuses": statuses, "ready_for_archive": bool(statuses) and all(v in ("通过销号", "结转持续整改") for v in statuses.values())}


@app.get("/api/notices")
def notices(session_id: str, role: str | None = None, mark_read: bool = False):
    """系统通知队列：按角色过滤；mark_read=1 时把当前角色通知标为已读"""
    orch = _get_orch(session_id)
    role = role or orch.current_role
    items = [n for n in orch.ctx.get("notices", []) if n.get("role") == role]
    unread = sum(1 for n in items if not n.get("read"))
    if mark_read:
        for n in items:
            n["read"] = True
        _persist(session_id, orch)
    return {
        "role": role,
        "role_name": role_name(role),
        "unread": unread,
        "items": [
            {"role": n.get("role"), "role_name": role_name(n.get("role")),
             "text": n.get("text"), "read": n.get("read")}
            for n in items
        ],
    }


@app.post("/api/run")
async def run_stage_api(req: Request):
    """自由命令 / 单阶段：任意输入 → 阶段判定 → 角色感知执行"""
    body = await req.json()
    session_id = body.get("session_id", "")
    text = (body.get("input") or "").strip()
    role = body.get("role") or ""
    if not text:
        return JSONResponse({"err": "请输入审计任务或指令。"}, status_code=400)

    orch = _get_orch(session_id)
    orch.ctx["_actor"] = actor(req)
    # 保存本次真实对话输入，供文稿生成 Agent 与审计留痕使用。
    # 前端会将可解析附件的文本内容拼接到 input；不可解析文件至少保留元数据。
    orch.ctx["latest_user_input"] = text
    orch.ctx["latest_attachments"] = body.get("attachments") or []
    duplicate_pending = orch.ctx.get("duplicate_check_pending")
    if duplicate_pending and re.search(r"^(结束创建|不要创建|不创建|取消|否|不用)$", text.strip()):
        orch.ctx.pop("duplicate_check_pending", None)
        _persist(session_id, orch)
        return {
            "ok": True,
            "status": "duplicate_check_cancelled",
            "reply": "已取消新建项目，本次未生成新的项目立项表。",
            "execution": {"agent_id": "project_precheck", "status": "cancelled", "completed_steps": 1, "elapsed_seconds": 0.1, "step_durations": [0.1]},
        }
    if duplicate_pending and re.search(r"^(需要|继续创建|确认继续|是|确认)$", text.strip()):
        text = orch.ctx.pop("duplicate_check_pending")
        orch.ctx["latest_user_input"] = text
        orch.ctx["duplicate_check_confirmed"] = True

    # 年度审计计划立项必须先确认计划文件；无论前端版本或缓存状态如何，后端都不得直接进入普通立项 Agent。
    normalized_text = re.sub(r"[\\s\\u3000]+", "", text)
    annual_plan_command = bool(re.search(r"基于年度审计计划自动生成立项|年度审计计划.*自动生成立项|自动生成立项.*年度审计计划", normalized_text))
    if annual_plan_command and not orch.ctx.get("annual_plan_upload_pending") and not body.get("attachments"):
        named_match = re.search(r"项目名称\s*(?:是|为|：|:)\s*[“\"]?([^”\"，,。\n]+)", text)
        demand = db.find_latest_plan_demand(named_match.group(1).strip() if named_match else "", exclude_session_id=session_id)
        if demand:
            fields = demand["fields"]
            plan_lines = "\n".join(f"{key}：{value}" for key, value in fields.items())
            orch.ctx["latest_user_input"] = "基于已检索到的审计计划需求表生成项目立项草稿\n" + plan_lines
            orch.ctx["plan_source_confirmed"] = True
            try:
                orch.agents["initiation"].run(orch.ctx["latest_user_input"], orch.ctx)
            except Exception as exc:
                logger.exception("审计计划需求表转立项失败")
                return JSONResponse({"err": f"审计计划需求表解析失败：{type(exc).__name__}"}, status_code=422)
            _persist(session_id, orch)
            result = render_stage("initiation", orch.ctx)
            result["reply"] = "已从系统立项依据库检索到对应的《审计计划需求表》，已提取字段生成项目立项表，请人工审核确认。"
            result["status"] = "plan_demand_reused"
            return result
        orch.ctx["annual_plan_upload_pending"] = True
        _persist(session_id, orch)
        return {
            "ok": True,
            "status": "awaiting_annual_plan_file",
            "reply": "请上传具体的年度审计计划文件，我会从中提取立项表单需要的字段。\n如果您没有这份文件，也可以直接告诉我，我会为您生成《审计计划需求表》。",
            "execution": {"agent_id": "project_initiation", "completed_steps": 1, "elapsed_seconds": 0.1, "step_durations": [0.1]},
        }
    if orch.ctx.get("annual_plan_upload_pending") and re.search(r"没有|无文件|没有文件|暂时没有|生成审计计划需求表", text):
        orch.ctx.pop("annual_plan_upload_pending", None)
        fields = {"年度": str(time.localtime().tm_year), "状态": "草稿", "操作人": str((orch.ctx.get("_actor") or {}).get("name") or (orch.ctx.get("_actor") or {}).get("actorId") or "项目负责人"), "填报说明": "未上传计划文件，由系统生成需求表，请补充计划信息后提交业务负责人审核。"}
        orch.ctx["plan_demand_fields"] = fields
        orch.ctx["demand_form"] = render_plan_demand_form(fields)
        orch.ctx["demand_status"] = "pending_business_review"
        orch.notify("business_lead", "请审核由年度审计计划生成的审计计划需求表。")
        _persist(session_id, orch)
        return {"ok": True, "status": "pending_business_review", "reply": "已经为您生成《审计计划需求表》，请您审核后确认提交。", "execution": {"agent_id": "project_initiation", "completed_steps": 2, "elapsed_seconds": 0.2, "step_durations": [0.1, 0.1]}, "blocks": [{"type": "markdown", "label": "审计计划需求表", "content": orch.ctx["demand_form"]}]}

    # 创建项目之前先做同名/相似项目预检，命中时暂停 Agent，不生成新的项目草稿。
    # 以后端文本本身兜底识别创建意图，避免前端阶段标记丢失时跳过同名/相似项目预检。
    creation_phrase = bool(re.search(r"(?:创建|新建|发起|建立|复制上一项目).*(?:审计项目|专项审计|常规审计|审计立项)|(?:审计项目|专项审计|常规审计).*立项", text))
    is_creation_request = body.get("audit_project_creation") or body.get("task_key") == "create_project" or creation_phrase
    # 同一会话中再次明确创建新项目时，不能沿用上一项目的表单；复制上一项目配置由专用接口负责。
    explicit_new_creation = bool(creation_phrase and "复制上一项目" not in text)
    if explicit_new_creation and orch.ctx.get("info_form"):
        for stale_key in ("info_form", "parsed", "project_form_rag", "initiation_risks", "initiation_trace", "initiation_trace_rag", "info_form_sources", "initiation_change_log", "creation_precheck_result", "duplicate_check_pending"):
            orch.ctx.pop(stale_key, None)
    if is_creation_request and (not orch.ctx.get("info_form") or explicit_new_creation):
        skip_duplicate_precheck = bool(orch.ctx.pop("duplicate_check_confirmed", False))
        precheck_started = time.perf_counter()
        similar_projects = [] if skip_duplicate_precheck else _creation_precheck(text, session_id)
        if similar_projects:
            orch.ctx["duplicate_check_pending"] = text
            _persist(session_id, orch)
            _log_chat(session_id, "user", text)
            _log_chat(session_id, "ai", "已完成项目名称预检，发现同名或相似项目，等待人工选择。")
            precheck_elapsed = max(0.1, time.perf_counter() - precheck_started)
            return {
                "status": "duplicate_check",
                "reply": "发现同名或相似审计项目，请先查看项目详情并确认是否继续新建。",
                "blocks": [
                    {"type": "similar_projects", "label": "同名/相似项目清单", "items": similar_projects},
                ],
                "execution": {"agent_id": "project_precheck", "status": "blocked", "completed_steps": 1, "duration_ms": round(precheck_elapsed * 1000), "elapsed_seconds": round(precheck_elapsed, 3), "step_durations": [round(precheck_elapsed, 3)]},
                "duplicate_check": True,
            }
        orch.ctx["creation_precheck_result"] = "未发现同名或相似项目，已通过项目名称预检，继续生成项目立项表。"
        # 同一会话切换到新的创建指令时，清除上一任务残留的阶段状态，避免被旧任务守卫拦截。
        orch.ctx["task"] = ""
    stage, ctx, err = orch.dispatch_silent(text, role or None)
    _persist(session_id, orch)
    _log_chat(session_id, "user", text)

    # 编排器已记录本次 Agent 的真实耗时和状态，这里作为可审计执行元数据透传给前端。
    # 步骤数量与前端时间线保持同一阶段定义，不能由模型自行编造。
    execution_steps = {
        "initiation": 7,
        "preaudit": 5,
        "field": 5,
        "report": 5,
        "rectification": 5,
        "archive": 5,
    }
    run_record = ((ctx or {}).get("agent_runs") or [])[-1] if isinstance(ctx, dict) else {}
    execution = {
        "agent_id": stage,
        "status": run_record.get("status", "error" if err else "success"),
        "duration_ms": run_record.get("duration_ms"),
        "elapsed_seconds": round(float(run_record.get("duration_ms", 0)) / 1000, 3) if run_record.get("duration_ms") is not None else None,
        "completed_steps": (len((ctx or {}).get("initiation_step_durations", [])) if stage == "initiation" and isinstance(ctx, dict) and (ctx or {}).get("initiation_step_durations") else execution_steps.get(stage, 5)) if not err and run_record.get("status") != "error" else 0,
        "step_durations": (ctx or {}).get("initiation_step_durations", []) if stage == "initiation" and isinstance(ctx, dict) else [],
        "failed_step": run_record.get("failed_step", ""),
        "failure_reason": run_record.get("error", "") if run_record.get("status") == "error" else "",
    }
    # 前端按一位小数展示步骤耗时；立项摘要必须与这些展示值的合计完全一致。
    if stage == "initiation" and execution["step_durations"]:
        shown_total = round(sum(round(max(0.1, float(value)), 1) for value in execution["step_durations"]), 1)
        execution["actual_elapsed_seconds"] = execution["elapsed_seconds"]
        execution["elapsed_seconds"] = shown_total
        execution["duration_ms"] = round(shown_total * 1000)

    if err:
        # 回退/闭环/终止/会签/补全等是「状态提示」，非错误；用 info 通道返回给前端中性渲染
        _log_chat(session_id, "ai", err)
        return ({"info": err} if _is_info_msg(err) else {"err": err}) | {"execution": execution}
    if ctx is None:
        return {"err": "内部错误：阶段未执行。"}
    if ctx.get("blocked"):
        _log_chat(session_id, "ai", "已拦截：" + ctx.get("blocked_msg", ""))
        return {"blocked": True, "blocked_msg": ctx.get("blocked_msg", "")}
    _log_chat(session_id, "ai", "已执行阶段：" + STAGE_NAMES.get(stage, stage))

    result = render_stage(stage, ctx)
    result["execution"] = execution
    result["phases"] = phase_progress(ctx)[0]
    result["current_role"] = orch.current_role
    result["current_role_name"] = role_name(orch.current_role)
    result["reply"] = orch.entry_reply()
    # 立项结果统一由前端按“深度思考 + 提示语 + 可编辑表单”渲染，禁止混入旧版模板/思考详情文本。
    if stage == "initiation" and ctx.get("info_form"):
        result["reply"] = "已完成项目立项信息解析并生成草稿，请人工审核并补充待确认字段。"
    if ctx.get("fill_hint"):
        result["fill"] = True
        result["info"] = ctx.get("fill_hint")
    if ctx.get("creation_precheck_result") and is_creation_request:
        result["precheck_reply"] = ctx.pop("creation_precheck_result")
    return result


@app.post("/api/follow-up")
async def follow_up_api(req: Request):
    """基于当前会话成果的增量追问，不重新执行阶段 Agent 链路。"""
    body = await req.json()
    session_id = body.get("session_id", "")
    question = (body.get("question") or "").strip()
    if not session_id or not question:
        return JSONResponse({"err": "追问内容不能为空。"}, status_code=400)
    orch = _get_orch(session_id)
    form = orch.ctx.get("info_form") or {}
    if not form:
        return JSONResponse({"err": "当前会话还没有可继续处理的审计成果。"}, status_code=422)
    missing = [k for k in ("项目组长", "主审", "业务负责人", "被审计单位名称", "迎审人员") if not str(form.get(k) or "").strip() or str(form.get(k)).strip() in ("待补全", "待明确")]
    if "关键依据" in question:
        answer = "本次立项草稿的处理依据包括：用户本次自然语言指令、立项标准表单模板、审计项目类型规则及当前会话已获取的业务主数据。正式项目事实仍需以表单确认和授权业务接口结果为准。"
    elif "人工复核" in question:
        answer = "仍需人工复核：" + "、".join(missing or ["项目名称和项目类型的 AI 识别结果", "被审计单位及人员配置", "立项依据与审批意见"]) + "。"
    elif "下一步" in question:
        answer = "下一步建议：先在项目立项表中审核字段并保存草稿，提交立项；校验通过后由对应角色确认，项目再进入审前准备阶段。"
    else:
        answer = "已基于当前立项草稿继续处理。你可以继续询问关键依据、人工复核事项或下一步审计工作。"
    _log_chat(session_id, "user", question)
    _log_chat(session_id, "ai", answer)
    return {"reply": answer, "incremental": True, "phases": phase_progress(orch.ctx)[0]}


@app.post("/api/project/initiation-form")
async def initiation_form_api(req: Request):
    """真实保存/校验立项信息表单，并在通过后生成立项申请草稿。"""
    body = await req.json()
    session_id = body.get("session_id", "")
    action = body.get("action", "draft")
    fields = body.get("fields") or {}
    if not session_id or not isinstance(fields, dict):
        return JSONResponse({"err": "立项表单参数不完整。"}, status_code=400)
    orch = _get_orch(session_id)
    orch.ctx["_actor"] = actor(req)
    form = dict(orch.ctx.get("info_form") or {})
    aliases = {"项目编号": "项目编号", "项目年度": "项目年度", "项目名称": "项目名称",
               "项目类型": "项目类型", "项目所属单位": "项目所属单位", "项目实施单位": "项目所属单位",
               "项目负责人": "项目负责人", "项目组长": "项目组长", "组长": "项目组长", "主审": "主审", "业务负责人": "业务负责人",
               "被审计单位": "被审计单位名称", "被审计单位名称": "被审计单位名称", "迎审人员": "迎审人员",
               "立项依据": "立项依据"}
    for key, value in fields.items():
        canonical = aliases.get(str(key).strip())
        if canonical and str(value or "").strip():
            form[canonical] = str(value).strip()
    orch.ctx["info_form"] = form
    required = ["项目组长", "主审", "业务负责人", "被审计单位名称", "迎审人员"]
    # 基础信息必须完整；立项依据缺失时走审计计划需求表审核分支。
    missing = [key for key in required if not str(form.get(key) or "").strip()]
    if action == "draft":
        orch.ctx["fill_hint"] = "请完善项目立项表：" + "、".join(missing) if missing else None
        _persist(session_id, orch)
        return {"ok": True, "status": "draft", "missing": missing, "form": form}
    if missing:
        orch.ctx["fill_hint"] = "请完善项目立项表：" + "、".join(missing)
        _persist(session_id, orch)
        return JSONResponse({"ok": False, "status": "invalid", "missing": missing, "info": "请完善表单字段后再提交立项。"}, status_code=422)
    # 复制上一项目只复用业务配置；来源项目名称和编号不得原样提交为新项目。
    if action == "submit" and orch.ctx.get("copy_source_session_id"):
        source = db.load_project(orch.ctx.get("copy_source_session_id")) or {}
        source_form = dict((source.get("state") or {}).get("info_form") or {})
        source_name = str(source_form.get("项目名称") or source.get("project_name") or "").strip()
        source_code = str(source_form.get("项目编号") or source.get("project_code") or "").strip()
        def _copy_name_base(value):
            return re.sub(r"\s*[（(](?:上一项目名称|上一项目配置|复制草稿)[）)]\s*$", "", str(value or "").strip())
        same_name = source_name and _copy_name_base(form.get("项目名称")) == _copy_name_base(source_name)
        same_code = source_code and str(form.get("项目编号") or "").strip() == source_code
        if same_name or same_code:
            duplicated = "项目名称" if same_name else "项目编号"
            return JSONResponse({
                "ok": False,
                "status": "duplicate_source",
                "missing": [],
                "info": f"项目{duplicated}与上一项目相同，请补充修改后，再确认立项。",
                "source_project": source_name,
            }, status_code=422)
    orch.ctx.pop("fill_hint", None)
    # 立项依据缺失：生成需求表并推送总部/直属单位业务负责人，不提前流转。
    if not str(form.get("立项依据") or "").strip() and orch.ctx.get("demand_status") != "approved":
        from docs import render_demand_form
        orch.ctx["demand_form"] = render_demand_form(form, orch.ctx.get("task", form.get("项目名称", "")))
        orch.ctx["demand_status"] = "pending_business_review"
        orch.notify("business_lead", "请审核审计计划需求表；审核确认后将向项目负责人发送立项准入提醒。")
        _persist(session_id, orch)
        result = render_stage("initiation", orch.ctx)
        result["reply"] = "立项依据尚未提供，已生成《审计计划需求表》，并推送总部/直属单位业务负责人审核。审核确认后将通知项目负责人继续完成立项准入。"
        result["status"] = "demand_pending"
        return result

    # 项目负责人确认后完成立项出口条件，并自动进入审前准备阶段。
    orch.ctx.pop("demand_form", None)
    orch.ctx["initiation_status"] = "已完成"
    orch.ctx.setdefault("approved", set()).update({"项目立项表已人工确认", "立项信息已填写完整", "立项依据已确认"})
    orch.ctx["task"] = orch.ctx.get("task") or form.get("项目名称", "")
    try:
        orch.agents["preaudit"].run(orch.ctx)
        next_stage = "preaudit"
    except Exception as exc:
        logger.exception("提交立项后自动进入审前准备失败")
        orch.ctx["transition_error"] = f"审前准备自动启动失败：{type(exc).__name__}"
        next_stage = "initiation"
    _persist(session_id, orch)
    result = render_stage(next_stage, orch.ctx)
    if next_stage == "preaudit":
        result["reply"] = (
            "已确认立项，项目进入审前准备阶段。请问是否生成审计实施方案？<br>"
            "是否推送审计通知书和审前资料清单，并提醒完成审前调查表单审核及审计单位承诺书签署工作？"
        )
    else:
        result["reply"] = "已确认立项，但审前准备成果尚未生成，请稍后重试或转人工处理。"
    result["status"] = "transitioned" if next_stage == "preaudit" else "submitted"
    result["next_stage"] = "preaudit" if next_stage == "preaudit" else None
    result["next_stage_name"] = STAGE_NAMES.get(result["next_stage"] or "", "")
    return result


@app.post("/api/project/demand-review")
async def demand_review_api(req: Request):
    """审计计划需求表审核：提交审核或由业务负责人确认。"""
    body = await req.json()
    session_id = body.get("session_id", "")
    action = body.get("action", "submit")
    if not session_id:
        return JSONResponse({"err": "会话信息不完整。"}, status_code=400)
    orch = _get_orch(session_id)
    if not orch.ctx.get("demand_form"):
        return JSONResponse({"err": "当前会话没有待审核的审计计划需求表。"}, status_code=422)
    submitted_fields = body.get("fields") or {}
    if isinstance(submitted_fields, dict) and submitted_fields:
        current_fields = dict(orch.ctx.get("plan_demand_fields") or {})
        for key, value in submitted_fields.items():
            if key in ("状态", "操作人"):
                continue
            current_fields[str(key)] = str(value or "待补全").strip() or "待补全"
        orch.ctx["plan_demand_fields"] = current_fields
        orch.ctx["demand_form"] = render_plan_demand_form(current_fields)
    # 需求表仍有待补全字段时不得进入业务负责人审核状态。
    # 状态、操作人由系统维护，不作为人工提交必填项。
    required_demand_fields = ("计划名称", "年度", "开始时间", "结束时间", "填报单位", "填报说明")
    demand_values = orch.ctx.get("plan_demand_fields") or {}
    demand_missing = [
        key for key in required_demand_fields
        if not str(demand_values.get(key) or "").strip()
        or str(demand_values.get(key) or "").strip() in ("待补全", "待填写", "未明确", "未提供")
    ]
    if demand_missing:
        _persist(session_id, orch)
        return JSONResponse({
            "ok": False,
            "status": "incomplete",
            "missing": demand_missing,
            "info": "请先补全：" + "、".join(demand_missing) + "。补全后才能提交审核。",
        }, status_code=422)
    if action == "approve":
        orch.ctx["demand_status"] = "approved"
        orch.ctx["initiation_status"] = "可立项"
        orch.ctx.setdefault("approved", set()).add("立项依据已确认")
        orch.notify("project_lead", "审计计划需求表已由业务负责人审核确认，项目已具备立项准入条件，请返回项目立项表提交。")
        message = "审计计划需求表已审核确认，已向项目负责人发送立项准入提醒。"
    else:
        orch.ctx["demand_status"] = "pending_business_review"
        orch.notify("business_lead", "请审核审计计划需求表；审核确认后将通知项目负责人。")
        message = "审计计划需求表已提交总部/直属单位业务负责人审核。待审核确认后，系统会提醒你项目进入可立项状态。"
    _persist(session_id, orch)
    return {"ok": True, "status": orch.ctx["demand_status"], "reply": message, "phases": phase_progress(orch.ctx)[0]}


@app.post("/api/project/plan-demand")
async def plan_demand_api(req: Request):
    """从年度审计计划附件（或空输入）生成审计计划需求表草稿。"""
    started_at = time.perf_counter()
    body = await req.json()
    session_id = body.get("session_id", "")
    plan_text = str(body.get("plan_text") or "")
    if not session_id:
        return JSONResponse({"err": "会话信息不完整。"}, status_code=400)
    orch = _get_orch(session_id)
    orch.ctx["_actor"] = actor(req)
    if body.get("has_file"):
        # 年度审计计划文件视为有效立项依据：直接进入立项草稿，不生成需求表、不发起业务负责人审批。
        orch.ctx["latest_user_input"] = plan_text
        orch.ctx["latest_attachments"] = body.get("attachments") or []
        orch.ctx.pop("demand_form", None)
        orch.ctx.pop("demand_status", None)
        orch.ctx["plan_source_confirmed"] = True
        try:
            orch.agents["initiation"].run("基于已上传年度审计计划生成项目立项草稿\n" + plan_text, orch.ctx)
        except Exception as exc:
            logger.exception("年度审计计划转立项失败")
            return JSONResponse({"err": f"年度审计计划解析失败：{type(exc).__name__}"}, status_code=422)
        _persist(session_id, orch)
        result = render_stage("initiation", orch.ctx)
        result["reply"] = "已读取年度审计计划并提取立项信息，未生成审计计划需求表。请人工核对项目立项表后提交立项。"
        result["status"] = "plan_parsed"
        return result
    labels = ("计划名称", "年度", "开始时间", "结束时间", "填报单位", "操作人", "状态", "填报说明")
    fields = {}
    for label in labels:
        match = re.search(rf"{re.escape(label)}\s*(?:是|为|：|:)\s*([^\n;；]+)", plan_text)
        if match:
            fields[label] = match.group(1).strip()
    fields.setdefault("年度", str(time.localtime().tm_year))
    fields.setdefault("状态", "草稿")
    fields.setdefault("操作人", str((orch.ctx.get("_actor") or {}).get("name") or (orch.ctx.get("_actor") or {}).get("actorId") or "项目负责人"))
    fields.setdefault("填报说明", "已根据年度审计计划文件提取字段，请人工核对后提交业务负责人审核。" if plan_text else "未上传计划文件，由系统生成需求表，请补充计划信息后提交业务负责人审核。")
    orch.ctx["plan_demand_fields"] = fields
    orch.ctx["demand_form"] = render_plan_demand_form(fields)
    orch.ctx["demand_status"] = "pending_business_review"
    orch.notify("business_lead", "请审核由年度审计计划生成的审计计划需求表。")
    _persist(session_id, orch)
    elapsed = max(0.1, time.perf_counter() - started_at)
    return {"ok": True, "status": "pending_business_review", "reply": "已经为您生成《审计计划需求表》，请您审核后确认提交。", "execution": {"elapsed_seconds": elapsed, "completed_steps": 2, "step_durations": [elapsed / 2, elapsed / 2]}, "blocks": [{"type": "markdown", "label": "审计计划需求表", "content": orch.ctx["demand_form"]}]}


@app.post("/api/project/copy-latest")
async def copy_latest_project_api(req: Request):
    """从项目台复制最近立项项目的业务配置，生成当前会话的新项目立项草稿。"""
    body = await req.json()
    session_id = body.get("session_id", "")
    if not session_id:
        return JSONResponse({"err": "会话信息不完整。"}, status_code=400)
    source = db.load_latest_initiation_project(exclude_session_id=session_id)
    if not source:
        return JSONResponse({"ok": False, "status": "empty", "reply": "项目台暂未找到可复制的立项项目，请直接描述新项目需求。"})
    orch = _get_orch(session_id)
    orch.ctx["_actor"] = actor(req)
    source_form = dict((source.get("state") or {}).get("info_form") or {})
    copied = dict(source_form)
    copied["项目编号"] = "AUD-" + time.strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:8].upper()
    copied["项目年度"] = str(time.localtime().tm_year)
    # 复制配置只保留一个用户可读的来源标识，清除历史重复“复制草稿”后缀。
    copied_name = str(copied.get("项目名称") or source.get("project_name") or "审计项目")
    copied_name = re.sub(r"\s*[（(](?:复制草稿|上一项目名称|上一项目配置)[）)]", "", copied_name).strip()
    copied["项目名称"] = copied_name + "（上一项目名称）"
    copied["项目负责人"] = str((orch.ctx.get("_actor") or {}).get("name") or (orch.ctx.get("_actor") or {}).get("actorId") or copied.get("项目负责人") or "项目负责人")
    orch.ctx["info_form"] = copied
    orch.ctx["task"] = copied.get("项目名称", "")
    orch.ctx["copy_source_session_id"] = source.get("session_id")
    orch.ctx["copy_source_project_name"] = source.get("project_name") or source_form.get("项目名称")
    orch.ctx["initiation_status"] = "草稿"
    orch.ctx.pop("demand_form", None)
    orch.ctx.pop("demand_status", None)
    orch.ctx["info_form_sources"] = {}
    _persist(session_id, orch)
    return {"ok": True, "status": "copied_draft", "reply": "已提取上一项目的项目立项表，请参考，看看是否有需要修改的地方，修改完成后，我会生成项目立项表。", "source_project": source.get("project_name"), "blocks": [{"type": "markdown", "label": "项目立项表", "content": render_info_form(copied, orch.ctx.get("info_form_sources"))}]}


@app.post("/api/pipeline/start")
async def pipeline_start(req: Request):
    """交互式全流程：跑第一步「立项」，之后逐步 + 人工闸门确认"""
    body = await req.json()
    session_id = body.get("session_id", "")
    task = (body.get("task") or "").strip()
    if not task:
        return JSONResponse({"err": "请提供具体审计任务。"}, status_code=400)

    orch = _get_orch(session_id)
    orch.ctx["_actor"] = actor(req)
    orch.run_stage("initiation", task)

    if orch.ctx.get("blocked"):
        return {"blocked": True, "blocked_msg": orch.ctx.get("blocked_msg", "")}

    _persist(session_id, orch)
    _save_pipeline(session_id, {"idx": 1, "task": task})
    _log_chat(session_id, "user", task)
    _log_chat(session_id, "ai", "已执行阶段：" + STAGE_NAMES["initiation"])
    result = render_stage("initiation", orch.ctx)
    result["reply"] = orch.entry_reply()
    result["next_stage"] = STAGE_ORDER[1]
    result["next_stage_name"] = STAGE_NAMES[STAGE_ORDER[1]]
    result["phases"] = phase_progress(orch.ctx)[0]
    return result


@app.post("/api/pipeline/next")
async def pipeline_next(req: Request):
    """交互式全流程：确认闸门后，执行下一个阶段"""
    body = await req.json()
    session_id = body.get("session_id", "")

    orch = _get_orch(session_id)
    orch.ctx["_actor"] = actor(req)
    state = _get_pipeline(session_id)
    if not state:
        return JSONResponse({"err": "请先发起审计任务。"}, status_code=400)

    idx = state["idx"]
    if idx >= len(STAGE_ORDER):
        return {"done": True}

    stage = STAGE_ORDER[idx]
    if not _demo_mode():
        missing = orch.preconditions_missing(stage)
        if missing:
            return JSONResponse({
                "blocked": True,
                "blocked_msg": "当前阶段仍有未完成会签，请由对应角色逐项审批后再推进。",
                "pending_approvals": [
                    {"condition": _cond_name(c), "role": _cond_role(c),
                     "role_name": role_name(_cond_role(c)) if _cond_role(c) else "系统"}
                    for c in missing
                ],
            }, status_code=409)
    orch.run_stage(stage, state["task"])

    if orch.ctx.get("blocked"):
        return {"blocked": True, "blocked_msg": orch.ctx.get("blocked_msg", "")}

    state["idx"] = idx + 1
    _persist(session_id, orch)
    _save_pipeline(session_id, state)
    _log_chat(session_id, "ai", "已执行阶段：" + STAGE_NAMES.get(stage, stage))
    result = render_stage(stage, orch.ctx)
    result["reply"] = orch.entry_reply()
    result["next_stage"] = STAGE_ORDER[idx + 1] if idx + 1 < len(STAGE_ORDER) else None
    result["next_stage_name"] = STAGE_NAMES.get(result["next_stage"], "") if result["next_stage"] else ""
    result["done"] = idx + 1 >= len(STAGE_ORDER)
    result["phases"] = phase_progress(orch.ctx)[0]
    return result


@app.post("/api/pipeline/auto")
async def pipeline_auto(req: Request):
    """一键全流程：六阶段连续跑完，闸门以「待确认」标注（不交互暂停）"""
    body = await req.json()
    session_id = body.get("session_id", "")
    task = (body.get("task") or "").strip()
    if not task:
        return JSONResponse({"err": "请提供具体审计任务。"}, status_code=400)

    orch = _get_orch(session_id)
    orch.ctx["_actor"] = actor(req)
    orch.run_pipeline(task)

    if orch.ctx.get("blocked"):
        return {"blocked": True, "blocked_msg": orch.ctx.get("blocked_msg", "")}

    _persist(session_id, orch)
    _log_chat(session_id, "user", task)
    _log_chat(session_id, "ai", "一键全流程完成")
    stages = []
    for s in STAGE_ORDER:
        item = render_stage(s, orch.ctx)
        item["reply"] = replies.stage_entry_reply(s, STAGE_NAMES[s], orch._project(), orch._next_gate(), orch._entry_think(s))
        stages.append(item)
    return {"stages": stages, "phases": phase_progress(orch.ctx)[0], "task": task}


# ==================== 支线功能（六阶段主线之外的独立功能点） ====================

@app.post("/api/function")
async def function_api(req: Request):
    """支线功能：审前调查 / 征求意见 / 逾期督办 / 资料归档 等独立功能点。

    与 /api/run（六阶段主线）的区别：这些功能不经六阶段前置条件链，
    按自身所需上下文就绪度决定是否执行；缺上下文时返回 blocked 提示，而非臆造。
    """
    body = await req.json()
    session_id = body.get("session_id", "")
    function_key = body.get("function_key", "")
    input_text = (body.get("input") or "").strip()

    handler = FUNCTION_HANDLERS.get(function_key)
    if not handler:
        return JSONResponse({"err": f"未知支线功能：{function_key}"}, status_code=400)

    orch = _get_orch(session_id)
    # 支线功能不能绕过六阶段状态机。把状态从服务端编排器计算后传入守卫，
    # 不信任前端 body 中可能伪造的 stage/context。
    completed = orch.completed()
    guard_ctx = dict(orch.ctx)
    guard_ctx["_completed_stage"] = completed
    stage_err = guard_function_stage(guard_ctx, function_key)
    if stage_err:
        return {"blocked": True, "blocked_msg": stage_err, "current_stage": completed}
    result = handler(orch.ctx, input_text)
    _persist(session_id, orch)
    if isinstance(result, dict) and result.get("blocked"):
        return result
    if isinstance(result, dict) and "phases" not in result:
        result["phases"] = phase_progress(orch.ctx)[0]
    return result


# ==================== 流程引擎（六阶段 + 流转条件 + 回退分支） ====================

@app.get("/api/flow")
def flow():
    """全流程定义：六阶段、流转条件（人工闸门）、回退分支，供前端「流程引擎」渲染"""
    return FLOW


@app.get("/api/agents")
def agents_spec():
    """六阶段 Agent 能力矩阵：职责 / 绑定工具 / 能力边界 / 输入 / 输出 / 人工闸门"""
    return AGENT_SPEC


# ==================== 工具层（统一契约 + 工具目录 §3.1–§3.5） ====================

@app.get("/api/tools")
def tools_catalog():
    """工具目录：统一契约 + 五类工具 + 24 个子工具规格"""
    return {
        "contract": {
            "schema_version": SCHEMA_VERSION,
            "input": [
                {"name": "operator_id", "type": "string", "required": True, "desc": "操作人 ID，用于权限与审计"},
                {"name": "project_id", "type": "string", "required": True, "desc": "所属审计项目，用于四级隔离"},
                {"name": "schema_version", "type": "string", "required": True, "desc": "工具参数 Schema 版本号"},
                {"name": "context", "type": "object", "required": False, "desc": "阶段上下文（阶段、当前已确认结论快照）"},
                {"name": "arguments", "type": "object", "required": True, "desc": "工具专属参数"},
            ],
            "output": [
                {"name": "trace_id", "type": "string", "required": True, "desc": "全链路追踪 ID"},
                {"name": "result_status", "type": "enum", "required": True, "desc": "success / partial / blocked / error"},
                {"name": "source_refs", "type": "array", "required": True, "desc": "来源引用列表（数据出处、知识条目、法规条款）"},
                {"name": "data", "type": "object/array", "required": False, "desc": "结构化业务结果"},
                {"name": "risk_level", "type": "enum", "required": False, "desc": "low / medium / high，决定是否触发人工闸门"},
            ],
            "idempotency": "所有写入类工具幂等：同一 request_id 重复调用返回同一结果，不重复落库。",
        },
        "families": tool_families(),
        "tools": TOOL_SPECS,
    }


@app.post("/api/tool")
async def tool_api(req: Request):
    """工具统一调用入口：按统一契约校验 → 项目隔离 → 分发到具体工具 → 返回标准信封。"""
    body = await req.json()
    ok, missing = validate(body)
    if not ok:
        return JSONResponse(
            envelope("blocked", data={"reason": f"契约校验未通过，缺少：{'、'.join(missing)}"}, risk_level="low"),
            status_code=400,
        )

    project_id = body.get("project_id")
    orch = _get_orch(project_id)  # 四级隔离：project_id 即项目上下文

    # 只接受无副作用的追踪字段；阶段、审批、结论等状态只能由服务端编排器产生，
    # 防止调用方通过 context 伪造已确认条件或覆盖项目上下文。
    context = body.get("context") or {}
    if not isinstance(context, dict):
        return JSONResponse(envelope("blocked", data={"reason": "context 必须是对象"}, risk_level="low"), status_code=400)
    safe_context = {k: context[k] for k in ("trace_id", "task_id") if k in context}

    args = dict(body.get("arguments") or {})
    args.setdefault("request_id", args.get("request_id"))
    args.setdefault("context", safe_context)
    result = tool_dispatch(orch, args)
    _persist(project_id, orch)
    return result


# ==================== 知识库（两级 + 元数据 + 检索配置） ====================

@app.get("/api/knowledge")
def knowledge():
    """两级知识库结构 + 检索配置 + 示例条目清单"""
    return knowledge_manifest()


@app.get("/api/admin/data-sources")
def admin_data_sources(request: Request):
    """业务真实数据与评测集目录清单；只返回元数据，不把原始文件直接暴露给前端。"""
    require_admin(request)
    root = os.getenv("AUDIT_DATA_DIR", "/Users/mac/作品集/业务真实数据")
    items = []
    if os.path.isdir(root):
        for current, _, files in os.walk(root):
            for name in sorted(files):
                if name.startswith("."):
                    continue
                path = os.path.join(current, name)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = 0
                items.append({"name": name, "path": path, "size": size,
                              "type": os.path.splitext(name)[1].lower(),
                              "role": "evaluation_or_business_data"})
    return {"root": root, "items": items, "count": len(items),
            "policy": "原始业务文件只读；进入模型前必须脱敏、字段映射和权限校验"}


@app.post("/api/login")
async def login(req: Request):
    """原型登录页的后端校验入口。生产环境应替换为企业 SSO，禁止使用演示口令。"""
    body = await req.json()
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    expected_user = os.getenv("ADMIN_USERNAME", "admin")
    expected_password = os.getenv("ADMIN_PASSWORD", "admin")
    if username != expected_user or password != expected_password:
        return JSONResponse({"ok": False, "err": "用户名或密码错误"}, status_code=401)
    # Token 仅用于原型接入；正式环境由 SSO/JWT 网关签发并校验。
    token = _token_sign({"sub": username, "name": os.getenv("ADMIN_DISPLAY_NAME", "系统管理员"),
                         "department": os.getenv("DEFAULT_ORG", "未明确"), "role": "system_admin",
                         "exp": int(time.time()) + 8 * 3600})
    return {"ok": True, "token": token, "user": {"id": username, "name": "系统管理员", "role": "system_admin", "roleName": "系统管理员"}}


@app.get("/api/admin/users")
def admin_users(request: Request):
    require_admin(request)
    return {
        "users": [{"id": k, "name": role_name(k), "role": k, "department": "审计部门", "status": "enabled"} for k in ROLES],
        "stats": {"users": len(ROLES), "roles": len(ROLES), "online": 0, "pending": 0},
    }


@app.get("/api/admin/agents")
def admin_agents(request: Request):
    require_admin(request)
    saved = {item["agent_id"]: item for item in db.load_agent_configs()}
    base = []
    for item in AGENT_SPEC.get("agents", []):
        key = "agent-" + item["key"]
        override = saved.get(key, {})
        base.append({**item, "agent_id": key, "status": override.get("status", "enabled"), "version": override.get("version", 1), "model": override.get("model", os.getenv("MODEL_NAME", "deepseek-chat"))})
    payload = dict(AGENT_SPEC)
    payload["agents"] = base
    payload["model"] = {"provider": os.getenv("MODEL_PROVIDER", "deepseek"), "name": os.getenv("MODEL_NAME", "deepseek-chat"), "temperature": os.getenv("MODEL_TEMPERATURE", "0.3")}
    payload["runtime"] = {"orchestrator": "LangGraph（规划中）", "retriever": "BM25（当前）", "tool_gateway": "enabled"}
    return payload


@app.get("/api/admin/knowledge")
def admin_knowledge(request: Request):
    require_admin(request)
    payload = knowledge_manifest()
    payload["managed_assets"] = db.load_knowledge_assets()
    return payload


@app.get("/api/admin/metrics")
def admin_metrics(request: Request):
    require_admin(request)
    manifest = knowledge_manifest()
    events = db.load_audit_events(1000)
    ai_events = [e for e in events if "AI" in e.get("action", "") or "TASK" in e.get("action", "")]
    return {"projects": db.count_projects(), "knowledge_docs": len(manifest.get("docs", [])), "ai_calls": len(ai_events), "success_rate": None, "source": "项目和管理审计事件为真实数据；模型质量指标需评测服务产出"}


@app.get("/api/admin/tools")
def admin_tools(request: Request):
    require_admin(request)
    return {"tools": TOOL_SPECS, "families": tool_families()}


@app.get("/api/admin/prompts")
def admin_prompts(request: Request):
    require_admin(request)
    return {"prompts": db.load_prompts()}


@app.post("/api/admin/prompts")
async def admin_prompt_update(request: Request):
    admin = require_admin(request)
    body = await request.json()
    required = ("prompt_id", "prompt_key", "name", "agent_key", "content")
    if any(not str(body.get(k, "")).strip() for k in required):
        return JSONResponse({"err": "prompt_id、prompt_key、name、agent_key、content 必填"}, status_code=400)
    item = {**body, "updated_by": admin["sub"]}
    db.upsert_prompt(item)
    await _write_admin_event(request, "PROMPT_CONFIG_UPDATED", item["prompt_id"], {"status": item.get("status", "draft")})
    return {"ok": True, "data": item}


@app.post("/api/admin/agents")
async def admin_agent_update(request: Request):
    admin = require_admin(request)
    body = await request.json()
    required = ("agent_id", "name", "stage")
    if any(not str(body.get(k, "")).strip() for k in required):
        return JSONResponse({"err": "agent_id、name、stage 必填"}, status_code=400)
    item = {**body, "updated_by": admin["sub"], "config_json": json.dumps(body.get("config", {}), ensure_ascii=False)}
    db.upsert_agent_config(item)
    await _write_admin_event(request, "AGENT_CONFIG_UPDATED", item["agent_id"], {"status": item.get("status", "enabled")})
    return {"ok": True, "data": item}


@app.post("/api/admin/knowledge")
async def admin_knowledge_update(request: Request):
    admin = require_admin(request)
    body = await request.json()
    required = ("asset_id", "title", "category", "source", "content")
    if any(not str(body.get(k, "")).strip() for k in required):
        return JSONResponse({"err": "asset_id、title、category、source、content 必填"}, status_code=400)
    item = {**body, "updated_by": admin["sub"]}
    db.upsert_knowledge_asset(item)
    await _write_admin_event(request, "KNOWLEDGE_ASSET_UPDATED", item["asset_id"], {"status": item.get("status", "pending")})
    return {"ok": True, "data": item}


@app.delete("/api/admin/knowledge/{asset_id}")
async def admin_knowledge_delete(asset_id: str, request: Request):
    require_admin(request)
    db.delete_knowledge_asset(asset_id)
    await _write_admin_event(request, "KNOWLEDGE_ASSET_DELETED", asset_id, {})
    return {"ok": True, "asset_id": asset_id}


@app.post("/api/admin/knowledge/publish")
async def admin_knowledge_publish(request: Request):
    """发布当前 enabled 资产并重建本进程 RAG 索引。"""
    admin = require_admin(request)
    count = reload_retriever()
    await _write_admin_event(request, "KNOWLEDGE_INDEX_PUBLISHED", "knowledge-index", {"chunk_count": count, "operator": admin["sub"]})
    return {"ok": True, "chunk_count": count, "message": "知识资产已发布并完成当前实例索引刷新"}


@app.get("/api/admin/audit-events")
def admin_audit_events(request: Request, limit: int = 200):
    require_admin(request)
    return {"events": db.load_audit_events(limit)}


# ==================== 历史整改台账（真实数据） ====================

@app.get("/api/ledger")
def ledger(year: int | None = None, cat: str | None = None, status: str | None = None,
           keyword: str | None = None, page: int = 1, size: int = 20):
    """真实历史整改台账：汇总 + 类别选项 + 筛选分页列表（脱敏数据）"""
    rows = load_ledger()
    page = max(1, page)
    size = min(max(1, size), 100)
    return {
        "summary": summarize(rows),
        "categories": category_options(rows),
        "filters": {"year": year, "cat": cat, "status": status, "keyword": keyword},
        "result": filter_rows(rows, year=year, cat=cat, status=status, keyword=keyword, page=page, size=size),
    }


# ==================== 审计问题库（真实数据，脱敏） ====================

@app.get("/api/problems")
def problems(year: int | None = None, supervision: str | None = None, domain: str | None = None,
             keyword: str | None = None, page: int = 1, size: int = 20):
    """真实审计问题库：汇总 + 筛选项 + 筛选分页列表（脱敏数据）"""
    rows = load_problems()
    page = max(1, page)
    size = min(max(1, size), 100)
    return {
        "summary": problems_summarize(rows),
        "options": problems_options(rows),
        "filters": {"year": year, "supervision": supervision, "domain": domain, "keyword": keyword},
        "result": filter_problems(rows, year=year, supervision=supervision, domain=domain,
                                  keyword=keyword, page=page, size=size),
    }


# ==================== 健康检查 & 会话回放 ====================

@app.get("/health")
def health():
    """健康检查：服务存活 + 持久化层状态（供部署探活）"""
    return {
        "status": "ok",
        "db": os.path.basename(db.DB_PATH),
        "persisted_projects": db.count_projects(),
    }


@app.get("/api/chat")
def chat_history(session_id: str, limit: int = 200):
    """会话对话流水（粗粒度审计轨迹，供「会话记录」回放）"""
    return {"session_id": session_id, "messages": db.load_chat(session_id, limit)}


# ==================== 静态前端 ====================

@app.get("/")
def index():
    # 原型页面迭代频繁，禁止浏览器缓存旧版 HTML；生产环境可改为构建 hash 缓存。
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"},
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("AUDIT_HOST", "0.0.0.0")
    port = int(os.getenv("AUDIT_PORT", "8000"))
    print("审计数字员工 · Web 版已就绪")
    print(f"  本机访问 http://127.0.0.1:{port}")
    print(f"  局域网访问 http://<本机局域网IP>:{port}")
    uvicorn.run(app, host=host, port=port)
