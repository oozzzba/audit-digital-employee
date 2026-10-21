"""支线功能工具层：六阶段主线之外的独立审计功能点。

对应 FR 规格里的若干支线功能（审前调查 / 征求意见 / 逾期督办 / 资料归档），
由阶段 Agent 主线之外直接调用，复用「五类工具」（取数/检索/分析/生成/流程）。
支线功能仍属于六阶段业务的一部分，必须经过阶段就绪校验；缺上下文或阶段未到达时给出明确指引，
不允许从接口直接绕过流程闸门。
"""
import json
import datetime
from collections import Counter

from generator import (
    parse_task,
    generate_pre_survey,
    generate_seek_opinion,
    generate_overdue_supervise,
    generate_file_materials,
)
from ledger_data import load_ledger
from problem_data import load_problems, summarize as problems_summarize


# 支线功能的最早可执行阶段。阶段状态由 orchestrator 统一维护，不能由前端传入覆盖。
FUNCTION_MIN_STAGE = {
    "pre_survey": "initiation",       # 立项产出后才可开展审前调查
    "seek_opinion": "report",         # 报告初稿产出后才可征求意见
    "overdue_supervise": "report",       # 报告发布后才可进入整改督办
    "file_materials": "archive",      # 归档阶段才可生成归档目录
}


def guard_function_stage(ctx, function_key):
    """校验支线功能是否处于允许阶段，返回 None 表示通过。"""
    from orchestrator import STAGE_ORDER, STAGE_NAMES

    required = FUNCTION_MIN_STAGE.get(function_key)
    if not required:
        return None
    # completed() 按顺序返回最后一个连续产出阶段；不能用用户传入的 stage 字段代替。
    completed = ctx.get("_completed_stage")
    if not completed:
        # Web 层会在调用前注入由 Orchestrator 计算的值；缺失时保守阻断。
        return f"⚠️ 当前项目尚未完成必要的前置阶段，暂不能执行「{function_key}」。"
    if STAGE_ORDER.index(completed) < STAGE_ORDER.index(required):
        return (
            f"⚠️ 当前仅完成「{STAGE_NAMES.get(completed, completed)}」，"
            f"请先完成「{STAGE_NAMES[required]}」后再执行该功能。"
        )
    return None


def _obj_ok(parsed_text):
    """审计对象是否已明确（合规红线：审计对象未明确 → 不能开展支线业务）"""
    d = None
    try:
        d = json.loads(parsed_text)
    except Exception:
        start, end = parsed_text.find("{"), parsed_text.rfind("}")
        if start != -1 and end > start:
            try:
                d = json.loads(parsed_text[start:end + 1])
            except Exception:
                d = None
    if not isinstance(d, dict):
        return False
    obj = str(d.get("审计对象", "")).strip()
    return bool(obj) and obj != "未明确"


def _parsed_or_block(ctx, text):
    """取项目上下文里的任务解析；没有则尝试解析输入；仍无审计对象 → 返回拦截信息"""
    parsed = ctx.get("parsed")
    if parsed and _obj_ok(parsed):
        return parsed, None
    task = text or ctx.get("task", "")
    if task:
        parsed = parse_task(task)
        if _obj_ok(parsed):
            return parsed, None
    return None, (
        "⚠️ 缺少审计任务上下文（审计对象未明确）。\n"
        "   请先在聊天工作台发起审计任务（例如「审计 对某公司2025年度差旅费报销开展专项审计」），"
        "或输入含审计对象的指令后再试。"
    )


def _history_ref():
    """真实历史审计问题分布（供审前调查「数据画像」引用，取自审计问题库_脱敏.csv）"""
    ps = problems_summarize(load_problems())
    func = "；".join(f"{k} {v} 条" for k, v in ps.get("by_func", []))
    domain = "；".join(f"{k} {v} 条" for k, v in ps.get("by_domain", [])[:10])
    return (
        f"历史审计问题共 {ps['total']} 条，涉及金额 {ps['涉及金额_万元']} 万元。\n"
        f"监督职能分布：{func}\n业务领域分布（TOP10）：{domain}"
    )


def fn_pre_survey(ctx, text):
    """审前调查：资料清单 + 数据画像（引用真实历史问题分布）+ 风险初评"""
    parsed, err = _parsed_or_block(ctx, text)
    if err:
        return {"blocked": True, "blocked_msg": err}
    initiation = ctx.get("initiation", "") or "（尚未生成立项申请，以下基于审计任务解析开展）"
    report = generate_pre_survey(parsed, initiation, _history_ref())
    return {
        "title": "审前调查",
        "blocks": [{"type": "markdown", "label": "审前调查报告（资料清单 + 风险评估）", "content": report}],
        "trace": {"data_source": "审计问题库_脱敏.csv", "anomalies": [], "retrieved": []},
        "gate": "风险评估为建议，人工确认后生效。",
        "next_hint": "继续「编制审计方案」",
    }


def fn_seek_opinion(ctx, text):
    """征求意见：基于审计报告初稿，生成征求意见函 + 意见汇总表"""
    parsed, err = _parsed_or_block(ctx, text)
    if err:
        return {"blocked": True, "blocked_msg": err}
    report = ctx.get("report")
    if not report:
        return {
            "blocked": True,
            "blocked_msg": "⚠️ 尚未生成审计报告初稿。请先推进到「报告编制」阶段生成报告初稿，再征求意见。",
        }
    opinion = generate_seek_opinion(parsed, report)
    return {
        "title": "征求意见",
        "blocks": [{"type": "markdown", "label": "征求意见函 + 意见汇总表", "content": opinion}],
        "trace": {"data_source": None, "anomalies": [], "retrieved": []},
        "gate": "意见吸收须人工确认；下发须审批。",
        "next_hint": "继续「提交审理」",
    }


def _overdue_rows():
    """从真实整改台账筛出「未销号 且 整改时限已过」的逾期事项（容错解析脏数据）"""
    today = datetime.date.today()
    overdue = []
    for r in load_ledger():
        if r.get("是否销号") != "未销号":
            continue
        deadline = (r.get("整改时限") or "").strip()
        if not deadline:
            continue
        d = None
        try:
            d = datetime.date.fromisoformat(deadline)
        except ValueError:
            try:
                y, m, dd = deadline.split("-")
                d = datetime.date(int(y), int(m), int(dd))
            except Exception:
                d = None
        if d is not None and d < today:
            overdue.append(r)
    return overdue


def _overdue_summary(overdue):
    by_status = Counter(r.get("整改状态", "") for r in overdue)
    by_dept = Counter(r.get("整改责任部门", "") for r in overdue)
    return (
        f"逾期未销号事项共 {len(overdue)} 条（整改时限已过且未销号）。\n"
        f"按整改状态：{'；'.join(f'{k} {v} 条' for k, v in by_status.most_common())}\n"
        f"按整改责任部门（TOP10）：{'；'.join(f'{k} {v} 条' for k, v in by_dept.most_common(10))}"
    )


def fn_overdue_supervise(ctx, text):
    """逾期督办：真实台账 → 逾期筛选 → 督办通知 + 结构化督办任务清单"""
    overdue = _overdue_rows()
    summary = _overdue_summary(overdue)
    notice = generate_overdue_supervise(summary, ctx.get("task", ""))
    sample = overdue[:20]
    tasks = [
        {
            "年度": r.get("年度"),
            "项目名称": r.get("项目名称"),
            "问题名称": r.get("问题名称"),
            "问题定性": r.get("问题定性"),
            "整改责任部门": r.get("整改责任部门"),
            "整改状态": r.get("整改状态"),
            "整改时限": r.get("整改时限"),
            "是否销号": r.get("是否销号"),
        }
        for r in sample
    ]
    return {
        "title": "逾期督办",
        "blocks": [
            {"type": "markdown", "label": "督办通知", "content": notice},
            {
                "type": "json",
                "label": f"督办任务清单（真实脱敏数据 · 逾期 {len(overdue)} 条 · 展示前 {len(sample)} 条）",
                "content": json.dumps(tasks, ensure_ascii=False, indent=2),
            },
        ],
        "trace": {"data_source": "整改台账_脱敏.csv", "anomalies": [], "retrieved": []},
        "gate": "督办下发须人工确认；对外提醒须审批。",
        "next_hint": "继续「整改验收」",
    }


def fn_file_materials(ctx, text):
    """资料归档：基于全流程真实产出，生成归档目录 + 完整性检查"""
    parsed, err = _parsed_or_block(ctx, text)
    if err:
        return {"blocked": True, "blocked_msg": err}
    archive = generate_file_materials(
        parsed,
        ctx.get("initiation", "") or "（尚未生成立项申请）",
        ctx.get("audit_plan", "") or "（尚未生成审计方案）",
        ctx.get("workpaper", "") or "（尚未生成审计底稿）",
        ctx.get("report", "") or "（尚未生成审计报告）",
        ctx.get("rectification", "") or "（尚未生成整改台账）",
    )
    return {
        "title": "资料归档",
        "blocks": [{"type": "markdown", "label": "归档目录 + 完整性检查", "content": archive}],
        "trace": {"data_source": None, "anomalies": [], "retrieved": []},
        "gate": "密级须人工确定；不得擅自归档。",
        "next_hint": "继续「归档清单」",
    }


# 支线功能登记表：function_key → 处理器
FUNCTION_HANDLERS = {
    "pre_survey": fn_pre_survey,
    "seek_opinion": fn_seek_opinion,
    "overdue_supervise": fn_overdue_supervise,
    "file_materials": fn_file_materials,
}
