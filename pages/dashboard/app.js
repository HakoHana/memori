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
  episodic: "📌 事件", factual: "📖 知识", preference: "💕 偏好",
  planned: "🎯 约定", relational: "👥 关系",
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
  } catch(e) { console.error(e); }
}

function renderStats(data) {
  const el = document.getElementById("graph-stats");
  el.innerHTML = `<span>🟣 节点 ${data.nodes.length}</span><span>🔗 边 ${data.edges.length}</span>`;
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
   记忆管理（表格）
   ═══════════════════════════════════════ */

// memPage defined above

async function loadMemories() {
try { document.getElementById('mem-tbody').innerHTML='<tr><td style="color:var(--accent);padding:20px">加载中...</td></tr>'; } catch(e) {}
  try {
    var kw = document.getElementById("mem-search").value;
    var t = document.getElementById("mem-type-filter").value;
    var url = "/memories?page=" + memPage + "&page_size=50"; console.log('api url:', url);
    if (kw) url += "&keyword=" + encodeURIComponent(kw);
    if (t) url += "&type=" + t;
    var data = await apiGet(url);
    renderMemTable(data);
  } catch(e) { console.error(e); }
}

function renderMemTable(data) {
  document.getElementById("mem-count").textContent = data.total;
  var tb = document.getElementById("mem-tbody");
  if (!data.atoms || !data.atoms.length) {
    tb.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:40px;color:var(--text2)">暂无记忆</td></tr>';
    document.getElementById("mem-pagination").innerHTML = "";
    return;
  }
  tb.innerHTML = data.atoms.map(function(a) {
    var ts = "";
    if (a.created_at) {
      var d = new Date(a.created_at * 1000);
      var pad = function(n) { return n < 10 ? "0" + n : n; };
      ts = d.getFullYear() + "-" + pad(d.getMonth()+1) + "-" + pad(d.getDate()) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
    }
    var typeLabel = TYPE_LABELS[a.type] || a.type;
    var snippet = a.diary_snippet ? '<div class="mem-snippet">' + escapeHtml(a.diary_snippet) + '</div>' : "";
    return '<tr class="mem-row" onclick="editAtomById(' + a.id + ')">'
      + '<td class="id-col">#' + a.id + '</td>'
      + '<td><div class="mem-text">' + escapeHtml(a.content) + '</div>' + snippet + '</td>'
      + '<td class="type-col"><span class="tag tag-' + a.type + '">' + typeLabel + '</span></td>'
      + '<td class="imp-col">' + a.importance + '</td>'
      + '<td class="status-col">' + (a.status || "active") + '</td>'
      + '<td class="ts-col" style="color:var(--text2);font-size:0.85em">' + ts + '</td>'
      + '</tr>';
  }).join("");

  var totalPages = Math.ceil(data.total / data.page_size);
  var pg = document.getElementById("mem-pagination");
  var btns = "";
  for (var p = 1; p <= Math.min(totalPages, 20); p++) {
    btns += '<button class="' + (p === data.page ? "current" : "") + '" onclick="memPage=' + p + ';loadMemories()">' + p + '</button>';
  }
  pg.innerHTML = btns;
}

async function searchMemories() {
  memPage = 1;
  loadMemories();
}


async function editAtomById(id) {
  try {
    var data = await apiGet("/memories/detail?id=" + id);
    editAtom(data.id, data.content, data.type, data.importance);
  } catch(e) { alert("加载失败: " + e.message); }
}

function editAtom(id, content, type, importance) {
  const body = document.getElementById("modal-body");
  const typeOpts = ["episodic","factual","preference","planned","relational"].map(t =>
    "<option value='" + t + "' " + (t===type?"selected":"") + ">" + t + "</option>"
  ).join("");
  body.innerHTML = [
    "<div class='field'><label>内容</label><textarea id='edit-content'>" + content + "</textarea></div>",
    "<div class='field'><label>类型</label><select id='edit-type'>" + typeOpts + "</select></div>",
    "<div class='field'><label>重要度 (0~1)</label><input type='number' id='edit-imp' step='0.05' min='0' max='1' value='" + importance + "'></div>",
    "<div class='field'><label>状态</label><select id='edit-status'>",
    "  <option value='active'>活跃 🔥</option>",
    "  <option value='dormant'>休眠 🫧</option>",
    "  <option value='archived'>归档 ❄️</option>",
    "</select></div>",
  ].join("\n");
  document.getElementById("modal-title").textContent = "编辑记忆 #" + id;
  document.getElementById("modal").dataset.id = id;
  document.getElementById("modal").style.display = "flex";
  const mf = document.querySelector(".modal-footer") || body.parentElement.querySelector(".modal-footer");
  if (mf) mf.innerHTML = "<button onclick='closeModal()'>取消</button><button onclick='saveEdit()' style='background:var(--accent);color:#fff;border:none'>💾 保存</button>";
}

async function saveEdit() {
  const id = parseInt(document.getElementById("modal").dataset.id);
  try {
    await apiPost("/memories/update", {
      id: id,
      content: document.getElementById("edit-content").value,
      atom_type: document.getElementById("edit-type").value,
      importance: parseFloat(document.getElementById("edit-imp").value),
    });
    closeModal();
    const dateEl = document.querySelector(".detail-date");
    if (dateEl) loadDayDetail(dateEl.textContent.replace("📅 ", ""));
  } catch(e) { alert(e.message); }
}

async function deleteMemory(id) {
  if (!confirm("确定删除这条记忆？")) return;
  try {
    await apiPost("/memories/delete", {id: id});
    loadMemories();
    document.getElementById("detail-content").style.display = "none";
    document.getElementById("detail-placeholder").style.display = "block";
  } catch(e) { alert(e.message); }
}

function closeModal(e) {
  if (e && e.target !== e.currentTarget) return;
  document.getElementById("modal").style.display = "none";
}

function escapeJs(s) {
  if (!s) return "";
  return s.replace(/"/g, " ").replace(/\\/g, " ").replace(/\n/g, " ").replace(/\r/g, " ").trim();
}


// DOMContentLoaded 后开始等待桥接

async function loadPersona() {
  try {
    const data = await apiGet("/persona");
    document.getElementById("persona-editor").value = data.persona || "（暂无画像）";
  } catch(e) { console.error(e); }
}

async function savePersona() {
  try {
    await apiPost("/persona/update", {content: document.getElementById("persona-editor").value});
  } catch(e) { alert(e.message); }
}

/* ===== 系统 ===== */

async function loadSystemStats() {
  try {
    const data = await apiGet("/stats");
    document.getElementById("sys-stats").innerHTML = [
      '<div class="sys-card"><div class="num">' + (data.atoms?.total || 0) + '</div><div class="label">🧬 记忆原子</div></div>',
      '<div class="sys-card"><div class="num">' + (data.diary_months || 0) + '</div><div class="label">📔 日记月份</div></div>',
      '<div class="sys-card"><div class="num">' + (data.graph_nodes || 0) + '</div><div class="label">🕸 图谱节点</div></div>',
      '<div class="sys-card"><div class="num">' + (data.graph_edges || 0) + '</div><div class="label">🔗 图谱连接</div></div>',
    ].join("");
  } catch(e) { console.error(e); }
}


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
    footer.innerHTML = "<button onclick='closeModal()'>取消</button><button onclick='saveEdit()' style='background:var(--accent);color:#fff;border:none'>💾 保存</button>";
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
