#!/bin/bash
# QLever ORDER BY + LIMIT 对拍回归（S1 收尾）：带 LIMIT 的查询结果必须是
# 全量结果的对应切片，否则说明引擎存在"先截断后排序"类缺陷。
#
# 什么时候用：动了 QLever 版本之后——重建容器（run-dev-container.sh）、
# 改 Dockerfile 里的 digest、或换了镜像源。平时不用跑。
# 用法：bash regression-limit.sh [endpoint]   # endpoint 默认 http://localhost:7001/sparql
# 退出码：0 全过 / 1 有 FAIL / 2 查询本身出错
set -u
EP="${1:-http://localhost:7001/sparql}"

python - "$EP" <<'PYEOF'
# -*- coding: utf-8 -*-
import json
import sys
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
EP = sys.argv[1]


def sparql(q: str) -> list[dict]:
    data = urllib.parse.urlencode({"query": q}).encode()
    req = urllib.request.Request(EP, data=data,
                                 headers={"Accept": "application/sparql-results+json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    b = d.get("results", {}).get("bindings")
    if b is None:
        print("!! 端点响应不是 SPARQL JSON：", str(d)[:200])
        sys.exit(2)
    return b


def counts(b: list[dict]) -> list[int]:
    return [int(x["n"]["value"]) for x in b]


fails = 0

full = sparql("SELECT ?cls (COUNT(?x) AS ?n) WHERE { ?x a ?cls } "
              "GROUP BY ?cls ORDER BY DESC(?n)")
print(f"图上共 {len(full)} 个类，最大计数 {counts(full)[:3]}")
if len(full) < 4:
    print("!! 当前图数据太少（<4 类），对拍信号弱——建议切到大数据工作空间再跑")
    sys.exit(2)

# ---- 测试 1：聚合 ORDER BY DESC + LIMIT 3 == 全量前 3 个计数 ----
lim = sparql("SELECT ?cls (COUNT(?x) AS ?n) WHERE { ?x a ?cls } "
             "GROUP BY ?cls ORDER BY DESC(?n) LIMIT 3")
ok = counts(lim) == counts(full)[:3]
print(("PASS" if ok else "FAIL"), "测试1 聚合 DESC + LIMIT 3:",
      counts(lim), "vs 全量切片", counts(full)[:3])
fails += 0 if ok else 1

# ---- 测试 2：唯一键升序 LIMIT 1 == 全量第一行（类 IRI 互异，全序无并列） ----
lim1 = sparql("SELECT ?cls (COUNT(?x) AS ?n) WHERE { ?x a ?cls } "
              "GROUP BY ?cls ORDER BY ?cls LIMIT 1")
full_asc = sparql("SELECT ?cls (COUNT(?x) AS ?n) WHERE { ?x a ?cls } "
                  "GROUP BY ?cls ORDER BY ?cls")
got = (lim1[0]["cls"]["value"], counts(lim1)[0]) if lim1 else None
want = (full_asc[0]["cls"]["value"], counts(full_asc)[0]) if full_asc else None
ok = got == want
print(("PASS" if ok else "FAIL"), "测试2 唯一键升序 + LIMIT 1：", got, "vs", want)
fails += 0 if ok else 1

# ---- 测试 3：DESC + LIMIT 2 OFFSET 1 == 全量第 2~3 个计数 ----
off = sparql("SELECT ?cls (COUNT(?x) AS ?n) WHERE { ?x a ?cls } "
             "GROUP BY ?cls ORDER BY DESC(?n) LIMIT 2 OFFSET 1")
ok = counts(off) == counts(full)[1:3]
print(("PASS" if ok else "FAIL"), "测试3 DESC + LIMIT 2 OFFSET 1:",
      counts(off), "vs 全量切片", counts(full)[1:3])
fails += 0 if ok else 1

print("====", "全部通过" if fails == 0 else f"{fails} 项失败")
sys.exit(1 if fails else 0)
PYEOF
