"""真实历史整改台账数据模块（脱敏后）

数据来源：国家管网审计管理信息系统的 2023-2025 年审计发现问题整改台账，
经 scripts/anonymize_ledger.py 脱敏后落盘为 data/整改台账_脱敏.csv（2899 条）。

职责：加载、汇总统计、筛选查询，供 Web 结果页「历史整改台账」渲染。
与 data_tool.py（现场实施取数筛查）不同——这里是「审计结果数据」，只读查询，不做异常检测。
"""
import csv
import os

DATA_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "整改台账_脱敏.csv")

_cache = None


def load_ledger():
    """加载全部脱敏台账行（list[dict]，进程内缓存一次）"""
    global _cache
    if _cache is None:
        rows = []
        with open(DATA_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append(r)
        _cache = rows
    return _cache


def _money(v):
    try:
        return float(v or 0)
    except (ValueError, TypeError):
        return 0.0


def _cat1(r):
    return r.get("问题类别1", "").lstrip("*")


def summarize(rows):
    """汇总统计：供看板顶部数字卡与类别分布"""
    total = len(rows)
    xiaohao = sum(1 for r in rows if r.get("是否销号") == "已销号")
    amount = sum(_money(r.get("问题涉及金额_元")) for r in rows)
    result = sum(_money(r.get("整改成果金额_元")) for r in rows)

    by_year, by_cat, by_status = {}, {}, {}
    for r in rows:
        by_year[r.get("年度", "")] = by_year.get(r.get("年度", ""), 0) + 1
        c = _cat1(r)
        by_cat[c] = by_cat.get(c, 0) + 1
        st = r.get("整改状态", "")
        by_status[st] = by_status.get(st, 0) + 1

    return {
        "total": total,
        "已销号": xiaohao,
        "未销号": total - xiaohao,
        "涉及金额": int(amount),
        "成果金额": int(result),
        "by_year": sorted(by_year.items()),
        "by_cat": sorted(by_cat.items(), key=lambda x: -x[1])[:12],
        "by_status": by_status,
    }


def category_options(rows):
    """问题类别1（去 *）分布，供筛选下拉框"""
    cats = {}
    for r in rows:
        c = _cat1(r)
        cats[c] = cats.get(c, 0) + 1
    return sorted(cats.items(), key=lambda x: -x[1])


def filter_rows(rows, year=None, cat=None, status=None, keyword=None, page=1, size=20):
    """按条件筛选 + 分页"""
    out = []
    for r in rows:
        if year and r.get("年度") != str(year):
            continue
        if cat and _cat1(r) != cat:
            continue
        if status and r.get("是否销号") != status:
            continue
        if keyword:
            hay = "".join(r.get(k, "") for k in ("问题名称", "问题定性", "整改措施", "审计建议", "问题编号"))
            if keyword not in hay:
                continue
        out.append(r)

    total = len(out)
    start = (page - 1) * size
    return {
        "total": total,
        "page": page,
        "size": size,
        "items": out[start:start + size],
    }
