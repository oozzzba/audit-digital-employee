"""整改台账脱敏脚本（真实数据 → 可展示/可入库的脱敏 CSV）

数据源：/Users/mac/作品集/业务真实数据/2023-2025年审计发现问题整改台账（截至2025年12月31号）.xlsx
产出：code/data/整改台账_脱敏.csv

脱敏范围（只去标识，保留业务价值内容）：
  - 立项单位 / 被审计单位 / 整改责任主体 / 整改责任部门 → 单位A/B/C…
  - 整改责任人（人名）→ 责任人A/B/C…
  - 项目名称 → 审计项目A/B/C…
  - 问题编号（含项目代码）→ W-PROJ-{年}-{序号}
  - 自由文本（问题名称/原因分析/整改措施/审计建议）中的单位名、人名 → 同一代号

保留（业务价值、非标识）：
  问题类别1/2/3、问题定性、问题来源、整改状态、是否销号、涉及金额、整改时限、
  整改措施、审计建议、成果金额、审计成果、问责等。

用法：
    python3 scripts/anonymize_ledger.py
"""
import csv
import os
import re
from collections import Counter

import openpyxl

# 原始台账位置（真实数据，不进项目目录，不随演示分发）
RAW_XLSX = "/Users/mac/作品集/业务真实数据/2023-2025年审计发现问题整改台账（截至2025年12月31号）.xlsx"
OUT_CSV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "整改台账_脱敏.csv")

# 表头在第 1、2 行（0-indexed 0、1），数据从第 3 行（index 2）开始
HEADER_ROWS = 2
SHEETS = ["2023", "2024", "2025"]

# 列索引（0-based，三个 sheet 一致）
C_PROJECT = 2      # 项目名称
C_UNITS = [3, 4, 5, 16]   # 立项单位 / 被审计单位 / 整改责任主体 / 整改责任部门
C_PERSON = 17      # 整改责任人
C_TEXT = [8, 22, 23, 24]   # 问题名称 / 原因分析 / 整改措施 / 审计建议（自由文本，需替换内嵌单位/人名）
C_AMOUNT_YUAN = 15         # 问题涉及金额（元）
C_RESULT_COLS = list(range(25, 35))  # 成果金额：25..34 九个金额字段
C_SYSTEM = 35      # 新建或修订制度
C_PROCESS = 36     # 优化或完善流程
C_HANDLE = 39      # 处理（个）
C_TRANSFER = 40    # 移送（个）


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
    """金额字段 → float（容错）"""
    s = _g(v)
    if not s:
        return 0.0
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


# 机构名后缀：自由文本里出现的第三方公司/单位（施工方、供应商、物业、集团等）
ORG_RE = re.compile(
    r"[一-龥A-Za-z0-9]{2,30}?"
    r"(?:集团有限公司|股份有限公司|有限责任公司|集团|分公司|子公司|公司|中心|局|处|院|队|站|室|所|委)"
)


def _find_orgs(text):
    """从自由文本里捞出机构名（去标识兜底，含第三方公司/单位）"""
    return set(m.group(0) for m in ORG_RE.finditer(text))


def main():
    wb = openpyxl.load_workbook(RAW_XLSX, read_only=True, data_only=True)

    # 1) 收集需要脱敏的取值
    units, persons, projects = Counter(), Counter(), Counter()
    extra_orgs = Counter()
    rows = []
    for sheet in SHEETS:
        ws = wb[sheet]
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i < HEADER_ROWS:
                continue
            if _g(row[8]) == "" and _g(row[0]) == "":  # 空行
                continue
            rows.append((sheet, row))
            for j in C_UNITS:
                if _g(row[j]):
                    units[_g(row[j])] += 1
            if _g(row[C_PERSON]):
                persons[_g(row[C_PERSON])] += 1
            if _g(row[C_PROJECT]):
                projects[_g(row[C_PROJECT])] += 1
            for j in C_TEXT:
                for org in _find_orgs(_g(row[j])):
                    extra_orgs[org] += 1

    # 2) 建立代号映射（按出现频次排序；替换时按长度降序，避免短名误伤长名）
    unit_map = {name: "单位" + _code(i) for i, (name, _) in enumerate(units.most_common(), 1)}
    # 自由文本里捞出的第三方机构名，续在结构化单位之后编号
    next_idx = len(unit_map) + 1
    for name, _ in extra_orgs.most_common():
        if name not in unit_map:
            unit_map[name] = "单位" + _code(next_idx)
            next_idx += 1
    person_map = {name: "责任人" + _code(i) for i, (name, _) in enumerate(persons.most_common(), 1)}
    project_map = {name: "审计项目" + _code(i) for i, (name, _) in enumerate(projects.most_common(), 1)}

    # 母公司全称/简称（顶层标识，强制替换，避免正则漏掉简称）
    force_replace = [
        ("国家石油天然气管网集团有限公司", "某集团公司"),
        ("国家石油天然气管网集团", "某集团公司"),
        ("国家管网集团有限公司", "某集团公司"),
        ("国家管网集团", "某集团"),
        ("国家管网", "某集团"),
    ]
    replace_pairs = sorted(
        force_replace + list(unit_map.items()) + list(person_map.items()),
        key=lambda kv: len(kv[0]),
        reverse=True,
    )

    def anonymize_text(text):
        for src, dst in replace_pairs:
            if src in text:
                text = text.replace(src, dst)
        return text

    # 3) 逐行脱敏 + 写入 CSV
    out_cols = [
        "年度", "问题编号", "项目名称", "被审计单位", "问题名称", "问题来源",
        "问题类别1", "问题类别2", "问题类别3", "问题定性", "问题涉及金额_元",
        "整改责任部门", "整改状态", "是否销号", "整改时限",
        "问题原因分析", "整改措施", "审计建议", "整改成果金额_元",
        "新建或修订制度", "优化或完善流程", "处理_个", "移送_个",
    ]
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    seq = Counter()  # 每年一个全局序号，用于问题编号

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(out_cols)
        for sheet, row in rows:
            year = int(sheet)
            seq[year] += 1

            problem_no = f"W-PROJ-{year}-{seq[year]:03d}"
            project = project_map.get(_g(row[C_PROJECT]), "审计项目")
            audited = unit_map.get(_g(row[4]), _g(row[4]))
            dept = unit_map.get(_g(row[C_UNITS[3]]), _g(row[C_UNITS[3]]))

            amount = _num(row[C_AMOUNT_YUAN])
            result_amount = sum(_num(row[j]) for j in C_RESULT_COLS)

            w.writerow([
                year,
                problem_no,
                project,
                audited,
                anonymize_text(_g(row[8])),
                _g(row[9]),
                _g(row[10]),
                _g(row[11]),
                _g(row[12]),
                _g(row[13]),
                int(amount),
                dept,
                _g(row[18]),
                _g(row[19]),
                _g(row[20])[:10] if _g(row[20]) else "",
                anonymize_text(_g(row[22])),
                anonymize_text(_g(row[23])),
                anonymize_text(_g(row[24])),
                int(result_amount),
                _g(row[C_SYSTEM]),
                _g(row[C_PROCESS]),
                _g(row[C_HANDLE]),
                _g(row[C_TRANSFER]),
            ])

    print(f"脱敏完成 → {OUT_CSV}")
    print(f"  总行数: {len(rows)}")
    print(f"  单位 {len(unit_map)} → 单位A..单位{_code(len(unit_map))}")
    print(f"  人名 {len(person_map)} → 责任人A..责任人{_code(len(person_map))}")
    print(f"  项目 {len(project_map)} → 审计项目A..审计项目{_code(len(project_map))}")


if __name__ == "__main__":
    main()
