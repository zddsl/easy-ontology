/* easy_ontology 控制台逻辑（纯 vanilla JS，无依赖） */
const $ = (id) => document.getElementById(id);

/* ---------- 基础 ---------- */

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const text = await r.text();
  let data;
  try { data = JSON.parse(text); } catch { data = text; }
  if (!r.ok) {
    const msg = (data && (data.detail?.message || data.detail || data.error)) || text;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

/* ---------- 工作空间（localStorage 按空间隔离） ---------- */

let WS_ID = null; // 启动时从 /api/workspaces 取激活空间 id

const lsKey = (base) => (WS_ID ? `${base}:${WS_ID}` : base);

// 一次性：老全局 key → 带空间 id 的新 key（新 key 不存在才拷）
function lsMigrate(base) {
  try {
    const nk = lsKey(base);
    if (localStorage.getItem(nk) === null && localStorage.getItem(base) !== null)
      localStorage.setItem(nk, localStorage.getItem(base));
  } catch {}
}

/* ---------- 状态轮询 ---------- */

let dsFormFilled = false; // 数据源表单是否已按已保存配置回填过（只回填一次）

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    const badge = $("state-badge");
    badge.textContent = s.state;
    badge.className = "badge " + s.state;
    $("ontop-info").textContent = s.ontop
      ? `pid ${s.ontop.pid} · 端口 ${s.ontop.port} · ${s.ontop.started_at} 起`
      : "端点未运行（查询时自动启动）";
    if (s.last_error) $("ontop-info").textContent += ` · ⚠ ${s.last_error}`;

    // 回显已保存的本体/映射/数据源（避免刷新后要重传）
    const ontEl = $("ontology-result");
    if (s.ontology && !ontEl.textContent.startsWith("上传中")) {
      ontEl.textContent = `已保存：${s.ontology.filename} · ${s.ontology.saved_at}`;
    }
    const bEl = $("builder-ontology-status");
    if (s.ontology) {
      const src = s.ontology.origin === "builder" ? "平台搭建" : "上传";
      bEl.textContent = `当前本体：来自${src} · ${s.ontology.saved_at}`;
    } else {
      bEl.textContent = "尚未保存本体";
    }
    const mapEl = $("mapping-result");
    if (s.mapping && !mapEl.textContent.startsWith("上传中")) {
      mapEl.textContent = `已保存：${s.mapping.filename} · ${s.mapping.n_mappings} 条 · ${s.mapping.saved_at}`;
    }
    const mcEl = $("mapping-current-status");
    if (mcEl) {
      mcEl.textContent = s.mapping
        ? `当前映射：来自${s.mapping.origin === "builder" ? "平台搭建" : "上传"} · ${s.mapping.n_mappings} 条 · ${s.mapping.saved_at}`
        : "尚未保存映射";
    }
    // 数据源表单只在首次加载时回填一次（原判断 !ds-host.value 永远为假——
    // HTML 默认值让输入框非空，导致保存的数据源从不回显）；此后轮询不再碰表单，
    // 避免覆盖用户正在编辑的内容
    if (s.datasource && !dsFormFilled) {
      dsFormFilled = true;
      $("ds-type").value = s.datasource.db_type || "dm8";
      $("ds-host").value = s.datasource.host || "";
      $("ds-port").value = s.datasource.port || "";
      $("ds-database").value = s.datasource.database || "";
      $("ds-user").value = s.datasource.user || "";
      $("ds-password").value = s.datasource.password || "";
      typeHint();
    }
  } catch (e) { console.error(e); }
}

/* ---------- ① 本体两种方式：上传 | 平台搭建 ---------- */

const ONT_MODE_KEY = "easy_ontology_ontology_mode";

function currentOntMode() {
  try {
    const v = localStorage.getItem(lsKey(ONT_MODE_KEY));
    return v === "builder" ? "builder" : "upload";
  } catch { return "upload"; }
}

function setOntologyMode(mode) {
  const isBuilder = mode === "builder";
  $("mode-upload").classList.toggle("active", !isBuilder);
  $("mode-builder").classList.toggle("active", isBuilder);
  $("ont-upload").style.display = isBuilder ? "none" : "block";
  $("ont-builder").style.display = isBuilder ? "block" : "none";
  try { localStorage.setItem(lsKey(ONT_MODE_KEY), mode); } catch {}
  if (isBuilder) refreshBuilderStatus();
}

async function refreshBuilderStatus() {
  // 草稿概要
  try {
    const r = await api("/api/builder/draft");
    const el = $("builder-draft-status");
    if (r.exists && r.counts) {
      const c = r.counts;
      el.textContent = `草稿：${c.classes} 类 · ${c.objectProperties} 对象属性 · ${c.dataProperties} 数据属性${r.updated_at ? " · " + r.updated_at : ""}`;
    } else {
      el.textContent = "暂无草稿，点「去搭建」新建";
    }
  } catch (e) { $("builder-draft-status").textContent = "草稿：读取失败"; }
}

function showBuilderToast(msg) {
  const t = $("builder-toast");
  t.textContent = msg;
  t.style.display = "block";
  setTimeout(() => { t.style.display = "none"; }, 6000);
}

/* ---------- ② 映射两种方式：上传 | 平台搭建 ---------- */

const MAPPING_MODE_KEY = "easy_ontology_mapping_mode";

function currentMapMode() {
  try {
    const v = localStorage.getItem(lsKey(MAPPING_MODE_KEY));
    return v === "builder" ? "builder" : "upload";
  } catch { return "upload"; }
}

function setMappingMode(mode) {
  const isBuilder = mode === "builder";
  $("map-mode-upload").classList.toggle("active", !isBuilder);
  $("map-mode-builder").classList.toggle("active", isBuilder);
  $("map-upload").style.display = isBuilder ? "none" : "block";
  $("map-builder").style.display = isBuilder ? "block" : "none";
  try { localStorage.setItem(lsKey(MAPPING_MODE_KEY), mode); } catch {}
  if (isBuilder) refreshMappingDraftStatus();
}

async function refreshMappingDraftStatus() {
  try {
    const r = await api("/api/mapping/draft");
    const el = $("mapping-draft-status");
    if (r.exists && r.counts) {
      const c = r.counts;
      el.textContent = `草稿：${c.classes_mapped} 类已映射 · ${c.relations_mapped} 关系已映射${r.updated_at ? " · " + r.updated_at : ""}`;
    } else {
      el.textContent = "暂无草稿，点「去搭建映射」新建";
    }
  } catch (e) { $("mapping-draft-status").textContent = "草稿：读取失败"; }
}

/* ---------- 上传 ---------- */

async function uploadOntology() {
  const f = $("file-ontology").files[0];
  if (!f) return alert("先选本体文件");
  const fd = new FormData();
  fd.append("file", f);
  $("ontology-result").textContent = "上传中…";
  try {
    const r = await api("/api/files/ontology", { method: "POST", body: fd });
    $("ontology-result").textContent = `✓ ${r.triples} 三元组 (${r.format})`;
    refreshGraph();
  } catch (e) { $("ontology-result").textContent = "✗"; showError(e.message); }
}

async function uploadMapping() {
  const f = $("file-mapping").files[0];
  if (!f) return alert("先选映射文件");
  const fd = new FormData();
  fd.append("file", f);
  $("mapping-result").textContent = "上传中…";
  try {
    const r = await api("/api/files/mapping", { method: "POST", body: fd });
    $("mapping-result").textContent = `✓ ${r.n_mappings} 条映射`;
  } catch (e) { $("mapping-result").textContent = "✗"; showError(e.message); }
}

async function clearOntology() {
  if (!confirm("确认清空已保存的本体？此操作不可撤销。")) return;
  try {
    await api("/api/files/ontology", { method: "DELETE" });
    $("ontology-result").textContent = "已清空";
    $("builder-ontology-status").textContent = "尚未保存本体";
    refreshStatus();
  } catch (e) { showError(e.message); }
}

async function clearMapping() {
  if (!confirm("确认清空已保存的映射？此操作不可撤销。")) return;
  try {
    await api("/api/files/mapping", { method: "DELETE" });
    $("mapping-result").textContent = "已清空";
    refreshStatus();
  } catch (e) { showError(e.message); }
}

/* ---------- 数据源 ---------- */

function collectForm() {
  return {
    db_type: $("ds-type").value,
    host: $("ds-host").value.trim(),
    port: $("ds-port").value.trim(),
    database: $("ds-database").value.trim(),
    user: $("ds-user").value.trim(),
    password: $("ds-password").value,
  };
}

async function typeHint() {
  const t = $("ds-type").value;
  $("ds-port").value = t === "dm8" ? "5236" : "3306";
  $("ds-database").parentElement.style.opacity = t === "dm8" ? .5 : 1; // dm8 URL 不带库名
  try {
    const r = await api(`/api/dbtype-hint?type=${t}`);
    $("ds-hint").textContent = r.hint;
  } catch { $("ds-hint").textContent = ""; }
}

async function saveDatasource() {
  try {
    const r = await api("/api/datasource", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectForm()),
    });
    $("ping-result").textContent = `已保存（${r.state}）`;
    $("ping-result").className = "ping-result ok";
    refreshStatus();
  } catch (e) { showError(e.message); }
}

async function testConnection() {
  const btn = $("btn-test");
  btn.disabled = true;
  $("ping-result").textContent = "连接中…";
  $("ping-result").className = "ping-result";
  try {
    const r = await api("/api/test-connection", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectForm()),   // 测当前表单所填，不再是上次保存的配置
    });
    if (r.ok) {
      $("ping-result").textContent = `✓ 连通 ${r.ms}ms`;
      $("ping-result").className = "ping-result ok";
    } else {
      $("ping-result").textContent = `✗ ${r.error || "失败"}`;
      $("ping-result").className = "ping-result fail";
    }
  } catch (e) {
    $("ping-result").textContent = `✗ ${e.message}`;
    $("ping-result").className = "ping-result fail";
  }
  btn.disabled = false;
}

/* ---------- 端点控制 ---------- */
/* 端点不再手动管理：虚拟路线查询到达时后端自动拉起（sparql_proxy 懒启动）。
   重启保留为僵尸查询逃生舱，收在 API 卡片底部。 */

async function restartEndpoint() {
  if (!confirm("重启端点会强制断开当前查询连接，确定？")) return;
  try {
    const r = await api("/api/endpoint/restart", { method: "POST" });
    if (r.boot_ms !== undefined) showMeta(`端点 ${r.status}（pid ${r.pid}，启动 ${r.boot_ms}ms）`);
  } catch (e) { showError(e.message); }
  refreshStatus();
}

/* ---------- 顶栏 API 卡片：暴露对外查询接口与参数 ---------- */

function toggleApiCard(ev) {
  ev && ev.stopPropagation();
  const card = $("api-card");
  const willOpen = card.style.display === "none";
  card.style.display = willOpen ? "block" : "none";
  if (willOpen) buildApiCard();
}

// 点卡片外任意处收起（按钮自身已 stopPropagation）
document.addEventListener("click", (e) => {
  const pop = document.querySelector(".api-pop");
  if (pop && !pop.contains(e.target)) $("api-card").style.display = "none";
});

function buildApiCard() {
  const origin = location.origin;
  $("api-card").innerHTML = `
    <div class="api-sec">
      <h4>SPARQL 查询<em>推荐入口</em></h4>
      <div class="api-line"><code>POST ${origin}/sparql</code>（GET 同路径 <code>?query=&amp;route=</code> 亦可）</div>
      <table class="api-params">
        <tr><td>query</td><td>SPARQL 语句（必填，form-urlencoded）</td></tr>
        <tr><td>route</td><td><b>virtual</b> 直查数据库（默认）｜<b>materialized</b> 查已生成 ABox</td></tr>
        <tr><td>Accept</td><td>text/csv（默认）｜application/sparql-results+json</td></tr>
      </table>
      <div class="api-curl-wrap">
        <button class="btn small" onclick="copyApi(this)">复制</button>
        <pre>curl -X POST "${origin}/sparql" \\
  -H "Accept: text/csv" \\
  --data-urlencode "query=SELECT ?s ?p ?o WHERE { ?s ?p ?o } LIMIT 5" \\
  --data-urlencode "route=virtual"</pre>
      </div>
      <div class="hint">返回 CSV / JSON 结果集；virtual 端点未运行时自动启动（首次约 10~20s），materialized 需先生成 ABox</div>
    </div>
    <div class="api-sec">
      <h4>自然语言问答</h4>
      <div class="api-line"><code>POST ${origin}/api/ask</code>（JSON body）</div>
      <table class="api-params">
        <tr><td>question</td><td>中文问题（必填）</td></tr>
        <tr><td>route</td><td><b>virtual</b>（默认）｜<b>materialized</b></td></tr>
      </table>
      <div class="api-curl-wrap">
        <button class="btn small" onclick="copyApi(this)">复制</button>
        <pre>curl -X POST "${origin}/api/ask" -H "Content-Type: application/json" \\
  -d "{\\"question\\": \\"工单一共有多少条\\", \\"route\\": \\"virtual\\"}"</pre>
      </div>
      <div class="hint">需先在顶栏「配置」中设置 LLM API Key；返回 <code>{answer, sparql, csv}</code></div>
    </div>
    <div class="api-sec">
      <h4>本体摘要（LLM 提示词）</h4>
      <div class="api-line"><code>GET ${origin}/api/ontology/summary</code></div>
      <table class="api-params">
        <tr><td>无参数</td><td>返回当前工作空间本体的结构摘要</td></tr>
      </table>
      <div class="api-curl-wrap">
        <button class="btn small" onclick="copyApi(this)">复制</button>
        <pre>curl ${origin}/api/ontology/summary</pre>
      </div>
      <div class="hint">返回 <code>{ns, classes, obj_props, dt_props}</code>；classes/props 已附中文名与描述，外部 LLM 可直接用来组装提示词</div>
    </div>
    <div class="api-foot">端点卡死（查询一直挂起）时：<a href="javascript:void(0)" onclick="restartEndpoint()">重启端点</a></div>`;
}

function copyApi(btn) {
  const pre = btn.parentElement.querySelector("pre");
  const text = pre ? pre.textContent : "";
  (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject())
    .then(() => { btn.textContent = "已复制✓"; setTimeout(() => (btn.textContent = "复制"), 1500); })
    .catch(() => {
      // clipboard API 不可用时退回选区复制
      const ta = document.createElement("textarea");
      ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); btn.textContent = "已复制✓"; } catch {}
      ta.remove(); setTimeout(() => (btn.textContent = "复制"), 1500);
    });
}

/* ---------- 拓扑图 ---------- */

async function refreshGraph() {
  const frame = $("graph-frame");
  // Playground 是否已构建（EO-7 之后才有）
  try {
    const probe = await fetch("/playground/index.html", { method: "HEAD" });
    if (!probe.ok) return;
  } catch { return; }
  frame.style.display = "block";
  $("graph-placeholder").style.display = "none";
  const t = Date.now();
  frame.src = `/playground/?rdf=${encodeURIComponent("/api/ontology/rdf?viewer=1")}&t=${t}#/`;
}

/* ---------- SPARQL ---------- */

/* ---------- 查询路线（SPARQL 与自然语言问答共用一份） ---------- */

const ROUTE_KEY = "easy_ontology_route";

function currentRoute() {
  try { return localStorage.getItem(lsKey(ROUTE_KEY)) || "virtual"; } catch { return "virtual"; }
}

function setRoute(v) {
  v = v === "materialized" ? "materialized" : "virtual";
  try { localStorage.setItem(lsKey(ROUTE_KEY), v); } catch {}
  $("ask-route").value = v;
  $("query-route").value = v;
}

async function runQuery() {
  const q = $("query-text").value.trim();
  if (!q) return;
  const btn = $("btn-run");
  btn.disabled = true; btn.textContent = "执行中…（宽查询要几十秒）";
  showError(""); $("result-table").innerHTML = ""; showMeta("");
  const accept = $("json-mode").checked ? "application/sparql-results+json" : "text/csv";
  const route = currentRoute();
  const t0 = Date.now();
  try {
    const r = await fetch("/sparql", {
      method: "POST",
      headers: { "Accept": accept, "Content-Type": "application/x-www-form-urlencoded" },
      body: "query=" + encodeURIComponent(q) + "&route=" + route,
    });
    const text = await r.text();
    const ms = Date.now() - t0;
    if (!r.ok) {
      let msg = text;
      try { msg = JSON.parse(text).error || text; } catch {}
      showError(msg);
    } else if (accept.includes("json")) {
      renderJson(JSON.parse(text), ms);
    } else {
      renderCsv(text, ms, r.headers.get("X-Row-Cap"));
    }
  } catch (e) { showError("请求失败：" + e.message); }
  btn.disabled = false; btn.textContent = "执行";
  refreshQueries();
}

function renderCsv(text, ms, capped) {
  const rows = parseCsv(text);
  const [head, ...body] = rows;
  let html = "<tr>" + head.map(h => `<th>${esc(h)}</th>`).join("") + "</tr>";
  body.forEach(r => { html += "<tr>" + r.map(c => `<td>${esc(c)}</td>`).join("") + "</tr>"; });
  $("result-table").innerHTML = html;
  showMeta(`${body.length} 行 · ${ms}ms${capped ? " · ⚠ 已按上限截断" : ""}`);
}

function renderJson(data, ms) {
  const vars = data.head?.vars || [];
  const bindings = data.results?.bindings || [];
  let html = "<tr>" + vars.map(v => `<th>${esc(v)}</th>`).join("") + "</tr>";
  bindings.forEach(b => {
    html += "<tr>" + vars.map(v => `<td>${esc(b[v]?.value ?? "")}</td>`).join("") + "</tr>";
  });
  $("result-table").innerHTML = html;
  showMeta(`${bindings.length} 行 · ${ms}s`);
}

/* 引号感知的 CSV 解析 */
function parseCsv(text) {
  const rows = []; let row = []; let cell = ""; let inQ = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQ) {
      if (ch === '"' && text[i + 1] === '"') { cell += '"'; i++; }
      else if (ch === '"') inQ = false;
      else cell += ch;
    } else if (ch === '"') inQ = true;
    else if (ch === ",") { row.push(cell); cell = ""; }
    else if (ch === "\n") { row.push(cell); rows.push(row); row = []; cell = ""; }
    else if (ch !== "\r") cell += ch;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  return rows;
}

/* ---------- 查询登记簿 ---------- */

let _queryEntries = [];

async function refreshQueries() {
  try {
    const r = await api("/api/queries");
    _queryEntries = r.queries || [];
    let html = "<tr><th>时间</th><th>来源</th><th>状态</th><th>耗时</th><th>查询/问题</th><th>回答</th><th></th></tr>";
    _queryEntries.forEach((q, i) => {
      const state = { done: "完成", running: "运行中", timeout: "挂起", error: "错误" }[q.state] || q.state;
      const isAsk = !!q.question;
      const source = isAsk ? '<span class="qlog-badge ask">问答</span>' : '<span class="qlog-badge sparql">SPARQL</span>';
      const content = isAsk ? esc(q.question.slice(0, 80)) : esc(q.query.slice(0, 80));
      const answer = q.answer ? esc(q.answer.slice(0, 60)) : "-";
      const copyQ = isAsk ? "question" : "query";
      html += `<tr class="state-${q.state}">` +
        `<td>${q.started_at}</td>` +
        `<td>${source}</td>` +
        `<td>${state}</td>` +
        `<td>${q.elapsed_ms ?? "-"}ms</td>` +
        `<td class="qlog-cell qlog-clickable" onclick="copyCellByIdx(${i},'${copyQ}')">${content}</td>` +
        `<td class="qlog-cell qlog-clickable" onclick="copyCellByIdx(${i},'answer')">${q.answer ? answer : '-'}</td>` +
        `<td><span class="qlog-detail-link" onclick="showQueryDetail(${i})">详情</span></td>` +
        `</tr>`;
    });
    $("queries-table").innerHTML = html;
  } catch {}
}

function copyCellByIdx(idx, field) {
  const q = _queryEntries[idx];
  if (!q || !q[field]) return;
  navigator.clipboard.writeText(q[field]).then(() => {
    const t = $("builder-toast");
    t.textContent = "已复制";
    t.style.display = "block";
    setTimeout(() => { t.style.display = "none"; }, 1500);
  });
}

function showQueryDetail(idx) {
  const q = _queryEntries[idx];
  if (!q) return;
  let body = "";
  if (q.question) body += '<div class="qd-section"><div class="qd-label">问题</div><div class="qd-content qlog-clickable" onclick="copyCellByIdx(' + idx + ',\'question\')">' + esc(q.question) + '</div></div>';
  body += '<div class="qd-section"><div class="qd-label">SPARQL</div><pre class="qd-content qd-code qlog-clickable" onclick="copyCellByIdx(' + idx + ',\'query\')">' + esc(q.query) + '</pre></div>';
  if (q.answer) body += '<div class="qd-section"><div class="qd-label">回答</div><div class="qd-content qlog-clickable" onclick="copyCellByIdx(' + idx + ',\'answer\')">' + esc(q.answer) + '</div></div>';
  body += '<div class="qd-section"><div class="qd-label">信息</div><div class="qd-content">来源：' + (q.question ? '问答' : 'SPARQL') + ' | 状态：' + q.state + ' | 耗时：' + (q.elapsed_ms ?? '-') + 'ms | 路线：' + (q.route || '-') + '</div></div>';
  $("query-detail-body").innerHTML = body;
  $("query-detail-modal").style.display = "flex";
}

function closeQueryDetail() {
  $("query-detail-modal").style.display = "none";
}

/* ---------- 工具 ---------- */

const esc = (s) => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const showError = (m) => ($("query-error").textContent = m || "");
const showMeta = (m) => ($("result-meta").textContent = m || "");

/* ---------- 自然语言问答（DeepSeek） ---------- */

/* ---------- ABox 物化 ---------- */

const ABOX_ENABLED_KEY = "easy_ontology_abox_enabled";

function setAboxEnabled(on) {
  $("abox-enable").classList.toggle("active", on);
  $("abox-disable").classList.toggle("active", !on);
  $("abox-panel").style.display = on ? "block" : "none";
  try { localStorage.setItem(lsKey(ABOX_ENABLED_KEY), on ? "1" : "0"); } catch {}
  if (on) refreshAboxStatus();
}

function currentAboxEnabled() {
  try { return localStorage.getItem(lsKey(ABOX_ENABLED_KEY)) === "1"; } catch { return false; }
}

async function fetchIndexStatus() {
  try { return await api("/api/materialize/index-status"); }
  catch { return null; }
}

let aboxIndexTimer = null;
let pendingIndexMsg = false;  // 上传后等索引就绪，就绪时把上传结果行翻成 ✓
function scheduleAboxPoll() {
  if (aboxIndexTimer) return;
  aboxIndexTimer = setInterval(async () => {
    const st = await fetchIndexStatus();
    if (!st) return;
    renderAboxStatus(null, st);
    if (st.state === "ready" || st.state === "no_abox") {
      clearInterval(aboxIndexTimer); aboxIndexTimer = null;
    }
  }, 5000);
}

// 状态条复合渲染：入库数 + QLever 索引进度（未就绪自动轮询）
function renderAboxStatus(meta, st) {
  const el = $("abox-status");
  const n = (st && st.abox_triples) || (meta && meta.triples) || 0;
  if (!n) {
    el.textContent = "未生成";
    el.className = "ping-result";
    resetMaterializeBtn();
    return;
  }
  if (st && st.state === "ready" && pendingIndexMsg) {
    pendingIndexMsg = false;
    const up = $("abox-upload-result");
    if (up) {
      up.textContent = `✓ 物化就绪 · ${n.toLocaleString()} 三元组已可查询`;
      up.className = "hint ok";
    }
  }
  const when = meta && meta.generated_at ? ` · ${meta.generated_at}` : "";
  const took = meta && meta.elapsed_ms ? ` · 总耗时 ${Math.round(meta.elapsed_ms / 1000)}s` : "";
  const state = st ? st.state : "unknown";
  if (state === "ready") {
    el.textContent = `✓ 物化就绪 · ${n.toLocaleString()} 三元组${took}${when}`;
    el.className = "ping-result ok";
    resetMaterializeBtn();  // 生成按钮灰到索引就绪（不只是落盘）才恢复
  } else if (state === "rebuilding") {
    el.textContent = `⏳ 已入库 ${n.toLocaleString()} 三元组 · 索引重建中…（期间物化查询不可用）`;
    el.className = "ping-result";
    scheduleAboxPoll();
  } else if (state === "syncing") {
    el.textContent = `⏳ 索引同步中 · 已索引 ${(st.indexed || 0).toLocaleString()} / ${n.toLocaleString()}（物化查询尚未就绪）`;
    el.className = "ping-result";
    scheduleAboxPoll();
  } else {
    const kb = meta ? ` · ${(meta.size_bytes / 1024).toFixed(1)} KB` : "";
    el.textContent = `✓ 已入库 ${n.toLocaleString()} 三元组${kb}${when}`;
    el.className = "ping-result ok";
    if (state === "unknown") scheduleAboxPoll();
  }
}

async function refreshAboxStatus() {
  try {
    const [s, st] = await Promise.all([api("/api/materialize/status"), fetchIndexStatus()]);
    renderAboxStatus(s.abox, st);
  } catch (e) { console.error(e); }
}

async function doMaterialize() {
  const btn = $("btn-materialize");
  btn.disabled = true; btn.textContent = "生成中…";
  $("abox-status").textContent = "启动生成…";
  $("abox-status").className = "ping-result";
  try {
    await api("/api/materialize", { method: "POST" });
  } catch (e) {
    $("abox-status").textContent = "✗ " + e.message;
    $("abox-status").className = "ping-result err";
    resetMaterializeBtn();
    return;
  }
  startJobPoll();
}

// 生成 job 轮询（3s）：按钮全程保持灰"生成中…"（宽度初始化时锁定不跳动），
// 取消入口是状态条里的"取消"链接；done 后交棒 index-status 轮询，ready 前不给 ✓
let jobTimer = null;
function resetMaterializeBtn() {
  const btn = $("btn-materialize");
  btn.disabled = false; btn.textContent = "生成 ABox";
}
async function cancelMaterialize() {
  $("abox-status").textContent = "取消中…";
  try { await api("/api/materialize/cancel", { method: "POST" }); } catch (e) {}
}
function stopJobPoll() {
  if (jobTimer) { clearInterval(jobTimer); jobTimer = null; }
}
function startJobPoll() {
  if (jobTimer) return;
  $("btn-materialize").disabled = true;
  jobTimer = setInterval(async () => {
    let j;
    try { j = await api("/api/materialize/job"); } catch { return; }
    if (!j || j.phase === null) { stopJobPoll(); resetMaterializeBtn(); return; }
    if (j.phase === "running") {
      $("abox-status").innerHTML =
        `⏳ 生成中 · 已产出 ${(j.triples_done || 0).toLocaleString()} 三元组 · 映射 ${j.workers_done}/${j.workers_total} 完成 · ${Math.round(j.elapsed_s)}s` +
        ` <a href="#" onclick="cancelMaterialize();return false">取消</a>`;
      $("abox-status").className = "ping-result";
    } else if (j.phase === "merging") {
      $("abox-status").textContent = `⏳ 合并去重落盘中… · ${Math.round(j.elapsed_s)}s`;
      $("abox-status").className = "ping-result";
    } else if (j.phase === "done") {
      stopJobPoll();
      const n = (j.result && j.result.triples) || 0;
      $("abox-status").textContent =
        `⏳ 已生成 ${n.toLocaleString()} 三元组 · 总耗时 ${Math.round(j.elapsed_s)}s · 索引后台重建中，就绪前物化查询不可用`;
      $("abox-status").className = "ping-result";
      refreshAboxStatus();
    } else if (j.phase === "error") {
      stopJobPoll(); resetMaterializeBtn();
      $("abox-status").textContent = "✗ " + (j.error || "生成失败");
      $("abox-status").className = "ping-result err";
    } else if (j.phase === "canceled") {
      stopJobPoll(); resetMaterializeBtn();
      $("abox-status").textContent = `已取消 · 耗时 ${Math.round(j.elapsed_s)}s`;
      $("abox-status").className = "ping-result";
      refreshAboxStatus();
    }
  }, 3000);
}

async function clearAbox() {
  if (!confirm("清除已生成的数据？")) return;
  const btn = $("btn-clear-abox");
  btn.disabled = true;
  $("abox-status").textContent = "清除中…";
  $("abox-status").className = "ping-result";
  $("abox-upload-result").textContent = "";
  try {
    await api("/api/materialize", { method: "DELETE" });
    $("abox-status").textContent = "✓ 已清除";
    $("abox-status").className = "ping-result ok";
  } catch (e) {
    $("abox-status").textContent = "✗ " + e.message;
    $("abox-status").className = "ping-result err";
  }
  btn.disabled = false;
}

/* ---------- ABox 预览 ---------- */

// IRI/字面量缩短显示（全称放 title 悬浮提示）
function shortIri(v) {
  if (!v) return "";
  let t = v;
  if (t.startsWith("<") && t.endsWith(">")) t = t.slice(1, -1);
  if (t.startsWith('"')) return t.length > 60 ? t.slice(0, 60) + "…" : t;
  const seg = t.split("#").pop().split("/").filter(Boolean).pop() || t;
  // IRI 里的中文是百分号编码（quote safe=''），显示前解码；坏序列保持原样
  let out = seg;
  try { out = decodeURIComponent(seg); } catch (e) { /* 保留原文 */ }
  return out.length > 48 ? out.slice(0, 48) + "…" : out;
}

let aboxViewState = { q: "", offset: 0, rows: [] };

async function viewAbox() {
  document.getElementById("abox-view-modal")?.remove();
  const overlay = document.createElement("div");
  overlay.className = "ws-modal";
  overlay.id = "abox-view-modal";
  overlay.innerHTML =
    '<div class="ws-modal-box abox-view-box">' +
      '<div class="abox-view-head">' +
        '<div class="ws-modal-title">ABox 预览</div>' +
        '<div class="abox-view-meta" id="abox-view-meta">加载中…</div>' +
        '<button type="button" class="btn small" id="abox-view-close">关闭</button>' +
      "</div>" +
      '<div class="abox-search-row">' +
        '<input id="abox-view-q" spellcheck="false" placeholder="过滤：IRI / 字面量片段（不区分大小写）">' +
        '<button type="button" class="btn small" id="abox-view-search">过滤</button>' +
        '<button type="button" class="btn small" id="abox-view-reset">重置</button>' +
      "</div>" +
      '<div class="table-wrap abox-view-wrap"><table id="abox-view-table">' +
        "<thead><tr><th>#</th><th>主语</th><th>谓语</th><th>宾语</th></tr></thead><tbody></tbody>" +
      "</table></div>" +
      '<div class="abox-view-foot">' +
        '<button type="button" class="btn small" id="abox-view-more" style="display:none">加载更多</button>' +
        '<span class="hint" id="abox-view-note"></span>' +
      "</div>" +
    "</div>";
  document.body.appendChild(overlay);
  overlay.querySelector("#abox-view-close").addEventListener("click", () => overlay.remove());
  overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
  const qEl = overlay.querySelector("#abox-view-q");
  const doSearch = () => loadAboxPreview(qEl.value, 0);
  overlay.querySelector("#abox-view-search").addEventListener("click", doSearch);
  overlay.querySelector("#abox-view-reset").addEventListener("click", () => { qEl.value = ""; doSearch(); });
  qEl.addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
  overlay.querySelector("#abox-view-more").addEventListener("click", () =>
    loadAboxPreview(aboxViewState.q, aboxViewState.offset, true));
  aboxViewState = { q: "", offset: 0, rows: [] };
  loadAboxPreview("", 0);
}

async function loadAboxPreview(q, offset, append = false) {
  const modal = document.getElementById("abox-view-modal");
  if (!modal) return;
  const note = modal.querySelector("#abox-view-note");
  const moreBtn = modal.querySelector("#abox-view-more");
  note.textContent = "加载中…";
  moreBtn.style.display = "none";
  let r;
  try {
    r = await api(`/api/materialize/preview?q=${encodeURIComponent(q)}&limit=100&offset=${offset}`);
  } catch (e) {
    note.textContent = "✗ " + e.message;
    return;
  }
  if (!r.exists) {
    modal.querySelector("#abox-view-meta").textContent = "";
    modal.querySelector("#abox-view-table").style.display = "none";
    note.textContent = "尚未生成 ABox（点「生成 ABox」物化，或用「NT 文件生成 ABox」上传）";
    return;
  }
  const mb = (r.size_bytes / 1024 / 1024).toFixed(1);
  modal.querySelector("#abox-view-meta").textContent =
    (r.meta ? `${Number(r.meta.triples).toLocaleString()} 三元组（元数据） · ` : "无元数据 · ") + `文件 ${mb} MB`;
  if (!append) aboxViewState = { q: r.q, offset: 0, rows: [] };
  aboxViewState.rows.push(...r.lines);
  aboxViewState.offset = aboxViewState.rows.length;
  // abox.nt 经 sort -u，同一主语的三元组天然连续：主语只显示一次，后续行 ↳（悬停可见完整 IRI）
  let prevS = null;
  modal.querySelector("#abox-view-table tbody").innerHTML = aboxViewState.rows.map((l) => {
    const grouped = l.s && l.s === prevS;
    prevS = l.s;
    return `<tr class="${grouped ? "grp-cont" : "grp-head"}"><td>${l.n}</td>` +
      `<td title="${esc(l.s || "")}">${grouped ? "↳" : esc(shortIri(l.s))}</td>` +
      `<td title="${esc(l.p || "")}">${esc(shortIri(l.p))}</td>` +
      `<td title="${esc(l.raw)}">${esc(shortIri(l.o))}</td></tr>`;
  }).join("");
  const bits = [`显示 ${aboxViewState.rows.length} 条`];
  if (r.truncated_scan) bits.push("⚠️ 扫描超 30s 截断，仅覆盖文件前段");
  note.textContent = bits.join(" · ");
  moreBtn.style.display = r.has_more ? "" : "none";
}

// NT 上传按钮（label 包 file input，disabled 属性不生效，用 class 变灰）
function setNtBtnBusy(busy) {
  const lb = $("btn-abox-upload"), inp = $("file-abox");
  if (!lb) return;
  lb.classList.toggle("disabled", busy);
  if (inp) inp.disabled = busy;
  lb.replaceChild(document.createTextNode(busy ? "\n          上传中…\n        " : "\n          NT 文件生成 ABox\n        "), lb.firstChild);
}

async function uploadAbox() {
  const f = $("file-abox").files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  setNtBtnBusy(true);
  $("abox-upload-result").textContent = "上传中…";
  try {
    const r = await api("/api/files/abox", { method: "POST", body: fd });
    $("abox-upload-result").textContent = `⏳ 已入库 ${r.triples.toLocaleString()} 三元组 · 后台建索引中，完成前物化查询不可用（就绪后这里自动变 ✓）`;
    $("abox-upload-result").className = "hint";
    pendingIndexMsg = true;
    refreshAboxStatus();
  } catch (e) {
    $("abox-upload-result").textContent = "✗ " + e.message;
    $("abox-upload-result").className = "hint err";
  } finally {
    setNtBtnBusy(false);
  }
}

async function askLLM() {
  const q = $("ask-question").value.trim();
  if (!q) return alert("先输入问题");
  const btn = $("btn-ask");
  btn.disabled = true; btn.textContent = "思考中…";
  $("ask-error").textContent = "";
  $("ask-answer").textContent = "";
  $("ask-details").style.display = "none";
  try {
    const route = $("ask-route").value;
    const r = await api("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q, route: route }),
    });
    $("ask-answer").textContent = r.answer;
    $("ask-sparql").textContent = r.sparql;
    $("query-text").value = r.sparql;
    $("ask-csv").textContent = r.csv;
    const t = r.timings;
    $("ask-meta").textContent = `耗时：生成 SPARQL ${t.gen_ms}ms · 执行 ${t.sparql_ms}ms · 生成回答 ${t.answer_ms}ms`;
    $("ask-details").style.display = "block";
  } catch (e) {
    $("ask-error").textContent = "✗ " + e.message;
  }
  btn.disabled = false; btn.textContent = "问";
}

/* ---------- 全局 LLM 配置弹框 ---------- */

function openSettings() {
  $("settings-modal").style.display = "flex";
  $("settings-msg").textContent = "";
  // 加载当前 key 的掩码状态
  api("/api/settings").then(r => {
    $("settings-key").placeholder = r.llm_api_key_set ? "已配置（留空不修改）" : "sk-...";
    $("settings-key").value = "";
  }).catch(() => {});
}

function closeSettings() {
  $("settings-modal").style.display = "none";
}

async function saveSettings() {
  const v = $("settings-key").value.trim();
  if (!v) return ($("settings-msg").textContent = "请输入 API Key");
  try {
    await api("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ llm_api_key: v }),
    });
    $("settings-msg").textContent = "✓ 已保存";
    $("settings-msg").className = "ws-modal-err ok";
    refreshKeyStatus();
    setTimeout(closeSettings, 800);
  } catch (e) {
    $("settings-msg").textContent = "✗ " + e.message;
    $("settings-msg").className = "ws-modal-err";
  }
}

async function refreshKeyStatus() {
  const el = $("ask-key-status");
  if (!el) return;
  try {
    const r = await api("/api/settings");
    if (r.llm_api_key_set) {
      el.innerHTML = `LLM Key: <span class="key-ok">✓ 已配置</span>`;
    } else {
      el.innerHTML = `LLM Key: <span class="key-miss">✗ 未配置</span> <a href="javascript:void(0)" onclick="openSettings()">去配置</a>`;
    }
  } catch {
    el.textContent = "";
  }
}

/* ---------- 启动 ---------- */

// 搭建页提交后跳回（/?from=builder）：一次性成功提示
{
  const params = new URLSearchParams(location.search);
  if (params.get("from") === "builder") {
    history.replaceState(null, "", "/");
    showBuilderToast("✓ 本体已生成并保存，拓扑图已刷新");
  }
}

// 先取激活工作空间 id（localStorage 一切按空间隔离），再初始化各面板
(async function initApp() {
  try {
    const w = await (await fetch("/api/workspaces")).json();
    WS_ID = w.active;
  } catch {}
  lsMigrate(ONT_MODE_KEY);
  lsMigrate(MAPPING_MODE_KEY);
  lsMigrate(ABOX_ENABLED_KEY);
  lsMigrate(ROUTE_KEY);
  setRoute(currentRoute());
  $("ask-route").addEventListener("change", e => setRoute(e.target.value));
  $("query-route").addEventListener("change", e => setRoute(e.target.value));
  refreshKeyStatus();
  setOntologyMode(currentOntMode());
  setMappingMode(currentMapMode());
  setAboxEnabled(currentAboxEnabled());
  refreshStatus();
  typeHint();
  refreshQueries();
  refreshAboxStatus();
  refreshGraph();
  // 锁定 ABox 两个按钮宽度：状态文字（"生成中…"/"上传中…"）变化不引起按钮跳动
  for (const el of [$("btn-materialize"), $("btn-abox-upload")]) {
    if (el) el.style.minWidth = el.offsetWidth + "px";
  }
  api("/api/materialize/job").then(j => {
    if (j && ["running", "merging", "done"].includes(j.phase)) startJobPoll();
  }).catch(() => {});
  setInterval(refreshStatus, 3000);
  setInterval(refreshQueries, 5000);
})();
