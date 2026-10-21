"""角色层：审计项目多方会签的参与者注册表。

业务流程涉及多个角色之间的会签与通知。单会话通过「角色切换器」在角色间切换，
每个角色只看到并确认自己负责的会签项。此处仅登记角色 → 中文名，不做权限控制
（权限/任命由人工完成，与立项红线一致）。
"""

# 角色 key → 中文名
ROLES = {
    "project_lead":   "项目负责人",
    "business_lead":  "业务负责人",     # 总部/直属单位业务负责人
    "group_lead":     "组长",
    "chief_auditor":  "主审",
    "team_lead":      "小组长",
    "auditor":        "审计人员",
    "respondent":     "迎审人员",       # 被审计单位迎审人员
    "review_panel":   "审理组",
    "rect_dept_lead": "部门整改负责人",
    "biz_dept_lead":  "业务部门负责人",
}

# 默认角色：项目负责人（发起审计任务的人）
DEFAULT_ROLE = "project_lead"


def role_name(role_key):
    return ROLES.get(role_key, role_key)


def is_role(role_key):
    return role_key in ROLES


def notify(ctx, role, text):
    """记录一条系统通知（按角色）。ctx 为项目上下文 dict。

    通知形状：{"role": 角色key, "text": 内容, "read": False}，供 /api/notices 与前端角标消费。
    """
    ctx.setdefault("notices", []).append({"role": role, "text": text, "read": False})
