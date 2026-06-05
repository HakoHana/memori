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
    if (currentPage === "diaries") loadDiaryMonths();
    if (currentPage === "persona") loadPersona();
    if (currentPage === "system") loadSystemStats();
  });
});

/* ── API 工具 ── */
async function apiGet(path) {
  const r = await fetch(API + path);
  const j = await r.json();
  if (j.status === "error") throw new Error(j.message);
  return j.data;
}
async function apiPost(path, body) {
  const r = await fetch(API + path, { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) });
  const j = await r.json();
  if (j.status === "error") throw new Error(j.message);
  return j.data;
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
  const svg = d3.select("#graph-svg");
  svg.selectAll("*").remove();
  const container = document.getElementById("canvas-container");
  const w = container.clientWidth || 800, h = container.clientHeight || 500;

  if (!data.nodes.length) {
    svg.append("text").attr("x", w/2).attr("y", h/2).attr("text-anchor","middle").attr("fill","#666").text("暂无图谱数据");
    return;
  }

  svg.attr("viewBox", [0, 0, w, h]);

  // Build node/edge data
  const nodes = data.nodes.map(n => ({...n, id: n.id}));
  const nodeMap = new Map(nodes.map(n => [n.id, n]));
  const links = data.edges
    .filter(e => nodeMap.has(e.source) && nodeMap.has(e.target))
    .map(e => ({...e, source: e.source, target: e.target}));

  // Scale for node size
  const maxDegree = d3.max(nodes, n => n.degree) || 1;

  // Simulation
  const sim = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(links).id(d => d.id).distance(80).strength(0.3))
    .force("charge", d3.forceManyBody().strength(-120))
    .force("center", d3.forceCenter(w/2, h/2))
    .force("collision", d3.forceCollide().radius(d => 8 + Math.sqrt(d.degree / maxDegree) * 12));

  // Zoom
  const g = svg.append("g");
  svg.call(d3.zoom().scaleExtent([0.2, 5]).on("zoom", (e) => g.attr("transform", e.transform)));

  // Edges
  const link = g.append("g").selectAll("line").data(links).join("line")
    .attr("stroke", "#30363d").attr("stroke-width", d => Math.max(0.5, d.weight * 2))
    .attr("stroke-opacity", 0.6);

  // Edge labels
  const linkLabel = g.append("g").selectAll("text").data(links).join("text")
    .text(d => d.relation)
    .attr("font-size", 9).attr("fill", "#666").attr("text-anchor", "middle");

  // Nodes
  const node = g.append("g").selectAll("g").data(nodes).join("g").call(
    d3.drag()
      .on("start", (e, d) => { if (!e.active) sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
      .on("drag", (e, d) => { d.fx = e.x; d.fy = e.y; })
      .on("end", (e, d) => { if (!e.active) sim.alphaTarget(0); d.fx = null; d.fy = null; })
  );

  node.append("circle")
    .attr("r", d => 6 + Math.sqrt(d.degree / maxDegree) * 10)
    .attr("fill", d => COLORS[d.type] || "#666")
    .attr("stroke", "#fff").attr("stroke-width", 1)
    .attr("opacity", 0.85);

  node.append("text")
    .text(d => d.label.length > 12 ? d.label.slice(0,12)+"…" : d.label)
    .attr("dx", d => 8 + Math.sqrt(d.degree / maxDegree) * 10 + 4)
    .attr("dy", 4)
    .attr("font-size", d => d.type === "atom_type" ? 13 : 11)
    .attr("fill", d => d.type === "atom_type" ? "#fff" : "#8b949e");

  // Click handler
  node.on("click", (e, d) => showNodeDetail(d));

  // Tick
  sim.on("tick", () => {
    link.attr("x1", d => d.source.x).attr("y1", d => d.source.y)
        .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
    linkLabel.attr("x", d => (d.source.x + d.target.x)/2)
             .attr("y", d => (d.source.y + d.target.y)/2);
    node.attr("transform", d => `translate(${d.x},${d.y})`);
  });

  graphSim = sim;
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
   记忆管理
   ═══════════════════════════════════════ */

async function loadMemories() {
  memKeyword = document.getElementById("mem-search").value;
  memType = document.getElementById("mem-type-filter").value;
  try {
    let url = `/memories?page=${memPage}&page_size=50`;
    if (memKeyword) url += `&keyword=${encodeURIComponent(memKeyword)}`;
    if (memType) url += `&type=${memType}`;
    const data = await apiGet(url);
    renderMemories(data);
  } catch(e) { console.error(e); }
}

function renderMemories(data) {
  document.getElementById("mem-count").textContent = data.total;
  const list = document.getElementById("mem-list");
  list.innerHTML = data.atoms.map(a => `
    <div class="mem-card" onclick="editMemory(${a.id})">
      <span class="mem-type">${TYPE_LABELS[a.type] || a.type}</span>
      <div class="mem-content">
        <div class="c">${escapeHtml(a.content)}</div>
        <div class="m">${a.date || ""} · 重要度 ${a.importance} · 访问 ${a.access_count} 次 · ID ${a.id}</div>
        ${a.diary_snippet ? `<div class="m" style="color:#666">📝 ${escapeHtml(a.diary_snippet)}</div>` : ""}
      </div>
      <div class="mem-actions">
        <button onclick="event.stopPropagation();editMemory(${a.id})">✏️</button>
        <button onclick="event.stopPropagation();deleteMemory(${a.id})" style="color:var(--danger)">🗑</button>
      </div>
    </div>
  `).join("");

  // Pagination
  const totalPages = Math.ceil(data.total / data.page_size);
  const pg = document.getElementById("mem-pagination");
  pg.innerHTML = Array.from({length: Math.min(totalPages, 20)}, (_, i) => i+1).map(p =>
    `<button class="${p === data.page ? 'current' : ''}" onclick="memPage=${p};loadMemories()">${p}</button>`
  ).join("");
}

function editMemory(id) {
  apiGet(`/memories/detail?id=${id}`).then(atom => {
    const body = document.getElementById("modal-body");
    body.innerHTML = `
      <div class="field"><label>内容</label><textarea id="mem-edit-content">${escapeHtml(atom.content)}</textarea></div>
      <div class="field"><label>类型</label><select id="mem-edit-type">
        ${Object.entries(TYPE_LABELS).map(([k,v]) => `<option value="${k}" ${k===atom.type?"selected":""}>${v}</option>`).join("")}
      </select></div>
      <div class="field"><label>重要度 (0~1)</label><input type="number" id="mem-edit-imp" step="0.05" min="0" max="1" value="${atom.importance}"></div>
      <div class="field"><label>原文片段</label><div style="color:var(--text2);font-size:0.85em">${escapeHtml(atom.diary_snippet || "无")}</div></div>
    `;
    document.getElementById("modal-title").textContent = `编辑记忆 #${id}`;
    document.getElementById("modal").style.display = "flex";
    document.getElementById("modal").dataset.id = id;
  }).catch(e => alert(e.message));
}

async function saveEdit() {
  const id = parseInt(document.getElementById("modal").dataset.id);
  const content = document.getElementById("mem-edit-content").value;
  const type = document.getElementById("mem-edit-type").value;
  const imp = parseFloat(document.getElementById("mem-edit-imp").value);
  try {
    await apiPost("/memories/update", {id, content, atom_type: type, importance: imp});
    closeModal();
    loadMemories();
  } catch(e) { alert(e.message); }
}

async function deleteMemory(id) {
  if (!confirm("确定删除这条记忆？")) return;
  try {
    await apiPost("/memories/delete", {id});
    loadMemories();
  } catch(e) { alert(e.message); }
}

function closeModal(e) {
  if (e && e.target !== e.currentTarget) return;
  document.getElementById("modal").style.display = "none";
}

/* ═══════════════════════════════════════
   日记
   ═══════════════════════════════════════ */

async function loadDiaryMonths() {
  try {
    const data = await apiGet("/diaries");
    const sel = document.getElementById("diary-month");
    sel.innerHTML = (data.months || []).map(m =>
      `<option value="${m.year}-${m.month}">${m.year}年${m.month}月</option>`
    ).join("");
    if (sel.options.length) loadDiaryDates();
  } catch(e) { console.error(e); }
}

async function loadDiaryDates() {
  const val = document.getElementById("diary-month").value;
  if (!val) return;
  const [y, m] = val.split("-");
  try {
    const data = await apiGet(`/diaries?year=${y}&month=${m}`);
    const list = document.getElementById("diary-list");
    list.innerHTML = (data.dates || []).map(d =>
      `<button onclick="loadDiaryContent('${d.date}')" data-date="${d.date}">${d.date}</button>`
    ).join("");
    if (data.dates?.length) loadDiaryContent(data.dates[0].date);
  } catch(e) { console.error(e); }
}

async function loadDiaryContent(date) {
  document.querySelectorAll("#diary-list button").forEach(b => b.classList.remove("active"));
  const btn = document.querySelector(`#diary-list button[data-date="${date}"]`);
  if (btn) btn.classList.add("active");
  document.getElementById("diary-date-hidden").value = date;
  try {
    const data = await apiGet(`/diary?date=${date}`);
    document.getElementById("diary-editor").value = data.content || "";
  } catch(e) { console.error(e); }
}

async function saveDiary() {
  const date = document.getElementById("diary-date-hidden").value;
  const content = document.getElementById("diary-editor").value;
  if (!date) return;
  try {
    await apiPost("/diary/update", {date, content});
    alert("✅ 已保存");
  } catch(e) { alert(e.message); }
}

/* ═══════════════════════════════════════
   画像
   ═══════════════════════════════════════ */

async function loadPersona() {
  try {
    const data = await apiGet("/persona");
    document.getElementById("persona-editor").value = data.persona || "（暂无画像）";
  } catch(e) { console.error(e); }
}

async function savePersona() {
  try {
    await apiPost("/persona/update", {content: document.getElementById("persona-editor").value});
    alert("✅ 已保存");
  } catch(e) { alert(e.message); }
}

/* ═══════════════════════════════════════
   系统
   ═══════════════════════════════════════ */

async function loadSystemStats() {
  try {
    const data = await apiGet("/stats");
    document.getElementById("sys-stats").innerHTML = `
      <div class="sys-card"><div class="num">${data.atoms?.total || 0}</div><div class="label">🧬 记忆原子</div></div>
      <div class="sys-card"><div class="num">${data.diary_months || 0}</div><div class="label">📔 日记月份</div></div>
      <div class="sys-card"><div class="num">${data.graph_nodes || 0}</div><div class="label">🕸 图谱节点</div></div>
      <div class="sys-card"><div class="num">${data.graph_edges || 0}</div><div class="label">🔗 图谱连接</div></div>
    `;
  } catch(e) { console.error(e); }
}

async function runImport() {
  const path = document.getElementById("import-path").value;
  const out = document.getElementById("import-output");
  out.textContent = "导入中...";
  try {
    const data = await apiPost("/import/livingmemory", {source: path});
    out.textContent = data.stdout + "\n" + (data.stderr || "") + `\n退出码: ${data.returncode}`;
  } catch(e) { out.textContent = "错误: " + e.message; }
}

/* ── 工具 ── */
function escapeHtml(s) {
  if (!s) return "";
  return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

// 添加保存按钮到编辑模态框
document.addEventListener("DOMContentLoaded", () => {
  const modal = document.getElementById("modal");
  const footer = document.createElement("div");
  footer.className = "modal-footer";
  footer.innerHTML = `<button onclick="closeModal()">取消</button><button onclick="saveEdit()" style="background:var(--accent);color:#fff;border:none">💾 保存</button>`;
  modal.querySelector(".modal-body").after(footer);

  // 默认加载图谱
  loadGraphOverview();
});
