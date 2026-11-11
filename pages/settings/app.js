/* GitHub Release 加速下载 · 参数设置页面 */
(function () {
  "use strict";

  const PLUGIN = "astrbot_plugin_ghfetch";
  /* 插件名由宿主自动拼在 /api/v1/plugins/extensions/<plugin>/ 后面，这里只写路由尾段 */
  const API_CONFIG = "/config";
  const API_SAVE = "/config/save";
  const API_STATUS = "/status";

  const GROUPS = [
    {
      title: "基础开关",
      desc: "决定这个插件在哪些会话里干活",
      keys: ["enabled", "allow_group", "allow_private", "whitelist_groups"],
    },
    {
      title: "下载与加速",
      desc: "aria2 是加速主力，并发数越大越吃带宽",
      keys: ["aria2_path", "connections", "max_concurrent", "max_size_mb", "mirrors"],
    },
    {
      title: "回传与清理",
      desc: "文件下载完要交给 SnowLuma 上传，这一组是那段链路",
      keys: ["http_port", "callback_host", "clean_delay"],
    },
    {
      title: "识别与选择",
      desc: "决定「下载 xxx」时挑哪个项目、哪个安装包",
      keys: [
        "win_prefer",
        "ask_on_ambiguous",
        "search_candidates",
        "asset_candidates",
        "aliases",
        "pending_ttl",
      ],
    },
    {
      title: "仓库简介",
      desc: "发裸仓库主页链接时，先发一条简介和一句话总结",
      keys: ["repo_intro", "repo_intro_llm", "repo_intro_readme_chars", "info_cache_ttl"],
    },
    {
      title: "GitHub API",
      desc: "匿名请求每小时只有 60 次，填个 token 能到 5000 次",
      keys: ["github_token"],
    },
  ];

  /** 需要重启才生效的键（后端也会返回，这里做兜底） */
  let restartKeys = ["http_port", "callback_host", "aria2_path", "max_concurrent", "enabled"];

  let schema = {};
  let values = {};
  let baseline = "";
  let bridge = null;

  const el = (id) => document.getElementById(id);

  function ready() {
    return new Promise((resolve) => {
      const started = Date.now();
      const timer = setInterval(() => {
        const sdk = window.AstrBotPluginView || window.AstrBotPluginPage;
        if (sdk || Date.now() - started > 5000) {
          clearInterval(timer);
          resolve(sdk || null);
        }
      }, 50);
    });
  }

  function apiGet(endpoint) {
    return bridge.apiGet(endpoint, {});
  }

  function apiPost(endpoint, body) {
    return bridge.apiPost(endpoint, body);
  }

  function setTheme(isDark) {
    document.body.classList.toggle("dark", !!isDark);
  }

  /* ---------------- 渲染 ---------------- */

  function inputFor(key, spec) {
    const type = spec.type || "string";
    const value = values[key];

    if (type === "bool") {
      const row = document.createElement("div");
      row.className = "switch-row";
      const label = document.createElement("label");
      label.className = "switch";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = !!value;
      input.dataset.key = key;
      input.dataset.type = "bool";
      const slider = document.createElement("span");
      label.appendChild(input);
      label.appendChild(slider);
      const text = document.createElement("span");
      text.className = "switch-label";
      text.textContent = value ? "已开启" : "已关闭";
      input.addEventListener("change", () => {
        text.textContent = input.checked ? "已开启" : "已关闭";
        markDirty();
      });
      row.appendChild(label);
      row.appendChild(text);
      return row;
    }

    if (type === "int") {
      const input = document.createElement("input");
      input.type = "number";
      input.value = value === null || value === undefined ? "" : String(value);
      input.dataset.key = key;
      input.dataset.type = "int";
      input.addEventListener("input", markDirty);
      return input;
    }

    if (type === "list") {
      const wrap = document.createElement("div");
      const area = document.createElement("textarea");
      const list = Array.isArray(value) ? value : [];
      area.value = list.join("\n");
      area.rows = Math.min(6, Math.max(3, list.length + 1));
      area.dataset.key = key;
      area.dataset.type = "list";
      area.placeholder = "每行一项，空行表示留空";
      area.addEventListener("input", markDirty);
      wrap.appendChild(area);
      const hint = document.createElement("div");
      hint.className = "field-desc";
      hint.textContent =
        key === "mirrors"
          ? "每行一个前缀；空行代表 GitHub 直连，按顺序逐个尝试"
          : key === "whitelist_groups"
            ? "每行一个群号；全部删空表示不限制"
            : "每行一项";
      wrap.appendChild(hint);
      return wrap;
    }

    if (key === "github_token") {
      const wrap = document.createElement("div");
      wrap.className = "pwd-wrap";
      const input = document.createElement("input");
      input.type = "password";
      input.value = value === null || value === undefined ? "" : String(value);
      input.placeholder = "ghp_xxx 或 github_pat_xxx，留空则匿名请求";
      input.dataset.key = key;
      input.dataset.type = "string";
      input.addEventListener("input", markDirty);
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "btn";
      toggle.textContent = "显示";
      toggle.addEventListener("click", () => {
        const show = input.type === "password";
        input.type = show ? "text" : "password";
        toggle.textContent = show ? "隐藏" : "显示";
      });
      wrap.appendChild(input);
      wrap.appendChild(toggle);
      return wrap;
    }

    const input = document.createElement("input");
    input.type = "text";
    input.value = value === null || value === undefined ? "" : String(value);
    input.dataset.key = key;
    input.dataset.type = "string";
    input.addEventListener("input", markDirty);
    return input;
  }

  function fieldRow(key, spec) {
    const row = document.createElement("div");
    row.className = "field";

    const label = document.createElement("div");
    label.className = "field-label";
    const name = document.createElement("div");
    name.className = "field-name";
    name.textContent = key;
    label.appendChild(name);

    const keyNode = document.createElement("span");
    keyNode.className = "field-key";
    keyNode.textContent = "默认：" + JSON.stringify(spec.default);
    label.appendChild(keyNode);

    if (restartKeys.includes(key)) {
      const badge = document.createElement("span");
      badge.className = "badge-restart";
      badge.textContent = "改动需重启";
      name.appendChild(badge);
    }

    const control = document.createElement("div");
    control.className = "field-control";
    control.appendChild(inputFor(key, spec));

    if (spec.description && key !== "mirrors" && key !== "whitelist_groups") {
      const desc = document.createElement("div");
      desc.className = "field-desc";
      desc.textContent = spec.description;
      control.appendChild(desc);
    }

    row.appendChild(label);
    row.appendChild(control);
    return row;
  }

  function render() {
    const host = el("groups");
    host.innerHTML = "";
    const used = new Set();

    const layout = GROUPS.map((group) => ({
      ...group,
      keys: group.keys.filter((k) => schema[k]),
    })).filter((g) => g.keys.length);

    layout.forEach((g) => g.keys.forEach((k) => used.add(k)));

    const rest = Object.keys(schema).filter((k) => !used.has(k));
    if (rest.length) {
      layout.push({
        title: "其他参数",
        desc: "配置表里还有这些，先放在这里",
        keys: rest,
      });
    }

    if (!layout.length) {
      host.innerHTML = '<div class="empty">配置项是空的</div>';
      return;
    }

    layout.forEach((group) => {
      const card = document.createElement("section");
      card.className = "card";
      const h2 = document.createElement("h2");
      h2.textContent = group.title;
      card.appendChild(h2);
      if (group.desc) {
        const p = document.createElement("p");
        p.className = "card-desc";
        p.textContent = group.desc;
        card.appendChild(p);
      }
      group.keys.forEach((key) => card.appendChild(fieldRow(key, schema[key])));
      host.appendChild(card);
    });

    baseline = snapshot();
    markDirty(true);
  }

  /* ---------------- 取值 ---------------- */

  function readForm() {
    const out = {};
    document.querySelectorAll("[data-key]").forEach((node) => {
      const key = node.dataset.key;
      const type = node.dataset.type;
      if (type === "bool") out[key] = !!node.checked;
      else if (type === "int") out[key] = node.value === "" ? 0 : Number(node.value);
      else if (type === "list") out[key] = node.value.split("\n").map((line) => line.trim());
      else out[key] = node.value;
    });
    return out;
  }

  function snapshot() {
    return JSON.stringify(readForm());
  }

  function markDirty(silent) {
    const dirty = snapshot() !== baseline;
    el("saveBtn").disabled = !dirty;
    const tip = el("dirtyTip");
    tip.textContent = dirty ? "有未保存的修改" : "没有改动";
    tip.classList.toggle("dirty", dirty);
    if (!silent) {
      const saveTip = el("saveTip");
      if (saveTip.classList.contains("ok")) {
        saveTip.textContent = "";
        saveTip.className = "save-tip";
      }
    }
  }

  /* ---------------- 状态条 ---------------- */

  function renderStatus(data) {
    const aria2 = el("aria2Chip");
    aria2.textContent = data.aria2_ready ? "aria2 就绪" : "aria2 未找到";
    aria2.className = "chip " + (data.aria2_ready ? "good" : "bad");

    const port = el("portChip");
    port.textContent = data.http_port ? "回传端口 " + data.http_port : "回传服务未启动";

    const left = el("leftoverChip");
    const mb = (data.leftover_size || 0) / 1024 / 1024;
    left.textContent =
      "残留 " + (data.leftover_files || 0) + " 个 / " + mb.toFixed(1) + "MB";
    left.className = "chip " + (data.leftover_files ? "bad" : "");
  }

  async function loadStatus() {
    try {
      const data = await apiGet(API_STATUS);
      if (data) renderStatus(data);
    } catch (err) {
      console.warn("status failed", err);
    }
  }

  /* ---------------- 主流程 ---------------- */

  async function loadConfig(keepTip) {
    const host = el("groups");
    if (!keepTip) host.innerHTML = '<div class="loading">正在读取配置…</div>';
    const data = await apiGet(API_CONFIG);
    if (!data || !data.schema) throw new Error("后端没有返回配置定义");
    schema = data.schema;
    values = data.values || {};
    if (data.meta && Array.isArray(data.meta.restart_keys)) {
      restartKeys = data.meta.restart_keys;
    }
    render();
    if (!keepTip) {
      const tip = el("saveTip");
      tip.textContent = "已读取 " + Object.keys(schema).length + " 项参数";
      tip.className = "save-tip";
    }
  }

  async function save() {
    const tip = el("saveTip");
    const btn = el("saveBtn");
    btn.disabled = true;
    tip.textContent = "正在保存…";
    tip.className = "save-tip";
    try {
      const form = readForm();
      const result = await apiPost(API_SAVE, { values: form });
      values = (result && result.values) || form;
      baseline = snapshot();
      const need = (result && result.restart_needed) || [];
      tip.textContent =
        "已保存喵～" + (need.length ? "（" + need.join("、") + " 要重启插件才生效）" : "");
      tip.className = "save-tip ok";
      markDirty(true);
      btn.disabled = false;
      loadStatus();
    } catch (err) {
      tip.textContent = "保存失败：" + (err && err.message ? err.message : err);
      tip.className = "save-tip err";
      btn.disabled = false;
    }
  }

  function applyDefaults() {
    Object.keys(schema).forEach((key) => {
      values[key] = schema[key].default;
    });
    render();
    const tip = el("saveTip");
    tip.textContent = "已把表单填回默认值，点保存才会真正写盘";
    tip.className = "save-tip";
  }

  async function boot() {
    bridge = await ready();
    if (bridge) {
      try {
        const ctx = bridge.getContext ? bridge.getContext() || {} : {};
        setTheme(ctx.isDark);
        if (bridge.onContext) {
          bridge.onContext((next) => setTheme(next && next.isDark));
        }
      } catch (err) {
        console.warn(err);
      }
    }

    el("reloadBtn").addEventListener("click", async () => {
      try {
        await loadConfig(false);
      } catch (err) {
        el("groups").innerHTML = '<div class="empty">读取失败：' + err.message + "</div>";
      }
    });
    el("defaultsBtn").addEventListener("click", applyDefaults);
    el("saveBtn").addEventListener("click", save);

    try {
      await loadConfig(false);
      loadStatus();
    } catch (err) {
      el("groups").innerHTML =
        '<div class="empty">读取失败：' + (err && err.message ? err.message : err) + "</div>";
    }
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
