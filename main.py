"""审计数字员工 · 主入口

六阶段闭环：立项 → 审前准备 → 现场实施 → 报告编制 → 整改督办 → 项目归档
由调度中心（orchestrator）编排，人工终审兜底。

用法：
    python main.py
    1) 输入「审计 <任务>」→ 开始立项（六阶段第一步）
    2) 输入「继续 / 下一步」→ 按顺序推进到下一阶段
    3) 输入「生成审计方案 / 生成报告 / 生成整改台账 / 归档」→ 进入对应阶段（需上游阶段已完成）
    4) 输入「自动审计 <任务>」→ 一键跑完六阶段
"""
from orchestrator import Orchestrator


_PIPELINE_TRIGGERS = ["自动审计", "一键审计", "全流程"]


def _banner(text):
    print("=" * 60)
    print(text)
    print("=" * 60)


def _show_field(ctx):
    """展示现场实施阶段的产出"""
    print("[1/4] 任务解析")
    print(ctx["parsed"])
    print()

    print("[2/4] 取数筛查（取数工具）")
    if ctx.get("data_source"):
        print(f"数据源：{ctx['data_source']}")
    print(ctx.get("anomalies_text", ""))
    print()

    print("[3/4] 法规检索")
    for i, r in enumerate(ctx["retrieved"], 1):
        print(f"  [{i}] {r['source']}（相关度 {r['score']}）")
        print(f"      {r['text'][:70].replace(chr(10), ' ')}...")
    print()

    print("[4/4] 风险判断")
    print(ctx["risks"])
    print()

    print("── 审计工作底稿 ──")
    print(ctx["workpaper"])


def _show_stage(stage, ctx):
    if stage == "initiation":
        print("── 审计立项申请 ──")
        print(ctx["initiation"])
        print()
        print("【状态】立项完成 —— 待「立项审批通过」")
        print("下一步：输入「生成审计方案」或「继续」进入审前准备")
    elif stage == "preaudit":
        print("── 审计实施方案 ──")
        print(ctx["audit_plan"])
        print()
        print("【状态】审前准备完成 —— 待「方案审批 + 通知签发」")
        print("下一步：输入「继续」或「审计」进入现场实施")
    elif stage == "field":
        _show_field(ctx)
        print()
        print("【状态】现场实施完成 —— 待「问题定性确认」")
        print("下一步：输入「生成报告」或「继续」进入报告编制")
    elif stage == "report":
        print("── 审计报告初稿 ──")
        print(ctx["report"])
        print()
        print("【状态】报告编制完成 —— 待「报告审签发布」")
        print("下一步：输入「生成整改台账」或「继续」进入整改督办")
    elif stage == "rectification":
        print("── 整改台账与督办建议 ──")
        print(ctx["rectification"])
        print()
        print("【状态】整改督办完成 —— 待「整改闭环销号」（或项目终止）")
        print("下一步：输入「归档」或「继续」进入项目归档")
    elif stage == "archive":
        print("── 项目归档清单 ──")
        print(ctx["archive"])
        print()
        print("【状态】归档完成 —— 待「归档审批」")


def _extract_pipeline_task(text):
    for t in _PIPELINE_TRIGGERS:
        text = text.replace(t, "")
    return text.strip()


def _show_pipeline(ctx):
    _banner("【一、立项】")
    print("── 审计立项申请 ──")
    print(ctx.get("initiation", ""))
    print("\n▶ 【人工闸门】本阶段产出待人工确认，确认后进入下一阶段\n")

    _banner("【二、审前准备】")
    print("── 审计实施方案 ──")
    print(ctx.get("audit_plan", ""))
    print("\n▶ 【人工闸门】本阶段产出待人工确认，确认后进入下一阶段\n")

    _banner("【三、现场实施】")
    _show_field(ctx)
    print("\n▶ 【人工闸门】本阶段产出待人工确认，确认后进入下一阶段\n")

    _banner("【四、报告编制】")
    print("── 审计报告初稿 ──")
    print(ctx.get("report", ""))
    print("\n▶ 【人工闸门】本阶段产出待人工确认，确认后进入下一阶段\n")

    _banner("【五、整改督办】")
    print("── 整改台账与督办建议 ──")
    print(ctx.get("rectification", ""))
    print("\n▶ 【人工闸门】本阶段产出待人工确认，确认后进入下一阶段\n")

    _banner("【六、项目归档】")
    print("── 项目归档清单 ──")
    print(ctx.get("archive", ""))
    print()
    _banner("全流程跑通 —— 最终由人工终审放行（合规红线）")


def run(orchestrator, task_input):
    # 一键全流程
    if any(t in task_input for t in _PIPELINE_TRIGGERS):
        task = _extract_pipeline_task(task_input)
        if not task:
            print("⚠️ 请提供具体审计任务，例如：自动审计 对某国企差旅费报销开展审计")
            return
        _banner(f"【自动审计·全流程】{task}")
        ctx = orchestrator.run_pipeline(task)
        if ctx.get("blocked"):
            print(f"⚠️ {ctx['blocked_msg']}")
            return
        _show_pipeline(ctx)
        return

    # 单阶段
    _banner(f"【审计任务】{task_input}")
    stage, ctx = orchestrator.dispatch(task_input)

    if ctx is None:
        return

    if ctx.get("blocked"):
        print(f"⚠️ {ctx['blocked_msg']}")
        return

    _show_stage(stage, ctx)
    print()
    print("=" * 60)
    print("【状态】AI 仅生成草稿，最终放行权在人（合规红线）")
    print("=" * 60)


if __name__ == "__main__":
    orch = Orchestrator()
    print("审计数字员工 · 六阶段闭环已就绪")
    print("  ① 立项 → ② 审前准备 → ③ 现场实施 → ④ 报告编制 → ⑤ 整改督办 → ⑥ 项目归档")
    print("  输入「审计 <任务>」开始立项，之后「继续 / 下一步」按顺序推进")
    print("  输入「自动审计 <任务>」：一键跑完六阶段")
    print("  退出：输入 exit\n")

    while True:
        try:
            task = input("请输入：").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if task.lower() == "exit":
            break
        if not task:
            continue
        try:
            run(orch, task)
        except Exception as e:
            print(f"❌ 运行出错：{e}")
        print()
