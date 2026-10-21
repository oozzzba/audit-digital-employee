"""工具网关：所有 Agent 工具请求的统一入口。

Agent 不直接持有数据库/RAG/文件连接；网关负责公共契约、阶段白名单和调用留痕。
"""
import hashlib
import json
import time
import uuid

import db
from tool_catalog import dispatch
from tool_contract import SCHEMA_VERSION, validate

_STAGE_TOOLS = {
    "initiation": {"entity_extract", "json_validate", "template_load", "form_schema_assemble", "vector_rag_search", "log_format", "permission_check", "resource_load_check", "risk_level_assemble"},
    "preaudit": {"template_search", "vector_rag_search", "log_format"},
}


def call(request, *, session_id="", operator_id=""):
    """校验并执行工具。兼容旧版 dispatch 参数，但新调用必须使用标准信封。"""
    request = request or {}
    if "arguments" in request:
        ok, missing = validate(request)
        if not ok:
            return {"result_status": "blocked", "data": {"reason": "工具契约校验失败", "missing": missing}}
        args = dict(request["arguments"])
        tool = args.get("tool") or request.get("tool")
        stage = request.get("context", {}).get("stage") or request.get("stage")
        if stage and tool not in _STAGE_TOOLS.get(stage, set()):
            return {"result_status": "blocked", "data": {"reason": "工具不在当前阶段白名单", "tool": tool}}
        started = time.time()
        result = dispatch(None, args)
        try:
            db.append_audit_event(operator_id or request.get("operator_id", "system"), "TOOL_CALL", "TOOL", tool or "", {"trace_id": request.get("trace_id", ""), "status": result.get("result_status")})
        except Exception:
            pass
        result.setdefault("trace_id", request.get("trace_id") or uuid.uuid4().hex)
        result["duration_ms"] = round((time.time() - started) * 1000)
        return result
    return dispatch(None, request)


def request(tool, arguments, *, stage, project_id, operator_id, trace_id=None):
    args = dict(arguments or {})
    args["tool"] = tool
    return call({"trace_id": trace_id or uuid.uuid4().hex, "operator_id": operator_id, "project_id": project_id,
                 "schema_version": SCHEMA_VERSION, "context": {"stage": stage}, "arguments": args},
                operator_id=operator_id)
