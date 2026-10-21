"""豆包级交互回复层：把调度中心的状态变化转成友好、可复制、带折叠块的回复。

回复结构（对齐「审计数字员工智能体系统指令」）：
  1. 主对话气泡：状态提示 + 可复制操作模板 + 简短操作说明（用户第一眼看到）
  2. <details> 思考过程（默认折叠）
  3. <details> 数据溯源与计算过程（仅生成业务成果时附带，默认折叠）

本模块只做「从 ctx 取状态 → 渲染文案」，不调用模型、不虚构数据/法规/文号。
对外函数只接收字符串/元组等简单参数，由 orchestrator 计算好状态后传入渲染。
"""
from roles import role_name

SEP = "——————————"


# ==================== 会签项 → 回复模板 ====================
# 每个会签项：(head 状态提示, lead 模板引导语, template 可复制内容, note 操作说明, rule 业务规则)
# head 里 {stage}/{project} 为占位符，渲染时按阶段名/项目名填充。
GATES = {
    "立项依据已确认": dict(
        head="📢待您审核：《2026年审计计划需求表》\n项目名称：{project}",
        lead="📝您可以复制下面模板，修改状态后发送完成审核：",
        template="审核结果：通过/驳回\n审核意见：",
        note="💡操作说明：输入模板内容提交，审核通过后项目负责人将收到立项准入提醒，可以完善项目信息正式立项。",
        rule="立项依据缺失时系统自动生成《2026年审计计划需求表》，须业务负责人审核通过后方可正式立项。",
    ),
    "实施方案主审审核通过": dict(
        head="📢当前：【{stage}阶段】，待您处理：审核审计实施方案。",
        lead="📝您可以复制模板，填写审核意见后发送完成审批：",
        template="实施方案审核：\n审核结果：通过/驳回\n审核意见：",
        note="💡提示：\n1.审核通过后，流转至组长复核；驳回将退回修改实施方案。\n2.组长审核完成后，系统自动把实施方案条目化，生成【审计事项清单】（审计事项/重点内容/疑点线索三级），作为后续底稿、质量考核依据。\n3.清单生成后需要确认人员分工、时间计划、审计工作方法。",
        rule="实施方案须主审审核通过后方流转组长复核；驳回退回修改。",
    ),
    "实施方案组长审核通过": dict(
        head="📢当前：【{stage}阶段】，主审已完成实施方案审核，请组长复核。",
        lead="📋审核操作模板，复制修改发送：",
        template="实施方案复核：\n审核结果：通过/驳回\n审核意见：",
        note="✅通过后系统自动生成三级审计事项清单；\n驳回将退回主审重新修订实施方案。",
        rule="实施方案须主审审核通过后方流转组长复核；驳回退回修改。",
    ),
    "承诺书已签署": dict(
        head="📢当前：【{stage}阶段】\n已下发审计通知书、审前资料清单。",
        lead="📋请复制模板反馈处理结果：",
        template="审前准备反馈：\n审前调查表单：已审核完成\n审计单位承诺书：已签署完成\n备注：",
        note="提交完成后等待项目组推进至现场实施。",
        rule="现场实施前须被审计单位签署承诺书、确认审前调查表单。",
    ),
    "底稿审计人员确认": dict(
        head="📢当前：【{stage}阶段】，待您审核审计底稿。",
        lead="📋底稿审核模板：",
        template="审计底稿审核：\n审核结果：通过/驳回\n修改备注：",
        note="💡规则：审计报告签发前底稿支持修改；报告签发后底稿自动锁定不可编辑。\n审核通过后下发迎审人员签字确认。",
        rule="审计报告签发前底稿可修改，签发后自动锁定。",
    ),
    "底稿被审计单位签字": dict(
        head="📢当前：【{stage}阶段】请对审计底稿进行签字确认，如有异议可填写说明并上传佐证。",
        lead="📝反馈模板：",
        template="底稿签字确认：\n确认状态：同意/持有异议\n异议说明：\n佐证材料：【上传附件】",
        note="提交后底稿回流至主审。",
        rule="底稿须被审计单位签字确认；有异议可上传佐证。",
    ),
    "问题定性主审确认": dict(
        head="📢当前：【{stage}阶段】，请主审复核问题定性。",
        lead="📋定性确认模板：",
        template="问题定性确认：\n审核结果：通过/驳回\n审核意见：",
        note="定性确认通过后，方可进入审计报告编制。",
        rule="问题定性以主审复核为准，未确认不进入报告编制。",
    ),
    "报告主审审核通过": dict(
        head="📢当前：【{stage}阶段】，系统已生成审计问题清单与审计报告初稿，请主审审核。",
        lead="📝审核模板，复制提交：",
        template="审计报告初稿审核：\n审核结果：通过/驳回\n修改意见：",
        note="审核通过推送组长/项目负责人复核；驳回重新修订报告。",
        rule="审计报告初稿须主审审核通过后方流转复核。",
    ),
    "报告组长负责人审核通过": dict(
        head="📢当前：【{stage}阶段】，主审已审核报告，请组长/项目负责人复核。",
        lead="📋复核模板：",
        template="报告复核：\n审核结果：通过/驳回\n复核意见：",
        note="通过后推送审理组集体审核；驳回退回主审修改。",
        rule="报告须主审审核后由组长/项目负责人复核，再送审理组。",
    ),
    "审理组审核通过": dict(
        head="📢当前：【{stage}阶段】，待审理组开展报告集体审核。",
        lead="📝审理审核模板：",
        template="审理组审核：\n审核结果：通过/驳回\n审理意见：",
        note="审核通过，系统生成审计报告征求意见稿下发被审计单位。",
        rule="报告须经审理组集体审核后，方生成征求意见稿。",
    ),
    "征求意见反馈已回收": dict(
        head="📢当前：【{stage}阶段】，请处理审计报告征求意见稿，反馈意见并上传佐证。",
        lead="📋反馈模板：",
        template="征求意见反馈：\n反馈意见：\n佐证附件：【上传佐证】",
        note="提交后主审汇总意见，生成审计报告完成稿。",
        rule="征求意见反馈回收后，主审汇总形成报告完成稿。",
    ),
    "整改成果复核通过": dict(
        head="📢当前：【{stage}阶段】，待复核被审计单位提交的整改成果。",
        lead="📋整改复核模板：",
        template="整改成果复核：\n问题编号：\n复核结果：整改通过 / 整改不通过退回 / 问题结转持续整改\n复核意见：",
        note="💡说明：\n1.整改通过：执行问题销号；\n2.整改不通过退回：交由迎审人员重新整改填报；\n3.问题结转持续整改：纳入持续整改台账跟踪。\n全部问题复核完成后，项目进入归档阶段。",
        rule="整改结果三态：通过销号 / 不通过退回重改 / 结转持续整改。",
    ),
    "归档审批": dict(
        head="✅当前：【{stage}阶段】，全部整改事项已复核完毕。\n系统将自动汇总：整改问题清单、审计建议清单、违规追责建议等全套项目资料，下发对应整改负责人、业务部门负责人备案留存。",
        lead="📋归档确认模板，复制发送确认归档：",
        template="项目归档确认：\n归档备注：",
        note="确认提交后，本审计项目全流程闭环完成。",
        rule="归档须人工审批；追责建议由有权部门审定，系统不作责任认定。",
    ),
}

# 各阶段「产出完成」的状态提示（阶段产出回复的头部，{stage} 为占位符）
STAGE_PRODUCED_HEAD = {
    "initiation": "✅收到立项请求，正在自动提取信息生成项目信息表单。",
    "preaudit": "✅当前：【{stage}阶段】，已生成审计实施方案、审计通知书、审前资料清单、审前调查表单与承诺书。",
    "field": "✅当前：【{stage}阶段】，已生成审计工作底稿、现场会议议程与实施资料清单。",
    "report": "✅当前：【{stage}阶段】，已生成审计问题清单与审计报告初稿。",
    "rectification": "✅当前：【{stage}阶段】，已生成整改台账与督办建议。",
    "archive": "✅当前：【{stage}阶段】，已汇总整改问题清单、审计建议清单、违规追责建议等全套资料。",
}

# 立项阶段：信息表单补全模板（场景 1）
INITIATION_FILL = dict(
    head="✅收到立项请求，正在自动提取信息生成项目信息表单。",
    lead="📋【待补全项目信息模板】，如果部分信息未识别到，请复制模板修改后发送给我补录：",
    template="项目所属单位：总部/直属单位\n项目名称：{project}\n项目组长：\n主审：\n业务负责人：\n立项依据：\n被审计单位名称：\n迎审人员：",
    note="💡提示：\n1. 如果缺少立项依据，系统会自动生成《2026年审计计划需求表》提交业务负责人审核；\n2. 业务负责人审核通过后，本项目才允许正式立项；\n3. 全部信息补全校验通过，项目自动流转至【审前准备阶段】。",
)

# 立项阶段：越界 / 尚未立项时的「新建项目」模板
NEW_PROJECT_TEMPLATE = "帮我创建____专项审计项目\n项目所属单位：总部/直属单位\n被审计单位："

# 各阶段「数据溯源」内容（来源 / 计算过程 / 业务规则）
TRACE = {
    "initiation": (
        "用户输入的审计任务 + 系统默认「待补全」占位",
        "从任务解析抽取字段 → 缺失字段标「待补全」→ 补全后生成立项申请",
        "审计立项须明确被审计单位、审计类型、审计期间（审计实务基线）",
    ),
    "preaudit": (
        "任务解析结果 + 知识库《审计实施方案编制规范》检索 + 历史问题分布（真实脱敏数据）",
        "按审计类型套实施方案模板 → 结合数据画像 → 生成通知书 / 资料清单 / 调查表单 / 承诺书",
        "实施方案 9 章结构；审前资料清单按固定目录；承诺书为被审计单位签署件",
    ),
    "field": (
        "任务解析 + 真实脱敏数据源取数筛查 + 知识库法规检索 + LLM 风险判断",
        "取数筛查异常 → 法规检索匹配依据 → 风险定性 → 生成底稿",
        "风险点必须引用知识库法规依据；检索不到则明确「信息不足」，不臆造",
    ),
    "report": (
        "现场风险点 / 底稿 + 知识库报告编制规范检索 + 审计类型路由模板",
        "按审计类型套报告模板 → 汇总问题清单 → 生成征求意见稿",
        "四大审计类型对应四套报告模板；问题定性以人工闸门确认为准",
    ),
    "rectification": (
        "风险点 + 知识库整改督办规范检索 + 整改台账模板",
        "逐问题生成整改措施 / 责任部门 / 完成时限 / 销号标准 → 复核三态",
        "整改结果三态：通过销号 / 不通过退回重改 / 结转持续整改",
    ),
    "archive": (
        "各阶段产出汇总：问题清单 / 报告 / 整改台账 / 底稿 / 通知书",
        "汇总归档三清单（问题 / 建议 / 追责）→ 归档审批",
        "归档须人工审批；追责建议最终由有权部门审定，系统不作责任认定",
    ),
}


# ==================== 渲染工具 ====================

def _fmt(s, stage="", project="", role=""):
    return s.format(stage=stage, project=project, role=role)


def _think(reason, rule=None):
    """思考过程折叠块（默认折叠）"""
    lines = ["<details>", "<summary>🔍思考过程（点击展开）</summary>", "", reason]
    if rule:
        lines += ["", f"业务规则：{rule}"]
    lines.append("</details>")
    return "\n".join(lines)


def _trace(stage_key):
    """数据溯源折叠块（默认折叠）"""
    src, proc, rule = TRACE.get(stage_key, ("系统运行结果", "按六阶段流程逐步产出", "六阶段审计闭环"))
    return "\n".join([
        "<details>",
        "<summary>📊数据溯源与计算过程（点击展开）</summary>",
        "",
        f"1. 数据来源：{src}",
        f"2. 计算/推导过程：{proc}",
        f"3. 业务规则依据：{rule}",
        "</details>",
    ])


def _gate_body(gate_name, stage="", project=""):
    """某会签项的主对话内容（状态提示 + 模板 + 说明）"""
    g = GATES.get(gate_name)
    if not g:
        return ""
    parts = [_fmt(g["head"], stage=stage, project=project)]
    if g.get("lead"):
        parts += ["", g["lead"]]
    parts += ["", SEP, g["template"], SEP]
    if g.get("note"):
        parts += ["", g["note"]]
    return "\n".join(parts)


def _assemble(body, think_reason, rule=None, stage_key=None):
    """主对话内容 + 思考过程折叠块 +（可选）数据溯源折叠块"""
    blocks = [body, "", _think(think_reason, rule)]
    if stage_key:
        blocks += ["", _trace(stage_key)]
    return "\n".join(blocks)


# ==================== 对外回复函数 ====================

def gate_reply(gate_name, stage="", project="", think_reason="", rule=None):
    """待办审核回复：某会签项待对应角色处理"""
    body = _gate_body(gate_name, stage=stage, project=project)
    return _assemble(body, think_reason, rule)


def stage_entry_reply(stage_key, stage, project, next_gate, think_reason):
    """阶段产出回复：已生成 X 文档 + 下一会签项模板 + 数据溯源"""
    head = f"🔎已识别用户任务：{stage}"
    if stage_key == "initiation":
        head += "。将调用知识库中的标准立项模板，先生成项目信息表单。"
    else:
        head += "。"
    head += "\n\n" + _fmt(STAGE_PRODUCED_HEAD.get(stage_key, "✅当前：【{stage}阶段】。"), stage=stage)
    body = head
    if stage_key == "initiation":
        body += ("\n\n系统自动填充：项目编号、项目年度、项目名称、项目类型、项目实施单位、项目负责人。"
                 "\n人工填写：项目组长、主审、业务负责人、被审计单位、迎审人员。"
                 "\n表单支持保存草稿、实时校验、补录提示和提交校验；校验通过后生成立项申请。"
                 "\n立项依据缺失时，系统生成审计计划需求表并提交业务负责人审核。")
    if next_gate:
        body += "\n\n" + _gate_body(next_gate, stage=stage, project=project)
    rule = GATES.get(next_gate, {}).get("rule")
    return _assemble(body, think_reason, rule, stage_key=stage_key)


def initiation_fill_reply(project="", think_reason="", rule=None):
    """立项阶段：信息表单待补全（场景 1）"""
    parts = ["🔎已识别用户任务：审计立项。将调用知识库中的标准立项模板，先生成项目信息表单。", "", INITIATION_FILL["head"], "", INITIATION_FILL["lead"],
             "", SEP, _fmt(INITIATION_FILL["template"], project=project), SEP,
             "", INITIATION_FILL["note"]]
    body = "\n".join(parts)
    return _assemble(body, think_reason, rule, stage_key="initiation")


def out_of_order_reply(head, reason, template, note="", think_reason="", rule=None):
    """越界提示仅保留当前阶段说明；不再输出旧的横线模板和中间状态卡。"""
    body = "\n\n".join(part for part in (head, reason) if part)
    return _assemble(body, think_reason, rule)


def fill_reply(missing, think_reason="", rule=None):
    """补全表单回复（场景 3）：仍缺字段 → 补充模板"""
    template = "补充项目信息：\n" + "\n".join(f"{f}：" for f in missing)
    parts = [
        "⚠️已更新表单，仍缺以下字段，无法完成立项校验。",
        "请复制下方模板，补齐缺失字段后发送：",
        "",
        SEP, template, SEP,
        "",
        "全部字段填写完成校验无误，即可进入审前准备阶段。",
    ]
    body = "\n".join(parts)
    return _assemble(body, think_reason, rule)


def rollback_reply(stage, branch, template, note, think_reason, rule=None):
    """回退回复：已回退至 X 阶段 + 当前阶段重做模板"""
    parts = [
        f"↩️ 已回退至【{stage}阶段】（{branch}）。",
        "该阶段及之后的产出已作废，请按当前阶段模板重新执行。",
        "",
        SEP, template, SEP,
    ]
    if note:
        parts += ["", note]
    body = "\n".join(parts)
    return _assemble(body, think_reason, rule)


def confirm_reply(confirmed, remaining, stage="", project="", think_reason="", rule=None):
    """会签确认回复：已确认 X + 尚待确认 Y（含下一会签项模板）；当前角色无待办时给中性提示"""
    if confirmed:
        head = "✅已确认：" + "、".join(f"「{n}」" for n in confirmed) + "。"
        tail = ""
    else:
        head = "⚠️ 你当前角色暂无待确认的会签项。"
        tail = "请切换到对应角色继续确认。"
    if remaining:
        head += "\n尚待确认：" + "、".join(
            f"「{n}」（{role_name(r) if r else '系统'}）" for n, r in remaining
        ) + "。"
        if tail:
            head += tail
        body = head
        first_name, first_role = remaining[0]
        body += "\n\n" + _gate_body(first_name, stage=stage, project=project)
        rule = rule or GATES.get(first_name, {}).get("rule")
    else:
        body = head + "\n本阶段会签已全部完成，自动流转下一阶段。"
    return _assemble(body, think_reason, rule)


def closed_reply(think_reason="", rule=None):
    """项目已闭环回复（再次操作时提示开新项目）"""
    body = (
        "✅ 项目已闭环归档。\n"
        "若要开启新项目，请复制下方模板修改后发送：\n\n"
        f"{SEP}\n{NEW_PROJECT_TEMPLATE}\n{SEP}"
    )
    return _assemble(body, think_reason, rule)


def closure_reply(think_reason="", rule=None):
    """归档审批刚通过的闭环回复（项目正式闭环）"""
    body = (
        "✅「归档审批」已通过，项目正式闭环归档。\n"
        "若要开启新项目，请复制下方模板修改后发送：\n\n"
        f"{SEP}\n{NEW_PROJECT_TEMPLATE}\n{SEP}"
    )
    return _assemble(body, think_reason, rule)


def wrap(body, think_reason="", rule=None, stage_key=None):
    """通用包装：主对话内容 + 思考过程折叠块（+ 可选数据溯源折叠块）。"""
    return _assemble(body, think_reason, rule, stage_key)


def unknown_reply(think_reason="", rule=None):
    """无法识别回复"""
    body = (
        "⚠️ 无法识别你要执行的操作。\n"
        "审计按严格顺序推进：①「审计 <任务>」审计立项 → ②「生成审计方案」审前准备 → "
        "③「审计/继续」现场实施 → ④「生成报告」→ ⑤「生成整改台账」→ ⑥「归档」。\n"
        "每步前置条件满足后才能进入下一步，可复制下方模板推进：\n\n"
        f"{SEP}\n{NEW_PROJECT_TEMPLATE}\n{SEP}"
    )
    return _assemble(body, think_reason, rule)
