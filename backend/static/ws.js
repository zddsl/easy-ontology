/* 工作空间切换条 v3（index/builder/mapping 三页共用）。
 * 自包含：下拉面板（各空间配置徽标 + 行内删除）+ 新建弹窗（实时校验）+ 操作提示 toast。
 * 切换/新建/删除前派发 ws:before-switch 让 builder/mapping 先冲洗草稿（此时 active
 * 还是旧空间），并置 window.__wsSwitching 压制 pagehide 的二次冲洗（那时 active
 * 可能已变，再冲会跨空间污染）。
 */
(function () {
  var bar = document.getElementById("ws-bar");
  if (!bar) return;

  var data = null;    // {active, workspaces:[{id,has_*,endpoint_running,...}]}
  var panelOpen = false;

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  async function api(path, opts) {
    var r = await fetch(path, opts);
    var text = await r.text();
    var d;
    try { d = JSON.parse(text); } catch { d = text; }
    if (!r.ok) {
      var msg = (d && (d.detail && (d.detail.message || d.detail) || d.error)) || text;
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return d;
  }

  async function refresh() {
    try {
      data = await api("/api/workspaces");
    } catch (e) {
      bar.textContent = "工作空间加载失败：" + e.message;
      return;
    }
    render();
  }

  function activeWs() {
    return data && data.workspaces.find(function (w) { return w.active; });
  }

  /* ---------- 折叠条 + 下拉面板 ---------- */

  function badge(on, label) {
    return '<i class="ws-b' + (on ? " ok" : "") + '">' + label + "</i>";
  }

  function render() {
    var cur = activeWs();
    bar.innerHTML =
      '<span class="ws-label">工作空间</span>' +
      '<button type="button" id="ws-toggle" class="ws-toggle"' + (cur ? "" : " disabled") + '>' +
        '<span class="ws-name">' + esc(cur ? cur.id : "?") +
          (cur && cur.endpoint_running ? '<i class="ws-dot on" title="端点运行中"></i>' : "") +
        "</span><span class='ws-caret'>▾</span>" +
      "</button>" +
      '<button type="button" id="ws-new" class="btn small" title="新建工作空间">＋新建</button>' +
      '<div id="ws-panel" class="ws-panel" hidden></div>';
    document.getElementById("ws-toggle").addEventListener("click", togglePanel);
    document.getElementById("ws-new").addEventListener("click", openCreate);
    renderPanel();
    var p = document.getElementById("ws-panel");
    if (p) p.hidden = !panelOpen;
  }

  function renderPanel() {
    var p = document.getElementById("ws-panel");
    if (!p || !data) return;
    p.innerHTML = data.workspaces.map(function (w) {
      return '<div class="ws-row' + (w.active ? " cur" : "") + '" data-id="' + esc(w.id) + '">' +
        '<span class="ws-row-main">' +
          '<span class="ws-row-name">' + esc(w.id) + (w.active ? "<em>（当前）</em>" : "") + "</span>" +
          '<span class="ws-row-badges">' +
            badge(w.has_ontology, "本体") + badge(w.has_mapping, "映射") +
            badge(w.has_datasource, "数据源") + badge(w.has_abox, "ABox") +
          "</span>" +
        "</span>" +
        (data.workspaces.length > 1
          ? '<button type="button" class="ws-del" data-id="' + esc(w.id) + '" title="删除此空间">✕</button>'
          : "") +
      "</div>";
    }).join("");
    Array.prototype.forEach.call(p.querySelectorAll(".ws-row"), function (row) {
      row.addEventListener("click", function (e) {
        if (e.target.closest(".ws-del")) return;
        closePanel();
        doSwitch(row.getAttribute("data-id"));
      });
    });
    Array.prototype.forEach.call(p.querySelectorAll(".ws-del"), function (b) {
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        closePanel();
        doDelete(b.getAttribute("data-id"));
      });
    });
  }

  function togglePanel() {
    panelOpen = !panelOpen;
    var p = document.getElementById("ws-panel");
    if (!p) return;
    p.hidden = !panelOpen;
    if (panelOpen) refresh(); // 打开时刷新（端点状态/配置徽标可能已变）
  }

  function closePanel() {
    panelOpen = false;
    var p = document.getElementById("ws-panel");
    if (p) p.hidden = true;
  }

  document.addEventListener("click", function (e) {
    if (panelOpen && !e.target.closest("#ws-bar")) closePanel();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closePanel();
  });

  /* ---------- 切换/新建/删除的统一时序：冲洗草稿 → 操作 → 提示 → reload ---------- */

  async function switchTo(action, op, id) {
    window.__wsSwitching = true;
    window.dispatchEvent(new CustomEvent("ws:before-switch")); // builder/mapping 同步冲洗草稿
    await new Promise(function (r) { setTimeout(r, 150); });   // 等冲洗请求交递
    var resp;
    try {
      resp = await action();
    } catch (e) {
      alert("操作失败：" + e.message);
      window.__wsSwitching = false;
      refresh(); // 复位折叠条显示
      return;
    }
    try {
      sessionStorage.setItem("ws-flash", JSON.stringify({
        op: op, id: id,
        active: resp && resp.active,
        stopped: !!(resp && resp.endpoint_was_running),
      }));
    } catch {}
    location.reload();
  }

  async function doSwitch(id) {
    if (!id) return;
    await refresh(); // 端点状态以最新为准（页面加载后可能启动过端点）
    if (!data || id === data.active) return;
    var cur = activeWs();
    var msg = cur && cur.endpoint_running
      ? "切换到「" + id + "」会先停止 Ontop 端点，切过去后需手动重新启动。\n当前页面草稿会先自动保存到 " + cur.id + "。\n\n继续？"
      : "切换到「" + id + "」？";
    if (!confirm(msg)) return;
    switchTo(function () {
      return api("/api/workspaces/" + encodeURIComponent(id) + "/switch", { method: "POST" });
    }, "switch", id);
  }

  async function doDelete(id) {
    if (!id) return;
    await refresh();
    if (!data) return;
    var ws = data.workspaces.find(function (w) { return w.id === id; });
    if (!ws) return;
    var isActive = !!ws.active;
    var rest = data.workspaces.filter(function (w) { return w.id !== id; });
    var lost = [];
    if (ws.has_ontology || ws.has_mapping) lost.push("本体/映射");
    if (ws.has_datasource) lost.push("数据源配置");
    if (ws.has_abox) lost.push("ABox");
    var msg = "删除工作空间「" + id + "」？" +
      (lost.length ? "\n其中的 " + lost.join("、") + " 将全部消失，不可恢复。" : "\n该空间没有已保存的配置。") +
      (isActive && rest.length
        ? "\n\n这是当前工作空间：删除后将自动切换到「" + rest[0].id + "」" +
          (ws.endpoint_running ? "，运行中的端点会先停止" : "") + "。"
        : "");
    if (!confirm(msg)) return;
    switchTo(function () {
      return api("/api/workspaces/" + encodeURIComponent(id), { method: "DELETE" });
    }, "delete", id);
  }

  /* ---------- 新建弹窗（实时校验，替代 prompt） ---------- */

  function openCreate() {
    closePanel();
    var overlay = document.createElement("div");
    overlay.className = "ws-modal";
    overlay.innerHTML =
      '<div class="ws-modal-box">' +
        '<div class="ws-modal-title">新建工作空间</div>' +
        '<input id="ws-new-name" maxlength="32" spellcheck="false" placeholder="字母/数字/_/-，1-32 位">' +
        '<div class="ws-modal-hint">创建后自动切换过去；若端点在运行会先停止（提示会告知）</div>' +
        '<div class="ws-modal-err" id="ws-new-err"></div>' +
        '<div class="ws-modal-actions">' +
          '<button type="button" class="btn small" id="ws-new-cancel">取消</button>' +
          '<button type="button" class="btn small primary" id="ws-new-ok">创建</button>' +
        "</div>" +
      "</div>";
    document.body.appendChild(overlay);
    var input = overlay.querySelector("#ws-new-name");
    var errEl = overlay.querySelector("#ws-new-err");
    input.focus();

    function validate() {
      var v = input.value.trim();
      errEl.textContent = "";
      if (!v) return null;
      if (!/^[A-Za-z0-9_-]{1,32}$/.test(v)) {
        errEl.textContent = "名字只允许字母、数字、下划线、连字符，1-32 位";
        return null;
      }
      if (data && data.workspaces.some(function (w) { return w.id.toLowerCase() === v.toLowerCase(); })) {
        errEl.textContent = "工作空间「" + v + "」已存在";
        return null;
      }
      return v;
    }

    function ok() {
      var v = validate();
      if (!v) return;
      overlay.remove();
      switchTo(function () {
        return api("/api/workspaces", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: v }),
        });
      }, "create", v);
    }

    input.addEventListener("input", validate);
    input.addEventListener("keydown", function (e) { if (e.key === "Enter") ok(); });
    overlay.querySelector("#ws-new-ok").addEventListener("click", ok);
    overlay.querySelector("#ws-new-cancel").addEventListener("click", function () { overlay.remove(); });
    overlay.addEventListener("click", function (e) { if (e.target === overlay) overlay.remove(); });
  }

  /* ---------- 操作后的 toast（sessionStorage 跨 reload 传递） ---------- */

  function flash() {
    var raw;
    try { raw = sessionStorage.getItem("ws-flash"); } catch { return; }
    if (!raw) return;
    try { sessionStorage.removeItem("ws-flash"); } catch {}
    var f;
    try { f = JSON.parse(raw); } catch { return; }
    var msg = "";
    if (f.op === "switch") msg = "已切换到 " + f.id + (f.stopped ? "（端点已停止，需手动启动）" : "");
    else if (f.op === "create") msg = "已创建并切换到 " + f.id + (f.stopped ? "（端点已停止，需手动启动）" : "");
    else if (f.op === "delete") msg = "已删除 " + f.id + " · 当前：" + (f.active || "?");
    if (!msg) return;
    var t = document.createElement("div");
    t.className = "ws-toast";
    t.textContent = "✓ " + msg;
    document.body.appendChild(t);
    setTimeout(function () { t.classList.add("show"); }, 10);
    setTimeout(function () {
      t.classList.remove("show");
      setTimeout(function () { t.remove(); }, 300);
    }, 4000);
  }

  refresh();
  flash();
})();
