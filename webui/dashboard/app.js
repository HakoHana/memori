/* ═══ 主题切换（try/catch 保护，避免 iframe 下 localStorage 抛 SecurityError 阻断脚本）═══ */
(function() {
  var theme = "light";
  try { theme = localStorage.getItem("mem-theme") || "light"; } catch(e) {}
  try { document.documentElement.setAttribute("data-theme", theme); } catch(e) {}
  document.addEventListener("DOMContentLoaded", function() {
    try {
      var btn = document.getElementById("theme-btn");
      if (btn) btn.innerHTML = theme === "dark" ? "☀️ 亮色模式" : "🌙 暗色模式";
    } catch(e) {}
  });
})();
function toggleTheme() {
  try {
    var cur = document.documentElement.getAttribute("data-theme") || "dark";
    var next = cur === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("mem-theme", next); } catch(e) {}
    var btn = document.getElementById("theme-btn");
    if (btn) btn.innerHTML = next === "dark" ? "☀️ 亮色模式" : "🌙 暗色模式";
  } catch(e) {}
}

window.addEventListener("error", function(e) {
  console.error("JS ErrorEvent:", e.message, e.filename, e.lineno, e.error);
  if (e.message === "Script error." || !e.error) return;
  showErr("ERR: " + (e.error.stack || e.message).slice(0,500));
  e.preventDefault();
});
window.addEventListener("unhandledrejection", function(e) {
  console.error("Unhandled promise rejection:", e.reason);
  showErr("PROMISE: " + ((e.reason && (e.reason.stack || e.reason.message)) || e.reason));
});
function showErr(msg) {
  var el = document.getElementById("err-display");
  if (!el) {
    el = document.createElement("div");
    el.id = "err-display";
    el.style.cssText = "position:fixed;bottom:0;left:0;right:0;background:#f85149;color:#fff;padding:12px;font-size:14px;z-index:9999;white-space:pre-wrap;word-break:break-all;max-height:300px;overflow-y:auto";
    document.body.appendChild(el);
  }
  el.textContent = msg.slice(0,1000);
}

/* Memory Dashboard — 主逻辑 */

const API = "/astrbot_plugin_memory/page";
const COLORS = {
  atom_type: "#58a6ff", entity: "#3fb950", date: "#d29922",
  topic: "#bc8cff", person: "#f0883e",
};
const TYPE_LABELS = {
  episodic: " 事件", factual: " 知识", preference: " 偏好",
  planned: " 约定", relational: " 关系",
};
let currentPage = "graph";
let memPage = 1, memKeyword = "", memType = "";
let graphData = null, graphSim = null;

/* ── 路由 ── */
document.querySelectorAll(".nav-item").forEach(el => {
  el.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach(n => n.classList.remove("active"));
    el.classList.add("active");
    document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
    const page = document.getElementById("page-" + el.dataset.page);
    if (page) page.classList.add("active");
    currentPage = el.dataset.page;
    if (currentPage === "graph") loadGraphOverview();
    if (currentPage === "memories") loadMemories();
    if (currentPage === "memories") loadMemories();
    if (currentPage === "persona") loadPersona();
    if (currentPage === "system") loadSystemStats();
  });
});

/* ── API 工具（通过 iframe 桥接，由父窗口调用） ── */
async function apiGet(path) {
  const b = window.AstrBotPluginPage;
  if (!b) throw new Error("桥接不可用，请从插件页面入口打开");
  // 分离路径和查询参数
  const parts = path.split("?");
  const ep = "page/" + parts[0].replace(/^\//, "");
  const params = parts[1] ? Object.fromEntries(new URLSearchParams(parts[1])) : {};
  const r = await b.apiGet(ep, params);
  if (r && r.status === "error") throw new Error(r.message || "请求失败");
  return r && r.data !== undefined ? r.data : r;
}

async function apiPost(path, body) {
  const b = window.AstrBotPluginPage;
  if (!b) throw new Error("桥接不可用，请从插件页面入口打开");
  const ep = "page/" + path.replace(/^\//, "");
  const r = await b.apiPost(ep, body || {});
  if (r && r.status === "error") throw new Error(r.message || "请求失败");
  return r && r.data !== undefined ? r.data : r;
}

/* ═══════════════════════════════════════
   知识图谱
   ═══════════════════════════════════════ */

async function loadGraphOverview() {
  try {
    const data = await apiGet("/graph/overview");
    graphData = data;
    renderStats(data);
    renderLegend();
    renderGraph(data);
  } catch(e) { console.error(e); try { var ed = document.getElementById("err-display"); if (!ed) { ed = document.createElement("div"); ed.id = "err-display"; ed.style.cssText = "position:fixed;bottom:0;left:0;right:0;background:#f85149;color:#fff;padding:12px;font-size:14px;z-index:9999"; document.body.appendChild(ed); } ed.textContent = "loadMemories err: " + (e.message || e).slice(0,500); } catch(ex) {} }
}

function renderStats(data) {
  const el = document.getElementById("graph-stats");
  el.innerHTML = `<span> 节点 ${data.nodes.length}</span><span> 边 ${data.edges.length}</span>`;
}

function renderLegend() {
  const el = document.getElementById("graph-legend");
  el.innerHTML = Object.entries(COLORS).map(([k,v]) =>
    `<span class="legend-item"><span class="legend-dot" style="background:${v}"></span>${k}</span>`
  ).join("");
}

function renderGraph(data) {
  const container = document.getElementById("canvas-container");
  const svg = document.getElementById("graph-svg");
  svg.innerHTML = "";
  const w = container.clientWidth || 800, h = container.clientHeight || 500;
  svg.setAttribute("viewBox", "0 0 "+w+" "+h);

  if (!data.nodes.length) {
    svg.innerHTML = '<text x="'+w/2+'" y="'+h/2+'" text-anchor="middle" fill="#666" font-size="14">暂无图谱数据</text>';
    return;
  }

  const nodes = data.nodes.map((n,i) => ({...n, id:n.id, x: w/2 + (Math.random()-0.5)*w*0.6, y: h/2 + (Math.random()-0.5)*h*0.6, vx:0, vy:0}));
  const nodeMap = {};
  nodes.forEach(n => nodeMap[n.id] = n);
  const links = data.edges.filter(e => nodeMap[e.source] && nodeMap[e.target]).map(e => ({...e, source:e.source, target:e.target}));
  const maxDeg = Math.max(1, ...nodes.map(n => n.degree || 1));

  function getNode(id) { return nodes.find(n => n.id === id); }

  // Force simulation
  for (let iter = 0; iter < 120; iter++) {
    // Repulsion
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i+1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y;
        let dist = Math.sqrt(dx*dx + dy*dy) || 1;
        const force = 5000 / (dist * dist);
        const fx = dx/dist * force, fy = dy/dist * force;
        a.vx += fx; a.vy += fy;
        b.vx -= fx; b.vy -= fy;
      }
    }
    // Attraction along edges
    for (const link of links) {
      const s = nodeMap[link.source], t = nodeMap[link.target];
      if (!s || !t) continue;
      const dx = t.x - s.x, dy = t.y - s.y;
      const dist = Math.sqrt(dx*dx + dy*dy) || 1;
      const force = (dist - 80) * 0.01;
      const fx = dx/dist * force, fy = dy/dist * force;
      s.vx += fx; s.vy += fy;
      t.vx -= fx; t.vy -= fy;
    }
    // Center gravity
    for (const n of nodes) {
      n.vx += (w/2 - n.x) * 0.001;
      n.vy += (h/2 - n.y) * 0.001;
    }
    // Apply velocity + damping
    for (const n of nodes) {
      n.x += n.vx; n.y += n.vy;
      n.vx *= 0.85; n.vy *= 0.85;
      n.x = Math.max(10, Math.min(w-10, n.x));
      n.y = Math.max(10, Math.min(h-10, n.y));
    }
  }

  // Build SVG
  const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
  svg.appendChild(g);

  // Zoom
  let zoomScale = 1, zoomX = 0, zoomY = 0;
  svg.addEventListener("wheel", e => {
    e.preventDefault();
    const rect = svg.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    const dz = e.deltaY > 0 ? 0.9 : 1.1;
    zoomScale = Math.max(0.2, Math.min(5, zoomScale * dz));
    g.setAttribute("transform", "translate("+zoomX+","+zoomY+") scale("+zoomScale+")");
  }, {passive: false});

  let dragging = false, dragStartX, dragStartY, dragStartMX, dragStartMY;
  svg.addEventListener("mousedown", e => {
    if (e.target.tagName === "circle") return;
    dragging = true; dragStartX = zoomX; dragStartY = zoomY;
    dragStartMX = e.clientX; dragStartMY = e.clientY;
  });
  window.addEventListener("mousemove", e => {
    if (!dragging) return;
    zoomX = dragStartX + (e.clientX - dragStartMX);
    zoomY = dragStartY + (e.clientY - dragStartMY);
    g.setAttribute("transform", "translate("+zoomX+","+zoomY+") scale("+zoomScale+")");
  });
  window.addEventListener("mouseup", () => { dragging = false; });

  // Edges
  for (const link of links) {
    const s = nodeMap[link.source], t = nodeMap[link.target];
    if (!s || !t) continue;
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", s.x); line.setAttribute("y1", s.y);
    line.setAttribute("x2", t.x); line.setAttribute("y2", t.y);
    line.setAttribute("stroke", "#30363d");
    line.setAttribute("stroke-width", Math.max(0.5, (link.weight || 0.5) * 2));
    line.setAttribute("stroke-opacity", "0.6");
    g.appendChild(line);
  }

  // Nodes
  let selectedNode = null;
  for (const n of nodes) {
    const ng = document.createElementNS("http://www.w3.org/2000/svg", "g");
    ng.setAttribute("transform", "translate("+n.x+","+n.y+")");
    ng.style.cursor = "pointer";

    const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    const r = 6 + Math.sqrt((n.degree || 1) / maxDeg) * 10;
    circle.setAttribute("r", r);
    circle.setAttribute("fill", COLORS[n.type] || "#666");
    circle.setAttribute("stroke", "#fff");
    circle.setAttribute("stroke-width", "1");
    circle.setAttribute("opacity", "0.85");
    ng.appendChild(circle);

    const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
    const label = n.label && n.label.length > 12 ? n.label.slice(0,12)+"..." : n.label || n.canonical || "";
    text.setAttribute("x", r + 4);
    text.setAttribute("y", "4");
    text.setAttribute("font-size", n.type === "atom_type" ? "13" : "11");
    text.setAttribute("fill", n.type === "atom_type" ? "#fff" : "#8b949e");
    text.textContent = label;
    ng.appendChild(text);

    ng.addEventListener("click", () => showNodeDetail(n));
    g.appendChild(ng);

    // Basic drag for individual node
    let ndrag = false, nox, noy;
    circle.addEventListener("mousedown", e => {
      e.stopPropagation(); ndrag = true;
      nox = n.x; noy = n.y;
      dragStartMX = e.clientX; dragStartMY = e.clientY;
    });
    circle.addEventListener("mousemove", e => {
      if (!ndrag) return;
      n.x = nox + (e.clientX - dragStartMX) / zoomScale;
      n.y = noy + (e.clientY - dragStartMY) / zoomScale;
      ng.setAttribute("transform", "translate("+n.x+","+n.y+")");
    });
    circle.addEventListener("mouseup", () => { ndrag = false; });
  }
}

async function searchGraph() {
  const q = document.getElementById("graph-query").value;
  if (!q) { loadGraphOverview(); return; }
  try {
    const data = await apiPost("/graph/query", {query: q});
    if (data.nodes.length === 0) {
      document.getElementById("graph-stats").innerHTML = "<span>未找到匹配节点</span>";
      return;
    }
    renderStats(data);
    renderGraph(data);
  } catch(e) { console.error(e); }
}

function showNodeDetail(d) {
  const el = document.getElementById("graph-detail");
  const typeInfo = TYPE_LABELS[d.type] || d.type;
  el.innerHTML = `
    <div style="margin-bottom:12px"><strong style="font-size:1.1em">${d.label}</strong></div>
    <div class="field"><label>类型</label><div>${typeInfo}</div></div>
    <div class="field"><label>连接度</label><div>${d.degree} 条连接</div></div>
    <div class="field"><label>引用次数</label><div>${d.refs || 0} 次</div></div>
  `;
}

/* ═══════════════════════════════════════
/* ===== 记忆管理（表格 + 侧边窗 + 批量操作） ===== */

let currentDate = null;
let selectedIds = new Set();

async function loadMemories() {
  try {
    var kw = document.getElementById("mem-search").value;
    var y = document.getElementById("mem-year").value;
    var m = document.getElementById("mem-month").value;
    var ps = (document.getElementById("mem-page-size")||{}).value || 50;
    var url = "/memories?page=" + memPage + "&page_size=" + ps;
    if (kw) url += "&keyword=" + encodeURIComponent(kw);
    if (y) url += "&year=" + y;
    if (m) url += "&month=" + m;
    var data = await apiGet(url);
    renderTable(data);
  } catch(e) { console.error(e); }
}

function renderTable(data) {
  document.getElementById("mem-count").textContent = data.total;
  var tb = document.getElementById("mem-tbody");
  if (!data.items || !data.items.length) {
    tb.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:40px;color:var(--text2)">暂无记忆</td></tr>';
    document.getElementById("mem-pagination").innerHTML = "";
    return;
  }
  tb.innerHTML = data.items.map(function(item) {
    var sel = item.id === currentDate ? ' selected' : '';
    var checked = selectedIds.has(item.id) ? ' checked' : '';
    var typesHtml = '';
    if (item.types && item.types.length) {
      typesHtml = item.types.map(function(t) {
        return '<span class="type-dot">' + (TYPE_LABELS[t.type] || t.type) + '</span>';
      }).join('');
    }
    var statusMap = {active: '热', dormant: '温', archived: '冷'};
    var statusClass = {active: 'status-hot', dormant: 'status-warm', archived: 'status-cold'};
    var statusText = statusMap[item.status] || '热';
    var sc = statusClass[item.status] || 'status-hot';
    var ts = item.created_at ? new Date(item.created_at * 1000).toLocaleDateString() : '';
    var updatedStr = '';
    if (item.updated_at && item.updated_at !== item.created_at) {
      var ud = new Date(item.updated_at * 1000);
      updatedStr = ud.getFullYear() + '-' + String(ud.getMonth()+1).padStart(2,'0') + '-' + String(ud.getDate()).padStart(2,'0') + ' ' + String(ud.getHours()).padStart(2,'0') + ':' + String(ud.getMinutes()).padStart(2,'0');
    }
    return '<tr class="mem-row' + sel + '" onclick="openDetail(' + item.id + ')" data-id="' + item.id + '" data-date="' + item.date + '">'
      + '<td style="width:30px" onclick="event.stopPropagation()"><input type="checkbox" class="mem-cb" value="' + item.id + '"' + checked + ' onchange="toggleSel(' + item.id + ')"></td>'
      + '<td style="font-size:0.85em;color:var(--text2)">#' + item.id + '</td>'
      + '<td><div class="mem-summary"><div class="preview">' + escapeHtml(item.content) + '</div>' + (updatedStr ? '<div class="updated">' + updatedStr + ' 更新</div>' : '') + '</div></td>'
      + '<td><div class="type-dots">' + typesHtml + '</div></td>'
      + '<td class="imp-cell">' + (item.avg_importance || '-') + '</td>'
      + '<td class="imp-cell"><span class="' + sc + '">' + statusText + '</span></td>'
      + '<td style="font-size:0.85em;color:var(--text2);white-space:nowrap">' + ts + '</td>'
      + '</tr>';
  }).join('');
  var totalPages = Math.ceil(data.total / data.page_size);
  var pg = document.getElementById("mem-pagination");
  var btns = '';
  for (var p = 1; p <= Math.min(totalPages, 20); p++) {
    btns += '<button class="' + (p === data.page ? 'current' : '') + '" onclick="memPage=' + p + ';loadMemories()">' + p + '</button>';
  }
  pg.innerHTML = btns;
}

async function searchMemories() { clearSelection(); memPage = 1; loadMemories(); }

function toggleSel(id) {
  if (selectedIds.has(id)) selectedIds.delete(id); else selectedIds.add(id);
  updateBatchBar();
}
function toggleSelectAll() {
  var cbs = document.querySelectorAll('.mem-cb');
  var allChecked = document.getElementById('sel-all').checked;
  cbs.forEach(function(cb) { cb.checked = allChecked; var id = parseInt(cb.value); if (allChecked) selectedIds.add(id); else selectedIds.delete(id); });
  updateBatchBar();
}
function updateBatchBar() {
  var bar = document.getElementById('batch-bar');
  if (selectedIds.size > 0) { bar.style.display = 'block'; document.getElementById('batch-count').textContent = '已选 ' + selectedIds.size + ' 篇'; }
  else { bar.style.display = 'none'; }
}
function clearSelection() {
  selectedIds.clear();
  document.querySelectorAll('.mem-cb').forEach(function(cb) { cb.checked = false; });
  document.getElementById('sel-all').checked = false;
  updateBatchBar();
}
async function batchDelete() {
  var ids = Array.from(selectedIds);
  if (!ids.length || !confirm('确定删除 ' + ids.length + ' 篇记忆？')) return;
  try { await apiPost('/memories/batch-delete', {ids: ids}); selectedIds.clear(); updateBatchBar(); closeSidePanel(); loadMemories(); }
  catch(e) { alert(e.message); }
}

async function openDetail(id) {
  console.log('openDetail called, id=' + id);
  // Toggle: if clicking the same row, close the side panel
  if (currentDate === id && document.getElementById('mem-side').style.display === 'flex') {
    closeSidePanel(); return;
  }
  currentDate = id;
  document.querySelectorAll('.mem-row').forEach(function(r) { r.classList.remove('selected'); });
  var row = document.querySelector('.mem-row[data-id="' + id + '"]');
  if (row) row.classList.add('selected');
  document.getElementById('mem-side').style.display = 'flex';
  document.getElementById('side-diary').textContent = '加载中...';
  document.getElementById('side-imp').innerHTML = '';
  document.getElementById('side-atoms').innerHTML = '';
  try {
    console.log('Calling day API for did=' + id);
    var p = apiGet('/memories/day?did=' + id);
    var timeout = new Promise(function(_, reject) { setTimeout(function() { reject(new Error('超时')); }, 10000); });
    var data = await Promise.race([p, timeout]);
    renderSidePanel(data);
  } catch(e) { document.getElementById('side-diary').textContent = '加载失败: ' + (e.message || e); console.error('Detail error:', e); }
}

function renderSidePanel(data) {
  var diaryEl = document.getElementById('side-diary');
  if (data.diary && data.diary.content) { diaryEl.textContent = data.diary.content; }
  else { diaryEl.innerHTML = '<div style="color:var(--text2)">无日记内容</div>'; }
  var impEl = document.getElementById('side-imp');
  if (data.imp_stats && data.imp_stats.count > 0) {
    impEl.innerHTML = '<div class="side-imp-bar">'
      + '<div class="side-imp-item"><div class="num">' + data.imp_stats.avg + '</div><div class="lbl">平均</div></div>'
      + '<div class="side-imp-item"><div class="num">' + data.imp_stats.max + '</div><div class="lbl">最高</div></div>'
      + '<div class="side-imp-item"><div class="num">' + data.imp_stats.count + '</div><div class="lbl">条数</div></div>'
      + '</div>';
  }
  var atomsEl = document.getElementById('side-atoms');
  if (!data.atoms || !data.atoms.length) { atomsEl.innerHTML = '<div style="color:var(--text2);font-size:0.9em">无关键事实</div>'; return; }
  atomsEl.innerHTML = data.atoms.map(function(a, i) {
    var typeLabel = TYPE_LABELS[a.type] || a.type;
    var content = escapeHtml(a.content);
    if (content.length > 80) content = content.slice(0,80) + '...';
    return '<div class="atom-item">'
      + '<span class="atom-num">' + (i + 1) + '</span>'
      + '<div class="atom-body">'
      + '<div class="atom-c">' + content + '</div>'
      + '<div class="atom-m"><span class="atom-tag">' + typeLabel + '</span></div>'
      + '</div></div>';
  }).join('');
  window._detailData = data;
}

function editCurrentMemory() {
  var data = window._detailData;
  if (!data) return;
  var body = document.getElementById('modal-body');
  body.innerHTML = '<div class="field"><label>状态(热/温/冷)</label><select id="edit-status">'
    + '<option value="active"' + (data.status === 'active' ? ' selected' : '') + '>热</option>'
    + '<option value="dormant"' + (data.status === 'dormant' ? ' selected' : '') + '>温</option>'
    + '<option value="archived"' + (data.status === 'archived' ? ' selected' : '') + '>冷</option>'
    + '</select></div>';
  if (data.atoms) {
    data.atoms.forEach(function(a, i) {
      var typeOpts = ['episodic','factual','preference','planned','relational'].map(function(t) {
        return '<option value="' + t + '" ' + (t === a.type ? 'selected' : '') + '>' + (TYPE_LABELS[t] || t) + '</option>';
      }).join('');
      body.innerHTML += '<div style="margin-top:8px;padding:8px;border:1px solid var(--border);border-radius:6px">'
        + '<div style="font-size:0.85em;margin-bottom:4px">#' + (i+1) + ' ' + escapeHtml((a.content||'').slice(0,60)) + '</div>'
        + '<select id="edit-type-' + i + '" style="margin-right:6px;padding:4px 6px;font-size:0.85em">' + typeOpts + '</select>'
        + '<input type="number" id="edit-imp-' + i + '" value="' + a.importance + '" step="0.05" min="0" max="1" style="width:60px;padding:4px 6px;font-size:0.85em">'
        + '</div>';
    });
  }
  document.getElementById('modal-title').textContent = '编辑记忆';
  document.getElementById('modal').style.display = 'flex';
}

async function saveMemoryEdit() {
  var data = window._detailData;
  if (!data) return;
  try {
    var status = document.getElementById('edit-status').value;
    var rowEl = document.querySelector('.mem-row.selected');
    var id = rowEl ? parseInt(rowEl.getAttribute('data-id')) : 0;
    if (id) await apiPost('/memories/update-status', {id: id, status: status});
    if (data.atoms) {
      for (var i = 0; i < data.atoms.length; i++) {
        var newType = document.getElementById('edit-type-' + i);
        var newImp = document.getElementById('edit-imp-' + i);
        if (newType && newImp) {
          await apiPost('/memories/update', {id: data.atoms[i].id, atom_type: newType.value, importance: parseFloat(newImp.value)});
        }
      }
    }
    closeModal();
    loadMemories();
    if (id) openDetail(id);
  } catch(e) { alert(e.message); }
}

async function deleteCurrentMemory() {
  var rowEl = document.querySelector('.mem-row.selected');
  var id = rowEl ? parseInt(rowEl.getAttribute('data-id')) : 0;
  if (!id || !confirm('确定删除此记忆？')) return;
  try { await apiPost('/memories/delete', {id: id}); closeSidePanel(); loadMemories(); }
  catch(e) { alert(e.message); }
}

function closeSidePanel() {
  document.getElementById('mem-side').style.display = 'none';
  currentDate = null;
  document.querySelectorAll('.mem-row').forEach(function(r) { r.classList.remove('selected'); });
}

// Populate year filter
try {
  var yearSel = document.getElementById('mem-year');
  if (yearSel) {
    var y = new Date().getFullYear();
    for (var i = y; i >= y-3; i--) {
      var opt = document.createElement('option');
      opt.value = String(i);
      opt.textContent = i + '年';
      yearSel.appendChild(opt);
    }
  }
} catch(e) {}
function escapeHtml(s) {
  if (!s) return "";
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function waitForBridge(retries) {
  if (retries === undefined) retries = 20;
  if (window.AstrBotPluginPage) {
    var modal = document.getElementById("modal");
    var footer = document.createElement("div");
    footer.className = "modal-footer";
    footer.innerHTML = "<button onclick='closeModal()'>取消</button><button onclick='saveEdit()' style='background:var(--accent);color:#fff;border:none'> 保存</button>";
    if (modal) modal.querySelector(".modal-body").after(footer);
    loadGraphOverview();
    return;
  }
  if (retries <= 0) {
    console.error("桥接 SDK 加载超时");
    var pg = document.getElementById("page-graph");
    if (pg) pg.innerHTML = "<div style='padding:40px;text-align:center;color:#f85149'>❌ 桥接 SDK 加载失败</div>";
    return;
  }
  setTimeout(function() { waitForBridge(retries - 1); }, 200);
}

document.addEventListener("DOMContentLoaded", () => waitForBridge());
