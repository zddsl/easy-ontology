# -*- coding: utf-8 -*-
"""HD_SAAS 物化验收探针：等 /api/materialize/job 结束 → 等 QLever 索引恢复 → 跑验收套件。
用法：python tools/verify_hd2.py   （物化启动后再跑；phase=error 直接中止不误验收）
"""
import urllib.request, urllib.parse, json, time, sys

BACKEND = 'http://localhost:8010'
QLEVER = 'http://localhost:7001/'

def get(url, timeout=60):
    return json.load(urllib.request.urlopen(url, timeout=timeout))

def q(sparql, tries=8, sleep=20):
    url = QLEVER + '?query=' + urllib.parse.quote(sparql)
    last = None
    for _ in range(tries):
        try:
            d = json.load(urllib.request.urlopen(url, timeout=120))
            if 'exception' in d:
                return ('QERR', str(d['exception'])[:150])
            return ('OK', d.get('results', {}).get('bindings', []))
        except Exception as e:
            last = e
            time.sleep(sleep)
    return ('HERR', str(last)[:150])

# ---- 1) 等物化 job 结束（error 即中止） ----
job = None
for i in range(60):
    try:
        job = get(BACKEND + '/api/materialize/job')
        ph = job.get('phase') or job.get('status')
        print(f'[{i}] job: {ph} done={job.get("workers_done")}/{job.get("workers_total")} triples={job.get("triples_done")}', flush=True)
        if ph in ('done', 'error', 'failed'):
            break
    except Exception as e:
        print(f'[{i}] job-status ERR {str(e)[:100]}', flush=True)
    time.sleep(30)
else:
    print('job poll timeout'); sys.exit(2)

if job and (job.get('phase') in ('error', 'failed')):
    print('=== MATERIALIZE FAILED ===')
    print(json.dumps(job, ensure_ascii=False)[:600])
    sys.exit(3)
print('=== job done ===', flush=True)

# ---- 2) 等 QLever 索引恢复（换血期间查询会挂/半新半旧） ----
# 就绪判据必须锚定"新图特有值"（Asset=509161 是 join 版独有；旧图 560086）。
# 2026-09-07 教训：只查"Ticket 计数非零"会在索引换血窗口放行，B8 差点误判 FAIL。
NS = 'PREFIX hd: <http://tohi.cn/2026/hd#>'
cnt = lambda body: f'{NS} SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE {{ {body} }}'
for i in range(30):
    st, b = q(cnt('?x a hd:Asset'), tries=1, sleep=1)
    if st == 'OK' and b and list(b[0].values())[0]['value'] == '509161':
        print(f'index swapped to new graph after {i} probes', flush=True)
        break
    got = list(b[0].values())[0]['value'] if st == 'OK' and b else st
    print(f'[idx {i}] waiting for rebuild... (Asset={got})', flush=True)
    time.sleep(20)

# ---- 3) 验收套件 ----
# 期望值（2026-09-05 join 版映射 + ticketId string 重物化）：
#   三个幽灵尾巴类应精确等于表行数；ticketId 用字符串锚点必须命中
tests = [
    ('ticketId字符串锚点', NS + ' SELECT ?c WHERE { ?t hd:ticketId "2769695" ; hd:ticketOnCode ?c }',
     lambda v: any('AQ000189_Y' in x[0] for x in v)),
    ('ticketId数据类型', NS + ' SELECT (DATATYPE(?v) AS ?d) WHERE { ?t hd:ticketId "2205326" ; hd:ticketId ?v } LIMIT 1',
     lambda v: v and 'string' in v[0][0]),
    ('Ticket',     cnt('?x a hd:Ticket'),     lambda v: v and v[0][0] == '750601'),
    ('Asset',      cnt('?x a hd:Asset'),      lambda v: v and v[0][0] == '509161'),
    ('WorkOrder',  cnt('?x a hd:WorkOrder'),  lambda v: v and v[0][0] == '440080'),
    ('Locations',  cnt('?x a hd:Locations'),  lambda v: v and v[0][0] == '943274'),
    ('FailureCode',cnt('?x a hd:FailureCode'),lambda v: v and v[0][0] == '84462'),
    ('ticketOnCode边', NS + ' SELECT (COUNT(*) AS ?n) WHERE { ?t hd:ticketOnCode ?c }',
     lambda v: v and v[0][0] == '103018'),
    ('B4 状态流转', NS + ' SELECT (COUNT(?s) AS ?n) WHERE { ?w hd:woNum "DLCZ42014" ; hd:hasStatus ?s }',
     lambda v: v and v[0][0] == '23'),
    ('B8 AQ000040', cnt('?t hd:ticketOnCode <http://tohi.cn/2026/hd#failurecode/AQ000040>'),
     lambda v: v and v[0][0] == '5530'),
    ('C1 物料成本', NS + ' SELECT (COUNT(?m) AS ?n) (SUM(?lc) AS ?s) WHERE { ?a hd:assetNum "01191300023" . ?w hd:concernsAsset ?a ; hd:hasMaterial ?m . ?m hd:lineCost ?lc }',
     lambda v: v and v[0][0] == '128'),
    ('D1 工单王', NS + ' SELECT ?an (COUNT(?w) AS ?n) WHERE { ?w hd:concernsAsset ?a . ?a hd:assetNum ?an } GROUP BY ?an ORDER BY DESC(?n) LIMIT 1',
     lambda v: v and v[0][0] == '340115010005' and v[0][1] == '1071'),
]

print('\n== 验收 ==', flush=True)
bad = 0
for name, s, ok in tests:
    st, b = q(s)
    if st != 'OK':
        print(f'{name:16s} {st} {b}', flush=True); bad += 1; continue
    vals = [tuple(vv['value'] for vv in r.values()) for r in b]
    try:
        verdict = 'PASS' if ok(vals) else 'FAIL'
    except Exception as e:
        verdict = f'CHECK {e}'
    if verdict != 'PASS':
        bad += 1
    print(f'{name:16s} {verdict} {vals[:2]}', flush=True)
    time.sleep(2)

print(f'\nDONE: {len(tests)-bad}/{len(tests)} PASS', flush=True)
sys.exit(0 if bad == 0 else 1)
