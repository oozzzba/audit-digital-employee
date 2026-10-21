"""工具调用统一契约（与 SRS §2.5 一致）。

所有工具（取数 / 检索 / 分析 / 生成 / 流程）遵循同一输入/输出信封。

输入（公共）：
    operator_id    string  ✅ 操作人 ID，用于权限与审计
    project_id     string  ✅ 所属审计项目，用于四级隔离
    schema_version string  ✅ 工具参数 Schema 版本号
    context        object  —  阶段上下文（阶段、当前已确认结论快照）
    arguments      object  ✅ 工具专属参数

输出（公共）：
    trace_id       string  ✅ 全链路追踪 ID
    result_status  enum    ✅ success / partial / blocked / error
    source_refs    array   ✅ 来源引用列表（数据出处、知识条目、法规条款）
    data           object  —  结构化业务结果
    risk_level     enum    —  low / medium / high，决定是否触发人工闸门

幂等：所有写入类工具必须幂等——同一 request_id 重复调用返回同一结果，不重复落库。
request_id 由 Agent 生成并随调用传递。
"""
import uuid

SCHEMA_VERSION = "1.0"

RESULT_STATUS = ("success", "partial", "blocked", "error")
RISK_LEVEL = ("low", "medium", "high")

# 写入类工具：需要 request_id 幂等
WRITE_TOOLS = {"generate", "flow"}

# request_id -> 结果缓存（进程内，演示幂等语义）
_IDEMPOTENCY = {}


def make_trace_id():
    return uuid.uuid4().hex[:32]


def validate(payload):
    """校验统一契约必填字段，返回 (ok, missing_list)。"""
    missing = []
    for field in ("operator_id", "project_id", "schema_version", "arguments"):
        if not payload.get(field):
            missing.append(field)
    if not isinstance(payload.get("arguments"), dict):
        missing.append("arguments(应为对象)")
    if payload.get("schema_version") and payload.get("schema_version") != SCHEMA_VERSION:
        missing.append(f"schema_version(应为 {SCHEMA_VERSION})")
    return (len(missing) == 0), missing


def envelope(result_status="success", source_refs=None, data=None, risk_level="low",
             trace_id=None, replay=False, note=""):
    """构造标准输出信封。"""
    out = {
        "trace_id": trace_id or make_trace_id(),
        "result_status": result_status,
        "source_refs": source_refs or [],
        "data": data,
        "risk_level": risk_level,
    }
    if replay:
        out["replayed"] = True
    if note:
        out["note"] = note
    return out


def call_idempotent(request_id, fn, *args, **kwargs):
    """写入类工具幂等执行：同一 request_id 命中缓存直接返回，不重复执行/落库。"""
    if request_id and request_id in _IDEMPOTENCY:
        cached = dict(_IDEMPOTENCY[request_id])
        cached["replayed"] = True
        cached["note"] = (cached.get("note", "") + "；命中幂等缓存，未重复落库").strip("；")
        return cached
    result = fn(*args, **kwargs)
    if request_id:
        _IDEMPOTENCY[request_id] = dict(result)
    return result
