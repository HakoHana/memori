/* settings.js — 独立配置页渲染，支持 __MEMORI_CONFIG__ 注入或 API 回退 */
(function() {

function esc(v) {
  return String(v).replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

function buildHTML(groups) {
  var html = "";
  for (var name in groups) {
    if (name === "模型提供商") continue;
    var fields = groups[name];
    html += '<div class="card"><h2>' + esc(name) + '</h2>';
    for (var i = 0; i < fields.length; i++) {
      var f = fields[i];
      var id = "cfg_" + f.key;
      var val = f.value;
      var hint = f.hint ? '<span class="field-hint">' + esc(f.hint) + '</span>' : "";
      var input = "";
      if (f.type === "bool") {
        input = '<label><input type="checkbox" id="' + id + '" ' + (val ? "checked" : "") + '> ' + esc(f.label) + hint + "</label>";
        html += '<div class="field">' + input + "</div>";
        continue;
      } else if (f.type === "text") {
        input = '<textarea id="' + id + '" rows="3">' + esc(val) + "</textarea>";
      } else if (f.type === "select") {
        var opts = "";
        for (var j = 0; j < f.options.length; j++) {
          opts += '<option value="' + f.options[j] + '"' + (val === f.options[j] ? " selected" : "") + ">" + f.options[j] + "</option>";
        }
        input = '<select id="' + id + '">' + opts + "</select>";
      } else {
        input = '<input type="text" id="' + id + '" value="' + esc(val) + '">';
      }
      html += '<div class="field"><label style="flex:1;font-size:0.92em;font-weight:500;color:#333;min-width:160px">' + esc(f.label) + hint + "</label>" + input + "</div>";
    }
    html += "</div>";
  }

  // 模型提供商
  html += '<div class="card"><h2>🔌 模型提供商</h2>';
  html += '<p style="color:#888;font-size:0.85em;margin-bottom:12px">在「基础」中选用的 ID 需与此处一致</p>';
  html += '<table class="settings-prov-table"><thead><tr>';
  html += '<th style="padding:8px 6px;text-align:left;font-weight:600">类型</th>';
  html += '<th style="padding:8px 6px;text-align:left;font-weight:600">ID</th>';
  html += '<th style="padding:8px 6px;text-align:left;font-weight:600">API 地址</th>';
  html += '<th style="padding:8px 6px;text-align:left;font-weight:600">API Key</th>';
  html += '<th style="padding:8px 6px;text-align:left;font-weight:600">模型</th><th></th>';
  html += '</tr></thead><tbody id="settings-prov-tbody"></tbody></table>';
  html += '<button class="btn-sm" onclick="renderSettingsPage.addProv()" style="margin-top:8px">+ 添加</button>';
  html += '<p style="color:#999;font-size:0.8em;margin-top:6px">类型说明: LLM=对话模型, Embed-API=远程API嵌入, Embed-Ollama=Ollama嵌入, Embed-Local=本地sentence-transformers(需pip安装)</p>';
  html += '</div>';

  // 系统操作
  html += '<div class="card"><h2>⚡ 系统</h2>';
  html += '<button onclick="renderSettingsPage.shutdown()" style="padding:8px 24px;background:#e33;color:#fff;border:none;border-radius:8px;font-size:0.9em;cursor:pointer">⏻ 停止 memori 服务</button>';
  html += '</div>';

  html += '<button onclick="renderSettingsPage.save()" style="margin-top:16px;padding:10px 32px;background:#06c;color:#fff;border:none;border-radius:8px;font-size:1em;cursor:pointer;width:100%;font-weight:500">💾 保存全部</button>';
  return html;
}

async function save() {
  var body = {};
  document.querySelectorAll("#page-settings .field").forEach(function(field) {
    var input = field.querySelector("input, select, textarea");
    if (!input) return;
    var key = input.id.replace("cfg_", "");
    if (input.type === "checkbox") body[key] = input.checked;
    else if (input.tagName === "SELECT") body[key] = input.value;
    else body[key] = input.value;
  });

  var providers = [];
  document.querySelectorAll("#settings-prov-tbody tr").forEach(function(tr) {
    var name = tr.querySelector(".pv_n")?.value?.trim();
    if (!name) return;
    providers.push({
      name: name,
      type: tr.querySelector(".pv_t")?.value || "llm",
      api_base: tr.querySelector(".pv_b")?.value?.trim() || "",
      api_key: tr.querySelector(".pv_k")?.value || "",
      model: tr.querySelector(".pv_m")?.value?.trim() || "",
    });
  });

  try {
    await Promise.all([
      fetch("/api/v1/config", { method: "PUT", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) }),
      fetch("/api/v1/providers", { method: "PUT", headers: {"Content-Type":"application/json"}, body: JSON.stringify({providers: providers}) }),
    ]);
    var el = document.getElementById("settings-toast");
    if (el) { el.textContent = "✅ 已保存"; el.style.display = "block"; setTimeout(function() { el.style.display = "none"; }, 2000); }
  } catch (e) {
    var el = document.getElementById("settings-toast");
    if (el) { el.textContent = "❌ " + (e.message || "保存失败"); el.style.display = "block"; setTimeout(function() { el.style.display = "none"; }, 3000); }
  }
}

async function loadProvs() {
  try {
    var resp = await fetch("/api/v1/providers");
    var d = await resp.json();
    if (!d.ok) return;
    var tb = document.getElementById("settings-prov-tbody");
    if (!tb) return;
    tb.innerHTML = "";
    for (var i = 0; i < (d.providers||[]).length; i++) addProvRow(d.providers[i]);
  } catch (_) {}
}

function addProvRow(p) {
  p = p || {};
  var tb = document.getElementById("settings-prov-tbody");
  if (!tb) return;
  var tr = document.createElement("tr");
  var ptype = p.type || "llm";
  // 构建类型下拉
  var typeOpts = [
    {v:"llm", l:"LLM"},
    {v:"embed:api", l:"Embed-API"},
    {v:"embed:ollama", l:"Embed-Ollama"},
    {v:"embed:local", l:"Embed-Local"},
  ];
  var typeHtml = '<select class="pv_t" style="width:100%;padding:6px 4px;border:1px solid #ddd;border-radius:6px;font-size:0.85em">';
  for (var i = 0; i < typeOpts.length; i++) {
    typeHtml += '<option value="' + typeOpts[i].v + '"' + (ptype === typeOpts[i].v ? ' selected' : '') + '>' + typeOpts[i].l + '</option>';
  }
  typeHtml += '</select>';
  tr.innerHTML =
    '<td>' + typeHtml + '</td>' +
    '<td><input class="pv_n" value="' + esc(p.name||"") + '" placeholder="my-model" style="width:100%;padding:6px 8px;border:1px solid #ddd;border-radius:6px;box-sizing:border-box"></td>' +
    '<td><input class="pv_b" value="' + esc(p.api_base||"") + '" placeholder="https://api.openai.com/v1" style="width:100%;padding:6px 8px;border:1px solid #ddd;border-radius:6px;box-sizing:border-box"></td>' +
    '<td><input class="pv_k" type="password" value="' + esc(p.api_key||"") + '" style="width:100%;padding:6px 8px;border:1px solid #ddd;border-radius:6px;box-sizing:border-box"></td>' +
    '<td><input class="pv_m" value="' + esc(p.model||"") + '" placeholder="gpt-4o" style="width:100%;padding:6px 8px;border:1px solid #ddd;border-radius:6px;box-sizing:border-box"></td>' +
    '<td><button onclick="this.closest(\'tr\').remove()" style="background:none;border:none;cursor:pointer;color:#999;font-size:18px">✕</button></td>';
  tb.appendChild(tr);
}

async function loadConfig() {
  // 优先使用服务器注入的配置（零延迟）
  var cfg = window.__MEMORI_CONFIG__;
  if (cfg && cfg.groups) return cfg.groups;

  // 回退：从 API 获取
  try {
    var resp = await fetch("/api/v1/config");
    var data = await resp.json();
    if (data.ok && data.groups) return data.groups;
  } catch (_) {}
  return null;
}

window.renderSettingsPage = {
  render: async function() {
    var body = document.getElementById("settings-body");
    if (!body) return;

    body.innerHTML = '<div style="padding:40px;text-align:center;color:var(--text-tertiary)">加载配置中...</div>';

    var groups = await loadConfig();
    if (!groups) {
      body.innerHTML = '<p style="padding:40px;text-align:center;color:var(--text-tertiary)">暂无配置数据</p>';
      return;
    }

    body.innerHTML = buildHTML(groups);
    body.innerHTML += '<div id="settings-toast" style="display:none;position:fixed;bottom:30px;left:50%;transform:translateX(-50%);background:#1d1d1f;color:#fff;padding:10px 24px;border-radius:24px;font-size:14px;z-index:200"></div>';
    loadProvs();
  },
  save: save,
  addProv: function() { addProvRow({}); },
  shutdown: function() {
    if (!confirm("确定停止 memori 服务？")) return;
    fetch("/api/v1/shutdown", { method: "POST" });
  },
};

})();
