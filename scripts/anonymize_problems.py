"""审计问题库脱敏脚本（真实数据 → 可展示/可入库的脱敏 CSV）

数据源：/Users/mac/作品集/业务真实数据/问题清单信息导出.xlsx
产出：code/data/审计问题库_脱敏.csv

脱敏范围（只去标识，保留业务价值内容）：
  - 立项单位 / 实施单位 / 被监督对象 / 问题涉及单位 / 整改责任主体 /
    管理责任主体 / 监督责任主体 → 单位A/B/C…
  - 项目名称、问题名称（自由文本）中内嵌的单位/机构名 → 同一代号
  - 母公司「国家管网」全称/简称（含文中简称「集团」）→ 某集团
  - 经济责任审计对象中的个人姓名（如「张平」「赵庆春」）→ 责任人A/B/C…

保留（业务价值、非标识）：
  年度、监督职能、业务领域、问题定性、流程名称、KCP编号、KCP名称、
  问题名称（去标识后）、问题涉及金额（万元）。

已知局限（与整改台账脱敏脚本一致，可接受）：
  - 管道线路名 / 站场名（如「兰成渝成品油管道」「秦沈线」）等基础设施地理标识，
    不属于机构名，不脱敏——不暴露公司主体、不暴露个人。

用法：
    python3 scripts/anonymize_problems.py
"""
import csv
import os
import re
from collections import Counter

import openpyxl

# 原始问题清单位置（真实数据，不进项目目录，不随演示分发）
RAW_XLSX = "/Users/mac/作品集/业务真实数据/问题清单信息导出.xlsx"
OUT_CSV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "审计问题库_脱敏.csv")

HEADER_ROWS = 2
SHEET = "Sheet1"

# 列索引（0-based，第 1、2 行是表头）
C_YEAR = 1        # 年度
C_FUNC = 2        # 监督职能
C_PROJECT = 3     # 项目名称（自由文本，需替换内嵌单位/人名）
C_UNITS = [4, 5, 6, 7, 15, 16, 17]   # 立项单位 / 实施单位 / 被监督对象 / 问题涉及单位 / 整改责任主体 / 管理责任主体 / 监督责任主体
C_DOMAIN = 8      # 业务领域
C_QUALIF = 9      # 问题定性
C_PROC = 10       # 流程名称
C_KCP_NO = 11     # KCP编号
C_KCP_NAME = 12   # KCP名称
C_PROBLEM = 13    # 问题名称（自由文本，需替换内嵌单位/人名）
C_AMOUNT = 14     # 问题涉及金额（万元）

# 复合串分隔符：出现这些字符的单位格，按「逐词替换」处理，保留部门结构
COMPOSITE_SEP = "/、-"


def _code(n):
    """序号 → Excel 列号风格代号：1→A, 26→Z, 27→AA"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _g(v):
    """单元格 → 干净字符串（None/空 → ""）"""
    if v is None:
        return ""
    return str(v).strip()


def _num(v):
    """金额字段 → float（容错，兼容千分位逗号）"""
    s = _g(v)
    if not s:
        return 0.0
    s = s.replace(",", "")
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _is_composite(text):
    return any(sep in text for sep in COMPOSITE_SEP)


# 机构名后缀：分两类，避免「研究总院分公司」被拆成「研究总院」+「分公司」。
#  - 公司类：以公司/分公司/子公司/事务所等法律形式结尾（优先整体匹配）
#  - 机构类：以中心/局/处/院/队/站/室/所/委结尾（无公司后缀时才用）
# 注意：不含裸「集团」——本数据中的「集团」均指母公司，由 force_replace 统一替换为「某集团」，
# 避免「2021年第一阶段集团」这类「阶段+集团」的误匹配。
ORG_COMPANY_RE = re.compile(
    r"[一-龥A-Za-z0-9]{2,30}?"
    r"(?:集团有限公司|集团股份有限公司|集团有限责任公司|集团公司|股份有限公司|有限责任公司|事务所|分公司|子公司|公司)"
)
ORG_INSTITUTE_RE = re.compile(
    r"[一-龥A-Za-z0-9]{2,30}?"
    r"(?:中心|局|处|院|队|站|室|所|委)"
)


def _find_orgs(text):
    """从自由文本/复合串里捞出机构名（公司类优先，机构类排除已被公司类覆盖的部分）"""
    company = [m for m in ORG_COMPANY_RE.finditer(text)]
    orgs = set(m.group(0) for m in company)

    def covered(pos):
        return any(m.start() <= pos < m.end() for m in company)

    for m in ORG_INSTITUTE_RE.finditer(text):
        if not covered(m.start()):
            orgs.add(m.group(0))
    return orgs


# 经济责任审计对象（个人姓名）：标题 + 姓名 +（同志） +（离任/任中） + 经济责任审计
ECON_AUDIT_RE = re.compile(
    r"([一-龥]{2,3})(?:同志)?(?:进行)?(?:离任|任中|任内|任期)?经济责任审计"
)
_PERSON_STOPWORDS = ("负责", "领导", "代表", "董事", "经理", "同志", "书记")


def _find_persons(text):
    """从文本里捞出经济责任审计目标人物姓名"""
    names = set()
    for m in ECON_AUDIT_RE.finditer(text):
        name = m.group(1)
        if any(sw in name for sw in _PERSON_STOPWORDS):
            continue
        names.add(name)
    return names


def _short_alias(name):
    """从完整单位名推导「短名别名」（去母公司前缀 + 法律形式后缀，仅保留管道/储运类名词）"""
    n = name
    for p in ("国家石油天然气管网集团有限公司", "国家石油天然气管网集团",
              "国家管网集团有限公司", "国家管网集团", "国家管网"):
        if n.startswith(p):
            n = n[len(p):]
            break
    n = re.sub(r"(有限责任公司|股份有限公司|有限公司|公司|分公司|子公司)$", "", n)
    if len(n) >= 3 and re.search(r"(管道|储运|原油|输气|气东输|管网)", n):
        return n
    return None


def _build_replacer(pairs):
    """单遍正则替换器：按 key 长度降序组成一个交替式，re.sub 不会重扫替换结果，
    从而避免「集团→某集团」这类自引用替换产生二次替换。"""
    pairs = sorted(pairs, key=lambda kv: len(kv[0]), reverse=True)
    mapping = {src: dst for src, dst in pairs}
    pattern = "|".join(re.escape(src) for src, _ in pairs)
    compiled = re.compile(pattern)

    def replacer(text):
        return compiled.sub(lambda m: mapping[m.group(0)], text)

    return replacer


def main():
    wb = openpyxl.load_workbook(RAW_XLSX, read_only=True, data_only=True)
    ws = wb[SHEET]

    # 1) 收集需要脱敏的取值
    units = Counter()        # 纯单位名（整格映射）
    extra_orgs = Counter()   # 复合串 + 问题名称 + 项目名称里捞出的机构名
    persons = Counter()      # 经济责任审计对象姓名
    rows = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i < HEADER_ROWS:
            continue
        if _g(row[C_YEAR]) == "" and _g(row[C_PROBLEM]) == "":  # 空行
            continue
        rows.append(row)
        for j in C_UNITS:
            cell = _g(row[j])
            if not cell:
                continue
            if _is_composite(cell):
                for org in _find_orgs(cell):
                    extra_orgs[org] += 1
            else:
                units[cell] += 1
        # 问题名称 / 项目名称 / 问题定性 自由文本里的机构名、人名
        for ci in (C_PROBLEM, C_PROJECT, C_QUALIF):
            text = _g(row[ci])
            for org in _find_orgs(text):
                extra_orgs[org] += 1
            for name in _find_persons(text):
                persons[name] += 1

    # 2) 代号映射（按频次排序）
    unit_map = {name: "单位" + _code(i) for i, (name, _) in enumerate(units.most_common(), 1)}
    # 短名别名（西部管道 / 北方管道 等）→ 复用同一代号
    for name in list(unit_map.keys()):
        alias = _short_alias(name)
        if alias and alias not in unit_map:
            unit_map[alias] = unit_map[name]
    next_idx = len(unit_map) + 1
    for name, _ in extra_orgs.most_common():
        if name not in unit_map:
            unit_map[name] = "单位" + _code(next_idx)
            next_idx += 1
    person_map = {name: "责任人" + _code(i) for i, (name, _) in enumerate(persons.most_common(), 1)}

    # 母公司全称/简称（顶层标识；含文中简称「集团」→ 某集团）
    force_replace = [
        ("国家石油天然气管网集团有限公司", "某集团公司"),
        ("国家石油天然气管网集团", "某集团公司"),
        ("国家管网集团有限公司", "某集团公司"),
        ("管网总部", "某集团总部"),
        ("管网集团", "某集团"),
        ("国家管网集团", "某集团"),
        ("国家管网", "某集团"),
        ("集团", "某集团"),
    ]
    replace_pairs = force_replace + list(unit_map.items())
    anonymize_text = _build_replacer(replace_pairs)

    def anonymize_persons(text):
        def repl(m):
            code = person_map.get(m.group(1))
            return m.group(0).replace(m.group(1), code, 1) if code else m.group(0)
        return ECON_AUDIT_RE.sub(repl, text)

    # 3) 逐行脱敏 + 写入 CSV
    out_cols = [
        "年度", "监督职能", "项目名称", "立项单位", "实施单位", "被监督对象",
        "问题涉及单位", "业务领域", "问题定性", "流程名称", "KCP编号", "KCP名称",
        "问题名称", "问题涉及金额_万元", "整改责任主体", "管理责任主体", "监督责任主体",
    ]
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(out_cols)
        for row in rows:
            def unit_cell(j):
                cell = _g(row[j])
                if not cell:
                    return ""
                if _is_composite(cell):
                    return anonymize_text(cell)
                return unit_map.get(cell, anonymize_text(cell))

            w.writerow([
                _g(row[C_YEAR]),
                _g(row[C_FUNC]),
                anonymize_persons(anonymize_text(_g(row[C_PROJECT]))),
                unit_cell(C_UNITS[0]),
                unit_cell(C_UNITS[1]),
                unit_cell(C_UNITS[2]),
                unit_cell(C_UNITS[3]),
                _g(row[C_DOMAIN]),
                anonymize_text(_g(row[C_QUALIF])),
                _g(row[C_PROC]),
                _g(row[C_KCP_NO]),
                _g(row[C_KCP_NAME]),
                anonymize_persons(anonymize_text(_g(row[C_PROBLEM]))),
                round(_num(row[C_AMOUNT]), 2),
                unit_cell(C_UNITS[4]),
                unit_cell(C_UNITS[5]),
                unit_cell(C_UNITS[6]),
            ])

    print(f"脱敏完成 → {OUT_CSV}")
    print(f"  总行数: {len(rows)}")
    print(f"  单位代号 {len(unit_map)} → 单位A..单位{_code(len(unit_map))}")
    print(f"  人名代号 {len(person_map)} → 责任人A..责任人{_code(len(person_map))}")


if __name__ == "__main__":
    main()
