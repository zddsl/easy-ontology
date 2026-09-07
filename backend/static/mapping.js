/* 映射搭建向导：gate（本体+连通+表结构）→ ①类映射 → ②对象属性 → ③预览/手改/保存。
   草稿自动保存（800ms 防抖 PUT /api/mapping/draft）；生成在服务端做（表单是唯一事实来源）。 */
"use strict";

let elements = null;      // /api/mapping/elements：本体元素清单
let schema = null;        // /api/mapping/schema：库表结构
let draft = null;
let statusInfo = null;    // /api/status 快照（commit 覆盖确认用）
let step = 1;
let selCls = null;        // 选中的类 iri
let selRel = null;        // 选中的对象属性 iri
let saveTimer = null;
let obdaEdited = false;   // 第3步是否手改过（未重新生成）

// java.sql.Types int → range 家族（与后端 JDBC_TYPE_CATEGORY 对齐，仅前端提示用）
const JDBC_CAT = { "1": "string", "12": "string", "-1": "string", "-15": "string", "-9": "string", "2005": "string", "2011": "string",
  "-6": "integer", "5": "integer", "4": "integer", "-5": "integer", "2": "decimal", "3": "decimal",
  "7": "double", "6": "double", "8": "double", "91": "date", "93": "datetime", "-101": "datetime", "-102": "datetime", "-7": "boolean", "16": "boolean" };

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
const CIRC = "①②③④⑤⑥⑦⑧⑨⑩";

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
  const r = await fetch(path, init);
  if (!r.ok) {
    let msg = `HTTP ${r.status}`, detail = null;
    try { detail = await r.json(); } catch (e) { /* ignore */ }
    if (detail) {
      if (typeof detail.detail === "string") msg = detail.detail;
      else if (detail.detail && detail.detail.message) {
        msg = detail.detail.message;
        if (detail.detail.issues) msg += "\n" + detail.detail.issues.join("\n");
      } else msg = JSON.stringify(detail);
    }
    throw new Error(msg);
  }
  return r.json();
}

// ---------- 闸门 ----------

function gateItem(text, state) {
  const li = document.createElement("li");
  li.className = state;
  li.textContent = text;
  $("gate-list").appendChild(li);
}

async function init() {
  $("gate-list").innerHTML = "";
  $("gate-error").textContent = "";
  $("gate").style.display = "";
  $("wizard").style.display = "none";
  try {
    gateItem("读取本体元素…", "loading");
    elements = await api("/api/mapping/elements");
    if (!elements.classes.length) throw new Error("当前本体没有类，先去本体搭建页添加");
    gateItem(`本体 ✓ ${elements.classes.length} 类 · ${elements.objectProperties.length} 对象属性 · ${elements.dataProperties.length} 数据属性`, "ok");
  } catch (e) { $("gate-error").textContent = "本体：" + e.message; return; }
  try {
    gateItem("测试数据源连接…", "loading");
    const ping = await api("/api/test-connection", { method: "POST" });
    if (!ping.ok) throw new Error(ping.error || "连接失败");
    gateItem(`数据源 ✓ ${ping.ms}ms`, "ok");
  } catch (e) { $("gate-error").textContent = "数据源连不上：" + e.message + "（本页要求先连通，去主页检查配置）"; return; }
  try {
    gateItem("读取库表结构…", "loading");
    schema = await api("/api/mapping/schema");
    gateItem(`表结构 ✓ ${schema.tables.length} 张表${schema.cached ? "（缓存，改库表后在步骤①点「重新拉取表结构」）" : ""}`, "ok");
  } catch (e) { $("gate-error").textContent = "读表结构：" + e.message; return; }

  try { statusInfo = await api("/api/status"); } catch (e) { statusInfo = null; }

  const dr = await api("/api/mapping/draft");
  if (dr.exists && dr.draft) {
    draft = dr.draft;
    mergeElements();
    // 迁移：修正旧草稿中子类 iri_path 默认值（旧版没有父类继承逻辑，可能存了错误路径）
    migrateDraftIriPaths();
    if (dr.stale && dr.stale.length) {
      $("stale-bar").style.display = "";
      $("stale-bar").innerHTML = "<h4>⚠ 本体与草稿不一致</h4><ul>" + dr.stale.map((s) => `<li>${esc(s)}</li>`).join("") + "</ul>";
    } else { $("stale-bar").style.display = "none"; }
  } else {
    draft = defaultDraft();
  }
  $("gate").style.display = "none";
  $("wizard").style.display = "";
  setStatusText("已就绪");
  gotoStep(1);
}

// ---------- 草稿构造 ----------

function migrateDraftIriPaths() {
  // 修正旧草稿：若子类 iri_path 等于类名小写（旧默认值），且父类已有 iri_path，则继承父类
  const draftByIri = Object.fromEntries(draft.classes.map((c) => [c.iri, c]));
  for (const e of elements.classes) {
    if (!e.parentIri) continue;
    const child = draftByIri[e.iri];
    const parent = draftByIri[e.parentIri];
    if (!child || !parent) continue;
    // 只修正还是旧默认值的情况（避免覆盖用户主动设置的值）
    const defaultPath = e.name.toLowerCase();
    if (child.iri_path === defaultPath && parent.iri_path && parent.iri_path !== defaultPath) {
      child.iri_path = parent.iri_path;
    }
  }
}

function clsEntry(e) {
  // 子类默认继承父类的 iri_path，保证子类个体 IRI 与父类一致（Ontop 推理不断链）
  let iri_path = e.name.toLowerCase();
  if (e.parentIri) {
    const parentE = elements.classes.find((c) => c.iri === e.parentIri);
    const parentDraft = parentE ? draft?.classes?.find((c) => c.iri === e.parentIri) : null;
    if (parentDraft && parentDraft.iri_path) iri_path = parentDraft.iri_path;
    else if (parentE) iri_path = parentE.name.toLowerCase();
  }
  return { iri: e.iri, name: e.name, label: e.label || "", parentIri: e.parentIri || null,
    table: null, pk: [], iri_path, iri_sep: "-",
    props: elements.dataProperties.filter((p) => p.domainIri === e.iri)
      .map((p) => ({ iri: p.iri, name: p.name, col: null })) };
}

function relEntry(e) {
  return { iri: e.iri, name: e.name, domainIri: e.domainIri, rangeIri: e.rangeIri,
    mode: null, table: null, domain_cols: [], range_cols: [] };
}

function defaultDraft() {
  return { version: 1, namespace: elements.namespace,
    ontology: { digest: elements.digest },
    classes: elements.classes.map(clsEntry),
    relations: elements.objectProperties.map(relEntry),
    edited_obda: null, updated_at: null };
}

function mergeElements() {
  // 本体后来新增的元素补进草稿（domain 匹配的属性照默认勾选）；已删的保留（黄条已提示）
  const cIris = new Set(draft.classes.map((c) => c.iri));
  for (const e of elements.classes) if (!cIris.has(e.iri)) draft.classes.push(clsEntry(e));
  const rIris = new Set(draft.relations.map((r) => r.iri));
  for (const e of elements.objectProperties) if (!rIris.has(e.iri)) draft.relations.push(relEntry(e));
  if (!draft.namespace) draft.namespace = elements.namespace;
}

const clsByIri = (iri) => draft.classes.find((c) => c.iri === iri);
const relByIri = (iri) => draft.relations.find((r) => r.iri === iri);
const elemOpByIri = (iri) => elements.objectProperties.find((p) => p.iri === iri);
const elemDpByIri = (iri) => elements.dataProperties.find((p) => p.iri === iri);

function tableCols(table) {
  if (!table) return null;
  const t = schema.tables.find((x) => x.name === table);
  return t ? t.columns : [];
}

function guessCol(table, prop) {
  const cols = tableCols(table);
  if (!cols) return null;
  const pl = (prop.name || "").toLowerCase();
  const hit = cols.find((c) => c.name.toLowerCase() === pl)
    || cols.find((c) => c.name.toLowerCase() === (prop.label || "").toLowerCase())
    || cols.find((c) => c.name.toLowerCase().includes(pl));
  return hit ? hit.name : null;
}

const clsComplete = (c) => !!(c.table && c.pk.length);
const relComplete = (r) => !!(r.table && r.domain_cols.length);

// ---------- 步骤切换 ----------

async function gotoStep(n) {
  if (n === 3) {
    if (!draft.classes.some(clsComplete)) { alert("至少先在步骤①为一个类配置数据表和主键"); return; }
    if (n > step && draft.edited_obda && obdaEdited
        && !confirm("重新生成会覆盖你在第 ③ 步的手工修改，继续？")) return;
  }
  step = n;
  for (let i = 1; i <= 3; i++) {
    $("step-" + i).style.display = i === n ? "" : "none";
    $("stab-" + i).className = i === n ? "active" : (i < n ? "done" : "");
  }
  if (n === 1) renderStep1();
  if (n === 2) renderStep2();
  if (n === 3) await renderStep3();
}

// ---------- 步骤①：类映射 ----------

function renderStep1() {
  const mapped = draft.classes.filter(clsComplete).length;
  $("cnt-mapped").textContent = mapped;
  $("cnt-classes").textContent = draft.classes.length;
  const elemClsIris = new Set(elements.classes.map((c) => c.iri));
  $("list-cls").innerHTML = draft.classes.map((c) => {
    const stale = !elemClsIris.has(c.iri) ? " ⚠" : "";
    const ok = clsComplete(c);
    return `<li class="${selCls === c.iri ? "selected" : ""}" onclick="selCls='${esc(c.iri)}';renderStep1()">
      <span class="iname">${esc(c.name)}${stale}</span>
      ${ok ? `<span class="pill ok" title="已映射到 ${esc(c.table)}">✓ ${esc(c.table)}</span>` : `<span class="pill">未映射</span>`}</li>`;
  }).join("") || '<li class="empty">（本体没有类）</li>';
  const c = clsByIri(selCls);
  $("cls-title").textContent = c ? `类：${c.name}${c.label ? " · " + c.label : ""}` : "在左侧选择类";
  $("cls-form").innerHTML = c ? clsFormHtml(c)
    : '<p class="hint">选一个类，选数据表、主键（可多列，勾选顺序即 IRI 拼接顺序），再勾选要映射的数据属性。</p>';
}

function clsFormHtml(c) {
  const cols = tableCols(c.table);
  // 使用 datalist 实现搜索功能，不区分大小写
  const tableDatalist = schema.tables.map((t) =>
    `<option value="${esc(t.name)}">${esc(t.name)}</option>`).join("");
  const tableInput = `<input type="text" list="table-list-${esc(c.iri)}" placeholder="搜索表名..." value="${esc(c.table || "")}" onchange="onClsTable('${esc(c.iri)}',this.value)">
    <datalist id="table-list-${esc(c.iri)}">${tableDatalist}</datalist>`;
  const pkGroup = orderedColsHtml(cols, c.pk, (n) => `onTogglePk('${esc(c.iri)}','${esc(n)}',this.checked)`);
  const preview = iriTpl(c.pk, c.iri_path || c.name.toLowerCase(), c.iri_sep || "-");

  // 数据属性搜索框
  const propSearchInput = `<input type="text" id="prop-search-${esc(c.iri)}" placeholder="搜索数据属性..." class="prop-search" oninput="filterProps('${esc(c.iri)}',this.value)">`;

  const propsHtml = elements.dataProperties.length
    ? `<div class="prop-search-wrapper">${propSearchInput}</div><div class="prop-list" id="prop-list-${esc(c.iri)}">` + elements.dataProperties.map((p) => {
        const on = c.props.some((x) => x.iri === p.iri);
        const tag = p.domainIri === c.iri ? '<span class="ptag">domain</span>' : "";
        const colSel = on
          ? colSelectHtml(c, p, cols)
          : "";
        return `<div class="prop-row ${on ? "" : "off"}" data-prop-name="${esc(p.name.toLowerCase())}">
          <input type="checkbox" ${on ? "checked" : ""} onchange="toggleProp('${esc(c.iri)}','${esc(p.iri)}',this.checked)">
          <span class="pname">${esc(p.name)} ${tag}<span class="hint">xsd:${esc(p.range)}</span></span>${colSel}</div>`;
      }).join("") + "</div>"
    : '<p class="hint">本体没有数据属性</p>';
  return `
    <div class="form-group">
      <div class="grouptitle">数据表与主键</div>
      <div class="form-row"><label>数据表</label>${tableInput}</div>
      <div class="form-row"><label>主键列（有序）</label><div class="colgroup">${pkGroup}</div></div>
    </div>
    <div class="form-group">
      <div class="grouptitle">IRI 模板</div>
      ${(() => {
        const parentC = c.parentIri ? clsByIri(c.parentIri) : null;
        const parentElem = c.parentIri ? elements.classes.find((e) => e.iri === c.parentIri) : null;
        const parentName = parentElem ? parentElem.name : null;
        const parentPath = parentC ? (parentC.iri_path || parentC.name.toLowerCase()) : (parentElem ? parentElem.name.toLowerCase() : null);
        const myPath = c.iri_path || c.name.toLowerCase();
        const mismatch = parentPath && myPath !== parentPath;
        const parentHint = parentName
          ? `<span class="hint" style="display:block;margin-top:2px">子类请与父类 <b>${esc(parentName)}</b> 保持一致（父类路径段：<code>${esc(parentPath)}</code>）</span>`
          : "";
        const mismatchWarn = mismatch
          ? `<div class="warn-inline">⚠ 与父类 ${esc(parentName)} 路径段「${esc(parentPath)}」不一致，子类推理将断链</div>`
          : "";
        return `<div class="form-row"><label>路径段 / 分隔符</label><span><input value="${esc(myPath)}" oninput="onClsText('${esc(c.iri)}','iri_path',this.value)">${parentHint}
          <input class="short" value="${esc(c.iri_sep || "-")}" oninput="onClsText('${esc(c.iri)}','iri_sep',this.value)" title="多列拼接分隔符"></span></div>${mismatchWarn}`;
      })()}
      <div class="form-row"><label>预览</label><span class="iri-preview">${esc(preview)}</span></div>
    </div>
    <div class="form-group">
      <div class="grouptitle">数据属性映射 <span class="hint">domain 匹配默认勾选，其余自由勾选</span></div>
      ${propsHtml}
    </div>`;
}

function iriTpl(cols, path, sep) {
  return ":" + path + "/" + (cols || []).map((x) => `{${x}}`).join(sep || "-");
}

function colSelectHtml(c, p, cols) {
  const entry = c.props.find((x) => x.iri === p.iri);
  const col = entry ? entry.col : null;
  if (!cols) return '<select disabled><option>先选表</option></select>';
  let warn = false, colCat = null;
  if (col) {
    const info = cols.find((x) => x.name === col);
    if (info) { colCat = JDBC_CAT[info.jdbc_type]; warn = !!colCat && colCat !== p.range; }
  }
  // 使用 datalist 实现列搜索功能，不区分大小写
  const colDatalist = cols.map((x) =>
    `<option value="${esc(x.name)}">${esc(x.name)} · ${esc(x.type_name)}</option>`).join("");
  const colInput = `<input type="text" list="col-list-${esc(c.iri)}-${esc(p.iri)}" placeholder="搜索列名..." value="${esc(col || "")}" onchange="onPropCol('${esc(c.iri)}','${esc(p.iri)}',this.value)">
    <datalist id="col-list-${esc(c.iri)}-${esc(p.iri)}">${colDatalist}</datalist>`;
  return warn ? `<span title="列类型 ${colCat} 与属性 xsd:${esc(p.range)} 可能不匹配" class="colwarn">${colInput} ⚠</span>` : colInput;
}

function orderedColsHtml(cols, selected, attrFn) {
  if (!cols) return '<span class="none">先选数据表</span>';
  if (!cols.length) return '<span class="none">该表没有列</span>';
  return cols.map((x) => {
    const idx = selected.indexOf(x.name);
    return `<label class="chk"><input type="checkbox" ${idx >= 0 ? "checked" : ""} onchange="${attrFn(x.name)}">
      ${esc(x.name)} <span class="ctype">${esc(x.type_name)}</span>${idx >= 0 ? `<span class="ord-badge">${CIRC[idx] || idx + 1}</span>` : ""}</label>`;
  }).join("");
}

function onClsTable(iri, table) {
  const c = clsByIri(iri);
  c.table = table || null;
  const cols = tableCols(c.table);
  if (cols) {
    c.pk = c.pk.filter((x) => cols.some((y) => y.name === x));
    for (const p of c.props) {
      if (p.col && !cols.some((y) => y.name === p.col)) p.col = guessCol(c.table, p);
    }
  }
  renderStep1(); scheduleSave();
}

function onClsText(iri, field, value) {
  const c = clsByIri(iri);
  c[field] = value;
  // 只更新预览行，避免重渲染丢输入焦点
  const preview = iriTpl(c.pk, c.iri_path || c.name.toLowerCase(), c.iri_sep || "-");
  const el = document.querySelector("#cls-form .iri-preview");
  if (el) el.textContent = preview;
  scheduleSave();
}

function onTogglePk(iri, col, checked) {
  const c = clsByIri(iri);
  if (checked && !c.pk.includes(col)) c.pk.push(col);
  if (!checked) c.pk = c.pk.filter((x) => x !== col);
  renderStep1(); scheduleSave();
}

function toggleProp(iri, piri, checked) {
  const c = clsByIri(iri);
  if (checked) {
    const p = elemDpByIri(piri);
    if (!c.props.some((x) => x.iri === piri)) {
      c.props.push({ iri: piri, name: p ? p.name : piri, col: guessCol(c.table, p || { name: piri }) });
    }
  } else {
    c.props = c.props.filter((x) => x.iri !== piri);
  }
  renderStep1(); scheduleSave();
}

function onPropCol(iri, piri, col) {
  const c = clsByIri(iri);
  const p = c.props.find((x) => x.iri === piri);
  if (p) p.col = col || null;
  scheduleSave();
}

function filterProps(iri, searchText) {
  const container = $(`prop-list-${iri}`);
  const rows = container.querySelectorAll('.prop-row');
  const searchLower = searchText.toLowerCase();

  rows.forEach(row => {
    const propName = row.getAttribute('data-prop-name');
    if (!searchText || propName.includes(searchLower)) {
      row.style.display = '';
    } else {
      row.style.display = 'none';
    }
  });
}

// ---------- 步骤②：对象属性 ----------

function deriveMode(r) {
  const a = clsByIri(r.domainIri), b = clsByIri(r.rangeIri);
  if (!r.table || !a || !b || !a.table || !b.table) return null;
  // 大小写不敏感比较；命中即把表名规范回写为类映射里的确切写法
  // （否则手输小写 workorder 会与类映射的 WORKORDER 匹配失败→误判 junction）
  const t = r.table.toLowerCase();
  if (t === a.table.toLowerCase()) { r.table = a.table; return "domain_fk"; }
  if (t === b.table.toLowerCase()) { r.table = b.table; return "range_fk"; }
  return "junction";
}

const MODE_LABEL = { domain_fk: "并入 domain 类映射", range_fk: "新开一条映射", junction: "中间表 · 新开一条" };
const MODE_SHORT = { domain_fk: "并入", range_fk: "独立", junction: "中间表" };

function renderStep2() {
  const mapped = draft.relations.filter(relComplete).length;
  $("cnt-rel").textContent = mapped;
  $("cnt-oprops").textContent = draft.relations.length;
  $("list-rel").innerHTML = draft.relations.map((r) => {
    const a = clsByIri(r.domainIri), b = clsByIri(r.rangeIri);
    const inv = elemOpByIri(r.iri);
    const invTag = inv && inv.inverseOfIri ? " ⇄" : "";
    const ok = relComplete(r);
    const tail = ok
      ? `<span class="pill ok" title="已映射到 ${esc(r.table)}">✓ ${esc(r.table)} · ${esc(MODE_SHORT[r.mode] || "")}</span>`
      : `<span class="pill">${esc(a ? a.name : "?")} → ${esc(b ? b.name : "?")}</span>`;
    return `<li class="${selRel === r.iri ? "selected" : ""}" onclick="selRel='${esc(r.iri)}';renderStep2()">
      <span class="iname">${esc(r.name)}${invTag}</span>${tail}</li>`;
  }).join("") || '<li class="empty">（本体没有对象属性，直接去第③步）</li>';
  const r = relByIri(selRel);
  $("rel-title").textContent = r ? `对象属性：${r.name}` : "在左侧选择对象属性";
  $("rel-form").innerHTML = r ? relFormHtml(r)
    : '<p class="hint">选一个对象属性，确定它的外键放在哪张表（domain 表 / range 表 / 中间表均可）。</p>';
}

function relFormHtml(r) {
  const a = clsByIri(r.domainIri), b = clsByIri(r.rangeIri);
  const endpoints = `<div class="endpoints">
    <div class="domain-end">${esc(a ? a.name : "?")}${a && a.table ? `（${esc(a.table)}）` : "（未映射 ⚠）"}</div>
    <div class="prop-label">${esc(r.name)}</div>
    <div class="range-end">${esc(b ? b.name : "?")}${b && b.table ? `（${esc(b.table)}）` : "（未映射 ⚠）"}</div>
  </div>`;
  const inv = elemOpByIri(r.iri);
  let invNote = "";
  if (inv && inv.inverseOfIri) {
    const partner = relByIri(inv.inverseOfIri);
    const partnerName = (elemOpByIri(inv.inverseOfIri) || {}).name || "?";
    const partnerMapped = partner && relComplete(partner);
    const selfMapped = relComplete(r);
    if (partnerMapped && selfMapped) {
      invNote = `<div class="warn-inline">⚠ 互逆属性 <code>${esc(partnerName)}</code> 与本属性都配置了映射，通常只需映射其中一个，另一个由 inverseOf 公理自动改写，双向映射可能产生重复断言。</div>`;
    } else {
      invNote = `<p class="hint">⇄ 互逆属性 <code>${esc(partnerName)}</code>${
        partnerMapped ? " 已映射——inverseOf 公理查询时自动改写，本关系可不映射" : " 尚未映射，映射其中一个方向即可"}</p>`;
    }
  }
  const bothMapped = a && b && a.table && b.table;
  if (!bothMapped) {
    return `<div class="form-group"><div class="grouptitle">关系端点</div>${endpoints}</div>${invNote}
      <p class="error">两端类尚未完成映射（步骤①），先补全后再配置此关系。</p>`;
  }
  const mode = r.mode = deriveMode(r);  // 回写 draft：两端类的表变了要同步，预览/生成才一致
  // 使用 datalist 实现搜索功能，不区分大小写
  const tableDatalist = schema.tables.map((t) =>
    `<option value="${esc(t.name)}">${esc(t.name)}</option>`).join("");
  const tableInput = `<input type="text" list="rel-table-list-${esc(r.iri)}" placeholder="搜索表名..." value="${esc(r.table || "")}" onchange="onRelTable('${esc(r.iri)}',this.value)">
    <datalist id="rel-table-list-${esc(r.iri)}">${tableDatalist}</datalist>`;
  let groups = "";
  const cols = tableCols(r.table);
  if (mode === "domain_fk") {
    groups = colGroupRow(`引用 ${esc(b.name)} 主键（${esc(b.pk.join(", "))}）的外键列，顺序须与主键一致`, cols, r.domain_cols,
      (n) => `onRelCol('${esc(r.iri)}','domain_cols','${esc(n)}',this.checked)`);
  } else if (mode === "range_fk") {
    groups = colGroupRow(`引用 ${esc(a.name)} 主键（${esc(a.pk.join(", "))}）的外键列，顺序须与主键一致`, cols, r.domain_cols,
      (n) => `onRelCol('${esc(r.iri)}','domain_cols','${esc(n)}',this.checked)`);
  } else if (mode === "junction") {
    groups = colGroupRow(`domain 端：引用 ${esc(a.name)} 主键（${esc(a.pk.join(", "))}）的列`, cols, r.domain_cols,
        (n) => `onRelCol('${esc(r.iri)}','domain_cols','${esc(n)}',this.checked)`)
      + colGroupRow(`range 端：引用 ${esc(b.name)} 主键（${esc(b.pk.join(", "))}）的列`, cols, r.range_cols,
        (n) => `onRelCol('${esc(r.iri)}','range_cols','${esc(n)}',this.checked)`);
  }
  return `<div class="form-group"><div class="grouptitle">关系端点</div>${endpoints}${invNote}</div>
    <div class="form-group">
      <div class="grouptitle">外键配置 <button class="btn small" disabled title="规划中：按外键约束自动推断表和列" style="margin-left:8px;vertical-align:middle">一键配置外键（规划中）</button></div>
      <div class="form-row"><label>外键所在表</label>${tableInput}</div>
      ${groups}
      <div class="form-row"><label>target 预览</label><span class="iri-preview">${esc(relPreview(r))}</span></div>
      <div class="btn-row"><button class="btn small warn" onclick="clearRel('${esc(r.iri)}')">暂不映射此关系</button></div>
    </div>`;
}

function colGroupRow(label, cols, selected, attrFn) {
  return `<div class="form-row"><label>${label}</label><div class="colgroup">${orderedColsHtml(cols, selected, attrFn)}</div></div>`;
}

function relPreview(r) {
  const a = clsByIri(r.domainIri), b = clsByIri(r.rangeIri);
  if (!a || !b) return "";
  const aPath = a.iri_path || a.name.toLowerCase(), bPath = b.iri_path || b.name.toLowerCase();
  if (!r.table || !r.domain_cols.length) return "（未配置）";
  if (r.mode === "domain_fk") return `${iriTpl(a.pk, aPath, a.iri_sep)} :${r.name} ${iriTpl(r.domain_cols, bPath, b.iri_sep)}`;
  if (r.mode === "range_fk") return `${iriTpl(r.domain_cols, aPath, a.iri_sep)} :${r.name} ${iriTpl(b.pk, bPath, b.iri_sep)}`;
  if (r.mode === "junction") return `${iriTpl(r.domain_cols, aPath, a.iri_sep)} :${r.name} ${iriTpl(r.range_cols || [], bPath, b.iri_sep)}`;
  return "";
}

function onRelTable(iri, table) {
  const r = relByIri(iri);
  r.table = table || null;
  r.domain_cols = [];
  r.range_cols = [];
  r.mode = deriveMode(r);
  renderStep2(); scheduleSave();
}

function onRelCol(iri, field, col, checked) {
  const r = relByIri(iri);
  const arr = r[field] || [];
  if (checked && !arr.includes(col)) arr.push(col);
  if (!checked) r[field] = arr.filter((x) => x !== col);
  renderStep2(); scheduleSave();
}

function clearRel(iri) {
  const r = relByIri(iri);
  r.table = null; r.mode = null; r.domain_cols = []; r.range_cols = [];
  renderStep2(); scheduleSave();
}

// ---------- 步骤③：生成 / 手改 / 保存 ----------

async function renderStep3() {
  $("commit-result").style.display = "none";
  $("commit-result").className = "okbox";
  try {
    const r = await api("/api/mapping/generate", { method: "POST", body: draft });
    draft.edited_obda = r.obda;
    obdaEdited = false;
    $("obda-text").value = r.obda;
    $("gen-status").textContent = `已生成 ${r.n_mappings} 条映射`;
    const ws = r.warnings || [];
    $("gen-warnings").style.display = ws.length ? "" : "none";
    $("gen-warnings").innerHTML = "<h4>⚠ 软警告（不阻塞保存）</h4><ul>" + ws.map((w) => `<li>${esc(w)}</li>`).join("") + "</ul>";
    scheduleSave();
  } catch (e) {
    $("gen-status").textContent = "生成失败";
    $("gen-warnings").style.display = "";
    $("gen-warnings").innerHTML = `<h4>生成失败</h4><pre>${esc(e.message)}</pre>`;
  }
}

async function regen(force) {
  if (force && obdaEdited && !confirm("重新生成会覆盖手工修改，继续？")) return;
  await renderStep3();
}

// 外键命中率抽检：逐边抽样 join 对端表，0%=死外键（边会全悬空成幽灵）
async function fkCheck() {
  const box = $("fk-result");
  box.style.display = "";
  box.className = "warnbox";
  box.textContent = "检查中…（逐边全量 LEFT JOIN 核对，约 5~30 秒）";
  const btn = $("btn-fkcheck");
  if (btn) btn.disabled = true;
  try {
    const r = await api("/api/mapping/fk-check", { method: "POST", body: draft });
    const rows = r.results || [];
    if (!rows.length) { box.textContent = "没有可检查的外键边（关系都没配表/列）"; return; }
    const icon = { ok: "✅", warn: "⚠️", dead: "🚨", empty: "➖", error: "❓" };
    const th = "border:1px solid #bbb;padding:3px 8px;text-align:left;background:#f5f5f5";
    const td = "border:1px solid #bbb;padding:3px 8px";
    box.innerHTML = `<h4>外键命中率核对（${r.full ? "全量" : `抽样 ${r.sample_n} 行`} · ${new Date().toLocaleTimeString()}）</h4>` +
      `<table style="border-collapse:collapse"><tr><th style="${th}">边</th><th style="${th}">模式</th><th style="${th}">外键表 → 对端表</th><th style="${th}">命中 / 样本</th><th style="${th}">判定</th></tr>` +
      rows.map(x => {
        const hit = x.total == null ? `❓ ${esc(x.error || "查询失败")}`
          : `${x.hit} / ${x.total}${x.rate != null ? `（${(x.rate * 100).toFixed(1)}%）` : ""}`;
        return `<tr><td style="${td}">${esc(x.name)}</td><td style="${td}">${esc(x.mode)}</td>` +
          `<td style="${td}">${esc(x.fk_table)} → ${esc(x.other_table)}</td>` +
          `<td style="${td}">${hit}</td><td style="${td}">${icon[x.verdict] || "❓"} ${esc(x.verdict)}</td></tr>`;
      }).join("") + "</table>" +
      `<div class="hint" style="margin-top:4px">✅≥90% 健康 · ⚠️&lt;90% 脏外键（部分边悬空，属性锚定查询不受影响） · 🚨0% 死外键（整条边会物化成幽灵，建议改配置）</div>`;
  } catch (e) {
    box.className = "errbox";
    box.textContent = "检查失败：\n" + e.message;
  } finally {
    if (btn) btn.disabled = false;
  }
}

function onObdaInput(value) {
  draft.edited_obda = value;
  obdaEdited = true;
  $("gen-status").textContent = "已手动修改（未保存到项目）";
  scheduleSave();
}

async function commitMapping() {
  const cur = statusInfo && statusInfo.mapping;
  if (cur && !confirm(`将覆盖已保存的映射（当前 ${cur.n_mappings} 条 · ${cur.filename}），继续？`)) return;
  try {
    const r = await api("/api/mapping/commit", { method: "POST", body: { draft, obda: $("obda-text").value } });
    const box = $("commit-result");
    box.style.display = "";
    box.textContent = `✓ 已保存 ${r.n_mappings} 条映射 · 项目状态 ${r.state}` + (r.hint ? `\n${r.hint}` : "");
    $("gen-status").textContent = `已保存 ${r.n_mappings} 条`;
    obdaEdited = false;
    statusInfo = await api("/api/status");
  } catch (e) {
    const box = $("commit-result");
    box.style.display = "";
    box.className = "errbox";
    box.textContent = "保存失败：\n" + e.message;
  }
}

// ---------- 表结构刷新 / 草稿自动保存 ----------

async function refreshSchema() {
  try {
    schema = await api("/api/mapping/schema?refresh=1");
    if (step === 1) renderStep1();
    if (step === 2) renderStep2();
  } catch (e) { alert("拉取表结构失败：" + e.message); }
}

function setStatusText(text) { $("save-status").textContent = text; }

function scheduleSave() {
  setStatusText("有未保存改动…");
  clearTimeout(saveTimer);
  saveTimer = setTimeout(doSave, 800);
}

async function doSave() {
  try {
    const r = await api("/api/mapping/draft", { method: "PUT", body: draft });
    setStatusText("草稿已保存 " + (r.saved_at || "").slice(11));
  } catch (e) {
    setStatusText("草稿保存失败");
  }
}

// __wsSwitching 守卫：工作空间切换触发的 reload 已在切换前（旧空间时）冲洗过，
// 此时 active 已是新空间，再冲会把旧空间草稿写进新空间（跨空间污染）。
window.addEventListener("pagehide", () => {
  if (window.__wsSwitching) return;
  if (draft) {
    fetch("/api/mapping/draft", { method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(draft), keepalive: true }).catch(() => {});
  }
});

// 切换工作空间前（active 仍是本空间）同步冲洗草稿
window.addEventListener("ws:before-switch", () => { if (draft) doSave(); });

init();
