/* 梗采集器 WebUI —— 依赖 /api/plugin/page/bridge-sdk.js 提供的 window.AstrBotPluginPage */
(function () {
  "use strict";

  const B = window.AstrBotPluginPage || window.AstrBotPluginView || null;
  let readyPromise = B ? B.ready() : Promise.resolve({});

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const esc = (value) =>
    String(value === undefined || value === null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");

  function toast(message, kind) {
    const box = $("toast");
    box.textContent = message;
    box.className = "toast show " + (kind || "");
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => {
      box.className = "toast";
    }, 3200);
  }

  async function call(method, endpoint, payload) {
    if (!B) throw new Error("未检测到 AstrBot 页面桥接，请从插件页进入");
    await readyPromise;
    let res;
    try {
      res = method === "GET" ? await B.apiGet(endpoint, payload) : await B.apiPost(endpoint, payload);
    } catch (err) {
      toast(err.message || "请求失败", "error");
      throw err;
    }
    const body = res && typeof res === "object" && "ok" in res ? res : { ok: true, data: res };
    if (!body.ok) {
      const message = body.message || body.error || "请求失败";
      toast(message, "error");
      const error = new Error(message);
      error.code = body.error;
      throw error;
    }
    return body.data;
  }

  const fmtTime = (value) => {
    if (!value) return "-";
    const text = String(value).replace("T", " ");
    return text.slice(0, 19);
  };

  /* ---------------------------------------------------------------- 标签页 */

  const loaders = {};
  let currentTab = "overview";

  function switchTab(tab) {
    document.querySelectorAll(".tab").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === tab);
    });
    document.querySelectorAll(".panel").forEach((panel) => {
      panel.classList.toggle("active", panel.id === "panel-" + tab);
    });
    currentTab = tab;
    if (loaders[tab]) loaders[tab]();
  }

  document.getElementById("tabs").addEventListener("click", (event) => {
    const btn = event.target.closest(".tab");
    if (btn) switchTab(btn.dataset.tab);
  });

  /* ---------------------------------------------------------------- 概览 */

  function statCard(label, value, hint) {
    const card = el("div", "stat");
    card.appendChild(el("div", "stat-value", String(value)));
    card.appendChild(el("div", "stat-label", label));
    if (hint) card.appendChild(el("div", "stat-hint", hint));
    return card;
  }

  function taskRow(task, compact) {
    const wrap = el("div", "task");
    const head = el("div", "task-head");
    head.appendChild(el("span", "task-kind", task.kind === "collection" ? "合集" : task.kind === "sync" ? "同步" : "视频"));
    head.appendChild(el("span", "task-target", task.target || ""));
    head.appendChild(el("span", "task-status " + (task.status || ""), task.status || ""));
    wrap.appendChild(head);

    const progress = Math.max(0, Math.min(100, Number(task.progress) || 0));
    const bar = el("div", "bar");
    const fill = el("div", "bar-fill");
    fill.style.width = progress + "%";
    bar.appendChild(fill);
    wrap.appendChild(bar);

    const stage = (task.payload && task.payload.stage) || "";
    const meta = el("div", "task-meta");
    meta.textContent = progress + "%" + (stage ? " · " + stage : "") + (task.last_error ? " · " + task.last_error : "");
    wrap.appendChild(meta);

    if (!compact && task.payload && task.payload.result) {
      wrap.appendChild(renderResult(task.payload.result));
    }
    return wrap;
  }

  function renderResult(result) {
    const box = el("div", "result");
    if (!result || typeof result !== "object") {
      box.textContent = String(result || "");
      return box;
    }
    if (result.error && !result.memes_total) {
      box.appendChild(el("div", "result-err", "失败：" + result.error));
      return box;
    }
    const lines = [];
    if (result.title) lines.push("《" + result.title + "》");
    if (result.status) lines.push("状态：" + result.status);
    if (result.track_lan || result.provider) lines.push("字幕：" + (result.track_lan || "-") + " / " + (result.provider || "-"));
    if (result.memes_total !== undefined) {
      lines.push(
        "抽取 " + result.memes_total + " 条 · 新增 " + (result.memes_created || 0) +
        " · 冲突 " + (result.conflicts || 0) + " · 待审 " + (result.reviews || 0)
      );
    }
    if (result.total !== undefined) {
      lines.push("共 " + result.total + " 条 · 成功 " + (result.done || 0) + " · 失败 " + (result.failed || 0) + " · 跳过 " + (result.skipped || 0));
    }
    (result.notes || []).forEach((note) => lines.push("· " + note));
    box.textContent = lines.join("\n");
    return box;
  }

  loaders.overview = async function () {
    try {
      const data = await call("GET", "status", {});
      const stats = data.stats || {};
      const cards = $("stat-cards");
      cards.innerHTML = "";
      cards.appendChild(statCard("词条", stats.memes || 0, "published + draft"));
      cards.appendChild(statCard("视频", stats.videos || 0));
      cards.appendChild(statCard("待审核", stats.reviews || 0, "冲突与低置信"));
      cards.appendChild(statCard("群黑话", stats.slangs || 0));
      cards.appendChild(statCard("字幕", stats.transcripts || 0, stats.fts ? "FTS5 已启用" : "LIKE 降级"));
      $("review-badge").hidden = !(stats.reviews > 0);
      $("review-badge").textContent = String(stats.reviews || 0);

      $("brand-sub").textContent = "v" + (data.version || "0.1.0");
      renderAuth(data.auth || {});
      renderMemewiki(data.memewiki || {});
      renderProviders(data.providers || [], data.settings || {});
      renderRecent(data.tasks || []);
      renderWarnings(data.settings_warnings || []);
    } catch (err) {
      /* toast 已提示 */
    }
  };

  function renderAuth(auth) {
    const dot = $("auth-dot");
    const logged = !!auth.logged_in;
    dot.className = "dot " + (logged ? "on" : "off");
    $("auth-text").textContent = logged
      ? (auth.uname || auth.mid || "已登录")
      : auth.status === "expired" ? "登录已失效" : "未登录 B 站";
    const desc = $("auth-desc");
    if (logged) {
      desc.textContent = "已登录：" + (auth.uname || "") + (auth.mid ? "（" + auth.mid + "）" : "") + "，字幕与合集接口将带登录态。";
    } else if (auth.status === "expired") {
      desc.textContent = "登录态已失效（Cookie 过期或被踢），请重新扫码登录。";
    } else {
      desc.textContent = "未登录。扫码登录后即可解析需要登录态的字幕（AI 字幕必须登录）。";
    }
    if (auth.masked) $("cookie-input").placeholder = auth.masked;
  }

  function renderMemewiki(info) {
    const box = $("memewiki-box");
    const mode = info.mode || "unavailable";
    const labels = {
      inprocess: "已连接「梗百科」（进程内写入）",
      export: "未连接「梗百科」，可导出 JSONL 手动导入",
      unavailable: "未检测到「梗百科」插件",
    };
    box.textContent = (labels[mode] || mode) + (info.pending !== undefined ? " · 待同步 " + info.pending + " 条" : "");
  }

  function renderProviders(providers, settings) {
    const box = $("provider-box");
    box.innerHTML = "";
    if (!providers.length) {
      box.textContent = "AstrBot 尚未配置可用的对话模型，请先在「服务提供商」中添加。";
      return;
    }
    const list = el("div", "chips");
    providers.forEach((p) => {
      const chip = el("span", "chip" + (settings.provider_id === p.id ? " active" : ""), (p.model || p.id) + (p.type ? " · " + p.type : ""));
      list.appendChild(chip);
    });
    box.appendChild(list);
    if (!settings.provider_id) {
      box.appendChild(el("div", "muted small", "当前跟随 AstrBot 默认模型；可在设置页指定固定 Provider。"));
    }
  }

  function renderRecent(tasks) {
    const box = $("recent-tasks");
    box.innerHTML = "";
    if (!tasks.length) {
      box.textContent = "暂无任务";
      return;
    }
    tasks.forEach((task) => box.appendChild(taskRow(task, true)));
  }

  function renderWarnings(warnings) {
    const box = $("settings-warn");
    if (!warnings || !warnings.length) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    box.textContent = "配置提醒：" + warnings.join("；");
  }

  /* ---------------------------------------------------------------- 登录 */

  let qrTimer = null;

  function stopQr() {
    if (qrTimer) {
      clearInterval(qrTimer);
      qrTimer = null;
    }
  }

  $("btn-qr").addEventListener("click", async () => {
    stopQr();
    try {
      const data = await call("POST", "auth/qrcode", {});
      const wrap = $("qr-wrap");
      wrap.hidden = false;
      const box = $("qr-box");
      box.innerHTML = "";
      const url = data.url || "";
      if (window.qrcode) {
        const qr = window.qrcode(0, "M");
        qr.addData(url);
        qr.make();
        box.innerHTML = qr.createSvgTag({ cellSize: 4, margin: 6 });
      }
      const link = $("qr-link");
      link.innerHTML = "";
      const anchor = el("a", "", "在浏览器打开登录页");
      anchor.href = url;
      anchor.target = "_blank";
      anchor.rel = "noreferrer";
      link.appendChild(anchor);
      $("qr-hint").textContent = "请使用 B 站手机客户端扫码（约 3 分钟内有效）";
      const key = data.qrcode_key;
      qrTimer = setInterval(async () => {
        try {
          const poll = await call("GET", "auth/poll", { key });
          $("qr-hint").textContent = poll.message || "";
          if (poll.status === "ok") {
            stopQr();
            $("qr-wrap").hidden = true;
            toast("登录成功", "ok");
            loaders.overview();
          } else if (poll.status === "expired" || poll.status === "error") {
            stopQr();
          }
        } catch (err) {
          stopQr();
        }
      }, 2000);
    } catch (err) {
      /* toast 已提示 */
    }
  });

  $("btn-auth-refresh").addEventListener("click", async () => {
    try {
      const data = await call("GET", "auth/status", { verify: 1 });
      renderAuth(data);
      toast(data.logged_in ? "登录态有效" : "当前未登录或已失效", data.logged_in ? "ok" : "error");
    } catch (err) {
      /* ignore */
    }
  });

  $("btn-logout").addEventListener("click", async () => {
    if (!confirm("确定退出登录？本地保存的 Cookie 会被删除。")) return;
    try {
      await call("POST", "auth/logout", {});
      stopQr();
      $("qr-wrap").hidden = true;
      await loaders.overview();
      toast("已退出登录");
    } catch (err) {
      /* ignore */
    }
  });

  $("btn-cookie-save").addEventListener("click", async () => {
    const raw = $("cookie-input").value.trim();
    if (!raw) {
      toast("请先粘贴 Cookie", "error");
      return;
    }
    try {
      await call("POST", "auth/cookie", { cookie: raw });
      $("cookie-input").value = "";
      await loaders.overview();
      toast("Cookie 已保存", "ok");
    } catch (err) {
      /* ignore */
    }
  });

  /* ---------------------------------------------------------------- 解析 */

  async function pollTasks(containerId, filters) {
    const box = $(containerId);
    try {
      const tasks = await call("GET", "tasks", { limit: 20 });
      const items = (tasks || []).filter((task) => !filters || filters.includes(task.kind));
      if (!items.length) {
        box.textContent = "暂无任务";
        box.className = "muted";
        return false;
      }
      box.className = "";
      box.innerHTML = "";
      items.forEach((task) => box.appendChild(taskRow(task, false)));
      return items.some((task) => task.status === "queued" || task.status === "running");
    } catch (err) {
      return false;
    }
  }

  let taskTimer = null;
  function startTaskPolling() {
    if (taskTimer) return;
    taskTimer = setInterval(async () => {
      if (currentTab !== "parse" && currentTab !== "collection") return;
      const active = await pollTasks("task-list", ["video"]);
      const activeCol = await pollTasks("col-task-list", ["collection"]);
      if (!active && !activeCol) {
        clearInterval(taskTimer);
        taskTimer = null;
      }
    }, 1500);
  }

  $("btn-parse").addEventListener("click", async () => {
    const url = $("parse-url").value.trim();
    if (!url) {
      toast("请输入视频链接或 BV 号", "error");
      return;
    }
    try {
      const data = await call("POST", "parse", { url, force: $("parse-force").checked });
      toast("已加入队列：" + (data.target || ""), "ok");
      await pollTasks("task-list", ["video"]);
      startTaskPolling();
    } catch (err) {
      /* ignore */
    }
  });

  $("btn-refresh-tasks").addEventListener("click", async () => {
    const active = await pollTasks("task-list", ["video"]);
    if (active) startTaskPolling();
  });

  $("btn-collection").addEventListener("click", async () => {
    const url = $("col-url").value.trim();
    if (!url) {
      toast("请输入合集链接", "error");
      return;
    }
    try {
      const data = await call("POST", "collection", { url, force: $("col-force").checked });
      toast("合集已加入队列：" + (data.target || ""), "ok");
      await pollTasks("col-task-list", ["collection"]);
      startTaskPolling();
    } catch (err) {
      /* ignore */
    }
  });

  loaders.parse = () => pollTasks("task-list", ["video"]);
  loaders.collection = () => pollTasks("col-task-list", ["collection"]);

  /* ---------------------------------------------------------------- 词条 */

  let memeOffset = 0;
  const MEME_PAGE = 20;
  let memeItems = [];

  async function loadMemes(reset) {
    if (reset) {
      memeOffset = 0;
      memeItems = [];
    }
    const query = $("meme-query").value.trim();
    const status = $("meme-status").value;
    try {
      const data = await call("GET", "memes", { query, status, limit: MEME_PAGE, offset: memeOffset });
      memeItems = memeItems.concat(data.items || []);
      memeOffset = memeItems.length;
      $("meme-count").textContent = "共 " + (data.total || 0) + " 条，已显示 " + memeItems.length + " 条";
      renderMemes();
      $("btn-meme-more").hidden = memeItems.length >= (data.total || 0);
    } catch (err) {
      /* ignore */
    }
  }

  function renderMemes() {
    const table = $("meme-table");
    table.innerHTML =
      "<thead><tr><th>词条</th><th>释义</th><th>热度</th><th>置信</th><th>状态</th><th>同步</th><th></th></tr></thead>";
    const body = el("tbody");
    memeItems.forEach((meme, index) => {
      const tr = el("tr");
      tr.appendChild(el("td", "term", meme.term));
      const def = el("td", "def", meme.definition || "");
      def.title = meme.definition || "";
      tr.appendChild(def);
      tr.appendChild(el("td", "", String(meme.heat || 0)));
      tr.appendChild(el("td", "", (Number(meme.confidence || 0) * 100).toFixed(0) + "%"));
      const statusTd = el("td");
      statusTd.appendChild(el("span", "pill " + (meme.status || ""), meme.status || ""));
      tr.appendChild(statusTd);
      tr.appendChild(el("td", "small", meme.sync_state || "-"));
      const ops = el("td", "ops");
      const edit = el("button", "btn tiny", "编辑");
      edit.addEventListener("click", () => openMemeEditor(meme));
      const del = el("button", "btn tiny danger", "删除");
      del.addEventListener("click", async () => {
        if (!confirm("删除词条「" + meme.term + "」？")) return;
        try {
          await call("POST", "meme/delete", { id: meme.id });
          memeItems.splice(index, 1);
          renderMemes();
          toast("已删除");
        } catch (err) {
          /* ignore */
        }
      });
      ops.appendChild(edit);
      ops.appendChild(del);
      tr.appendChild(ops);
      body.appendChild(tr);
    });
    table.appendChild(body);
  }

  function openMemeEditor(meme) {
    const dialog = el("dialog", "modal");
    dialog.innerHTML =
      '<form method="dialog">' +
      '<h3>编辑词条：' + esc(meme.term) + "</h3>" +
      '<label class="lbl">释义</label><textarea name="definition" rows="3">' + esc(meme.definition || "") + "</textarea>" +
      '<label class="lbl">用法</label><textarea name="usage" rows="2">' + esc(meme.usage || "") + "</textarea>" +
      '<label class="lbl">出处</label><textarea name="origin" rows="2">' + esc(meme.origin || "") + "</textarea>" +
      '<label class="lbl">状态</label><select name="status">' +
      ["published", "draft", "archived"].map((s) => '<option value="' + s + '"' + (meme.status === s ? " selected" : "") + ">" + s + "</option>").join("") +
      "</select>" +
      '<div class="btn-row"><button class="btn" value="cancel">取消</button><button class="btn primary" value="save">保存</button></div>' +
      "</form>";
    document.body.appendChild(dialog);
    dialog.addEventListener("close", async () => {
      if (dialog.returnValue !== "save") {
        dialog.remove();
        return;
      }
      const form = dialog.querySelector("form");
      const payload = {
        id: meme.id,
        definition: form.definition.value,
        usage: form.usage.value,
        origin: form.origin.value,
        status: form.status.value,
      };
      try {
        await call("POST", "meme/update", payload);
        dialog.remove();
        await loadMemes(true);
        toast("已保存", "ok");
      } catch (err) {
        dialog.remove();
      }
    });
    dialog.showModal();
  }

  $("btn-meme-search").addEventListener("click", () => loadMemes(true));
  $("meme-query").addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadMemes(true);
  });
  $("btn-meme-more").addEventListener("click", () => loadMemes(false));
  loaders.memes = () => loadMemes(true);

  /* ---------------------------------------------------------------- 审核 */

  async function loadReviews() {
    try {
      const data = await call("GET", "reviews", { status: "pending", limit: 50 });
      const box = $("review-list");
      const items = data.items || [];
      if (!items.length) {
        box.textContent = "暂无待审核项";
        box.className = "muted";
        return;
      }
      box.className = "";
      box.innerHTML = "";
      items.forEach((review) => {
        const card = el("div", "review");
        card.appendChild(el("div", "review-reason", review.kind + " · " + (review.reason || "")));
        const payload = review.payload || {};
        const term = payload.term || (payload.candidate && payload.candidate.term) || "";
        const def = payload.definition || (payload.candidate && payload.candidate.definition) || "";
        const old = payload.existing_definition || payload.old_definition || "";
        card.appendChild(el("div", "review-term", term));
        if (def) card.appendChild(el("div", "review-def", "新：" + def));
        if (old) card.appendChild(el("div", "review-def old", "旧：" + old));
        const row = el("div", "btn-row");
        const ok = el("button", "btn tiny primary", "采纳");
        const no = el("button", "btn tiny", "忽略");
        ok.addEventListener("click", () => resolveReview(review.id, "approve"));
        no.addEventListener("click", () => resolveReview(review.id, "reject"));
        row.appendChild(ok);
        row.appendChild(no);
        card.appendChild(row);
        box.appendChild(card);
      });
    } catch (err) {
      /* ignore */
    }
  }

  async function resolveReview(id, action) {
    try {
      await call("POST", "review/resolve", { id, action });
      await loadReviews();
      loaders.overview();
      toast(action === "approve" ? "已采纳" : "已忽略");
    } catch (err) {
      /* ignore */
    }
  }

  $("btn-review-refresh").addEventListener("click", loadReviews);
  loaders.reviews = loadReviews;

  /* ---------------------------------------------------------------- 黑话 */

  async function loadSlangs() {
    const scope = $("slang-scope").value.trim();
    if (!scope) {
      toast("请先填写作用域", "error");
      return;
    }
    localStorage.setItem("mh_scope", scope);
    try {
      const items = await call("GET", "slangs", { scope });
      const box = $("slang-list");
      box.className = "";
      box.innerHTML = "";
      if (!items.length) {
        box.className = "muted";
        box.textContent = "该作用域暂无黑话";
        return;
      }
      items.forEach((item) => {
        const row = el("div", "slang");
        row.appendChild(el("span", "term", item.term));
        row.appendChild(el("span", "def", item.definition || ""));
        const del = el("button", "btn tiny danger", "删除");
        del.addEventListener("click", async () => {
          try {
            await call("POST", "slang/delete", { scope, term: item.term });
            await loadSlangs();
          } catch (err) {
            /* ignore */
          }
        });
        row.appendChild(del);
        box.appendChild(row);
      });
    } catch (err) {
      /* ignore */
    }
  }

  $("btn-slang-load").addEventListener("click", loadSlangs);

  $("btn-slang-save").addEventListener("click", async () => {
    const scope = $("slang-scope").value.trim();
    const term = $("slang-term").value.trim();
    const definition = $("slang-def").value.trim();
    const aliases = $("slang-aliases").value.trim();
    if (!scope || !term || !definition) {
      toast("作用域、名称、释义均为必填", "error");
      return;
    }
    try {
      await call("POST", "slang/save", { scope, term, definition, aliases });
      $("slang-term").value = "";
      $("slang-aliases").value = "";
      $("slang-def").value = "";
      await loadSlangs();
      toast("已保存", "ok");
    } catch (err) {
      /* ignore */
    }
  });

  loaders.slangs = () => {
    const saved = localStorage.getItem("mh_scope");
    if (saved && !$("slang-scope").value) $("slang-scope").value = saved;
    if ($("slang-scope").value) loadSlangs();
  };

  /* ---------------------------------------------------------------- 设置 */

  const SETTING_META = {
    provider_id: { label: "对话模型 Provider", type: "provider", hint: "留空表示跟随 AstrBot 默认模型" },
    json_mode: { label: "JSON 模式", type: "bool" },
    temperature: { label: "温度", type: "float" },
    max_tokens: { label: "单次最大 tokens", type: "int" },
    llm_timeout: { label: "LLM 超时（秒）", type: "int" },
    chunk_chars: { label: "字幕分块字数", type: "int" },
    max_chunks: { label: "单视频最大分块", type: "int" },
    request_interval: { label: "B 站请求间隔（秒）", type: "float" },
    collection_page_size: { label: "合集每页条数", type: "int" },
    max_collection_items: { label: "合集单次上限", type: "int" },
    queue_concurrency: { label: "任务并发", type: "int" },
    raw_retention_days: { label: "原始字幕保留天数", type: "int" },
    review_required: { label: "新词条进审核", type: "bool" },
    memewiki_enabled: { label: "同步到梗百科", type: "bool" },
    memewiki_min_confidence: { label: "同步最低置信度", type: "float" },
    auto_parse_on_link: { label: "群里发链接自动解析", type: "bool" },
    inject_enabled: { label: "聊天注入梗库", type: "bool" },
    inject_max_chars: { label: "注入最大字数", type: "int" },
  };

  async function loadSettings() {
    try {
      const [settings, providers] = await Promise.all([
        call("GET", "settings", {}),
        call("GET", "providers", {}).catch(() => []),
      ]);
      const form = $("settings-form");
      form.innerHTML = "";
      Object.keys(settings).forEach((key) => {
        const meta = SETTING_META[key] || { label: key, type: "str" };
        const row = el("div", "field");
        const label = el("label", "lbl", meta.label);
        row.appendChild(label);
        let input;
        if (meta.type === "bool") {
          input = el("input");
          input.type = "checkbox";
          input.checked = !!settings[key];
        } else if (meta.type === "provider") {
          input = el("select");
          const auto = el("option", "", "跟随默认模型");
          auto.value = "";
          input.appendChild(auto);
          (providers || []).forEach((p) => {
            const option = el("option", "", (p.model || p.id) + " · " + (p.id || ""));
            option.value = p.id;
            input.appendChild(option);
          });
          input.value = settings[key] || "";
        } else if (meta.type === "int" || meta.type === "float") {
          input = el("input");
          input.type = "number";
          input.step = meta.type === "int" ? "1" : "0.1";
          input.value = settings[key];
        } else {
          input = el("input");
          input.type = "text";
          input.value = settings[key] || "";
        }
        input.dataset.key = key;
        input.dataset.type = meta.type;
        row.appendChild(input);
        if (meta.hint) row.appendChild(el("div", "muted small", meta.hint));
        form.appendChild(row);
      });
      renderWarnings([]);
    } catch (err) {
      /* ignore */
    }
  }

  $("btn-settings-save").addEventListener("click", async () => {
    const payload = {};
    document.querySelectorAll("#settings-form [data-key]").forEach((input) => {
      const key = input.dataset.key;
      const type = input.dataset.type;
      if (type === "bool") payload[key] = input.checked;
      else if (type === "int") payload[key] = parseInt(input.value, 10) || 0;
      else if (type === "float") payload[key] = parseFloat(input.value) || 0;
      else payload[key] = input.value;
    });
    try {
      const data = await call("POST", "settings/save", payload);
      if (data.warnings && data.warnings.length) {
        toast("已保存，但有配置提醒", "error");
        renderWarnings(data.warnings);
      } else {
        toast("设置已保存", "ok");
        renderWarnings([]);
      }
    } catch (err) {
      /* ignore */
    }
  });

  loaders.settings = loadSettings;

  /* ---------------------------------------------------------------- 启动 */

  (async function boot() {
    try {
      await readyPromise;
    } catch (err) {
      /* 独立打开页面时没有父窗口，仍尝试直连 */
    }
    try {
      await loaders.overview();
      await pollTasks("task-list", ["video"]);
    } catch (err) {
      /* ignore */
    }
  })();
})();
