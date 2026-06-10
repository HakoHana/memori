/* settings.js — 独立配置页渲染，不依赖 app.js */
(function() {

function esc(v) {
  return String(v).replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

function buildHTML(groups) {
  var html = "";
  for (var name in groups) {
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
        input = '<textarea id="' + id + '" rows="3" style="width:260px;padding:7px 10px;border:1px solid #d1d1d6;border-radius:8px;font-size:0.9em">' + esc(val) + "</textarea>";
      } else if (f.type === "select") {
        var opts = "";
        for (var j = 0; j < f.options.length; j++) {
          opts += '<option value="' + f.options[j] + '"' + (val === f.options[j] ? " selected" : "") + ">" + f.options[j] + "</option>";
        }
        input = '<select id="' + id + '" style="width:260px;padding:7px 10px;border:1px solid #d1d1d6;border-radius:8px;font-size:0.9em">' + opts + "</select>";
      } else {
        input = '<input type="text" id="' + id + '" value="' + esc(val) + '" style="width:260px;padding:7px 10px;border:1px solid #d1d1d6;border-radius:8px;font-size:0.9em">';
      }
      html += '<div class="field"><label style="flex:1;font-size:0.92em;font-weight:500;color:#333;min-width:160px">' + esc(f.label) + hint + "</label>" + input + "</div>";
    }
    html += "</div>";
  }
  // 保存按钮
  html += '<button onclick="renderSettingsPage.save()" style="margin-top:16px;padding:10px 32px;background:#06c;color:#fff;border:none;border-radius:8px;font-size:1em;cursor:pointer;width:100%;font-weight:500">💾 保存全部</button>';
  return html;
}

function save() {
  var body = {};
  document.querySelectorAll("#page-settings .field").forEach(function(field) {
    var input = field.querySelector("input, select, textarea");
    if (!input) return;
    var key = input.id.replace("cfg_", "");
    if (input.type === "checkbox") body[key] = input.checked;
    else if (input.tagName === "SELECT") body[key] = input.value;
    else body[key] = input.value;
  });
  fetch("/api/v1/config", {
    method: "PUT",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  }).then(function(r) { return r.json(); }).then(function(d) {
    var el = document.getElementById("settings-toast");
    if (el) {
      el.textContent = d.ok ? "✅ 已保存" : "❌ 保存失败";
      el.style.display = "block";
      setTimeout(function() { el.style.display = "none"; }, 2000);
    }
  });
}

// 暴露给外部调用
window.renderSettingsPage = {
  render: function() {
    var body = document.getElementById("settings-body");
    if (!body) {  return; }
    var cfg = window.__MEMORI_CONFIG__;
    
    if (!cfg || !cfg.groups) {
      body.innerHTML = '<p style="padding:40px;text-align:center;color:#999">NONE</p>';
      return;
    }
    
    
    body.innerHTML = buildHTML(cfg.groups);
    body.innerHTML += '<div id="settings-toast" style="display:none;position:fixed;bottom:30px;left:50%;transform:translateX(-50%);background:#1d1d1f;color:#fff;padding:10px 24px;border-radius:24px;font-size:14px"></div>';
  },
  save: save
};

})();
