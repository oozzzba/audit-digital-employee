"""取数工具：数据源注册表 + 规则化异常筛查。

这是五类工具里的「取数 + 分析」工具，设计成可横向扩展：
  - 每个审计场景 = 一份数据文件 + 一套筛查规则函数。
  - 新增场景（采购、资产、工程…）只需两步：
      1. 在 DATA_SOURCES 里注册一行：关键词 → 数据文件 + 规则函数
      2. 写一个 detect_xxx(rows) 规则函数
  - 调度逻辑（match_source / load_rows / detect_anomalies）完全不用改。

MVP 用 CSV 模拟数据源；真实场景这一步通过 MCP（模型上下文协议）
标准化工具网关对接财务系统 / 数据仓库。
"""
import csv
import os
from collections import defaultdict

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# 采购招标限额（元）：超过此金额须走招标程序（简化示例）
PROCUREMENT_BID_LIMIT = 1000000  # 100 万
# 供应商集中度预警线：单一供应商累计金额占比超过此值则预警
SUPPLIER_CONCENTRATION_LIMIT = 0.4


# ============ 数据源一：差旅费报销 ============
def detect_travel(rows):
    """差旅费报销筛查规则：票据缺失 / 超标准 / 重复报销 / 抬头异常"""
    anomalies = []
    summary = defaultdict(int)
    seen = defaultdict(list)  # 重复报销检测：(员工, 日期, 目的地) -> 记录列表

    summary["总记录数"] = len(rows)
    summary["总金额"] = round(sum(float(r["报销总金额"]) for r in rows), 2)

    for r in rows:
        amount = float(r["报销总金额"])
        limit = float(r["标准上限"])

        # 规则 1：票据缺失（无票据 / 收据）
        if r["票据类型"] in ("无票据", "收据"):
            anomalies.append({
                "类型": "票据缺失", "主体": r["员工"], "日期": r["出差日期"], "金额": amount,
                "说明": f"事由「{r['出差事由']}」（{r['目的地']}）票据类型为「{r['票据类型']}」",
            })
            summary["票据缺失"] += 1

        # 规则 2：超标准报销
        if amount > limit:
            anomalies.append({
                "类型": "超标准报销", "主体": r["员工"], "日期": r["出差日期"], "金额": amount,
                "说明": f"事由「{r['出差事由']}」（{r['目的地']}）金额超出上限 {limit} 元",
            })
            summary["超标准报销"] += 1

        # 规则 4：发票抬头异常
        if "抬头" in r["备注"]:
            anomalies.append({
                "类型": "发票抬头异常", "主体": r["员工"], "日期": r["出差日期"], "金额": amount,
                "说明": f"事由「{r['出差事由']}」（{r['目的地']}）{r['备注']}",
            })
            summary["发票抬头异常"] += 1

        # 规则 3（先记录，遍历完判定）
        seen[(r["员工"], r["出差日期"], r["目的地"])].append(r)

    for (emp, date, dest), group in seen.items():
        if len(group) >= 2:
            r = group[0]
            anomalies.append({
                "类型": "重复报销", "主体": emp, "日期": date, "金额": float(r["报销总金额"]),
                "说明": f"事由「{r['出差事由']}」（{dest}）同一行程报销 {len(group)} 次",
            })
            summary["重复报销"] += 1

    summary["异常条数"] = len(anomalies)
    return anomalies, dict(summary)


# ============ 数据源二：物资采购 ============
def detect_procurement(rows):
    """物资采购筛查规则：应招标未招标 / 化整为零 / 供应商集中 / 关联交易"""
    anomalies = []
    summary = defaultdict(int)
    supplier_total = defaultdict(float)   # 供应商累计金额（用于集中度）
    split_groups = defaultdict(list)      # 化整为零检测：(供应商, 部门, 年月) -> 记录列表

    summary["总记录数"] = len(rows)
    summary["总金额"] = round(sum(float(r["采购金额"]) for r in rows), 2)

    for r in rows:
        amount = float(r["采购金额"])

        # 规则 1：应招标未招标（金额超限额且未走招标）
        if amount > PROCUREMENT_BID_LIMIT and r["采购方式"] != "招标":
            anomalies.append({
                "类型": "应招标未招标", "主体": r["供应商"], "日期": r["采购日期"], "金额": amount,
                "说明": f"「{r['采购事项']}」金额 {amount} 元超招标限额，采购方式为「{r['采购方式']}」",
            })
            summary["应招标未招标"] += 1

        # 规则 4：供应商关联交易
        if "关联" in r["备注"] or "关联" in r["供应商"]:
            anomalies.append({
                "类型": "供应商关联交易", "主体": r["供应商"], "日期": r["采购日期"], "金额": amount,
                "说明": f"「{r['采购事项']}」供应商存在关联关系",
            })
            summary["供应商关联交易"] += 1

        supplier_total[r["供应商"]] += amount
        split_groups[(r["供应商"], r["采购部门"], r["采购日期"][:7])].append(r)

    # 规则 3：供应商集中（累计金额占比超预警线）
    total = summary["总金额"] or 1
    for supplier, amt in supplier_total.items():
        if amt / total > SUPPLIER_CONCENTRATION_LIMIT:
            anomalies.append({
                "类型": "供应商集中", "主体": supplier, "日期": "", "金额": round(amt, 2),
                "说明": f"累计采购金额占比 {round(amt / total * 100, 1)}%，超 {int(SUPPLIER_CONCENTRATION_LIMIT * 100)}% 预警线",
            })
            summary["供应商集中"] += 1

    # 规则 2：化整为零规避招标（同一供应商+部门+当月，多笔小单累计超限额）
    for (supplier, dept, month), group in split_groups.items():
        amounts = [float(r["采购金额"]) for r in group]
        total_amt = sum(amounts)
        if len(group) >= 2 and total_amt > PROCUREMENT_BID_LIMIT and all(a < PROCUREMENT_BID_LIMIT for a in amounts):
            anomalies.append({
                "类型": "化整为零规避招标", "主体": supplier, "日期": month, "金额": round(total_amt, 2),
                "说明": f"{dept} 当月 {len(group)} 笔小单累计 {round(total_amt, 2)} 元超招标限额",
            })
            summary["化整为零规避招标"] += 1

    summary["异常条数"] = len(anomalies)
    return anomalies, dict(summary)


# ============ 数据源注册表 ============
# 新增场景时，在这里加一行即可，调度逻辑不动。
DATA_SOURCES = {
    "差旅": {"file": "差旅费报销明细.csv", "detector": detect_travel},
    "采购": {"file": "物资采购明细.csv", "detector": detect_procurement},
}


def match_source(audit_scope):
    """根据审计范围匹配数据源，返回 (source_key, meta) 或 (None, None)"""
    if not audit_scope:
        return None, None
    for keyword, meta in DATA_SOURCES.items():
        if keyword in audit_scope:
            return keyword, meta
    return None, None


def load_rows(filename):
    """读取 CSV 数据源，返回 list[dict]（每行一条记录）"""
    path = os.path.join(DATA_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def detect_anomalies(meta, rows):
    """按数据源路由到对应的规则函数"""
    return meta["detector"](rows)


def format_anomalies(anomalies, summary):
    """把筛查结果整理成给 LLM 阅读的文本（与具体数据源无关）"""
    if not anomalies:
        return "（未命中异常规则）"

    by_type = defaultdict(list)
    for a in anomalies:
        by_type[a["类型"]].append(a)

    lines = [
        f"数据源共 {summary.get('总记录数', 0)} 条记录，合计金额 {summary.get('总金额', 0)} 元；"
        f"命中异常 {summary.get('异常条数', 0)} 条。"
    ]
    for typ, items in by_type.items():
        lines.append(f"\n【{typ}】{len(items)} 条：")
        for a in items:
            meta = a["主体"]
            if a.get("日期"):
                meta += f" {a['日期']}"
            lines.append(f"  - {meta}（{a['金额']}元）—— {a['说明']}")
    return "\n".join(lines)
