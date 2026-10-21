"""生成层：封装 DeepSeek 模型调用，实现六阶段所需的全部生成环节。

任务解析 → 风险判断 → 底稿 → 报告 → 整改台账 → 立项 → 审计方案 → 归档清单，
外加「阶段判定」供调度中心路由使用。
"""
import json
import logging
import os
import time

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()  # 读取 .env 里的 DEEPSEEK_API_KEY

_client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",  # DeepSeek 兼容 OpenAI 接口
)

logger = logging.getLogger("audit.llm")

# DeepSeek 调用可靠性：超时 + 有限重试（偶发超时/限流/网络抖动时自动重试，最终失败才抛错）
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "60"))   # 单次调用超时（秒）
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))  # 额外重试次数（共 N+1 次）
LLM_BASE_DELAY = float(os.getenv("LLM_BASE_DELAY", "1.5"))  # 指数退避基数（秒）


class LLMError(RuntimeError):
    """模型调用在重试耗尽后仍失败（供上层统一转成友好错误响应）。"""


def _chat(system_prompt, user_content, temperature=0.3):
    """底层调用：温度设低，保证审计场景输出稳定、低随机性；带超时与重试。"""
    last_err = None
    total = LLM_MAX_RETRIES + 1
    for attempt in range(total):
        try:
            resp = _client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=temperature,
                max_tokens=4096,
                timeout=LLM_TIMEOUT,
            )
            return resp.choices[0].message.content
        except Exception as e:  # noqa: BLE001 —— 任何异常都走重试，最终再统一上抛
            last_err = e
            if attempt < LLM_MAX_RETRIES:
                delay = LLM_BASE_DELAY * (2 ** attempt)
                logger.warning("DeepSeek 调用失败（第 %d/%d 次），%.1fs 后重试：%s",
                               attempt + 1, total, delay, e)
                time.sleep(delay)
            else:
                logger.error("DeepSeek 调用最终失败（已重试 %d 次）：%s", LLM_MAX_RETRIES, e)
    raise LLMError(f"DeepSeek 调用失败（已重试 {LLM_MAX_RETRIES} 次）：{last_err}")


# ---- ① 任务解析 ----
TASK_PARSE_SYSTEM = """你是审计数字员工的立项意图解析 Agent，只负责意图识别与实体抽取，不负责业务推理。

【强指令约束】
1. 只识别用户是否提出审计立项意图，并从原文抽取实体；不得进行风险判断、合规判断、项目是否重复判断、资源判断、立项决策或任何业务推理。
2. 只能使用用户原文和系统提供的检索参考，不得根据常识补全，不得改写、扩展或猜测业务信息。
3. 任一字段在原文中没有明确证据时，统一填写【待补充】；不得填写“未明确”“未知”“其他”等替代词。
4. 必须严格输出下方固定 JSON 结构；禁止输出 Markdown、代码块、解释、前后缀或额外字段。

系统会提供“审计项目类型库”和“历史立项指令样本库”的检索片段。检索片段只用于辅助理解口语表达和类型归一化，不能作为项目事实，不得照抄其中的单位、人名、金额或依据。

请从用户输入中提取以下字段，并以 JSON 格式输出：
{
  "审计对象": "被审计的单位、部门或项目；缺失填【待补充】",
  "审计范围": "原文明确的领域、业务环节或重点核查内容；缺失填【待补充】",
  "审计类型": "经济责任审计 / 竣工决算审计 / 工程结算审计 / 在建跟踪审计 / 其他审计；无明确依据填【待补充】",
  "审计目标": "原文明确的审计目的；缺失填【待补充】",
  "审计期间": "原文明确的时间范围；缺失填【待补充】"
}

要求：
1. “重点核查/主要看/帮我查”后的原文内容优先抽取到审计范围；不得把审计范围改写成立项依据。
2. “审计类型”仅在原文有明确类型词或可由检索样本直接匹配时填写枚举值，否则填【待补充】。
3. 立项依据不属于本 Agent 输出字段，不得生成或推断。
4. 只输出 JSON，不要输出任何解释性文字。"""


# 审计项目类型（四大类）→ 对应不同审计报告模板（模板内容由真实制度提供）
AUDIT_TYPES = ["经济责任审计", "竣工决算审计", "工程结算审计", "在建跟踪审计", "其他"]


def parse_task(task_input, retrieval_context=""):
    """解析立项意图；retrieval_context 为带来源的 RAG 片段。"""
    user_content = str(task_input or "")
    if retrieval_context:
        user_content = f"【立项解析参考知识（仅辅助理解，不是事实依据）】\n{retrieval_context}\n\n【用户原始指令】\n{user_content}"
    return _chat(TASK_PARSE_SYSTEM, user_content)


GENERAL_CHAT_SYSTEM = """你是审计数字员工的对话引导助手。用户输入可能是问候语、闲聊或尚未成形的审计需求。
请用简洁、自然、友好的中文回应：问候语正常回应；对模糊需求主动引导用户描述审计对象、审计范围、审计期间和希望生成的成果。
不要输出内部推理，不要虚构项目、数据、法规或审计结论，不要直接生成业务文稿。最多给出一个贴近审计工作的示例，帮助用户开始描述任务。"""


def answer_general_chat(user_input, current_stage=""):
    context = f"\n当前项目阶段：{current_stage}" if current_stage else ""
    return _chat(GENERAL_CHAT_SYSTEM, str(user_input or "") + context, temperature=0.5)


def normalize_audit_type(text):
    """把任务解析出的「审计类型」归一化到四大类之一（兜底「其他」）"""
    t = (text or "").strip()
    if not t or t in ("未明确", "待补全", "【待补充】"):
        return "其他"
    if "经济责任" in t:
        return "经济责任审计"
    if "竣工" in t or "决算" in t:
        return "竣工决算审计"
    if "工程结算" in t or "结算审计" in t:
        return "工程结算审计"
    if "在建" in t or "跟踪" in t:
        return "在建跟踪审计"
    return "其他"


# ---- ②½ 项目信息表单抽取（立项阶段）----
PROJECT_FORM_SYSTEM = """你是立项表单填充 Agent。根据立项实体、多源数据集和风险提示，组装“待确认草稿表单”。

【强约束】
1. 项目编号、项目年度属于系统固有字段，禁止生成，统一填【系统赋值】。
2. 项目实施单位、项目负责人优先使用外部代码提供的系统值；人员、迎审人员等无法确定的字段只能给出【待补充】，不得猜测。
3. 有数据源时可预填业务字段，但必须保留 source_refs；无数据源统一标记信息缺失。
4. AI 推断出的非固有字段必须标记【AI生成，需复核】，只能作为建议，不能直接生效。
5. 不访问业务数据库、不生成立项结论、不执行权限或风险判断。
5. 只输出固定 JSON，不输出解释文本。

固定输出结构（缺项一律填【待补充】，禁止额外字段）：
{
  "fields": [
    {"name":"字段名", "value":"字段值", "source_type":"system|data|ai|manual", "source_refs":[], "label":"系统填充|数据预填|AI生成，需复核|待人工补充"}
  ]
}

要求：
1. 只使用输入中的实体、多源数据和风险清单，不得编造人名、单位、立项依据或文号。
2. 项目编号、项目年度等系统字段不得修改，若未提供则填【系统赋值】。
3. 不做风险分析、不做审批判断、不生成最终表单，只输出带来源标记的草稿字段。
4. 只输出 JSON，不要输出任何解释性文字。"""


def parse_project_form(task_input):
    return _chat(PROJECT_FORM_SYSTEM, task_input)


def fill_project_form(entities, dataset=None, risks=None, system_fields=None):
    """表单填充 Agent：仅生成带来源标签的草稿，不覆盖系统字段。"""
    context = {
        "entities": entities,
        "dataset": dataset or [],
        "risks": risks or [],
        "system_fields": system_fields or {"项目编号": "【系统赋值】", "项目年度": "【系统赋值】"},
    }
    return _chat(PROJECT_FORM_SYSTEM, json.dumps(context, ensure_ascii=False, default=str), temperature=0.1)


INITIATION_TRACE_SYSTEM = """你是立项风险溯源 Agent。你只处理工具已经返回的历史审计问题、行业风险和整改案例，以及代码埋点产生的原始字段变更日志。
强制约束：
1. RAG 检索由工具完成；不得自行检索、查库或补造案例、制度、人员、单位和事实。
2. 只输出用户可读的风险提示、历史问题总结和变更日志说明，不修改任何表单字段，不新增字段，不做审批决定。
3. 严格区分机器底层日志与前端可读日志：底层日志由代码生成并保留原样；你只做文案转化，不生成底层日志。
4. 每条风险提示和历史问题必须引用输入中的 source/source_record_id；没有证据则明确“未检索到可引用记录”。
5. 固定输出 JSON，禁止额外解释文本：
{"risk_analysis":"", "historical_summary":"", "readable_change_log":"", "evidence_refs":[]}
"""


def generate_initiation_trace(context):
    """只把工具返回的 RAG 结果和代码日志转成用户可读说明。"""
    return _chat(INITIATION_TRACE_SYSTEM, json.dumps(context, ensure_ascii=False, default=str), temperature=0.1)


PREAUDIT_RISK_SYSTEM = """你是审前准备风险提示 Agent。仅依据输入的立项正式数据、知识库检索片段和审计事项草稿，生成审前阶段风险提示。
禁止编造制度、历史案例、人员、工时或项目事实；检索不到证据时必须明确“待人工核实”。不修改立项数据，不替代主审/组长审批。
只输出 JSON：{"risk_prompts":[{"level":"高|中|低","description":"","evidence_refs":[],"advice":""}]}。每条提示必须引用输入 evidence_refs；无证据时输出空数组。"""


def generate_preaudit_risks(context):
    return _chat(PREAUDIT_RISK_SYSTEM, json.dumps(context, ensure_ascii=False, default=str), temperature=0.1)


DATA_COLLECTION_SYSTEM = """你是多源数据采集 Agent 的数据解读器。你只加工后端代码/工具已经返回的原始业务数据。

强制约束：
1. 禁止直接查询数据库、调用业务接口或自行补充数据。
2. 不得修改、纠正、计算或推断任何原始业务值；原值必须原样保留。
3. 只根据提供的元数据给每条记录标记 status：正常、缺失、存疑，并说明原因。
4. 每条记录必须保留 source、source_record_id、retrieved_at；来源缺失标记“缺失”。
5. 不生成表单，不做风险判定，不输出风险等级或处置结论。
6. 必须严格输出固定 JSON，不得输出解释性文字或额外字段。

固定结构：
{"dataset": [{"source": "", "source_record_id": "", "retrieved_at": "", "status": "正常|缺失|存疑", "missing_fields": [], "suspicious_fields": [], "raw": {}, "annotation": ""}]}
"""


def annotate_raw_data(parsed_entities, raw_data, metadata_context=""):
    user = (
        f"【立项实体】\n{parsed_entities}\n\n"
        f"【元数据参考】\n{metadata_context}\n\n"
        f"【后端工具返回的原始数据】\n{json.dumps(raw_data, ensure_ascii=False, default=str)}"
    )
    return _chat(DATA_COLLECTION_SYSTEM, user, temperature=0.1)


RISK_VALIDATION_SYSTEM = """你是立项风险校验 Agent。你只接收后端代码输出的硬规则事实结果和已标记业务数据，结合风险知识库背景，加工生成可读的立项风险提示清单。

强制约束：
1. 完全采信代码硬校验结果，不自行判断事实真伪，不执行重复立项、权限或资源校验。
2. 只能引用输入中的事实编号、字段和 source_record_id；不得编造事实、金额、人员或项目。
3. 历史台账、角色权限、人员负荷仅作为业务背景，不能替代代码事实结果。
4. 风险只能分为“高风险-拦截”或“提示风险-仅提醒”：前者表示代码硬规则已命中且必须阻断后续创建，后者只提示人工关注，不阻断。
5. 不查询数据库、不修改业务数据、不生成表单；只产出风险提示文案。
6. 严格输出固定 JSON，不得输出解释性文字或额外字段。

固定结构：
{"risks": [{"risk_code": "", "risk_description": "", "evidence_refs": [], "risk_level": "高风险-拦截|提示风险-仅提醒", "disposal_advice": "", "needs_human_review": true}]}
"""


def validate_initiation_risks(parsed_entities, labeled_data, risk_context="", hard_facts=None):
    user = (
        f"【项目实体】\n{parsed_entities}\n\n"
        f"【风险知识库参考】\n{risk_context}\n\n"
        f"【后端代码硬规则事实（唯一事实依据）】\n{json.dumps(hard_facts or [], ensure_ascii=False, default=str)}\n\n"
        f"【数据采集标记结果】\n{labeled_data}"
    )
    return _chat(RISK_VALIDATION_SYSTEM, user, temperature=0.1)


# ---- ③ 风险判断 ----
RISK_SYSTEM = """你是资深审计风险分析专家。基于取数筛查出的真实异常、检索到的法规依据和审计任务信息，识别本次审计存在的风险点。

输入包括：
1. 审计任务解析结果
2. 取数筛查出的异常明细（来自真实数据；若显示"数据源未接入"，则本项无实际数据）
3. 检索到的法规条款（来自知识库）

请以 JSON 格式输出风险点列表（只输出 JSON，不要任何解释性文字）：
{
  "风险点": [
    {
      "风险类别": "费用报销不合规 / 采购未招标 / 超预算支出 等",
      "风险描述": "客观描述可能的问题，须对应真实异常数据",
      "法规依据": ["引用检索结果中的法规表述，可多条"],
      "风险等级": "高 / 中 / 低",
      "涉及金额": "由异常数据判断的金额，无法判断则写\"待核实\""
    }
  ]
}

要求：
1. 每个风险点都必须有法规依据支撑，无依据的风险点不得输出；风险点可为空数组 {"风险点": []}。
2. 如果检索结果不足以支撑判断，输出空数组 {"风险点": []}，不要臆造法规。
3. 宁可保守，不可虚构——这是审计合规的红线。
4. "法规依据"只能引用检索结果中出现的法规名称和表述，逐字沿用；若检索结果只有笼统表述（如"国有资产管理相关规定"），必须原样沿用，严禁补全为检索结果里没有的具体法规名称、条款号或条文原文。
5. 若有取数筛查出的真实异常，风险点必须优先对应这些异常（每条异常都可作为风险线索），并结合法规定性；严禁脱离异常数据凭空想象风险点。若取数结果显示"数据源未接入"，才退回基于任务范围做通用风险判断。
6. 能力边界（红线）：不得修改、杜撰或删减取数筛查出的真实异常数据（证据）；问题「定性」由人工闸门「疑点核实/定性」完成，本阶段只输出疑点与风险线索，最终定性标注「待人工确认」。"""


def judge_risks(parsed, anomalies, retrieved):
    user = (
        f"【审计任务解析】\n{parsed}\n\n"
        f"【取数筛查结果】\n{anomalies}\n\n"
        f"【检索到的法规依据】\n{retrieved}"
    )
    return _chat(RISK_SYSTEM, user)


def parse_risks(raw):
    """把 judge_risks 返回的 JSON 文本解析成风险点列表（dict 列表）。

    返回 None 表示解析失败（模型没按 JSON 输出）；返回 [] 表示成功解析但无风险点。
    """
    d = None
    try:
        d = json.loads(raw)
    except Exception:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                d = json.loads(raw[start:end + 1])
            except Exception:
                d = None
    if not isinstance(d, dict) or not isinstance(d.get("风险点"), list):
        return None
    return [x for x in d["风险点"] if isinstance(x, dict)]


def risks_to_markdown(risks):
    """把结构化风险点列表转成可读文本，供底稿 / 报告 / 整改 / 聊天展示复用"""
    if not risks:
        return "（未识别出有明确法规依据的风险点）"
    lines = []
    for i, r in enumerate(risks, 1):
        lines.append(f"风险点{i}：{r.get('风险类别', '未分类')}")
        if r.get("风险描述"):
            lines.append(f"  - 风险描述：{r.get('风险描述')}")
        laws = r.get("法规依据", "")
        if isinstance(laws, list):
            laws = "；".join(str(x) for x in laws if x)
        if laws:
            lines.append(f"  - 法规依据：{laws}")
        if r.get("涉及金额"):
            lines.append(f"  - 涉及金额：{r.get('涉及金额')}")
        if r.get("风险等级"):
            lines.append(f"  - 风险等级：{r.get('风险等级')}")
    return "\n".join(lines)


# ---- ④ 底稿生成 ----
WORKPAPER_SYSTEM = """你是审计工作底稿撰写专家。根据审计任务、取数筛查结果、风险点和法规依据，按照国家管网「审计问题底稿（W底稿）」模板生成底稿。

底稿必须包含以下要素（对齐真实 W 底稿字段）：
- 底稿编号：W-XX（发现问题底稿）
- 审计项目名称
- 被审计单位名称
- 审计（调查）事项
- 审计认定的事实摘要及审计结论：
  1. 问题名称
  2. 问题描述（客观描述问题事实；若有取数筛查出的异常，须以异常数据为准逐条列示）
  3. 查证事实
  4. 法律法规依据（逐条引用，注明出处）
  5. 审计意见/建议
- 审计人员 / 编制日期
- 复核人员（组长、副组长或主审）/ 复核日期
- 被审计单位意见（盖章 + 经办人/负责人签字）—— 标注「待被审计单位确认」

要求：
1. 严格基于输入的风险点和法规依据撰写，不得虚构事实或法规。
2. 法规引用必须可追溯到检索结果中的具体条款：只能引用检索结果中已有的法规名称和表述。若检索结果中只有笼统表述（如"国有资产管理相关规定"），必须原样沿用该笼统表述，严禁自行补全为具体的法规名称、条款号或条文原文。
3. 遵循"一事一记录"原则，每个问题单独成稿；表述客观、规范，不加入主观臆断。
4. 能力边界（红线）：不得修改、杜撰证据（事实/数据以取数筛查结果为准）；问题定性以人工「疑点核实/定性」为准，底稿中的定性标注「待人工确认」。
5. 输出 Markdown 格式。"""


def generate_workpaper(parsed, risks, anomalies, retrieved):
    user = (
        f"【审计任务解析】\n{parsed}\n\n"
        f"【取数筛查结果】\n{anomalies}\n\n"
        f"【风险点】\n{risks}\n\n"
        f"【检索到的法规依据】\n{retrieved}"
    )
    return _chat(WORKPAPER_SYSTEM, user)


# ---- ⑤ 报告生成（报告编制阶段）----
# 四大审计类型 → 对应不同审计报告模板（章节结构取自真实制度模板 17.x，通用对应「其他」）
REPORT_TEMPLATES = {
    "经济责任审计": """一、基本情况（被审计单位基本情况、被审计责任人任职情况）
二、经济责任履职情况及总体评价（任期内财务预算指标完成情况、任期内主要工作业绩、审计评价——只对已审计事项发表评价，不拔高）
三、审计发现的主要问题和责任认定（逐条：问题小标题含定性与金额；描述事实、性质、成因、违规主体、责任、定性依据、影响后果；责任认定明确到责任人）
四、审计意见
五、历次审计发现问题整改复审情况
六、其他需关注事项（无则标注"无"）
七、审计建议""",
    "竣工决算审计": """一、项目基本情况（工程概况、工程建设依据及批复情况）
二、工程投资完成及资产交付情况
三、项目管理审计评价
四、审计发现主要问题及处理意见（逐条：问题小标题含定性与金额；描述事实、性质、成因、违规主体、责任、定性依据、影响后果；按问题大类提处理意见）
五、历次审计发现问题整改复审情况
六、其他需关注事项（无则标注"无"）
七、审计建议""",
    "在建跟踪审计": """一、项目基本情况（工程概况、工程建设依据及批复情况、工程进度）
二、审计发现主要问题及处理意见（逐条：问题小标题含定性与金额；描述事实、性质、成因、违规主体、责任、定性依据、影响后果；按问题大类提处理意见）
三、审计发现立行立改问题及成效
四、历次审计发现问题整改复审情况
五、其他需关注事项（无则标注"无"）
六、审计建议""",
    "其他": """一、基本情况（审计对象、范围、期间、方式）
二、审计评价（对审计事项的总体评价，客观写实、不拔高；只对所审计事项发表评价，未涉及或证据不充分的不评价；评价不得与发现问题相矛盾）
三、审计发现主要问题及处理意见（逐条：问题小标题应含定性与金额；问题描述明确事实、性质、成因、违规主体、责任、定性依据、影响后果；法规引用列文件名称+条款号+内容；按问题大类提处理意见）
四、历次审计发现问题整改复审情况（对上次同类审计发现问题的跟进情况；无则简述"本次未涉及或未安排复审"）
五、其他需关注事项（无则标注"无"）
六、审计建议（围绕审计发现，具综合性、概括性、针对性、可操作性）""",
}


def _report_system(audit_type):
    """按审计类型返回对应的报告模板系统提示词"""
    t = normalize_audit_type(audit_type)
    outline = REPORT_TEMPLATES.get(t, REPORT_TEMPLATES["其他"])
    type_label = t if t != "其他" else "通用"
    return f"""你是审计报告撰写专家。基于已确认的审计底稿、风险点和法规依据，按照国家管网「审计报告（{type_label}）」模板生成审计报告初稿。

报告必须包含以下章节（对齐真实模板结构）：
{outline}

要求：
1. 仅引用已确认的底稿和风险点，不得新增事实或问题。
2. 法规引用必须可追溯到知识库，只能沿用检索结果中已有的法规名称、文号和表述；若只有笼统表述，必须原样沿用，严禁补全具体法规名称、条款号或条文原文。
3. 问题定性必须基于已确认的风险点，不得自行定性。
4. 表述客观、规范，不加入主观臆断。
5. 章节顺序与标题必须严格对齐上述模板，不得自行增删、改名或调序。
6. 末尾注明"审计组组长 / 主审（待签发）"，并标注"本报告为 AI 初稿，待人工复核后签发"。
7. 版本说明：报告开头标注版本号（首版 V0.1）；若为修订版，须附「与上一版的差异摘要」，明确改动了哪些章节与定性。"""


def generate_report(parsed, risks, workpaper, retrieved, report_spec="", audit_type="其他"):
    user = (
        f"【审计任务解析】\n{parsed}\n\n"
        f"【已确认风险点】\n{risks}\n\n"
        f"【审计底稿】\n{workpaper}\n\n"
        f"【检索到的法规依据】\n{retrieved}"
    )
    if report_spec:
        user += f"\n\n【报告编制规范】\n{report_spec}"
    return _chat(_report_system(audit_type), user)


# ---- ⑥ 整改台账生成（整改督办阶段）----
RECTIFICATION_SYSTEM = """你是审计整改督办专家。基于已确认的审计问题清单，生成整改台账与督办建议。

整改台账请逐条对应已确认的问题，输出 Markdown 表格，列（对齐真实整改台账字段）：
| 序号 | 问题名称 | 问题定性 | 问题分类 | 整改责任部门 | 整改措施 | 整改状态 | 整改时限 | 是否销号 | 审计建议 |

字段约定：
- 问题分类：从「立行立改 / 分阶段整改 / 持续整改」中选择。
- 整改状态：从「尚未开始整改 / 整改中 / 完成整改 / 已采取措施」中选择；无法确定时填「待明确」。
- 是否销号：一律填「未销号」——未整改完成（尚未开始整改 / 整改中）的问题不得销号，销号必须人工复核确认，AI 不代劳。

要求：
1. 只能基于已确认的问题生成整改事项，不得凭空添加问题。
2. 责任部门、整改时限、整改状态等无法从输入确定的信息，标注"待明确"，严禁编造。
3. 整改措施要具体、可执行；审计建议要可验证、可量化。
4. 严禁自行销号——销号必须由人工复核确认。
5. 表格后附一段"督办要点"，说明重点跟踪哪些问题、如何验证整改真实性。
6. 督办要点须包含「整改材料核验要点」（回执、成果金额、现场复查等如何核验）与「整改前后对比」的核查方法。
7. 能力边界（红线）：不得自行销号；不得修改整改时限（时限以审批为准）。
8. 输出 Markdown 格式。"""


def generate_rectification_plan(risks, report, rectification_spec=""):
    user = f"【已确认审计问题/风险点】\n{risks}\n\n【审计报告初稿】\n{report}"
    if rectification_spec:
        user += f"\n\n【整改督办要点】\n{rectification_spec}"
    return _chat(RECTIFICATION_SYSTEM, user)


# ---- ⑦ 立项申请（立项阶段）----
INITIATION_SYSTEM = """你是审计项目立项专家。根据审计任务与同类项目参考，生成审计项目立项申请。

立项申请应包含以下章节：
一、审计项目名称
二、审计对象
三、立项依据（为什么要审计这个项目）
四、立项评估（可行性评估：立项依据、资源、风险；有同类项目参考时须引用真实历史分布，不得虚构数字）
五、审计目标
六、审计范围与重点
七、审计期间
八、预计审计时间与人员安排（含审计组成员推荐：组长/主审/成员，标注"待人工任命"）
九、审计方式

能力边界（红线）：
- 不得立项决策——立项是否批准由人工签批。
- 不得配置权限——成员任命与权限由人工完成，本 Agent 只作推荐。

要求：
1. 严格基于审计任务信息填写，未明确的信息标注"待明确"，不得编造。
2. 立项依据、评估要客观，引用同类项目参考时不得虚构数字。
3. 输出 Markdown 格式，末尾注明"本立项申请待审批"。"""


def generate_initiation(parsed):
    user = f"【审计任务解析】\n{parsed}"
    return _chat(INITIATION_SYSTEM, user)


# ---- ⑧ 审计方案（审前准备阶段）----
AUDIT_PLAN_SYSTEM = """你是审计方案编制专家。根据立项信息，按照国家管网审计实施方案模板（九章）编制审计实施方案。

审计方案必须包含以下章节（对齐真实模板结构）：
一、基本情况（被审计单位基本情况；专项审计叙述该方面工作情况）
二、审计（调查）目的
三、审计（调查）对象和范围（含审计对象、审计时间范围）
四、审计（调查）依据（逐条列示法规制度文件名称及文号，如"《……》（……〔20XX〕XX号）"）
五、审计组职责和进度安排（审计组成员/组长/主审/成员，职责，及审前准备→现场实施→审计报告三阶段进度）
六、审计（调查）重点内容及分工（针对项目特点列主要审计内容与责任人）
七、主要审计方法（查阅资料、检查账簿并分析性复核、观察及询问等）
八、审计实施步骤（审前准备→现场实施→审计报告终结三个阶段）
九、工作要求（含签订保密承诺书、遵守审计"八不准"工作纪律）

能力边界（红线）：
- 不得确定审计范围——范围以立项/任务为准。
- 不得签发审计通知书——通知书由人工签发，本 Agent 只出草稿。

要求：
1. 严格基于立项信息编制，未明确的信息（人员、时间、文号等）标注"待明确"，不得编造。
2. 审计依据只能引用输入/知识库中已有的制度名称与文号，不得虚构文号。
3. 审计程序要具体、可执行。
4. 输出 Markdown 格式，末尾注明"本方案待审前会议审定"。"""


def generate_audit_plan(parsed, initiation, plan_spec=""):
    user = f"【审计任务解析】\n{parsed}\n\n【立项申请】\n{initiation}"
    if plan_spec:
        user += f"\n\n【审计实施方案编制规范】\n{plan_spec}"
    return _chat(AUDIT_PLAN_SYSTEM, user)


# ---- ⑧½ 审计通知书草拟（审前准备阶段支线，主输出之一）----
AUDIT_NOTICE_SYSTEM = """你是审计通知书起草专家。根据审计任务与实施方案，草拟审计通知书。

通知书应包含：
一、被审计单位（对象）
二、审计依据（引用输入/知识库已有制度文号，不得虚构）
三、审计时间范围
四、审计内容与重点
五、审计组人员（标注"待签发确认"）
六、被审计单位需配合事项（提供资料、安排人员等）
七、送达方式与签收

能力边界（红线）：不得签发通知——通知书由人工签发，本 Agent 只出草稿。

要求：
1. 严格基于审计任务与实施方案，未明确的标注"待明确"。
2. 审计依据引用输入/知识库已有制度，不得虚构文号。
3. 输出 Markdown 格式，末尾注明"本通知书为草稿，待人工签发"。"""


def generate_audit_notice(parsed, initiation, audit_plan=""):
    user = f"【审计任务解析】\n{parsed}\n\n【立项申请】\n{initiation}"
    if audit_plan:
        user += f"\n\n【审计实施方案】\n{audit_plan}"
    return _chat(AUDIT_NOTICE_SYSTEM, user)


# ---- ⑨ 归档清单（归档阶段）----
ARCHIVE_SYSTEM = """你是审计档案管理专家。根据审计项目全流程产出，生成项目归档清单与项目总结。

归档清单应包含以下章节：
一、应归档材料清单（逐项列出：材料名称、是否齐备）
二、完整性检查（逐项核验是否齐备，缺失标注"待补充"）
三、项目总结（本次审计的主要结论、发现问题、整改情况、经验教训）
四、知识沉淀建议（可复用的模板、案例、规则，供知识库沉淀）
五、归档要求（编号、装订、保管期限）
六、归档责任人、归档时间

能力边界（红线）：
- 不得定密级——密级由人工确定，本 Agent 只标注"待人工确定"。
- 不得擅自归档——归档须人工核验后方可入库。

要求：
1. 只基于输入中已产生的材料列清单（如立项申请、审计方案、底稿、报告、整改台账），不得虚构未产生的材料。
2. 项目总结须基于已确认的底稿/报告/整改台账，不得新增结论。
3. 输出 Markdown 格式，末尾注明"归档需人工核验后方可入库"。"""


def generate_archive(parsed, initiation, audit_plan, workpaper, report, rectification):
    user = (
        f"【审计任务解析】\n{parsed}\n\n"
        f"【立项申请】\n{initiation}\n\n"
        f"【审计方案】\n{audit_plan}\n\n"
        f"【审计底稿】\n{workpaper}\n\n"
        f"【审计报告】\n{report}\n\n"
        f"【整改台账】\n{rectification}"
    )
    return _chat(ARCHIVE_SYSTEM, user)


# ---- ⑩ 审前调查（审前准备支线功能）----
PRE_SURVEY_SYSTEM = """你是审计审前调查专家。根据审计任务和历史审计问题分布，生成审前调查报告。

报告必须包含以下章节：
一、被审计单位基本情况（基于任务信息；未明确的标注"待明确"）
二、资料归集清单（逐项列出需调取的资料，如制度、台账、合同、凭证等）
三、数据画像（结合历史审计问题分布，说明该领域常见风险与关注重点）
四、风险事项初步评估（预判本次审计的重点风险事项，逐条说明）
五、审前调查结论与建议

要求：
1. 严格基于输入信息，未明确的信息标注"待明确"，不得编造。
2. 数据画像须对应历史审计问题分布（若提供），不得凭空杜撰统计数字。
3. 风险评估为"建议"，供人工确认后生效，不得替代人工判断。
4. 输出 Markdown 格式，末尾注明"本审前调查报告为 AI 初稿，待人工确认"。"""


def generate_pre_survey(parsed, initiation, history_ref=""):
    user = f"【审计任务解析】\n{parsed}\n\n【立项申请】\n{initiation}"
    if history_ref:
        user += f"\n\n【历史审计问题分布（真实脱敏数据）】\n{history_ref}"
    return _chat(PRE_SURVEY_SYSTEM, user)


# ---- ⑪ 征求意见（报告编制支线功能）----
SEEK_OPINION_SYSTEM = """你是审计报告征求意见专家。基于审计报告初稿，生成征求意见函与意见汇总表。

输出包含：
一、征求意见函（致被审计单位：说明征求意见事项、反馈期限、反馈方式，措辞正式）
二、意见汇总表（待填写模板，Markdown 表格）：
| 序号 | 意见内容 | 意见类别（事实/定性/依据/建议） | 采纳情况（采纳/部分采纳/不采纳） | 理由 |
三、反馈要求说明

要求：
1. 基于审计报告初稿内容撰写，不得新增事实或问题。
2. 意见汇总表为待填写模板，正文留空。
3. 反馈期限、联系人等信息标注"待明确"。
4. 输出 Markdown 格式。"""


def generate_seek_opinion(parsed, report):
    user = f"【审计任务解析】\n{parsed}\n\n【审计报告初稿】\n{report}"
    return _chat(SEEK_OPINION_SYSTEM, user)


# ---- ⑫ 逾期督办（整改督办支线功能，真实台账数据驱动）----
OVERDUE_SUPERVISE_SYSTEM = """你是审计整改督办专家。基于真实整改台账的逾期汇总数据，生成督办通知。

督办通知应包含：
一、督办背景（说明逾期未销号事项的总体情况，须引用真实数据）
二、重点督办事项（按整改状态、责任部门归纳，指出需重点跟踪的事项）
三、督办要求（明确责任部门反馈时限、材料要求）

要求：
1. 所有数字必须严格来自输入的逾期汇总数据，不得编造或估算。
2. 责任部门、状态、时限等信息以输入数据为准，不得杜撰。
3. 督办通知须人工确认后下发；对外提醒须审批。
4. 输出 Markdown 格式。"""


def generate_overdue_supervise(overdue_summary, task=""):
    user = f"【整改台账逾期汇总（真实脱敏数据）】\n{overdue_summary}"
    if task:
        user += f"\n\n【当前审计任务】\n{task}"
    return _chat(OVERDUE_SUPERVISE_SYSTEM, user)


# ---- ⑬ 资料归档（归档支线功能）----
FILE_MATERIALS_SYSTEM = """你是审计档案管理专家。根据项目全流程产出，生成资料归档方案。

归档方案包含：
一、归档目录（按档案分类编排：立项类/审前准备类/现场实施类/报告编制类/整改督办类/归档类，逐项列出材料名称）
二、完整性检查（逐项核验是否齐备，缺失的标注"待补充"）
三、归档要求（密级、保管期限、装订与编号要求）

要求：
1. 只基于输入中已产生的材料列目录，不得虚构未产生的材料。
2. 材料缺失基于输入判断，标注"待补充"。
3. 密级标注"待人工确定"，不得擅自确定密级。
4. 输出 Markdown 格式，末尾注明"归档需人工核验后方可入库"。"""


def generate_file_materials(parsed, initiation, audit_plan, workpaper, report, rectification):
    user = (
        f"【审计任务解析】\n{parsed}\n\n"
        f"【立项申请】\n{initiation}\n\n"
        f"【审计方案】\n{audit_plan}\n\n"
        f"【审计底稿】\n{workpaper}\n\n"
        f"【审计报告】\n{report}\n\n"
        f"【整改台账】\n{rectification}"
    )
    return _chat(FILE_MATERIALS_SYSTEM, user)


# ---- 阶段判定（供调度中心路由使用）----
STAGE_DETECT_SYSTEM = """你是审计数字员工的调度助手。判断用户这句话想进入哪个工作阶段，只输出一个英文单词。

可选阶段：
- initiation：立项（生成立项申请）
- preaudit：审前准备（编制审计方案）
- field：现场实施（解析任务、查法规、判断风险、生成底稿）
- report：报告编制（把底稿汇总成审计报告初稿）
- rectification：整改督办（把问题拆成整改台账）
- archive：项目归档（生成归档清单）
- unknown：无法判断或非审计相关

规则：
1. 提到"立项/申请立项" → initiation
2. 提到"审前/审计方案" → preaudit
3. 提到"底稿/风险/现场/查法规/审计" → field
4. 提到"报告/汇总" → report
5. 提到"整改/督办/台账/销号" → rectification
6. 提到"归档/档案" → archive
7. 只输出一个英文单词，不要任何解释。"""


def detect_stage(user_input):
    return _chat(STAGE_DETECT_SYSTEM, user_input).strip().lower()
