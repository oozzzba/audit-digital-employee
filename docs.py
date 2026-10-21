"""结构化文档层：表单类/清单类文档的确定性模板生成。

与 generator.py（LLM 生成实质文档）互补：本模块只做「从已确认上下文取字段 → 渲染模板」，
不调用模型、不虚构数据/法规/文号。缺失字段一律标「待补全 / 待明确」。
"""


def _fill(v):
    """缺失判定：None / 空串 / 「未明确」→ 待补全"""
    s = str(v or "").strip()
    if not s or s in ("未明确", "None", "待补全", "待明确", "【待补充】", "【系统赋值】"):
        return "待补全"
    return s


def _kv(k, v):
    return f"- **{k}**：{_fill(v)}"


# ---------------- 项目立项 ----------------

def render_info_form(info_form, sources=None):
    """项目信息表单（项目负责人补全后确认）"""
    f = info_form or {}
    lines = ["# 项目信息表单", ""]
    lines.append("| 字段 | 内容 |")
    lines.append("| --- | --- |")
    sources = sources or {}
    for k in ("项目编号", "项目年度", "项目所属单位", "项目名称", "项目类型", "项目负责人", "项目组长", "主审",
              "立项依据", "被审计单位名称", "迎审人员", "业务负责人"):
        source = sources.get(k, {}) if isinstance(sources, dict) else {}
        label = source.get("label") if isinstance(source, dict) else None
        suffix = f"（{label}）" if label else ""
        value = f.get(k)
        # 系统字段即使当前配置缺失，也不能被渲染成“待补全”的人工必填状态。
        display = str(value or "系统未返回") if label == "系统填充" else _fill(value)
        lines.append(f"| {k} | {display}{suffix} |")
    lines.append("")
    lines.append("> 标注「待补全」的字段需项目负责人补全后确认。")
    return "\n".join(lines)


def info_form_missing(info_form):
    """返回尚缺（待补全）的字段名列表"""
    if not info_form:
        return ["项目信息表单尚未生成"]
    # 项目编号、年度、所属单位、项目类型、项目负责人由系统生成或登录上下文回填，不要求人工填写。
    return [k for k in ("项目组长", "主审", "业务负责人", "被审计单位名称", "迎审人员", "立项依据")
            if _fill(info_form.get(k)) == "待补全"]


def render_demand_form(info_form, task):
    """审计计划需求表（立项依据缺失时自动生成，待业务负责人审核）"""
    f = info_form or {}
    lines = ["# 2026 年审计计划需求表", ""]
    lines.append("> 状态：**待业务负责人审核**")
    lines.append(_kv("审计项目名称", f.get("项目名称") or task))
    lines.append(_kv("项目所属单位", f.get("项目所属单位")))
    lines.append(_kv("被审计单位", f.get("被审计单位名称")))
    lines.append(_kv("业务负责人", f.get("业务负责人")))
    lines.append("")
    lines.append("## 立项依据说明")
    lines.append("（原立项依据未检测到，本需求表用于补充立项依据，供业务负责人审核确认。）")
    return "\n".join(lines)


def render_plan_demand_form(fields=None):
    """年度审计计划需求表：用于快捷指令无计划文件或计划文件字段抽取场景。"""
    f = fields or {}
    lines = ["# 审计计划需求表", "", "> 状态：**待业务负责人审核**"]
    for key in ("计划名称", "年度", "开始时间", "结束时间", "填报单位", "操作人", "状态"):
        lines.append(_kv(key, f.get(key)))
    lines.extend(["", "## 填报说明", _fill(f.get("填报说明"))])
    return "\n".join(lines)


# ---------------- 审前准备 ----------------

def render_pre_survey_form(info_form, parsed):
    """审前调查表单（迎审人员审核 + 签署承诺书）"""
    f = info_form or {}
    return (
        "# 审前调查表单\n\n"
        "> 请被审计单位迎审人员审核以下信息，并在确认无误后签署承诺书。\n\n"
        "| 调查项 | 内容 |\n| --- | --- |\n"
        f"| 被审计单位 | {_fill(f.get('被审计单位名称'))} |\n"
        f"| 审计期间 | {_fill((parsed or '')) and '以审计任务为准'} |\n"
        f"| 迎审人员 | {_fill(f.get('迎审人员'))} |\n"
        f"| 业务负责人 | {_fill(f.get('业务负责人'))} |\n\n"
        "> 状态：待迎审人员审核确认"
    )


def render_problem_list(risks_list):
    """审计问题清单（报告阶段，基于已确认风险点）"""
    lines = ["# 审计问题清单", ""]
    if risks_list:
        lines.append("| 序号 | 问题名称 | 问题定性 | 风险等级 |")
        lines.append("| --- | --- | --- | --- |")
        for i, r in enumerate(risks_list, 1):
            cat = str(r.get("风险类别", "")).strip() or "未分类"
            level = str(r.get("风险等级", "")).strip() or "待定"
            lines.append(f"| {i} | {cat} | 待人工确认 | {level} |")
    else:
        lines.append("（无已确认问题）")
    lines.append("")
    lines.append("> 问题定性以人工闸门确认为准，本清单标注「待人工确认」。")
    return "\n".join(lines)


def render_preaudit_materials(info_form, parsed):
    """审前资料清单（下发迎审人员）"""
    f = info_form or {}
    lines = ["# 审前资料清单", ""]
    lines.append("> 请被审计单位迎审人员按以下清单准备并提交资料。")
    for i, item in enumerate([
        "被审计单位基本情况说明",
        "相关制度文件与内控制度",
        "审计期间财务账册、凭证及报表",
        "合同、台账等业务资料",
        "前期自查与整改情况材料",
        "迎审人员及业务负责人联系方式",
    ], 1):
        lines.append(f"{i}. {item}")
    lines.append("")
    lines.append(_kv("被审计单位", f.get("被审计单位名称")))
    lines.append(_kv("迎审人员", f.get("迎审人员")))
    return "\n".join(lines)


def render_commitment(info_form):
    """审计单位承诺书（迎审人员签署）"""
    f = info_form or {}
    return (
        "# 审计单位承诺书\n\n"
        "我单位已按要求配合审计工作，对所提供资料的真实性、完整性负责，并承诺：\n"
        "1. 及时、如实提供审计所需的全部资料；\n"
        "2. 积极配合审计人员询问、调查与取证；\n"
        "3. 遵守审计纪律，不干扰审计工作。\n\n"
        f"- **被审计单位**：{_fill(f.get('被审计单位名称'))}\n"
        f"- **迎审人员（签字）**：{_fill(f.get('迎审人员'))}\n"
        f"- **签署日期**：待签署\n\n"
        "> 状态：待迎审人员签署"
    )


# ---------------- 现场实施 ----------------

def render_meeting_agenda(info_form, parsed):
    """现场审计会议通知 + 议程"""
    f = info_form or {}
    return (
        "# 现场审计会议通知\n\n"
        f"- **审计项目**：{_fill(f.get('项目名称'))}\n"
        f"- **被审计单位**：{_fill(f.get('被审计单位名称'))}\n\n"
        "## 会议议程\n"
        "1. 审计组介绍审计目标、范围与依据；\n"
        "2. 被审计单位介绍情况；\n"
        "3. 明确现场实施资料需求与时间安排；\n"
        "4. 双方确认联络人与配合事项。\n\n"
        "> 状态：待组长审核确认后下发"
    )


def render_field_materials(info_form, audit_plan):
    """实施资料清单 + 实施调查表"""
    f = info_form or {}
    lines = ["# 现场实施资料清单", ""]
    for i, item in enumerate([
        "审计实施方案及已审批底稿事项清单",
        "被审计单位账册、凭证、报表",
        "业务合同、台账、审批单据",
        "现场访谈记录表",
        "取证材料（含复印件、照片、系统截图）",
    ], 1):
        lines.append(f"{i}. {item}")
    lines.append("")
    lines.append("## 实施调查表")
    lines.append("| 调查事项 | 调查方法 | 责任人 | 结果 |")
    lines.append("| --- | --- | --- | --- |")
    lines.append("| 资金使用合规性 | 检查账册并分析性复核 | 待定 | 待反馈 |")
    lines.append("| 采购流程合规性 | 查阅资料、观察及询问 | 待定 | 待反馈 |")
    lines.append("| 内控执行情况 | 访谈与穿行测试 | 待定 | 待反馈 |")
    lines.append("")
    lines.append("> 状态：待组长/主审审核确认后下发给迎审人员反馈")
    return "\n".join(lines)


def render_audit_items(parsed, risks_list):
    """审计事项清单：按「审计事项 / 重点内容 / 疑点线索」三层条目化"""
    lines = ["# 审计事项清单", ""]
    lines.append("> 依据已审批审计实施方案条目化，供编写底稿时关联。")
    lines.append("| 序号 | 审计事项 | 重点内容 | 疑点线索 |")
    lines.append("| --- | --- | --- | --- |")
    if risks_list:
        for i, r in enumerate(risks_list, 1):
            cat = str(r.get("风险类别", "")).strip() or "待定"
            desc = str(r.get("风险描述", "")).strip() or "待核实"
            lines.append(f"| {i} | {cat} | 待定 | {desc} |")
    else:
        lines.append("| 1 | 待定 | 待定 | 待核实 |")
    lines.append("")
    lines.append("> 「重点内容」「疑点线索」以已确认风险点为准，未确认处标注「待定」。")
    return "\n".join(lines)


# ---------------- 整改督办 ----------------

def render_rectification_review(risks_list):
    """整改成果复核（三态：销号 / 继续整改 / 结转持续整改）"""
    lines = ["# 整改成果复核", ""]
    lines.append("> 状态：待主审/组长复核。复核结论三选一：通过销号 / 不通过继续整改 / 未完成结转持续整改。")
    if risks_list:
        lines.append("| 序号 | 问题 | 复核结论 |")
        lines.append("| --- | --- | --- |")
        for i, r in enumerate(risks_list, 1):
            cat = str(r.get("风险类别", "")).strip() or "未分类"
            lines.append(f"| {i} | {cat} | 待复核 |")
    else:
        lines.append("（无已确认问题）")
    lines.append("")
    lines.append("> 复核通过的问题可销号；不通过的继续整改；未完成的结转进入持续整改。")
    return "\n".join(lines)


# ---------------- 项目归档 ----------------

def render_archive_three(parsed, risks_list, report, rectification):
    """归档三清单：问题清单 / 审计建议清单 / 违规追责建议"""
    lines = ["# 项目归档三清单", ""]
    lines.append("## 一、审计问题清单")
    if risks_list:
        for i, r in enumerate(risks_list, 1):
            cat = str(r.get("风险类别", "")).strip() or "未分类"
            desc = str(r.get("风险描述", "")).strip() or "待核实"
            lines.append(f"{i}. {cat}：{desc}")
    else:
        lines.append("（无已确认问题）")
    lines.append("")
    lines.append("## 二、审计建议清单")
    lines.append("（基于已确认问题与整改台账汇总，见整改督办阶段产出。）")
    lines.append("")
    lines.append("## 三、违规追责建议")
    lines.append("（仅列示需追责事项，最终追责由有权部门审定，本系统不作责任认定。）")
    lines.append("")
    lines.append("> 状态：待归档审批后下发对应部门整改负责人、业务部门负责人。")
    return "\n".join(lines)
