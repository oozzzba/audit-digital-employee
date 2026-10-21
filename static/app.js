/* 审计数字员工 · 前端逻辑（原生 JS，无外部依赖） */
"use strict";

// ==================== 全局状态 ====================
let sessionId = null;
let currentView = "chat";
let pendingGates = 0; // 待人工确认的闸门数

// ==================== 配置 ====================
const NAV = [
  { view: "chat", label: "审计任务工作台", icon: "💬" },
  { view: "dashboard", label: "项目看板", icon: "▦" },
  { view: "problems", label: "问题清单", icon: "⚠" },
  { view: "report", label: "审计报告", icon: "📄" },
  { view: "rect", label: "整改督办", icon: "✓" },
  { view: "ledger", label: "历史整改台账", icon: "🗂" },
  { view: "problemlib", label: "审计问题库", icon: "🔎" },
];
const ROLES = [
  { name: "张审计", title: "审计组组长", avatar: "张" },
  { name: "李审计", title: "审计人员", avatar: "李" },
];
const SCENARIOS = [
  { label: "差旅费报销审计", text: "对某国有企业2025年度差旅费报销情况开展专项审计" },
  { label: "物资采购审计", text: "对某国有企业2025年度物资采购业务开展专项审计" },
  { label: "资产管理审计", text: "对某国有企业2025年度固定资产管理情况开展专项审计" },
];

// 六阶段助手：内部名 id / 首页导航名 name / 助手名 homeName / 主题色 color / 功能卡片 cards
// 每张卡片带 example（数据示例，脱敏演示数据）+ prompt（点击后发送的具体指令）
const STAGES = [
  {
    id: "initiation", num: "01", name: "项目立项", homeName: "立项助手", color: "#D32F2F",
    cards: [
      { key: "create_project", name: "创建审计项目", desc: "录入项目基本信息与立项依据", icon: "📋", example: "竣工决算审计 · 某公司XX天然气管道工程（2023—2025）", prompt: "审计 对某公司XX天然气管道工程项目2023至2025年度竣工决算情况开展竣工决算审计" },
      { key: "project_approval", name: "立项审批", desc: "提交审批流并查看审批状态", icon: "✅", example: "立项申请 → 年度计划审批 → 项目负责人任命", prompt: "确认立项审批通过，进入审前准备" },
      { key: "project_ledger", name: "项目台账管理", desc: "项目信息维护与台账查询", icon: "📚", example: "台账字段：立项编号 / 被审计单位 / 审计类型 / 审计期间", prompt: "立项：查询项目台账" },
      { key: "audit_plan", name: "年度审计计划", desc: "制定与分解年度审计计划", icon: "📅", example: "2025年度计划：天然气管道 / 差旅费 / 物资采购等专项", prompt: "立项：制定2025年度审计计划" },
    ],
  },
  {
    id: "preaudit", num: "02", name: "审前准备", homeName: "审前准备助手", color: "#E53935",
    cards: [
      { key: "form_team", name: "成立审计组", desc: "配置审计组组长与成员", icon: "👥", example: "组长：张审计；成员：李审计、王审计", prompt: "审前准备：成立审计组（组长张审计，成员李审计、王审计）" },
      { key: "audit_scheme", name: "编制审计方案", desc: "生成实施方案与审计重点", icon: "📝", example: "审计目标 / 范围 / 重点 / 方法 / 时间安排", prompt: "编制审计方案" },
      { key: "audit_notice", name: "审计通知书", desc: "自动生成并发送审计通知书", icon: "✉️", example: "致XX分公司：审计期间2023—2025、进场日期、审计范围", prompt: "审前准备：生成并发送审计通知书" },
      { key: "pre_survey", name: "审前调查", desc: "收集被审计单位基础资料", icon: "🔍", example: "制度 / 报表 / 合同 / 凭证等基础资料", prompt: "审前准备：审前调查，收集被审计单位基础资料" },
    ],
  },
  {
    id: "field", num: "03", name: "现场实施", homeName: "现场实施助手", color: "#F57C00",
    cards: [
      { key: "generate_draft", name: "生成审计底稿", desc: "AI 自动生成审计底稿", icon: "📄", example: "差旅费筛查：无票据报销、超标准列支等异常", prompt: "现场实施：生成审计底稿" },
      { key: "collect_evidence", name: "收集审计证据", desc: "上传与管理审计证据材料", icon: "🧾", example: "报销单 / 发票 / 审批单 / 会议纪要", prompt: "现场实施：收集审计证据" },
      { key: "find_issue", name: "问题发现与定性", desc: "识别问题并匹配审计定性", icon: "⚠️", example: "定性：报销依据不充分、超标准列支费用", prompt: "现场实施：问题发现与定性" },
      { key: "interview_record", name: "审计访谈记录", desc: "记录并整理访谈内容", icon: "🎙️", example: "访谈对象：财务负责人 / 经办人 / 审批人", prompt: "现场实施：审计访谈记录" },
      { key: "exception_scenarios", name: "异常处置示例", desc: "按统一规范处理数据与服务异常", icon: "🛠️", example: "数据缺失 / 字段空值 / 金额为负 / 超范围", prompt: "现场实施：异常处置示例" },
    ],
  },
  {
    id: "report", num: "04", name: "报告编制", homeName: "报告编制助手", color: "#00897B",
    cards: [
      { key: "write_report", name: "撰写审计报告", desc: "智能生成报告初稿", icon: "📑", example: "竣工决算审计报告（七章）", prompt: "生成报告" },
      { key: "seek_opinion", name: "征求意见", desc: "生成征求意见稿并收集反馈", icon: "💬", example: "征求意见稿 → 被审计单位书面反馈", prompt: "报告编制：征求意见" },
      { key: "submit_review", name: "提交审理", desc: "提交审理并查看审理意见", icon: "⚖️", example: "提交审理 → 出具审理意见", prompt: "报告编制：提交审理" },
      { key: "report_sign", name: "报告定稿签发", desc: "报告定稿与签章流程", icon: "✍️", example: "报告定稿 → 签章 → 签发", prompt: "报告编制：报告定稿签发" },
    ],
  },
  {
    id: "rectification", num: "05", name: "整改督办", homeName: "整改督办助手", color: "#7B1FA2",
    cards: [
      { key: "rect_plan", name: "制定整改方案", desc: "生成整改措施与时间节点", icon: "📋", example: "整改措施 / 责任部门 / 完成时限 / 销号标准", prompt: "整改督办：制定整改方案" },
      { key: "rect_progress", name: "整改进度跟踪", desc: "实时跟踪各问题整改进度", icon: "📈", example: "台账：已整改 / 未整改 / 逾期数量", prompt: "整改督办：整改进度跟踪" },
      { key: "overdue_supervise", name: "逾期督办", desc: "逾期预警与督办通知", icon: "⏰", example: "逾期预警：超时限未销号问题清单", prompt: "整改督办：逾期督办" },
      { key: "rect_accept", name: "整改验收", desc: "整改结果验收与销号", icon: "✅", example: "验收销号：整改到位、附件齐全", prompt: "整改督办：整改验收" },
    ],
  },
  {
    id: "archive", num: "06", name: "项目归档", homeName: "归档助手", color: "#5E35B1",
    cards: [
      { key: "file_materials", name: "资料归档", desc: "分类整理审计项目资料", icon: "🗂", example: "底稿 / 证据 / 报告 / 通知书 / 整改台账", prompt: "归档：资料归档" },
      { key: "archive_list", name: "归档清单", desc: "生成归档材料清单", icon: "📦", example: "卷内目录：分类 / 编号 / 页数", prompt: "归档：生成归档清单" },
      { key: "project_close", name: "项目终结", desc: "完成项目闭环与总结", icon: "🏁", example: "项目闭环：归档完成、总结归档", prompt: "归档：项目终结" },
      { key: "archive_borrow", name: "档案借阅", desc: "档案调阅与借阅管理", icon: "📖", example: "借阅：调阅申请、归还登记", prompt: "归档：档案借阅" },
    ],
  },
];

let currentStage = "initiation"; // 当前选中阶段（决定功能卡片）
let phaseStatus = {}; // 阶段 id -> done / active / pending（来自后端 phases）

// ==================== 工具 ====================
const $ = (id) => document.getElementById(id);

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => { t.classList.add("show"); }, 10);
  setTimeout(() => { t.classList.remove("show"); setTimeout(() => t.remove(), 300); }, 2600);
}

async function api(path, body) {
  const opt = body ? {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  } : { method: "GET" };
  const resp = await fetch(path, opt);
  return resp.json();
}

// ==================== Markdown 渲染（逐行解析，轻量自研） ====================
function inline(s) {
  let t = escapeHtml(s);
  t = t.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  return t;
}

function renderTable(tableLines) {
  // tableLines[0]=表头, [1]=分隔行, [2+]=数据行
  const cells = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  const header = cells(tableLines[0]);
  let html = "<table><thead><tr>" + header.map((c) => "<th>" + inline(c) + "</th>").join("") + "</tr></thead><tbody>";
  for (let k = 2; k < tableLines.length; k++) {
    const row = cells(tableLines[k]);
    html += "<tr>" + row.map((c) => "<td>" + inline(c) + "</td>").join("") + "</tr>";
  }
  return html + "</tbody></table>";
}

function md(src) {
  if (!src) return '<div class="empty-hint">暂无内容</div>';
  const lines = src.split("\n");
  let html = "", i = 0, para = [];
  const flushPara = () => { if (para.length) { html += "<p>" + inline(para.join("<br>")) + "</p>"; para = []; } };

  while (i < lines.length) {
    const line = lines[i];

    // 代码块
    if (/^```/.test(line.trim())) { flushPara(); let code = ""; i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) { code += (code ? "\n" : "") + lines[i]; i++; }
      i++; html += "<pre><code>" + escapeHtml(code) + "</code></pre>"; continue; }

    // 空行：段落结束
    if (!line.trim()) { flushPara(); i++; continue; }

    let m;
    if ((m = line.match(/^####\s+(.*)$/))) { flushPara(); html += "<h4>" + inline(m[1]) + "</h4>"; i++; continue; }
    if ((m = line.match(/^###\s+(.*)$/))) { flushPara(); html += "<h3>" + inline(m[1]) + "</h3>"; i++; continue; }
    if ((m = line.match(/^##\s+(.*)$/))) { flushPara(); html += "<h2>" + inline(m[1]) + "</h2>"; i++; continue; }
    if ((m = line.match(/^#\s+(.*)$/))) { flushPara(); html += "<h1>" + inline(m[1]) + "</h1>"; i++; continue; }
    if ((m = line.match(/^([一二三四五六七八九十]+)、\s*(.*)$/))) { flushPara(); html += "<h3>" + inline(m[1] + "、" + m[2]) + "</h3>"; i++; continue; }

    // 分隔线
    if (/^\s*-{3,}\s*$/.test(line)) { flushPara(); html += "<hr>"; i++; continue; }

    // 引用
    if (/^>\s*/.test(line)) { flushPara(); html += "<blockquote>" + inline(line.replace(/^>\s*/, "")) + "</blockquote>"; i++; continue; }

    // 表格
    if (/^\|.*\|\s*$/.test(line)) {
      flushPara(); const tableLines = [];
      while (i < lines.length && /^\|.*\|\s*$/.test(lines[i])) { tableLines.push(lines[i]); i++; }
      html += renderTable(tableLines); continue;
    }

    // 无序列表
    if (/^[-*]\s+/.test(line)) { flushPara(); html += "<ul>";
      while (i < lines.length && /^[-*]\s+/.test(lines[i])) { html += "<li>" + inline(lines[i].replace(/^[-*]\s+/, "")) + "</li>"; i++; }
      html += "</ul>"; continue; }

    // 有序列表
    if (/^\d+[.、]\s+/.test(line)) { flushPara(); html += "<ol>";
      while (i < lines.length && /^\d+[.、]\s+/.test(lines[i])) { html += "<li>" + inline(lines[i].replace(/^\d+[.、]\s+/, "")) + "</li>"; i++; }
      html += "</ol>"; continue; }

    // 普通段落
    para.push(line.trim()); i++;
  }
  flushPara();
  return '<div class="md">' + html + "</div>";
}

// ==================== 六阶段进度条 ====================
function renderPipeline(container, phases) {
  if (!container) return;
  if (!phases || !phases.length) {
    container.innerHTML = "";
    return;
  }
  container.innerHTML = phases.map((p) => `
    <div class="phase-step ${p.status}">
      <div class="phase-step-name">${escapeHtml(p.name)}</div>
      <div class="phase-step-bar"><div class="phase-step-bar-fill"></div></div>
    </div>`).join("");
}

// ==================== 六步导航 + 功能卡片 ====================
function stageById(id) { return STAGES.find((s) => s.id === id); }

function renderStepNav() {
  $("stepNav").innerHTML = STAGES.map((s) => {
    const done = phaseStatus[s.id] === "done";
    const active = s.id === currentStage;
    const locked = isStageLocked(s.id);
    return `<div class="step-nav-item ${done ? "done" : ""} ${active ? "active" : ""} ${locked ? "locked" : ""}" data-stage="${s.id}" style="--step-color:${s.color}">
      <span class="step-dot"></span>
      <span class="step-num">${s.num}</span>
      <span class="step-info">
        <span class="step-name">${s.name}</span>
        <span class="step-home">${s.homeName}</span>
      </span>
    </div>`;
  }).join("");
  document.querySelectorAll(".step-nav-item").forEach((n) => {
    n.onclick = () => { currentStage = n.dataset.stage; renderStepNav(); renderFunctionCards(); };
  });
}

function reachableIndex() {
  // 最远「已完成」阶段的序号；尚未开始返回 -1
  let last = -1;
  STAGES.forEach((s, i) => { if (phaseStatus[s.id] === "done") last = i; });
  return last;
}

function isStageLocked(stageId) {
  const ri = reachableIndex();
  const ci = STAGES.findIndex((s) => s.id === stageId);
  // 已完成阶段可回看/重跑；下一待办阶段可操作；再往后的阶段锁定
  return ci > ri + 1;
}

function renderFunctionCards() {
  const s = stageById(currentStage);
  if (!s) return;
  const locked = isStageLocked(s.id);
  $("functionCards").style.setProperty("--step-color", s.color);
  $("functionCards").innerHTML = s.cards.map((c) => `
    <div class="func-card${locked ? " locked" : ""}" data-prompt="${escapeHtml(c.prompt)}">
      <div class="func-top">
        <span class="func-icon">${c.icon}</span>
        <span class="func-name">${c.name}</span>
        <span class="func-key">${c.key}</span>
      </div>
      <div class="func-desc">${escapeHtml(c.desc)}</div>
      <div class="func-example">示例：${escapeHtml(c.example)}</div>
    </div>`).join("");
  document.querySelectorAll(".func-card").forEach((card) => {
    card.onclick = () => {
      if (card.classList.contains("locked")) {
        toast("🔒 该阶段尚未解锁：请先完成前面阶段，或点击右上角「⚡ 一键全流程」快速推进");
        return;
      }
      currentStage = s.id;
      $("chatInput").value = card.dataset.prompt;
      onSend();
    };
  });
}

function updatePhaseStatus(phases) {
  (phases || []).forEach((p) => {
    const s = STAGES.find((x) => x.name === p.name);
    if (s) phaseStatus[s.id] = p.status;
  });
}

// ==================== 结果溯源（折叠面板内容） ====================
function traceSummary(trace) {
  if (!trace) return "";
  const a = (trace.anomalies || []).length;
  const l = (trace.retrieved || []).length;
  if (!a && !l && !trace.data_source) return "";
  let s = "";
  if (a) s += "取数异常 " + a;
  if (l) s += (s ? " · " : "") + "法规依据 " + l;
  if (trace.data_source) s += (s ? " · " : "") + "数据源 " + trace.data_source;
  return s;
}

function renderTraceHtml(trace) {
  if (!trace || (!(trace.anomalies || []).length && !(trace.retrieved || []).length && !trace.data_source)) return "";
  let html = "";
  if (trace.anomalies && trace.anomalies.length) {
    html += '<div class="rt-section"><div class="rt-section-title">取数异常（' + trace.anomalies.length + '）</div>';
    trace.anomalies.slice(0, 12).forEach((a) => {
      html += `<div class="rt-anomaly"><div class="a-type">${escapeHtml(a["类型"] || "")}</div><div class="a-sub">${escapeHtml(a["主体"] || "")} · ${escapeHtml(String(a["金额"] ?? ""))}元</div></div>`;
    });
    html += "</div>";
  }
  if (trace.retrieved && trace.retrieved.length) {
    html += '<div class="rt-section"><div class="rt-section-title">法规依据（' + trace.retrieved.length + '）</div>';
    trace.retrieved.forEach((r) => {
      html += `<div class="rt-law"><div class="l-src">${escapeHtml(r.source)}</div>相关度 ${r.score}</div>`;
    });
    html += "</div>";
  }
  return html;
}

// ==================== 聊天：消息渲染 ====================
function appendUser(text) {
  const empty = $("chatEmpty");
  if (empty) empty.style.display = "none";
  const div = document.createElement("div");
  div.className = "msg user";
  div.innerHTML = '<div class="msg-bubble user">' + escapeHtml(text) + "</div>";
  $("chatMessages").appendChild(div);
  scrollChat();
}

function appendStageCard(r, interactive, auto) {
  const div = document.createElement("div");
  div.className = "msg ai";

  let blocksHtml = "";
  (r.blocks || []).forEach((b) => {
    if (b.type === "markdown") blocksHtml += '<div class="block"><div class="block-label">' + escapeHtml(b.label) + '</div><div class="block-content">' + md(b.content) + "</div></div>";
    else if (b.type === "json") blocksHtml += '<div class="block"><div class="block-label">' + escapeHtml(b.label) + '</div><div class="block-content"><pre class="json">' + escapeHtml(b.content) + "</pre></div></div>";
    else if (b.type === "retrieved") {
      const items = (b.items || []).map((it) => `<div class="retrieved-item"><span class="r-score">相关度 ${it.score}</span><div class="r-src">${escapeHtml(it.source)}</div><div class="r-text">${escapeHtml((it.text || "").slice(0, 80))}${(it.text || "").length > 80 ? "…" : ""}</div></div>`).join("");
      blocksHtml += '<div class="block"><div class="block-label">' + escapeHtml(b.label) + '</div><div class="retrieved-list">' + items + "</div></div>";
    }
  });

  let gateHtml = "";
  if (auto) {
    gateHtml = `<div class="gate-bar"><span class="gate-label">▶ 已自动通过（一键全流程）</span><span class="gate-done">${escapeHtml(r.gate)}</span></div>`;
  } else if (interactive && r.next_stage_name && !r.done) {
    gateHtml = `<div class="gate-bar"><span class="gate-label">▶ 人工闸门：${escapeHtml(r.gate)}</span><button class="gate-confirm-btn">确认通过，进入${escapeHtml(r.next_stage_name)}</button></div>`;
  } else if (r.done) {
    gateHtml = '<div class="gate-bar"><span class="gate-label">▶ 全流程跑通</span><span class="gate-done">最终由人工终审放行（合规红线）</span></div>';
  } else {
    gateHtml = `<div class="gate-bar"><span class="gate-label">▶ 人工闸门：${escapeHtml(r.gate)}</span></div>`;
  }

  const traceHtml = renderTraceHtml(r.trace);
  const traceSection = traceHtml
    ? `<details class="trace-details"><summary>🔍 结果溯源（${traceSummary(r.trace)}）</summary><div class="trace-body">${traceHtml}</div></details>`
    : "";

  div.innerHTML = `
    <div class="stage-card">
      <div class="stage-card-head"><span class="stage-card-title">${escapeHtml(r.title)}</span><span class="stage-card-tag">${r.stage === "field" ? "核心阶段" : "阶段产出"}</span></div>
      <div class="stage-card-body">${blocksHtml}${traceSection}${gateHtml}</div>
    </div>`;
  $("chatMessages").appendChild(div);

  // 绑定闸门确认按钮
  const btn = div.querySelector(".gate-confirm-btn");
  if (btn) btn.onclick = () => nextPipeline(btn);

  if (r.phases) {
    updatePhaseStatus(r.phases);
    const active = r.phases.find((p) => p.status === "active");
    if (active) { const st = STAGES.find((x) => x.name === active.name); if (st) currentStage = st.id; }
    renderStepNav(); renderFunctionCards();
  }
  scrollChat();
}

function appendError(err) {
  const div = document.createElement("div");
  div.className = "msg ai";
  div.innerHTML = '<div class="stage-card"><div class="stage-card-body"><div class="empty-hint" style="color:#B42318">' + escapeHtml(err).replace(/\n/g, "<br>") + "</div></div></div>";
  $("chatMessages").appendChild(div);
  scrollChat();
}

function showTyping() {
  const div = document.createElement("div");
  div.className = "msg ai typing";
  div.id = "typing";
  div.innerHTML = '<span class="typing-dots"><span></span><span></span><span></span></span> 正在生成（DeepSeek 调用中）…';
  $("chatMessages").appendChild(div);
  scrollChat();
}
function hideTyping() { const t = $("typing"); if (t) t.remove(); }
function scrollChat() { const m = $("chatMessages"); m.scrollTop = m.scrollHeight; }

function setStatus(text, ok = true) {
  const s = $("projectChatStatus");
  s.textContent = text;
  s.style.background = ok ? "var(--success-bg)" : "var(--warning-bg)";
  s.style.color = ok ? "var(--success)" : "var(--warning)";
}

// ==================== 聊天：交互 ====================
const PIPELINE_TRIGGER = /自动审计|一键审计|全流程/;

function isPipelineTrigger(text) { return PIPELINE_TRIGGER.test(text); }

function isFreshTask(text) {
  // 全新审计任务（非阶段指令）：以「审计」开头，或「对某…开展…审计」句式
  return /^审计\s*[对某]/.test(text) || /^对某.+开展.+审计$/.test(text);
}

async function onSend() {
  const text = $("chatInput").value.trim();
  if (!text) return;
  $("chatInput").value = "";
  appendUser(text);

  if (isPipelineTrigger(text)) {
    const task = text.replace(PIPELINE_TRIGGER, "").trim();
    if (!task) { appendError("⚠️ 请提供具体审计任务，例如：自动审计 对某国企差旅费报销开展审计"); return; }
    await runAuto(task);
  } else if (isFreshTask(text)) {
    await startPipeline(text);
  } else {
    await runSingle(text);
  }
}

async function runSingle(text) {
  setStatus("生成中…", true);
  showTyping();
  const r = await api("/api/run", { session_id: sessionId, input: text });
  hideTyping();
  if (r.err) { appendError(r.err); setStatus("就绪"); return; }
  if (r.blocked) { appendError(r.blocked_msg); setStatus("已拦截", false); return; }
  setStatus("就绪");
  appendStageCard(r, false);
}

async function startPipeline(task) {
  setStatus("生成中…");
  $("projectChatTitle").textContent = "审计项目 · 六阶段闭环";
  $("projectChatMeta").textContent = task;
  showTyping();
  const r = await api("/api/pipeline/start", { session_id: sessionId, task });
  hideTyping();
  if (r.err) { appendError(r.err); setStatus("就绪"); return; }
  if (r.blocked) { appendError(r.blocked_msg); setStatus("已拦截", false); return; }
  setStatus("待确认");
  pendingGates = 1; updateBadge();
  appendStageCard(r, true);
}

async function nextPipeline(btn) {
  if (btn) { btn.disabled = true; btn.textContent = "确认中…"; }
  setStatus("生成中…");
  showTyping();
  const r = await api("/api/pipeline/next", { session_id: sessionId });
  hideTyping();
  if (r.err) { appendError(r.err); setStatus("就绪"); return; }
  if (r.blocked) { appendError(r.blocked_msg); setStatus("已拦截", false); return; }
  if (r.done) { pendingGates = 0; setStatus("闭环完成"); }
  else { setStatus("待确认"); }
  updateBadge();
  appendStageCard(r, r.next_stage_name && !r.done);
}

async function runAuto(task) {
  setStatus("生成中…");
  $("projectChatTitle").textContent = "审计项目 · 六阶段闭环";
  $("projectChatMeta").textContent = task;
  showTyping();
  const r = await api("/api/pipeline/auto", { session_id: sessionId, task });
  hideTyping();
  if (r.err) { appendError(r.err); setStatus("就绪"); return; }
  if (r.blocked) { appendError(r.blocked_msg); setStatus("已拦截", false); return; }
  (r.stages || []).forEach((s) => appendStageCard(s, false, true));
  if (r.phases) {
    updatePhaseStatus(r.phases);
    currentStage = "archive";
    renderStepNav(); renderFunctionCards();
  }
  pendingGates = 0; updateBadge();
  setStatus("闭环完成");
  toast("✅ 六阶段已一键跑通，可点击各阶段卡片回看产出");
}

function updateBadge() {
  $("todoBadge").textContent = pendingGates > 0 ? pendingGates + " 待确认" : "0 待确认";
}

// ==================== 结果视图 ====================
async function loadResultView(view) {
  if (view === "ledger") { await loadLedgerView(); return; }
  if (view === "problemlib") { await loadProblemLibView(); return; }
  const r = await api("/api/state?session_id=" + sessionId);
  if (view === "dashboard") renderDashboard(r);
  else if (view === "problems") renderProblems(r);
  else if (view === "report") renderReport(r);
  else if (view === "rect") renderRect(r);
}

function renderDashboard(r) {
  $("dashTask").textContent = r.task ? "任务：" + r.task : "尚未发起审计任务";
  const stats = r.stats || {};
  const cards = [
    { label: "已完成阶段", value: stats.stages_done || 0, unit: "/ 6" },
    { label: "取数异常", value: stats.anomalies || 0, unit: "条" },
    { label: "检索法规", value: stats.retrieved || 0, unit: "条" },
    { label: "数据源接入", value: stats.data_sources || 0, unit: "个" },
  ];
  $("statGrid").innerHTML = cards.map((c) => `<div class="stat-card"><div class="stat-card-label">${c.label}</div><div class="stat-card-value">${c.value}<span class="unit">${c.unit}</span></div></div>`).join("");
  renderPipeline($("dashPhasePipeline"), r.phases);

  const anomalies = r.anomalies || [];
  if (!anomalies.length) {
    $("dashAnomalies").innerHTML = '<div class="empty-hint">暂无异常数据 —— 先在工作台执行现场实施</div>';
  } else {
    const byType = {};
    anomalies.forEach((a) => { byType[a["类型"]] = (byType[a["类型"]] || 0) + 1; });
    $("dashAnomalies").innerHTML = '<table class="data-table"><thead><tr><th>异常类型</th><th>条数</th></tr></thead><tbody>' +
      Object.entries(byType).map(([k, v]) => `<tr><td>${escapeHtml(k)}</td><td>${v}</td></tr>`).join("") + "</tbody></table>";
  }
}

function renderProblems(r) {
  const risks = r.risks_list || [];
  if (!risks.length) {
    $("problemGrid").innerHTML = '<div class="empty-hint" style="grid-column:1/-1">暂无已定性问题 —— 先在工作台对差旅费/采购业务执行现场实施（需命中异常并检索到法规）</div>';
    return;
  }
  const levelColor = { "高": "red", "中": "yellow", "低": "blue" };
  $("problemGrid").innerHTML = risks.map((k, i) => {
    const level = k["风险等级"] || "中";
    const color = levelColor[level] || "blue";
    const levelText = level === "高" ? "高风险" : level === "中" ? "中风险" : "低风险";
    let laws = k["法规依据"] || [];
    if (!Array.isArray(laws)) laws = [laws];
    const lawTags = laws.filter((l) => l && String(l).trim()).map((l) => `<span class="law-ref">${escapeHtml(String(l))}</span>`).join("");
    const amount = (k["涉及金额"] && String(k["涉及金额"]).trim() && k["涉及金额"] !== "待核实")
      ? `<span class="amt">涉及金额：${escapeHtml(String(k["涉及金额"]))}</span>` : "";
    return `<div class="problem-card">
      <div class="problem-card-header"><span class="problem-card-title">${escapeHtml(k["风险类别"] || ("风险点" + (i + 1)))}</span><span class="badge ${color}">${levelText}</span></div>
      <div class="problem-card-body">${escapeHtml(k["风险描述"] || "")}</div>
      <div class="problem-card-footer">${amount}<span class="issue-tag ${color}">${levelText}</span>${lawTags}</div>
    </div>`;
  }).join("");
}

function renderReport(r) {
  $("reportContent").innerHTML = md(r.report || "");
}

function renderRect(r) {
  $("rectContent").innerHTML = md(r.rectification || "");
}

// ==================== 历史整改台账（真实数据） ====================
const ledgerState = { year: "", cat: "", status: "", keyword: "", page: 1, cats: [] };

function fmtMoney(v) {
  v = Number(v) || 0;
  if (v >= 100000000) return (v / 100000000).toFixed(2) + " 亿";
  if (v >= 10000) return (v / 10000).toFixed(1) + " 万";
  return v.toLocaleString() + " 元";
}

function truncate(s, n) {
  s = String(s || "");
  return s.length > n ? s.slice(0, n) + "…" : s;
}

async function loadLedgerView() {
  const q = new URLSearchParams({ page: ledgerState.page, size: 20 });
  if (ledgerState.year) q.set("year", ledgerState.year);
  if (ledgerState.cat) q.set("cat", ledgerState.cat);
  if (ledgerState.status) q.set("status", ledgerState.status);
  if (ledgerState.keyword) q.set("keyword", ledgerState.keyword);
  const r = await api("/api/ledger?" + q.toString());
  renderLedger(r);
}

function renderLedger(r) {
  const s = r.summary || {};
  const cards = [
    { label: "历史问题总数", value: s.total || 0, unit: "条" },
    { label: "已销号", value: s["已销号"] || 0, unit: "条" },
    { label: "未销号", value: s["未销号"] || 0, unit: "条" },
    { label: "问题涉及金额", value: fmtMoney(s["涉及金额"] || 0), unit: "" },
    { label: "整改成果金额", value: fmtMoney(s["成果金额"] || 0), unit: "" },
  ];
  $("ledgerStatGrid").innerHTML = cards.map((c) => `<div class="stat-card"><div class="stat-card-label">${c.label}</div><div class="stat-card-value">${c.value}<span class="unit">${c.unit}</span></div></div>`).join("");

  // 类别下拉（首次填充）
  if (!ledgerState.cats.length && r.categories) {
    ledgerState.cats = r.categories;
    $("ledgerCat").innerHTML = '<option value="">全部类别</option>' + r.categories.map(([c, n]) => `<option value="${escapeHtml(c)}">${escapeHtml(c)}（${n}）</option>`).join("");
  }
  $("ledgerCat").value = ledgerState.cat;

  renderLedgerTable(r.result.items);
  renderLedgerPager(r.result.total, r.result.page, r.result.size);
}

function renderLedgerTable(items) {
  if (!items.length) { $("ledgerTable").innerHTML = '<div class="empty-hint">没有匹配的历史问题</div>'; return; }
  const rows = items.map((it, idx) => {
    const xh = it["是否销号"];
    const badge = xh === "已销号" ? '<span class="badge green">已销号</span>' : '<span class="badge yellow">未销号</span>';
    return `<tr class="ledger-row" data-idx="${idx}">
      <td class="mono">${escapeHtml(it["问题编号"] || "")}</td>
      <td title="${escapeHtml(it["问题定性"] || "")}">${escapeHtml(truncate(it["问题定性"], 16))}</td>
      <td title="${escapeHtml(it["问题名称"] || "")}">${escapeHtml(truncate(it["问题名称"], 22))}</td>
      <td>${escapeHtml((it["问题类别1"] || "").replace(/^\*/, ""))}</td>
      <td>${escapeHtml(it["被审计单位"] || "—")}</td>
      <td class="num">${fmtMoney(it["问题涉及金额_元"])}</td>
      <td>${escapeHtml(truncate(it["整改状态"], 12))}</td>
      <td>${badge}</td>
    </tr>
    <tr class="ledger-detail" id="ledger-detail-${idx}" style="display:none"><td colspan="8"><div class="ledger-detail-body">
      <div class="ld-block"><span class="ld-label">问题原因分析</span><p>${escapeHtml(it["问题原因分析"] || "—")}</p></div>
      <div class="ld-block"><span class="ld-label">整改措施</span><p>${escapeHtml(it["整改措施"] || "—")}</p></div>
      <div class="ld-block"><span class="ld-label">审计建议</span><p>${escapeHtml(it["审计建议"] || "—")}</p></div>
    </div></td></tr>`;
  }).join("");
  $("ledgerTable").innerHTML = '<table class="data-table ledger-table"><thead><tr><th>问题编号</th><th>问题定性</th><th>问题名称</th><th>类别</th><th>被审计单位</th><th>涉及金额</th><th>整改状态</th><th>销号</th></tr></thead><tbody>' + rows + "</tbody></table>";
  document.querySelectorAll(".ledger-row").forEach((row) => {
    row.onclick = () => {
      const d = document.getElementById("ledger-detail-" + row.dataset.idx);
      d.style.display = d.style.display === "none" ? "" : "none";
    };
  });
}

function renderLedgerPager(total, page, size) {
  const pages = Math.max(1, Math.ceil(total / size));
  $("ledgerCount").textContent = "问题列表（共 " + total + " 条）";
  let html = '<button class="page-btn" data-p="' + (page - 1) + '" ' + (page <= 1 ? "disabled" : "") + '>‹ 上一页</button>';
  html += '<span class="page-info">' + page + " / " + pages + "</span>";
  html += '<button class="page-btn" data-p="' + (page + 1) + '" ' + (page >= pages ? "disabled" : "") + '>下一页 ›</button>';
  $("ledgerPager").innerHTML = html;
  document.querySelectorAll(".page-btn").forEach((b) => {
    b.onclick = () => { if (!b.disabled) { ledgerState.page = Number(b.dataset.p); loadLedgerView(); } };
  });
}

function initLedgerFilters() {
  $("ledgerSearch").onclick = () => {
    ledgerState.year = $("ledgerYear").value;
    ledgerState.cat = $("ledgerCat").value;
    ledgerState.status = $("ledgerStatus").value;
    ledgerState.keyword = $("ledgerKeyword").value.trim();
    ledgerState.page = 1;
    loadLedgerView();
  };
  $("ledgerReset").onclick = () => {
    $("ledgerYear").value = ""; $("ledgerCat").value = ""; $("ledgerStatus").value = ""; $("ledgerKeyword").value = "";
    ledgerState.year = ""; ledgerState.cat = ""; ledgerState.status = ""; ledgerState.keyword = ""; ledgerState.page = 1;
    loadLedgerView();
  };
  $("ledgerKeyword").onkeydown = (e) => { if (e.key === "Enter") $("ledgerSearch").onclick(); };
}

// ==================== 审计问题库（真实数据，脱敏） ====================
const problemLibState = { year: "", supervision: "", domain: "", keyword: "", page: 1, filled: false };

function fmtWan(v) {
  v = Number(v) || 0;
  if (v <= 0) return "—";
  if (v >= 100000000) return (v / 100000000).toFixed(2) + " 万亿";
  if (v >= 10000) return (v / 10000).toFixed(2) + " 亿";
  return v.toLocaleString(undefined, { maximumFractionDigits: 2 }) + " 万";
}

function domain1(s) {
  const d = String(s || "").trim();
  return d ? d.split(",")[0] : "—";
}

async function loadProblemLibView() {
  const q = new URLSearchParams({ page: problemLibState.page, size: 20 });
  if (problemLibState.year) q.set("year", problemLibState.year);
  if (problemLibState.supervision) q.set("supervision", problemLibState.supervision);
  if (problemLibState.domain) q.set("domain", problemLibState.domain);
  if (problemLibState.keyword) q.set("keyword", problemLibState.keyword);
  const r = await api("/api/problems?" + q.toString());
  renderProblemLib(r);
}

function renderProblemLib(r) {
  const s = r.summary || {};
  const cards = [
    { label: "问题总数", value: s.total || 0, unit: "条" },
    { label: "有金额问题", value: s["有金额问题数"] || 0, unit: "条" },
    { label: "监督职能", value: s["监督职能数"] || 0, unit: "类" },
    { label: "业务领域", value: s["业务领域数"] || 0, unit: "类" },
  ];
  $("problemStatGrid").innerHTML = cards.map((c) => `<div class="stat-card"><div class="stat-card-label">${c.label}</div><div class="stat-card-value">${c.value}<span class="unit">${c.unit}</span></div></div>`).join("");

  // 下拉项（首次填充）
  const o = r.options || {};
  if (!problemLibState.filled) {
    problemLibState.filled = true;
    $("plYear").innerHTML = '<option value="">全部年度</option>' + (o.years || []).map(([y, n]) => `<option value="${escapeHtml(y)}">${escapeHtml(y)}（${n}）</option>`).join("");
    $("plFunc").innerHTML = '<option value="">全部监督职能</option>' + (o.funcs || []).map(([f, n]) => `<option value="${escapeHtml(f)}">${escapeHtml(f)}（${n}）</option>`).join("");
    $("plDomain").innerHTML = '<option value="">全部业务领域</option>' + (o.domains || []).map(([d, n]) => `<option value="${escapeHtml(d)}">${escapeHtml(d)}（${n}）</option>`).join("");
  }
  $("plYear").value = problemLibState.year;
  $("plFunc").value = problemLibState.supervision;
  $("plDomain").value = problemLibState.domain;

  renderProblemTable(r.result.items);
  renderProblemPager(r.result.total, r.result.page, r.result.size);
}

function renderProblemTable(items) {
  if (!items.length) { $("problemTable").innerHTML = '<div class="empty-hint">没有匹配的问题</div>'; return; }
  const rows = items.map((it, idx) => {
    return `<tr class="ledger-row" data-idx="${idx}">
      <td class="mono">${escapeHtml(it["年度"] || "—")}</td>
      <td>${escapeHtml(it["监督职能"] || "—")}</td>
      <td title="${escapeHtml(it["问题定性"] || "")}">${escapeHtml(truncate(it["问题定性"], 14))}</td>
      <td title="${escapeHtml(it["问题名称"] || "")}">${escapeHtml(truncate(it["问题名称"], 26))}</td>
      <td title="${escapeHtml(it["业务领域"] || "")}">${escapeHtml(truncate(domain1(it["业务领域"]), 16))}</td>
      <td class="num">${fmtWan(it["问题涉及金额_万元"])}</td>
      <td>${escapeHtml(truncate(it["问题涉及单位"], 14))}</td>
      <td>${escapeHtml(truncate(it["整改责任主体"], 14))}</td>
    </tr>
    <tr class="ledger-detail" id="pl-detail-${idx}" style="display:none"><td colspan="8"><div class="ledger-detail-body">
      <div class="ld-block"><span class="ld-label">问题名称</span><p>${escapeHtml(it["问题名称"] || "—")}</p></div>
      <div class="ld-block"><span class="ld-label">问题定性</span><p>${escapeHtml(it["问题定性"] || "—")}</p></div>
      <div class="ld-block"><span class="ld-label">审计项目 / 流程</span><p>${escapeHtml(it["项目名称"] || "—")}　|　${escapeHtml(it["流程名称"] || "—")}　|　KCP：${escapeHtml(it["KCP名称"] || "—")}</p></div>
      <div class="ld-block"><span class="ld-label">责任归属</span><p>整改：${escapeHtml(it["整改责任主体"] || "—")}　管理：${escapeHtml(it["管理责任主体"] || "—")}　监督：${escapeHtml(it["监督责任主体"] || "—")}</p></div>
      <div class="ld-block"><span class="ld-label">被监督对象 / 问题涉及单位</span><p>${escapeHtml(it["被监督对象"] || "—")}　|　${escapeHtml(it["问题涉及单位"] || "—")}</p></div>
    </div></td></tr>`;
  }).join("");
  $("problemTable").innerHTML = '<table class="data-table ledger-table"><thead><tr><th>年度</th><th>监督职能</th><th>问题定性</th><th>问题名称</th><th>业务领域</th><th>涉及金额</th><th>问题涉及单位</th><th>整改责任主体</th></tr></thead><tbody>' + rows + "</tbody></table>";
  document.querySelectorAll(".ledger-row").forEach((row) => {
    row.onclick = () => {
      const d = document.getElementById("pl-detail-" + row.dataset.idx);
      d.style.display = d.style.display === "none" ? "" : "none";
    };
  });
}

function renderProblemPager(total, page, size) {
  const pages = Math.max(1, Math.ceil(total / size));
  $("plCount").textContent = "问题列表（共 " + total + " 条）";
  let html = '<button class="page-btn" data-p="' + (page - 1) + '" ' + (page <= 1 ? "disabled" : "") + '>‹ 上一页</button>';
  html += '<span class="page-info">' + page + " / " + pages + "</span>";
  html += '<button class="page-btn" data-p="' + (page + 1) + '" ' + (page >= pages ? "disabled" : "") + '>下一页 ›</button>';
  $("problemPager").innerHTML = html;
  document.querySelectorAll("#problemPager .page-btn").forEach((b) => {
    b.onclick = () => { if (!b.disabled) { problemLibState.page = Number(b.dataset.p); loadProblemLibView(); } };
  });
}

function initProblemLibFilters() {
  $("plSearch").onclick = () => {
    problemLibState.year = $("plYear").value;
    problemLibState.supervision = $("plFunc").value;
    problemLibState.domain = $("plDomain").value;
    problemLibState.keyword = $("plKeyword").value.trim();
    problemLibState.page = 1;
    loadProblemLibView();
  };
  $("plReset").onclick = () => {
    $("plYear").value = ""; $("plFunc").value = ""; $("plDomain").value = ""; $("plKeyword").value = "";
    problemLibState.year = ""; problemLibState.supervision = ""; problemLibState.domain = ""; problemLibState.keyword = ""; problemLibState.page = 1;
    loadProblemLibView();
  };
  $("plKeyword").onkeydown = (e) => { if (e.key === "Enter") $("plSearch").onclick(); };
}

// ==================== 视图切换 ====================
function switchView(view) {
  currentView = view;
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  $("view-" + view).classList.add("active");
  document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.view === view));
  const title = NAV.find((n) => n.view === view).label;
  $("breadcrumb").innerHTML = '<span class="crumb">审计数字员工</span><span class="crumb-sep">/</span><span class="crumb active">' + title + "</span>";
  if (view !== "chat") loadResultView(view);
}

// ==================== 初始化 ====================
function renderNav() {
  $("navSection").innerHTML = NAV.map((n) => `<div class="nav-item ${n.view === "chat" ? "active" : ""}" data-view="${n.view}"><span class="nav-icon">${n.icon}</span>${n.label}</div>`).join("");
  document.querySelectorAll(".nav-item").forEach((n) => n.onclick = () => switchView(n.dataset.view));
}

function renderRoles() {
  $("roleSwitcher").innerHTML = ROLES.map((r, i) => `<span class="role-chip ${i === 0 ? "active" : ""}" data-i="${i}">${r.name}</span>`).join("");
  document.querySelectorAll(".role-chip").forEach((c) => c.onclick = () => {
    document.querySelectorAll(".role-chip").forEach((x) => x.classList.remove("active"));
    c.classList.add("active");
    const r = ROLES[+c.dataset.i];
    $("currentRoleAvatar").textContent = r.avatar;
    $("currentRoleName").textContent = r.name;
    $("currentRoleTitle").textContent = r.title;
  });
}

function renderScenarios() {
  $("quickScenarios").innerHTML = SCENARIOS.map((s) => `<span class="quick-chip">${s.label}</span>`).join("");
  document.querySelectorAll(".quick-chip").forEach((c, i) => c.onclick = () => { $("chatInput").value = SCENARIOS[i].text; $("chatInput").focus(); });
}

function bindEvents() {
  $("loginBtn").onclick = login;
  $("loginPassword").onkeydown = (e) => { if (e.key === "Enter") login(); };
  $("sendBtn").onclick = onSend;
  $("chatInput").onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); onSend(); } };
  $("newProjectBtn").onclick = newProject;
  $("autoBtn").onclick = () => {
    const t = $("chatInput").value.trim() || "审计 对某公司XX天然气管道工程项目2023至2025年度竣工决算情况开展竣工决算审计";
    $("chatInput").value = "";
    appendUser("⚡ 一键全流程：" + t);
    runAuto(t);
  };
  $("auditApprovalMode").onchange = onApprovalChange;
}

function onApprovalChange() {
  const v = $("auditApprovalMode").value;
  if (v === "request") { toast("已切换：请求审批 —— 每一步人工确认（MVP 已实现）"); }
  else if (v === "delegate") { toast("「替我审批」为规划中模式，MVP 仅实现「请求审批」"); $("auditApprovalMode").value = "request"; }
  else if (v === "autonomous") { toast("「完全自主审批」为规划中模式，MVP 仅实现「请求审批」"); $("auditApprovalMode").value = "request"; }
}

async function login() {
  $("loginOverlay").style.display = "none";
  $("app").style.display = "flex";
  if (!sessionId) { const r = await api("/api/session"); sessionId = r.session_id; }
}

async function newProject() {
  const r = await api("/api/session");
  sessionId = r.session_id;
  pendingGates = 0; updateBadge();
  $("chatMessages").innerHTML = '<div class="chat-empty" id="chatEmpty"><div class="chat-empty-icon">审</div><h4>输入一个审计任务开始</h4><p>例如：对某国有企业 2025 年度差旅费报销情况开展专项审计</p></div>';
  $("projectChatTitle").textContent = "审计任务工作台";
  $("projectChatMeta").textContent = "输入审计任务，六阶段逐步执行，每步设人工闸门";
  setStatus("就绪");
  phaseStatus = {}; renderStepNav(); renderFunctionCards();
  $("reportContent").innerHTML = ""; $("rectContent").innerHTML = "";
  $("problemGrid").innerHTML = '<div class="empty-hint" style="grid-column:1/-1">暂无问题</div>';
  toast("已新建审计任务会话");
}

// ==================== 启动 ====================
renderNav();
renderRoles();
renderScenarios();
bindEvents();
initLedgerFilters();
initProblemLibFilters();
renderStepNav(); renderFunctionCards();
updateBadge();

// toast 样式（动态注入）
const style = document.createElement("style");
style.textContent = `
.toast { position: fixed; top: 20px; left: 50%; transform: translateX(-50%) translateY(-20px); background: var(--gray-900); color: #fff; padding: 10px 18px; border-radius: 8px; font-size: 13px; z-index: 999; opacity: 0; transition: all .3s; max-width: 80vw; }
.toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
.data-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.data-table th, .data-table td { border: 1px solid var(--gray-200); padding: 8px 10px; text-align: left; }
.data-table th { background: var(--gray-50); }
`;
document.head.appendChild(style);
