-- ============================================================================
-- 审计数字员工 · 数据库建表 SQL（MySQL 8.0）
-- ============================================================================
-- 说明：
--   1. 引擎 InnoDB，字符集 utf8mb4（utf8mb4_unicode_ci），支持中文与 emoji。
--   2. 每张表均带审计字段：created_at / updated_at / created_by / updated_by / is_deleted。
--   3. 角色（10 个）统一用 role_key 表达，见 sys_role 角色字典；项目成员、审核记录均引用。
--   4. 六阶段阶段键：initiation 项目立项 / preaudit 审前准备 / field 现场实施
--                    report 报告编制 / rectification 整改督办 / archive 项目归档
--   5. 「状态流转 + 角色 + 审核状态」贯穿：主表 current_stage / project_status，
--      各业务表自带审批字段（审核人 role_key + 审核状态 + 审核时间 + 意见），
--      并由 approval_record 统一沉淀每一次会签/审核动作，形成完整审计轨迹。
--   6. 脱敏红线：问题库 / 整改台账为历史真实脱敏数据，敏感金额字段已脱敏，
--      此处字段与 CSV 表头一一对应，不新增臆造字段。
-- ============================================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ---------------------------------------------------------------------------
-- 0) 角色字典（配套表）
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS sys_role;
CREATE TABLE sys_role (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    role_key      VARCHAR(32)     NOT NULL COMMENT '角色键（项目负责人/业务负责人/组长/主审/小组长/审计人员/迎审人员/审理组/部门整改负责人/业务部门负责人）',
    role_name     VARCHAR(64)     NOT NULL COMMENT '角色中文名',
    role_desc     VARCHAR(255)    NULL     COMMENT '角色职责说明',
    is_enabled    TINYINT(1)      NOT NULL DEFAULT 1 COMMENT '是否启用 1=是 0=否',
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_role_key (role_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='角色字典（10 个多方会签角色）';

-- ---------------------------------------------------------------------------
-- 1) 审计项目主表
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS audit_project;
CREATE TABLE audit_project (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    project_id      CHAR(32)        NOT NULL COMMENT '项目业务ID（UUID，前端会话/隔离用）',
    project_no      VARCHAR(64)     NULL     COMMENT '项目文号（如 SJ-2026-001）',
    project_name    VARCHAR(255)    NOT NULL COMMENT '项目名称',
    audit_type      VARCHAR(32)     NULL     COMMENT '审计类型（专项审计/经济责任审计/内控审计/其他）',
    audit_scope     VARCHAR(1000)   NULL     COMMENT '审计范围',
    audit_object    VARCHAR(255)    NULL     COMMENT '审计对象 / 被审计单位',
    audit_year      VARCHAR(8)      NULL     COMMENT '审计年度（如 2026）',
    init_unit       VARCHAR(255)    NULL     COMMENT '立项单位',
    exec_unit       VARCHAR(255)    NULL     COMMENT '实施单位',
    current_stage   VARCHAR(32)     NOT NULL DEFAULT 'initiation' COMMENT '当前阶段：initiation/preaudit/field/report/rectification/archive/closed',
    project_status  VARCHAR(32)     NOT NULL DEFAULT 'draft' COMMENT '项目状态：draft 草稿 / running 进行中 / closed 已完结 / archived 已归档 / terminated 已终止',
    stage_progress  TINYINT         NOT NULL DEFAULT 0 COMMENT '阶段进度百分比 0-100',
    start_date      DATE            NULL     COMMENT '计划开始日期',
    end_date        DATE            NULL     COMMENT '计划结束日期',
    budget          DECIMAL(18,2)   NULL     COMMENT '预算金额（元）',
    secrecy_level   VARCHAR(16)     NULL     COMMENT '密级（公开/内部/秘密/机密）',
    lead_user_id    VARCHAR(64)     NULL     COMMENT '项目负责人（用户ID，可冗余自 project_member）',
    archived_at     DATETIME        NULL     COMMENT '归档时间（六阶段闭环 + 归档审批通过）',
    created_by      VARCHAR(64)     NULL     COMMENT '创建人',
    updated_by      VARCHAR(64)     NULL     COMMENT '最后更新人',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted      TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '逻辑删除 1=已删',
    PRIMARY KEY (id),
    UNIQUE KEY uk_project_id (project_id),
    KEY idx_project_no (project_no),
    KEY idx_current_stage (current_stage),
    KEY idx_project_status (project_status),
    KEY idx_audit_object (audit_object)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='审计项目主表';

-- ---------------------------------------------------------------------------
-- 2) 项目成员
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS project_member;
CREATE TABLE project_member (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    project_id    CHAR(32)        NOT NULL COMMENT '所属项目业务ID',
    user_id       VARCHAR(64)     NOT NULL COMMENT '成员用户ID',
    user_name     VARCHAR(64)     NULL     COMMENT '成员姓名',
    role_key      VARCHAR(32)     NOT NULL COMMENT '成员在项目中的角色键（引用 sys_role.role_key）',
    is_lead       TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '是否项目负责人 1=是 0=否',
    join_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '加入时间',
    leave_at      DATETIME        NULL     COMMENT '离开时间（空=在组）',
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted    TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '逻辑删除 1=已删',
    PRIMARY KEY (id),
    UNIQUE KEY uk_project_member (project_id, user_id, role_key),
    KEY idx_user_id (user_id),
    KEY idx_role_key (role_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='项目成员（含角色任命，权限由人工完成）';

-- ---------------------------------------------------------------------------
-- 3) 立项依据
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS initiation_basis;
CREATE TABLE initiation_basis (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    basis_id      CHAR(32)        NOT NULL COMMENT '立项依据业务ID',
    project_id    CHAR(32)        NOT NULL COMMENT '所属项目业务ID',
    basis_type    VARCHAR(32)     NOT NULL COMMENT '依据类型：年度计划/专项任务/委托/其他',
    basis_source  VARCHAR(255)    NULL     COMMENT '依据来源（文件名/文号/部门）',
    basis_doc_no  VARCHAR(64)     NULL     COMMENT '依据文号',
    basis_content TEXT            NULL     COMMENT '立项依据内容（立项理由与合规依据）',
    attachment    VARCHAR(1000)   NULL     COMMENT '附件路径/URL（逗号分隔）',
    audit_status  VARCHAR(32)     NOT NULL DEFAULT 'pending' COMMENT '审核状态：pending 待审核 / approved 通过 / rejected 驳回',
    audit_role    VARCHAR(32)     NULL     COMMENT '审核角色（业务负责人 business_lead）',
    audit_user_id VARCHAR(64)     NULL     COMMENT '审核人用户ID',
    audit_opinion VARCHAR(1000)   NULL     COMMENT '审核意见',
    audit_at      DATETIME        NULL     COMMENT '审核时间',
    created_by    VARCHAR(64)     NULL     COMMENT '创建人',
    updated_by    VARCHAR(64)     NULL     COMMENT '最后更新人',
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted    TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '逻辑删除 1=已删',
    PRIMARY KEY (id),
    UNIQUE KEY uk_basis_id (basis_id),
    KEY idx_project_id (project_id),
    KEY idx_audit_status (audit_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='立项依据（缺失时自动生成需求表，业务负责人审核）';

-- ---------------------------------------------------------------------------
-- 4) 审计实施方案
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS audit_plan;
CREATE TABLE audit_plan (
    id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    plan_id           CHAR(32)        NOT NULL COMMENT '实施方案业务ID',
    project_id        CHAR(32)        NOT NULL COMMENT '所属项目业务ID',
    plan_name         VARCHAR(255)    NULL     COMMENT '方案名称',
    plan_content      MEDIUMTEXT      NULL     COMMENT '方案正文（审计目标/范围/重点/分工/时间安排）',
    audit_points      TEXT            NULL     COMMENT '审计重点与风险点',
    version           INT             NOT NULL DEFAULT 1 COMMENT '版本号（修订递增）',
    status            VARCHAR(32)     NOT NULL DEFAULT 'draft' COMMENT '方案状态：draft 草稿 / pending_chief 待主审 / pending_group 待组长 / approved 已审批 / issued 已签发 / rejected 驳回',
    chief_audit_status VARCHAR(32)    NULL     COMMENT '主审审核状态：pending/approved/rejected',
    chief_audit_role   VARCHAR(32)    NULL     COMMENT '主审角色（chief_auditor）',
    chief_audit_user   VARCHAR(64)    NULL     COMMENT '主审审核人',
    chief_audit_opinion VARCHAR(1000) NULL     COMMENT '主审审核意见',
    chief_audit_at     DATETIME       NULL     COMMENT '主审审核时间',
    group_audit_status VARCHAR(32)    NULL     COMMENT '组长审核状态：pending/approved/rejected',
    group_audit_role   VARCHAR(32)    NULL     COMMENT '组长角色（group_lead）',
    group_audit_user   VARCHAR(64)    NULL     COMMENT '组长审核人',
    group_audit_opinion VARCHAR(1000) NULL     COMMENT '组长审核意见',
    group_audit_at     DATETIME       NULL     COMMENT '组长审核时间',
    issued_at          DATETIME       NULL     COMMENT '签发时间',
    created_by         VARCHAR(64)    NULL     COMMENT '创建人',
    updated_by         VARCHAR(64)    NULL     COMMENT '最后更新人',
    created_at         DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at         DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted         TINYINT(1)     NOT NULL DEFAULT 0 COMMENT '逻辑删除 1=已删',
    PRIMARY KEY (id),
    UNIQUE KEY uk_plan_id (plan_id),
    KEY idx_project_id (project_id),
    KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='审计实施方案（主审→组长 多级审核）';

-- ---------------------------------------------------------------------------
-- 5) 审计事项清单
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS audit_item;
CREATE TABLE audit_item (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    item_id       CHAR(32)        NOT NULL COMMENT '事项清单业务ID',
    project_id    CHAR(32)        NOT NULL COMMENT '所属项目业务ID',
    plan_id       CHAR(32)        NULL     COMMENT '来源方案业务ID',
    item_name     VARCHAR(255)    NOT NULL COMMENT '审计事项',
    focus_content VARCHAR(1000)   NULL     COMMENT '重点内容',
    clue_content  VARCHAR(1000)   NULL     COMMENT '疑点线索',
    risk_level    VARCHAR(16)     NULL     COMMENT '风险等级：low/medium/high',
    status        VARCHAR(32)     NOT NULL DEFAULT 'pending' COMMENT '事项状态：pending 待开展 / doing 进行中 / done 已完成',
    sort_no       INT             NOT NULL DEFAULT 0 COMMENT '排序号',
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted    TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '逻辑删除 1=已删',
    PRIMARY KEY (id),
    UNIQUE KEY uk_item_id (item_id),
    KEY idx_project_id (project_id),
    KEY idx_plan_id (plan_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='审计事项清单（方案条目化：事项/重点/疑点三层）';

-- ---------------------------------------------------------------------------
-- 6) 审计底稿
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS workpaper;
CREATE TABLE workpaper (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    wp_id           CHAR(32)        NOT NULL COMMENT '底稿业务ID',
    wp_no           VARCHAR(64)     NULL     COMMENT '底稿编号（审核通过后生成）',
    project_id      CHAR(32)        NOT NULL COMMENT '所属项目业务ID',
    title           VARCHAR(255)    NULL     COMMENT '底稿标题',
    audit_object    VARCHAR(255)    NULL     COMMENT '审计对象',
    content         MEDIUMTEXT      NULL     COMMENT '底稿正文（事实-证据-法规-定性-处理意见）',
    evidence_chain  TEXT            NULL     COMMENT '证据链描述',
    law_basis       TEXT            NULL     COMMENT '法规依据',
    anomaly_text    TEXT            NULL     COMMENT '取数筛查异常（脱敏）',
    status          VARCHAR(32)     NOT NULL DEFAULT 'draft' COMMENT '底稿状态：draft 草稿 / pending_auditor 待审计人员确认 / pending_sign 待被审单位签字 / pending_chief 待主审确认 / confirmed 已确认 / locked 已锁定',
    author_role     VARCHAR(32)     NULL     COMMENT '编制人角色（auditor）',
    author_user_id  VARCHAR(64)     NULL     COMMENT '编制人用户ID',
    auditor_status  VARCHAR(32)     NULL     COMMENT '审计人员确认状态：pending/confirmed',
    auditor_user_id VARCHAR(64)     NULL     COMMENT '审计人员确认人',
    auditor_at      DATETIME        NULL     COMMENT '审计人员确认时间',
    sign_status     VARCHAR(32)     NULL     COMMENT '被审计单位签字状态：pending/signed',
    sign_user_id    VARCHAR(64)     NULL     COMMENT '签字人（respondent）',
    sign_at         DATETIME        NULL     COMMENT '签字时间',
    chief_status    VARCHAR(32)     NULL     COMMENT '主审确认状态：pending/confirmed',
    chief_user_id   VARCHAR(64)     NULL     COMMENT '主审确认人',
    chief_at        DATETIME        NULL     COMMENT '主审确认时间',
    locked_at       DATETIME        NULL     COMMENT '锁定时间（报告签发后底稿锁定不可改）',
    created_by      VARCHAR(64)     NULL     COMMENT '创建人',
    updated_by      VARCHAR(64)     NULL     COMMENT '最后更新人',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted      TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '逻辑删除 1=已删',
    PRIMARY KEY (id),
    UNIQUE KEY uk_wp_id (wp_id),
    KEY idx_project_id (project_id),
    KEY idx_status (status),
    KEY idx_wp_no (wp_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='审计底稿（编制→确认→签字→定性→锁定）';

-- ---------------------------------------------------------------------------
-- 7) 审计报告
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS audit_report;
CREATE TABLE audit_report (
    id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    report_id         CHAR(32)        NOT NULL COMMENT '报告业务ID',
    project_id        CHAR(32)        NOT NULL COMMENT '所属项目业务ID',
    report_no         VARCHAR(64)     NULL     COMMENT '报告编号（定稿签发后生成）',
    report_type       VARCHAR(32)     NOT NULL DEFAULT 'draft' COMMENT '报告类型：draft 初稿 / seek_opinion 征求意见稿 / final 完成稿 / signed 定稿',
    title             VARCHAR(255)    NULL     COMMENT '报告标题',
    content           MEDIUMTEXT      NULL     COMMENT '报告正文',
    audit_type        VARCHAR(32)     NULL     COMMENT '审计类型（冗余，便于归类）',
    version           INT             NOT NULL DEFAULT 1 COMMENT '版本号',
    status            VARCHAR(32)     NOT NULL DEFAULT 'draft' COMMENT '报告状态：draft 初稿 / pending_chief 待主审 / pending_group 待组长 / pending_review 待审理 / seeking_opinion 征求意见 / finalizing 定稿中 / signed 已签发 / rejected 驳回',
    chief_audit_status  VARCHAR(32)   NULL     COMMENT '主审审核状态：pending/approved/rejected',
    chief_audit_user    VARCHAR(64)   NULL     COMMENT '主审审核人',
    chief_audit_opinion VARCHAR(1000) NULL     COMMENT '主审审核意见',
    chief_audit_at      DATETIME      NULL     COMMENT '主审审核时间',
    group_audit_status  VARCHAR(32)   NULL     COMMENT '组长/负责人审核状态',
    group_audit_user    VARCHAR(64)   NULL     COMMENT '组长/负责人审核人',
    group_audit_opinion VARCHAR(1000) NULL     COMMENT '组长/负责人审核意见',
    group_audit_at      DATETIME      NULL     COMMENT '组长/负责人审核时间',
    review_status       VARCHAR(32)   NULL     COMMENT '审理组审核状态：pending/approved/rejected',
    review_user         VARCHAR(64)   NULL     COMMENT '审理人（review_panel）',
    review_opinion      VARCHAR(1000) NULL     COMMENT '审理意见',
    review_at           DATETIME      NULL     COMMENT '审理时间',
    opinion_status      VARCHAR(32)   NULL     COMMENT '征求意见反馈状态：pending 待反馈 / received 已回收',
    opinion_user        VARCHAR(64)   NULL     COMMENT '反馈人（respondent）',
    opinion_at          DATETIME      NULL     COMMENT '反馈时间',
    signed_at           DATETIME      NULL     COMMENT '签发时间（签发后关联底稿锁定）',
    created_by          VARCHAR(64)   NULL     COMMENT '创建人',
    updated_by          VARCHAR(64)   NULL     COMMENT '最后更新人',
    created_at          DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at          DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted          TINYINT(1)    NOT NULL DEFAULT 0 COMMENT '逻辑删除 1=已删',
    PRIMARY KEY (id),
    UNIQUE KEY uk_report_id (report_id),
    KEY idx_project_id (project_id),
    KEY idx_status (status),
    KEY idx_report_no (report_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='审计报告（主审→组长→审理→征求意见→签发）';

-- ---------------------------------------------------------------------------
-- 8) 审计问题库（历史真实脱敏数据，字段与 CSV 表头一一对应）
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS audit_problem;
CREATE TABLE audit_problem (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    problem_id      CHAR(32)        NOT NULL COMMENT '问题业务ID',
    project_id      CHAR(32)        NULL     COMMENT '关联项目业务ID（历史问题库可为空）',
    audit_year      VARCHAR(8)      NULL     COMMENT '年度',
    supervision     VARCHAR(255)    NULL     COMMENT '监督职能',
    project_name    VARCHAR(255)    NULL     COMMENT '项目名称',
    init_unit       VARCHAR(255)    NULL     COMMENT '立项单位',
    exec_unit       VARCHAR(255)    NULL     COMMENT '实施单位',
    audit_target    VARCHAR(255)    NULL     COMMENT '被监督对象',
    involved_unit   VARCHAR(255)    NULL     COMMENT '问题涉及单位',
    biz_domain      VARCHAR(255)    NULL     COMMENT '业务领域',
    problem_nature  VARCHAR(255)    NULL     COMMENT '问题定性',
    flow_name       VARCHAR(255)    NULL     COMMENT '流程名称',
    kcp_no          VARCHAR(64)     NULL     COMMENT 'KCP 编号',
    kcp_name        VARCHAR(255)    NULL     COMMENT 'KCP 名称',
    problem_name    VARCHAR(500)    NULL     COMMENT '问题名称',
    amount_wan      DECIMAL(18,4)   NULL     COMMENT '问题涉及金额（万元，已脱敏）',
    rectify_subject VARCHAR(255)    NULL     COMMENT '整改责任主体',
    manage_subject  VARCHAR(255)    NULL     COMMENT '管理责任主体',
    supervise_subject VARCHAR(255)  NULL     COMMENT '监督责任主体',
    source_type     VARCHAR(32)     NULL     COMMENT '来源：历史库导入/本项目现场发现',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted      TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '逻辑删除 1=已删',
    PRIMARY KEY (id),
    UNIQUE KEY uk_problem_id (problem_id),
    KEY idx_audit_year (audit_year),
    KEY idx_biz_domain (biz_domain),
    KEY idx_problem_nature (problem_nature),
    KEY idx_supervision (supervision),
    KEY idx_project_id (project_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='审计问题库（历史真实脱敏数据，用于数据画像/检索）';

-- ---------------------------------------------------------------------------
-- 9) 整改台账（历史真实脱敏数据 + 本项目整改状态流转）
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS rectification_ledger;
CREATE TABLE rectification_ledger (
    id                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    ledger_id         CHAR(32)        NOT NULL COMMENT '整改台账业务ID',
    project_id        CHAR(32)        NULL     COMMENT '关联项目业务ID（历史台账可为空）',
    audit_year        VARCHAR(8)      NULL     COMMENT '年度',
    problem_no        VARCHAR(64)     NULL     COMMENT '问题编号',
    project_name      VARCHAR(255)    NULL     COMMENT '项目名称',
    audited_unit      VARCHAR(255)    NULL     COMMENT '被审计单位',
    problem_name      VARCHAR(500)    NULL     COMMENT '问题名称',
    problem_source    VARCHAR(255)    NULL     COMMENT '问题来源',
    category1         VARCHAR(255)    NULL     COMMENT '问题类别1',
    category2         VARCHAR(255)    NULL     COMMENT '问题类别2',
    category3         VARCHAR(255)    NULL     COMMENT '问题类别3',
    problem_nature    VARCHAR(255)    NULL     COMMENT '问题定性',
    amount_yuan       DECIMAL(18,2)   NULL     COMMENT '问题涉及金额（元，已脱敏）',
    rectify_dept      VARCHAR(255)    NULL     COMMENT '整改责任部门',
    rectify_status    VARCHAR(32)     NOT NULL DEFAULT 'pending' COMMENT '整改状态：pending 待整改 / rectifying 整改中 / done 已销号 / overdue 逾期 / continuous 持续整改',
    is_closed         TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '是否销号 1=已销号',
    rectify_deadline  DATE            NULL     COMMENT '整改时限',
    cause_analysis    TEXT            NULL     COMMENT '问题原因分析',
    rectify_measure   TEXT            NULL     COMMENT '整改措施',
    audit_suggestion  TEXT            NULL     COMMENT '审计建议',
    result_amount     DECIMAL(18,2)   NULL     COMMENT '整改成果金额（元）',
    new_system        VARCHAR(500)    NULL     COMMENT '新建或修订制度',
    improved_process  VARCHAR(500)    NULL     COMMENT '优化或完善流程',
    handled_count     INT             NULL     COMMENT '处理数量（个）',
    transferred_count INT             NULL     COMMENT '移送数量（个）',
    review_status     VARCHAR(32)     NULL     COMMENT '复核状态：pending 待复核 / approved 通过销号 / rejected 不通过继续整改 / carried 结转持续整改',
    review_role       VARCHAR(32)     NULL     COMMENT '复核角色（chief_auditor）',
    review_user       VARCHAR(64)     NULL     COMMENT '复核人',
    review_opinion    VARCHAR(1000)   NULL     COMMENT '复核意见（退回必填）',
    review_at         DATETIME        NULL     COMMENT '复核时间',
    closed_at         DATETIME        NULL     COMMENT '销号时间',
    created_at        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted        TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '逻辑删除 1=已删',
    PRIMARY KEY (id),
    UNIQUE KEY uk_ledger_id (ledger_id),
    KEY idx_audit_year (audit_year),
    KEY idx_rectify_status (rectify_status),
    KEY idx_review_status (review_status),
    KEY idx_rectify_dept (rectify_dept),
    KEY idx_project_id (project_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='整改台账（整改状态流转 + 复核销号三态分支）';

-- ---------------------------------------------------------------------------
-- 10) 智能体配置表
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS agent_config;
CREATE TABLE agent_config (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    agent_id        CHAR(32)        NOT NULL COMMENT '智能体业务ID',
    agent_key       VARCHAR(32)     NOT NULL COMMENT '智能体键：initiation/preaudit/field/report/rectification/archive',
    agent_name      VARCHAR(64)     NOT NULL COMMENT '智能体名称（如 立项 Agent）',
    stage_key       VARCHAR(32)     NULL     COMMENT '所属阶段键',
    stage_name      VARCHAR(64)     NULL     COMMENT '所属阶段中文名',
    responsibilities JSON           NULL     COMMENT '核心职责（JSON 数组）',
    tools           JSON           NULL     COMMENT '绑定工具（JSON 数组）',
    boundaries      JSON           NULL     COMMENT '能力边界/红线（JSON 数组）',
    inputs          JSON           NULL     COMMENT '主要输入（JSON 数组）',
    outputs         JSON           NULL     COMMENT '主要输出（JSON 数组）',
    gate            VARCHAR(255)    NULL     COMMENT '人工闸门',
    model           VARCHAR(64)     NULL     COMMENT '绑定模型（如 deepseek-chat）',
    prompt_version  VARCHAR(32)     NULL     COMMENT '提示词版本',
    status          VARCHAR(32)     NOT NULL DEFAULT 'enabled' COMMENT '状态：enabled 启用 / disabled 停用',
    version         INT             NOT NULL DEFAULT 1 COMMENT '配置版本号',
    created_by      VARCHAR(64)     NULL     COMMENT '创建人',
    updated_by      VARCHAR(64)     NULL     COMMENT '最后更新人',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted      TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '逻辑删除 1=已删',
    PRIMARY KEY (id),
    UNIQUE KEY uk_agent_id (agent_id),
    UNIQUE KEY uk_agent_key (agent_key),
    KEY idx_stage_key (stage_key),
    KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='智能体配置表（六阶段 Agent 编排）';

-- ---------------------------------------------------------------------------
-- 11) 工具契约表
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS tool_contract;
CREATE TABLE tool_contract (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    tool_id         CHAR(32)        NOT NULL COMMENT '工具业务ID',
    tool_key        VARCHAR(64)     NOT NULL COMMENT '工具键（如 flow_query/ledger_query/policy_search）',
    family          VARCHAR(32)     NOT NULL COMMENT '工具族：取数/检索/分析/生成/流程',
    tool_name       VARCHAR(64)     NOT NULL COMMENT '工具名称',
    risk_level      VARCHAR(16)     NOT NULL DEFAULT 'low' COMMENT '风险等级：low/medium/high',
    write_flag      TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '是否写入类工具 1=是（需 request_id 幂等）',
    condition       VARCHAR(1000)   NULL     COMMENT '触发条件',
    inputs          JSON            NULL     COMMENT '输入参数 Schema（JSON 数组）',
    outputs         JSON            NULL     COMMENT '输出结构（JSON 数组）',
    rules           JSON            NULL     COMMENT '约束规则（JSON 数组）',
    schema_version  VARCHAR(16)     NOT NULL DEFAULT '1.0' COMMENT '参数 Schema 版本号',
    result_statuses JSON            NULL     COMMENT '允许的结果状态（success/partial/blocked/error）',
    status          VARCHAR(32)     NOT NULL DEFAULT 'enabled' COMMENT '状态：enabled/disabled',
    created_by      VARCHAR(64)     NULL     COMMENT '创建人',
    updated_by      VARCHAR(64)     NULL     COMMENT '最后更新人',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted      TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '逻辑删除 1=已删',
    PRIMARY KEY (id),
    UNIQUE KEY uk_tool_id (tool_id),
    UNIQUE KEY uk_tool_key (tool_key),
    KEY idx_family (family),
    KEY idx_risk_level (risk_level)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='工具契约表（统一输入/输出信封 + 风险等级 + 幂等）';

-- ---------------------------------------------------------------------------
-- 12) 知识库表
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS knowledge_base;
CREATE TABLE knowledge_base (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    kb_id         CHAR(32)        NOT NULL COMMENT '知识条目业务ID',
    doc_id        VARCHAR(64)     NULL     COMMENT '来源文档标识（对应 knowledge/*.md）',
    title         VARCHAR(255)    NOT NULL COMMENT '条目标题',
    source        VARCHAR(255)    NULL     COMMENT '来源文件',
    category      VARCHAR(32)     NULL     COMMENT '类别/阶段（法规/制度/模板/案例/报告规范/整改要点/实施方案规范）',
    stage_key     VARCHAR(32)     NULL     COMMENT '适用阶段键（可空=通用）',
    content       MEDIUMTEXT      NULL     COMMENT '条目内容/正文',
    chunk_index   INT             NULL     COMMENT '切片序号（BM25 分块）',
    tags          VARCHAR(500)    NULL     COMMENT '标签（逗号分隔）',
    effective_from DATE           NULL     COMMENT '生效日期',
    effective_to  DATE            NULL     COMMENT '失效日期（空=现行有效）',
    is_effective  TINYINT(1)      NOT NULL DEFAULT 1 COMMENT '是否现行有效 1=是 0=否',
    version       INT             NOT NULL DEFAULT 1 COMMENT '版本号',
    status        VARCHAR(32)     NOT NULL DEFAULT 'enabled' COMMENT '状态：enabled/disabled',
    created_by    VARCHAR(64)     NULL     COMMENT '创建人',
    updated_by    VARCHAR(64)     NULL     COMMENT '最后更新人',
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    is_deleted    TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '逻辑删除 1=已删',
    PRIMARY KEY (id),
    UNIQUE KEY uk_kb_id (kb_id),
    KEY idx_doc_id (doc_id),
    KEY idx_category (category),
    KEY idx_stage_key (stage_key),
    KEY idx_is_effective (is_effective)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='知识库表（BM25 阶段化检索语料）';

-- ---------------------------------------------------------------------------
-- 13) 审核/会签记录（配套表，统一沉淀状态流转轨迹）
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS approval_record;
CREATE TABLE approval_record (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    record_id     CHAR(32)        NOT NULL COMMENT '审核记录业务ID',
    project_id    CHAR(32)        NOT NULL COMMENT '所属项目业务ID',
    stage_key     VARCHAR(32)     NOT NULL COMMENT '所属阶段键',
    biz_type      VARCHAR(32)     NOT NULL COMMENT '业务对象：立项依据/实施方案/审计事项/底稿/报告/整改/归档',
    biz_id        CHAR(32)        NULL     COMMENT '业务对象ID',
    condition_name VARCHAR(255)   NOT NULL COMMENT '会签条件名（如「实施方案主审审核通过」）',
    action        VARCHAR(16)     NOT NULL COMMENT '动作：approve 通过 / reject 驳回 / sign 签署 / confirm 确认',
    role_key      VARCHAR(32)     NOT NULL COMMENT '执行角色键',
    user_id       VARCHAR(64)     NULL     COMMENT '执行人用户ID',
    opinion       VARCHAR(1000)   NULL     COMMENT '意见/说明（驳回必填）',
    result        VARCHAR(32)     NOT NULL DEFAULT 'approved' COMMENT '结果：approved 通过 / rejected 驳回',
    operated_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '操作时间',
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_record_id (record_id),
    KEY idx_project_stage (project_id, stage_key),
    KEY idx_biz (biz_type, biz_id),
    KEY idx_role_key (role_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='审核/会签记录（状态流转全轨迹，驳回必留意见）';

-- ---------------------------------------------------------------------------
-- 14) 通知表（配套表，分角色待办/通知）
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS notice;
CREATE TABLE notice (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    notice_id     CHAR(32)        NOT NULL COMMENT '通知业务ID',
    project_id    CHAR(32)        NOT NULL COMMENT '所属项目业务ID',
    role_key      VARCHAR(32)     NOT NULL COMMENT '接收角色键',
    text          VARCHAR(1000)   NOT NULL COMMENT '通知内容',
    is_read       TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '是否已读 1=已读',
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    read_at       DATETIME        NULL     COMMENT '阅读时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_notice_id (notice_id),
    KEY idx_project_role (project_id, role_key),
    KEY idx_is_read (is_read)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='通知表（分角色待办通知，供角标消费）';

SET FOREIGN_KEY_CHECKS = 1;

-- ============================================================================
-- 说明：外键约束（可选）。演示环境为保持 CSV/内存态兼容，未强开外键；
--      生产落地时可在以下业务表上补充 FOREIGN KEY 引用 audit_project(project_id)：
--        project_member / initiation_basis / audit_plan / audit_item /
--        workpaper / audit_report / audit_problem / rectification_ledger /
--        approval_record / notice
--      角色字段统一引用 sys_role(role_key)。
-- ============================================================================
