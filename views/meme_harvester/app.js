/* 梗采集器 WebUI —— 依赖 /api/plugin/page/bridge-sdk.js 提供的 window.AstrBotPluginPage */
(function () {
  "use strict";

  const B = window.AstrBotPluginPage || window.AstrBotPluginView || null;
  const readyPromise = B ? B.ready() : Promise.resolve({});
  let pageContext = {};

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

  /* ------------------------------------------------------------ 文案映射 */

  const KIND_LABELS = { video: "视频", url: "链接", collection: "合集", sync: "同步" };
  const STATUS_LABELS = {
    queued: "排队中",
    running: "解析中",
    done: "已完成",
    failed: "失败",
    cancelled: "已取消",
    skipped: "已跳过",
  };
  const STAGE_LABELS = {
    resolving: "解析链接",
    probing: "探测平台字幕",
    fetching_subtitle: "获取字幕",
    fetching_page: "抓取网页",
    extracting: "抽取词条",
    storing: "写入数据库",
    done: "完成",
  };
  const ERROR_LABELS = {
    LOGIN_REQUIRED: "需要先在「概览」扫码登录 B 站",
    LOGIN_EXPIRED: "B 站登录已失效，请重新扫码登录",
    RISK_CONTROL: "触发 B 站风控，已暂停请求 30 分钟",
    NO_SUBTITLE: "没有可用字幕",
    SUBTITLE_MISMATCH: "字幕与视频时长不符，已拒绝",
    CHARGE_BLOCKED: "充电专属视频，无法读取字幕",
    SUBTITLE_EXPIRED: "字幕下载链接已过期",
    VIDEO_NOT_FOUND: "视频不存在或已删除",
    DOWNLOAD_FAILED: "音视频下载失败",
    MEDIA_UNSUPPORTED: "该平台或链接不受支持",
    ASR_UNAVAILABLE: "未安装 faster-whisper，无法本地转写",
    TRANSCRIBE_FAILED: "本地转写失败",
    WEB_FETCH_FAILED: "网页抓取失败",
    WEB_CONTENT_EMPTY: "网页正文过少，无法总结",
    BAD_URL: "链接无法识别",
    LLM_FAILED: "模型调用失败",
    LLM_JSON_INVALID: "模型返回内容格式异常",
    PROVIDER_NOT_CONFIGURED: "未配置可用的对话模型",
    MEMEWIKI_UNAVAILABLE: "梗百科插件不可用",
    COLLECTION_EMPTY: "合集为空或没有可解析条目",
    NOT_CANCELLED: "任务已结束，无法取消",
    NOT_FOUND: "记录不存在",
    BAD_REQUEST: "请求参数有误",
    INTERNAL: "内部错误",
  };

  const errorText = (code) => ERROR_LABELS[code] || code || "未知错误";
  const statusText = (status) => STATUS_LABELS[status] || status || "";
  const kindText = (kind) => KIND_LABELS[kind] || kind || "";
  const stageText = (stage) => {
    if (!stage) return "";
    if (stage.indexOf("collection ") === 0) return "合集进度 " + stage.slice(11);
    return STAGE_LABELS[stage] || stage;
  };

  /* ------------------------------------------------------------ 提示与请求 */

  function toast(message, kind) {
    const box = $("toast");
    if (!box || !message) return;
    box.hidden = false;
    box.textContent = message;
    box.className = "toast show " + (kind || "");
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => {
      box.className = "toast";
      setTimeout(() => {
        box.hidden = true;
      }, 260);
    }, kind === "error" ? 5000 : 3200);
  }

  async function call(method, endpoint, payload) {
    if (!B) throw new Error("未检测到 AstrBot 页面桥接，请从插件页进入");
    await readyPromise;
    let res;
    try {
      res = method === "GET" ? await B.apiGet(endpoint, payload) : await B.apiPost(endpoint, payload);
    } catch (err) {
      const text = (err && (err.message || err.error)) || "网络请求失败";
      toast(text, "error");
      throw err;
    }
    const body = res && typeof res === "object" && "ok" in res ? res : { ok: true, data: res };
    if (!body.ok) {
      const message = body.message || errorText(body.error);
      toast(message, "error");
      const error = new Error(message);
      error.code = body.error;
      throw error;
    }
    return body.data;
  }

  const fmtTime = (value) => {
    if (!value) return "-";
    return String(value).replace("T", " ").slice(0, 19);
  };

  function loading(box) {
    if (!box) return;
    box.className = "skeleton";
    box.textContent = "加载中…";
  }

  function emptyState(box, title, hint) {
    if (!box) return;
    box.className = "empty";
    box.innerHTML = "";
    box.appendChild(el("div", "empty-title", title));
    if (hint) box.appendChild(el("div", "empty-hint", hint));
  }

  function errorState(box, title, hint) {
    if (!box) return;
    box.className = "banner error";
    box.textContent = title + (hint ? "（" + hint + "）" : "");
  }

  /* ---------------------------------------------------------------- 标签页 */

  const loaders = {};
  let currentTab = "overview";
  const tabsEl = $("tabs");

  function switchTab(tab, focus) {
    currentTab = tab;
    tabsEl.querySelectorAll(".tab").forEach((btn) => {
      const on = btn.dataset.tab === tab;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
      btn.tabIndex = on ? 0 : -1;
      if (on && focus) btn.focus();
    });
    document.querySelectorAll(".panel").forEach((panel) => {
      panel.classList.toggle("active", panel.id === "panel-" + tab);
    });
    if (loaders[tab]) loaders[tab]();
  }

  tabsEl.addEventListener("click", (event) => {
    const btn = event.target.closest(".tab");
    if (btn) switchTab(btn.dataset.tab);
  });
  tabsEl.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const tabs = Array.from(tabsEl.querySelectorAll(".tab"));
    const index = tabs.findIndex((btn) => btn.classList.contains("active"));
    const next = tabs[(index + (event.key === "ArrowDown" ? 1 : tabs.length - 1)) % tabs.length];
    if (next) {
      event.preventDefault();
      switchTab(next.dataset.tab, true);
    }
  });

  /* ---------------------------------------------------------------- 任务卡 */

  function taskRow(task, options) {
    const opts = options || {};
    const wrap = el("div", "task" + (task.status === "failed" ? " failed" : ""));
    const head = el("div", "task-head");
    head.appendChild(el("span", "task-kind", kindText(task.kind)));
    head.appendChild(el("span", "task-target", task.target || ""));
    head.appendChild(el("span", "task-status " + (task.status || ""), statusText(task.status)));
    if (!opts.compact && (task.status === "queued" || task.status === "running")) {
      const cancel = el("button", "btn tiny", "取消");
      cancel.addEventListener("click", async () => {
        try {
          await call("POST", "task/cancel", { id: task.id });
          toast("已请求取消", "ok");
          refreshVisibleTasks();
        } catch (err) {
          /* toast 已提示 */
        }
      });
      head.appendChild(cancel);
    }
    if (!opts.compact) {
      const clearBtn = el("button", "btn tiny ghost", "清除");
      clearBtn.title = "删除这条任务记录；同一链接再次解析时会自动清除旧记录并重新排队";
      clearBtn.addEventListener("click", async () => {
        if (!window.confirm("清除这条任务记录？")) return;
        try {
          await call("POST", "task/clear", { id: task.id });
          toast("已清除任务记录", "ok");
          refreshVisibleTasks();
          refreshStats();
        } catch (err) {
          /* toast 已提示 */
        }
      });
      head.appendChild(clearBtn);
    }
    wrap.appendChild(head);

    const progress = Math.max(0, Math.min(100, Number(task.progress) || 0));
    const bar = el("div", "bar");
    const fill = el("div", "bar-fill" + (task.status === "failed" ? " bad" : ""));
    fill.style.width = progress + "%";
    bar.appendChild(fill);
    wrap.appendChild(bar);

    const stage = (task.payload && task.payload.stage) || "";
    const meta = el("div", "task-meta");
    const parts = [progress + "%"];
    if (stage) parts.push(stageText(stage));
    if (task.last_error && task.last_error !== "用户取消") parts.push(errorText(task.last_error));
    meta.textContent = parts.join(" · ");
    wrap.appendChild(meta);

    if (!opts.compact) {
      const result = task.payload && task.payload.result;
      if (result && result.aborted) {
        wrap.appendChild(el("div", "banner error", "已中止：" + errorText(result.aborted)));
      }
      if (result) {
        const details = el("details", "task-detail");
        details.appendChild(el("summary", "", "查看结果"));
        details.appendChild(renderResult(result));
        wrap.appendChild(details);
      } else if (task.status === "failed") {
        wrap.appendChild(el("div", "banner error", errorText(task.last_error)));
      }
    }
    return wrap;
  }

  function renderResult(result) {
    const box = el("div", "result");
    if (!result || typeof result !== "object") {
      box.textContent = String(result || "");
      return box;
    }
    if (result.error || result.status === "failed") {
      box.appendChild(el("div", "result-err", "失败：" + errorText(result.error)));
      if (result.message) box.appendChild(el("div", "muted small", result.message));
      return box;
    }
    const lines = [];
    if (result.title) lines.push("《" + result.title + "》");
    if (result.platform && result.platform !== "bilibili") lines.push("来源：" + result.platform);
    if (result.status) lines.push("状态：" + statusText(result.status));
    if (result.track_lan || result.provider) {
      lines.push("字幕轨：" + (result.track_lan || "-") + " / " + (result.provider || "-"));
    }
    if (result.memes_total !== undefined) {
      lines.push(
        "抽取 " + result.memes_total + " 条 · 新增 " + (result.memes_created || 0) +
        " · 冲突 " + (result.conflicts || 0) + " · 待审 " + (result.reviews || 0)
      );
    }
    if (result.total !== undefined) {
      lines.push(
        "共 " + result.total + " 条 · 成功 " + (result.done || 0) + " · 失败 " +
        (result.failed || 0) + " · 跳过 " + (result.skipped || 0)
      );
      if (result.aborted) lines.push("已中止：" + errorText(result.aborted));
    }
    (result.notes || []).forEach((note) => lines.push("· " + note));
    box.textContent = lines.join("\n");
    return box;
  }

  async function loadTaskList(boxId, kinds) {
    const box = $(boxId);
    try {
      const tasks = await call("GET", "tasks", { limit: 30, kind: "" });
      const items = (tasks || []).filter((task) => !kinds || kinds.includes(task.kind));
      if (!items.length) {
        emptyState(
          box,
          "还没有任务",
          kinds && kinds.includes("collection")
            ? "在上方填入合集链接后点「加入队列」"
            : "在上方输入视频/网页链接后点「开始解析」"
        );
        return false;
      }
      box.className = "task-list";
      box.innerHTML = "";
      items.forEach((task) => box.appendChild(taskRow(task, {})));
      return items.some((task) => task.status === "queued" || task.status === "running");
    } catch (err) {
      errorState(box, "任务列表加载失败", err.message || "");
      return false;
    }
  }

  const TASK_VIEWS = {
    parse: { box: "task-list", kinds: ["video", "url"] },
    collection: { box: "col-task-list", kinds: ["collection"] },
  };

  let taskTimer = null;
  function refreshVisibleTasks() {
    const view = TASK_VIEWS[currentTab];
    return view ? loadTaskList(view.box, view.kinds) : Promise.resolve(false);
  }

  function ensureTaskPolling() {
    if (taskTimer) return;
    taskTimer = setInterval(async () => {
      if (document.hidden) return;
      const active = await refreshVisibleTasks();
      if (!active && !["parse", "collection"].includes(currentTab)) {
        clearInterval(taskTimer);
        taskTimer = null;
      }
    }, 2000);
  }

  /* ---------------------------------------------------------------- 概览 */

  function statCard(label, value, hint) {
    const card = el("div", "stat");
    card.appendChild(el("div", "stat-value", String(value)));
    card.appendChild(el("div", "stat-label", label));
    if (hint) card.appendChild(el("div", "stat-hint", hint));
    return card;
  }

  function renderAuth(auth) {
    const dot = $("auth-dot");
    const state = auth.status || "unknown";
    const logged = !!auth.logged_in;
    dot.className = "dot " + (logged ? "on" : state === "unknown" ? "pending" : "off");
    $("auth-text").textContent = logged
      ? auth.uname || String(auth.mid || "") || "已登录"
      : state === "unknown"
        ? "正在校验登录态…"
        : state === "expired"
          ? "登录已失效"
          : "未登录 B 站";

    const desc = $("auth-desc");
    if (logged) {
      desc.textContent =
        "已登录：" + (auth.uname || "") + (auth.mid ? "（" + auth.mid + "）" : "") +
        "，字幕与合集接口会带登录态。" +
        (auth.last_ok_at ? " 最近校验 " + fmtTime(auth.last_ok_at) : "");
    } else if (state === "expired") {
      desc.textContent = "登录态已失效（Cookie 过期或被踢下线），请重新扫码登录。";
    } else if (state === "unknown") {
      desc.textContent = "已恢复本地 Cookie，正在向 B 站确认登录态…";
    } else {
      desc.textContent = "未登录。扫码登录后才能读取 CC/AI 字幕（AI 字幕必须登录）。";
    }
    if (auth.masked) $("cookie-input").placeholder = auth.masked;
  }

  function renderMemewiki(info) {
    const box = $("memewiki-box");
    const mode = info.mode || "unavailable";
    const labels = {
      inprocess: "已连接「梗百科」（进程内写入，新增词条自动重建索引）",
      export: "未连接「梗百科」，可导出 JSONL 手动导入",
      unavailable: "未检测到「梗百科」插件",
    };
    box.className = "muted";
    box.textContent =
      (labels[mode] || mode) + (info.pending !== undefined ? " · 待同步 " + info.pending + " 条" : "");
  }

  function renderProviders(providers, settings) {
    const box = $("provider-box");
    box.innerHTML = "";
    if (!providers.length) {
      box.className = "banner warn";
      box.textContent = "AstrBot 尚未配置可用的对话模型，请先到「服务提供商」添加后再解析。";
      return;
    }
    box.className = "";
    const list = el("div", "chips");
    providers.forEach((p) => {
      const chip = el(
        "span",
        "chip" + (settings.provider_id === p.id ? " active" : ""),
        (p.model || p.id) + (p.type ? " · " + p.type : "")
      );
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
    const items = (tasks || []).slice(0, 8);
    if (!items.length) {
      emptyState(box, "还没有任务", "到「单条解析」粘贴链接开始");
      return;
    }
    box.className = "";
    items.forEach((task) => box.appendChild(taskRow(task, { compact: true })));
  }

  function renderWarnings(warnings) {
    const text = warnings && warnings.length ? "配置提醒：" + warnings.join("；") : "";
    ["ov-warn", "settings-warn"].forEach((id) => {
      const box = $(id);
      if (!box) return;
      box.hidden = !text;
      box.textContent = text;
    });
  }

  loaders.overview = async function () {
    const cards = $("stat-cards");
    if (!cards.childElementCount) cards.appendChild(el("div", "muted small", "加载中…"));
    try {
      const data = await call("GET", "status", {});
      const stats = data.stats || {};
      cards.innerHTML = "";
      cards.appendChild(statCard("词条", stats.memes || 0, "published + draft"));
      cards.appendChild(statCard("视频/网页", stats.videos || 0));
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
      renderTemp(data.temp || {});
      renderWarnings(data.settings_warnings || []);
    } catch (err) {
      if (!cards.childElementCount) {
        cards.appendChild(el("div", "banner error", "概览加载失败：" + (err.message || "")));
      }
    }
  };

  $("btn-sync").addEventListener("click", async () => {
    const btn = $("btn-sync");
    btn.disabled = true;
    try {
      const data = await call("POST", "memewiki/sync", { limit: 200 });
      const pieces = [];
      if (data.synced) pieces.push("新增 " + data.synced);
      if (data.existed) pieces.push("已存在 " + data.existed);
      if (data.skipped) pieces.push("跳过 " + data.skipped);
      toast(pieces.length ? "同步完成：" + pieces.join("，") : "没有需要同步的词条", "ok");
      loaders.overview();
    } catch (err) {
      /* toast 已提示 */
    } finally {
      btn.disabled = false;
    }
  });

  $("btn-export").addEventListener("click", async () => {
    try {
      if (B && typeof B.download === "function") {
        await B.download("memewiki/export", { limit: 500 }, "memewiki.jsonl");
        toast("开始下载 JSONL", "ok");
      } else {
        toast("当前 AstrBot 版本不支持页面内下载，请直接访问插件接口", "error");
      }
    } catch (err) {
      toast((err && err.message) || "导出失败", "error");
    }
  });

  /* ---------------------------------------------------------------- 登录 */

  const qr = { timer: null, ticker: null, key: "", deadline: 0, fails: 0 };

  function clearQrBox() {
    $("qr-box").innerHTML = "";
    $("qr-link").innerHTML = "";
    $("qr-count").textContent = "";
  }

  function stopQr(message, kind) {
    if (qr.timer) clearInterval(qr.timer);
    if (qr.ticker) clearInterval(qr.ticker);
    qr.timer = null;
    qr.ticker = null;
    if (message) toast(message, kind || "");
  }

  function updateCountdown() {
    const left = Math.max(0, Math.ceil((qr.deadline - Date.now()) / 1000));
    $("qr-count").textContent = left > 0 ? "剩余 " + left + " 秒" : "";
    if (left <= 0 && qr.timer) {
      stopQr("二维码已过期，请重新获取", "error");
      $("qr-hint").textContent = "二维码已过期";
      clearQrBox();
    }
  }

  $("btn-qr").addEventListener("click", async () => {
    stopQr();
    qr.fails = 0;
    $("qr-wrap").hidden = false;
    $("qr-hint").textContent = "正在获取二维码…";
    clearQrBox();
    try {
      const data = await call("POST", "auth/qrcode", {});
      const url = data.url || "";
      const box = $("qr-box");
      box.innerHTML = "";
      if (window.qrcode) {
        const qrCode = window.qrcode(0, "M");
        qrCode.addData(url);
        qrCode.make();
        box.innerHTML = qrCode.createSvgTag({ cellSize: 4, margin: 6 });
      }
      const anchor = el("a", "", url ? "在浏览器打开登录页" : "");
      anchor.href = url;
      anchor.target = "_blank";
      anchor.rel = "noreferrer";
      $("qr-link").appendChild(anchor);
      $("qr-hint").textContent = "请使用 B 站手机客户端扫码";
      qr.key = data.qrcode_key;
      qr.deadline = Date.now() + (Number(data.expires_in) || 180) * 1000;
      updateCountdown();
      qr.ticker = setInterval(updateCountdown, 1000);
      qr.timer = setInterval(async () => {
        try {
          const poll = await call("GET", "auth/poll", { key: qr.key });
          qr.fails = 0;
          $("qr-hint").textContent = poll.message || "等待扫码…";
          if (poll.status === "ok") {
            stopQr("登录成功", "ok");
            $("qr-wrap").hidden = true;
            clearQrBox();
            loaders.overview();
          } else if (poll.status === "expired" || poll.status === "error") {
            stopQr(poll.message || "二维码已失效", "error");
            $("qr-hint").textContent = poll.message || "二维码已失效";
            clearQrBox();
          }
        } catch (err) {
          qr.fails += 1;
          if (qr.fails >= 3) {
            stopQr("网络异常，已暂停轮询；可点「检查登录态」确认结果", "error");
          }
        }
      }, 2000);
    } catch (err) {
      $("qr-hint").textContent = "二维码获取失败，请稍后重试";
    }
  });

  $("btn-qr-cancel").addEventListener("click", () => {
    stopQr();
    $("qr-wrap").hidden = true;
    clearQrBox();
  });

  $("btn-auth-refresh").addEventListener("click", async () => {
    const btn = $("btn-auth-refresh");
    btn.disabled = true;
    try {
      const data = await call("GET", "auth/status", { verify: 1 });
      renderAuth(data);
      toast(data.logged_in ? "登录态有效" : "当前未登录或已失效", data.logged_in ? "ok" : "error");
    } catch (err) {
      /* toast 已提示 */
    } finally {
      btn.disabled = false;
    }
  });

  $("btn-logout").addEventListener("click", async () => {
    if (!confirm("确定退出登录？本地保存的 Cookie 会被删除。")) return;
    try {
      await call("POST", "auth/logout", {});
      stopQr();
      $("qr-wrap").hidden = true;
      clearQrBox();
      await loaders.overview();
      toast("已退出登录");
    } catch (err) {
      /* toast 已提示 */
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
      toast("Cookie 已校验并保存", "ok");
    } catch (err) {
      /* toast 已提示 */
    }
  });

  let authTimer = null;
  function ensureAuthPolling() {
    if (authTimer) return;
    authTimer = setInterval(async () => {
      if (document.hidden || currentTab !== "overview") return;
      try {
        const data = await call("GET", "auth/status", {});
        renderAuth(data);
      } catch (err) {
        /* 静默 */
      }
    }, 45000);
  }

  /* ---------------------------------------------------------------- 解析 */

  $("btn-parse").addEventListener("click", async () => {
    const url = $("parse-url").value.trim();
    const page = Math.max(1, parseInt($("parse-page").value, 10) || 1);
    if (!url) {
      toast("请输入视频链接或 BV 号", "error");
      return;
    }
    const btn = $("btn-parse");
    btn.disabled = true;
    try {
      const data = await call("POST", "parse", {
        url,
        page,
        force: $("parse-force").checked,
      });
      toast("已加入队列：" + (data.target || url) + (data.replaced ? "（已清除同一链接的旧进度）" : ""), "ok");
      if (currentTab !== "parse") switchTab("parse");
      await refreshVisibleTasks();
      ensureTaskPolling();
    } catch (err) {
      /* toast 已提示 */
    } finally {
      btn.disabled = false;
    }
  });

  $("parse-url").addEventListener("keydown", (event) => {
    if (event.key === "Enter") $("btn-parse").click();
  });

  $("btn-refresh-tasks").addEventListener("click", async () => {
    const active = await refreshVisibleTasks();
    if (active) ensureTaskPolling();
  });

  $("btn-collection").addEventListener("click", async () => {
    const url = $("col-url").value.trim();
    if (!url) {
      toast("请输入合集链接", "error");
      return;
    }
    const btn = $("btn-collection");
    btn.disabled = true;
    try {
      const data = await call("POST", "collection", { url, force: $("col-force").checked });
      toast("合集已加入队列：" + (data.target || url) + (data.replaced ? "（已清除同一链接的旧进度）" : ""), "ok");
      if (currentTab !== "collection") switchTab("collection");
      await refreshVisibleTasks();
      ensureTaskPolling();
    } catch (err) {
      /* toast 已提示 */
    } finally {
      btn.disabled = false;
    }
  });

  loaders.parse = () => loadTaskList("task-list", ["video", "url"]).then((active) => {
    if (active) ensureTaskPolling();
  });
  loaders.collection = () => loadTaskList("col-task-list", ["collection"]).then((active) => {
    if (active) ensureTaskPolling();
  });

  /* ---------------------------------------------------------------- 词条 */

  let memeOffset = 0;
  const MEME_PAGE = 20;
  let memeItems = [];

  async function loadMemes(reset) {
    if (reset) {
      memeOffset = 0;
      memeItems = [];
      loading($("meme-table"));
    }
    const query = $("meme-query").value.trim();
    const status = $("meme-status").value;
    const source = $("meme-source") ? $("meme-source").value : "";
    try {
      const data = await call("GET", "memes", {
        query,
        status,
        source,
        limit: MEME_PAGE,
        offset: memeOffset,
      });
      memeItems = memeItems.concat(data.items || []);
      memeOffset = memeItems.length;
      $("meme-count").textContent = "共 " + (data.total || 0) + " 条，已显示 " + memeItems.length + " 条";
      if (!memeItems.length) {
        const box = $("meme-table");
        box.innerHTML = "";
        box.className = "empty";
        box.textContent = query || status ? "没有匹配的词条" : "词条库还是空的，先去解析一个视频吧";
      } else {
        renderMemes();
      }
      $("btn-meme-more").hidden = memeItems.length >= (data.total || 0);
    } catch (err) {
      errorState($("meme-table"), "词条加载失败", err.message || "");
    }
  }

  async function loadSourceOptions() {
    const select = $("meme-source");
    if (!select) return;
    try {
      const data = await call("GET", "sources", { limit: 200 });
      const current = select.value;
      select.innerHTML = '<option value="">全部来源</option>';
      (data || []).forEach((item) => {
        const option = el("option", "", (item.source_label || item.url || "?") + "（" + (item.memes || 0) + "）");
        option.value = item.source_label || "";
        select.appendChild(option);
      });
      if (current) select.value = current;
    } catch (err) {
      /* 下拉加载失败不阻塞主列表 */
    }
  }

  function renderTemp(temp) {
    const box = $("temp-box");
    if (!box) return;
    const info = temp || {};
    const bytes = Number(info.bytes || 0);
    if (!info.files) {
      box.textContent = "字幕临时文件：暂无";
      return;
    }
    const size = bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(bytes / 1024)) + " KB";
    box.textContent = "字幕临时文件：" + info.files + " 个 · " + size + " · 最旧 " + (info.oldest || "-");
  }

  function renderMemes() {
    const table = $("meme-table");
    table.className = "";
    table.innerHTML =
      "<thead><tr><th>词条</th><th>释义</th><th>来源</th><th>热度</th><th>置信</th><th>状态</th><th>同步</th><th></th></tr></thead>";
    const body = el("tbody");
    memeItems.forEach((meme, index) => {
      const tr = el("tr");
      tr.appendChild(el("td", "term", meme.term));
      const def = el("td", "def", meme.definition || "");
      def.title = meme.definition || "";
      tr.appendChild(def);
      const src = el("td", "small", meme.source_label || "—");
      src.title = meme.origin || "";
      tr.appendChild(src);
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
          toast("已删除", "ok");
        } catch (err) {
          /* toast 已提示 */
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
      "<h3>编辑词条：" + esc(meme.term) + "</h3>" +
      '<label class="lbl">释义</label><textarea name="definition" rows="3">' + esc(meme.definition || "") + "</textarea>" +
      '<label class="lbl">用法</label><textarea name="usage" rows="2">' + esc(meme.usage || "") + "</textarea>" +
      '<label class="lbl">出处</label><textarea name="origin" rows="2">' + esc(meme.origin || "") + "</textarea>" +
      '<label class="lbl">状态</label><select name="status">' +
      ["published", "draft", "archived"]
        .map((s) => '<option value="' + s + '"' + (meme.status === s ? " selected" : "") + ">" + s + "</option>")
        .join("") +
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
  $("meme-status").addEventListener("change", () => loadMemes(true));
  $("btn-meme-more").addEventListener("click", () => loadMemes(false));
  loaders.memes = async () => {
    loadSourceOptions();
    await loadMemes(true);
  };

  $("meme-source").addEventListener("change", () => loadMemes(true));
  $("btn-temp-clean").addEventListener("click", async () => {
    try {
      const data = await call("POST", "temp/clean", {});
      toast("已清理 " + (data.removed || 0) + " 个过期文件（保留 " + (data.days || 3) + " 天）", "ok");
      renderTemp(data);
    } catch (err) {
      /* toast 已提示 */
    }
  });

  /* ---------------------------------------------------------------- 审核 */

  async function loadReviews() {
    const box = $("review-list");
    loading(box);
    try {
      const data = await call("GET", "reviews", { status: "pending", limit: 50 });
      const items = data.items || [];
      if (!items.length) {
        emptyState(box, "暂无待审核项", "新词条与已有词条定义冲突时会出现在这里");
        return;
      }
      box.className = "";
      box.innerHTML = "";
      items.forEach((review) => {
        const card = el("div", "review");
        card.appendChild(
          el("div", "review-reason", (review.kind || "冲突") + " · " + (review.reason || ""))
        );
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
      errorState(box, "审核列表加载失败", err.message || "");
    }
  }

  async function resolveReview(id, action) {
    try {
      await call("POST", "review/resolve", { id, action });
      await loadReviews();
      loaders.overview();
      toast(action === "approve" ? "已采纳" : "已忽略", "ok");
    } catch (err) {
      /* toast 已提示 */
    }
  }

  $("btn-review-refresh").addEventListener("click", loadReviews);
  loaders.reviews = loadReviews;

  /* ---------------------------------------------------------------- 黑话 */

  async function loadSlangs() {
    const scope = $("slang-scope").value.trim();
    const box = $("slang-list");
    if (!scope) {
      toast("请先填写作用域，如 group:123456789", "error");
      return;
    }
    localStorage.setItem("mh_scope", scope);
    loading(box);
    try {
      const items = await call("GET", "slangs", { scope });
      if (!items.length) {
        emptyState(box, "该作用域暂无黑话", "在下方「新增 / 覆盖」里添加");
        return;
      }
      box.className = "";
      box.innerHTML = "";
      items.forEach((item) => {
        const row = el("div", "slang");
        row.appendChild(el("span", "term", item.term));
        row.appendChild(el("span", "def", item.definition || ""));
        const del = el("button", "btn tiny danger", "删除");
        del.addEventListener("click", async () => {
          if (!confirm("删除黑话「" + item.term + "」？")) return;
          try {
            await call("POST", "slang/delete", { scope, term: item.term });
            await loadSlangs();
          } catch (err) {
            /* toast 已提示 */
          }
        });
        row.appendChild(del);
        box.appendChild(row);
      });
    } catch (err) {
      errorState(box, "群黑话加载失败", err.message || "");
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
      /* toast 已提示 */
    }
  });

  loaders.slangs = () => {
    const saved = localStorage.getItem("mh_scope");
    if (saved && !$("slang-scope").value) $("slang-scope").value = saved;
    if ($("slang-scope").value) loadSlangs();
    else emptyState($("slang-list"), "先填写作用域", "例如 group:123456789，然后点「加载」");
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
    risk_cooldown: { label: "风控熔断时长（秒）", type: "float", hint: "触发 412 后暂停请求的时间，默认 1800" },
    user_agent: { label: "自定义 User-Agent", type: "str", hint: "留空使用默认浏览器 UA" },
    collection_page_size: { label: "合集每页条数", type: "int" },
    max_collection_items: { label: "合集单次上限", type: "int" },
    queue_concurrency: { label: "任务并发", type: "int" },
    raw_retention_days: { label: "原始字幕保留天数", type: "int" },
    temp_retention_days: { label: "字幕临时文件保留天数", type: "int", hint: "默认 3 天，到期自动清理" },
    review_required: { label: "新词条进审核", type: "bool" },
    memewiki_enabled: { label: "同步到梗百科", type: "bool" },
    memewiki_min_confidence: { label: "同步最低置信度", type: "float" },
    auto_parse_on_link: { label: "群里发链接自动解析", type: "bool" },
    inject_enabled: { label: "聊天注入梗库", type: "bool" },
    inject_max_chars: { label: "注入最大字数", type: "int" },
    media_enabled: { label: "解析其它平台视频", type: "bool", hint: "需安装可选依赖 yt-dlp" },
    transcribe_enabled: { label: "无字幕时本地转写", type: "bool", hint: "需安装可选依赖 faster-whisper" },
    whisper_model: { label: "Whisper 模型", type: "str", hint: "tiny / base / small / medium / large-v3" },
    whisper_device: { label: "Whisper 设备", type: "str", hint: "cpu / cuda / auto" },
    whisper_compute_type: { label: "Whisper 计算精度", type: "str", hint: "int8 / float16 / float32" },
    transcribe_language: { label: "转写语言", type: "str", hint: "留空自动识别，如 zh / en" },
    asr_deps_path: { label: "faster-whisper 依赖目录", type: "str", hint: "可选：pip install --target 到此目录，避免改动 AstrBot 自带 Python" },
    media_max_duration: { label: "视频时长上限（秒）", type: "int" },
    transcribe_timeout: { label: "转写超时（秒）", type: "int" },
    download_timeout: { label: "音视频下载超时（秒）", type: "int" },
    ytdlp_cookies_file: { label: "yt-dlp Cookie 文件", type: "str", hint: "可选，Netscape 格式" },
    ytdlp_proxy: { label: "yt-dlp 代理", type: "str", hint: "如 http://127.0.0.1:7890" },
    keep_temp_files: { label: "保留临时音频", type: "bool" },
    web_enabled: { label: "解析网页链接", type: "bool" },
    web_timeout: { label: "网页抓取超时（秒）", type: "int" },
    web_max_bytes: { label: "网页最大字节数", type: "int" },
    web_min_chars: { label: "正文最少字数", type: "int" },
    web_cookies: { label: "网页抓取 Cookie", type: "str", hint: "可选；萌娘百科等站点 403 时填写" },
    max_source_chars: { label: "文本源最大字数", type: "int" },
  };

  const SETTING_GROUPS = [
    {
      title: "模型与抽取",
      keys: ["provider_id", "json_mode", "temperature", "max_tokens", "llm_timeout", "chunk_chars", "max_chunks", "max_source_chars"],
    },
    {
      title: "多平台视频与本地转写",
      keys: ["media_enabled", "transcribe_enabled", "whisper_model", "whisper_device", "whisper_compute_type", "transcribe_language", "asr_deps_path", "media_max_duration", "transcribe_timeout", "download_timeout", "ytdlp_cookies_file", "ytdlp_proxy", "keep_temp_files"],
    },
    {
      title: "网页抓取",
      keys: ["web_enabled", "web_timeout", "web_max_bytes", "web_min_chars", "web_cookies"],
    },
    {
      title: "B 站采集与队列",
      keys: ["request_interval", "risk_cooldown", "user_agent", "collection_page_size", "max_collection_items", "queue_concurrency", "raw_retention_days", "temp_retention_days"],
    },
    {
      title: "入库、检索与同步",
      keys: ["review_required", "memewiki_enabled", "memewiki_min_confidence", "inject_enabled", "inject_max_chars", "auto_parse_on_link"],
    },
  ];

  function settingField(key, value, providers) {
    const meta = SETTING_META[key] || { label: key, type: "str" };
    const row = el("div", "field");
    row.appendChild(el("label", "lbl", meta.label));
    let input;
    if (meta.type === "bool") {
      input = el("input");
      input.type = "checkbox";
      input.checked = !!value;
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
      input.value = value || "";
    } else if (meta.type === "int" || meta.type === "float") {
      input = el("input");
      input.type = "number";
      input.step = meta.type === "int" ? "1" : "0.1";
      input.value = value;
    } else {
      input = el("input");
      input.type = "text";
      input.value = value || "";
    }
    input.dataset.key = key;
    input.dataset.type = meta.type;
    row.appendChild(input);
    if (meta.hint) row.appendChild(el("div", "muted small", meta.hint));
    return row;
  }

  async function loadSettings() {
    const form = $("settings-form");
    form.innerHTML = "";
    form.appendChild(el("div", "muted small", "加载中…"));
    try {
      const [settings, providers] = await Promise.all([
        call("GET", "settings", {}),
        call("GET", "providers", {}).catch(() => []),
      ]);
      form.innerHTML = "";
      const used = new Set();
      SETTING_GROUPS.forEach((group) => {
        const keys = group.keys.filter((key) => key in settings);
        if (!keys.length) return;
        keys.forEach((key) => used.add(key));
        const details = el("details", "group");
        details.open = true;
        details.appendChild(el("summary", "", group.title + "（" + keys.length + " 项）"));
        const body = el("div", "group-body");
        keys.forEach((key) => body.appendChild(settingField(key, settings[key], providers)));
        details.appendChild(body);
        form.appendChild(details);
      });
      const rest = Object.keys(settings).filter((key) => !used.has(key));
      if (rest.length) {
        const details = el("details", "group");
        details.appendChild(el("summary", "", "其它（" + rest.length + " 项）"));
        const body = el("div", "group-body");
        rest.forEach((key) => body.appendChild(settingField(key, settings[key], providers)));
        details.appendChild(body);
        form.appendChild(details);
      }
      renderWarnings([]);
    } catch (err) {
      errorState(form, "设置加载失败", err.message || "");
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
    const btn = $("btn-settings-save");
    btn.disabled = true;
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
      /* toast 已提示 */
    } finally {
      btn.disabled = false;
    }
  });

  loaders.settings = loadSettings;

  /* ---------------------------------------------------------------- 启动 */

  (async function boot() {
    try {
      pageContext = (await readyPromise) || {};
    } catch (err) {
      pageContext = {};
    }
    try {
      await loaders.overview();
      ensureAuthPolling();
    } catch (err) {
      /* 概览已提示 */
    }
  })();
})();
