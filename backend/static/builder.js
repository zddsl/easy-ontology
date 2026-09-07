/* easy_ontology 本体搭建页（纯 vanilla JS，无依赖；api() 辅助函数与 app.js 保持一致） */
const $ = (id) => document.getElementById(id);

const KINDS = {
  classes:          { prefix: "c", title: "类" },
  objectProperties: { prefix: "o", title: "对象属性" },
  dataProperties:   { prefix: "d", title: "数据属性" },
};
const XSD_OPTIONS = [
  ["string", "字符串"], ["integer", "整数"], ["decimal", "小数"], ["double", "双精度浮点"],
  ["date", "日期"], ["datetime", "日期时间"], ["boolean", "布尔"],
];
const DEFAULT_NS = "http://tohi.cn/2026/onto#";
const NAME_RE = /^[A-Za-z][A-Za-z0-9_]{0,63}$/;
const NS_RE = /^https?:\/\/[^\s<>"{}|^`\\]+[#/]$/;

let draft = null;                 // {version, namespace, classes, objectProperties, dataProperties}
let selected = null;              // {kind, id} | null
let saveTimer = null;
let dirty = false;

/* ---------- 基础 ---------- */

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const text = await r.text();
  let data;
  try { data = JSON.parse(text); } catch { data = text; }
  if (!r.ok) {
    const msg = (data && (data.detail?.message || (Array.isArray(data.detail?.issues) && data.detail.issues.join("\n")) || data.detail || data.error)) || text;
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return data;
}

/* ---------- 启动 ---------- */

async function loadDraft() {
  try {
    const r = await api("/api/builder/draft");
    if (r.exists) {
      draft = r.draft;
    } else {
      draft = await autoImportOntology();
    }
  } catch {
    draft = defaultDraft();
  }
  renderAll();
  $("save-status").textContent = "草稿就绪";
}

// 无草稿但已上传本体 → 自动解析回草稿并落盘，打开即可编辑导入的内容
async function autoImportOntology() {
  try {
    const s = await api("/api/status");
    if (!s.ontology) return defaultDraft();
    const r = await api("/api/builder/import", { method: "POST" });
    const d = r.draft || defaultDraft();
    const n = `${d.classes.length} 类 · ${d.objectProperties.length} 对象属性 · ${d.dataProperties.length} 数据属性`;
    try {
      await api("/api/builder/draft", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(d),
      });
    } catch {}
    const box = $("import-warnings");
    if (box) {
      const skipped = r.skipped || {};
      let lines = (r.warnings || []).map(w => `<li>${esc(w)}</li>`).join("");
      if (skipped.foreign && skipped.foreign.length)
        lines += `<li>外来命名空间元素 ${skipped.foreign.length} 个已跳过</li>`;
      box.innerHTML = `<button class="close" onclick="this.parentElement.style.display='none'">✕</button>
        <h4>已自动导入当前已保存的本体（${n}），可直接编辑</h4>${lines ? `<ul>${lines}</ul>` : ""}`;
      box.style.display = "block";
    }
    return d;
  } catch {
    return defaultDraft();
  }
}

function defaultDraft() {
  return { version: 1, namespace: DEFAULT_NS, classes: [], objectProperties: [], dataProperties: [] };
}

/* ---------- 渲染 ---------- */

function renderAll() {
  renderNs();
  renderLists();
  renderEditor();
}

function renderNs() {
  if (document.activeElement !== $("ns-input")) $("ns-input").value = draft.namespace;
  $("ns-preview").textContent = draft.namespace + "WorkOrder";
}

function displayName(item) { return item.name || "（未命名）"; }

function renderLists() {
  $("cnt-classes").textContent = draft.classes.length;
  $("cnt-oprops").textContent = draft.objectProperties.length;
  $("cnt-dprops").textContent = draft.dataProperties.length;
  const classById = Object.fromEntries(draft.classes.map(c => [c.id, c]));

  const fill = (ulId, kind, extra) => {
    const items = draft[kind];
    const ul = $(ulId);
    if (!items.length) {
      ul.innerHTML = `<li class="empty">（空，点上方"＋ 添加"）</li>`;
      return;
    }
    ul.innerHTML = "";
    items.forEach(item => {
      const li = document.createElement("li");
      const isSel = selected && selected.kind === kind && selected.id === item.id;
      if (isSel) li.className = "selected";
      const sub = extra ? extra(item) : (item.label || "");
      li.innerHTML = `<span class="iname">${esc(displayName(item))}</span><span class="ilabel">${esc(sub)}</span>`;
      li.onclick = () => { selected = { kind, id: item.id }; renderLists(); renderEditor(); };
      ul.appendChild(li);
    });
  };
  fill("list-classes", "classes", c => c.parentId ? "↳ " + displayName(classById[c.parentId] || { name: "?" }) : (c.label || ""));
  fill("list-oprops", "objectProperties", o => {
    const d = classById[o.domainId], r = classById[o.rangeId] || { name: "?" };
    return (o.label ? o.label + " · " : "") + `${d ? displayName(d) : "?"} → ${displayName(r)}`;
  });
  fill("list-dprops", "dataProperties", p => {
    const d = classById[p.domainId];
    return (p.label ? p.label + " · " : "") + `${d ? displayName(d) : "?"} : ${p.range || "?"}`;
  });
}

function renderEditor() {
  const box = $("editor-form");
  const del = $("btn-del");
  if (!selected) {
    $("editor-title").textContent = "在左侧选择或添加元素";
    box.innerHTML = `<p class="hint">搭建流程：先建类（可设父类层级），再加对象属性（类→类的关系）和数据属性（类的字段）。每步自动存草稿，点右下"生成并保存本体"提交。</p>`;
    del.disabled = true;
    return;
  }
  const { kind, id } = selected;
  const item = draft[kind].find(x => x.id === id);
  if (!item) { selected = null; return renderEditor(); }
  del.disabled = false;
  const title = KINDS[kind].title;
  $("editor-title").textContent = `编辑${title}：${displayName(item)}`;

  const opts = (items, selectedId, excludeSelf) =>
    items
      .filter(x => !(excludeSelf && x.id === id))
      .map(x => `<option value="${x.id}" ${selectedId === x.id ? "selected" : ""}>${esc(displayName(x))}${x.label ? "（" + esc(x.label) + "）" : ""}</option>`)
      .join("");

  // 多选 checklist（等价类/不相交类），勾选状态由 checkbox 自身维护，不重渲染
  const checklist = (field, title) => {
    const others = draft.classes.filter(c => c.id !== id);
    const body = others.length
      ? others.map(c => `<label class="chk"><input type="checkbox" ${(item[field] || []).includes(c.id) ? "checked" : ""} onchange="onMultiField('classes','${id}','${field}','${c.id}',this.checked)">${esc(displayName(c))}${c.label ? "（" + esc(c.label) + "）" : ""}</label>`).join("")
      : `<span class="none">（先添加其他类）</span>`;
    return `<label class="full">${title}<div class="checklist">${body}</div></label>`;
  };

  let html = `<div class="editor-grid">`;
  html += `<label>名称（英文，字母开头）<input class="iri" value="${esc(item.name || "")}" spellcheck="false" oninput="onField('${kind}','${id}','name',this.value)"></label>`;
  html += `<label>中文标签（拓扑图显示）<input value="${esc(item.label || "")}" oninput="onField('${kind}','${id}','label',this.value)"></label>`;
  html += `<label class="full">描述（comment，进问答提示词帮 LLM 认字段）<textarea rows="2" oninput="onField('${kind}','${id}','comment',this.value)">${esc(item.comment || "")}</textarea></label>`;
  if (kind === "classes") {
    html += `<label class="full">父类（subClassOf）
      <select onchange="onField('classes','${id}','parentId',this.value)">
        <option value="">（顶层）</option>
        ${opts(draft.classes, item.parentId, true)}
      </select></label>`;
    html += checklist("equivalentIds", "等价类（equivalentClass，可多选）");
    html += checklist("disjointIds", "不相交类（disjointWith，可多选）");
  } else {
    html += `<label>domain（定义在哪个类）
      <select onchange="onField('${kind}','${id}','domainId',this.value)">
        <option value="">${kind === "dataProperties" ? "（不限，全局属性）" : "（未选，提交前必填）"}</option>
        ${opts(draft.classes, item.domainId, false)}
      </select></label>`;
    if (kind === "objectProperties") {
      html += `<label>range（指向哪个类）
        <select onchange="onField('objectProperties','${id}','rangeId',this.value)">
          <option value="">（未选，提交前必填）</option>
          ${opts(draft.classes, item.rangeId, false)}
        </select></label>`;
      html += `<label>互逆属性（inverseOf）
        <select onchange="onField('objectProperties','${id}','inverseOfId',this.value)">
          <option value="">（无）</option>
          ${opts(draft.objectProperties, item.inverseOfId, true)}
        </select></label>`;
      html += `<label>父属性（subPropertyOf）
        <select onchange="onField('objectProperties','${id}','subPropertyOfId',this.value)">
          <option value="">（无）</option>
          ${opts(draft.objectProperties, item.subPropertyOfId, true)}
        </select></label>`;
    } else {
      html += `<label>值类型（xsd）
        <select onchange="onField('dataProperties','${id}','range',this.value)">
          ${XSD_OPTIONS.map(([v, t]) => `<option value="${v}" ${item.range === v ? "selected" : ""}>${t}（xsd:${v}）</option>`).join("")}
        </select></label>`;
      html += `<label class="full">父属性（subPropertyOf）
        <select onchange="onField('dataProperties','${id}','subPropertyOfId',this.value)">
          <option value="">（无）</option>
          ${opts(draft.dataProperties, item.subPropertyOfId, true)}
        </select></label>`;
    }
  }
  html += `</div>`;
  box.innerHTML = html;
}

/* ---------- 编辑操作 ---------- */

const NULLABLE_FIELDS = ["parentId", "domainId", "rangeId", "inverseOfId", "subPropertyOfId"];

function onField(kind, id, field, value) {
  const item = draft[kind].find(x => x.id === id);
  if (!item) return;
  item[field] = value === "" && NULLABLE_FIELDS.includes(field) ? null : value;
  // 文本输入只刷列表（避免重渲染丢焦点）；描述不进列表，什么都不刷；下拉已改完，安全重渲染编辑器
  if (field === "comment") {
    // textarea 自持状态，重渲染会丢焦点
  } else if (field === "name" || field === "label") {
    renderLists();
    if (field === "name") $("editor-title").textContent = `编辑${KINDS[kind].title}：${displayName(item)}`;
  } else {
    renderLists();
    renderEditor();
  }
  runIssues();
  scheduleSave();
}

function onMultiField(kind, id, field, refId, checked) {
  const item = draft[kind].find(x => x.id === id);
  if (!item) return;
  if (!Array.isArray(item[field])) item[field] = [];
  const i = item[field].indexOf(refId);
  if (checked && i < 0) item[field].push(refId);
  if (!checked && i >= 0) item[field].splice(i, 1);
  // 勾选框自身已呈现状态，只跑校验+保存，不重渲染
  runIssues();
  scheduleSave();
}

function nextId(kind) {
  const prefix = KINDS[kind].prefix;
  let max = 0;
  draft[kind].forEach(x => {
    const m = /^([a-z])(\d+)$/.exec(x.id || "");
    if (m) max = Math.max(max, parseInt(m[2], 10));
  });
  return prefix + (max + 1);
}

function addItem(kind) {
  const item = { id: nextId(kind), name: "", label: "", comment: "" };
  if (kind === "classes") { item.parentId = null; item.equivalentIds = []; item.disjointIds = []; }
  if (kind === "objectProperties") { item.domainId = null; item.rangeId = null; item.inverseOfId = null; item.subPropertyOfId = null; }
  if (kind === "dataProperties") { item.domainId = null; item.range = "string"; item.subPropertyOfId = null; }
  draft[kind].push(item);
  selected = { kind, id: item.id };
  renderAll();
  runIssues();
  scheduleSave();
}

function removeSelected() {
  if (!selected) return;
  const { kind, id } = selected;
  const item = draft[kind].find(x => x.id === id);
  if (!item) return;
  let msg = `删除${KINDS[kind].title}「${displayName(item)}」？`;
  let refs = 0;
  if (kind === "classes") {
    refs = draft.classes.filter(c => c.parentId === id).length
      + draft.objectProperties.filter(o => o.domainId === id || o.rangeId === id).length
      + draft.dataProperties.filter(p => p.domainId === id).length
      + draft.classes.reduce((n, c) => n + (c.equivalentIds || []).concat(c.disjointIds || []).filter(r => r === id).length, 0);
    if (refs) msg += `\n将同时清除 ${refs} 处对它的引用（父类/等价/不相交/domain/range 置空）`;
  }
  if (kind === "objectProperties") {
    refs = draft.objectProperties.filter(o => o.inverseOfId === id || o.subPropertyOfId === id).length;
    if (refs) msg += `\n将同时清除 ${refs} 处对它的引用（互逆/父属性置空）`;
  }
  if (kind === "dataProperties") {
    refs = draft.dataProperties.filter(p => p.subPropertyOfId === id).length;
    if (refs) msg += `\n将同时清除 ${refs} 处对它的引用（父属性置空）`;
  }
  if (!confirm(msg)) return;
  const pull = (arr) => { const i = (arr || []).indexOf(id); if (i >= 0) arr.splice(i, 1); };
  draft[kind] = draft[kind].filter(x => x.id !== id);
  if (kind === "classes") {
    draft.classes.forEach(c => {
      if (c.parentId === id) c.parentId = null;
      pull(c.equivalentIds);
      pull(c.disjointIds);
    });
    draft.objectProperties.forEach(o => { if (o.domainId === id) o.domainId = null; if (o.rangeId === id) o.rangeId = null; });
    draft.dataProperties.forEach(p => { if (p.domainId === id) p.domainId = null; });
  }
  if (kind === "objectProperties") {
    draft.objectProperties.forEach(o => { if (o.inverseOfId === id) o.inverseOfId = null; if (o.subPropertyOfId === id) o.subPropertyOfId = null; });
  }
  if (kind === "dataProperties") {
    draft.dataProperties.forEach(p => { if (p.subPropertyOfId === id) p.subPropertyOfId = null; });
  }
  selected = null;
  renderAll();
  runIssues();
  scheduleSave();
}

/* ---------- 命名空间 ---------- */

function onNamespace(value) {
  draft.namespace = value.trim();
  renderNs();
  runIssues();
  scheduleSave();
}

/* ---------- 校验（与后端 builder_service 同规则） ---------- */

function detectCycle(items, field = "parentId") {
  const parent = {};
  items.forEach(it => parent[it.id] = it[field]);
  const state = {};
  for (const start of Object.keys(parent)) {
    if (state[start]) continue;
    const path = [];
    let cur = start;
    while (cur && cur in parent && !state[cur]) {
      state[cur] = 1;
      path.push(cur);
      cur = parent[cur];
    }
    if (cur && state[cur] === 1) return cur;
    path.forEach(n => state[n] = 2);
  }
  return null;
}

function validate() {
  const errors = [];
  const ns = draft.namespace || "";
  if (!ns) errors.push("命名空间不能为空");
  else if (!NS_RE.test(ns)) errors.push("命名空间须为 http(s):// 开头且以 # 或 / 结尾");
  if (!draft.classes.length) errors.push("提交前至少需要 1 个类");

  const seen = {};
  const lower = {};
  const all = [["类", draft.classes], ["对象属性", draft.objectProperties], ["数据属性", draft.dataProperties]];
  for (const [kindName, items] of all) {
    for (const it of items) {
      const name = (it.name || "").trim();
      if (!name) errors.push(`${kindName} ${it.id} 未填名称`);
      else if (!NAME_RE.test(name)) errors.push(`${kindName}「${name}」名称须字母开头、仅字母/数字/下划线；中文请放标签`);
      else if (seen[name]) errors.push(`名称「${name}」重复`);
      else {
        seen[name] = kindName;
        if (lower[name.toLowerCase()] && lower[name.toLowerCase()] !== name)
          errors.push(`名称「${name}」与「${lower[name.toLowerCase()]}」仅大小写不同，拓扑图会混淆`);
        else lower[name.toLowerCase()] = name;
      }
      if ((it.label || "").length > 120) errors.push(`${kindName}「${name}」标签超过 120 字符`);
      if ((it.comment || "").length > 500) errors.push(`${kindName}「${name}」描述超过 500 字符`);
    }
  }
  const classIds = new Set(draft.classes.map(c => c.id));
  const byId = Object.fromEntries(draft.classes.map(c => [c.id, c]));
  draft.classes.forEach(c => {
    if (c.parentId === c.id) errors.push(`类「${displayName(c)}」的父类不能是自己`);
    else if (c.parentId && !classIds.has(c.parentId)) errors.push(`类「${displayName(c)}」的父类引用已失效`);
    for (const [f, n] of [["equivalentIds", "等价类"], ["disjointIds", "不相交类"]]) {
      (c[f] || []).forEach(r => {
        if (r === c.id) errors.push(`类「${displayName(c)}」的${n}不能是自己`);
        else if (!classIds.has(r)) errors.push(`类「${displayName(c)}」的${n}引用已失效`);
      });
    }
  });
  const cyc = detectCycle(draft.classes);
  if (cyc) errors.push(`类「${displayName(byId[cyc])}」的父类层级成环`);
  const oIds = new Set(draft.objectProperties.map(o => o.id));
  draft.objectProperties.forEach(o => {
    for (const [f, n] of [["domainId", "domain"], ["rangeId", "range"]]) {
      if (!o[f]) errors.push(`对象属性「${displayName(o)}」未选 ${n}（必填）`);
      else if (!classIds.has(o[f])) errors.push(`对象属性「${displayName(o)}」的 ${n} 引用的类不存在`);
    }
    for (const [f, n] of [["inverseOfId", "互逆属性"], ["subPropertyOfId", "父属性"]]) {
      if (!o[f]) continue;
      if (o[f] === o.id) errors.push(`对象属性「${displayName(o)}」的${n}不能是自己`);
      else if (!oIds.has(o[f])) errors.push(`对象属性「${displayName(o)}」的${n}引用已失效`);
    }
  });
  const ocyc = detectCycle(draft.objectProperties, "subPropertyOfId");
  if (ocyc) errors.push("对象属性的父属性层级成环");
  const dIds = new Set(draft.dataProperties.map(p => p.id));
  draft.dataProperties.forEach(p => {
    if (p.domainId && !classIds.has(p.domainId)) errors.push(`数据属性「${displayName(p)}」的 domain 引用的类不存在`);
    if (!XSD_OPTIONS.some(([v]) => v === p.range)) errors.push(`数据属性「${displayName(p)}」的值类型无效`);
    if (!p.subPropertyOfId) return;
    if (p.subPropertyOfId === p.id) errors.push(`数据属性「${displayName(p)}」的父属性不能是自己`);
    else if (!dIds.has(p.subPropertyOfId)) errors.push(`数据属性「${displayName(p)}」的父属性引用已失效`);
  });
  const dcyc = detectCycle(draft.dataProperties, "subPropertyOfId");
  if (dcyc) errors.push("数据属性的父属性层级成环");
  return errors;
}

function runIssues() {
  const errs = validate();
  $("issues").textContent = errs.length ? "⚠ " + errs.slice(0, 8).join("\n⚠ ") + (errs.length > 8 ? `\n…还有 ${errs.length - 8} 条` : "") : "";
}

/* ---------- 草稿保存 ---------- */

function scheduleSave() {
  dirty = true;
  $("save-status").textContent = "有未保存改动…";
  clearTimeout(saveTimer);
  saveTimer = setTimeout(doSave, 800);
}

async function doSave() {
  if (!draft) return;
  try {
    const r = await api("/api/builder/draft", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(draft),
    });
    dirty = false;
    $("save-status").textContent = `✓ 已保存 ${r.saved_at}`;
  } catch (e) {
    $("save-status").textContent = "✗ 保存失败：" + e.message;
  }
}

// 离开页面（刷新/关闭/提交跳转）时防抖可能还没触发——keepalive 兜底落盘。
// __wsSwitching 守卫：工作空间切换触发的 reload 已在切换前（旧空间时）冲洗过，
// 此时 active 已是新空间，再冲会把旧空间草稿写进新空间（跨空间污染）。
window.addEventListener("pagehide", () => {
  if (window.__wsSwitching) return;
  if (dirty && draft) {
    try {
      fetch("/api/builder/draft", {
        method: "PUT", keepalive: true,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(draft),
      });
    } catch {}
  }
});

// 切换工作空间前（active 仍是本空间）同步冲洗草稿
window.addEventListener("ws:before-switch", () => { if (dirty && draft) doSave(); });

/* ---------- 导入回填 ---------- */

async function importSaved() {
  if (!confirm("导入会读取当前已保存的本体并覆盖草稿，继续？")) return;
  try {
    const r = await api("/api/builder/import", { method: "POST" });
    draft = r.draft;
    selected = null;
    renderAll();
    runIssues();
    scheduleSave();
    const box = $("import-warnings");
    const skipped = r.skipped || {};
    let lines = (r.warnings || []).map(w => `<li>${esc(w)}</li>`).join("");
    if (skipped.foreign && skipped.foreign.length)
      lines += `<li>外来命名空间元素 ${skipped.foreign.length} 个已跳过</li>`;
    if (lines) {
      box.innerHTML = `<button class="close" onclick="this.parentElement.style.display='none'">✕</button>
        <h4>导入完成（部分内容不支持，已丢弃）</h4><ul>${lines}</ul>`;
      box.style.display = "block";
    } else {
      box.style.display = "none";
      alert("导入完成，无损");
    }
  } catch (e) {
    if (e.message && e.message.includes("404")) {
      alert("还没有已保存的本体，请先在主页上传本体文件，再回来导入。");
    } else {
      alert("导入失败：" + e.message);
    }
  }
}

/* ---------- 提交 ---------- */

async function commitDraft() {
  runIssues();
  const errs = validate();
  if (errs.length) return alert("还有 " + errs.length + " 个问题，先修复再提交（见下方清单）");
  if (!confirm("生成并保存本体？\n提交后与上传的本体互相覆盖（同一落点），拓扑图将显示新本体。")) return;

  // 端点在跑/已有 ABox 的提示（与后端 commit 返回的 hint 同口径）
  let extra = "";
  try {
    const s = await api("/api/status");
    const notes = [];
    if (s.ontop) notes.push("端点正在运行，提交后需重启端点新本体才生效");
    if (s.abox) notes.push("已有 ABox 基于旧本体，需要时请重新生成");
    if (notes.length && !confirm(notes.join("；") + "。仍要提交？")) return;
  } catch { /* 状态拿不到不拦提交 */ }

  const btn = $("btn-commit");
  btn.disabled = true; btn.textContent = "提交中…";
  try {
    const r = await api("/api/builder/commit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(draft),
    });
    location.href = "/?from=builder";
  } catch (e) {
    $("issues").textContent = "✗ 提交失败：\n" + e.message;
    alert("提交失败，详见问题清单");
  }
  btn.disabled = false; btn.textContent = "生成并保存本体";
}

/* ---------- 工具 ---------- */

const esc = (s) => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ---------- 启动 ---------- */

loadDraft();
