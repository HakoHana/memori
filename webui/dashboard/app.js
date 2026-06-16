(() => {
  "use strict";

  /* ================================================================
     State
     ================================================================ */
  const state = {
    page: "graph",
    memory: {
      items: [],
      total: 0,
      page: 1,
      pageSize: 20,
      hasMore: false,
      selected: new Set(),
      keyword: "",
      session: "",
      status: "all",
    },
    selectedMemory: null,
    isEditing: false,
    _detailCache: null,
    _nodeDetailCache: null,
    _recallCache: null,
    _systemCache: null,
    pendingSearch: null,
    personas: {
      users: [],
      keyword: "",
    },
    _personaDetailUid: null,
  };

  /* ================================================================
     Memori API — 直接调用独立记忆系统
     ================================================================ */
  var API_BASE = "/api/v1";

  function mapApiPath(path) {
    // 分离路径和查询参数
    var qi = path.indexOf("?");
    var base = qi !== -1 ? path.substring(0, qi) : path;
    var qs = qi !== -1 ? path.substring(qi + 1) : "";

    // 记忆详情: memories/day?did=X → /memories/{id}
    if (base === "memories/day" && qs.startsWith("did=")) {
      var did = qs.substring(4).split("&")[0];
      return "/memories/" + did;
    }
    // 用户详情: users/detail?uid=X → /users/{uid}
    if (base === "users/detail" && qs.startsWith("uid=")) {
      var uid = qs.substring(4).split("&")[0];
      return "/users/" + encodeURIComponent(uid);
    }
    // 路径映射：日记端点
    if (base === "diary" || base === "diary/update") base = "diaries";
    return "/" + base + (qs ? "?" + qs : "");
  }

  async function apiRequest(path, options) {
    options = options || {};
    var method = options.method || "GET";
    var body = options.body;
    var retries = options.retries || 2;
    var base = path.indexOf("?") !== -1 ? path.substring(0, path.indexOf("?")) : path;

    // 特殊处理：memories/delete → DELETE /memories/{id}
    if ((base === "memories/delete") && body && body.id) {
      var delResp = await fetch(API_BASE + "/memories/" + body.id, { method: "DELETE" });
      if (!delResp.ok) throw new Error("HTTP " + delResp.status);
      return await delResp.json();
    }
    // 批量删除
    if ((base === "memories/batch-delete") && body && body.ids) {
      for (var i = 0; i < body.ids.length; i++) {
        await apiRequest("memories/delete", { method: "POST", body: { id: body.ids[i] } });
      }
      return { ok: true };
    }
    // 特殊处理：memories/update → PUT /memories/{id} 并转换 field/value 格式
    if ((base === "memories/update") && body && body.memory_id) {
      var upBody = {};
      if (body.field === "content") upBody.content = body.value;
      else if (body.field === "importance") upBody.importance = parseFloat(body.value);
      else if (body.field === "status") upBody.status = body.value;
      var upResp = await fetch(API_BASE + "/memories/" + body.memory_id, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(upBody),
      });
      if (!upResp.ok) throw new Error("HTTP " + upResp.status);
      return await upResp.json();
    }

    var url = API_BASE + mapApiPath(path);
    var lastError;
    for (var attempt = 0; attempt <= retries; attempt++) {
      try {
        var fetchOpts = { method: method, headers: { "Content-Type": "application/json" } };
        if (body && method !== "GET") fetchOpts.body = JSON.stringify(body);
        var resp = await fetch(url, fetchOpts);
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return await resp.json();
      } catch (e) {
        lastError = e;
        if (attempt === retries) throw e;
        await new Promise(function(r) { setTimeout(r, Math.min(1000 * Math.pow(2, attempt), 5000)); });
      }
    }
    throw lastError || new Error("请求失败");
  }

  function unwrapApiData(response) {
    if (!response) return {};
    // memori 标准格式: { ok: true, ... }
    if (response.ok === true) {
      var data = Object.assign({}, response);
      delete data.ok;
      return data;
    }
    return response;
  }

  function normalizeImportance(value) {
    var n = Number(value);
    if (!Number.isFinite(n)) n = 0.5;
    if (n <= 1) n *= 10;
    return Math.min(10, Math.max(0, n));
  }

  function getDetailText(detail) {
    return detail.text || detail.content || detail.summary || "";
  }

  /* ── frontmatter 处理 ── */
  function stripFrontmatter(text) {
    if (!text) return "";
    // 标准格式：---\n...\n---\nbody
    if (text.startsWith("---")) {
      var end = text.indexOf("\n---", 3);
      if (end !== -1) return text.substring(end + 4).trim();
    }
    // 裸 YAML：开头的若干行 key: value 没有 --- 包裹
    var lines = text.split("\n");
    var fmKeys = ["date", "mood", "importance", "topics", "sentiment", "diary_id",
                  "atom_count", "tags", "tier", "version"];
    var fmLineCount = 0;
    for (var i = 0; i < lines.length && i < 20; i++) {
      var line = lines[i].trim();
      if (!line) { fmLineCount++; continue; }
      var m = line.match(/^([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.*)$/);
      if (m && (fmKeys.indexOf(m[1]) !== -1 || i < 3)) {
        fmLineCount++;
      } else {
        break;
      }
    }
    if (fmLineCount > 0) {
      text = lines.slice(fmLineCount).join("\n").trim();
      text = text.replace(/^---+/, "").trim();
    }
    return text;
  }

  function stripMarkdown(text) {
    if (!text) return "";
    return text
      .replace(/```[\s\S]*?```/g, "")
      .replace(/`([^`]+)`/g, "$1")
      .replace(/!\[([^\]]*)\]\([^)]+\)/g, "$1")
      .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
      .replace(/\*\*([^*]+)\*\*/g, "$1")
      .replace(/__([^_]+)__/g, "$1")
      .replace(/\*([^*\n]+)\*/g, "$1")
      .replace(/~~([^~]+)~~/g, "$1")
      .replace(/^#{1,6}\s+/gm, "")
      .replace(/^[\s]*[-*+]\s+/gm, "")
      .replace(/^\s*\d+\.\s+/gm, "")
      .replace(/^>\s+/gm, "")
      .replace(/^---+$/gm, "")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  function cleanDisplayText(text) {
    return stripMarkdown(stripFrontmatter(text || ""));
  }

  function renderWikilinks(text) {
    return esc(text).replace(/\[\[([^\]]+?)\]\]/g, '<a class="wikilink" onclick="searchWikilink(\'$1\')" style="color:var(--accent);cursor:pointer;text-decoration:underline">$1</a>');
  }

  function searchWikilink(name) {
    var input = document.getElementById("graph-query-input");
    if (input) input.value = name;
    var graphBtn = document.querySelector('.nav-item[data-page="graph"]');
    if (graphBtn) graphBtn.click();
    setTimeout(function() {
      var btn = document.getElementById("graph-search-btn");
      if (btn) btn.click();
    }, 300);
  }

  /* ================================================================
     Theme
     ================================================================ */
  function readTheme() {
    try {
      var stored = localStorage.getItem("memori_theme");
      if (stored) return stored;
    } catch (_) {}
    try {
      var stored = localStorage.getItem("lmem_theme");
      if (stored) return stored;
    } catch (_) {}
    var html = document.documentElement.getAttribute("data-theme");
    if (html) return html;
    return "light";
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    var darkIcon = document.getElementById("theme-icon-dark");
    var lightIcon = document.getElementById("theme-icon-light");
    if (darkIcon && lightIcon) {
      darkIcon.classList.toggle("hidden", theme === "light");
      lightIcon.classList.toggle("hidden", theme === "dark");
    }
  }

  function toggleTheme() {
    var current = document.documentElement.getAttribute("data-theme") || "light";
    var next = current === "light" ? "dark" : "light";
    applyTheme(next);
    try { localStorage.setItem("memori_theme", next); } catch (_) {}
    showToast(window.t(next === "dark" ? "theme.darkToast" : "theme.lightToast"));
  }

  function listenBridgeTheme() {
    // 独立模式下通过 localStorage 同步主题
    try {
      window.addEventListener("storage", function(e) {
        if (e.key === "memori_theme") {
          applyTheme(e.newValue || "light");
        }
      });
    } catch (_) {}
  }

  /* ================================================================
     Toast
     ================================================================ */
  var toastTimer;
  function showToast(msg, isError) {
    var el = document.getElementById("toast");
    el.textContent = msg;
    el.classList.remove("visible", "error");
    if (isError) el.classList.add("error");
    void el.offsetWidth;
    el.classList.add("visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function() { el.classList.remove("visible"); }, 2500);
  }

  /* ── Custom Confirm Dialog (renders inside peek panel, avoids sandbox + z-index issues) ── */
  var _confirmResolve = null;
  var _prevPeekContent = null;

  function showConfirmDialog(title, message) {
    return new Promise(function(resolve) {
      if (_confirmResolve) {
        _confirmResolve(false);
        _confirmResolve = null;
      }
      _confirmResolve = resolve;

      /* Save current peek content so we can restore on cancel */
      var peekBody = document.getElementById("peek-body");
      if (peekBody && !_prevPeekContent) {
        _prevPeekContent = peekBody.innerHTML;
      }

      /* Make sure peek panel is open and wide */
      var panel = document.getElementById("peek-panel");
      if (panel) {
        panel.classList.add("visible", "wide");
      }
      var peekOverlay = document.getElementById("peek-overlay");
      if (peekOverlay) {
        peekOverlay.classList.add("visible");
      }

      /* Render confirm view inside peek */
      document.getElementById("peek-badge").innerHTML = "";
      document.getElementById("peek-title").textContent = title || window.t("delete.confirmTitle");

      var html = "";
      html += '<div class="peek-section" style="text-align:center;padding:var(--space-8) var(--space-6)">';
      html += '<p style="white-space:pre-line;font-size:14px;color:var(--text-secondary);margin-bottom:var(--space-6)">' + esc(message || "") + '</p>';
      html += '<div style="display:flex;gap:var(--space-4);justify-content:center">';
      html += '<button class="btn btn-secondary" id="confirm-cancel-btn">' + window.t("common.cancel") + '</button>';
      html += '<button class="btn btn-danger" id="confirm-ok-btn">' + window.t("common.confirm") + '</button>';
      html += '</div></div>';

      if (peekBody) peekBody.innerHTML = html;

      /* Bind buttons */
      var okBtn = document.getElementById("confirm-ok-btn");
      var cancelBtn = document.getElementById("confirm-cancel-btn");
      if (okBtn) okBtn.addEventListener("click", function() { _closeConfirmDialog(true); });
      if (cancelBtn) cancelBtn.addEventListener("click", function() { _closeConfirmDialog(false); });
    });
  }

  function _closeConfirmDialog(result) {
    var peekBody = document.getElementById("peek-body");
    /* Restore previous content if cancelled */
    if (!result && _prevPeekContent && peekBody) {
      peekBody.innerHTML = _prevPeekContent;
      /* Re-bind detail view buttons */
      if (state._detailCache && !state.isEditing) {
        renderMemoryDetailView(state._detailCache);
      }
    }
    _prevPeekContent = null;

    if (_confirmResolve) {
      _confirmResolve(!!result);
      _confirmResolve = null;
    }
  }

  /* No init needed — buttons are bound fresh each time in showConfirmDialog */

  /* ================================================================
     Sidebar / Routing
     ================================================================ */
  function switchPage(name) {
    state.page = name;
    document.querySelectorAll(".nav-item[data-page]").forEach(function(item) {
      item.classList.toggle("active", item.dataset.page === name);
    });
    document.querySelectorAll(".page").forEach(function(p) {
      p.classList.toggle("active", p.id === "page-" + name);
    });
    if (name === "graph") { fetchGraphStats(); if (window.ensureGraphScene) window.ensureGraphScene(); }
    if (name === "memory" || name === "memories") { fetchMemories(); }
    if (name === "persons") { fetchPersonas(); }
    if (name === "system") { fetchSystemOverview(); }
    if (name === "settings") { renderSettingsPage.render(); }
  }

  function initSidebar() {
    document.querySelectorAll(".nav-item[data-page]").forEach(function(item) {
      item.addEventListener("click", function() { switchPage(item.dataset.page); });
    });
    document.getElementById("theme-toggle").addEventListener("click", toggleTheme);
    var langMenu = document.getElementById("lang-menu");
    document.querySelectorAll(".lang-option[data-lang]").forEach(function(option) {
      option.addEventListener("click", function() {
        var next = option.dataset.lang;
        window.setLanguage(next);
        updateLanguageMenu();
        if (langMenu) langMenu.open = false;
        showToast(window.t("language.toast", next.toUpperCase()));
      });
    });
    document.addEventListener("click", function(e) {
      if (langMenu && langMenu.open && !langMenu.contains(e.target)) langMenu.open = false;
    });
    window.addEventListener("languagechange", function() {
      updateLanguageMenu();
      refreshDynamicI18n();
    });
    updateLanguageMenu();
  }

  function updateLanguageMenu() {
    var current = window.getLanguage ? window.getLanguage() : "zh";
    var label = document.getElementById("lang-label");
    var currentLabelKeys = {
      zh: "language.current.zh",
      en: "language.current.en",
      ru: "language.current.ru",
    };
    if (label) {
      label.textContent = window.t("header.lang") + " · " + window.t(currentLabelKeys[current] || currentLabelKeys.zh);
    }
    document.querySelectorAll(".lang-option[data-lang]").forEach(function(option) {
      option.classList.toggle("active", option.dataset.lang === current);
    });
  }

  function refreshDynamicI18n() {
    if (state.page === "memory") {
      if (state.memory.items.length) renderMemoriesVirtual();
      else renderMemoriesVirtual();
      updateMemoryPagination();
      updateBatchBar();
    }
    if (state.page === "persons" && state.personas.users.length) {
      renderPersonaGrid();
    }
    if (state._detailCache) {
      if (state.isEditing) renderMemoryEditView(state._detailCache);
      else renderMemoryDetailView(state._detailCache);
    }
    if (state._nodeDetailCache) renderPeekNode(state._nodeDetailCache);
    if (state._recallCache) renderRecallResults(state._recallCache);
    if (state._systemCache) renderSystemOverview(state._systemCache);
    if (state._personaDetailUid && document.querySelector(".persona-detail-summary")) {
      // If persona detail is open, keep it as is (i18n already applied)
    }
  }

  /* ================================================================
     Peek Panel — Memory Detail & Edit View
     ================================================================ */
  function openPeek(isWide) {
    var panel = document.getElementById("peek-panel");
    panel.classList.add("visible");
    if (isWide) panel.classList.add("wide");
    else panel.classList.remove("wide");
    document.getElementById("peek-overlay").classList.add("visible");
  }

  function closePeek() {
    /* If a confirm dialog is pending, cancel it first */
    if (_confirmResolve) {
      _closeConfirmDialog(false);
    }
    var panel = document.getElementById("peek-panel");
    panel.classList.remove("visible", "wide");
    document.getElementById("peek-overlay").classList.remove("visible");
    state.selectedMemory = null;
    state.isEditing = false;
    state._detailCache = null;
    state._nodeDetailCache = null;
    state._personaDetailUid = null;
  }

  async function renderPeekMemory(memory) {
    state.selectedMemory = memory;
    state.isEditing = false;
    state._nodeDetailCache = null;
    var memoryId = memory.memory_id || memory.id;
    state._detailCache = null;

    var detail = null;
    try {
      detail = unwrapApiData(await apiRequest("memories/day?did=" + memoryId));
      if (detail) {
        detail.memory_id = memoryId;
        var rawText = (detail.diary && detail.diary.content) || memory.content || "";
        // Parse frontmatter
        var detailMood = "", detailTopics = [];
        if (rawText.startsWith("---")) {
          var fmEnd = rawText.indexOf("\n---", 3);
          if (fmEnd !== -1) {
            rawText.substring(3, fmEnd).trim().split("\n").forEach(function(line) {
              var m = line.match(/^([a-zA-Z_]+)\s*:\s*(.+)$/);
              if (!m) return;
              if (m[1] === "mood") detailMood = m[2].replace(/^["']|["']$/g, "");
              if (m[1] === "topics") { try { detailTopics = JSON.parse(m[2].replace(/'/g,'"')); } catch(e) {} }
            });
          }
        }
        detail.mood = detailMood;
        detail.topics = Array.isArray(detailTopics) ? detailTopics : [];
        detail.text = stripFrontmatter(rawText);
        detail.summary = stripFrontmatter(rawText);
        detail.memory_type = "DIARY";
        detail.status = detail.status || "active";
        detail.created_at = memory.created_at || "--";
        detail.updated_at = memory.updated_at || "--";
        detail.key_facts = Array.isArray(detail.atoms) ? detail.atoms : [];
        detail.session_id = "--";
        detail.persona_id = "--";
        detail.update_history = [];
        detail.graph_context = null;
        detail._diary = detail.diary;
        state._detailCache = detail;
      }
    } catch (_) {
      detail = null;
    }

    if (!detail) {
      detail = {
        memory_id: parseInt(memoryId),
        text: memory.content || "",
        summary: memory.summary || "",
        memory_type: "DIARY",
        importance: memory.importance != null ? Number(memory.importance) : 5,
        status: "active",
        created_at: memory.created_at || "--",
        updated_at: memory.updated_at || "--",
        key_facts: [],
        topics: [],
        session_id: "--",
        persona_id: "--",
        update_history: [],
        graph_context: null,
        _diary: null,
      };
    }

    detail.importance = normalizeImportance(detail.importance);

    renderMemoryDetailView(detail);
    openPeek(true);
  }

  function renderMemoryDetailView(detail) {
    state._detailCache = detail;
    state._nodeDetailCache = null;
    state.isEditing = false;
    var id = detail.memory_id;
    var type = detail.memory_type || "GENERAL";
    var status = detail.status || "active";
    var importance = normalizeImportance(detail.importance).toFixed(1);
    var content = getDetailText(detail);
    var created = detail.created_at || "--";
    var updated = detail.updated_at || "--";
    var sessionId = detail.session_id || "--";
    var personaId = detail.persona_id || "--";
    var keyFacts = detail.key_facts || [];
    var topics = detail.topics || [];
    var editHistory = detail.update_history || [];
    var graphCtx = detail.graph_context;

    document.getElementById("peek-badge").innerHTML = "";
    document.getElementById("peek-title").textContent = window.t("detail.memoryTitle", id);

    var html = "";

    /* Status + Type pill row */
    html += '<div class="memory-detail-header">';
    html += statusPill(status);
    if (detail.mood) html += '<span class="type-tag">' + esc(detail.mood) + '</span>';
    html += '<span class="type-tag">' + esc(type) + '</span>';
    html += '<span class="memory-detail-importance">' + window.t("detail.importance") + ': ' + importance + '/10</span>';
    html += '</div>';

    /* Actions bar */
    html += '<div class="memory-detail-actions">';
    html += '<button class="btn btn-sm btn-secondary" id="peek-edit-btn">' + window.t("detail.editBtn") + '</button>';
    html += '<button class="btn btn-sm btn-danger" id="peek-delete-btn">' + window.t("detail.deleteBtn") + '</button>';
    html += '</div>';

    /* Content section — clean markdown/yaml, keep wikilinks clickable */
    var displayContent = cleanDisplayText(content);
    html += '<div class="peek-section"><div class="peek-section-title">' + window.t("detail.content") + '</div>';
    html += '<div class="memory-detail-content" id="detail-content-display" style="white-space:pre-wrap;line-height:1.6">' + renderWikilinks(displayContent) + '</div></div>';

    /* Graph Context mini view */
    if (graphCtx && graphCtx.nodes && graphCtx.nodes.length) {
      html += '<div class="peek-section"><div class="peek-section-title">' + window.t("detail.graphContext") + '</div>';
      html += '<canvas id="peek-mini-graph" class="memory-detail-mini-graph" width="440" height="160" data-memory-id="' + id + '"></canvas></div>';
    }

    /* Metadata grid */
    html += '<div class="peek-section"><div class="peek-section-title">' + window.t("detail.metadata") + '</div>';
    html += '<div class="memory-detail-meta-grid">';
    html += metaItem(window.t("detail.status"), statusPill(status));
    html += metaItem(window.t("detail.type"), '<span class="type-tag">' + esc(type) + '</span>');
    html += metaItem(window.t("detail.importance"), importance + ' / 10');
    html += metaItem(window.t("detail.sessionId"), '<span style="font-size:11px;font-family:monospace">' + esc(String(sessionId)) + '</span>');
    html += metaItem(window.t("detail.personaId"), '<span style="font-size:11px;font-family:monospace">' + esc(String(personaId)) + '</span>');
    html += metaItem(window.t("detail.created"), esc(created));
    html += metaItem(window.t("detail.updated"), esc(updated));
    html += '</div></div>';

    /* Key Facts */
    if (keyFacts.length) {
      html += '<div class="peek-section"><div class="peek-section-title">' + window.t("detail.keyFacts") + '</div><div class="peek-fact-list">';
      keyFacts.forEach(function(f) { html += '<div class="peek-fact-item">' + esc(f.content || f.text || String(f)) + ' <span style="font-size:0.75em;color:var(--text-secondary)">(' + (f.type || "") + ' · ' + (f.importance || "") + ')</span></div>'; });
      html += '</div></div>';
    }

    /* Topics */
    if (topics.length) {
      html += '<div class="peek-section"><div class="peek-section-title">' + window.t("detail.topics") + '</div>';
      html += topics.map(function(t) { return '<span class="type-tag" style="margin-right:4px">' + esc(String(t)) + '</span>'; }).join("");
      html += '</div>';
    }

    /* Edit History */
    if (editHistory.length) {
      html += '<div class="peek-section"><div class="peek-section-title">' + window.t("detail.editHistory") + '</div><div class="edit-history-list">';
      editHistory.forEach(function(h) {
        var time = h.timestamp ? new Date(h.timestamp * 1000).toLocaleString() : (h.time || "--");
        html += '<div class="edit-history-item"><span class="edit-history-time">' + esc(time) + '</span>';
        html += '<span class="edit-history-desc">' + esc(h.description || h.field + ": " + h.old_value + " → " + h.new_value) + '</span></div>';
      });
      html += '</div></div>';
    }

    document.getElementById("peek-body").innerHTML = html;

    /* Bind buttons */
    var editBtn = document.getElementById("peek-edit-btn");
    var delBtn = document.getElementById("peek-delete-btn");
    if (editBtn) editBtn.addEventListener("click", function() { renderMemoryEditView(detail); });
    if (delBtn) delBtn.addEventListener("click", function() { deleteSingleMemory(parseInt(id)); });

    /* Load mini-graph if canvas exists */
    var miniCanvas = document.getElementById("peek-mini-graph");
    if (miniCanvas && graphCtx && graphCtx.nodes && graphCtx.nodes.length) {
      loadPeekMiniGraphFromData(miniCanvas, graphCtx.nodes, graphCtx.edges);
    }
  }

  function renderMemoryEditView(detail) {
    state.isEditing = true;
    state._detailCache = detail;
    state._nodeDetailCache = null;
    var id = detail.memory_id;
    var content = getDetailText(detail);
    var importance = normalizeImportance(detail.importance).toFixed(1);
    var type = detail.memory_type || "GENERAL";
    var status = detail.status || "active";

    var html = "";

    html += '<div class="memory-detail-header">';
    html += '<span style="font-size:12px;color:var(--text-secondary)">' + window.t("detail.editingTitle", id) + '</span>';
    html += '</div>';

    html += '<div class="memory-detail-actions">';
    html += '<button class="btn btn-sm btn-primary" id="peek-save-btn">' + window.t("detail.saveBtn") + '</button>';
    html += '<button class="btn btn-sm btn-ghost" id="peek-cancel-btn">' + window.t("detail.cancelBtn") + '</button>';
    html += '</div>';

    /* Editable Content */
    html += '<div class="peek-section"><div class="peek-section-title">' + window.t("detail.content") + '</div>';
    html += '<textarea id="edit-content-area" class="memory-detail-edit-area" rows="6">' + esc(content) + '</textarea>';
    html += '<p class="form-hint" style="margin-top:4px">' + window.t("detail.contentHint") + '</p>';
    html += '</div>';

    /* Editable Metadata */
    html += '<div class="peek-section"><div class="peek-section-title">' + window.t("detail.metadata") + '</div>';
    html += '<div class="memory-detail-meta-grid">';

    html += '<div class="memory-detail-meta-item">';
    html += '<span class="memory-detail-meta-label">' + window.t("detail.status") + '</span>';
    html += '<select id="edit-status" class="memory-detail-select">';
    html += '<option value="active"' + (status === "active" ? " selected" : "") + '>' + statusLabel("active") + '</option>';
    html += '<option value="archived"' + (status === "archived" ? " selected" : "") + '>' + statusLabel("archived") + '</option>';
    html += '<option value="deleted"' + (status === "deleted" ? " selected" : "") + '>' + statusLabel("deleted") + '</option>';
    html += '</select></div>';

    html += '<div class="memory-detail-meta-item">';
    html += '<span class="memory-detail-meta-label">' + window.t("detail.type") + '</span>';
    html += '<input type="text" id="edit-type" class="memory-detail-select" value="' + esc(type) + '" />';
    html += '</div>';

    html += '<div class="memory-detail-meta-item" style="grid-column:1/-1">';
    html += '<span class="memory-detail-meta-label">' + window.t("detail.importance") + '</span>';
    html += '<div class="memory-detail-slider">';
    html += '<input type="range" id="edit-importance" min="0" max="10" step="0.1" value="' + importance + '" />';
    html += '<span class="memory-detail-slider-value" id="importance-value">' + importance + '</span>';
    html += '</div></div>';

    html += '<div class="memory-detail-meta-item" style="grid-column:1/-1">';
    html += '<span class="memory-detail-meta-label">' + window.t("detail.updateReason") + '</span>';
    html += '<input type="text" id="peek-edit-reason" class="memory-detail-reason" placeholder="' + esc(window.t("detail.reasonPh")) + '" />';
    html += '</div>';

    html += '</div></div>';

    document.getElementById("peek-body").innerHTML = html;

    /* Bind slider */
    document.getElementById("edit-importance").addEventListener("input", function() {
      document.getElementById("importance-value").textContent = parseFloat(this.value).toFixed(1);
    });

    var saveBtn = document.getElementById("peek-save-btn");
    var cancelBtn = document.getElementById("peek-cancel-btn");
    if (saveBtn) saveBtn.addEventListener("click", function() { saveMemoryEdit(detail); });
    if (cancelBtn) cancelBtn.addEventListener("click", function() { renderMemoryDetailView(detail); });
  }

  async function saveMemoryEdit(detail) {
    var id = detail.memory_id;
    var newContent = document.getElementById("edit-content-area").value.trim();
    var newStatus = document.getElementById("edit-status").value;
    var newType = document.getElementById("edit-type").value.trim();
    var newImportance = parseFloat(document.getElementById("edit-importance").value);
    var reason = document.getElementById("peek-edit-reason").value.trim();

    var saveBtn = document.getElementById("peek-save-btn");
    if (saveBtn) saveBtn.disabled = true;
    var messages = [];

    try {
      if (!newContent) {
        showToast(window.t("detail.contentRequired"), true);
        return;
      }

      if (newContent !== getDetailText(detail)) {
        var result = unwrapApiData(await apiRequest("memories/update", {
          method: "POST",
          body: { memory_id: id, field: "content", value: newContent, reason: reason },
        }));
        if (result && result.new_memory_id) {
          messages.push(window.t("detail.contentUpdated", result.new_memory_id));
          id = parseInt(result.new_memory_id);
        }
      }

      if (newStatus !== detail.status) {
        unwrapApiData(await apiRequest("memories/update", {
          method: "POST", body: { memory_id: id, field: "status", value: newStatus, reason: reason },
        }));
        messages.push(window.t("detail.statusUpdated", statusLabel(newStatus)));
      }

      if (newType !== detail.memory_type) {
        unwrapApiData(await apiRequest("memories/update", {
          method: "POST", body: { memory_id: id, field: "type", value: newType, reason: reason },
        }));
        messages.push(window.t("detail.typeUpdated", newType));
      }

      if (Math.abs(newImportance - normalizeImportance(detail.importance)) > 0.01) {
        unwrapApiData(await apiRequest("memories/update", {
          method: "POST", body: { memory_id: id, field: "importance", value: newImportance, reason: reason },
        }));
        messages.push(window.t("detail.importanceUpdated", newImportance.toFixed(1)));
      }

      showToast(messages.length ? messages.join("; ") : window.t("detail.noChanges"));
      closePeek();
      await fetchMemories();
    } catch (e) {
      showToast(e.message || window.t("edit.updateFailed"), true);
    } finally {
      if (saveBtn) saveBtn.disabled = false;
    }
  }

  function renderPeekNode(nodeData) {
    state._nodeDetailCache = nodeData;
    state._detailCache = null;
    state.isEditing = false;
    var panel = document.getElementById("peek-panel");
    panel.classList.remove("wide");
    document.getElementById("peek-badge").innerHTML = nodeBadge(nodeData.type);
    document.getElementById("peek-title").textContent = nodeData.label || window.t("graph.unnamedNode");

    var html = '<div class="peek-section">';
    html += '<div class="peek-meta-grid">';
    html += '<div class="peek-meta-item"><span class="peek-meta-label">' + window.t("detail.nodeMemories") + '</span><span class="peek-meta-value">' + (nodeData.memory_count || 0) + '</span></div>';
    html += '<div class="peek-meta-item"><span class="peek-meta-label">' + window.t("detail.nodeDegree") + '</span><span class="peek-meta-value">' + (nodeData.degree || 0) + '</span></div>';
    html += '<div class="peek-meta-item"><span class="peek-meta-label">' + window.t("detail.nodeEntries") + '</span><span class="peek-meta-value">' + (nodeData.entry_count || 0) + '</span></div>';
    html += '<div class="peek-meta-item"><span class="peek-meta-label">' + window.t("detail.nodeWeight") + '</span><span class="peek-meta-value">' + Number(nodeData.weight || 0).toFixed(2) + '</span></div>';
    html += '</div></div>';

    document.getElementById("peek-body").innerHTML = html;
    openPeek(false);
  }

  function nodeBadge(type) {
    var t = String(type || "other").toLowerCase();
    return '<div class="peek-node-badge ' + t + '">' + typeLabel(t) + '</div>';
  }

  function statusPill(status) {
    var s = String(status || "active").toLowerCase();
    return '<span class="status-pill ' + s + '">' + statusLabel(s) + '</span>';
  }

  function statusLabel(status) {
    var s = String(status || "active").toLowerCase();
    var labels = { active: "status.active", archived: "status.archived", deleted: "status.deleted" };
    return labels[s] ? window.t(labels[s]) : s;
  }

  function typeLabel(type) {
    var t = String(type || "other").toLowerCase();
    var keys = { topic: "graph.nodeTopic", person: "graph.nodePerson", fact: "graph.nodeFact", summary: "graph.nodeSummary" };
    return window.t(keys[t] || "graph.nodeUnknown");
  }

  function metaItem(label, value) {
    return '<div class="memory-detail-meta-item"><span class="memory-detail-meta-label">' + esc(label) + '</span><span class="memory-detail-meta-value">' + value + '</span></div>';
  }

  function esc(text) {
    return String(text || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /* Mini Graph Canvas */
  var MINI_NODE_COLORS = {
    topic: "#7950f2", person: "#20c997", fact: "#fcc419", summary: "#f06595", other: "#909296",
  };

  function loadPeekMiniGraphFromData(canvas, nodes, edges) {
    var ctx = canvas.getContext("2d");
    var rect = canvas.getBoundingClientRect();
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var W = Math.max(220, Math.floor(rect.width || canvas.width || 440));
    var H = Math.max(140, Math.floor(rect.height || canvas.height || 160));
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (!nodes.length) {
      ctx.clearRect(0, 0, W, H);
      ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--text-tertiary").trim() || "#8a8f98";
      ctx.font = "11px -apple-system, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(window.t("detail.noGraphData"), W / 2, H / 2);
      return;
    }
    drawMiniGraph(ctx, W, H, nodes, edges);

    canvas.style.cursor = "pointer";
    canvas.onclick = function() {
      document.querySelector('.nav-item[data-page="graph"]').click();
      var mid = canvas.dataset.memoryId;
      if (mid) {
        setTimeout(function() {
          var mi = document.getElementById("graph-memory-id");
          if (mi) { mi.value = mid; mi.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" })); }
        }, 300);
      }
    };
  }

  function drawMiniGraph(ctx, W, H, nodes, edges) {
    ctx.clearRect(0, 0, W, H);
    var pad = 20;
    var cx = W / 2, cy = H / 2;

    var n = nodes.length;
    if (n === 0) return;

    /* ── Mini force-directed layout ── */
    var simNodes = nodes.map(function(node, i) {
      return {
        id: node.id,
        type: node.type || "other",
        x: cx + (Math.random() - 0.5) * W * 0.6,
        y: cy + (Math.random() - 0.5) * H * 0.6,
        vx: 0,
        vy: 0,
      };
    });

    var indexMap = {};
    simNodes.forEach(function(sn, i) { indexMap[sn.id] = i; });

    var simEdges = [];
    edges.forEach(function(edge) {
      var si = indexMap[edge.source];
      var ti = indexMap[edge.target];
      if (si != null && ti != null) simEdges.push({ source: si, target: ti });
    });

    /* Quick force simulation */
    var iterations = Math.min(100, 30 + n * 8);
    for (var step = 0; step < iterations; step++) {
      var alpha = 1 - step / iterations;

      /* Repulsion */
      for (var i = 0; i < simNodes.length; i++) {
        var a = simNodes[i];
        for (var j = i + 1; j < simNodes.length; j++) {
          var b = simNodes[j];
          var dx = a.x - b.x;
          var dy = a.y - b.y;
          var distSq = dx * dx + dy * dy;
          if (distSq < 1) distSq = 1;
          var dist = Math.sqrt(distSq);
          var repulse = 800 * alpha / distSq;
          var fx = dx / dist * repulse;
          var fy = dy / dist * repulse;
          a.vx += fx; a.vy += fy;
          b.vx -= fx; b.vy -= fy;
        }
      }

      /* Spring attraction */
      simEdges.forEach(function(e) {
        var s = simNodes[e.source];
        var t = simNodes[e.target];
        var dx = t.x - s.x;
        var dy = t.y - s.y;
        var dist = Math.sqrt(dx * dx + dy * dy) || 1;
        var desired = 55;
        var force = (dist - desired) * 0.004 * alpha;
        var fx = dx / dist * force;
        var fy = dy / dist * force;
        s.vx += fx; s.vy += fy;
        t.vx -= fx; t.vy -= fy;
      });

      /* Gentle centering */
      simNodes.forEach(function(sn) {
        sn.vx += (cx - sn.x) * 0.004 * alpha;
        sn.vy += (cy - sn.y) * 0.004 * alpha;
        sn.vx *= 0.72;
        sn.vy *= 0.72;
        sn.x += sn.vx;
        sn.y += sn.vy;
      });
    }

    /* Clamp to canvas */
    simNodes.forEach(function(sn) {
      sn.x = Math.min(W - pad, Math.max(pad, sn.x));
      sn.y = Math.min(H - pad, Math.max(pad, sn.y));
    });

    var nodeLookup = {};
    simNodes.forEach(function(sn) { nodeLookup[sn.id] = sn; });

    /* Draw edges */
    ctx.strokeStyle = "rgba(148,163,184,0.3)";
    ctx.lineWidth = 0.8;
    edges.forEach(function(edge) {
      var src = nodeLookup[edge.source];
      var tgt = nodeLookup[edge.target];
      if (!src || !tgt) return;
      ctx.beginPath();
      ctx.moveTo(src.x, src.y);
      ctx.lineTo(tgt.x, tgt.y);
      ctx.stroke();
    });

    /* Draw nodes */
    simNodes.forEach(function(sn) {
      var color = MINI_NODE_COLORS[sn.type] || MINI_NODE_COLORS.other;
      ctx.beginPath();
      ctx.arc(sn.x, sn.y, 4, 0, 2 * Math.PI);
      ctx.fillStyle = color;
      ctx.fill();
      ctx.strokeStyle = "rgba(255,255,255,0.5)";
      ctx.lineWidth = 1;
      ctx.stroke();
    });
  }

  /* ================================================================
     Memory Page — Card Waterfall
     ================================================================ */
  async function fetchMemories() {
    var params = new URLSearchParams();
    params.set("page", String(state.memory.page));
    params.set("size", String(state.memory.pageSize));
    if (state.memory.keyword) params.set("q", state.memory.keyword);

    try {
      var data = unwrapApiData(await apiRequest("memories?" + params.toString())) || {};
      state.memory.total = data.total || 0;
      state.memory.hasMore = data.items && data.items.length >= state.memory.pageSize;
      state.memory.selected.clear();

      state.memory.items = (Array.isArray(data.items) ? data.items : []).map(function(item) {
        var createdRaw = item.created_at;
        var updatedRaw = item.updated_at || item.created_at;
        var updatedStr = updatedRaw ? new Date(updatedRaw * 1000).toLocaleString() : "--";
        var createdStr = createdRaw ? new Date(createdRaw * 1000).toLocaleString() : "--";
        var typeStr = "";
        if (item.types && item.types.length) {
          typeStr = item.types.map(function(t) { return '<span class="type-dot">' + t.type + '(' + t.count + ')</span>'; }).join(" ");
        }
        return {
          id: item.id,
          memory_id: item.id,
          date: item.date || "",
          summary: cleanDisplayText(item.content || ""),
          content: item.content || "",
          memory_type: typeStr,
          importance: item.avg_importance != null ? Math.round(item.avg_importance * 10 * 10) / 10 : 5,
          status: item.status || "active",
          created_at: createdStr,
          updated_at: updatedStr,
          atom_count: item.atom_count || 0,
        };
      });


      renderMemoriesVirtual({ resetScroll: true });
      updateMemoryPagination();
      updateBatchBar();
      updateTimeline();  // 更新今日星轨
    } catch (e) {
      showToast(e.message || "加载失败", true);
      var list = document.getElementById("memories-list");
      if (list) list.innerHTML = '<div class="mem-card-empty">' + window.t("table.noData") + '</div>';
    }
  }

  /* ================================================================
     Star Trail Timeline — 今日星轨（5条记忆满）
     ================================================================ */
  async function updateTimeline() {
    var star = document.getElementById("timeline-star");
    var fill = document.getElementById("timeline-bar-fill");
    var countEl = document.getElementById("timeline-count");
    if (!star || !fill) return;
    try {
      var data = unwrapApiData(await apiRequest("memories/today-stats")) || {};
      var total = data.total || 0;

      // 5条记忆 = 100%
      var pct = Math.min(100, (total / 5) * 100);
      var fullNodes = Math.min(5, total);

      // 更新计数文字
      if (countEl) {
        if (total === 0) {
          countEl.textContent = "今天还没留下回忆呢…";
        } else if (total < 5) {
          countEl.innerHTML = "今日活跃度 <strong>" + total + "/5</strong> ✨";
        } else {
          countEl.innerHTML = "今日活跃度 <strong>5/5</strong> 圆满达成！🌟";
        }
      }

      // 填充进度条
      fill.style.width = pct + "%";
      fill.classList.toggle("full", total >= 5);

      // 移动星星
      star.style.left = pct + "%";

      // 点亮节点
      var nodes = document.querySelectorAll(".timeline-node");
      nodes.forEach(function(n, i) {
        n.classList.toggle("lit", i < fullNodes);
      });

      // 更新标签
      var labels = document.querySelectorAll(".timeline-labels span");
      labels.forEach(function(l, i) {
        l.classList.toggle("active", i < fullNodes);
      });

      // 满星闪烁
      star.classList.remove("sparkle");
      if (total >= 5) {
        void star.offsetWidth;
        star.classList.add("sparkle");
        star.style.filter = "";
      } else if (total >= 3) {
        star.style.filter = "drop-shadow(0 0 10px rgba(255, 215, 0, 0.7))";
      } else {
        star.style.filter = "";
      }
    } catch (_) {
      // 静默失败
    }
  }

  function renderMemoriesVirtual(options) {
    options = options || {};
    var list = document.getElementById("memories-list");
    var empty = document.getElementById("memories-empty");
    if (!list) return;
    if (!state.memory.items.length) {
      list.innerHTML = '<div class="mem-card-empty">' + window.t("table.noData") + '</div>';
      return;
    }

    var html = "";
    for (var i = 0; i < state.memory.items.length; i++) {
      var item = state.memory.items[i];
      var key = "m:" + item.memory_id;
      var sel = state.memory.selected.has(key);
      var imp = item.importance != null ? Number(item.importance).toFixed(1) : "5.0";
      var impNum = Math.min(10, Math.max(0, parseFloat(imp) || 0));
      var impCls = impNum >= 7 ? "high" : impNum >= 4 ? "medium" : "low";
      var dateStr = item.updated_at;
      // 提取类型标签
      var typeTags = "";
      if (item.types && item.types.length) {
        typeTags = item.types.map(function(t) {
          var tc = (t.type || "").toLowerCase();
          return '<span class="mem-card-tag type-' + tc + '">' + esc(t.type) + '</span>';
        }).join(" ");
      }
      html += '<div class="mem-card" data-key="' + key + '">' +
        '<div class="mem-card-importance ' + impCls + '"></div>' +
        '<div class="mem-card-body">' +
        '<div class="mem-card-top">' +
        '<div class="mem-card-summary">' + esc(item.summary || item.content || "") + '</div>' +
        '</div>' +
        '<div class="mem-card-bottom">' +
        '<span class="mem-card-date">' + esc(dateStr) + '</span>' +
        '<div class="mem-card-tags">' + typeTags + '</div>' +
        '</div>' +
        '</div>' +
        '<div class="mem-card-created">' +
        '<span class="mem-card-created-label">创建时间</span>' +
        '<span class="mem-card-created-value">' + esc(item.created_at) + '</span>' +
        '</div>' +
        '</div>';
    }
    list.innerHTML = html;

    // 点击卡片 → 显示详情
    list.querySelectorAll(".mem-card").forEach(function(card) {
      card.addEventListener("click", function() {
        var item = getMemoryItemByKey(this.dataset.key);
        if (item) renderPeekMemory(item);
      });
    });
  }

  function getMemoryItemByKey(key) {
    return state.memory.items.find(function(i) { return ("m:" + i.memory_id) === key; });
  }

  function updateBatchBar() {
    var bar = document.getElementById("batch-bar");
    var count = state.memory.selected.size;
    document.getElementById("batch-count").textContent = window.t("filter.selectedCount", count);
    bar.classList.toggle("visible", count > 0);
  }

  function updateMemoryPagination() {
    var p = state.memory.page;
    var ps = state.memory.pageSize;
    var t = state.memory.total;
    var tp = Math.max(1, Math.ceil(t / ps));
    var info = document.getElementById("mem-pagination-info");
    if (info) {
      info.textContent = p + " / " + tp + " 页 · 共 " + t + " 条";
      info.dataset.page = p;
      info.dataset.totalPages = tp;
    }
    document.getElementById("mem-prev").disabled = p <= 1;
    document.getElementById("mem-next").disabled = !state.memory.hasMore;
  }

  function initPageJump() {
    var info = document.getElementById("mem-pagination-info");
    if (!info) return;
    info.addEventListener("click", function() {
      if (info.querySelector("input")) return;
      var cur = parseInt(info.dataset.page) || 1;
      var total = parseInt(info.dataset.totalPages) || 1;
      var input = document.createElement("input");
      input.type = "number";
      input.className = "pagination-info-input";
      input.min = 1;
      input.max = total;
      input.value = cur;
      info.textContent = "";
      info.appendChild(input);
      input.focus();
      input.select();

      function commit() {
        var val = parseInt(input.value) || cur;
        val = Math.max(1, Math.min(val, total));
        if (val !== cur) {
          state.memory.page = val;
          fetchMemories();
        } else {
          updateMemoryPagination();
        }
      }

      input.addEventListener("blur", commit);
      input.addEventListener("keydown", function(e) {
        if (e.key === "Enter") { input.blur(); }
        if (e.key === "Escape") { updateMemoryPagination(); }
      });
    });
  }

  async function deleteSingleMemory(id) {
    if (!id) return;
    var confirmed = await showConfirmDialog(
      window.t("delete.confirmTitle"),
      window.t("delete.confirmMsg", 1)
    );
    if (!confirmed) {
      showToast(window.t("delete.cancelled"));
      return;
    }
    try {
      await apiRequest("memories/delete", { method: "POST", body: { id: id } });
      showToast(window.t("delete.successOne", id));
      closePeek();
      await fetchMemories();
    } catch (e) {
      showToast(e.message || window.t("delete.error"), true);
    }
  }

  async function batchDelete() {
    if (!state.memory.selected.size) return;
    var ids = [];
    state.memory.selected.forEach(function(k) {
      var id = parseInt(k.replace("m:", ""));
      if (!isNaN(id)) ids.push(id);
    });
    if (!ids.length) return;
    var confirmed = await showConfirmDialog(
      window.t("delete.confirmTitle"),
      window.t("delete.confirmMsg", ids.length)
    );
    if (!confirmed) {
      showToast(window.t("delete.cancelled"));
      return;
    }
    try {
      await apiRequest("memories/batch-delete", { method: "POST", body: { ids: ids } });
      showToast(window.t("delete.success", ids.length));
      state.memory.selected.clear();
      await fetchMemories();
    } catch (e) {
      showToast(e.message || window.t("delete.error"), true);
    }
  }

  async function batchArchive() {
    if (!state.memory.selected.size) return;
    var ids = [];
    state.memory.selected.forEach(function(k) {
      var id = parseInt(k.replace("m:", ""));
      if (!isNaN(id)) ids.push(id);
    });
    if (!ids.length) return;
    try {
      var result = unwrapApiData(await apiRequest("memories/batch-update", {
        method: "POST",
        body: { memory_ids: ids, field: "status", value: "archived" },
      }));
      var updated = (result && result.updated_count) || ids.length;
      showToast(window.t("archive.success", updated));
      state.memory.selected.clear();
      await fetchMemories();
    } catch (e) {
      showToast(e.message || window.t("archive.fail"), true);
    }
  }

  function initMemoryPage() {
    var list = document.getElementById("memories-list");
    if (list) {
      list.addEventListener("click", function(e) {
        var card = e.target.closest(".mem-card");
        if (!card || !card.dataset.key) return;
        var item = getMemoryItemByKey(card.dataset.key);
        if (item) renderPeekMemory(item);
      });
    }

    document.getElementById("mem-keyword").addEventListener("input", debounce(function() {
      state.memory.keyword = this.value.trim();
      state.memory.page = 1;
      fetchMemories();
    }, 300));

    document.getElementById("mem-page-size").addEventListener("change", function() {
      state.memory.pageSize = parseInt(this.value) || 20;
      state.memory.page = 1;
      fetchMemories();
    });

    document.getElementById("mem-prev").addEventListener("click", function() {
      if (state.memory.page > 1) { state.memory.page--; fetchMemories(); }
    });
    document.getElementById("mem-next").addEventListener("click", function() {
      if (state.memory.hasMore) { state.memory.page++; fetchMemories(); }
    });
    initPageJump();
    updateTimeline();  // 初始加载星轨
  }

  function debounce(fn, ms) {
    var timer;
    return function() {
      var self = this, args = arguments;
      clearTimeout(timer);
      timer = setTimeout(function() { fn.apply(self, args); }, ms || 300);
    };
  }

  /* ================================================================
     Recall Page
     ================================================================ */
  async function runRecall() {
    var query = document.getElementById("recall-query").value.trim();
    if (!query) return showToast("请输入查询内容", true);
    var k = parseInt(document.getElementById("recall-k").value) || 5;
    var uid = document.getElementById("recall-uid").value.trim();
    var btn = document.getElementById("recall-search-btn");
    btn.disabled = true;

    try {
      var params = new URLSearchParams();
      params.set("q", query);
      params.set("k", String(k));
      if (uid) params.set("uid", uid);
      var data = unwrapApiData(await apiRequest("recall-test?" + params.toString())) || {};
      renderRecallResults(data);
    } catch (e) {
      showToast(e.message || "搜索失败", true);
    } finally {
      btn.disabled = false;
    }
  }

  function renderRecallResults(data) {
    state._recallCache = data;
    var stats = document.getElementById("recall-stats");
    var container = document.getElementById("recall-results");
    var total = data.total || 0;
    var diaryRefs = data.diary_refs || [];
    var injectedText = data.injected_text || "";

    // 统计信息
    if (total) {
      stats.classList.remove("hidden");
      var countText = "召回 " + total + " 条原子";
      if (diaryRefs.length) countText += "，回溯 " + diaryRefs.length + " 篇日记";
      document.getElementById("recall-count-text").textContent = countText;
      var timeEl = document.getElementById("recall-time-text");
      if (timeEl) timeEl.textContent = "";
    } else {
      stats.classList.add("hidden");
    }

    if (!total && !injectedText) {
      container.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-tertiary)">' + window.t("recall.noMatch") + '</div>';
      return;
    }

    // ── 主结果：注入 LLM 的格式化内容 ──
    var html = "";
    html += '<div class="recall-injected-section">';
    html += '<div class="recall-injected-header">';
    html += '<span class="recall-injected-label">📋 实际注入 LLM 的内容</span>';
    html += '</div>';
    html += '<pre class="recall-injected-text">' + esc(injectedText) + '</pre>';
    html += '</div>';

    // ── 折叠：技术详情（原子列表 + 分数） ──
    html += '<div class="recall-detail-section">';
    html += '<div class="recall-detail-header" onclick="toggleRecallDetail()">';
    html += '<span class="recall-detail-toggle" id="recall-detail-toggle">▸</span>';
    html += '<span>召回原子详情（' + total + ' 条）</span>';
    html += '</div>';
    html += '<div class="recall-detail-body hidden" id="recall-detail-body">';

    html += (data.results || []).map(function(r, i) {
      var pct = r.score_percentage || 0;
      var badgeCls = pct >= 70 ? "high" : pct >= 45 ? "medium" : "low";
      return '<div class="result-card" data-memory-id="' + r.memory_id + '">' +
        '<div class="result-card-header">' +
          '<span class="result-rank">#' + (i + 1) + '</span>' +
          '<span class="result-score-badge ' + badgeCls + '">' + pct.toFixed(1) + '%</span>' +
          '<span class="result-card-type">' + esc(r.type || "") + '</span>' +
          '<span style="margin-left:auto;font-size:11px;color:var(--text-tertiary)">' +
            (r.date || "") + ' · imp: ' + (r.importance != null ? Number(r.importance).toFixed(2) : "--") +
          '</span>' +
        '</div>' +
        '<div class="result-content">' + esc(cleanDisplayText(r.content || "")) + '</div>' +
      '</div>';
    }).join("");

    html += '</div></div>'; // recall-detail-section

    // ── 折叠：日记溯源 ──
    if (diaryRefs.length) {
      html += '<div class="recall-detail-section">';
      html += '<div class="recall-detail-header" onclick="toggleRecallDiary()">';
      html += '<span class="recall-detail-toggle" id="recall-diary-toggle">▸</span>';
      html += '<span>溯源日记（' + diaryRefs.length + ' 篇）</span>';
      html += '</div>';
      html += '<div class="recall-detail-body hidden" id="recall-diary-body">';
      diaryRefs.forEach(function(dr) {
        html += '<div class="recall-diary-card">';
        html += '<div class="recall-diary-header">[' + esc(dr.date || "") + ' 日记#' + dr.diary_id + ']</div>';
        html += '<div class="recall-diary-content">' + esc(dr.snippet || "") + '</div>';
        html += '</div>';
      });
      html += '</div></div>';
    }

    container.innerHTML = html;
  }

  function toggleRecallDetail() {
    var body = document.getElementById("recall-detail-body");
    var toggle = document.getElementById("recall-detail-toggle");
    if (!body || !toggle) return;
    body.classList.toggle("hidden");
    toggle.classList.toggle("open");
  }

  function toggleRecallDiary() {
    var body = document.getElementById("recall-diary-body");
    var toggle = document.getElementById("recall-diary-toggle");
    if (!body || !toggle) return;
    body.classList.toggle("hidden");
    toggle.classList.toggle("open");
  }

  function initRecallPage() {
    document.getElementById("recall-search-btn").addEventListener("click", runRecall);
    document.getElementById("recall-query").addEventListener("keydown", function(e) {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) runRecall();
    });
    document.getElementById("recall-k").addEventListener("input", function() {
      document.getElementById("recall-k-value").textContent = this.value;
    });
  }

  /* ================================================================
     System Page
     ================================================================ */
  async function fetchSystemOverview() {
    try {
      // 同时拉取 stats 和 health
      var [statsData, healthData] = await Promise.all([
        apiRequest("stats").then(unwrapApiData).catch(function() { return {}; }),
        fetch("/health").then(function(r) { return r.json(); }).catch(function() { return {}; }),
      ]);
      renderSystemOverview(statsData, healthData);
    } catch (e) {
      showToast(e.message || "系统加载失败", true);
    }
  }

  function renderSystemOverview(data, health) {
    health = health || {};
    state._systemCache = { data: data, health: health };

    // ── 统计卡片 ──
    setText("ss-users", data.users || 0);
    setText("ss-diaries", data.diaries || 0);
    setText("ss-atoms", data.atoms || 0);
    setText("ss-nodes", data.graph_nodes || 0);
    setText("ss-edges", data.graph_edges || 0);

    // ── 健康状态 ──
    var statusEl = document.getElementById("ss-health-status");
    if (statusEl) {
      if (health.status === "ok") {
        statusEl.innerHTML = '<span class="status-pill active">● 运行中</span>';
      } else {
        statusEl.innerHTML = '<span class="status-pill archived">● ' + (health.status || "未知") + '</span>';
      }
    }

    setText("ss-health-db", health.db && health.db.connected
      ? '✓ 已连接 (' + (health.db.nodes || 0) + ' 节点, ' + (health.db.edges || 0) + ' 边)'
      : '✗ 未连接');

    var llmEl = document.getElementById("ss-health-llm");
    if (llmEl) {
      llmEl.textContent = health.llm && health.llm.configured ? '✓ 已配置' : '✗ 未配置';
      llmEl.style.color = health.llm && health.llm.configured ? 'var(--success)' : 'var(--danger)';
    }

    var embedEl = document.getElementById("ss-health-embed");
    if (embedEl) {
      embedEl.textContent = health.embed && health.embed.configured ? '✓ 已配置' : '✗ 未配置';
      embedEl.style.color = health.embed && health.embed.configured ? 'var(--success)' : 'var(--text-tertiary)';
    }
  }

  function setText(id, val) {
    var el = document.getElementById(id);
    if (el) el.textContent = String(val ?? "--");
  }

  function initSystemPage() {
    var refreshBtn = document.getElementById("system-refresh-btn");
    if (refreshBtn) refreshBtn.addEventListener("click", fetchSystemOverview);

    var archiveBtn = document.getElementById("sys-archive-btn");
    if (archiveBtn) archiveBtn.addEventListener("click", async function() {
      var resultEl = document.getElementById("sys-op-result");
      if (resultEl) resultEl.textContent = "归档中...";
      try {
        var resp = await fetch("/api/v1/archive/run", { method: "POST" });
        var data = await resp.json();
        if (resultEl) resultEl.textContent = data.ok ? "✅ 归档完成，已处理 " + (data.archived || 0) + " 条" : "❌ " + (data.error || "归档失败");
      } catch (e) {
        if (resultEl) resultEl.textContent = "❌ " + e.message;
      }
    });

    var decayBtn = document.getElementById("sys-decay-btn");
    if (decayBtn) decayBtn.addEventListener("click", async function() {
      var resultEl = document.getElementById("sys-op-result");
      if (resultEl) resultEl.textContent = "衰减中...";
      try {
        var resp = await fetch("/api/v1/decay/run", { method: "POST" });
        var data = await resp.json();
        if (resultEl) resultEl.textContent = data.ok ? "✅ 衰减完成，已处理 " + (data.decay_count || 0) + " 条" : "❌ " + (data.error || "衰减失败");
      } catch (e) {
        if (resultEl) resultEl.textContent = "❌ " + e.message;
      }
    });
  }

  /* ================================================================
     Graph Page
     ================================================================ */
  async function fetchGraphStats() {
    try {
      var data = unwrapApiData(await apiRequest("stats")) || {};
      document.getElementById("gs-total").textContent = data.atoms || data.diary_months || "0";
      document.getElementById("gs-nodes").textContent = data.graph_nodes || "0";
      document.getElementById("gs-edges").textContent = data.graph_edges || "0";
    } catch (_) {}
  }

  /* ================================================================
     Init
     ================================================================ */
  async function init() {
    try {
      applyTheme(readTheme());
      listenBridgeTheme();

      initSidebar();
      initMemoryPage();
      initRecallPage();
      initPersonaPage();
      initSystemPage();

      var peekClose = document.getElementById("peek-close");
      if (peekClose) peekClose.addEventListener("click", closePeek);
      var peekOverlay = document.getElementById("peek-overlay");
      if (peekOverlay) peekOverlay.addEventListener("click", closePeek);
      document.addEventListener("keydown", function(e) {
        if (e.key === "Escape") {
          closePeek();
          state.memory.selected.clear();
          renderMemoriesVirtual();
          updateBatchBar();
        }
      });

      fetchGraphStats();
      startHealthPolling();
      switchPage("graph");
    } catch (e) {
      console.error("Init error:", e);
    }
  }

  /* ================================================================
     Status Dot + Bottom Timeline
     ================================================================ */
  function updateStatusDot(health) {
    var dot = document.getElementById("status-dot");
    var core = dot ? dot.querySelector(".status-dot-core") : null;
    if (!core) return;
    if (!health) { core.className = "status-dot-core"; return; }
    if (health.status === "ok") {
      core.className = "status-dot-core ok";
      dot.title = "服务运行中";
    } else if (health.db && health.db.connected) {
      core.className = "status-dot-core warn";
      dot.title = "服务异常";
    } else {
      core.className = "status-dot-core err";
      dot.title = "服务离线";
    }
  }

  async function startHealthPolling() {
    async function tick() {
      try {
        var health = await fetch("/health").then(function(r) { return r.json(); }).catch(function() { return {}; });
        updateStatusDot(health);
      } catch (_) {}
    }
    tick();
    setInterval(tick, 30000);
  }

  /* ================================================================
     Persona / User Profile Page
     ================================================================ */
  async function fetchPersonas() {
    var grid = document.getElementById("persona-grid");
    if (!grid) return;
    grid.innerHTML = '<div class="persona-empty">' + window.t("common.loading") + '</div>';

    try {
      var data = unwrapApiData(await apiRequest("users")) || {};
      state.personas.users = Array.isArray(data) ? data : (data.users || []);
      renderPersonaGrid();
    } catch (e) {
      grid.innerHTML = '<div class="persona-empty">' + window.t("persons.loadError") + '</div>';
      showToast(e.message || window.t("persons.loadError"), true);
    }
  }

  function renderPersonaGrid() {
    var grid = document.getElementById("persona-grid");
    if (!grid) return;

    var users = state.personas.users;
    var keyword = state.personas.keyword.trim().toLowerCase();

    // Filter by keyword
    if (keyword) {
      users = users.filter(function(u) {
        return (u.uid || "").toLowerCase().includes(keyword)
            || (u.name || "").toLowerCase().includes(keyword)
            || (u.tags || []).some(function(t) { return t.toLowerCase().includes(keyword); })
            || (u.summary || "").toLowerCase().includes(keyword);
      });
    }

    // Update count
    var countEl = document.getElementById("persona-count");
    if (countEl) countEl.textContent = window.t("persons.count", users.length);

    if (!users.length) {
      grid.innerHTML = '<div class="persona-empty">' + window.t(keyword ? "common.noData" : "persons.noUsers") + '</div>';
      return;
    }

    var html = "";
    users.forEach(function(u) {
      var initial = (u.name || u.uid || "?").charAt(0).toUpperCase();
      var tier = (u.tier || "new").toLowerCase();
      var tags = u.tags || [];
      var summary = u.summary || "";
      var lastUpdate = u.last_update ? formatPersonaTime(u.last_update) : "--";

      // Tier badge label
      var tierLabels = { new: "New", known: "Known", trusted: "Trusted", core: "Core" };
      var tierLabel = tierLabels[tier] || tier;

      // Confidence
      var conf = u.confidence != null ? Number(u.confidence) : 0;
      var confClass = conf >= 0.7 ? "high" : conf >= 0.4 ? "medium" : "low";

      html += '<div class="persona-card" data-uid="' + esc(u.uid) + '">';
      html += '<div class="persona-card-top">';
      html += '<div class="persona-card-avatar">' + esc(initial) + '</div>';
      html += '<div class="persona-card-info">';
      html += '<div class="persona-card-name">' + esc(u.name || u.uid) + '</div>';
      html += '<div class="persona-card-uid">' + esc(u.uid) + '</div>';
      html += '</div>';
      html += '<div class="persona-card-tier ' + esc(tier) + '">' + esc(tierLabel) + '</div>';
      html += '</div>';

      if (summary) {
        html += '<div class="persona-card-summary">' + esc(summary) + '</div>';
      }

      if (tags.length) {
        html += '<div class="persona-card-tags">';
        tags.forEach(function(t) {
          html += '<span class="persona-card-tag">' + esc(t) + '</span>';
        });
        html += '</div>';
      }

      html += '<div class="persona-card-meta">';
      html += '<span class="persona-card-meta-item">';
      html += '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>';
      html += esc(lastUpdate);
      html += '</span>';
      html += '<span class="persona-confidence">';
      html += '<span class="persona-confidence-dot ' + confClass + '"></span>';
      html += (conf * 100).toFixed(0) + '%';
      html += '</span>';
      html += '<span style="margin-left:auto">' + (u.identity_count || 0) + ' ' + window.t("persons.identities") + '</span>';
      html += '</div>';

      html += '</div>';
    });

    grid.innerHTML = html;

    // Bind click
    grid.querySelectorAll(".persona-card").forEach(function(card) {
      card.addEventListener("click", function() {
        var uid = card.dataset.uid;
        if (uid) openPersonaDetail(uid);
      });
    });
  }

  function formatPersonaTime(ts) {
    if (!ts) return "--";
    var d = new Date(Number(ts) * 1000);
    if (isNaN(d.getTime())) return String(ts).slice(0, 10);
    var now = Date.now();
    var diff = now - d.getTime();
    if (diff < 60000) return window.t("misc.justNow") || "刚刚";
    if (diff < 3600000) return Math.floor(diff / 60000) + "m ago";
    if (diff < 86400000) return Math.floor(diff / 3600000) + "h ago";
    return d.toLocaleDateString();
  }

  async function openPersonaDetail(uid) {
    if (!uid) return;
    state._personaDetailUid = uid;

    var panel = document.getElementById("peek-panel");
    panel.classList.add("visible", "wide");
    document.getElementById("peek-overlay").classList.add("visible");
    document.getElementById("peek-badge").innerHTML = "";
    document.getElementById("peek-title").textContent = window.t("persons.detailTitle");
    document.getElementById("peek-body").innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-tertiary)">' + window.t("common.loading") + '</div>';

    try {
      var data = unwrapApiData(await apiRequest("users/detail?uid=" + encodeURIComponent(uid)));
      renderPersonaDetail(data);
    } catch (e) {
      document.getElementById("peek-body").innerHTML = '<div style="text-align:center;padding:40px;color:var(--danger)">' + esc(e.message || window.t("misc.requestFailed")) + '</div>';
    }
  }

  function renderPersonaDetail(data) {
    if (!data) return;
    var uid = data.uid || "";
    var name = data.name || uid;
    var initial = name.charAt(0).toUpperCase();
    var tags = data.tags || [];
    var summary = data.summary || "";
    var fullMarkdown = data.full_markdown || "";
    var tier = (data.tier || "new").toLowerCase();
    var tierLabels = { new: "New", known: "Known", trusted: "Trusted", core: "Core" };
    var identities = data.identities || [];
    var atoms = data.recent_atoms || [];
    var lastUpdate = data.last_update ? formatPersonaTime(data.last_update) : "--";

    document.getElementById("peek-title").textContent = name;

    var html = "";

    // Header
    html += '<div class="persona-detail-header">';
    html += '<div class="persona-detail-avatar">' + esc(initial) + '</div>';
    html += '<div class="persona-detail-info">';
    html += '<div class="persona-detail-name">' + esc(name) + '</div>';
    html += '<div class="persona-detail-uid">' + esc(uid) + '</div>';
    html += '</div>';
    html += '<div class="persona-card-tier ' + esc(tier) + '" style="margin-top:0">' + esc(tierLabels[tier] || tier) + '</div>';
    html += '</div>';

    // Tags
    if (tags.length) {
      html += '<div class="persona-detail-section">';
      html += '<div class="persona-detail-section-title">' + window.t("detail.topics") + '</div>';
      html += '<div class="persona-detail-tags">';
      tags.forEach(function(t) {
        html += '<span class="persona-detail-tag">' + esc(t) + '</span>';
      });
      html += '</div></div>';
    }

    // Summary
    if (summary) {
      html += '<div class="persona-detail-section">';
      html += '<div class="persona-detail-section-title">' + window.t("detail.content") + '</div>';
      html += '<div class="persona-detail-summary">' + renderWikilinks(summary || fullMarkdown) + '</div>';
      html += '</div>';
    }

    // Metadata grid
    html += '<div class="persona-detail-section">';
    html += '<div class="persona-detail-section-title">' + window.t("detail.metadata") + '</div>';
    html += '<div class="persona-detail-meta-grid">';

    html += '<div class="persona-detail-meta-item">';
    html += '<div class="persona-detail-meta-label">' + window.t("persons.tier") + '</div>';
    html += '<div class="persona-detail-meta-value">' + esc(tierLabels[tier] || tier) + '</div></div>';

    html += '<div class="persona-detail-meta-item">';
    html += '<div class="persona-detail-meta-label">' + window.t("persons.version") + '</div>';
    html += '<div class="persona-detail-meta-value">v' + (data.version || 0) + '</div></div>';

    html += '<div class="persona-detail-meta-item">';
    html += '<div class="persona-detail-meta-label">' + window.t("persons.confidence") + '</div>';
    html += '<div class="persona-detail-meta-value">' + (data.confidence != null ? (Number(data.confidence) * 100).toFixed(0) + "%" : "--") + '</div></div>';

    html += '<div class="persona-detail-meta-item">';
    html += '<div class="persona-detail-meta-label">' + window.t("persons.lastUpdate") + '</div>';
    html += '<div class="persona-detail-meta-value">' + esc(lastUpdate) + '</div></div>';

    if (data.diary_count_since_full != null) {
      html += '<div class="persona-detail-meta-item">';
      html += '<div class="persona-detail-meta-label">' + window.t("persons.diaryCount") + '</div>';
      html += '<div class="persona-detail-meta-value">' + data.diary_count_since_full + '</div></div>';
    }

    if (data.incremental_count != null) {
      html += '<div class="persona-detail-meta-item">';
      html += '<div class="persona-detail-meta-label">' + window.t("persons.incCount") + '</div>';
      html += '<div class="persona-detail-meta-value">' + data.incremental_count + '</div></div>';
    }

    html += '</div></div>';

    // Identities
    if (identities.length) {
      html += '<div class="persona-detail-section">';
      html += '<div class="persona-detail-section-title">' + window.t("persons.identities") + ' (' + identities.length + ')</div>';
      identities.forEach(function(id) {
        html += '<div class="persona-detail-identity">';
        html += '<div class="persona-detail-identity-left">';
        html += '<div class="persona-detail-identity-platform">' + esc(id.platform || "") + '</div>';
        html += '<div class="persona-detail-identity-name">' + esc(id.name || "") + '</div>';
        html += '</div>';
        html += '<div class="persona-detail-identity-since">' + (id.since || id.first_seen || "") + (id.verified ? ' ✓' : '') + '</div>';
        html += '</div>';
      });
      html += '</div>';
    }

    // Recent atoms
    html += '<div class="persona-detail-section">';
    html += '<div class="persona-detail-section-title">' + window.t("persons.recentAtoms") + ' (' + atoms.length + ')</div>';
    if (atoms.length) {
      html += '<div class="persona-detail-atom-list">';
      atoms.forEach(function(a) {
        html += '<div class="persona-detail-atom-item" data-date="' + esc(a.date || "") + '">';
        html += '<div class="persona-detail-atom-content">' + esc(a.content || "") + '</div>';
        html += '<div class="persona-detail-atom-meta">';
        html += '<span class="type-tag">' + esc(a.type || "") + '</span>';
        html += '<span>' + window.t("detail.importance") + ': ' + (a.importance != null ? Number(a.importance).toFixed(2) : "--") + '</span>';
        if (a.date) html += '<span>' + esc(a.date) + '</span>';
        html += '</div></div>';
      });
      html += '</div>';
    } else {
      html += '<div style="text-align:center;padding:var(--space-6);color:var(--text-tertiary);font-size:13px">' + window.t("persons.noAtoms") + '</div>';
    }
    html += '</div>';

    document.getElementById("peek-body").innerHTML = html;

    // Bind atom click → open diary peek
    document.querySelectorAll(".persona-detail-atom-item").forEach(function(item) {
      item.addEventListener("click", function() {
        var date = item.dataset.date;
        if (date) {
          // Search for this date's diary in the memories page
          var memInput = document.getElementById("mem-keyword");
          if (memInput) memInput.value = date;
          switchPage("memories");
        }
      });
    });
  }

  function initPersonaPage() {
    var searchInput = document.getElementById("persona-search");
    if (searchInput) {
      searchInput.addEventListener("input", debounce(function() {
        state.personas.keyword = this.value;
        renderPersonaGrid();
      }, 300));
    }
  }

  /* ================================================================
     Expose to graph-ui
     ================================================================ */
  window.lmState = state;
  window.lmShowToast = showToast;
  window.lmApiRequest = apiRequest;
  window.lmOpenPeekNode = renderPeekNode;
  window.lmOpenPeekMemory = renderPeekMemory;
  window.lmClosePeek = closePeek;
  window.lmFetchGraphStats = fetchGraphStats;
  window.lmEsc = esc;
  window.lmStatusPill = statusPill;
  window.lmNodeBadge = nodeBadge;

  document.addEventListener("DOMContentLoaded", init);
})();
