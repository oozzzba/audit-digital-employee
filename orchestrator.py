"""调度中心：阶段判定 + 路由 + 项目上下文管理 + 全流程编排。

把审计六阶段「审计立项 → 审前准备 → 现场实施 → 审计报告编制 → 问题整改 → 项目归档」
编排成一条可衔接的闭环，内置**人工闸门（流转条件）+ 回退分支**：

  开始：审计任务下达
  审计立项   流转条件：立项审批通过
  审前准备   回退分支：资料不足回补；流转条件：方案审批 + 通知签发
  现场实施   回退分支：缺证据回补；流转条件：问题定性确认
  审计报告编制 回退分支：退回修订 / 补证；流转条件：报告审签发布
  问题整改   回退分支：整改不通过退回；流转条件：整改闭环销号
  项目归档   回退分支：缺件退回补齐；流转条件：归档审批

每一阶段的产出是「草稿」，必须通过对应的**人工闸门**后进入下一阶段；闸门驳回
则按回退分支回到本阶段重做。用户说「审计 / 继续 / 下一步 / 确认 / 通过」推进；
「驳回 / 退回 / 回补 / 修订 / 补证 / 不通过」触发回退；乱序请求会被拦截。

合规红线（非审计任务拒答、信息不足不臆造）由各 Agent 内建拦截。

对外入口：
- dispatch()：CLI 用，错误时打印到 stdout
- dispatch_silent()：Web 用，错误以字符串返回，不打印
"""
import re

from agents import (
    InitiationAgent,
    PreAuditAgent,
    FieldAgent,
    ReportAgent,
    RectificationAgent,
    ArchiveAgent,
)
from generator import answer_general_chat, LLMError
from roles import ROLES, DEFAULT_ROLE, role_name, notify
from docs import info_form_missing
import replies


# 六阶段顺序 + 中文名（供 Web 前端 / 全流程 / 看板复用）
STAGE_ORDER = ["initiation", "preaudit", "field", "report", "rectification", "archive"]
STAGE_NAMES = {
    "initiation": "审计立项",
    "preaudit": "审前准备",
    "field": "现场实施",
    "report": "审计报告编制",
    "rectification": "问题整改",
    "archive": "项目归档",
}

# 各阶段「产出」名称（乱序拦截提示原因句：……需要完成……后才可以使用）
_STAGE_OUTPUT = {
    "initiation": "立项",
    "preaudit": "审计实施方案",
    "field": "工作底稿",
    "report": "审计报告",
    "rectification": "整改台账",
    "archive": "项目归档",
}


# 阶段 → 完成标记（ctx 中哪个 key 出现即视为该阶段草稿已产出）
STAGE_OUTPUT_KEY = {
    "initiation": "initiation",
    "preaudit": "audit_plan",
    "field": "workpaper",
    "report": "report",
    "rectification": "rectification",
    "archive": "archive",
}

# 进入某阶段需满足的前置条件（会签链）：(条件名, 负责角色)。role=None 表示系统自动产出。
# 角色感知：某角色「确认」时只满足该角色负责的会签项，全部满足才进入下一阶段。
STAGE_PRECONDITIONS = {
    "initiation": [("审计任务已下达", None)],         # 开始：审计任务下达
    "preaudit": [
        ("立项信息已填写完整", "project_lead"),        # 项目负责人补全并确认
        ("立项依据已确认", "business_lead"),           # 业务负责人审核需求表/立项依据
    ],
    "field": [
        ("审前调查表已审核", "respondent"),             # 迎审人员审核调查表
        ("实施方案主审审核通过", "chief_auditor"),      # 主审审核方案
        ("实施方案组长审核通过", "group_lead"),         # 组长审核方案
        ("承诺书已签署", "respondent"),                # 迎审人员签署承诺书
    ],
    "report": [
        ("现场实施资料组长审核通过", "group_lead"),     # 现场会议/资料包审核
        ("现场实施资料主审审核通过", "chief_auditor"),   # 现场资料包二级审核
        ("底稿审计人员确认", "auditor"),               # 审计人员确认底稿
        ("底稿被审计单位签字", "respondent"),           # 迎审人员签字
        ("问题定性主审确认", "chief_auditor"),          # 主审定性确认
    ],
    "rectification": [
        ("报告审签发布", "chief_auditor"),              # 报告正式签发后锁定底稿
        ("报告主审审核通过", "chief_auditor"),
        ("报告组长负责人审核通过", "group_lead"),
        ("审理组审核通过", "review_panel"),
        ("征求意见反馈已回收", "respondent"),
    ],
    "archive": [
        ("整改成果复核通过", "chief_auditor"),          # 或项目终止
    ],
}

# 最终闸门：六阶段产出完成后，项目归档还须「归档审批」才正式闭环
FINAL_GATE = [("归档审批", "chief_auditor")]

# 各阶段人工闸门驳回 → 回退分支（回到本阶段重做）
ROLLBACK_BRANCHES = {
    "preaudit": "资料不足回补",
    "field": "缺证据回补",
    "report": "退回修订 / 补证",
    "rectification": "整改不通过退回",
    "archive": "缺件退回补齐",
}

# 回退时需作废的上下文键（回到本阶段重做；不含上游 task/parsed 等公共上下文）
_STAGE_CLEAR_KEYS = {
    "preaudit": ["audit_plan"],
    "field": ["workpaper", "risks", "risks_list", "retrieved",
              "anomalies", "anomaly_summary", "anomalies_text", "data_source"],
    "report": ["report", "audit_type"],
    "rectification": ["rectification"],
    "archive": ["archive"],
}

# 全流程定义（供 /api/flow 前端「流程引擎」渲染）：阶段 + 流转条件 + 回退分支
FLOW = {
    "start": "审计任务下达",
    "stages": [
        {"key": "initiation", "name": "审计立项", "gate": "立项审批通过", "rollback": None},
        {"key": "preaudit", "name": "审前准备", "gate": "方案审批 + 通知签发", "rollback": "资料不足回补"},
        {"key": "field", "name": "现场实施", "gate": "问题定性确认", "rollback": "缺证据回补"},
        {"key": "report", "name": "审计报告编制", "gate": "报告审签发布", "rollback": "退回修订 / 补证"},
        {"key": "rectification", "name": "问题整改", "gate": "整改闭环销号", "rollback": "整改不通过退回"},
        {"key": "archive", "name": "项目归档", "gate": "归档审批", "rollback": "缺件退回补齐"},
    ],
}


# 关键词兜底：LLM 阶段判定不稳定时，先用明确关键词路由（具体阶段排前）
_STAGE_KEYWORDS = {
    "initiation": ["立项"],
    "preaudit": ["审前", "方案"],
    "report": ["报告", "汇总", "出具", "成文"],
    "rectification": ["整改", "督办", "台账", "销号", "跟踪"],
    "archive": ["归档", "档案"],
    "field": ["底稿", "风险", "现场", "检索", "法规"],
}

# 确认/审批类指令：表示「当前闸门已通过」，触发确认并推进到下一阶段
_CONFIRM_KEYWORDS = ["通过", "确认", "放行", "审批", "发布", "闭环"]

# 驳回/回退类指令：闸门驳回 → 回到本阶段重做（「不通过」须在「通过」之前判定）
_REJECT_KEYWORDS = ["驳回", "退回", "回补", "修订", "补证", "补齐", "不通过", "重做", "返工"]

# 泛化推进指令：不指定具体阶段，按当前所处状态推进到下一个待办阶段
_ADVANCE_KEYWORDS = ["审计", "继续", "下一步", "开始", "启动", "接着", "往下", "往后", "推进"]


def _keyword_stage(text):
    for stage, words in _STAGE_KEYWORDS.items():
        if any(w in text for w in words):
            return stage
    return None


def _is_confirm(text):
    return any(w in text for w in _CONFIRM_KEYWORDS)


def _is_reject(text):
    value = str(text or "").strip()
    # 业务输入中常会出现“补齐资料/补证据”等正常生成要求，不能因此误触发整阶段回退。
    # 仅将明确的驳回词，或短指令形式的“回补/修订/补证/补齐”识别为回退操作。
    explicit = ["驳回", "退回", "不通过", "重做", "返工"]
    if any(word in value for word in explicit):
        return True
    return len(value) <= 12 and any(word in value for word in ["回补", "修订", "补证", "补齐"])


def _is_advance(text):
    return any(w in text for w in _ADVANCE_KEYWORDS)


def _is_terminate(text):
    return "终止" in text


# 项目信息表单字段补全：识别「字段=值 / 字段：值」输入
_PROJECT_FORM_FIELDS = ["项目所属单位", "项目名称", "项目负责人", "项目组长", "主审",
                        "立项依据", "被审计单位名称", "迎审人员", "业务负责人"]

_FIELD_ALIASES = {
    "项目所属单位": ["项目所属单位", "项目实施单位", "所属单位", "单位"],
    "项目名称": ["项目名称", "名称"],
    "项目负责人": ["项目负责人", "负责人"],
    "项目组长": ["项目组长", "组长"],
    "主审": ["主审"],
    "立项依据": ["立项依据", "依据"],
    "被审计单位名称": ["被审计单位名称", "被审计单位"],
    "迎审人员": ["迎审人员", "迎审"],
    "业务负责人": ["业务负责人"],
}


def _is_fill(text):
    """是否为项目立项表补充输入，支持「字段=值」和「字段是值」写法。"""
    value = str(text or "")
    return any(sep in value for sep in ("=", "：", ":")) or any(
        re.search(rf"(?:{'|'.join(map(re.escape, aliases))})\s*(?:是|为)\s*[^，,；;\n]+", value)
        for aliases in _FIELD_ALIASES.values()
    )


def _is_form_completion_ack(text):
    """用户已在表单中完成补充后，用简短确认词触发后端重新组装成果。"""
    normalized = re.sub(r"\s+", "", str(text or "")).lower()
    return normalized in {"已补充", "补充完成", "已填写", "填写完成", "已填完", "都填好了", "已经填完"}


def _cond_name(cond):
    """会签项可能是 (name, role) 元组，也可能是纯字符串"""
    return cond[0] if isinstance(cond, tuple) else cond


def _cond_role(cond):
    return cond[1] if isinstance(cond, tuple) else None


def _stage_conds(stage):
    """进入某阶段的前置条件（会签项）列表"""
    return STAGE_PRECONDITIONS.get(stage, [])


class Orchestrator:
    def __init__(self):
        self.ctx = {}  # 项目上下文：parsed / initiation / audit_plan / risks / workpaper / report / rectification / archive / approved
        self.ctx["approved"] = set()  # 已满足的前置条件名（人工确认项）
        self.ctx["notices"] = []      # 系统通知队列（按角色）
        self.current_role = DEFAULT_ROLE
        self.agents = {
            "initiation": InitiationAgent(),
            "preaudit": PreAuditAgent(),
            "field": FieldAgent(),
            "report": ReportAgent(),
            "rectification": RectificationAgent(),
            "archive": ArchiveAgent(),
        }

    # ==================== 角色与通知 ====================

    def set_role(self, role):
        if role in ROLES:
            self.current_role = role
        return self.current_role

    def notify(self, role, text):
        notify(self.ctx, role, text)

    def _project(self):
        """项目名称（供回复模板 {project} 填充；取不到则为空，模板留空待用户填写）"""
        info = self.ctx.get("info_form")
        if isinstance(info, dict):
            name = str(info.get("项目名称") or "").strip()
            if name and name not in ("待补全", "待明确", "None", "未明确"):
                return name
        return ""

    def pending_approvals(self):
        """当前需要人工确认的会签项列表（含最终闸门）"""
        nxt = self.next_pending()
        if nxt is None:
            return [c for c in FINAL_GATE if not self._condition_satisfied(c)]
        return self.preconditions_missing(nxt)

    # ==================== 前置条件状态机 ====================

    def _condition_satisfied(self, cond):
        """某前置条件是否已满足（闸门是否已通过）"""
        name = _cond_name(cond)
        if name == "整改成果复核通过":
            # 归档前置：整改成果复核通过 或 项目终止
            return name in self.ctx["approved"] or "项目终止" in self.ctx["approved"]
        return name in self.ctx["approved"]

    def _satisfy(self, cond):
        self.ctx["approved"].add(_cond_name(cond))

    def preconditions_missing(self, stage):
        """返回进入该阶段尚未满足的前置条件（会签项）列表"""
        return [c for c in _stage_conds(stage) if not self._condition_satisfied(c)]

    def stage_done(self, stage):
        """某阶段草稿是否已产出"""
        return bool(self.ctx.get(STAGE_OUTPUT_KEY[stage]))

    def completed(self):
        """按顺序返回最后一个已产出草稿的阶段名（None 表示尚未立项）"""
        last = None
        for s in STAGE_ORDER:
            if self.stage_done(s):
                last = s
            else:
                break
        return last

    def next_pending(self):
        """下一个待产出草稿的阶段（严格顺序）；全部产出返回 None"""
        for s in STAGE_ORDER:
            if not self.stage_done(s):
                return s
        return None

    def progress_desc(self):
        """一句话描述当前所处状态（用于错误提示与进度展示）"""
        done = self.completed()
        if done is None:
            return "尚未立项（流程第 1 步，共 6 步）"
        idx = STAGE_ORDER.index(done)
        if idx + 1 < len(STAGE_ORDER):
            nxt = STAGE_ORDER[idx + 1]
            missing = self.preconditions_missing(nxt)
            gate = "、".join(
                f"「{_cond_name(c)}」（{role_name(_cond_role(c)) if _cond_role(c) else '系统'}）"
                for c in missing
            ) if missing else "已全部满足"
            return f"已完成「{STAGE_NAMES[done]}」，进入「{STAGE_NAMES[nxt]}」还差：{gate}"
        return "六阶段已全部完成（项目已归档闭环）"

    def _guard(self, stage):
        """前置守卫：进入 stage 前校验前置条件；不满足则按「顺序校验未通过」拦截，
        并输出「当前阶段可复制操作模板」，不堆砌后续阶段的前置条件。
        """
        missing = self.preconditions_missing(stage)
        if not missing:
            return stage, None

        done = self.completed()
        output = _STAGE_OUTPUT[stage]

        if done is None:
            if stage == "initiation":
                head = "⚠️ 顺序校验未通过"
                reason = "你的项目还没有下达审计任务，请先输入审计任务启动立项。"
            else:
                chain = "、".join(STAGE_NAMES[s] for s in STAGE_ORDER[:STAGE_ORDER.index(stage)])
                head = "⚠️ 顺序校验未通过"
                reason = (
                    f"你的项目尚未完成审计立项，{output}需要依次完成"
                    f"【{chain}】全部流程后才可以使用。"
                )
            template = replies.NEW_PROJECT_TEMPLATE
            note = "把横线替换成实际项目名称，补充被审计单位，发送即自动开始立项流程。"
            think = f"用户请求{output}，但项目尚未立项；六阶段严格顺序推进，已拦截跨阶段操作。"
        else:
            done_name = STAGE_NAMES[done]
            nxt = self.next_pending()
            nxt_name = STAGE_NAMES[nxt]
            head = "⚠️ 顺序校验未通过"
            reason = (
                f"你的项目已完成【{done_name}】，当前应推进【{nxt_name}】；"
                f"{output}属于后续阶段，已拦截。"
            )
            miss = self.preconditions_missing(nxt)
            gate_name = next((_cond_name(c) for c in miss if _cond_role(c)), None)
            g = replies.GATES.get(gate_name) if gate_name else None
            template = g["template"] if g else "继续 / 下一步"
            note = f"请先完成【{nxt_name}】的会签：切到对应角色，输入「通过」确认会签项。"
            think = (
                f"用户请求{output}，但当前所处阶段为【{nxt_name}】；"
                f"按六阶段顺序先完成本阶段会签才能进入后续阶段。"
            )

        return None, replies.out_of_order_reply(
            head=head,
            reason=reason,
            template=template,
            note=note,
            think_reason=think,
            rule="六阶段严格顺序推进，禁止跨阶段非法流转。",
        )

    def _advance(self, role=None):
        """角色感知推进：当前角色确认自己负责的会签项；全部满足才进入下一阶段。

        返回 (stage, err)：stage 非空表示该阶段前置已全部满足、可执行；err 非空为友好回复。
        """
        role = role or self.current_role
        # 尚未下达审计任务 → 启动立项（自动满足立项前置）
        if not self.ctx.get("task"):
            for c in _stage_conds("initiation"):
                self._satisfy(c)
            self.notify("project_lead", "项目已启动立项，请补全项目信息表单后确认。")
            return "initiation", None

        nxt = self.next_pending()
        if nxt is None:
            return self._final_confirm(role)

        missing = self.preconditions_missing(nxt)
        if not missing:
            return nxt, None  # 闸门已全开，直接进入该阶段
        mine = [c for c in missing if _cond_role(c) == role]
        if not mine:
            return None, self._confirm_hint([], missing, nxt)
        for c in mine:
            self._satisfy(c)
        remaining = self.preconditions_missing(nxt)
        if not remaining:
            return nxt, None
        self._notify_remaining(remaining)
        return None, self._confirm_hint(mine, remaining, nxt)

    def _final_confirm(self, role=None):
        """六阶段产出完成后的最终闸门（归档审批）确认"""
        role = role or self.current_role
        missing = [c for c in FINAL_GATE if not self._condition_satisfied(c)]
        if not missing:
            return None, replies.closed_reply(
                think_reason="六阶段产出全部完成且归档审批已通过，项目正式闭环。",
                rule="归档须人工审批通过后方闭环。",
            )
        mine = [c for c in missing if _cond_role(c) == role]
        if not mine:
            return None, self._confirm_hint([], missing, "archive")
        for c in mine:
            self._satisfy(c)
        remaining = [c for c in FINAL_GATE if not self._condition_satisfied(c)]
        if not remaining:
            return None, replies.closure_reply(
                think_reason="六阶段产出全部完成，归档审批通过，项目正式闭环。",
                rule="归档须人工审批通过后方闭环。",
            )
        self._notify_remaining(remaining)
        return None, self._confirm_hint(mine, remaining, "archive")

    def _confirm_hint(self, confirmed, remaining, stage):
        """构造会签确认的友好回复（已确认 X + 尚待确认 Y + 下一会签项模板）"""
        return replies.confirm_reply(
            confirmed=[_cond_name(c) for c in confirmed],
            remaining=remaining,
            stage=STAGE_NAMES.get(stage, stage),
            project=self._project(),
            think_reason="按六阶段顺序推进：本阶段会签须相关角色依次确认，全部满足后才流转下一阶段。",
            rule="会签项须由对应角色确认；非负责角色确认无效。",
        )

    def _notify_remaining(self, remaining):
        for c in remaining:
            r = _cond_role(c)
            if r:
                self.notify(r, f"待办：请确认会签项「{_cond_name(c)}」。")

    def _rollback(self):
        """人工闸门驳回：回到最近已完成阶段重做（对应回退分支）。返回 (stage, err)。"""
        done = self.completed()
        if done is None:
            return None, replies.wrap(
                "⚠️ 当前尚无已产出草稿，无法回退。请先发起审计任务。",
                "用户请求回退，但项目尚未产出任何阶段草稿，无可回退对象。",
            )
        if done == "initiation":
            return None, replies.wrap(
                "⚠️「审计立项」不设回退分支。\n若立项审批不通过，请输入「终止」中止项目，或重新下达审计任务。",
                "立项阶段无回退分支；立项审批不通过走「终止」分支。",
            )
        idx = STAGE_ORDER.index(done)
        for s in STAGE_ORDER[idx:]:
            for k in _STAGE_CLEAR_KEYS.get(s, []):
                self.ctx.pop(k, None)
        branch = ROLLBACK_BRANCHES.get(done, "")
        stage_name = STAGE_NAMES[done]
        return done, replies.rollback_reply(
            stage=stage_name,
            branch=branch,
            template="继续 / 下一步",
            note="该阶段及之后的产出已作废，请补充材料/证据后重新执行。",
            think_reason=f"人工闸门驳回，回退至【{stage_name}】重做（{branch}）。",
            rule="驳回回退对应阶段，后续产出作废重做。",
        )

    def _fill_form(self, text):
        """补全项目信息表单：解析「字段=值 / 字段：值」，更新 ctx["info_form"]。"""
        form = self.ctx.get("info_form")
        if not form:
            return None, replies.wrap(
                "⚠️ 尚未生成项目信息表单，请先发起审计任务。\n\n"
                f"{replies.SEP}\n{replies.NEW_PROJECT_TEMPLATE}\n{replies.SEP}",
                "用户尝试补全表单，但项目尚未立项生成表单，已引导先发起审计任务。",
            )
        updated = 0
        for seg in re.split(r"[；;，,\n]+", text):
            seg = seg.strip()
            m = re.match(r"^(.+?)\s*[=：:]\s*(.+)$", seg)
            if not m:
                continue
            key, val = m.group(1).strip(), m.group(2).strip()
            for field, aliases in _FIELD_ALIASES.items():
                if key in aliases:
                    form[field] = val
                    updated += 1
                    break
        # 兼容“项目组长是张三，主审是李四”的自然语言补充方式。
        for field, aliases in _FIELD_ALIASES.items():
            alias_pattern = "|".join(map(re.escape, sorted(aliases, key=len, reverse=True)))
            match = re.search(rf"(?:{alias_pattern})\s*(?:是|为)\s*([^，,；;\n]+)", text)
            if match:
                value = match.group(1).strip()
                if value:
                    form[field] = value
                    updated += 1
        if not updated:
            return None, replies.wrap(
                "⚠️ 未识别出要补充的字段。格式示例：迎审人员=张三，业务负责人=李四",
                "用户输入未命中项目表单字段别名，无法识别要补充的字段。",
            )
        # 立项依据走「业务负责人审核需求表」分支，不计入表单补全的必填项
        missing = [f for f in info_form_missing(form) if f != "立项依据"]
        if not missing:
            self._satisfy(("立项信息已填写完整", "project_lead"))
            # 返回 initiation 让 dispatch_silent 重新执行 Agent，统一返回完整表单、思考摘要和溯源结果。
            return "initiation", None
        return None, replies.fill_reply(
            missing=missing,
            think_reason="解析「字段=值」输入后，项目信息表单仍有缺失字段，继续引导补全。",
            rule="立项信息须填写完整方可流转审前准备。",
        )

    # ==================== 意图解析 ====================

    def _next_gate(self):
        """下一个需人工确认的会签项名（用于阶段产出回复的下一步模板）"""
        for c in self.pending_approvals():
            if _cond_role(c):
                return _cond_name(c)
        return None

    def _entry_think(self, stage):
        return {
            "initiation": "解析审计任务并抽取项目信息表单；缺失字段标「待补全」，补全后生成立项申请。",
            "preaudit": "检索《审计实施方案编制规范》生成实施方案、通知书、审前资料清单、调查表单与承诺书。",
            "field": "取数筛查异常 → 法规检索依据 → 风险判断 → 生成底稿、会议议程与实施资料清单。",
            "report": "按审计类型路由报告模板，汇总问题清单并生成报告初稿与征求意见稿。",
            "rectification": "逐问题生成整改措施/责任部门/时限/销号标准，并生成整改成果复核三态。",
            "archive": "汇总整改问题清单、审计建议清单、违规追责建议等全套资料。",
        }.get(stage, "按六阶段流程执行。")

    def entry_reply(self):
        """最近产出阶段的友好回复（主内容 + 思考过程 + 数据溯源）。立项缺项时优先返回补全模板。"""
        if self.ctx.get("fill_hint"):
            return replies.initiation_fill_reply(
                project=self._project(),
                think_reason="已解析审计任务，但项目信息表单存在缺失字段，需项目负责人补全后才能正式立项。",
                rule="立项信息须填写完整（项目负责人确认）后方可流转审前准备。",
            )
        stage = self.completed() or self.next_pending() or "initiation"
        stage_name = STAGE_NAMES.get(stage, stage)
        return replies.stage_entry_reply(
            stage, stage_name, self._project(), self._next_gate(), self._entry_think(stage)
        )

    def detect(self, text):
        """原始阶段识别：仅由后端代码关键词路由；调度层不调用 Agent。"""
        kw = _keyword_stage(text)
        if kw:
            return kw
        return "unknown"

    def resolve(self, text, role=None):
        """意图解析 + 前置条件校验，返回 (stage, err)。

        优先级：
          1) 终止指令（「终止」）→ 记录项目终止
          2) 驳回/回退指令 → 回到本阶段重做
          3) 补全表单指令（「字段=值」）→ 更新项目信息表单
          4) 确认/审批类指令 → 角色感知确认会签项并推进
          5) 明确阶段关键词 → 前置校验后执行
          6) 泛化推进指令 → 推进
          7) LLM 精判兜底 → 同样前置校验
        """
        role = role or self.current_role
        if _is_terminate(text):
            # 项目终止：归档前置「整改成果复核通过（或项目终止）」的备选路径
            self._satisfy("项目终止")
            if self.next_pending() == "archive":
                return "archive", None
            return None, replies.wrap(
                "⚠️ 已记录：本项目终止。归档前置「整改成果复核通过（或项目终止）」已满足，"
                "输入「继续 / 归档」进入项目归档。",
                "用户请求终止项目；记录「项目终止」作为归档前置的备选路径。",
                "项目终止可作为归档前置「整改成果复核通过」的替代条件。",
            )
        if _is_reject(text):
            return self._rollback()
        if self.ctx.get("info_form") and _is_form_completion_ack(text):
            # 用户可能直接编辑了表单后发送“已补充”，此时仍需重新生成后端结果。
            missing = [f for f in info_form_missing(self.ctx.get("info_form")) if f != "立项依据"]
            if not missing:
                self._satisfy(("立项信息已填写完整", "project_lead"))
                return "initiation", None
            return None, replies.fill_reply(
                missing=missing,
                think_reason="已接收表单补充确认，但仍存在未填写字段，继续提示项目负责人补全。",
                rule="立项信息须填写完整方可流转审前准备。",
            )
        # 只有当前会话已经生成立项表单时，才把“字段是值”识别为补表输入；
        # 首次创建指令中的“项目名称是……”必须继续进入立项 Agent。
        if self.ctx.get("info_form") and _is_fill(text):
            return self._fill_form(text)
        if _is_confirm(text):
            return self._advance(role)
        kw = _keyword_stage(text)
        if kw:
            # 首次输入已明确包含“立项/创建项目”意图时，直接启动立项 Agent；
            # 不把正常的任务启动误判为跨阶段操作。
            if kw == "initiation" and not self.ctx.get("task"):
                self._satisfy("审计任务已下达")
                return "initiation", None
            return self._guard(kw)
        if _is_advance(text):
            return self._advance(role)
        stage = self.detect(text)
        if stage in self.agents:
            return self._guard(stage)
        return None, self._unknown(text)

    # ==================== 对外入口 ====================

    def dispatch(self, text, role=None):
        """CLI 入口：返回 (stage, ctx)；出错时打印并返回 (stage, None)"""
        if role:
            self.set_role(role)
        stage, err = self.resolve(text, self.current_role)
        if err:
            print(err)
            return stage, None
        ctx, err2 = self._route(stage, text)
        if err2:
            print(err2)
            return stage, None
        return stage, ctx

    def dispatch_silent(self, text, role=None):
        """Web 入口：返回 (stage, ctx, err)；err 为 None 表示成功"""
        if role:
            self.set_role(role)
        stage, err = self.resolve(text, self.current_role)
        if err:
            return stage, None, err
        ctx, err2 = self._route(stage, text)
        return stage, ctx, err2

    def _route(self, stage, text):
        """执行已通过前置校验的阶段，返回 (ctx, err)。前置校验已由 resolve() 完成。"""
        import time, uuid, hashlib, json
        import db
        started = time.time()
        try:
            if stage == "initiation":
                out = self.agents["initiation"].run(text, self.ctx)
            elif stage == "field":
                out = self.agents["field"].run(text, self.ctx)
            elif stage in ("preaudit", "report", "rectification", "archive"):
                out = self.agents[stage].run(self.ctx)
            else:
                return None, self._unknown(text)
            self.ctx.setdefault("agent_runs", []).append({"agent_id": stage, "trace_id": uuid.uuid4().hex, "status": "success", "duration_ms": round((time.time()-started)*1000)})
            return out, None
        except Exception as exc:
            self.ctx.setdefault("agent_runs", []).append({"agent_id": stage, "trace_id": uuid.uuid4().hex, "status": "error", "error": type(exc).__name__, "duration_ms": round((time.time()-started)*1000)})
            return self.ctx, f"{stage} Agent 执行失败，已保留上下文草稿。请重试或转人工处理（{type(exc).__name__}）。"

    def run_stage(self, stage, task):
        """按阶段名执行单步（供 Web 交互式闸门 / 一键全流程按顺序调用）。

        交互式闸门模式下，上层在闸门确认后调用，此处视为该阶段前置条件已满足。
        """
        for c in STAGE_PRECONDITIONS[stage]:
            self._satisfy(c)
        if stage == "initiation":
            return self.agents["initiation"].run(task, self.ctx)
        if stage == "preaudit":
            return self.agents["preaudit"].run(self.ctx)
        if stage == "field":
            return self.agents["field"].run(task, self.ctx)
        if stage == "report":
            return self.agents["report"].run(self.ctx)
        if stage == "rectification":
            return self.agents["rectification"].run(self.ctx)
        if stage == "archive":
            return self.agents["archive"].run(self.ctx)
        return self.ctx

    def run_pipeline(self, task):
        """一键跑完六阶段（自动模式：视为全部闸门通过，产出标注「待确认」）"""
        ctx = self.ctx
        ctx["task"] = task
        for stage in STAGE_ORDER:
            self.run_stage(stage, task)
            if ctx.get("blocked"):  # 某阶段拦截（如审计对象缺失）→ 中止后续
                break
        return ctx

    def _unknown(self, text=""):
        try:
            # 对问候语和模糊需求使用大模型自然引导，不直接抛出僵硬的阶段错误。
            answer = answer_general_chat(text, STAGE_NAMES.get(self.completed() or "", ""))
            if answer and answer.strip():
                return answer.strip()
        except LLMError:
            pass
        return replies.unknown_reply(
            think_reason="用户输入未命中任何阶段关键词、推进/驳回/补全/确认指令，也未通过 LLM 精判识别出明确阶段。",
            rule="审计按六阶段严格顺序推进，未识别意图时不臆测、不路由。",
        )
