"""工具目录（§3.1–§3.5）+ 统一契约分发。

五类工具：取数 / 检索 / 分析 / 生成 / 流程，全部遵循 tool_contract 统一信封。
每个子工具含：调用条件 / 输入参数 / 输出参数 / 规则与异常。

实现策略（诚信原则）：
  - 取数/检索/生成/分析类尽量接真实脱敏数据与真实生成；
  - 流程类为「写入类」工具，按 request_id 幂等，写入项目上下文并留痕；
  - 无真实数据支撑的字段（如对外导出 file_url）不虚构，返回 blocked/说明。
"""
import uuid
import json
import time

from tool_contract import envelope, call_idempotent, make_trace_id, SCHEMA_VERSION

from retriever import BM25Retriever, load_documents, RETRIEVAL_CONFIG
from data_tool import (
    DATA_SOURCES,
    match_source,
    load_rows,
    detect_anomalies,
    format_anomalies,
    detect_travel,
    detect_procurement,
)
from ledger_data import load_ledger
from problem_data import load_problems
from generator import (
    parse_task,
    generate_workpaper,
    generate_report,
    generate_audit_plan,
    generate_rectification_plan,
    generate_audit_notice,
    generate_seek_opinion,
)

CHUNKS = load_documents()
RETRIEVER = BM25Retriever(CHUNKS)


def reload_retriever():
    """管理端发布知识资产后重建进程内检索索引；生产多实例需广播失效消息。"""
    global CHUNKS, RETRIEVER
    CHUNKS = load_documents()
    RETRIEVER = BM25Retriever(CHUNKS)
    return len(CHUNKS)


# ==================== 通用辅助 ====================


def _entity_extract(orch, args):
    """立项意图解析专用实体抽取工具：只做文本到 JSON 的结构化转换，不访问业务数据。"""
    text = str(args.get("text") or "").strip()
    if not text:
        return envelope("blocked", data={"reason": "text 必填"}, risk_level="low")
    parsed = parse_task(text, str(args.get("retrieval_context") or ""))
    return envelope("success", args.get("source_refs") or [], {"json": parsed}, "low")


def _json_validate(orch, args):
    """立项意图解析专用 JSON 校验工具：固定字段、缺失占位、禁止额外字段。"""
    required = ["审计对象", "审计范围", "审计类型", "审计目标", "审计期间"]
    payload = args.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return envelope("blocked", data={"reason": "模型输出不是合法 JSON"}, risk_level="low")
    if not isinstance(payload, dict):
        return envelope("blocked", data={"reason": "payload 必须是 JSON 对象"}, risk_level="low")
    extra = sorted(set(payload) - set(required))
    if extra:
        return envelope("blocked", data={"reason": "包含未允许字段", "extra_fields": extra}, risk_level="low")
    normalized = {key: str(payload.get(key) or "【待补充】").strip() or "【待补充】" for key in required}
    return envelope("success", [], {"json": json.dumps(normalized, ensure_ascii=False), "fields": required}, "low")


def _risk_level_assemble(orch, args):
    """按代码硬规则结果组装风险分级；不判断事实真伪、不查询数据。"""
    facts = args.get("facts")
    if not isinstance(facts, list):
        return envelope("blocked", data={"reason": "facts 必须是数组"}, risk_level="low")
    risks = []
    for index, fact in enumerate(facts, 1):
        if not isinstance(fact, dict):
            continue
        blocking = bool(fact.get("blocking") or fact.get("hard_fail") or fact.get("severity") == "high")
        risks.append({
            "risk_code": str(fact.get("code") or f"INIT-{index:03d}"),
            "risk_description": str(fact.get("description") or fact.get("message") or "代码硬规则命中，待人工处理"),
            "evidence_refs": fact.get("evidence_refs") or fact.get("source_record_ids") or [],
            "risk_level": "高风险-拦截" if blocking else "提示风险-仅提醒",
            "disposal_advice": str(fact.get("disposal_advice") or ("阻断项目创建并转授权角色处理" if blocking else "提示人工关注并留痕")),
            "needs_human_review": True,
        })
    return envelope("success", [], {"risks": risks}, "high" if any(r["risk_level"] == "高风险-拦截" for r in risks) else "low")


def _permission_check(orch, args):
    role = str(args.get("actor_role") or "")
    allowed = role in {"project_lead", "chief_auditor", "group_lead", "admin"}
    return envelope("success", [], {"allowed": allowed, "actor_role": role}, "low" if allowed else "high")


def _resource_load_check(orch, args):
    load = args.get("load")
    limit = args.get("limit")
    if load is None or limit is None:
        return envelope("partial", [], {"available": False, "reason": "人员资源接口未接入"}, "low")
    over = float(load) > float(limit)
    return envelope("success", [], {"available": True, "over_limit": over, "load": load, "limit": limit}, "high" if over else "low")


def _template_load(orch, args):
    """代码加载标准立项模板，模型不得自行构造 Schema。"""
    result = _template_search(orch, {"template_type": args.get("template_type") or "审计立项", "top_k": 5})
    return result


def _form_schema_assemble(orch, args):
    fields = args.get("fields")
    if not isinstance(fields, list):
        return envelope("blocked", data={"reason": "fields 必须是数组"}, risk_level="low")
    return envelope("success", [], {"schema": {"fields": fields, "readonly_system_fields": ["项目编号", "项目年度"]}}, "low")


def _vector_rag_search(orch, args):
    query = str(args.get("query") or "").strip()
    if not query:
        return envelope("blocked", data={"reason": "缺少 query"}, risk_level="low")
    results = RETRIEVER.retrieve(query, top_k=args.get("top_k") or 8, stage="initiation", threshold=args.get("threshold") or 0.05)
    return envelope("success" if results else "partial", [{"source": r.get("source"), "library": r.get("library")} for r in results], {"items": results}, "low")


def _log_format(orch, args):
    logs = args.get("logs") or []
    if not isinstance(logs, list):
        return envelope("blocked", data={"reason": "logs 必须是数组"}, risk_level="low")
    text = "；".join(f"字段“{x.get('field','')}”由“{x.get('old') or '空'}”变更为“{x.get('new') or '空'}”（{x.get('source','代码记录')}）" for x in logs if isinstance(x, dict))
    return envelope("success", [], {"readable_change_log": text or "暂无字段变更记录"}, "low")

def _fmt(retrieved):
    return "\n\n".join(f"[来源：{r['source']}]\n{r['text']}" for r in retrieved)


def _scope(orch, args):
    return args.get("scope") or args.get("query") or orch.ctx.get("parsed") or orch.ctx.get("task") or ""


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _get_snapshots(orch):
    return orch.ctx.setdefault("_snapshots", {})


def _get_snapshot(orch, snapshot_id):
    return _get_snapshots(orch).get(snapshot_id) if snapshot_id else None


def _rows_from(orch, args):
    """优先取快照，否则按 scope 匹配数据源；返回 rows 或 None"""
    snap = _get_snapshot(orch, args.get("snapshot_id"))
    if snap and isinstance(snap.get("data"), list):
        return snap["data"]
    _, meta = match_source(_scope(orch, args))
    return load_rows(meta["file"]) if meta else None


def _detector_for(rows):
    if rows and "报销总金额" in rows[0]:
        return detect_travel
    if rows and "采购金额" in rows[0]:
        return detect_procurement
    return detect_travel


def _amount_key(rows):
    return "报销总金额" if (rows and "报销总金额" in rows[0]) else "采购金额"


def _date_key(rows):
    return "出差日期" if (rows and "出差日期" in rows[0]) else "采购日期"


# ==================== 3.1 取数工具 ====================

def _flow_query(orch, args):
    source_key, meta = None, None
    if args.get("source"):
        src = args.get("source")
        if src in DATA_SOURCES:
            source_key, meta = src, DATA_SOURCES[src]
        else:
            meta = {"file": src}
            source_key = src
    else:
        source_key, meta = match_source(_scope(orch, args))
    if not meta:
        return envelope("blocked", data={"reason": "数据源未接入或未指定 source"}, risk_level="low")
    rows = load_rows(meta["file"])
    date_from, date_to = args.get("date_from"), args.get("date_to")
    if not (date_from or date_to):
        return envelope("blocked", data={"reason": "缺少时间窗：请提供 date_from / date_to", "total": len(rows)}, risk_level="low")
    out = rows
    dk = _date_key(rows)
    if dk:
        if date_from:
            out = [r for r in out if str(r.get(dk, "")) >= str(date_from)]
        if date_to:
            out = [r for r in out if str(r.get(dk, "")) <= str(date_to)]
    account = args.get("account")
    if account:
        acct_keys = ("员工", "供应商", "账户", "往来单位")
        out = [r for r in out if any(account in str(r.get(k, "")) for k in acct_keys)]
    fields = args.get("fields")
    if fields:
        out = [{k: r.get(k, "") for k in fields if k in r} for r in out]
    page = max(1, int(args.get("page") or 1))
    size = min(max(1, int(args.get("size") or 20)), 100)
    total = len(out)
    return envelope("success", [{"data_source": meta["file"]}],
                    {"data": out[(page - 1) * size:(page - 1) * size + size], "total": total,
                     "page": page, "size": size}, "low")


def _ledger_query(orch, args):
    ledger_type = args.get("ledger_type")
    if not ledger_type:
        return envelope("blocked", data={"reason": "需指定 ledger_type（合同台账/采购台账/报销台账/整改台账/问题台账）"}, risk_level="low")
    if ledger_type in ("整改台账", "rectification"):
        rows = load_ledger()
        src = "整改台账_脱敏.csv"
    elif ledger_type in ("问题台账", "问题库", "problem"):
        rows = load_problems()
        src = "审计问题库_脱敏.csv"
    elif "采购" in ledger_type:
        rows = load_rows("物资采购明细.csv")
        src = "物资采购明细.csv"
    elif "报销" in ledger_type or "差旅" in ledger_type:
        rows = load_rows("差旅费报销明细.csv")
        src = "差旅费报销明细.csv"
    else:
        rows = load_ledger()
        src = "整改台账_脱敏.csv"
    out = rows
    for k, v in (args.get("filter") or {}).items():  # 白名单：仅等于匹配，防注入
        if isinstance(v, str):
            out = [r for r in out if r.get(k) == v]
    page = max(1, int(args.get("page") or 1))
    size = min(max(1, int(args.get("size") or 20)), 100)
    total = len(out)
    return envelope("success", [{"data_source": src}],
                    {"data": out[(page - 1) * size:(page - 1) * size + size], "total": total}, "low")


def _snapshot_create(orch, args):
    qr, biz_date, purpose = args.get("query_result"), args.get("biz_date"), args.get("purpose")
    if not qr or not biz_date or not purpose:
        return envelope("blocked", data={"reason": "query_result / biz_date / purpose 必填"}, risk_level="low")
    sid = uuid.uuid4().hex[:16]
    _get_snapshots(orch)[sid] = {
        "data": qr, "biz_date": biz_date, "purpose": purpose,
        "immutable": True, "created_at": _now(),
    }
    return envelope("success", [{"snapshot_id": sid}],
                    {"snapshot_id": sid, "snapshot_time": _now(),
                     "note": "快照不可变、不可删，已纳入证据链"}, "medium")


def _quality_check(orch, args):
    sid = args.get("snapshot_id")
    snap = _get_snapshot(orch, sid)
    if not snap:
        return envelope("blocked", data={"reason": "快照不存在"}, risk_level="low")
    rows = snap["data"] if isinstance(snap.get("data"), list) else []
    checks = args.get("checks") or ["完整性", "一致性", "空值", "重复"]
    report = {"缺失": 0, "空值": 0, "重复": 0, "总记录数": len(rows)}
    if rows and isinstance(rows[0], dict):
        keys = list(rows[0].keys())
        for r in rows:
            for k in keys:
                if not r.get(k):
                    report["空值"] += 1
        seen = set()
        for r in rows:
            sig = tuple(str(r.get(k, "")) for k in keys)
            if sig in seen:
                report["重复"] += 1
            seen.add(sig)
    bad = report["空值"] + report["重复"]
    status = "partial" if bad else "success"
    return envelope(status, [{"snapshot_id": sid}],
                    {"report": report, "checks": checks,
                     "note": "" if not bad else "质量不达标，列出问题项，不进入分析"}, "low")


def _masked_export(orch, args):
    sid = args.get("snapshot_id")
    snap = _get_snapshot(orch, sid)
    if not snap:
        return envelope("blocked", data={"reason": "快照不存在"}, risk_level="low")
    rules = args.get("mask_rules")
    if not rules:
        return envelope("blocked", data={"reason": "未按规则脱敏不可导出：mask_rules 必填"}, risk_level="low")
    rows = snap["data"] if isinstance(snap.get("data"), list) else []
    masked, log = [], []
    amount_key = "报销总金额" if (rows and "报销总金额" in rows[0]) else "采购金额"
    for r in rows:
        m = dict(r)
        if "金额" in rules and amount_key in m:
            m[amount_key] = "***（已脱敏）"
            log.append(f"金额脱敏：{amount_key}")
        for k in ("手机号", "身份证", "电话"):
            if k in rules:
                log.append(f"{k} 脱敏：当前数据源无该字段，跳过")
        masked.append(m)
    return envelope("success", [],
                    {"file_url": None, "masked": masked[:5], "mask_log": log,
                     "note": "演示返回脱敏内容与留痕日志；正式导出文件由对象存储生成，未虚构 URL"}, "medium")


# ==================== 3.2 检索工具 ====================

def _eff_expired(eff):
    return any(k in str(eff) for k in ("废止", "失效", "过期"))


def _policy_search(orch, args):
    query = args.get("query")
    if not query:
        return envelope("blocked", data={"reason": "缺少 query"}, risk_level="low")
    results = RETRIEVER.retrieve(query, top_k=args.get("top_k"), threshold=args.get("threshold"))
    items = []
    for r in results:
        expired = _eff_expired(r.get("effective"))
        if args.get("effective") and expired:
            continue
        items.append({"source": r["source"], "article": r.get("article", ""),
                      "effective": r.get("effective", ""), "expired": expired,
                      "confidence": r.get("confidence"), "similarity": r.get("similarity"),
                      "text": r["text"]})
    refs = [{"source": r["source"], "article": r.get("article", ""), "effective": r.get("effective", "")} for r in results]
    return envelope("success" if items else "partial", refs, {"items": items},
                    "low", note="" if items else "未召回（阈值或阶段过滤后无结果）")


def _case_search(orch, args):
    query = args.get("query")
    if not query:
        return envelope("blocked", data={"reason": "缺少 query"}, risk_level="low")
    rows = load_problems()
    if query:
        hay = ("问题名称", "问题定性", "项目名称", "业务领域")
        rows = [r for r in rows if query in "".join(r.get(k, "") for k in hay)]
    cases = [{"问题名称": r.get("问题名称", ""), "问题定性": r.get("问题定性", ""),
              "业务领域": r.get("业务领域", ""), "涉及金额_万元": r.get("问题涉及金额_万元", ""),
              "年度": r.get("年度", "")} for r in rows[:10]]
    return envelope("success", [{"data_source": "审计问题库_脱敏.csv"}],
                    {"cases": cases, "total": len(cases), "note": "脱敏后返回；案例结论仅作参考，不替代本次研判"}, "low")


def _template_search(orch, args):
    tt = args.get("template_type")
    if not tt:
        return envelope("blocked", data={"reason": "需指定 template_type"}, risk_level="low")
    results = RETRIEVER.retrieve(f"{tt} 模板", top_k=args.get("top_k") or 3)
    results = [r for r in results if "模板" in r.get("library", "") or "规范" in r.get("source", "")]
    if not results:
        return envelope("blocked", data={"reason": "无匹配模板，请人工建模板"}, risk_level="low")
    r = results[0]
    return envelope("success", [{"source": r["source"], "version": "最新生效版本"}],
                    {"template": {"source": r["source"], "content": r["text"], "fields": ["见模板正文"]}}, "low")


def _clause_locate(orch, args):
    doc_id = args.get("doc_id")
    if not doc_id:
        return envelope("blocked", data={"reason": "缺少 doc_id"}, risk_level="low")
    keyword = args.get("keyword", "")
    clauses = [c for c in CHUNKS if c["source"] == doc_id and (not keyword or keyword in c["text"])]
    if not clauses:
        clauses = [c for c in CHUNKS if doc_id in c["source"] and (not keyword or keyword in c["text"])]
    return envelope("success" if clauses else "partial", [{"source": doc_id}],
                    {"clauses": [{"source": c["source"], "text": c["text"]} for c in clauses[:5]]},
                    "low", note="" if clauses else "未定位到条款；条款必须指向原文，禁止二次概括失实")


# ==================== 3.3 分析工具 ====================

def _rule_screen(orch, args):
    rows = _rows_from(orch, args)
    if rows is None:
        return envelope("blocked", data={"reason": "需 snapshot_id 或可匹配的 scope"}, risk_level="low")
    anomalies, _ = _detector_for(rows)(rows)
    hits = [{"规则": a.get("类型"), "主体": a.get("主体"), "金额": a.get("金额"), "说明": a.get("说明")}
            for a in anomalies]
    return envelope("success", [], {"hits": hits, "count": len(hits),
                                    "rule_set": args.get("rule_set"), "note": "输出「疑似」结论，不得定性"}, "medium")


def _metric_calc(orch, args):
    rows = _rows_from(orch, args)
    if rows is None:
        return envelope("blocked", data={"reason": "需 snapshot_id 或可匹配的 scope"}, risk_level="low")
    ak = _amount_key(rows)
    metrics = args.get("metrics") or ["总金额", "总记录数"]
    result = []
    for m in metrics:
        if m in ("总金额", "sum"):
            result.append({"指标": "总金额", "值": round(sum(float(r[ak]) for r in rows), 2), "口径": f"sum({ak})"})
        if m in ("总记录数", "count"):
            result.append({"指标": "总记录数", "值": len(rows), "口径": "count(*)"})
        if m in ("均值", "avg"):
            result.append({"指标": "均值", "值": round(sum(float(r[ak]) for r in rows) / len(rows), 2), "口径": f"avg({ak})"})
    return envelope("success", [], {"result": result, "formula": "定点/分精度，可回放可追溯"}, "low")


def _trend_chart(orch, args):
    rows = _rows_from(orch, args)
    if rows is None:
        return envelope("blocked", data={"reason": "需 snapshot_id 或可匹配的 scope"}, risk_level="low")
    dk, ak = _date_key(rows), _amount_key(rows)
    buckets = {}
    for r in rows:
        month = (r.get(dk) or "")[:7]
        buckets[month] = buckets.get(month, 0) + float(r[ak])
    chart = {"chart_type": args.get("chart_type") or "折线",
             "series": [{"name": "金额", "data": [[k, v] for k, v in sorted(buckets.items())]}]}
    return envelope("success", [], {"chart": chart, "note": "图表仅呈现数据，不附加定性结论"}, "low")


def _anomaly_detect(orch, args):
    r = _rule_screen(orch, args)
    if r.get("data") is not None:
        r["data"]["note"] = "统计/规则异常仅提示「需关注」，必须人工核验；高风险转人工闸门"
    r["risk_level"] = "medium"
    return r


# ==================== 3.4 生成工具 ====================

def _draft_generate(orch, args):
    ctx = orch.ctx
    if not ctx.get("parsed"):
        return envelope("blocked", data={"reason": "证据已确认后生成底稿；缺少任务解析，请先发起审计"}, risk_level="medium")
    draft = generate_workpaper(ctx.get("parsed", ""), ctx.get("risks", ""),
                               ctx.get("anomalies_text", ""), _fmt(ctx.get("retrieved", [])))
    return envelope("success", [], {"draft": draft, "note": "仅引用已确认内容，缺失证据标黄提示"}, "medium")


def _report_generate(orch, args):
    ctx = orch.ctx
    if not ctx.get("workpaper"):
        return envelope("blocked", data={"reason": "底稿与问题已确认后生成报告；缺少底稿"}, risk_level="medium")
    prev = ctx.get("report", "")
    report = generate_report(ctx.get("parsed", ""), ctx.get("risks", ""), ctx.get("workpaper", ""),
                             _fmt(ctx.get("retrieved", [])), audit_type=ctx.get("audit_type", "其他"))
    ctx["report"] = report
    diff = "首版（无历史版本）" if not prev else "已生成修订版，差异见正文「版本说明」"
    return envelope("success", [], {"report": report, "diff": diff, "note": "版本对比留痕"}, "medium")


def _scheme_generate(orch, args):
    ctx = orch.ctx
    if not ctx.get("initiation"):
        return envelope("blocked", data={"reason": "需立项/问题上下文；缺少立项申请"}, risk_level="medium")
    scheme = generate_audit_plan(ctx.get("parsed", ""), ctx.get("initiation", ""), "")
    return envelope("success", [], {"scheme": scheme, "note": "范围/期限不得自行确定，须人工确认"}, "medium")


def _ledger_generate(orch, args):
    ctx = orch.ctx
    if ctx.get("risks") and ctx.get("report"):
        ledger = generate_rectification_plan(ctx.get("risks", ""), ctx.get("report", ""), "")
        return envelope("success", [], {"ledger": ledger, "note": "台账反映已确认数据；不得自行销号/改期限"}, "medium")
    return envelope("blocked", data={"reason": "需已确认问题与报告"}, risk_level="medium")


def _doc_generate(orch, args):
    ctx = orch.ctx
    parsed = ctx.get("parsed", "")
    doc_type = args.get("doc_type")
    if not doc_type:
        return envelope("blocked", data={"reason": "需指定 doc_type（通知书/征求意见函）"}, risk_level="medium")
    if doc_type in ("通知书", "audit_notice"):
        if not ctx.get("initiation"):
            return envelope("blocked", data={"reason": "缺少立项信息"}, risk_level="medium")
        doc = generate_audit_notice(parsed, ctx.get("initiation", ""), ctx.get("audit_plan", ""))
    elif doc_type in ("征求意见", "seek_opinion"):
        if not ctx.get("report"):
            return envelope("blocked", data={"reason": "缺少报告初稿"}, risk_level="medium")
        doc = generate_seek_opinion(parsed, ctx.get("report", ""))
    else:
        return envelope("blocked", data={"reason": f"暂不支持的 doc_type：{doc_type}"}, risk_level="medium")
    return envelope("success", [], {"doc": doc, "note": "对外文书须人工签发；发布走流程工具"}, "medium")


# ==================== 3.5 流程工具（写入类，幂等） ====================

def _approval_advance(orch, args):
    node, approver = args.get("node"), args.get("approver")
    if not node:
        return envelope("blocked", data={"reason": "缺少审批节点 node"}, risk_level="high")
    if not approver:
        return envelope("blocked", data={"reason": "审批人缺失→转上级/升级提醒；不得代审"}, risk_level="high")
    aid = uuid.uuid4().hex[:12]
    orch.ctx.setdefault("_approvals", []).append(
        {"approval_id": aid, "node": node, "approver": approver,
         "status": "已记录（待正式签批）", "payload": bool(args.get("payload"))})
    return envelope("success", [], {"approval_id": aid, "status": "已记录（待正式签批）", "note": "不得代审"}, "high")


def _notice_dispatch(orch, args):
    if not args.get("receiver"):
        return envelope("blocked", data={"reason": "缺少接收方 receiver"}, risk_level="high")
    if not args.get("approved"):
        return envelope("blocked", data={"reason": "对外下发必须人工审批后执行"}, risk_level="high")
    did = uuid.uuid4().hex[:12]
    orch.ctx.setdefault("_dispatches", []).append(
        {"dispatch_id": did, "receiver": args.get("receiver"), "content": bool(args.get("content"))})
    return envelope("success", [], {"dispatch_id": did, "status": "已下发（高风险留痕）"}, "high")


def _task_create(orch, args):
    if not args.get("assignee") or not args.get("task_type"):
        return envelope("blocked", data={"reason": "assignee / task_type 必填"}, risk_level="medium")
    tid = uuid.uuid4().hex[:12]
    orch.ctx.setdefault("_tasks", []).append(
        {"task_id": tid, "assignee": args.get("assignee"), "due": args.get("due"),
         "task_type": args.get("task_type"), "confirmed": False})
    return envelope("success", [], {"task_id": tid, "note": "任务分派需负责人确认"}, "medium")


_VALID_STATES = {"未开始", "进行中", "已结束"}


def _status_update(orch, args):
    entity, frm, to, reason = args.get("entity"), args.get("from"), args.get("to"), args.get("reason")
    if not (entity and frm and to and reason):
        return envelope("blocked", data={"reason": "entity / from / to / reason 必填"}, risk_level="medium")
    if frm not in _VALID_STATES or to not in _VALID_STATES:
        return envelope("blocked", data={"reason": f"状态须在 {_VALID_STATES} 内，非法流转阻断"}, risk_level="medium")
    if frm == to:
        return envelope("blocked", data={"reason": "原状态与目标状态相同"}, risk_level="medium")
    uid = uuid.uuid4().hex[:12]
    risk = "high" if to == "已结束" else "medium"
    orch.ctx.setdefault("_status_updates", []).append(
        {"update_id": uid, "entity": entity, "from": frm, "to": to, "reason": reason})
    return envelope("success", [], {"update_id": uid,
                                    "note": "高风险状态变更需人工闸门" if risk == "high" else "状态流转已记录"}, risk)


# ==================== 工具规格（§3.1–§3.5，供 /api/tools 渲染） ====================

TOOL_SPECS = [
    # 立项意图解析 Agent 专用工具（无业务数据库访问、无写入）
    {"tool": "entity_extract", "family": "解析", "name": "立项实体抽取", "risk_level": "low", "write": False,
     "condition": "立项意图解析；将用户自然语言转换为固定实体 JSON",
     "inputs": [{"name": "text", "type": "string", "required": True, "desc": "用户原始立项指令"},
                {"name": "retrieval_context", "type": "string", "required": False, "desc": "类型库与历史样本检索片段"}],
     "outputs": [{"name": "json", "desc": "模型输出的实体 JSON"}],
     "rules": ["只做意图识别和实体抽取", "不访问业务数据库", "不做风险判断", "不生成表单"]},
    {"tool": "json_validate", "family": "解析", "name": "立项 JSON 校验", "risk_level": "low", "write": False,
     "condition": "实体抽取后；校验固定 JSON Schema",
     "inputs": [{"name": "payload", "type": "object|string", "required": True, "desc": "待校验 JSON"}],
     "outputs": [{"name": "json", "desc": "补齐【待补充】后的固定 JSON"}, {"name": "fields", "desc": "允许字段列表"}],
     "rules": ["只允许五个固定字段", "缺失字段统一为【待补充】", "存在额外字段直接阻断"]},
    {"tool": "risk_level_assemble", "family": "分析", "name": "风险分级组装", "risk_level": "medium", "write": False,
     "condition": "立项硬规则校验后；将代码事实映射为拦截风险或提示风险",
     "inputs": [{"name": "facts", "type": "array", "required": True, "desc": "代码输出的硬规则事实"}],
     "outputs": [{"name": "risks", "desc": "结构化风险提示数组"}],
     "rules": ["完全采信代码事实", "高风险-拦截阻断创建", "提示风险-仅提醒", "不查询数据库、不修改业务数据"]},
    {"tool": "permission_check", "family": "流程", "name": "立项权限校验", "risk_level": "high", "write": False,
     "condition": "立项创建前由代码校验当前角色", "inputs": [{"name": "actor_role", "type": "string", "required": True, "desc": "当前角色"}], "outputs": [{"name": "allowed", "desc": "是否允许"}], "rules": ["不交给模型判断"]},
    {"tool": "resource_load_check", "family": "流程", "name": "人员资源校验", "risk_level": "medium", "write": False,
     "condition": "立项创建前校验人员负荷", "inputs": [{"name": "load", "type": "number", "required": False, "desc": "当前负荷"}, {"name": "limit", "type": "number", "required": False, "desc": "负荷上限"}], "outputs": [{"name": "over_limit", "desc": "是否超限"}], "rules": ["无资源接口时返回未接入，不伪造结果"]},
    {"tool": "template_load", "family": "生成", "name": "立项模板加载", "risk_level": "low", "write": False,
     "condition": "立项表单填充前；按模板库加载标准表单 Schema", "inputs": [{"name": "template_type", "type": "string", "required": True, "desc": "模板类型"}],
     "outputs": [{"name": "template", "desc": "标准模板与字段 Schema"}], "rules": ["模板由代码检索加载", "不允许模型自造系统字段"]},
    {"tool": "form_schema_assemble", "family": "生成", "name": "表单结构组装", "risk_level": "low", "write": False,
     "condition": "立项表单填充；组装待确认草稿", "inputs": [{"name": "fields", "type": "array", "required": True, "desc": "字段定义"}],
     "outputs": [{"name": "schema", "desc": "表单 Schema"}], "rules": ["项目编号和项目年度只读"]},
    {"tool": "vector_rag_search", "family": "检索", "name": "立项风险溯源检索", "risk_level": "low", "write": False,
     "condition": "立项风险溯源；工具执行 RAG 检索", "inputs": [{"name": "query", "type": "string", "required": True, "desc": "检索词"}],
     "outputs": [{"name": "items", "desc": "带来源检索结果"}], "rules": ["Agent 不自行检索"]},
    {"tool": "log_format", "family": "生成", "name": "变更日志格式化", "risk_level": "low", "write": False,
     "condition": "将代码底层变更日志加工为用户可读文本", "inputs": [{"name": "logs", "type": "array", "required": True, "desc": "代码埋点日志"}],
     "outputs": [{"name": "readable_change_log", "desc": "可读日志"}], "rules": ["不生成底层日志、不修改表单"]},
    # 取数
    {"tool": "flow_query", "family": "取数", "name": "流水查询", "risk_level": "low", "write": False,
     "condition": "现场实施阶段；需指定账户/往来单位/时间窗；权限覆盖对应数据域",
     "inputs": [{"name": "source", "type": "string", "required": True, "desc": "数据源标识"},
                {"name": "account", "type": "string", "required": False, "desc": "账户/往来单位"},
                {"name": "date_from", "type": "string", "required": False, "desc": "起始日期"},
                {"name": "date_to", "type": "string", "required": False, "desc": "结束日期"},
                {"name": "fields", "type": "array", "required": False, "desc": "返回字段白名单"},
                {"name": "page", "type": "int", "required": False, "desc": "分页"}],
     "outputs": [{"name": "data", "desc": "流水明细"}, {"name": "total", "desc": "总数"}, {"name": "source_refs", "desc": "数据出处"}],
     "rules": ["只读，不修改源数据", "超大数据量强制分页", "缺时间窗时返回 blocked 并追问", "越权数据域返回 blocked"]},
    {"tool": "ledger_query", "family": "取数", "name": "台账查询", "risk_level": "low", "write": False,
     "condition": "需指定台账类型（合同台账/采购台账/报销台账等）与筛选条件",
     "inputs": [{"name": "ledger_type", "type": "string", "required": True, "desc": "台账类型"},
                {"name": "filter", "type": "object", "required": False, "desc": "筛选条件"},
                {"name": "page", "type": "int", "required": False, "desc": "分页"}],
     "outputs": [{"name": "data", "desc": "台账记录"}, {"name": "total", "desc": "总数"}, {"name": "source_refs", "desc": "数据出处"}],
     "rules": ["只读", "筛选条件需经 Schema 白名单校验，防止任意 SQL 注入"]},
    {"tool": "snapshot_create", "family": "取数", "name": "快照创建", "risk_level": "medium", "write": True,
     "condition": "取数结果需固化为审计证据时；由现场 Agent 在证据收集环节触发",
     "inputs": [{"name": "query_result", "type": "object", "required": True, "desc": "待快照数据"},
                {"name": "biz_date", "type": "string", "required": True, "desc": "业务日期"},
                {"name": "purpose", "type": "string", "required": True, "desc": "用途说明"}],
     "outputs": [{"name": "snapshot_id", "desc": "快照 ID"}, {"name": "snapshot_time", "desc": "快照时间"}, {"name": "source_refs", "desc": "来源"}],
     "rules": ["快照不可变、不可删", "生成后即纳入证据链"]},
    {"tool": "quality_check", "family": "取数", "name": "质量检查", "risk_level": "low", "write": False,
     "condition": "取数完成后自动触发，或人工要求核验数据质量",
     "inputs": [{"name": "snapshot_id", "type": "string", "required": True, "desc": "快照 ID"},
                {"name": "checks", "type": "array", "required": False, "desc": "检查项（完整性/一致性/空值/重复）"}],
     "outputs": [{"name": "report", "desc": "检查报告（缺失、异常计数）"}, {"name": "result_status", "desc": "状态"}],
     "rules": ["质量不达标返回 partial 并列出问题项，不进入分析"]},
    {"tool": "masked_export", "family": "取数", "name": "脱敏导出", "risk_level": "medium", "write": True,
     "condition": "需对外/跨组织导出数据时",
     "inputs": [{"name": "snapshot_id", "type": "string", "required": True, "desc": "快照 ID"},
                {"name": "mask_rules", "type": "array", "required": True, "desc": "脱敏规则（手机号/身份证/金额等）"}],
     "outputs": [{"name": "file_url", "desc": "脱敏后文件"}, {"name": "mask_log", "desc": "脱敏记录"}],
     "rules": ["未按规则脱敏不可导出", "脱敏动作留痕"]},
    # 检索
    {"tool": "policy_search", "family": "检索", "name": "政策法规检索", "risk_level": "low", "write": False,
     "condition": "任意阶段；需引用法规/制度作为依据时",
     "inputs": [{"name": "query", "type": "string", "required": True, "desc": "检索意图"},
                {"name": "scope", "type": "string", "required": False, "desc": "范围（制度/法规/准则）"},
                {"name": "effective", "type": "bool", "required": False, "desc": "仅现行有效"}],
     "outputs": [{"name": "items", "desc": "条款、生效期、来源"}, {"name": "source_refs", "desc": "来源引用"}],
     "rules": ["结果必须带来源引用与生效期", "过期法规标记 expired"]},
    {"tool": "case_search", "family": "检索", "name": "案例检索", "risk_level": "low", "write": False,
     "condition": "需参考历史同类项目/问题处置时",
     "inputs": [{"name": "query", "type": "string", "required": True, "desc": "检索意图"},
                {"name": "stage", "type": "string", "required": False, "desc": "阶段"},
                {"name": "industry", "type": "string", "required": False, "desc": "行业"}],
     "outputs": [{"name": "cases", "desc": "案例摘要、处置、结论"}, {"name": "source_refs", "desc": "来源引用"}],
     "rules": ["脱敏后返回", "案例结论仅作参考，不替代本次研判"]},
    {"tool": "template_search", "family": "检索", "name": "模板检索", "risk_level": "low", "write": False,
     "condition": "生成文书/底稿/方案前，获取标准模板",
     "inputs": [{"name": "template_type", "type": "string", "required": True, "desc": "模板类型"},
                {"name": "version", "type": "string", "required": False, "desc": "指定版本"}],
     "outputs": [{"name": "template", "desc": "模板内容与字段"}, {"name": "source_refs", "desc": "来源引用"}],
     "rules": ["返回最新生效版本", "无匹配模板返回 blocked 并提示人工建模板"]},
    {"tool": "clause_locate", "family": "检索", "name": "条款定位", "risk_level": "low", "write": False,
     "condition": "已定位法规/合同，需精确到条款原文",
     "inputs": [{"name": "doc_id", "type": "string", "required": True, "desc": "文档"},
                {"name": "keyword", "type": "string", "required": False, "desc": "关键词"}],
     "outputs": [{"name": "clauses", "desc": "条款原文与章节"}, {"name": "source_refs", "desc": "来源引用"}],
     "rules": ["条款必须指向原文，禁止二次概括失实"]},
    # 分析
    {"tool": "rule_screen", "family": "分析", "name": "规则筛查", "risk_level": "medium", "write": False,
     "condition": "现场实施；对快照数据按既定规则筛查疑点",
     "inputs": [{"name": "snapshot_id", "type": "string", "required": True, "desc": "快照 ID"},
                {"name": "rule_set", "type": "string", "required": True, "desc": "规则集（大额支付/重复报销等）"},
                {"name": "thresholds", "type": "object", "required": False, "desc": "阈值"}],
     "outputs": [{"name": "hits", "desc": "命中疑点（规则、数据行、置信度）"}, {"name": "source_refs", "desc": "来源引用"}],
     "rules": ["输出「疑似」结论，不得定性", "规则集须业务专家签字后启用"]},
    {"tool": "metric_calc", "family": "分析", "name": "指标计算", "risk_level": "low", "write": False,
     "condition": "需计算比率/趋势/占比等确定性指标",
     "inputs": [{"name": "snapshot_id", "type": "string", "required": True, "desc": "快照 ID"},
                {"name": "metrics", "type": "array", "required": True, "desc": "指标定义"},
                {"name": "dimensions", "type": "array", "required": False, "desc": "维度"}],
     "outputs": [{"name": "result", "desc": "指标值"}, {"name": "formula", "desc": "计算口径（可追溯）"}],
     "rules": ["计算口径必须可回放、可追溯", "金额计算不得使用浮点误差（用定点/分）"]},
    {"tool": "trend_chart", "family": "分析", "name": "趋势图表", "risk_level": "low", "write": False,
     "condition": "需可视化趋势/分布时",
     "inputs": [{"name": "snapshot_id", "type": "string", "required": True, "desc": "快照 ID"},
                {"name": "chart_type", "type": "string", "required": True, "desc": "折线/柱状/饼图"},
                {"name": "x", "type": "object", "required": True, "desc": "X 轴定义"},
                {"name": "y", "type": "object", "required": True, "desc": "Y 轴定义"}],
     "outputs": [{"name": "chart", "desc": "图表数据/渲染结果"}, {"name": "source_refs", "desc": "来源引用"}],
     "rules": ["图表仅呈现数据，不附加定性结论"]},
    {"tool": "anomaly_detect", "family": "分析", "name": "异常识别", "risk_level": "medium", "write": False,
     "condition": "规则筛查之外的统计异常检测（离群、波动）",
     "inputs": [{"name": "snapshot_id", "type": "string", "required": True, "desc": "快照 ID"},
                {"name": "method", "type": "string", "required": False, "desc": "算法"},
                {"name": "sensitivity", "type": "string", "required": False, "desc": "敏感度"}],
     "outputs": [{"name": "anomalies", "desc": "异常点（含贡献度）"}, {"name": "source_refs", "desc": "来源引用"}],
     "rules": ["统计异常仅提示「需关注」，必须人工核验", "高风险转人工闸门"]},
    # 生成
    {"tool": "draft_generate", "family": "生成", "name": "底稿草拟", "risk_level": "medium", "write": True,
     "condition": "现场实施；证据已确认后生成底稿初稿",
     "inputs": [{"name": "evidence_ids", "type": "array", "required": True, "desc": "已确认证据"},
                {"name": "template_id", "type": "string", "required": True, "desc": "模板"},
                {"name": "issue", "type": "object", "required": False, "desc": "关联疑点"}],
     "outputs": [{"name": "draft", "desc": "底稿初稿（带引用锚点）"}, {"name": "source_refs", "desc": "来源引用"}],
     "rules": ["仅引用已确认内容，不得新增事实", "缺失证据处标黄提示"]},
    {"tool": "report_generate", "family": "生成", "name": "报告初稿", "risk_level": "medium", "write": True,
     "condition": "报告编制；底稿与问题已确认",
     "inputs": [{"name": "draft_ids", "type": "array", "required": True, "desc": "底稿"},
                {"name": "issue_ids", "type": "array", "required": True, "desc": "问题"},
                {"name": "template_id", "type": "string", "required": True, "desc": "模板"}],
     "outputs": [{"name": "report", "desc": "报告初稿"}, {"name": "diff", "desc": "与上版本差异"}],
     "rules": ["仅引用已确认内容", "版本对比留痕", "不得新增事实"]},
    {"tool": "scheme_generate", "family": "生成", "name": "方案初稿", "risk_level": "medium", "write": True,
     "condition": "审前编制审计方案；整改阶段制定整改方案",
     "inputs": [{"name": "context", "type": "object", "required": True, "desc": "立项/问题上下文"},
                {"name": "template_id", "type": "string", "required": True, "desc": "模板"}],
     "outputs": [{"name": "scheme", "desc": "方案初稿"}, {"name": "source_refs", "desc": "来源引用"}],
     "rules": ["范围/期限不得自行确定，须人工确认"]},
    {"tool": "ledger_generate", "family": "生成", "name": "台账生成", "risk_level": "medium", "write": True,
     "condition": "整改阶段生成整改台账；归档阶段生成归档清单",
     "inputs": [{"name": "items", "type": "array", "required": True, "desc": "台账条目"},
                {"name": "template_id", "type": "string", "required": True, "desc": "模板"}],
     "outputs": [{"name": "ledger", "desc": "台账"}, {"name": "source_refs", "desc": "来源引用"}],
     "rules": ["台账反映已确认数据", "不得自行销号/改期限"]},
    {"tool": "doc_generate", "family": "生成", "name": "文书生成", "risk_level": "medium", "write": True,
     "condition": "需生成通知书、征求意见稿等对外文书",
     "inputs": [{"name": "doc_type", "type": "string", "required": True, "desc": "文书类型"},
                {"name": "context", "type": "object", "required": True, "desc": "上下文"},
                {"name": "template_id", "type": "string", "required": True, "desc": "模板"}],
     "outputs": [{"name": "doc", "desc": "文书初稿"}, {"name": "source_refs", "desc": "来源引用"}],
     "rules": ["对外文书须人工签发", "发布动作走流程工具"]},
    # 流程
    {"tool": "approval_advance", "family": "流程", "name": "审批推进", "risk_level": "high", "write": True,
     "condition": "需要人工签批的节点（立项/签发/销号/归档）",
     "inputs": [{"name": "node", "type": "string", "required": True, "desc": "审批节点"},
                {"name": "approver", "type": "string", "required": True, "desc": "审批人"},
                {"name": "payload", "type": "object", "required": True, "desc": "待审批内容"}],
     "outputs": [{"name": "approval_id", "desc": "审批 ID"}, {"name": "status", "desc": "审批状态"}],
     "rules": ["审批人缺失→转上级", "超时→升级提醒", "不得代审"]},
    {"tool": "notice_dispatch", "family": "流程", "name": "通知下发", "risk_level": "high", "write": True,
     "condition": "需对外/对下发送正式通知",
     "inputs": [{"name": "receiver", "type": "array", "required": True, "desc": "接收方"},
                {"name": "content", "type": "object", "required": True, "desc": "内容"},
                {"name": "channel", "type": "string", "required": False, "desc": "渠道"}],
     "outputs": [{"name": "dispatch_id", "desc": "下发 ID"}, {"name": "status", "desc": "状态"}],
     "rules": ["对外下发必须人工审批后执行", "高风险留痕"]},
    {"tool": "task_create", "family": "流程", "name": "任务创建", "risk_level": "medium", "write": True,
     "condition": "需分派人工任务（督办、复核、补证）",
     "inputs": [{"name": "assignee", "type": "string", "required": True, "desc": "责任人"},
                {"name": "due", "type": "string", "required": False, "desc": "截止"},
                {"name": "task_type", "type": "string", "required": True, "desc": "任务类型"}],
     "outputs": [{"name": "task_id", "desc": "任务 ID"}],
     "rules": ["任务分派需负责人确认", "期限冲突提示"]},
    {"tool": "status_update", "family": "流程", "name": "状态更新", "risk_level": "medium", "write": True,
     "condition": "项目/事项状态流转（如「进行中→已结束」）",
     "inputs": [{"name": "entity", "type": "string", "required": True, "desc": "对象"},
                {"name": "from", "type": "string", "required": True, "desc": "原状态"},
                {"name": "to", "type": "string", "required": True, "desc": "目标状态"},
                {"name": "reason", "type": "string", "required": True, "desc": "理由"}],
     "outputs": [{"name": "update_id", "desc": "更新 ID"}],
     "rules": ["状态流转须符合状态机（未开始/进行中/已结束）", "非法流转阻断", "高风险状态变更需人工闸门"]},
]

_SPEC_BY_TOOL = {s["tool"]: s for s in TOOL_SPECS}

_HANDLERS = {
    "entity_extract": _entity_extract, "json_validate": _json_validate, "risk_level_assemble": _risk_level_assemble,
    "template_load": _template_load, "form_schema_assemble": _form_schema_assemble,
    "vector_rag_search": _vector_rag_search, "log_format": _log_format,
    "permission_check": _permission_check, "resource_load_check": _resource_load_check,
    "flow_query": _flow_query, "ledger_query": _ledger_query, "snapshot_create": _snapshot_create,
    "quality_check": _quality_check, "masked_export": _masked_export,
    "policy_search": _policy_search, "case_search": _case_search, "template_search": _template_search,
    "clause_locate": _clause_locate,
    "rule_screen": _rule_screen, "metric_calc": _metric_calc, "trend_chart": _trend_chart,
    "anomaly_detect": _anomaly_detect,
    "draft_generate": _draft_generate, "report_generate": _report_generate, "scheme_generate": _scheme_generate,
    "ledger_generate": _ledger_generate, "doc_generate": _doc_generate,
    "approval_advance": _approval_advance, "notice_dispatch": _notice_dispatch, "task_create": _task_create,
    "status_update": _status_update,
}


def tool_families():
    """五类工具 + 风险等级，供 /api/tools 顶层渲染"""
    return [
        {"family": "解析", "risk_level": "low", "desc": "立项意图识别、实体抽取与固定 JSON Schema 校验"},
        {"family": "取数", "risk_level": "low–medium", "desc": "数据源读取、快照固化、质量核验、脱敏导出"},
        {"family": "检索", "risk_level": "low", "desc": "政策法规/案例/模板检索、条款定位"},
        {"family": "分析", "risk_level": "low–medium", "desc": "规则筛查、指标计算、趋势图表、异常识别"},
        {"family": "生成", "risk_level": "medium", "desc": "底稿/报告/方案/台账/对外文书初稿"},
        {"family": "流程", "risk_level": "medium–high", "desc": "审批推进、通知下发、任务创建、状态更新"},
    ]


def dispatch(orch, args):
    """统一契约分发：定位工具 → 校验 → 执行 → 套信封；写入类按 request_id 幂等。"""
    tool = args.get("tool") or args.get("function") or args.get("name")
    spec = _SPEC_BY_TOOL.get(tool)
    if not spec:
        return envelope("error", data={"reason": f"未知工具：{tool}", "allowed": sorted(_SPEC_BY_TOOL)},
                        risk_level="low")
    handler = _HANDLERS[tool]
    try:
        if spec["write"]:
            request_id = args.get("request_id")
            result = call_idempotent(request_id, handler, orch, args)
        else:
            result = handler(orch, args)
    except Exception as e:  # 工具内部异常 → error，不抛出
        return envelope("error", data={"reason": str(e)}, risk_level=spec["risk_level"])
    if not isinstance(result, dict) or "result_status" not in result:
        result = envelope(data=result, risk_level=spec["risk_level"])
    result.setdefault("trace_id", make_trace_id())
    result["_tool"] = tool
    return result
