"""阶段 Agent 层：立项 / 审前准备 / 现场实施 / 报告编制 / 整改督办 / 项目归档 六个审计工作阶段。

每个 Agent 职责单一，输入输出通过共享的项目上下文（dict）在阶段间传递，
由调度中心（orchestrator.py）统一编排，形成「立项→查证→定性→报告→整改→归档」的闭环。

多角色会签：各 Agent 产出文档后，通过 notify() 向负责角色发送系统通知；
会签闸门（谁确认哪一项）由 orchestrator 的角色标记前置条件驱动。
"""
import json
import os
import re
import time
import uuid
from datetime import datetime

from retriever import BM25Retriever, load_documents
from data_tool import match_source, load_rows, detect_anomalies, format_anomalies


def normalize_initiation_project_name(value):
    """从用户指令中提取项目名称，避免把“创建/立项”等动作词写入项目实体。"""
    text = str(value or '').strip()
    named = re.search(r'项目名称\s*(?:是|为)\s*[“"「]?([^”"」]+)', text)
    if named:
        text = named.group(1).strip()
    text = re.sub(r'^(?:帮我|请|我要|希望|需要)?\s*(?:创建|新建|发起|完成|进行|开展|做|建立)\s*(?:一个新的?|一项)?\s*', '', text)
    text = re.sub(r'^(?:审计项目|项目)\s*[：:，,]?\s*', '', text)
    text = re.sub(r'\s*(?:项目)?立项\s*$', '', text)
    return text.strip(' ：:，,。') or str(value or '').strip()
from generator import (
    parse_task,
    parse_project_form,
    annotate_raw_data,
    validate_initiation_risks,
    judge_risks,
    parse_risks,
    risks_to_markdown,
    generate_workpaper,
    generate_report,
    generate_rectification_plan,
    generate_initiation,
    generate_audit_plan,
    fill_project_form,
    generate_initiation_trace,
    generate_preaudit_risks,
    generate_audit_notice,
    generate_seek_opinion,
    generate_archive,
    normalize_audit_type,
)
from problem_data import load_problems, summarize as problems_summarize
from roles import notify
from docs import (
    info_form_missing,
    render_demand_form,
    render_preaudit_materials,
    render_pre_survey_form,
    render_commitment,
    render_meeting_agenda,
    render_field_materials,
    render_audit_items,
    render_problem_list,
    render_rectification_review,
    render_archive_three,
)

# 启动时加载知识库、构建检索器（全局一次）
CHUNKS = load_documents()
RETRIEVER = BM25Retriever(CHUNKS)


def _format_retrieved(retrieved):
    return "\n\n".join(f"[来源：{r['source']}]\n{r['text']}" for r in retrieved)


def _parse_json_safe(text):
    """容错解析任务解析器返回的 JSON（容忍 markdown 代码块等）"""
    try:
        return json.loads(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                pass
        return None


_TASK_PARSE_FIELDS = ("审计对象", "审计范围", "审计类型", "审计目标", "审计期间")


def _validate_task_parse(parsed_text):
    """对立项意图 Agent 的 JSON 做代码侧 schema 归一化，禁止自由字段和非法类型。"""
    data = _parse_json_safe(parsed_text) or {}
    if not isinstance(data, dict):
        data = {}
    normalized = {field: str(data.get(field) or "【待补充】").strip() or "【待补充】" for field in _TASK_PARSE_FIELDS}
    normalized["审计类型"] = (
        "【待补充】" if normalized["审计类型"] in ("未明确", "待补全", "【待补充】")
        else normalize_audit_type(normalized["审计类型"])
    )
    return json.dumps(normalized, ensure_ascii=False)


def _retrieve_initiation_intent(task):
    """立项意图专属 RAG：类型规则 + 历史口语样本，均限定 initiation 阶段。"""
    query = f"{task} 审计项目类型 立项指令 口语实体抽取"
    return RETRIEVER.retrieve(query, top_k=8, stage="initiation", threshold=0.10)


def _collect_initiation_raw_data(parsed, ctx):
    """由代码/工具采集原始数据；Agent 不直接访问数据库。当前未接入的源显式标记。"""
    data = []
    actor = ctx.get("_actor") or {}
    data.append({"source": "组织主数据接口", "source_record_id": str(actor.get("department") or "ORG-NOT-CONNECTED"),
                 "retrieved_at": datetime.now().isoformat(timespec="seconds"),
                 "raw": {"actor_id": actor.get("actorId", ""), "department": actor.get("department", ""), "name": actor.get("name", "")}})
    # 台账/整改/人员等源统一走代码适配层；没有接入时不伪造记录，只返回源状态。
    for source in ("被审计单位档案库", "历史审计台账库", "历史整改记录库", "审计人员资源库", "业务风险库"):
        data.append({"source": source, "source_record_id": f"{source}-NOT-CONNECTED",
                     "retrieved_at": datetime.now().isoformat(timespec="seconds"),
                     "raw": {}, "source_status": "not_connected"})
    return data


class DataCollectionAgent:
    """只解读代码/工具返回的原始数据，输出带来源的数据标记集。"""

    name = "多源数据采集"

    def run(self, parsed, raw_data, ctx):
        from tool_catalog import dispatch as tool_dispatch
        result = tool_dispatch(None, {"tool": "vector_rag_search", "query": "组织主数据 单位档案 历史审计台账 整改记录 人员资源 业务风险 字段语义", "threshold": 0.05})
        refs = ((result.get("data") or {}).get("items") if isinstance(result, dict) else []) or []
        ctx["data_collection_rag"] = refs
        raw = annotate_raw_data(parsed, raw_data, _format_retrieved(refs))
        labeled = _parse_json_safe(raw)
        if not isinstance(labeled, dict) or not isinstance(labeled.get("dataset"), list):
            labeled = {"dataset": [{**item, "status": "缺失" if not item.get("raw") else "正常", "missing_fields": [], "suspicious_fields": [], "annotation": "模型标记失败，保留原始数据待人工复核"} for item in raw_data]}
        ctx["raw_business_data"] = raw_data
        ctx["labeled_business_data"] = labeled
        return ctx


def _initiation_hard_facts(parsed, raw_data, ctx):
    """代码侧硬事实，仅做结构/数据状态校验，不判断业务真伪。"""
    d = _parse_json_safe(parsed) or {}
    facts = []
    from tool_catalog import dispatch as tool_dispatch
    actor = ctx.get("_actor") or {}
    permission = tool_dispatch(None, {"tool": "permission_check", "actor_role": actor.get("actorRole", "")})
    if not ((permission.get("data") or {}).get("allowed", False)):
        facts.append({"code": "PERMISSION-DENIED", "description": "当前登录角色无权创建立项项目", "blocking": True, "evidence_refs": []})
    resource = tool_dispatch(None, {"tool": "resource_load_check"})
    if resource.get("result_status") == "partial":
        facts.append({"code": "RESOURCE-CHECK-UNAVAILABLE", "description": "审计人员资源接口未接入，人员负荷待人工确认", "blocking": False, "evidence_refs": []})
    # 重复、权限、资源等事实必须由代码/工具产生，模型只负责可读加工。
    try:
        import db
        form = ctx.get("info_form") or {}
        obj = form.get("被审计单位名称") or d.get("审计对象")
        duplicates = db.find_duplicate_projects(form.get("项目名称") or ctx.get("task"), form.get("项目年度"), obj)
        if duplicates:
            facts.append({"code": "DUPLICATE-PROJECT", "description": "项目名称+年度+审计对象已存在立项项目", "blocking": True,
                          "evidence_refs": [x.get("session_id") for x in duplicates]})
    except Exception:
        facts.append({"code": "DUPLICATE-CHECK-UNAVAILABLE", "description": "重复立项校验服务暂不可用，需人工复核", "blocking": True, "evidence_refs": []})
    for key in ("审计对象", "审计范围", "审计期间"):
        if str(d.get(key) or "").strip() in ("", "【待补充】", "未明确"):
            facts.append({"code": f"MISSING-{key}", "description": f"字段{key}缺失", "blocking": False, "evidence_refs": []})
    if not any(item.get("raw") for item in raw_data if isinstance(item, dict)):
        facts.append({"code": "SOURCE-EMPTY", "description": "业务数据源未返回可用原始记录", "blocking": False, "evidence_refs": [item.get("source_record_id") for item in raw_data]})
    return facts


def _is_blank_obj(v):
    """「审计对象 / 被审计单位」的缺失判定：空、None、「未明确」「待补全」均视为缺失"""
    s = str(v or "").strip()
    return (not s) or s in ("未明确", "待补全", "【待补充】", "None")


def _ensure_audit_object(ctx):
    """若 parsed.审计对象缺失，用 info_form.被审计单位名称 回填，保证下游底稿/报告有明确对象。

    立项阶段用户补全表单后，被审计单位名称即审计对象；此处回填使现场实施/报告不再显示「未明确」。
    """
    parsed = ctx.get("parsed")
    d = _parse_json_safe(parsed)
    if not d or not _is_blank_obj(d.get("审计对象")):
        return
    unit = str((ctx.get("info_form") or {}).get("被审计单位名称", "") or "").strip()
    if _is_blank_obj(unit):
        return
    d["审计对象"] = unit
    ctx["parsed"] = json.dumps(d, ensure_ascii=False)


def _audit_object_missing(parsed_text, info_form=None):
    """审计对象未明确 → 判定为无关输入或信息严重不足，应拒答。

    审计对象优先取 parsed.审计对象；缺失时回退 info_form.被审计单位名称。
    「未明确 / 待补全」一律视为缺失。
    """
    d = _parse_json_safe(parsed_text)
    obj = str(d.get("审计对象", "")).strip() if d else ""
    if not _is_blank_obj(obj):
        return False
    if info_form:
        unit = str(info_form.get("被审计单位名称", "") or "").strip()
        if not _is_blank_obj(unit):
            return False
    return True


def _extract_scope(parsed_text):
    """从任务解析结果里提取「审计范围」字段，供取数工具匹配数据源"""
    d = _parse_json_safe(parsed_text)
    if not d:
        return ""
    return str(d.get("审计范围", "")).strip()


def _extract_audit_type(parsed_text):
    """从任务解析结果里提取「审计类型」字段，归一化到四大类（兜底「其他」）"""
    d = _parse_json_safe(parsed_text)
    if not d:
        return "其他"
    return normalize_audit_type(str(d.get("审计类型", "")).strip())


def _data_portrait():
    """数据画像：基于真实脱敏历史审计问题库的分布快照（审前准备阶段参考）"""
    rows = load_problems()
    s = problems_summarize(rows)
    funcs = "；".join(f"{k} {v} 条" for k, v in s["by_func"][:6])
    domains = "；".join(f"{k} {v} 条" for k, v in s["by_domain"][:10])
    return (
        f"历史审计问题共 {s['total']} 条，涉及金额 {s['涉及金额_万元']} 万元"
        f"（其中有金额问题 {s['有金额问题数']} 条）。\n"
        f"监督职能分布（TOP6）：{funcs}。\n"
        f"业务领域分布（TOP10）：{domains}。"
    )


class InitiationAgent:
    """立项：抽取项目信息表单 → 缺项补全 / 缺立项依据生成需求表 → 立项申请"""

    name = "立项"

    def run(self, task, ctx):
        step_started = time.perf_counter()
        step_last = step_started
        step_durations = []
        def mark_step():
            nonlocal step_last
            now = time.perf_counter()
            # 前端以一位小数展示步骤耗时；极快的代码步骤也必须有可追溯的非零耗时。
            step_durations.append(round(max(0.1, now - step_last), 3))
            step_last = now
        ctx["initiation_step_durations"] = []
        intent_refs = ctx.get("initiation_intent_retrieved")
        if intent_refs is None:
            intent_refs = _retrieve_initiation_intent(task)
            ctx["initiation_intent_retrieved"] = intent_refs
        if ctx.get("parsed"):
            parsed = _validate_task_parse(ctx["parsed"])
        else:
            # 解析 Agent 只依赖两个低风险工具：实体抽取 + JSON Schema 校验。
            # 工具不访问业务数据库、不做风险判断、不生成业务表单。
            from tool_catalog import dispatch as tool_dispatch
            entity_result = tool_dispatch(None, {
                "tool": "entity_extract",
                "text": task,
                "retrieval_context": _format_retrieved(intent_refs),
                "source_refs": [{"source": r.get("source", "")} for r in intent_refs],
            })
            raw_parsed = ((entity_result.get("data") or {}).get("json")
                          if isinstance(entity_result, dict) else None) or "{}"
            validation_result = tool_dispatch(None, {"tool": "json_validate", "payload": raw_parsed})
            parsed = ((validation_result.get("data") or {}).get("json")
                      if isinstance(validation_result, dict) else None) or _validate_task_parse(raw_parsed)
            ctx["initiation_intent_tools"] = ["entity_extract", "json_validate"]
        mark_step()  # 解析自然语言任务
        # 表单 Agent 只接收结构化实体、数据标记和风险清单；模板和 Schema 由代码加载。
        info_form = ctx.get("info_form") or {}
        from tool_catalog import dispatch as tool_dispatch
        template_result = tool_dispatch(None, {"tool": "template_load", "template_type": "审计立项标准表单"})
        form_refs = RETRIEVER.retrieve("审计立项标准表单模板 字段 Schema", top_k=5, stage="initiation", threshold=0.05)
        role_refs = RETRIEVER.retrieve("审计岗位人员匹配规则 项目组长 主审 迎审人员", top_k=5, stage="initiation", threshold=0.05)
        ctx["project_form_rag"] = form_refs + role_refs
        # 兼容历史模板字段名，统一对外立项表单字段为“项目组长”。
        if info_form.get("组长") and not info_form.get("项目组长"):
            info_form["项目组长"] = info_form.pop("组长")
        if info_form.get("小组长") and not info_form.get("项目组长"):
            info_form["项目组长"] = info_form.pop("小组长")

        # 系统字段由服务端生成/注入，不能由模型臆造或被用户输入覆盖。
        parsed_obj = _parse_json_safe(parsed) or {}
        actor = ctx.get("_actor") or {}
        info_form["项目编号"] = info_form.get("项目编号") or ("AUD-" + datetime.now().strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:8].upper())
        info_form["项目年度"] = info_form.get("项目年度") or str(datetime.now().year)
        raw_project_name = info_form.get("项目名称") if not _is_blank_obj(info_form.get("项目名称")) else (parsed_obj.get("项目名称") or task)
        info_form["项目名称"] = normalize_initiation_project_name(raw_project_name)
        info_form["项目类型"] = normalize_audit_type(parsed_obj.get("审计类型", ""))
        info_form["项目负责人"] = info_form.get("项目负责人") if not _is_blank_obj(info_form.get("项目负责人")) else str(actor.get("name") or actor.get("actorId") or "未明确")
        info_form["项目所属单位"] = info_form.get("项目所属单位") if not _is_blank_obj(info_form.get("项目所属单位")) else str(actor.get("department") or os.getenv("DEFAULT_ORG", "未明确"))

        # 数据采集、硬规则和风险 Agent 均在表单组装前完成；表单 Agent 不修改系统字段。
        raw_data = _collect_initiation_raw_data(parsed, ctx)
        DataCollectionAgent().run(parsed, raw_data, ctx)
        mark_step()  # 自动抓取多源业务数据
        hard_facts = _initiation_hard_facts(parsed, raw_data, ctx)
        ctx["initiation_hard_facts"] = hard_facts
        mark_step()  # 立项风险自动校验
        # 核心立项字段尚未补齐时，只提示字段缺失，不提前生成业务风险清单，
        # 避免把“待填写”误呈现为高风险或造成用户信息过载。
        parsed_obj = _parse_json_safe(parsed) or {}
        core_missing = any(_is_blank_obj(parsed_obj.get(k)) for k in ("审计对象", "审计范围", "审计期间"))
        if core_missing:
            risks = []
        else:
            risk_result = tool_dispatch(None, {"tool": "risk_level_assemble", "facts": hard_facts})
            risks = ((risk_result.get("data") or {}).get("risks") if isinstance(risk_result, dict) else []) or []
            risk_refs = RETRIEVER.retrieve("历史审计台账风险 被审计单位历史问题 行业业务风险 历史整改案例", top_k=8, stage="initiation", threshold=0.05)
            risk_raw = validate_initiation_risks(parsed, ctx.get("labeled_business_data", {}), _format_retrieved(risk_refs), hard_facts)
            risk_ai = _parse_json_safe(risk_raw) or {"risks": []}
            risks = risk_ai.get("risks") or risks
        ctx["initiation_risks"] = risks
        system_fields = {"项目编号": info_form["项目编号"], "项目年度": info_form["项目年度"], "项目名称": info_form["项目名称"],
                         "项目实施单位": info_form["项目所属单位"], "项目负责人": info_form["项目负责人"]}
        mark_step()  # 加载标准立项表单模板并准备字段
        form_raw = fill_project_form(parsed, ctx.get("labeled_business_data", {}).get("dataset", []), risks, system_fields)
        form_ai = _parse_json_safe(form_raw) or {}
        sources = {}
        for item in form_ai.get("fields", []):
            if not isinstance(item, dict) or not item.get("name"):
                continue
            name, value = str(item["name"]), item.get("value", "【待补充】")
            if isinstance(item.get("label"), str):
                sources[name] = {"label": item["label"], "source_refs": item.get("source_refs") or []}
            if name in system_fields:
                info_form[name] = system_fields[name]
            elif name not in ("项目编号", "项目年度") and _is_blank_obj(info_form.get(name)) and str(value).strip() not in ("", "【待补充】", "【系统赋值】"):
                # AI/data 预填只形成建议草稿；已有人工作值时绝不覆盖。
                info_form[name] = value
        # 代码兜底标记系统字段和未填人工字段，避免模型未返回某字段时丢失来源。
        for name in ("项目编号", "项目年度", "项目所属单位", "项目实施单位", "项目负责人"):
            sources.setdefault(name, {"label": "系统填充"})
        sources.setdefault("项目名称", {"label": "数据预填"})
        sources.setdefault("项目类型", {"label": "AI生成，需复核"})
        for name in ("项目组长", "主审", "业务负责人", "被审计单位名称", "迎审人员", "立项依据"):
            sources.setdefault(name, {"label": "待人工补充" if _is_blank_obj(info_form.get(name)) else "数据预填"})
        ctx["info_form_sources"] = sources
        mark_step()  # 表单模板加载与字段填充
        # 底层字段变更由代码埋点；Agent 仅在前端生成可读说明。
        ctx["initiation_change_log"] = [{"field": k, "old": None, "new": v, "source": "system"} for k, v in info_form.items()]
        tool_dispatch(None, {"tool": "form_schema_assemble", "fields": list(info_form.keys())})
        # 表单未完整前不生成风险溯源长文本；提交校验通过后再生成。
        if not core_missing:
            trace_result = tool_dispatch(None, {"tool": "vector_rag_search", "query": "被审计单位历史审计问题 行业业务风险 历史整改案例"})
            trace_refs = ((trace_result.get("data") or {}).get("items") if isinstance(trace_result, dict) else []) or []
            ctx["initiation_trace_rag"] = trace_refs
            trace_raw = generate_initiation_trace({"risk_facts": risks, "rag_results": trace_refs, "change_log": ctx["initiation_change_log"]})
            ctx["initiation_trace"] = _parse_json_safe(trace_raw) or {"risk_analysis": "", "historical_summary": "", "readable_change_log": "", "evidence_refs": []}
            mark_step()  # 风险溯源与变更日志
        else:
            ctx.pop("initiation_trace", None)
            ctx.pop("initiation_trace_rag", None)

        # 合规红线：审计对象/被审计单位均未明确，且无项目名称 → 拒答
        # （「创建XXX项目」类请求有项目名称、无具体被审计单位，放行进补全，不硬拦）
        if _audit_object_missing(parsed, info_form) and _is_blank_obj(info_form.get("项目名称")):
            ctx["blocked"] = True
            ctx["blocked_msg"] = (
                "未识别出明确的「审计对象 / 被审计单位」。\n"
                "立项需要明确被审计单位与项目，请提供具体审计任务，例如：\n"
                "  帮我创建2026年业务外包专项审计项目"
            )
            return ctx

        ctx["blocked"] = False
        ctx["task"] = task
        ctx["parsed"] = parsed
        ctx["info_form"] = info_form

        missing = info_form_missing(info_form)
        # 意图实体缺失时给出针对性追问，帮助用户用一句话补齐解析所需关键词。
        parsed_for_followup = _parse_json_safe(parsed) or {}
        followup_prompts = []
        type_value = str(parsed_for_followup.get("审计类型") or "").strip()
        # “其他审计”仅是代码兜底值；用户没有明确类型关键词时，仍需引导确认，避免把兜底值当成识别结论。
        explicit_type = re.search(r"经济责任审计|竣工决算审计|工程结算审计|其他审计", str(task or ""))
        if _is_blank_obj(type_value) or (type_value in ("其他审计", "其他") and not explicit_type):
            followup_prompts.append("未识别到审计类型，请补充说明（如：经济责任审计、竣工决算审计、工程结算审计、其他审计）。")
            if not explicit_type:
                info_form["项目类型"] = "待补充"
        if followup_prompts:
            ctx["intent_followup"] = followup_prompts
        else:
            ctx.pop("intent_followup", None)
        # 立项依据缺失 → 自动生成审计计划需求表，走业务负责人审核分支
        # 先完成核心人工字段，再生成立项依据缺失的计划需求表，减少首次对话干扰。
        core_form_missing = [k for k in ("项目组长", "主审", "业务负责人", "被审计单位名称", "迎审人员") if k in missing]
        demand_needed = "立项依据" in missing and not core_form_missing
        if demand_needed:
            ctx["demand_form"] = render_demand_form(info_form, task)
            notify(ctx, "business_lead", "请审核 2026 年审计计划需求表（立项依据缺失）。")
            # 立项依据仍是立项阶段出口必填项；需求表仅作为补充材料，不得让项目提前流转。

        mark_step()  # 输出待确认立项表单草稿
        # 表单和立项申请尚未完整时也保留已完成节点，供前端准确展示。
        ctx["initiation_step_durations"] = step_durations

        # 仍有缺项 → 进入补全，暂不生成立项申请
        if missing:
            ctx["fill_hint"] = "请补全项目信息表单：\n" + "、".join(missing) + "\n格式示例：迎审人员=张三，业务负责人=李四"
            return ctx

        ctx.pop("fill_hint", None)  # 表单已完整，清除补全提示，避免后续阶段重复展示
        _ensure_audit_object(ctx)
        parsed = ctx.get("parsed") or parsed
        initiation = generate_initiation(parsed)
        ctx["initiation"] = initiation
        mark_step()  # 输出立项申请初稿
        ctx["initiation_step_durations"] = step_durations
        ctx.setdefault("approved", set()).add("立项信息已填写完整")
        notify(ctx, "business_lead", "项目信息表单已填写完整，请审核立项依据/立项申请。")
        return ctx


class PreAuditAgent:
    """审前准备：实施方案 + 数据画像 + 审查通知书 + 审前调查表单 + 承诺书"""

    name = "审前准备"

    def run(self, ctx):
        # 继承立项正式数据，按审计对象和范围检索制度、历史底稿、风险线索及同类案例。
        parsed = ctx.get("parsed", "")
        info_form = ctx.get("info_form") or {}
        query = f"{parsed} {info_form.get('被审计单位名称', '')} 制度 历史底稿 历史风险 同类审计案例"
        source_refs = RETRIEVER.retrieve(query, top_k=10, stage="preaudit", threshold=0.05)
        ctx["preaudit_retrieved"] = source_refs
        plan_spec = RETRIEVER.retrieve("审计实施方案编制规范 标准模板", top_k=5, stage="preaudit", threshold=0.05)
        audit_plan = generate_audit_plan(
            parsed, ctx.get("initiation", ""), _format_retrieved(plan_spec + source_refs)
        )
        ctx["audit_plan"] = audit_plan
        ctx["data_portrait"] = _data_portrait()
        ctx["audit_notice"] = generate_audit_notice(
            ctx.get("parsed", ""), ctx.get("initiation", ""), audit_plan
        )
        ctx["preaudit_materials"] = render_preaudit_materials(ctx.get("info_form"), ctx.get("parsed"))
        ctx["pre_survey_form"] = render_pre_survey_form(ctx.get("info_form"), ctx.get("parsed"))
        ctx["commitment"] = render_commitment(ctx.get("info_form"))
        # 方案审核通过后先生成三级事项清单草稿，现场阶段再由风险线索补充。
        ctx["audit_items"] = render_audit_items(parsed, [])
        # 三级事项清单草稿由方案拆解结果承载；历史风险线索作为疑点候选，不直接定性。
        ctx["preaudit_workload"] = {
            "basis": "按审计事项数量和重点内容复杂度估算，最终以项目组确认值为准",
            "items": max(1, len(str(audit_plan).splitlines())),
            "hours": "待项目组确认",
            "staffing": "待组长确认",
            "schedule": "待组长确认",
        }
        risk_raw = generate_preaudit_risks({
            "initiation": ctx.get("initiation", ""),
            "parsed": parsed,
            "retrieved": source_refs,
            "audit_items": ctx["audit_items"],
        })
        ctx["preaudit_risk_prompts"] = _parse_json_safe(risk_raw) or {"risk_prompts": []}
        ctx["preaudit_change_log"] = [{"action": "生成", "artifact": key, "source": "代码编排+知识库模板"}
                                       for key in ("audit_plan", "audit_notice", "preaudit_materials", "pre_survey_form", "commitment", "audit_items")]

        notify(ctx, "respondent", "现在进入审前准备阶段，请审核审前调查表单并签署审计单位承诺书。")
        notify(ctx, "chief_auditor", "现在进入审前准备阶段，请审核审计实施方案。")
        notify(ctx, "group_lead", "审计实施方案审核通过后，请确认审计事项清单（审计事项→重点内容→疑点线索）。")
        return ctx


class FieldAgent:
    """现场实施：任务解析 → 法规检索 → 取数筛查 → 风险判断 → 底稿（可锁定）"""

    name = "现场实施"

    def run(self, task, ctx):
        parsed = ctx.get("parsed") or parse_task(task)
        # 立项阶段已补全被审计单位名称 → 回填为审计对象，避免底稿显示「未明确」
        _ensure_audit_object(ctx)
        parsed = ctx.get("parsed") or parsed

        if _audit_object_missing(parsed, ctx.get("info_form")):
            ctx["blocked"] = True
            ctx["blocked_msg"] = (
                "未识别出明确的「审计对象」。\n"
                "本系统是审计专用流程执行工具，请提供具体的审计任务（含审计对象 + 范围）。"
            )
            return ctx

        retrieved = RETRIEVER.retrieve(f"{task}\n{parsed}", top_k=5, stage="field")
        if not retrieved:
            ctx["blocked"] = True
            ctx["blocked_msg"] = (
                "未检索到相关法规。按合规红线，信息不足时不强行判断、不臆造法规。\n"
                "请补充更具体的审计对象或范围后重试。"
            )
            return ctx

        source_key, meta = match_source(_extract_scope(parsed))
        if meta:
            rows = load_rows(meta["file"])
            anomalies, summary = detect_anomalies(meta, rows)
            anomalies_text = format_anomalies(anomalies, summary)
            ctx["data_source"] = meta["file"]
            ctx["anomalies_text"] = anomalies_text
            ctx["anomalies"] = anomalies
            ctx["anomaly_summary"] = summary
        else:
            anomalies_text = "（该审计范围的数据源尚未接入，本次仅做法规检索，不做取数筛查）"
            ctx["data_source"] = None
            ctx["anomalies_text"] = anomalies_text
            ctx["anomalies"] = []
            ctx["anomaly_summary"] = {}

        risks_raw = judge_risks(parsed, anomalies_text, _format_retrieved(retrieved))
        risks_list = parse_risks(risks_raw)
        if risks_list is None:
            risks = risks_raw
            risks_list = []
        else:
            risks = risks_to_markdown(risks_list)
        workpaper = generate_workpaper(parsed, risks, anomalies_text, _format_retrieved(retrieved))

        # 现场实施新增文档：会议议程 / 实施资料清单与调查表
        ctx["meeting_agenda"] = render_meeting_agenda(ctx.get("info_form"), parsed)
        ctx["field_materials"] = render_field_materials(ctx.get("info_form"), ctx.get("audit_plan"))
        ctx["workpaper_locked"] = False  # 报告签发前底稿可修改

        ctx["blocked"] = False
        ctx["task"] = task
        ctx["parsed"] = parsed
        ctx["retrieved"] = retrieved
        ctx["risks"] = risks
        ctx["risks_list"] = risks_list
        # 风险线索形成后刷新三级审计事项清单，供底稿关联和现场质量考核。
        ctx["audit_items"] = render_audit_items(ctx.get("parsed", ""), risks_list)
        ctx["workpaper"] = workpaper

        notify(ctx, "group_lead", "现场实施已启动：请审核会议通知+议程、实施资料清单/调查表。")
        notify(ctx, "auditor", "审计底稿已生成，请确认审核。")
        return ctx


class ReportAgent:
    """报告编制：审计问题清单 + 报告初稿 + 征求意见稿（底稿签发后锁定）"""

    name = "报告编制"

    def run(self, ctx):
        report_spec = RETRIEVER.retrieve(
            ctx.get("task", "") + " 审计报告编制规范", top_k=3, stage="report"
        )
        # 报告必须吸收本轮对话/附件中的真实补充内容，再与标准模板合并。
        # 该内容仅作为待核验输入，不改变“只能引用已确认底稿和风险点”的约束。
        latest_input = str(ctx.get("latest_user_input") or "").strip()
        if latest_input:
            report_spec = list(report_spec or [])
            report_spec.append({
                "source": "本轮用户输入及附件",
                "text": latest_input,
                "score": 1.0,
            })
        audit_type = _extract_audit_type(ctx.get("parsed", ""))
        report = generate_report(
            ctx.get("parsed", ""),
            ctx.get("risks", ""),
            ctx.get("workpaper", ""),
            _format_retrieved(ctx.get("retrieved", [])),
            _format_retrieved(report_spec),
            audit_type=audit_type,
        )
        ctx["report"] = report
        ctx["audit_type"] = audit_type
        ctx["problem_list"] = render_problem_list(ctx.get("risks_list", []))
        ctx["seek_opinion"] = generate_seek_opinion(ctx.get("parsed", ""), report)
        # 这里只生成报告初稿/征求意见稿，正式签发尚未完成，底稿仍可修改；
        # 由「报告审签发布」人工会签通过时由服务层锁定。
        ctx["workpaper_locked"] = False

        notify(ctx, "chief_auditor", "审计问题清单与审计报告已生成，请审核确认。")
        notify(ctx, "respondent", "审计报告征求意见稿已下发，请反馈意见并上传佐证。")
        return ctx


class RectificationAgent:
    """整改督办：整改台账 + 督办建议 + 整改成果复核（三态）"""

    name = "整改督办"

    def run(self, ctx):
        rectification_spec = RETRIEVER.retrieve(
            ctx.get("task", "") + " 审计整改督办", top_k=5, stage="rectification"
        )
        plan = generate_rectification_plan(
            ctx.get("risks", ""),
            ctx.get("report", ""),
            _format_retrieved(rectification_spec),
        )
        ctx["rectification"] = plan
        ctx["rectification_review"] = render_rectification_review(ctx.get("risks_list", []))

        notify(ctx, "respondent", "审计问题清单已下发，请填报整改情况。")
        notify(ctx, "chief_auditor", "整改成果待复核（通过销号 / 不通过继续整改 / 未完成结转持续整改）。")
        return ctx


class ArchiveAgent:
    """项目归档：归档清单 + 归档三清单（问题/建议/追责）"""

    name = "项目归档"

    def run(self, ctx):
        archive = generate_archive(
            ctx.get("parsed", ""),
            ctx.get("initiation", ""),
            ctx.get("audit_plan", ""),
            ctx.get("workpaper", ""),
            ctx.get("report", ""),
            ctx.get("rectification", ""),
        )
        ctx["archive"] = archive
        ctx["archive_three"] = render_archive_three(
            ctx.get("parsed"), ctx.get("risks_list", []), ctx.get("report", ""), ctx.get("rectification", "")
        )

        notify(ctx, "rect_dept_lead", "请签收整改问题清单与整改建议。")
        notify(ctx, "biz_dept_lead", "请签收审计建议清单与违规追责建议。")
        return ctx
