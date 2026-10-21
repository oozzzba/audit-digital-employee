"""审计数字员工 · 系统自检 + 55 测试用例

覆盖：
  能力一：意图识别（关键词路由 + LLM 精判兜底）
  能力二：阶段判定（前置条件状态机 / 乱序拦截 / 推进 / 回退 / 终止 / 闭环）
  能力三：路由到对应阶段 Agent（dispatch_silent → 正确 Agent 被调用）
  能力四：角色感知会签（角色确认只满足本角色会签项 / 多角色依次会签 / 补全表单）

另附独立自检：
  数据库（真实脱敏 CSV）/ 知识库（BM25）/ 提示词 / 六阶段 Agent / 工具契约与 22 子工具 /
  乱序拦截提示格式 / 持久化（SQLite 快照往返）/ LLM 容错（超时·重试·LLMError）。

用法：
    python3 test_suite.py            # 离线：LLM 已 mock，确定性、快、不烧 DeepSeek 额度
    python3 test_suite.py --live     # 额外跑 5 条真实 LLM 意图精判兜底用例（真实调 DeepSeek）
    python3 test_suite.py --pipeline # 额外跑六阶段 Agent 全流程集成冒烟（真实生成，较慢）
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 让 generator 读到 code/.env 里的 DEEPSEEK_API_KEY
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from orchestrator import Orchestrator, STAGE_ORDER, STAGE_OUTPUT_KEY, STAGE_PRECONDITIONS, STAGE_NAMES
from generator import detect_stage
from retriever import load_documents, BM25Retriever, knowledge_manifest
from problem_data import load_problems
from ledger_data import load_ledger
from data_tool import DATA_SOURCES
from tool_catalog import TOOL_SPECS, tool_families, dispatch as tool_dispatch
from tool_contract import validate, SCHEMA_VERSION

# 结果收集
RESULTS = []


def record(cat, name, ok, detail=""):
    RESULTS.append({"cat": cat, "name": name, "ok": ok, "detail": detail})
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {cat} · {name}" + (f"  -> {detail}" if (detail and not ok) else ""))


# ==================== 可记录调用的假 Agent（隔离 DeepSeek 生成，只验证路由） ====================

class RecordingAgent:
    def __init__(self, stage):
        self.stage = stage
        self.out_key = STAGE_OUTPUT_KEY[stage]
        self.calls = 0

    def run(self, *args):
        self.calls += 1
        ctx = None
        for a in args:
            if isinstance(a, dict):
                ctx = a
                break
        if ctx is not None:
            ctx[self.out_key] = f"<mock:{self.stage}>"
        return ctx


def make_orch(done=(), approved=(), role=None, form=None):
    """构造独立 Orchestrator：预置已完成阶段产出 + 已通过闸门 + 当前角色 + 项目信息表单。"""
    o = Orchestrator()
    o.agents = {s: RecordingAgent(s) for s in STAGE_ORDER}
    for s in done:
        o.ctx[STAGE_OUTPUT_KEY[s]] = f"<mock:{s}>"
    if done:
        o.ctx["task"] = "审计 对某公司2025年度差旅费报销开展专项审计"
    for c in approved:
        o.ctx["approved"].add(c)
    if role:
        o.current_role = role
    if form is not None:
        o.ctx["info_form"] = form
    return o


def _none_called(o):
    return all(o.agents[s].calls == 0 for s in STAGE_ORDER)


# ==================== 50 测试用例 ====================
# 每个用例：id / cat / name / input / done / approved / mode / 期望

CASES = [
    # ---------- 能力一：意图识别（关键词路由，24 例：19 关键词 + 5 LLM） ----------
    # 关键词 → 阶段（mode=intent_kw，走 o.detect() 的关键词短路，不调 LLM）
    {"id": 1,  "cat": "意图识别", "name": "关键词「立项」→ 立项", "input": "立项", "mode": "intent_kw", "expect": "initiation"},
    {"id": 2,  "cat": "意图识别", "name": "关键词「审前」→ 审前准备", "input": "审前", "mode": "intent_kw", "expect": "preaudit"},
    {"id": 3,  "cat": "意图识别", "name": "关键词「方案」→ 审前准备", "input": "方案", "mode": "intent_kw", "expect": "preaudit"},
    {"id": 4,  "cat": "意图识别", "name": "关键词「报告」→ 报告编制", "input": "报告", "mode": "intent_kw", "expect": "report"},
    {"id": 5,  "cat": "意图识别", "name": "关键词「汇总」→ 报告编制", "input": "汇总", "mode": "intent_kw", "expect": "report"},
    {"id": 6,  "cat": "意图识别", "name": "关键词「出具」→ 报告编制", "input": "出具", "mode": "intent_kw", "expect": "report"},
    {"id": 7,  "cat": "意图识别", "name": "关键词「成文」→ 报告编制", "input": "成文", "mode": "intent_kw", "expect": "report"},
    {"id": 8,  "cat": "意图识别", "name": "关键词「整改」→ 整改督办", "input": "整改", "mode": "intent_kw", "expect": "rectification"},
    {"id": 9,  "cat": "意图识别", "name": "关键词「督办」→ 整改督办", "input": "督办", "mode": "intent_kw", "expect": "rectification"},
    {"id": 10, "cat": "意图识别", "name": "关键词「台账」→ 整改督办", "input": "台账", "mode": "intent_kw", "expect": "rectification"},
    {"id": 11, "cat": "意图识别", "name": "关键词「销号」→ 整改督办", "input": "销号", "mode": "intent_kw", "expect": "rectification"},
    {"id": 12, "cat": "意图识别", "name": "关键词「跟踪」→ 整改督办", "input": "跟踪", "mode": "intent_kw", "expect": "rectification"},
    {"id": 13, "cat": "意图识别", "name": "关键词「归档」→ 项目归档", "input": "归档", "mode": "intent_kw", "expect": "archive"},
    {"id": 14, "cat": "意图识别", "name": "关键词「档案」→ 项目归档", "input": "档案", "mode": "intent_kw", "expect": "archive"},
    {"id": 15, "cat": "意图识别", "name": "关键词「底稿」→ 现场实施", "input": "底稿", "mode": "intent_kw", "expect": "field"},
    {"id": 16, "cat": "意图识别", "name": "关键词「风险」→ 现场实施", "input": "风险", "mode": "intent_kw", "expect": "field"},
    {"id": 17, "cat": "意图识别", "name": "关键词「现场」→ 现场实施", "input": "现场", "mode": "intent_kw", "expect": "field"},
    {"id": 18, "cat": "意图识别", "name": "关键词「检索」→ 现场实施", "input": "检索", "mode": "intent_kw", "expect": "field"},
    {"id": 19, "cat": "意图识别", "name": "关键词「法规」→ 现场实施", "input": "法规", "mode": "intent_kw", "expect": "field"},
    # LLM 精判兜底（mode=intent_llm，直接调 detect_stage 真实调 DeepSeek）
    {"id": 20, "cat": "意图识别", "name": "LLM「我想立项一个审计项目」→ 立项", "input": "我想立项一个审计项目", "mode": "intent_llm", "expect": "initiation"},
    {"id": 21, "cat": "意图识别", "name": "LLM「编制审计实施方案」→ 审前准备", "input": "编制审计实施方案", "mode": "intent_llm", "expect": "preaudit"},
    {"id": 22, "cat": "意图识别", "name": "LLM「现场查证采购事项」→ 现场实施", "input": "现场查证采购事项", "mode": "intent_llm", "expect": "field"},
    {"id": 23, "cat": "意图识别", "name": "LLM「出具审计报告」→ 报告编制", "input": "出具审计报告", "mode": "intent_llm", "expect": "report"},
    {"id": 24, "cat": "意图识别", "name": "LLM「整改销号」→ 整改督办", "input": "整改销号", "mode": "intent_llm", "expect": "rectification"},

    # ---------- 能力二：阶段判定（前置条件状态机，14 例） ----------
    {"id": 25, "cat": "阶段判定", "name": "乱序：未立项直接「生成审计报告」被拦截", "input": "生成审计报告", "mode": "dispatch", "expect_stage": None, "expect_err": "顺序校验未通过"},
    {"id": 26, "cat": "阶段判定", "name": "乱序：未立项直接「生成审计实施方案」被拦截", "input": "生成审计实施方案", "mode": "dispatch", "expect_stage": None, "expect_err": "顺序校验未通过"},
    {"id": 27, "cat": "阶段判定", "name": "初始态：下一个待办=立项，已完成=None", "mode": "state", "expect_next": "initiation", "expect_completed": None},
    {"id": 28, "cat": "阶段判定", "name": "立项后缺会签项，仍不能进审前", "input": "生成审计方案", "done": ("initiation",), "mode": "dispatch", "expect_stage": None, "expect_err": "顺序校验未通过"},
    {"id": 29, "cat": "阶段判定", "name": "立项信息完整+立项依据确认，可进审前", "input": "生成审计方案", "done": ("initiation",), "approved": ("立项信息已填写完整", "立项依据已确认"), "mode": "dispatch", "expect_stage": "preaudit"},
    {"id": 30, "cat": "阶段判定", "name": "跨阶段乱序：立项后直接「生成审计报告」被拦截", "input": "生成审计报告", "done": ("initiation",), "approved": ("立项信息已填写完整", "立项依据已确认"), "mode": "dispatch", "expect_stage": None, "expect_err": "顺序校验未通过"},
    {"id": 31, "cat": "阶段判定", "name": "审前完成后仍不能出报告（现场未做）", "input": "生成审计报告", "done": ("initiation", "preaudit"), "approved": ("承诺书已签署", "实施方案主审审核通过", "实施方案组长审核通过"), "mode": "dispatch", "expect_stage": None, "expect_err": "顺序校验未通过"},
    {"id": 32, "cat": "阶段判定", "name": "现场+底稿确认/签字+定性确认，可出报告", "input": "生成审计报告", "done": ("initiation", "preaudit", "field"), "approved": ("底稿审计人员确认", "底稿被审计单位签字", "问题定性主审确认"), "mode": "dispatch", "expect_stage": "report"},
    {"id": 33, "cat": "阶段判定", "name": "整改成果复核通过，可归档", "input": "项目归档", "done": ("initiation", "preaudit", "field", "report", "rectification"), "approved": ("整改成果复核通过",), "mode": "dispatch", "expect_stage": "archive"},
    {"id": 34, "cat": "阶段判定", "name": "六阶段完成+主审「归档审批」→ 项目正式闭环", "input": "归档审批", "done": ("initiation", "preaudit", "field", "report", "rectification", "archive"), "role": "chief_auditor", "mode": "dispatch", "expect_stage": None, "expect_err": "项目正式闭环归档"},
    {"id": 35, "cat": "阶段判定", "name": "已闭环后再「继续」→ 提示开启新项目", "input": "继续", "done": ("initiation", "preaudit", "field", "report", "rectification", "archive"), "approved": ("归档审批",), "mode": "dispatch", "expect_stage": None, "expect_err": "项目已闭环归档"},
    {"id": 36, "cat": "阶段判定", "name": "驳回：尚无产出时无法回退", "input": "驳回", "mode": "dispatch", "expect_stage": None, "expect_err": "尚无已产出草稿"},
    {"id": 37, "cat": "阶段判定", "name": "驳回：立项不设回退分支", "input": "驳回", "done": ("initiation",), "mode": "dispatch", "expect_stage": None, "expect_err": "不设回退分支"},
    {"id": 38, "cat": "阶段判定", "name": "驳回：现场实施回退并作废底稿", "input": "驳回", "done": ("initiation", "preaudit", "field"), "mode": "dispatch", "expect_stage": "field", "expect_err": "已回退至", "expect_absent": ["workpaper"]},

    # ---------- 能力三：路由到对应阶段 Agent（12 例） ----------
    {"id": 39, "cat": "路由", "name": "「审计 <任务>」→ 立项 Agent", "input": "审计 对某公司2025年度差旅费报销开展专项审计", "mode": "dispatch", "expect_stage": "initiation", "expect_agent": "initiation"},
    {"id": 40, "cat": "路由", "name": "「生成审计实施方案」→ 审前 Agent", "input": "生成审计实施方案", "done": ("initiation",), "approved": ("立项信息已填写完整", "立项依据已确认"), "mode": "dispatch", "expect_stage": "preaudit", "expect_agent": "preaudit"},
    {"id": 41, "cat": "路由", "name": "「生成审计工作底稿」→ 现场 Agent", "input": "生成审计工作底稿", "done": ("initiation", "preaudit"), "approved": ("承诺书已签署", "实施方案主审审核通过", "实施方案组长审核通过"), "mode": "dispatch", "expect_stage": "field", "expect_agent": "field"},
    {"id": 42, "cat": "路由", "name": "「生成审计报告」→ 报告 Agent", "input": "生成审计报告", "done": ("initiation", "preaudit", "field"), "approved": ("底稿审计人员确认", "底稿被审计单位签字", "问题定性主审确认"), "mode": "dispatch", "expect_stage": "report", "expect_agent": "report"},
    {"id": 43, "cat": "路由", "name": "「生成整改台账」→ 整改 Agent", "input": "生成整改台账", "done": ("initiation", "preaudit", "field", "report"), "approved": ("报告主审审核通过", "报告组长负责人审核通过", "审理组审核通过", "征求意见反馈已回收"), "mode": "dispatch", "expect_stage": "rectification", "expect_agent": "rectification"},
    {"id": 44, "cat": "路由", "name": "「项目归档」→ 归档 Agent", "input": "项目归档", "done": ("initiation", "preaudit", "field", "report", "rectification"), "approved": ("整改成果复核通过",), "mode": "dispatch", "expect_stage": "archive", "expect_agent": "archive"},
    {"id": 45, "cat": "路由", "name": "业务负责人「确认」→ 推进到审前 Agent", "input": "确认", "done": ("initiation",), "approved": ("立项信息已填写完整",), "role": "business_lead", "mode": "dispatch", "expect_stage": "preaudit", "expect_agent": "preaudit"},
    {"id": 46, "cat": "路由", "name": "「继续」→ 推进到现场 Agent", "input": "继续", "done": ("initiation", "preaudit"), "approved": ("承诺书已签署", "实施方案主审审核通过", "实施方案组长审核通过"), "mode": "dispatch", "expect_stage": "field", "expect_agent": "field"},
    {"id": 47, "cat": "路由", "name": "「驳回」→ 回退，不调用任何 Agent", "input": "驳回", "done": ("initiation", "preaudit", "field"), "mode": "dispatch", "expect_stage": "field", "expect_err": "已回退至", "expect_agent": "NONE"},
    {"id": 48, "cat": "路由", "name": "无关输入「今天天气怎么样」→ 追问，不路由", "input": "今天天气怎么样", "mode": "dispatch", "expect_stage": None, "expect_err": "无法识别", "expect_agent": "NONE"},
    {"id": 49, "cat": "路由", "name": "主审「归档审批」→ 闭环，不调用 Agent", "input": "归档审批", "done": ("initiation", "preaudit", "field", "report", "rectification", "archive"), "role": "chief_auditor", "mode": "dispatch", "expect_stage": None, "expect_err": "项目正式闭环归档", "expect_agent": "NONE"},
    {"id": 50, "cat": "路由", "name": "乱序「生成审计报告」→ 拦截，报告 Agent 不被调用", "input": "生成审计报告", "done": ("initiation",), "approved": ("立项信息已填写完整", "立项依据已确认"), "mode": "dispatch", "expect_stage": None, "expect_err": "顺序校验未通过", "expect_agent": "NONE"},

    # ---------- 能力四：角色感知会签（补全表单 + 多角色依次会签，5 例） ----------
    {"id": 51, "cat": "角色会签", "name": "项目负责人确认只满足「立项信息已填写完整」", "input": "确认", "done": ("initiation",), "role": "project_lead", "mode": "dispatch", "expect_stage": None, "expect_err": "尚待确认", "expect_approved": ["立项信息已填写完整"], "expect_not_approved": ["立项依据已确认"]},
    {"id": 52, "cat": "角色会签", "name": "非负责角色「确认」→ 提示暂无待确认项", "input": "确认", "done": ("initiation",), "approved": ("立项信息已填写完整",), "role": "auditor", "mode": "dispatch", "expect_stage": None, "expect_err": "你当前角色暂无待确认"},
    {"id": 53, "cat": "角色会签", "name": "多角色依次确认会签链 → 进入审前", "input": "确认", "done": ("initiation",), "approved": ("立项信息已填写完整",), "role": "business_lead", "mode": "dispatch", "expect_stage": "preaudit", "expect_agent": "preaudit"},
    {"id": 54, "cat": "角色会签", "name": "补全表单：识别「字段=值」并更新 info_form", "input": "迎审人员=张三，业务负责人=李四", "done": ("initiation",), "form": {"迎审人员": "待补全", "业务负责人": "待补全"}, "mode": "dispatch", "expect_stage": None, "expect_err": "已更新表单", "expect_form": {"迎审人员": "张三", "业务负责人": "李四"}},
    {"id": 55, "cat": "角色会签", "name": "补全全部字段后自动满足「立项信息已填写完整」", "input": "项目所属单位=总部，项目名称=业务外包专项，项目负责人=张三，组长=李四，主审=王五，小组长=赵六，立项依据=年度计划，被审计单位名称=某公司，迎审人员=钱七，业务负责人=孙八", "done": ("initiation",), "form": {"项目所属单位": "待补全", "项目名称": "待补全", "项目负责人": "待补全", "组长": "待补全", "主审": "待补全", "小组长": "待补全", "立项依据": "待补全", "被审计单位名称": "待补全", "迎审人员": "待补全", "业务负责人": "待补全"}, "mode": "dispatch", "expect_stage": None, "expect_err": "立项信息已填写完整", "expect_approved": ["立项信息已填写完整"]},
]


def run_case(c):
    if c["mode"] == "intent_kw":
        o = Orchestrator()
        got = o.detect(c["input"])
        return got == c["expect"], f"期望 {c['expect']}，实际 {got}"

    if c["mode"] == "intent_llm":
        try:
            got = detect_stage(c["input"]).strip().lower()
        except Exception as e:
            return False, f"LLM 调用失败：{e}"
        return got == c["expect"], f"期望 {c['expect']}，实际 {got}"

    if c["mode"] == "state":
        o = make_orch(c.get("done", ()), c.get("approved", ()))
        ok_next = o.next_pending() == c.get("expect_next", o.next_pending())
        ok_done = o.completed() == c.get("expect_completed", o.completed())
        return ok_next and ok_done, f"next_pending={o.next_pending()}, completed={o.completed()}"

    if c["mode"] == "dispatch":
        o = make_orch(c.get("done", ()), c.get("approved", ()), c.get("role"), c.get("form"))
        stage, ctx, err = o.dispatch_silent(c["input"])
        ok = True
        msgs = []
        if "expect_stage" in c:
            if stage != c["expect_stage"]:
                ok = False
                msgs.append(f"stage 期望 {c['expect_stage']} 实际 {stage}")
        if "expect_err" in c:
            if not err or c["expect_err"] not in err:
                ok = False
                msgs.append(f"err 期望包含「{c['expect_err']}」实际「{err}」")
        elif err:
            ok = False
            msgs.append(f"期望成功却返回 err「{err}」")
        if "expect_absent" in c:
            for k in c["expect_absent"]:
                if o.ctx.get(k) is not None:
                    ok = False
                    msgs.append(f"应作废 {k} 但仍存在")
        if "expect_approved" in c:
            for name in c["expect_approved"]:
                if name not in o.ctx.get("approved", set()):
                    ok = False
                    msgs.append(f"应已满足会签项「{name}」，实际 approved={sorted(o.ctx.get('approved', set()))}")
        if "expect_not_approved" in c:
            for name in c["expect_not_approved"]:
                if name in o.ctx.get("approved", set()):
                    ok = False
                    msgs.append(f"不应满足会签项「{name}」，实际已满足")
        if "expect_form" in c:
            form = o.ctx.get("info_form", {})
            for k, v in c["expect_form"].items():
                if form.get(k) != v:
                    ok = False
                    msgs.append(f"info_form[{k}] 期望 {v} 实际 {form.get(k)}")
        if "expect_agent" in c:
            if c["expect_agent"] == "NONE":
                if not _none_called(o):
                    ok = False
                    called = [s for s in STAGE_ORDER if o.agents[s].calls]
                    msgs.append(f"期望不调用任何 Agent，实际调用了 {called}")
            else:
                if o.agents[c["expect_agent"]].calls != 1:
                    ok = False
                    msgs.append(f"期望 {c['expect_agent']} 被调用 1 次，实际 {o.agents[c['expect_agent']].calls}")
                for s in STAGE_ORDER:
                    if s != c["expect_agent"] and o.agents[s].calls != 0:
                        ok = False
                        msgs.append(f"{s} 不应被调用，实际 {o.agents[s].calls}")
        return ok, "；".join(msgs)

    return False, "未知 mode"


# ==================== 五层自检 ====================

def check_db():
    print("\n【数据库 · 真实脱敏 CSV】")
    p = load_problems()
    record("数据库", f"审计问题库加载 {len(p)} 条", len(p) >= 6000, f"实际 {len(p)} 条")
    l = load_ledger()
    record("数据库", f"整改台账加载 {len(l)} 条", len(l) >= 2500, f"实际 {len(l)} 条")
    record("数据库", f"取数数据源注册 {len(DATA_SOURCES)} 个", len(DATA_SOURCES) >= 2,
           f"{[(k, m.get('file', '')) for k, m in DATA_SOURCES.items()]}")


def check_kb():
    print("\n【知识库 · 两级 + BM25】")
    chunks = load_documents()
    record("知识库", f"knowledge/ 加载切片 {len(chunks)} 条", len(chunks) >= 9, f"实际 {len(chunks)} 条")
    manifest = knowledge_manifest()
    docs = manifest.get("docs", [])
    record("知识库", f"知识条目 {len(docs)} 篇（公共/阶段专属）", len(docs) == 9, f"实际 {len(docs)} 篇")
    r = BM25Retriever(chunks)
    hits = r.retrieve("差旅费报销 超标准 票据缺失", stage="field")
    ok = len(hits) > 0 and all("source" in h and "confidence" in h for h in hits)
    record("知识库", f"BM25 检索命中 {len(hits)} 条（含来源/置信度）", ok, f"命中 {len(hits)} 条")


def check_prompts():
    print("\n【提示词 · 六阶段 + 能力边界】")
    import generator
    names = ["TASK_PARSE_SYSTEM", "RISK_SYSTEM", "WORKPAPER_SYSTEM", "RECTIFICATION_SYSTEM",
             "INITIATION_SYSTEM", "AUDIT_PLAN_SYSTEM", "AUDIT_NOTICE_SYSTEM", "ARCHIVE_SYSTEM",
             "PRE_SURVEY_SYSTEM", "SEEK_OPINION_SYSTEM", "OVERDUE_SUPERVISE_SYSTEM",
             "FILE_MATERIALS_SYSTEM", "STAGE_DETECT_SYSTEM"]
    for n in names:
        v = getattr(generator, n, "")
        record("提示词", f"{n}", isinstance(v, str) and len(v) > 200, f"长度 {len(v)}")
    # 能力边界红线抽查
    record("提示词", "RISK_SYSTEM 含「不得」红线", "不得" in generator.RISK_SYSTEM)


def check_agents():
    print("\n【Agent · 六阶段类 + 注册】")
    import agents
    names = ["InitiationAgent", "PreAuditAgent", "FieldAgent", "ReportAgent", "RectificationAgent", "ArchiveAgent"]
    for n in names:
        cls = getattr(agents, n, None)
        record("Agent", f"{n}", cls is not None and hasattr(cls, "run"), "缺失" if cls is None else "")
    o = Orchestrator()
    record("Agent", f"Orchestrator 注册 {len(o.agents)} 个 Agent", len(o.agents) == 6)


def check_tools():
    print("\n【工具 · 统一契约 + 22 子工具】")
    record("工具", f"子工具规格 {len(TOOL_SPECS)} 个", len(TOOL_SPECS) == 22, f"实际 {len(TOOL_SPECS)}")
    fams = tool_families()
    record("工具", f"五类工具 {len(fams)}", len(fams) == 5, f"实际 {len(fams)}")
    record("工具", f"契约版本 {SCHEMA_VERSION}", SCHEMA_VERSION == "1.0", SCHEMA_VERSION)
    ok, missing = validate({"operator_id": "u1", "project_id": "p1", "schema_version": "1.0",
                             "arguments": {"tool": "policy_search"}})
    record("工具", "契约校验：合法载荷通过", ok, f"缺失 {missing}")
    ok2, missing2 = validate({"arguments": {"tool": "x"}})
    record("工具", "契约校验：缺字段被拦截", not ok2, f"应拦截，实际 missing={missing2}")
    o = Orchestrator()
    r1 = tool_dispatch(o, {"tool": "policy_search", "query": "差旅费 报销"})
    ok = r1.get("result_status") == "success"
    record("工具", "policy_search 检索成功", ok, f"status={r1.get('result_status')}")
    # 写入类幂等：同一 request_id 重复调用返回同一结果
    args = {"tool": "status_update", "request_id": "req-idem-001", "entity": "项目A",
            "from": "进行中", "to": "已结束", "reason": "测试"}
    a = tool_dispatch(o, dict(args))
    b = tool_dispatch(o, dict(args))
    same = a.get("trace_id") == b.get("trace_id") or b.get("replayed") is True
    record("工具", "status_update 幂等（同 request_id 同结果）", same, f"trace_a={a.get('trace_id')} replayed={b.get('replayed')}")


# ==================== 乱序拦截提示格式（动作前置 + 可复制模板 + 会签指路） ====================

def check_out_of_order_format():
    print("\n【乱序拦截 · 提示格式】")
    o = make_orch()
    _, _, err = o.dispatch_silent("生成整改台账")
    ok = (err and "顺序校验未通过" in err and "帮我创建____专项审计项目" in err
          and "被审计单位" in err and "整改台账" in err)
    record("乱序格式", "未立项→整改台账：顺序拦截+立项模板+动作指路", ok, (err or "")[:40])

    o = make_orch()
    _, _, err = o.dispatch_silent("生成审计报告")
    ok = err and "顺序校验未通过" in err and "审计立项、审前准备、现场实施" in err
    record("乱序格式", "未立项→报告：原因含完整阶段链", ok, (err or "")[:50])

    o = make_orch(done=("initiation",))
    _, _, err = o.dispatch_silent("生成工作底稿")
    ok = err and "已完成【审计立项】" in err and "审前准备" in err
    record("乱序格式", "已立项→底稿：提示已完成节点+缺口阶段", ok, (err or "")[:50])

    o = make_orch()
    _, _, err = o.dispatch_silent("生成审计报告")
    ok = err and "实施方案主审审核通过" not in err and "底稿审计人员确认" not in err
    record("乱序格式", "不堆砌具名前置校验条件", ok)

    o = make_orch()
    _, _, err = o.dispatch_silent("生成整改台账")
    ok = err and "——————————" in err and "帮我创建____专项审计项目" in err \
        and "项目所属单位" in err and "被审计单位" in err
    record("乱序格式", "模板以分隔线包裹、三行可复制", ok)


# ==================== 持久化（SQLite 快照往返，临时库，不污染 audit.db） ====================

def check_persistence():
    print("\n【持久化 · SQLite 快照往返（临时库）】")
    import tempfile
    import db

    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(tmp)  # 让 init_db 重建
    old_path = db.DB_PATH
    db.DB_PATH = tmp
    try:
        db.init_db()

        ctx = {"task": "审计 X", "approved": {"立项信息已填写完整", "立项依据已确认"}, "notices": []}
        raw = db.serialize_ctx(ctx)
        back = db.deserialize_ctx(raw)
        ok = isinstance(back["approved"], set) and back["approved"] == ctx["approved"]
        record("持久化", "serialize/deserialize 往返（set↔list）", ok)

        db.save_project("s1", ctx=ctx, current_role="project_lead", current_stage="initiation",
                        project_status="running", project_name="2026年业务外包专项审计项目")
        row = db.load_project("s1")
        ok = bool(row) and row["project_name"] == "2026年业务外包专项审计项目" and row["current_role"] == "project_lead"
        record("持久化", "save_project/load_project 往返", ok, str(row)[:60] if row else "无")

        db.save_project("s1", ctx=ctx, current_role="business_lead", current_stage="preaudit",
                        project_status="running", project_name="X")
        n = db.count_projects()
        row2 = db.load_project("s1")
        ok = n == 1 and row2["current_role"] == "business_lead"
        record("持久化", "upsert 更新不重复（count=1）", ok, f"count={n}")

        db.append_chat("s1", "user", "你好")
        db.append_chat("s1", "ai", "你好，我是审计助手")
        msgs = db.load_chat("s1")
        ok = len(msgs) == 2 and msgs[0]["role"] == "user" and msgs[1]["role"] == "ai"
        record("持久化", "append_chat/load_chat 往返（seq 顺序）", ok, f"{len(msgs)} 条")

        db.save_docs("s1", [("initiation", "initiation", {"name": "demo"})])
        db.save_docs("s1", [("initiation", "initiation", {"name": "demo2"})])
        import sqlite3
        conn = sqlite3.connect(tmp)
        cnt = conn.execute("SELECT COUNT(*) FROM session_doc WHERE session_id='s1' AND doc_type='initiation'").fetchone()[0]
        conn.close()
        record("持久化", "save_docs 幂等（同 session+doc_type 单条）", cnt == 1, f"cnt={cnt}")

        db.save_pipeline("s1", 3, "审计 X")
        p = db.load_pipeline("s1")
        record("持久化", "save_pipeline/load_pipeline 往返", p == {"idx": 3, "task": "审计 X"}, str(p))
    finally:
        db.DB_PATH = old_path
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ==================== LLM 容错（超时·重试·LLMError，mock 客户端） ====================

def check_llm_error():
    print("\n【LLM 容错 · 超时/重试/异常】")
    import generator

    record("LLM容错", "LLMError 为 RuntimeError 子类", issubclass(generator.LLMError, RuntimeError))
    record("LLM容错", "超时/重试/退避默认值(60/3/1.5)",
           generator.LLM_TIMEOUT == 60 and generator.LLM_MAX_RETRIES == 3 and generator.LLM_BASE_DELAY == 1.5,
           f"{generator.LLM_TIMEOUT}/{generator.LLM_MAX_RETRIES}/{generator.LLM_BASE_DELAY}")

    class _FakeCompletions:
        def __init__(self):
            self.calls = 0
        def create(self, *a, **k):
            self.calls += 1
            raise RuntimeError("boom")

    class _FakeChat:
        def __init__(self):
            self.completions = _FakeCompletions()

    class _FakeClient:
        def __init__(self):
            self.chat = _FakeChat()

    old_client, old_delay = generator._client, generator.LLM_BASE_DELAY
    generator._client = _FakeClient()
    generator.LLM_BASE_DELAY = 0
    try:
        raised = False
        try:
            generator._chat("s", "u")
        except generator.LLMError:
            raised = True
        calls = generator._client.chat.completions.calls
        record("LLM容错", "底层失败重试耗尽后抛 LLMError（不裸抛）", raised,
               f"尝试 {calls} 次（期望 {generator.LLM_MAX_RETRIES + 1}）")
    finally:
        generator._client = old_client
        generator.LLM_BASE_DELAY = old_delay


# ==================== 六阶段 Agent 全流程集成冒烟 ====================

def check_pipeline():
    print("\n【六阶段 Agent · 全流程集成冒烟（真实 DeepSeek 生成）】")
    o = Orchestrator()
    task = "审计 对某国有企业2025年度差旅费报销情况开展专项审计"
    o.run_pipeline(task)
    for s in STAGE_ORDER:
        key = STAGE_OUTPUT_KEY[s]
        v = o.ctx.get(key)
        ok = bool(v) and len(str(v)) > 50
        record("全流程", f"{STAGE_NAMES[s]} 产出非空", ok, f"长度 {len(str(v)) if v else 0}")
    record("全流程", f"现场实施异常 {len(o.ctx.get('anomalies', []))} 条", len(o.ctx.get("anomalies", [])) >= 1)
    record("全流程", f"法规检索 {len(o.ctx.get('retrieved', []))} 条", len(o.ctx.get("retrieved", [])) >= 1)
    record("全流程", f"数据源 {o.ctx.get('data_source')}", bool(o.ctx.get("data_source")))


# ==================== 主入口 ====================

def main():
    LIVE = "--live" in sys.argv
    print("=" * 64)
    print("审计数字员工 · 系统自检 + 55 测试用例" + ("（含真实 LLM）" if LIVE else "（LLM 已 mock，离线）"))
    print("=" * 64)

    check_db()
    check_kb()
    check_prompts()
    check_agents()
    check_tools()
    check_out_of_order_format()
    check_persistence()
    check_llm_error()

    print("\n【55 测试用例 · 意图识别 / 阶段判定 / 路由 / 角色会签】")
    passed = 0
    skipped_llm = 0
    for c in CASES:
        if c["mode"] == "intent_llm" and not LIVE:
            skipped_llm += 1
            continue  # 离线模式跳过真实 LLM 精判兜底用例
        ok, detail = run_case(c)
        if ok:
            passed += 1
        record(c["cat"], f"{c['id']:02d} {c['name']}", ok, detail)
    if skipped_llm:
        print(f"  （离线模式：跳过 {skipped_llm} 条真实 LLM 意图用例，加 --live 可跑）")

    if "--pipeline" in sys.argv:
        check_pipeline()

    # 汇总
    total = len(RESULTS)
    fail = [r for r in RESULTS if not r["ok"]]
    print("\n" + "=" * 64)
    print(f"总计 {total} 项，通过 {total - len(fail)}，失败 {len(fail)}")
    cats = {}
    for r in RESULTS:
        cats.setdefault(r["cat"], [0, 0])
        cats[r["cat"]][0] += 1
        cats[r["cat"]][1] += 0 if r["ok"] else 1
    for k, (t, f) in cats.items():
        print(f"  {k}: {t - f}/{t} 通过")
    if fail:
        print("\n失败项：")
        for r in fail:
            print(f"  [FAIL] {r['cat']} · {r['name']}  -> {r['detail']}")
    print("=" * 64)

    # 落盘 JSON 报告
    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({"total": total, "passed": total - len(fail), "failed": len(fail), "results": RESULTS},
                  f, ensure_ascii=False, indent=2)
    print(f"报告已写入 {report_path}")

    sys.exit(0 if not fail else 1)


if __name__ == "__main__":
    main()
