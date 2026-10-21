"""真实审计问题库数据模块（脱敏后）

数据来源：国家管网审计管理信息系统「问题清单信息导出」（6096 条），
经 scripts/anonymize_problems.py 脱敏后落盘为 data/审计问题库_脱敏.csv。

职责：加载、汇总统计、筛选查询，供 Web 结果页「审计问题库」渲染。
与 data_tool.py（现场实施取数筛查）不同——这里是「审计结果数据」，只读查询，不做异常检测。
"""
import csv
import os

DATA_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "审计问题库_脱敏.csv")

_cache = None


def load_problems():
    """加载全部脱敏问题清单行（list[dict]，进程内缓存一次）"""
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


def _domain(r):
    """业务领域取一级（首个编码段，如「11.2管理法律事务」）"""
    d = (r.get("业务领域") or "").strip()
    return d.split(",")[0] if d else ""


def _func(r):
    return (r.get("监督职能") or "").strip()


def summarize(rows):
    """汇总统计：供看板顶部数字卡与分布"""
    total = len(rows)
    amount = sum(_money(r.get("问题涉及金额_万元")) for r in rows)
    with_amount = sum(1 for r in rows if _money(r.get("问题涉及金额_万元")) > 0)

    by_year, by_func, by_domain = {}, {}, {}
    for r in rows:
        y = r.get("年度", "")
        by_year[y] = by_year.get(y, 0) + 1
        f = _func(r)
        by_func[f] = by_func.get(f, 0) + 1
        d = _domain(r)
        if d:
            by_domain[d] = by_domain.get(d, 0) + 1

    return {
        "total": total,
        "有金额问题数": with_amount,
        "涉及金额_万元": round(amount, 2),
        "监督职能数": len([k for k in by_func if k]),
        "业务领域数": len(by_domain),
        "by_year": sorted(by_year.items()),
        "by_func": sorted(by_func.items(), key=lambda x: -x[1]),
        "by_domain": sorted(by_domain.items(), key=lambda x: -x[1])[:20],
    }


def filter_options(rows):
    """筛选项：年度 / 监督职能 / 业务领域（一级），供下拉框"""
    years, funcs, domains = {}, {}, {}
    for r in rows:
        y = r.get("年度", "")
        if y:
            years[y] = years.get(y, 0) + 1
        f = _func(r)
        if f:
            funcs[f] = funcs.get(f, 0) + 1
        d = _domain(r)
        if d:
            domains[d] = domains.get(d, 0) + 1
    return {
        "years": sorted(years.items()),
        "funcs": sorted(funcs.items(), key=lambda x: -x[1]),
        "domains": sorted(domains.items(), key=lambda x: -x[1])[:50],
    }


def filter_problems(rows, year=None, supervision=None, domain=None, keyword=None, page=1, size=20):
    """按条件筛选 + 分页"""
    out = []
    for r in rows:
        if year and r.get("年度") != str(year):
            continue
        if supervision and _func(r) != supervision:
            continue
        if domain and _domain(r) != domain:
            continue
        if keyword:
            hay = "".join(r.get(k, "") for k in ("问题名称", "问题定性", "项目名称", "业务领域", "KCP名称"))
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
