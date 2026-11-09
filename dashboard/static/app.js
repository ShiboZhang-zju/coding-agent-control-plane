/* UI-0/UI-1 dashboard — vanilla JS, no build step, no third-party code.
 *
 * Security rule of this file: every value that came from the ledger is
 * inserted with `textContent` (via `el()`), never with `innerHTML`. The only
 * `innerHTML` assignment in this file is the empty-string clear in `clear()`.
 *
 * Data rule: this file never computes project state. It renders what
 * /api/overview and /api/projects/{id} return.
 */
"use strict";

(function () {
  const REFRESH_MS = 10000;

  const state = {
    overview: null,
    detail: null,
    selectedProjectId: null,
    autoRefresh: true,
    timer: null,
    lastRefreshAt: null,
    connection: "unknown",
    ledgerError: null,
    goalFilter: "all",
    taskFilter: "all",
    loading: false,
    health: null,
    handling: null,
    publish: { confirmToken: null, projectId: null, roots: null },
    chat: {
      enabled: false,
      token: "",
      // The project of the *conversation*, as recorded by the store.  A global
      // conversation has none, and discussing a project never changes it.
      conversationProjectId: null,
      // Which project the LD resolved for the last turn (from the server's
      // routing decision, never inferred here).
      identified: null,
      // Publication target: an explicit project-level choice.
      projectId: null,
      conversationId: null,
      conversations: [],
      messages: [],
      pendingAction: null,
      sending: false,
      publishing: false,
      connected: false,
      lastIntentSource: null,
      restored: false,
    },
  };

  // ------------------------------------------------------------------ //
  // DOM helpers (the only place text enters the document)
  // ------------------------------------------------------------------ //
  function el(tag, opts, children) {
    const node = document.createElement(tag);
    const options = opts || {};
    if (options.class) node.className = options.class;
    if (options.text !== undefined && options.text !== null) {
      node.textContent = String(options.text);
    }
    if (options.title) node.title = String(options.title);
    if (options.attrs) {
      Object.keys(options.attrs).forEach(function (key) {
        const value = options.attrs[key];
        if (value === null || value === undefined || value === false) return;
        node.setAttribute(key, String(value));
      });
    }
    if (options.dataset) {
      Object.keys(options.dataset).forEach(function (key) {
        const value = options.dataset[key];
        if (value === null || value === undefined) return;
        node.dataset[key] = String(value);
      });
    }
    if (options.onClick) node.addEventListener("click", options.onClick);
    (children || []).forEach(function (child) {
      if (child === null || child === undefined) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function clear(node) {
    node.innerHTML = "";
    return node;
  }

  function $(id) {
    return document.getElementById(id);
  }

  // ------------------------------------------------------------------ //
  // formatting
  // ------------------------------------------------------------------ //
  function fmtTime(epochSeconds) {
    if (typeof epochSeconds !== "number" || !isFinite(epochSeconds)) return "—";
    const d = new Date(epochSeconds * 1000);
    if (isNaN(d.getTime())) return "—";
    const pad = function (n) { return String(n).padStart(2, "0"); };
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
      " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  function fmtRelative(epochSeconds) {
    if (typeof epochSeconds !== "number" || !isFinite(epochSeconds)) return null;
    const delta = Math.max(0, Date.now() / 1000 - epochSeconds);
    if (delta < 60) return "刚刚";
    if (delta < 3600) return Math.floor(delta / 60) + " 分钟前";
    if (delta < 86400) return Math.floor(delta / 3600) + " 小时前";
    return Math.floor(delta / 86400) + " 天前";
  }

  function num(value) {
    return typeof value === "number" ? String(value) : "0";
  }

  // ------------------------------------------------------------------ //
  // status vocabulary — text always present, colour is only a hint
  // ------------------------------------------------------------------ //
  //: 中文展示层。映射只影响显示，所有判断仍用原始枚举（见 DANGER_STATES 等）。
  const I18N = window.ACPI18N;

  /** 按语境取中文标签；未知值显示原始值 + “未知状态”。 */
  function label(kind, value) { return I18N.t(kind, value); }

  /** 该状态是否被识别（用于给未知值加提示样式/标题）。 */
  function isKnown(kind, value) { return I18N.translate(kind, value).known; }

  const DANGER_STATES = ["failed", "lost", "dispatch_unknown", "unknown", "cancelled"];
  const WARN_STATES = ["waiting_for_approval", "needs_approval", "needs_human"];

  function stateTone(value) {
    const v = String(value || "").toLowerCase();
    if (["verified", "succeeded", "completed", "active"].indexOf(v) >= 0) return "ok";
    if (DANGER_STATES.indexOf(v) >= 0 || v === "rejected") return "bad";
    if (WARN_STATES.indexOf(v) >= 0 || v === "inconclusive" || v === "waiting") return "warn";
    if (["running", "starting", "queued", "dispatching", "ready"].indexOf(v) >= 0) return "info";
    return "muted";
  }

  /**
   * 状态药丸。
   *
   * ``kind`` 是 i18n 的字段语境（taskState / verdict / goalStatus …），
   * ``prefix`` 是可选的标签前缀。原始值始终写进 ``title``，这样中文标签
   * 不会让人失去对机器状态的判断依据；未知状态额外加 ``pill-unknown``。
   */
  function pill(kind, value, prefix, suffix) {
    // kind 可以是单个语境，也可以是语境数组（键的含义不确定时用，见
    // linked_task_counts：同一个字典里既有 verdict 也有 state）。
    const info = Array.isArray(kind)
      ? I18N.translateAny(kind, value)
      : kind
        ? I18N.translate(kind, value)
        : { text: (value === null || value === undefined || value === "") ? "—" : String(value),
            known: true, raw: String(value === null || value === undefined ? "" : value) };
    const text = (prefix ? prefix + "：" + info.text : info.text) + (suffix || "");
    const cls = "mono small pill pill-" + stateTone(info.raw) +
      (info.known ? "" : " pill-unknown");
    return el("span", {
      class: cls,
      text: text,
      attrs: info.raw ? { title: info.raw } : null,
    });
  }

  function copyText(value) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(value).then(
        function () { return true; }, function () { return false; });
    }
    try {
      const area = document.createElement("textarea");
      area.value = value;
      area.setAttribute("readonly", "readonly");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(area);
      return Promise.resolve(ok);
    } catch (err) {
      return Promise.resolve(false);
    }
  }

  function copyButton(label, value) {
    if (!value) return null;
    const button = el("button", {
      class: "copy",
      text: "复制",
      title: "复制 " + label,
      attrs: { type: "button" },
    });
    button.addEventListener("click", function (event) {
      event.stopPropagation();
      copyText(String(value)).then(function (ok) {
        button.textContent = ok ? "已复制" : "复制失败";
        setTimeout(function () { button.textContent = "复制"; }, 1400);
      });
    });
    return button;
  }

  function idLine(value, prefix) {
    if (!value) return el("span", { class: "muted", text: "—" });
    return el("span", { class: "id-line" }, [
      el("code", { class: "mono small", text: value, title: (prefix || "ID") + ": " + value }),
      copyButton(prefix || "ID", value),
    ]);
  }

  // ------------------------------------------------------------------ //
  // API
  // ------------------------------------------------------------------ //
  function getJson(path) {
    return fetch(path, {
      method: "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
      credentials: "omit",
    }).then(function (response) {
      return response.json().catch(function () { return null; }).then(function (body) {
        if (!response.ok) {
          const err = new Error(
            (body && body.error && body.error.message) || ("HTTP " + response.status));
          err.code = (body && body.error && body.error.code) || ("http_" + response.status);
          err.status = response.status;
          throw err;
        }
        return body;
      });
    });
  }

  function chatJson(path, options) {
    const opts = options || {};
    const headers = { Accept: "application/json",
                      Authorization: "Bearer " + state.chat.token };
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    return fetch(path, {
      method: opts.method || "GET",
      headers: headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      cache: "no-store",
      credentials: "omit",
    }).then(function (response) {
      return response.json().catch(function () { return null; }).then(function (body) {
        if (!response.ok) {
          const err = new Error(
            (body && body.error && body.error.message) || ("HTTP " + response.status));
          err.code = (body && body.error && body.error.code) || ("http_" + response.status);
          err.status = response.status;
          throw err;
        }
        return body;
      });
    });
  }

  // ------------------------------------------------------------------ //
  // render: summary strip
  // ------------------------------------------------------------------ //
  const SUMMARY_KEYS = [
    { key: "projects", label: "项目总数", hint: "账本（Ledger）中注册的 工作项目（Project）" },
    { key: "active_goals", label: "活跃 工作目标（Goal）", hint: "状态 = 进行中（active）" },
    { key: "active_work_items", label: "进行中的 工作任务（WorkItem）",
      hint: "排队中 / 正在启动 / 运行中 / 等待审批" },
    { key: "attention_items", label: "待人工处理",
      hint: "目标待处理、待审批、验收未通过 / 结果不确定、失联" },
    { key: "rejected", label: "验收未通过（rejected）", hint: "验收器（Verifier）判定未通过" },
    { key: "inconclusive", label: "结果不确定（inconclusive）", hint: "无法判定（缺断言或无产物）" },
  ];

  function summaryCard(item, value, hint, tone) {
    return el("div", { class: "summary-card" + (tone ? " tone-" + tone : "") }, [
      el("span", { class: "summary-label", text: item.label }),
      el("strong", { class: "summary-value", text: String(value) }),
      el("span", { class: "summary-hint", text: hint }),
    ]);
  }

  function renderSummary() {
    const grid = clear($("summaryGrid"));
    if (!state.overview) {
      SUMMARY_KEYS.forEach(function (item) {
        grid.appendChild(summaryCard(item, "—", "尚未加载"));
      });
      return;
    }
    const totals = state.overview.totals || {};
    SUMMARY_KEYS.forEach(function (item) {
      let tone = "";
      const value = totals[item.key];
      if (item.key === "attention_items" && value > 0) tone = "bad";
      if (item.key === "rejected" && value > 0) tone = "bad";
      if (item.key === "inconclusive" && value > 0) tone = "warn";
      if (item.key === "active_work_items" && value > 0) tone = "info";
      grid.appendChild(summaryCard(item, num(value), item.hint, tone));
    });
    grid.appendChild(el("div", { class: "summary-extra" }, [
      el("span", { class: "muted", text: "人工介入细分：" }),
      pill("attentionFlag", "needs_human", "待人工处理"),
      pill("attentionFlag", "needs_approval", "待审批"),
      pill("attentionFlag", "dispatch_unknown", "派发状态不明"),
      pill("attentionFlag", "suspected_lost", "疑似失联"),
      pill("attentionFlag", "session_binding_conflict", "会话绑定冲突"),
    ]));
  }

  // ------------------------------------------------------------------ //
  // render: project nav
  // ------------------------------------------------------------------ //
  function renderNav() {
    const nav = clear($("projectNav"));
    const projects = state.overview ? state.overview.projects : [];
    $("projectCount").textContent = projects.length ? String(projects.length) : "";

    nav.appendChild(el("button", {
      class: "nav-item nav-all" + (state.selectedProjectId === null ? " is-active" : ""),
      attrs: { type: "button" },
      onClick: function () { selectProject(null); },
    }, [
      el("span", { class: "nav-name", text: "全部项目 · 总览" }),
      el("span", { class: "nav-meta", text: projects.length + " 个" }),
    ]));

    if (!projects.length) {
      nav.appendChild(el("p", { class: "placeholder", text: "Ledger 中还没有 Project。" }));
      return;
    }

    projects.forEach(function (project) {
      const active = project.project_id === state.selectedProjectId;
      const sub = el("span", { class: "nav-sub" }, [
        el("span", { text: I18N.count(project.task_count, "个任务") }),
        el("span", { class: "sep", text: "·" }),
        el("span", { text: I18N.count(project.goal_count, "个目标") }),
        project.attention_count > 0
          ? el("span", { class: "tag tag-bad", text: project.attention_count + " 待处理" })
          : null,
      ]);
      const meta = el("span", { class: "nav-meta" }, [
        project.active_task_count > 0
          ? el("span", { class: "dot dot-info", title: "活动任务 " + project.active_task_count })
          : null,
        project.has_blocker
          ? el("span", { class: "dot dot-bad", title: "有需要人工处理的事项" })
          : null,
        el("span", { text: label("projectStatus", project.active ? "active" : "inactive") }),
      ]);
      nav.appendChild(el("button", {
        class: "nav-item" + (active ? " is-active" : ""),
        attrs: { type: "button" },
        title: project.name,
        onClick: function () { selectProject(project.project_id); },
      }, [
        el("span", { class: "nav-name", text: project.name }),
        sub,
        el("span", { class: "nav-time", text: project.last_activity_at
          ? "更新 " + fmtTime(project.last_activity_at) : "无时间戳" }),
        meta,
      ]));
    });
  }

  // ------------------------------------------------------------------ //
  // render: overview
  // ------------------------------------------------------------------ //
  function goalCell(project) {
    const goal = project.current_goal;
    if (!goal) return el("span", { class: "muted", text: "无目标" });
    return el("div", {}, [
      el("div", { class: "clamp-2", text: goal.summary || "(空目标)" }),
      el("div", { class: "cell-sub" }, [
        pill("goalStatus", goal.status),
        idLine(goal.goal_id, "goal_id"),
      ]),
    ]);
  }

  function goalStatusCell(project) {
    const counts = project.goal_status_counts || {};
    const keys = Object.keys(counts);
    if (!keys.length) return el("span", { class: "muted", text: "—" });
    const box = el("div", { class: "pill-row" });
    keys.sort().forEach(function (key) {
      // 计数本身来自 Ledger，只把状态名换成中文；未知状态仍显示原始值。
      box.appendChild(pill("goalStatus", key, null, " × " + counts[key]));
    });
    return box;
  }

  function distributionCell(counts) {
    const c = counts || {};
    const box = el("div", { class: "dist" });
    // 这些 key 是**验收结论与目标状态**的混合分布，逐项标明语境。
    const order = [
      ["verified", "verdict", "ok"], ["rejected", "verdict", "bad"],
      ["inconclusive", "verdict", "warn"], ["pending", "verdict", "muted"],
      ["active", "goalStatus", "info"], ["needs_human", "goalStatus", "warn"],
      ["needs_approval", "goalStatus", "warn"],
    ];
    let shown = 0;
    order.forEach(function (entry) {
      const value = c[entry[0]];
      if (!value) return;
      shown += 1;
      const info = I18N.translate(entry[1], entry[0]);
      box.appendChild(el("span", {
        class: "dist-item tone-" + entry[2],
        attrs: { title: entry[0] },
      }, [
        el("span", { class: "dist-key", text: info.text }),
        el("span", { class: "dist-val", text: String(value) }),
      ]));
    });
    if (!shown) box.appendChild(el("span", { class: "muted", text: "无任务" }));
    return box;
  }

  function recentTaskCell(project) {
    const task = project.recent_task;
    if (!task) return el("span", { class: "muted", text: "无任务" });
    return el("div", {}, [
      el("div", { class: "clamp-1", text: task.title || "(无标题)" }),
      el("div", { class: "cell-sub" }, [pill("taskState", task.state),
                                        pill("verdict", task.verdict)]),
      el("div", { class: "cell-sub" }, [idLine(task.task_id, "task_id")]),
    ]);
  }

  function projectTable(projects) {
    const table = el("table", { class: "table table-projects" });
    table.appendChild(el("thead", {}, [el("tr", {}, [
      el("th", { text: "工作项目（Project）" }),
      el("th", { text: "当前 工作目标（Goal）" }),
      el("th", { text: "目标状态" }),
      el("th", { text: "工作任务（WorkItem）状态分布" }),
      el("th", { text: "最近任务" }),
      el("th", { text: "需要人工处理" }),
      el("th", { text: "最近活动" }),
    ])]));
    const body = el("tbody");

    projects.forEach(function (project) {
      const row = el("tr", {
        class: "row-clickable",
        title: "打开 " + project.name,
      });
      row.addEventListener("click", function () { selectProject(project.project_id); });

      row.appendChild(el("td", {}, [
        el("button", {
          class: "link-btn",
          attrs: { type: "button" },
          text: project.name,
          onClick: function (event) {
            event.stopPropagation();
            selectProject(project.project_id);
          },
        }),
        el("div", { class: "cell-sub" }, [idLine(project.project_id, "project_id")]),
      ]));

      row.appendChild(el("td", { class: "cell-goal" }, [goalCell(project)]));
      row.appendChild(el("td", {}, [goalStatusCell(project)]));
      row.appendChild(el("td", {}, [distributionCell(project.task_counts)]));
      row.appendChild(el("td", { class: "cell-recent" }, [recentTaskCell(project)]));

      const attentionCell = el("td");
      if (project.attention_count > 0) {
        attentionCell.appendChild(
          el("span", { class: "tag tag-bad", text: project.attention_count + " 项待处理" }));
      } else {
        attentionCell.appendChild(el("span", { class: "muted", text: "无需介入" }));
      }
      row.appendChild(attentionCell);

      row.appendChild(el("td", { class: "cell-time" }, [
        el("div", { text: project.last_activity_at
          ? fmtTime(project.last_activity_at) : "无可信时间戳" }),
        project.last_activity_at
          ? el("div", { class: "cell-sub", text: fmtRelative(project.last_activity_at) })
          : null,
      ]));
      body.appendChild(row);
    });

    table.appendChild(body);
    return el("div", { class: "table-wrap" }, [table]);
  }

  function emptyState(title, body) {
    return el("div", { class: "empty" }, [
      el("h3", { text: title }),
      el("p", { text: body }),
    ]);
  }

  function renderOverview() {
    const view = clear($("overviewView"));
    if (!state.overview) return;
    const projects = state.overview.projects || [];
    if (!projects.length) {
      view.appendChild(emptyState(
        "这个账本（Ledger）里还没有工作项目",
        "本页面只展示账本中真实存在的项目。用 controlplane_cli.py project add 注册项目后刷新即可。"));
      return;
    }
    view.appendChild(el("section", { class: "panel" }, [
      el("div", { class: "panel-head" }, [
        el("h2", { text: "项目列表" }),
        el("p", { class: "muted",
                  text: "任务数只是任务数，不是项目完成度——Ledger 不提供工作量度量。" }),
      ]),
      projectTable(projects),
    ]));
    view.appendChild(attentionPanel(state.overview.attention || [], "全局人工介入区"));
  }

  // ------------------------------------------------------------------ //
  // render: attention
  // ------------------------------------------------------------------ //
  function attentionPanel(items, title) {
    const panel = el("section", { class: "panel panel-attention" }, [
      el("div", { class: "panel-head" }, [
        el("h2", { text: title }),
        el("p", { class: "muted",
                  text: "状态保持原始类型：审批与失联需要不同的处理动作，不会被合并成一个“失败”。" }),
      ]),
    ]);
    if (!items.length) {
      panel.appendChild(el("p", { class: "placeholder", text: "当前没有需要人工介入的事项。" }));
      return panel;
    }
    const list = el("ul", { class: "attention-list" });
    items.forEach(function (item) {
      const flags = (item.flags || []).map(function (flag) {
        return pill("attentionFlag", flag);
      });
      // 目标与任务的 status 语义不同，必须按 item.kind 选语境。
      const statusKind = item.kind === "goal" ? "goalStatus" : "taskState";
      list.appendChild(el("li", { class: "attention-item" }, [
        el("div", { class: "attention-main" }, [
          el("div", { class: "attention-title" }, [
            pill(statusKind, item.status,
                 item.kind === "goal" ? "工作目标" : "工作任务"),
            item.verdict ? pill("verdict", item.verdict) : null,
            el("span", { class: "attention-project",
                         text: item.project_name || item.project_id }),
          ]),
          item.title ? el("div", { class: "attention-text", text: item.title }) : null,
          item.detail ? el("div", { class: "attention-detail", text: item.detail }) : null,
          el("div", { class: "pill-row" }, flags),
        ]),
        el("div", { class: "attention-side" }, [
          idLine(item.object_id, item.kind === "goal" ? "goal_id" : "task_id"),
          el("div", { class: "cell-sub",
                      text: item.updated_at ? fmtTime(item.updated_at) : "" }),
        ]),
      ]));
    });
    panel.appendChild(list);
    return panel;
  }

  // ------------------------------------------------------------------ //
  // render: project detail
  // ------------------------------------------------------------------ //
  function kvGrid(pairs) {
    const grid = el("dl", { class: "kv-grid" });
    pairs.forEach(function (pair) {
      grid.appendChild(el("div", { class: "kv" }, [
        el("dt", { text: pair[0] }),
        el("dd", { text: pair[1] }),
      ]));
    });
    return grid;
  }

  function uniqueValues(values) {
    const seen = {};
    const out = [];
    values.forEach(function (value) {
      if (value === null || value === undefined || value === "") return;
      const key = String(value);
      if (seen[key]) return;
      seen[key] = true;
      out.push(key);
    });
    return out.sort();
  }

  /**
   * 下拉筛选器。
   *
   * ``kind`` 给出 i18n 语境：option 的 **value 永远是原始枚举**（筛选、跳转
   * 都依赖它），只有可见文字是中文。未知值显示“原始值（未知状态）”，
   * 不会被悄悄归入某个已知类别。
   */
  function filterSelect(id, current, values, allLabel, onChange, kind) {
    const select = el("select", { class: "select",
                                  attrs: { id: id, "aria-label": allLabel } });
    values.forEach(function (value, index) {
      const isAll = index === 0 && value === "all";
      const text = isAll ? allLabel : (kind ? label(kind, value) : String(value));
      const option = el("option", {
        text: text,
        attrs: { value: String(value), title: isAll ? null : String(value) },
      });
      if (String(value) === String(current)) option.selected = true;
      select.appendChild(option);
    });
    select.addEventListener("change", function (event) { onChange(event.target.value); });
    return select;
  }

  function goalExecutionStage(goal, detail) {
    if (goal.status === "completed") {
      return { label: "目标已完成", tone: "ok",
               note: "工作目标（Goal）已到终态；不会再被自动唤醒。" };
    }
    if (goal.status === "needs_human") {
      return { label: "目标待人工处理", tone: "warn",
               note: goal.stop_reason || "需要人工负责人（Human Owner）处理。" };
    }
    const task = (detail.tasks || []).find(function (item) {
      return item.task_id === goal.current_task_id;
    });
    if (task && ["queued", "starting", "running", "waiting_for_approval",
                 "dispatch_unknown"].indexOf(task.state) >= 0) {
      // 这里描述的是**执行状态**，不是验收结论，所以不能写成“通过”。
      return { label: "执行器（Worker）正在执行", tone: "info",
               note: "执行状态：" + label("taskState", task.state) +
                 " · 验收结论：" + label("verdict", task.verdict || "pending") };
    }
    if (task && task.verdict === "verified") {
      return { label: "验收器（Verifier）已验收通过", tone: "ok",
               note: "账本（Ledger）判定 verdict = verified（验收通过）；"
                 + "等待目标的后续状态。" };
    }
    if ((goal.decision_count || 0) > 0 || (goal.task_count || 0) > 0) {
      return { label: "目标正在推进", tone: "info",
               note: "状态来自目标与账本；调度进程（Runner）是否在线仍为未知。" };
    }
    if (goal.linked_task_count) {
      // A WorkItem exists but is not in flight and has no verified verdict: the
      // LD forwarded it and the outcome is whatever the ledger says, nothing
      // more.
      return { label: "调度器（LD）已转发工作任务", tone: "info",
               note: I18N.count(goal.linked_task_count, "个工作任务") + "与这个目标关联；"
                 + "验收结论以账本为准。" };
    }
    const chat = (state.health && state.health.chat) || {};
    if (chat.intent_model_configured === false) {
      return { label: "目标已创建 · 模型未配置", tone: "warn",
               note: "本 Dashboard 未配置模型；调度进程是否已启动为未知。" };
    }
    return { label: "目标已创建 · 等待调度进程", tone: "muted",
             note: "尚无推进记录；调度进程存活状态为未知。" };
  }

  // ------------------------------------------------------------------ //
  // needs_human handling: open a read-only view of one stopped goal, then
  // stage its resume through the existing LDChat confirmation flow.
  // The browser never names a resume target by position, never picks a goal
  // for the operator and never applies the resume itself.
  // ------------------------------------------------------------------ //
  function handlingStatus(text) {
    const node = $("goalHandlingStatus");
    if (node) node.textContent = text || "";
  }

  function closeGoalHandling() {
    const modal = $("goalHandling");
    if (modal) modal.hidden = true;
    state.handling = null;
    $("goalHandlingConfirm").hidden = true;
    $("goalHandlingConfirmText").textContent = "";
    handlingStatus("");
  }

  function goalHandlingTaskCard(task) {
    const card = el("div", { class: "goal-task-card",
                             dataset: { taskId: task.task_id } });
    card.appendChild(el("div", { class: "pill-row" }, [
      el("button", {
        class: "link-btn mono small",
        attrs: { type: "button" },
        text: task.task_id,
        onClick: function () {
          closeGoalHandling();
          focusTask(task.task_id);
        },
      }),
      pill("taskState", task.state, "执行状态"),
      pill("verdict", task.verdict, "验收结论"),
      task.queue_status ? pill("queueStatus", task.queue_status, "队列") : null,
    ]));
    if (task.attention_flags && task.attention_flags.length) {
      card.appendChild(el("div", { class: "pill-row" },
        task.attention_flags.map(function (flag) {
          return pill("attentionFlag", flag);
        })));
    }
    const failures = task.failed_assertions || [];
    if (failures.length) {
      card.appendChild(el("div", { class: "cell-sub assertion-fail",
        text: "验收器（Verifier）未通过的检查项（" + failures.length + "）：" }));
      const list = el("ul");
      failures.forEach(function (failure) {
        // 断言原文（kind / target / value / observed）保持原样，不改写证据。
        list.appendChild(el("li", {
          class: "assertion-fail mono small",
          text: failure.kind + " " + (failure.target || "—") +
            (failure.value ? " = " + failure.value : "") +
            (failure.observed ? " · " + failure.observed : ""),
        }));
      });
      card.appendChild(list);
    } else if (task.verdict === "rejected" || task.verdict === "inconclusive") {
      card.appendChild(el("div", { class: "cell-sub assertion-fail",
        text: "验收结论：" + label("verdict", task.verdict) +
          "（verdict = " + task.verdict + "）。账本未记录可归因的产物断言，"
          + "这不是“通过”；重新派工前需要先看失败原因。" }));
    }
    const warnings = task.warnings || [];
    if (warnings.length) {
      card.appendChild(el("div", { class: "cell-sub",
        text: "警告信息（warnings，原始文本）：" }));
      const list = el("ul");
      warnings.forEach(function (warning) {
        list.appendChild(el("li", { class: "mono small", text: warning }));
      });
      card.appendChild(list);
    }
    card.appendChild(el("div", { class: "cell-sub muted",
      text: "尝试次数 " + num(task.attempt_count) + " · 账本证据 "
        + num(task.evidence_count) + " 条"
        + (task.evidence_count > (task.evidence || []).length
          ? "（仅列出前 " + (task.evidence || []).length + " 条 id）" : "") }));
    return card;
  }

  function renderGoalHandling() {
    const body = clear($("goalHandlingBody"));
    const data = state.handling;
    if (!data) {
      body.appendChild(el("p", { class: "placeholder", text: "正在读取目标…" }));
      return;
    }
    const goal = data.goal || {};
    $("goalHandlingTitle").textContent = "处理待人工处理的目标（Goal）" +
      (goal.goal_id ? " · " + goal.goal_id : "");
    // 三条信息必须分开写清楚，不能混成一句“失败”：
    //   · 停止原因 —— 谁把目标停下来的、为什么；
    //   · 执行状态 —— Worker 有没有跑完（taskState）；
    //   · 验收结果 —— Verifier 的结论（verdict）。
    body.appendChild(kvGrid([
      ["工作项目（Project）", (data.project_name || "—") + " · " + (data.project_id || "—")],
      ["目标 ID（Goal ID）", goal.goal_id || "—"],
      ["目标状态", label("goalStatus", goal.status) +
        (goal.status ? "（" + goal.status + "）" : "")],
      ["停止原因", data.stop_reason || "（账本未记录停止原因）"],
      ["最后一步", (data.last_action || "—") +
        (data.last_next_state ? " → " + data.last_next_state : "")],
      ["决策 / 任务计数", num(goal.decision_count) + " / " + num(goal.task_count)],
      ["预算（恢复不会扩大）", (goal.max_decisions_total === null ||
        goal.max_decisions_total === undefined
        ? "尚未盖章" : goal.max_decisions_total + " 次决策") + " · " +
        (goal.max_tasks_total === null || goal.max_tasks_total === undefined
          ? "—" : goal.max_tasks_total + " 个任务")],
      ["目标正文", goal.summary || "（空目标）"],
    ]));

    body.appendChild(el("p", { class: "modal-hint",
      text: "“执行状态”说明执行器（Worker）是否跑完；“验收结论”是验收器（Verifier）"
        + "依据冻结断言的独立判定。二者不是一回事：执行成功不等于验收通过。" }));

    const section = el("div", { class: "modal-section" }, [
      el("h3", { text: "关联工作任务与验收结果（" +
        num(data.linked_task_count) + " 个）" }),
    ]);
    const tasks = data.tasks || [];
    if (!tasks.length) {
      section.appendChild(el("p", { class: "placeholder",
        text: "这个目标还没有调度器（LD）转发的工作任务；停止可能发生在派工之前。" }));
    }
    tasks.forEach(function (task) {
      section.appendChild(goalHandlingTaskCard(task));
    });
    if (data.truncated) {
      section.appendChild(el("p", { class: "muted small",
        text: "只列出前 " + tasks.length + " 个工作任务（共 "
          + num(data.linked_task_count) + " 个）。" }));
    }
    body.appendChild(section);

    // 判断仍用原始枚举 needs_human；中文只用于显示。
    const resumable = goal.status === "needs_human";
    $("goalHandlingStage").hidden = !resumable;
    // Enabled only now: staging needs the goal/project identity from this
    // projection, so a click during loading would be a no-op with no feedback.
    // The operator must also be able to read the stop reason first.
    $("goalHandlingStage").disabled = !resumable;
    $("goalHandlingStage").dataset.goalId = goal.goal_id || "";
    if (!resumable) {
      handlingStatus("该目标当前状态为：" + label("goalStatus", goal.status)
        + "，没有可恢复的暂停状态。");
    } else {
      handlingStatus("确认停止原因与验收结论后，可暂存恢复请求；"
        + "真正恢复需要再点一次「确认恢复」。");
    }
  }

  function openGoalHandling(projectId, goalId) {
    const modal = $("goalHandling");
    // The chat drawer is a fixed overlay; leaving it open would cover the very
    // button that was just clicked and the table behind the dialog.  The token
    // and conversation stay in memory, so closing it costs nothing.
    $("chatPanel").hidden = true;
    modal.hidden = false;
    state.handling = null;
    $("goalHandlingConfirm").hidden = true;
    $("goalHandlingConfirmText").textContent = "";
    handlingStatus("正在读取目标…");
    $("goalHandlingStage").disabled = true;
    clear($("goalHandlingBody")).appendChild(
      el("p", { class: "placeholder", text: "正在读取目标…" }));
    getJson("/api/projects/" + encodeURIComponent(projectId) +
      "/goals/" + encodeURIComponent(goalId) + "/handling")
      .then(function (payload) {
        state.handling = payload;
        renderGoalHandling();
        handlingStatus("");
      })
      .catch(function (error) {
        handlingStatus("读取失败：" + (error.message || String(error)));
      });
  }

  // ------------------------------------------------------------------ //
  // publication target: the project a Goal will be created in.  The chat
  // project selector and the project being viewed are two different things,
  // so the mismatch is shown explicitly and every publication is confirmed
  // against a target the operator has seen.
  // ------------------------------------------------------------------ //
  function chatProjectRow(projectId) {
    const projects = (state.overview && state.overview.projects) || [];
    return projects.find(function (project) {
      return project.project_id === projectId;
    }) || null;
  }

  function publishTargetKnown() {
    return Boolean(state.chat.projectId && chatProjectRow(state.chat.projectId));
  }

  function resetPublishConfirm() {
    state.publish.confirmToken = null;
    state.publish.projectId = null;
    const box = $("publishConfirm");
    if (box) {
      box.hidden = true;
      clear(box);
    }
  }

  function renderPublishNotice() {
    const notice = $("publishProjectNotice");
    if (!notice) return;
    // The target moved (selector, restored session or a loaded conversation):
    // the previous confirmation described a different project and is dropped.
    if (state.publish.projectId && state.publish.projectId !== state.chat.projectId) {
      resetPublishConfirm();
      publishStatus("");
    }
    clear(notice);
    if (!state.chat.projectId) {
      // Nothing to warn about yet: the empty selector plus the disabled button
      // already say "choose a project".  A permanent red box here would be
      // noise, and noise is what makes a real mismatch easy to miss.
      notice.hidden = true;
      return;
    }
    // Two independent warnings: the target may differ from the project being
    // *viewed*, and from the project the LD currently *understands*.
    const row = chatProjectRow(state.chat.projectId);
    const viewed = state.selectedProjectId;
    const identified = state.chat.identified && state.chat.identified.project_id;
    const mismatch = (viewed && viewed !== state.chat.projectId) ||
      (identified && identified !== state.chat.projectId);
    notice.hidden = !mismatch;
    if (!mismatch) return;
    const viewedRow = chatProjectRow(viewed);
    const identifiedRow = chatProjectRow(identified);
    notice.appendChild(el("strong", {
      text: "发布目标与当前查看 / 识别到的 Project 不一致：",
    }));
    if (viewed && viewed !== state.chat.projectId) {
      notice.appendChild(el("span", {
        text: "当前查看 " + (viewedRow ? viewedRow.name + " · " : "") + viewed + "。",
      }));
    }
    if (identified && identified !== state.chat.projectId) {
      notice.appendChild(el("span", {
        text: "LD 当前识别 " + (identifiedRow ? identifiedRow.name + " · " : "")
          + identified + "。",
      }));
    }
    notice.appendChild(el("span", {
      text: "发布目标是 " + (row ? row.name + " · " : "") + state.chat.projectId
        + "。确认发布对象无误，或先在「发布目标项目」中重新选择。",
    }));
  }

  function renderPublishTargetSummary(container, projectId) {
    const row = chatProjectRow(projectId);
    container.appendChild(el("div", {}, [
      el("span", { class: "muted", text: "将发布到 Project：" }),
      el("strong", { text: row ? row.name : "(未知 Project)" }),
    ]));
    container.appendChild(el("div", { class: "publish-target",
      text: projectId }));
    const root = state.publish.roots && state.publish.roots[projectId];
    container.appendChild(el("div", { class: "publish-target",
      text: root ? "执行仓库路径：" + root : "执行仓库路径：未知（未读取到）" }));
  }

  function loadPublishTargets() {
    if (!state.chat.token || state.publish.roots) return Promise.resolve();
    return chatJson("/api/chat/projects").then(function (payload) {
      const roots = {};
      (payload.projects || []).forEach(function (project) {
        roots[project.project_id] = project.root;
      });
      state.publish.roots = roots;
      renderPublishNotice();
    }).catch(function () { /* the notice simply stays without a root */ });
  }

  function goalsPanel(detail) {
    const goals = detail.goals || [];
    const panel = el("section", { class: "panel" }, [
      el("div", { class: "panel-head" }, [
        el("h2", { text: "B. 工作目标（Goal）列表" }),
        // 选项 value 保持原始枚举（筛选逻辑依赖它），只把显示文字换成中文。
        filterSelect("goalFilter", state.goalFilter,
          ["all"].concat(uniqueValues(goals.map(function (g) { return g.status; }))),
          "全部状态", function (value) { state.goalFilter = value; renderDetail(); },
          "goalStatus"),
      ]),
    ]);
    if (!goals.length) {
      panel.appendChild(el("p", { class: "placeholder", text: "这个项目还没有工作目标。" }));
      return panel;
    }
    const rows = goals.filter(function (goal) {
      return state.goalFilter === "all" || goal.status === state.goalFilter;
    });
    if (!rows.length) {
      panel.appendChild(el("p", { class: "placeholder", text: "没有符合筛选条件的工作目标。" }));
      return panel;
    }
    const table = el("table", { class: "table" });
    table.appendChild(el("thead", {}, [el("tr", {}, [
      el("th", { text: "目标（Goal）" }),
      el("th", { text: "目标状态" }),
      el("th", { text: "执行阶段" }),
      el("th", { text: "决策次数" }),
      el("th", { text: "任务数" }),
      el("th", { text: "当前任务" }),
      el("th", { text: "停止/等待原因" }),
      el("th", { text: "处理" }),
      el("th", { text: "最后更新" }),
    ])]));
    const body = el("tbody");
    rows.forEach(function (goal) {
      const stage = goalExecutionStage(goal, detail);
      const decisionCell = el("td", {}, [
        el("div", { text: String(goal.decision_count) + " 次总计" }),
        el("div", { class: "cell-sub", text: "模型决策 " + goal.model_decision_count +
          " · 人工恢复 " + goal.resume_count }),
      ]);
      if (goal.max_decisions_total !== null && goal.max_decisions_total !== undefined) {
        decisionCell.appendChild(
          el("div", { class: "cell-sub", text: "上限 " + goal.max_decisions_total + " 次" }));
      }
      const taskCell = el("td", {}, [el("div", { text: String(goal.task_count) })]);
      if (goal.max_tasks_total !== null && goal.max_tasks_total !== undefined) {
        taskCell.appendChild(
          el("div", { class: "cell-sub", text: "上限 " + goal.max_tasks_total + " 个" }));
      }
      // Forwarding trail: the ledger records which WorkItems the LD submitted
      // for this goal (metadata.ld_goal_ref). Zero linked tasks means "not
      // forwarded yet", which is not the same as "nothing happened".
      if (goal.linked_task_count) {
        const linkBox = el("div", { class: "pill-row" });
        Object.keys(goal.linked_task_counts || {}).sort().forEach(function (key) {
          // 投影的键是 `verdict or state`，两种语境都可能出现。
          linkBox.appendChild(pill(["verdict", "taskState"], key, null,
            " × " + goal.linked_task_counts[key]));
        });
        taskCell.appendChild(linkBox);
        (goal.linked_task_ids || []).slice(0, 3).forEach(function (taskId) {
          taskCell.appendChild(el("div", { class: "cell-sub" }, [
            el("button", {
              class: "link-btn mono small",
              attrs: { type: "button" },
              text: taskId,
              onClick: function () { focusTask(taskId); },
            }),
          ]));
        });
      } else {
        taskCell.appendChild(el("div", { class: "cell-sub muted",
          text: "尚无调度器（LD）转发的工作任务" }));
      }
      const currentCell = el("td", { class: "cell-id" });
      if (goal.current_task_id) {
        currentCell.appendChild(el("button", {
          class: "link-btn mono small",
          attrs: { type: "button" },
          text: goal.current_task_id,
          onClick: function () { focusTask(goal.current_task_id); },
        }));
      } else {
        currentCell.appendChild(el("span", { class: "muted", text: "—" }));
      }
      const reasonCell = el("td");
      if (goal.stop_reason) {
        reasonCell.appendChild(
          el("div", { class: "text-warn clamp-3", text: goal.stop_reason }));
      } else {
        reasonCell.appendChild(el("span", { class: "muted", text: "—" }));
      }
      if (goal.last_action) {
        reasonCell.appendChild(el("div", { class: "cell-sub",
          text: "最后决策：" + goal.last_action +
            (goal.last_next_state ? " → " + goal.last_next_state : "") }));
      }

      // The handling cell: a stopped goal needs an obvious, clickable way in.
      // It opens a read-only view first; nothing is resumed from here.
      const handlingCell = el("td", { class: "cell-action" });
      if (goal.status === "needs_human") {
        handlingCell.appendChild(el("button", {
          class: "btn btn-small btn-primary goal-handle-btn",
          attrs: { type: "button",
                   "aria-label": "处理目标 " + goal.goal_id,
                   title: "查看停止原因、验收结果与证据" },
          dataset: { goalId: goal.goal_id },
          text: "处理",
          onClick: function () {
            openGoalHandling(detail.project.project_id, goal.goal_id);
          },
        }));
      } else if (goal.status === "failed") {
        handlingCell.appendChild(el("button", {
          class: "btn btn-small ghost goal-handle-btn",
          attrs: { type: "button",
                   "aria-label": "查看目标 " + goal.goal_id },
          dataset: { goalId: goal.goal_id },
          text: "查看",
          onClick: function () {
            openGoalHandling(detail.project.project_id, goal.goal_id);
          },
        }));
      } else {
        handlingCell.appendChild(el("span", { class: "muted", text: "—" }));
      }

      body.appendChild(el("tr", {}, [
        el("td", {}, [
          el("div", { class: "clamp-3", text: goal.summary || "(空目标)" }),
          el("div", { class: "cell-sub" }, [idLine(goal.goal_id, "goal_id")]),
          el("div", { class: "cell-sub muted", text: "goal_ref " + (goal.goal_ref || "—") }),
        ]),
        el("td", {}, [pill("goalStatus", goal.status)]),
        el("td", { class: "goal-stage" }, [
          el("div", { class: "pill pill-" + stage.tone, text: stage.label }),
          el("div", { class: "stage-note clamp-3", text: stage.note }),
        ]),
        decisionCell,
        taskCell,
        currentCell,
        reasonCell,
        handlingCell,
        el("td", { class: "cell-time", text: fmtTime(goal.updated_at) }),
      ]));
    });
    table.appendChild(body);
    panel.appendChild(el("div", { class: "table-wrap" }, [table]));
    return panel;
  }

  function tasksPanel(detail) {
    const tasks = detail.tasks || [];
    const panel = el("section", { class: "panel" }, [
      el("div", { class: "panel-head" }, [
        el("h2", { text: "C. 工作任务（WorkItem）列表" }),
        filterSelect("taskFilter", state.taskFilter,
          ["all"].concat(uniqueValues(tasks.map(function (t) { return t.state; })),
            uniqueValues(tasks.map(function (t) { return t.verdict; }))),
          "全部状态 / 判定", function (value) { state.taskFilter = value; renderDetail(); }),
      ]),
    ]);
    if (!tasks.length) {
      panel.appendChild(el("p", { class: "placeholder", text: "这个项目还没有工作任务。" }));
      return panel;
    }
    const rows = tasks.filter(function (task) {
      if (state.taskFilter === "all") return true;
      return task.state === state.taskFilter || task.verdict === state.taskFilter;
    });
    if (!rows.length) {
      panel.appendChild(el("p", { class: "placeholder", text: "没有符合筛选条件的工作任务。" }));
      return panel;
    }
    const table = el("table", { class: "table" });
    table.appendChild(el("thead", {}, [el("tr", {}, [
      el("th", { text: "任务（Task）" }),
      el("th", { text: "执行状态" }),
      el("th", { text: "验收结论" }),
      el("th", { text: "队列状态" }),
      el("th", { text: "尝试 / 会话" }),
      el("th", { text: "工作区 / 上下文" }),
      el("th", { text: "需要注意" }),
      el("th", { text: "创建时间" }),
    ])]));
    const body = el("tbody");
    rows.forEach(function (task) {
      const tr = el("tr", { attrs: { id: "task-row-" + task.task_id } });
      tr.appendChild(el("td", {}, [
        el("div", { class: "clamp-2", text: task.title || "(无标题)" }),
        el("div", { class: "cell-sub" }, [idLine(task.task_id, "task_id")]),
      ]));
      tr.appendChild(el("td", {}, [pill("taskState", task.state)]));
      tr.appendChild(el("td", {}, [
        pill("verdict", task.verdict),
        el("div", { class: "cell-sub muted", text: task.has_effective_prompt
          ? "有效提示词已冻结" : "无冻结提示词" }),
      ]));
      tr.appendChild(el("td", { class: "cell-id" }, [
        el("span", { class: "mono small", text: task.queue_status
          ? label("queueStatus", task.queue_status) : "—" }),
      ]));

      const attemptCell = el("td", { class: "cell-id" });
      attemptCell.appendChild(task.current_attempt_id
        ? el("span", { class: "mono small", text: task.current_attempt_id })
        : el("span", { class: "muted", text: "—" }));
      attemptCell.appendChild(el("div", { class: "cell-sub" }, [
        el("button", {
          class: "link-btn small",
          attrs: { type: "button" },
          text: "查看 Session",
          onClick: function () { focusSessions(task.task_id); },
        }),
      ]));
      tr.appendChild(attemptCell);

      tr.appendChild(el("td", { class: "cell-id" }, [
        el("div", { class: "mono small", text: task.workspace_id || "—" }),
        el("div", { class: "cell-sub", text: "上下文版本 v" + (task.context_version === null ||
          task.context_version === undefined ? "—" : task.context_version) }),
      ]));

      const attentionCell = el("td");
      if (task.attention_flags && task.attention_flags.length) {
        attentionCell.appendChild(el("div", { class: "pill-row" },
          task.attention_flags.map(function (flag) {
            return pill("attentionFlag", flag);
          })));
      } else {
        attentionCell.appendChild(el("span", { class: "muted", text: "—" }));
      }
      if (task.warnings && task.warnings.length) {
        attentionCell.appendChild(el("div", {
          class: "cell-sub text-warn clamp-2", text: task.warnings.join("；") }));
      }
      tr.appendChild(attentionCell);
      tr.appendChild(el("td", { class: "cell-time", text: fmtTime(task.created_at) }));
      body.appendChild(tr);
    });
    table.appendChild(body);
    panel.appendChild(el("div", { class: "table-wrap" }, [table]));
    return panel;
  }

  function sessionsPanel(detail) {
    const sessions = detail.sessions || [];
    const panel = el("section", { class: "panel", attrs: { id: "sessions-panel" } }, [
      el("div", { class: "panel-head" }, [
        el("h2", { text: "D. 会话 / 执行器（Session / Worker）" }),
        el("p", { class: "muted",
                  text: "账本记录的是绑定关系，不是心跳；存在会话引用（SessionReference）不代表执行器在线。" }),
      ]),
    ]);
    if (!sessions.length) {
      panel.appendChild(el("p", { class: "placeholder",
                                 text: "这个项目还没有观测到会话引用。" }));
      return panel;
    }
    const table = el("table", { class: "table" });
    table.appendChild(el("thead", {}, [el("tr", {}, [
      el("th", { text: "会话引用（SessionReference）" }),
      el("th", { text: "工作任务 / 尝试" }),
      el("th", { text: "执行器（Worker）" }),
      el("th", { text: "绑定状态" }),
      el("th", { text: "存活" }),
      el("th", { text: "观测时间" }),
    ])]));
    const body = el("tbody");
    sessions.forEach(function (ref) {
      const linkCell = el("td", { class: "cell-id" });
      if (ref.work_item_id) {
        linkCell.appendChild(el("button", {
          class: "link-btn mono small",
          attrs: { type: "button" },
          text: ref.work_item_id,
          onClick: function () { focusTask(ref.work_item_id); },
        }));
      } else {
        linkCell.appendChild(el("span", { class: "muted", text: "—" }));
      }
      linkCell.appendChild(
        el("div", { class: "mono small muted", text: ref.attempt_id || "无尝试记录" }));

      body.appendChild(el("tr", {
        attrs: { id: "session-row-" + ref.session_reference_id },
        dataset: { taskId: ref.work_item_id || "" },
      }, [
        el("td", {}, [
          el("div", { class: "cell-sub" },
            [idLine(ref.session_reference_id, "session_reference_id")]),
          el("div", { class: "mono small muted clamp-1",
                      text: "会话 ID " + (ref.session_id || "—") }),
        ]),
        linkCell,
        el("td", {}, [
          el("div", { text: ref.worker_instance_id || "—" }),
          el("div", { class: "cell-sub",
                      text: (ref.adapter || "—") + " · 节点 " + (ref.node_id || "—") }),
        ]),
        el("td", {}, [pill("bindingStatus", ref.binding_status)]),
        el("td", {}, [
          el("span", { class: "muted", text: "未知" }),
          el("div", { class: "cell-sub muted", text: "无心跳数据" }),
        ]),
        el("td", { class: "cell-time", text: fmtTime(ref.observed_at) }),
      ]));
    });
    table.appendChild(body);
    panel.appendChild(el("div", { class: "table-wrap" }, [table]));
    return panel;
  }

  function memoryCard(label, value, hint, tone) {
    return el("div", { class: "memory-card" + (tone ? " tone-" + tone : "") }, [
      el("span", { class: "memory-label", text: label }),
      el("strong", { class: "memory-value", text: value }),
      el("span", { class: "memory-hint", text: hint }),
    ]);
  }

  function memoryPanel(detail) {
    const memory = detail.memory || {};
    const panel = el("section", { class: "panel" }, [
      el("div", { class: "panel-head" }, [
        el("h2", { text: "E. 项目记忆（Project Memory）摘要" }),
        el("p", { class: "muted",
                  text: "只显示计数与条目元数据；知识正文不在本页面展示。" }),
      ]),
      el("div", { class: "memory-grid" }, [
        memoryCard("上下文版本", num(memory.context_version), "派发时冻结的版本号"),
        memoryCard("条目总数", num(memory.total), "全部上下文条目"),
        memoryCard("已验证", num(memory.verified), "已由证据支撑", "ok"),
        memoryCard("待确认", num(memory.candidate), "待确认", "warn"),
        memoryCard("未验证", num(memory.unverified), "未验证", "muted"),
        memoryCard("待合并", num(memory.needs_merge), "并发冲突待人工合并", "bad"),
      ]),
    ]);
    const entries = memory.entries || [];
    if (!entries.length) {
      panel.appendChild(el("p", { class: "placeholder", text: "这个项目还没有上下文条目。" }));
      return panel;
    }
    const table = el("table", { class: "table table-compact" });
    table.appendChild(el("thead", {}, [el("tr", {}, [
      el("th", { text: "标题" }),
      el("th", { text: "类型" }),
      el("th", { text: "验证等级" }),
      el("th", { text: "安全域" }),
      el("th", { text: "版本" }),
      el("th", { text: "更新时间" }),
    ])]));
    const body = el("tbody");
    entries.forEach(function (entry) {
      const titleCell = el("td", {}, [
        el("div", { class: "clamp-2", text: entry.title || "(无标题)" }),
      ]);
      if (entry.needs_merge) {
        titleCell.appendChild(el("span", { class: "tag tag-bad", text: "待合并" }));
      }
      titleCell.appendChild(
        el("div", { class: "cell-sub" }, [idLine(entry.entry_id, "entry_id")]));
      body.appendChild(el("tr", {}, [
        titleCell,
        el("td", {}, [pill("entryKind", entry.kind)]),
        el("td", {}, [pill("verification", entry.verification)]),
        el("td", {}, [pill("securityDomain", entry.security_domain)]),
        el("td", { text: entry.version === null || entry.version === undefined
          ? "—" : String(entry.version) }),
        el("td", { class: "cell-time", text: fmtTime(entry.updated_at) }),
      ]));
    });
    table.appendChild(body);
    panel.appendChild(el("div", { class: "table-wrap" }, [table]));
    return panel;
  }

  function renderDetail() {
    const view = clear($("detailView"));
    const detail = state.detail;
    if (!detail) {
      view.appendChild(el("p", { class: "placeholder", text: "正在加载项目详情…" }));
      return;
    }
    const project = detail.project;

    view.appendChild(el("div", { class: "detail-head" }, [
      el("div", {}, [
        el("h1", { text: project.name }),
        el("div", { class: "detail-ids" }, [
          idLine(project.project_id, "project_id"),
          el("span", { class: "sep", text: "·" }),
          pill("projectStatus", project.active ? "active" : "inactive"),
        ]),
      ]),
      el("div", { class: "detail-head-right" }, [
        el("button", {
          class: "btn btn-ghost",
          attrs: { type: "button" },
          text: "← 返回总览",
          onClick: function () { selectProject(null); },
        }),
      ]),
    ]));

    const goalCounts = detail.goal_status_counts || {};
    const goalCountText = Object.keys(goalCounts).length
      ? Object.keys(goalCounts).sort().map(function (key) {
        return key + " × " + goalCounts[key];
      }).join("，")
      : "无 Goal";

    view.appendChild(el("section", { class: "panel" }, [
      el("div", { class: "panel-head" }, [el("h2", { text: "A. 项目概要" })]),
      kvGrid([
        ["上下文版本", num(project.context_version)],
        ["默认适配器（Adapter）", project.default_adapter || "—"],
        ["并发上限", project.concurrency_limit === null ||
          project.concurrency_limit === undefined ? "—" : String(project.concurrency_limit)],
        ["目标总数", String((detail.goals || []).length)],
        ["目标状态分布", goalCountText],
        ["阻塞任务数", String((detail.blocked_tasks || []).length)],
        ["最近活动", project.last_activity_at
          ? fmtTime(project.last_activity_at) : "无可信时间戳"],
        ["创建时间", fmtTime(project.created_at)],
      ]),
      el("p", { class: "note",
                text: "不展示项目进度百分比：任务数量不代表项目实际工作量。" }),
    ]));

    view.appendChild(goalsPanel(detail));
    view.appendChild(tasksPanel(detail));
    view.appendChild(sessionsPanel(detail));
    view.appendChild(memoryPanel(detail));
    view.appendChild(attentionPanel(detail.attention || [], "F. 本项目的待人工处理事项"));
  }

  // ------------------------------------------------------------------ //
  // navigation helpers
  // ------------------------------------------------------------------ //
  function cssEscape(value) {
    if (window.CSS && window.CSS.escape) return window.CSS.escape(String(value));
    return String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  function flash(node) {
    node.classList.add("flash");
    setTimeout(function () { node.classList.remove("flash"); }, 1600);
    node.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  function focusTask(taskId, attempt) {
    if (!taskId) return;
    const row = document.getElementById("task-row-" + taskId);
    if (row) {
      flash(row);
      return;
    }
    // Filtered out: drop the filter, re-render, then try once more.
    if (state.taskFilter !== "all") {
      state.taskFilter = "all";
      renderDetail();
      setTimeout(function () { focusTask(taskId, attempt); }, 0);
    }
    if (attempt) return;
  }

  function focusSessions(taskId) {
    if (!taskId) return;
    const rows = document.querySelectorAll('[data-task-id="' + cssEscape(taskId) + '"]');
    if (rows.length) {
      flash(rows[0]);
      return;
    }
    const panel = document.getElementById("sessions-panel");
    if (panel) panel.scrollIntoView({ block: "start", behavior: "smooth" });
  }

  // ------------------------------------------------------------------ //
  // view state / banners
  // ------------------------------------------------------------------ //
  function setViewState(node) {
    const box = clear($("viewState"));
    if (node) box.appendChild(node);
  }

  function loadingNode(text) {
    return el("div", { class: "loading" }, [
      el("span", { class: "spinner", attrs: { "aria-hidden": "true" } }),
      el("span", { text: text }),
    ]);
  }

  function banner(text, tone, detail) {
    const node = $("connectionBanner");
    node.className = "banner" + (tone ? " banner-" + tone : "");
    clear(node);
    node.appendChild(el("strong", { text: text }));
    if (detail) node.appendChild(el("span", { text: " " + detail }));
    node.appendChild(el("button", {
      class: "btn btn-ghost btn-small",
      attrs: { type: "button" },
      text: "重试",
      onClick: function () { refresh(true); },
    }));
  }

  function hideBanner() {
    const node = $("connectionBanner");
    node.className = "banner banner-hidden";
    clear(node);
  }

  function ledgerErrorCard(code, message) {
    const hints = {
      ledger_missing: "账本文件不存在。本页面不会创建账本——请检查 --ledger 参数，或先用运行时/命令行生成一个。",
      ledger_unreadable: "账本无法读取：可能已损坏、被其他进程独占，或不是 SQLite 数据库。",
      schema_incompatible: "账本结构（Schema）与当前 Dashboard 不兼容：缺少必要的表，可能由不同版本写入。",
      ledger_busy: "数据库当前被写入方锁定，稍后会自动重试。",
    };
    return el("div", { class: "empty empty-error" }, [
      el("h3", { text: "账本读取失败" }),
      el("p", { text: message }),
      el("p", { class: "muted", text: hints[code] || "未知错误。" }),
      el("p", { class: "mono small muted", text: "error code: " + code }),
    ]);
  }

  // ------------------------------------------------------------------ //
  // UI-1 LD Chat. Every sentence goes through POST /api/chat/messages;
  // the browser never submits an intent, Goal id, command, or SQL operation.
  // ------------------------------------------------------------------ //
  function chatSaveSession() {
    try {
      if (state.chat.token) window.sessionStorage.setItem("ui1-chat-token", state.chat.token);
      // Only the *publication target* is remembered.  The conversation's own
      // project is server state and a global conversation must stay global
      // across reloads.
      if (state.chat.projectId) {
        window.sessionStorage.setItem("ui1-chat-project", state.chat.projectId);
      } else {
        window.sessionStorage.removeItem("ui1-chat-project");
      }
      if (state.chat.conversationId) {
        window.sessionStorage.setItem("ui1-chat-conversation", state.chat.conversationId);
      } else {
        window.sessionStorage.removeItem("ui1-chat-conversation");
      }
    } catch (err) { /* session storage is optional */ }
  }

  function chatError(error) {
    const box = $("chatError");
    if (!error) {
      box.hidden = true;
      box.textContent = "";
      return;
    }
    box.hidden = false;
    box.textContent = (error.message || String(error)) +
      (error.code ? " (" + error.code + ")" : "");
  }

  function renderChatRuntime() {
    const chat = (state.health && state.health.chat) || {};
    let text = "调度进程（Runner）：未知（账本无心跳）";
    if (chat.intent_model_configured === false) {
      text = "意图识别：本地确定性路由 · 调度器（LD）模型：本进程未配置 · 调度进程：未知";
    } else if (chat.model_local_domain_authorized === false) {
      text = "意图识别模型已配置，但本地域（local）未授权；本地消息不外发 · 调度进程：未知";
    } else if (chat.intent_model_configured === true) {
      text = "意图识别模型已配置并获本地域授权 · 调度进程：未知";
    }
    $("chatRuntimeStatus").textContent = text;
  }

  /**
   * Fill the *publication target* selector.
   *
   * Deliberately does not preselect a project: publishing is a project-level
   * write, and "the first project in the ledger" / "the one just clicked" is
   * not a statement that the operator meant that project.  The chat itself no
   * longer needs a project at all — that is the whole point of the global LD.
   */
  function syncChatProjects() {
    if (!state.chat.enabled) return;
    const select = $("chatProject");
    const projects = (state.overview && state.overview.projects) || [];
    const previous = state.chat.projectId;
    clear(select);
    select.appendChild(el("option", {
      text: "请选择发布目标项目…", attrs: { value: "" },
    }));
    projects.forEach(function (project) {
      const option = el("option", {
        text: project.name + " · " + project.project_id,
        attrs: { value: project.project_id },
      });
      if (project.project_id === previous) option.selected = true;
      select.appendChild(option);
    });
    // A previously chosen target that no longer exists is dropped rather than
    // silently pointing at a different project.
    if (state.chat.projectId && !projects.some(function (project) {
      return project.project_id === state.chat.projectId;
    })) {
      state.chat.projectId = null;
      resetPublishConfirm();
    }
    select.value = state.chat.projectId || "";
    renderIdentifiedProject();
    renderPublishNotice();
  }

  /** Which project the LD resolved for this turn, straight from the server. */
  function renderIdentifiedProject() {
    const badge = $("chatIdentifiedProject");
    if (!badge) return;
    const info = state.chat.identified;
    const bound = state.chat.conversationProjectId;
    let text = "尚未识别";
    let title = "";
    if (info && info.source === "ambiguous") {
      text = "待澄清（候选 " + (info.candidates || []).length + " 个）";
      title = (info.candidates || []).join(", ");
      badge.className = "badge badge-warn";
    } else if (info && info.project_id) {
      const row = chatProjectRow(info.project_id);
      text = (row ? row.name + " · " : "") + info.project_id;
      title = "识别依据：" + info.source;
      badge.className = "badge";
    } else if (bound) {
      const row = chatProjectRow(bound);
      text = (row ? row.name + " · " : "") + bound;
      title = "本会话绑定到此项目";
      badge.className = "badge";
    } else {
      badge.className = "badge badge-readonly";
    }
    badge.textContent = text;
    badge.title = title;
  }

  function renderConversationChoices() {
    const select = $("chatConversation");
    clear(select);
    select.appendChild(el("option", { text: "新会话（发送第一条消息后创建）",
                                      attrs: { value: "" } }));
    state.chat.conversations.forEach(function (conversation) {
      const label = conversation.conversation_id + " · " +
        conversation.message_count + " 条消息 · " + fmtTime(conversation.updated_at) +
        (conversation.requires_confirmation ? " · 待确认" : "");
      const option = el("option", {
        text: label, attrs: { value: conversation.conversation_id },
      });
      if (conversation.conversation_id === state.chat.conversationId) {
        option.selected = true;
      }
      select.appendChild(option);
    });
    select.value = state.chat.conversationId || "";
  }

  function renderChatMessages() {
    const box = clear($("chatMessages"));
    if (!state.chat.messages.length) {
      box.appendChild(el("p", { class: "placeholder",
        text: "这是一个新的全局会话。直接说你想问的项目或目标即可，例如"
          + "「现在有哪些项目？」「ACP-Development 进行到哪了？」"
          + "「在 ACP-Development 实现 greet.py」。写操作会先让你确认项目与目标。" }));
      return;
    }
    const lastSeq = state.chat.messages.reduce(function (best, row) {
      return Math.max(best, row.seq || 0);
    }, 0);
    state.chat.messages.forEach(function (message) {
      const intent = message.intent && message.intent.intent;
      let source = message.role ? I18N.t("chatRole", message.role) : "未知来源";
      if (intent) source += " / 意图：" + intent;
      if (message.seq === lastSeq && message.role !== "user" &&
          state.chat.lastIntentSource) {
        source += " / " + I18N.intentSource(state.chat.lastIntentSource);
      }
      box.appendChild(el("article", {
        class: "chat-message chat-message-" + (message.role || "system"),
      }, [
        el("div", { class: "chat-message-meta" }, [
          el("span", { text: source }),
          el("span", { text: fmtTime(message.created_at) }),
          message.goal_ids && message.goal_ids.length
            ? el("span", { text: "目标：" + message.goal_ids.join(", ") }) : null,
        ]),
        el("pre", { class: "chat-message-content", text: message.content }),
      ]));
    });
    box.scrollTop = box.scrollHeight;
  }

  function renderChatPending(conversation) {
    const pending = conversation && conversation.pending_action;
    state.chat.pendingAction = pending || null;
    const panel = $("chatConfirmation");
    panel.hidden = !pending;
    if (pending) {
      // pending.kind 是 LDChat 自己的动作类型，不是目标状态，不能套状态表。
      const KIND = { goal_resume: "恢复目标", resume_goal: "恢复目标",
                     publish_goal: "发布目标", cancel: "取消操作" };
      const kindText = KIND[pending.kind] || (pending.kind || "操作");
      // 目标正文在它上方的消息里完整给出（项目名称/ID/正文/目标 ID）；
      // 这里只补充标识，避免两处文案不一致。
      $("chatPendingLabel").textContent =
        kindText + (pending.project_id ? " · 项目 " + pending.project_id : "") +
        (pending.goal_id ? " · 目标 " + pending.goal_id : "") +
        "。确认或取消都会作为同一会话（conversation_id）的下一条消息交给调度器（LD）；"
        + "请先核对上方消息里的项目名称、项目 ID、完整目标与目标 ID。";
    }
  }

  // ------------------------------------------------------------------ //
  // Console publication: create a Goal through LDChat and then let the
  // existing LDGoalRunner/LD forward it. The browser never submits a
  // WorkItem, an intent, an assertion or a budget.
  // ------------------------------------------------------------------ //
  function publishStatus(text) {
    $("publishStatus").textContent = text || "";
  }

  function renderPublishResult(result) {
    const box = clear($("publishResult"));
    if (!result) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    const rows = [
      ["目标 ID", result.goal_id],
      ["目标引用", result.goal_ref],
      ["目标状态", result.status ? label("goalStatus", result.status) : result.status],
      ["是否新建", result.created ? "新建" : "复用既有目标"],
      ["预算", (result.max_decisions_total === null ||
        result.max_decisions_total === undefined)
        ? "尚未盖章（由调度进程首次推进时盖章）"
        : result.max_decisions_total + " 次决策 / " +
          (result.max_tasks_total === null || result.max_tasks_total === undefined
            ? "—" : result.max_tasks_total) + " 个任务"],
    ];
    rows.forEach(function (pair) {
      box.appendChild(el("div", { class: "kv-line" }, [
        el("span", { class: "muted", text: pair[0] + "：" }),
        el("span", { text: pair[1] === null || pair[1] === undefined
          ? "—" : String(pair[1]) }),
      ]));
    });
    if (result.next_step_note) {
      box.appendChild(el("p", { class: "text-warn", text: result.next_step_note }));
    }
    const chain = result.forwarding_chain || [];
    if (chain.length) {
      box.appendChild(el("p", { class: "muted small",
        text: "后续链路（不由本页面执行）：" }));
      const list = el("ol", { class: "chat-publish-chain" });
      chain.forEach(function (step) { list.appendChild(el("li", { text: step })); });
      box.appendChild(list);
    }
  }

  //: A publication is confirmed in two steps: "发布给 LD" shows the exact
  //: target (project name, id, repository root) and only the explicit confirm
  //: sends the request.  Switching project invalidates the pending confirmation,
  //: so a confirmation can never be applied to a project the operator did not
  //: read.
  function publishObjective() {
    if (state.chat.publishing) return;
    if (!state.chat.projectId) {
      chatError(new Error("请先选择 Project。"));
      return;
    }
    const input = $("publishObjective");
    const objective = input.value.trim();
    if (!objective) {
      chatError(new Error("请填写目标正文。"));
      return;
    }
    const target = state.chat.projectId;
    const token = target + "\u0000" + objective;
    if (state.publish.confirmToken === token) {
      sendPublish(target, objective);
      return;
    }
    state.publish.confirmToken = token;
    state.publish.projectId = target;
    const box = clear($("publishConfirm"));
    box.hidden = false;
    renderPublishTargetSummary(box, target);
    box.appendChild(el("div", { class: "muted small",
      text: "发布只会创建或复用一个工作目标；不会提交工作任务、不会授予预算，"
        + "也不会修改任何已有目标状态。" }));
    box.appendChild(el("div", { class: "actions" }, [
      el("button", {
        class: "btn btn-ghost btn-small",
        attrs: { type: "button" },
        text: "取消",
        onClick: function () {
          resetPublishConfirm();
          publishStatus("");
        },
      }),
      el("button", {
        class: "btn btn-primary btn-small",
        attrs: { type: "button", id: "publishConfirmBtn" },
        text: "确认发布到此项目",
        onClick: function () { sendPublish(target, objective); },
      }),
    ]));
    publishStatus("已选择发布目标，等待确认。");
  }

  function sendPublish(projectId, objective) {
    if (state.publish.projectId !== projectId ||
        state.publish.confirmToken !== projectId + "\u0000" + objective) {
      // The target changed between rendering the confirmation and clicking it.
      resetPublishConfirm();
      publishStatus("");
      chatError(new Error("发布目标已变化，请重新确认。"));
      return;
    }
    resetPublishConfirm();
    state.chat.publishing = true;
    chatError(null);
    publishStatus("正在提交 Goal…");
    renderChatConnection();
    chatJson("/api/chat/publish", {
      method: "POST",
      body: { project_id: projectId, objective: objective },
    }).then(function (result) {
      publishStatus("");
      renderPublishResult(result);
      $("publishObjective").value = "";
      // Goal truth belongs to the read-only projection path.
      refresh(true);
    }).catch(function (error) {
      publishStatus("");
      chatError(error);
    }).then(function () {
      state.chat.publishing = false;
      renderChatConnection();
    });
  }

  // ------------------------------------------------------------------ //
  // stage a resume of one named goal, then confirm it in the chat panel
  // ------------------------------------------------------------------ //
  function stageGoalResume() {
    const data = state.handling;
    if (!data) return;
    const goalId = (data.goal && data.goal.goal_id) || "";
    const projectId = data.project_id;
    if (!goalId || !projectId) return;
    $("goalHandlingStage").disabled = true;
    handlingStatus("正在暂存恢复请求…");
    chatJson("/api/chat/goal-resume", {
      method: "POST",
      body: { project_id: projectId, goal_id: goalId },
    }).then(function (turn) {
      handlingStatus("");
      $("goalHandlingStage").disabled = false;
      const confirmBox = $("goalHandlingConfirm");
      confirmBox.hidden = false;
      $("goalHandlingConfirmText").textContent =
        "将恢复目标 " + goalId + "（项目 " + projectId + "）。"
        + "只有单击下方「确认恢复」才会真正恢复；取消不会改变任何状态，"
        + "预算也不会被扩大。";
      $("goalHandlingConfirmBtn").dataset.conversationId = turn.conversation_id;
      $("goalHandlingConfirmBtn").dataset.goalId = goalId;
      // Show the same staging in the chat panel, so the operator can also finish
      // there with the ordinary confirm button.  This conversation *is* bound to
      // the goal's project (it was created for that resume), so it is carried as
      // the conversation binding, not as a publication target.
      state.chat.conversationId = turn.conversation_id;
      state.chat.conversationProjectId = projectId;
      state.chat.identified = { source: "bound", project_id: projectId };
      chatSaveSession();
      loadChatConversation(turn.conversation_id).catch(function () {
        /* the panel is best-effort; the modal stays authoritative */
      });
      // Deliberately not reopening the chat drawer here: it would cover the
      // modal's own confirmation buttons.  The staged action is already visible
      // in the chat panel whenever the operator opens it.
    }).catch(function (error) {
      $("goalHandlingStage").disabled = false;
      handlingStatus("");
      chatError(error);
      const message = (error && error.message) || String(error);
      $("goalHandlingConfirm").hidden = true;
      handlingStatus("暂存失败：" + message);
    });
  }

  function confirmGoalResume() {
    const button = $("goalHandlingConfirmBtn");
    const conversationId = button.dataset.conversationId;
    const goalId = button.dataset.goalId;
    if (!conversationId) return;
    button.disabled = true;
    handlingStatus("正在确认恢复…");
    chatJson("/api/chat/messages", {
      method: "POST",
      body: { conversation_id: conversationId,
              // The staged resume belongs to the handling view's project; the
              // publication target is irrelevant here.
              project_id: (state.handling && state.handling.project_id)
                || state.chat.conversationProjectId || null,
              message: "confirm" },
    }).then(function (turn) {
      button.disabled = false;
      $("goalHandlingConfirm").hidden = true;
      const resumed = Boolean(turn.facts && turn.facts.resumed);
      handlingStatus(resumed
        ? ("目标 " + goalId + " 已恢复；预算未变，调度进程连接状态仍为未知。")
        : ("恢复未生效：" + (turn.answer || "LDChat 未应用该确认。")));
      if (state.chat.conversationId === conversationId) {
        loadChatConversation(conversationId).catch(function () { /* rendered */ });
      }
      refresh(true);
    }).catch(function (error) {
      button.disabled = false;
      handlingStatus("确认失败：" + ((error && error.message) || String(error)));
    });
  }

  function cancelGoalResume() {
    const button = $("goalHandlingConfirmBtn");
    const conversationId = button.dataset.conversationId;
    button.dataset.conversationId = "";
    $("goalHandlingConfirm").hidden = true;
    if (!conversationId) {
      handlingStatus("已取消，未恢复任何目标。");
      return;
    }
    // The staged action is dropped by sending a superseding turn, which is the
    // existing LDChat behaviour: any non-confirmation message clears it.
    chatJson("/api/chat/messages", {
      method: "POST",
      body: { conversation_id: conversationId,
              project_id: (state.handling && state.handling.project_id)
                || state.chat.conversationProjectId || null,
              message: "cancel" },
    }).then(function (turn) {
      handlingStatus("已取消：" + (turn.answer || "未恢复任何目标。"));
      if (state.chat.conversationId === conversationId) {
        loadChatConversation(conversationId).catch(function () { /* rendered */ });
      }
    }).catch(function () {
      handlingStatus("已取消本地确认；若暂存仍在，可在 LD Chat 中回复 cancel。");
    });
  }

  function renderChatConnection() {
    $("chatAuthBox").hidden = state.chat.connected;
    $("chatControls").hidden = !state.chat.connected;
    $("publishBox").hidden = !state.chat.connected;
    $("publishBtn").disabled = state.chat.publishing || !state.chat.projectId;
    $("chatComposer").hidden = !state.chat.connected;
    $("chatConversationId").textContent = state.chat.conversationId
      ? "会话 ID：" + state.chat.conversationId
      : "尚未创建会话";
    $("chatSend").disabled = state.chat.sending;
    $("chatConfirm").disabled = state.chat.sending;
    $("chatCancel").disabled = state.chat.sending;
    $("chatSendStatus").textContent = state.chat.sending ? "调度器（LD）正在处理…" : "";
    // A global conversation is the default; a bound one only appears when the
    // operator arrived from a project-level entry point (console resume).
    const bound = state.chat.conversationProjectId;
    $("chatGlobalHint").hidden = Boolean(bound);
    if (bound) {
      $("chatGlobalHint").textContent =
        "本会话绑定到项目 " + bound + "，不能在此切换到其他项目；"
        + "需要跨项目讨论请新建会话（全局会话）。";
    }
    renderConversationChoices();
    renderChatMessages();
    renderChatRuntime();
    renderIdentifiedProject();
    renderPublishNotice();
  }

  function newChatConversation() {
    state.chat.conversationId = null;
    state.chat.messages = [];
    state.chat.pendingAction = null;
    state.chat.lastIntentSource = null;
    state.chat.conversationProjectId = null;
    // A new conversation starts with no identified project: the LD will resolve
    // one from the first sentence instead of inheriting the previous topic.
    state.chat.identified = null;
    chatSaveSession();
    renderChatPending(null);
    renderChatConnection();
    $("chatInput").focus();
  }

  function loadChatConversation(conversationId) {
    if (!conversationId) {
      newChatConversation();
      return Promise.resolve();
    }
    chatError(null);
    return chatJson("/api/chat/conversations/" + encodeURIComponent(conversationId))
      .then(function (payload) {
        const conversation = payload.conversation;
        state.chat.conversationId = conversation.conversation_id;
        // The conversation's own binding — never the publication target, which
        // is an unrelated, explicit choice.
        state.chat.conversationProjectId = conversation.project_id || null;
        state.chat.messages = payload.messages || [];
        state.chat.lastIntentSource = null;
        renderChatPending(conversation);
        chatSaveSession();
        renderChatConnection();
      }).catch(function (error) {
        chatError(error);
        if (error.status === 404) newChatConversation();
        throw error;
      });
  }

  function loadChatConversations(restoreConversation) {
    // Global conversation list: no project filter.  Conversations of every
    // project (and unbound ones) appear in one history, which is what makes the
    // entry point global.
    const url = "/api/chat/conversations?limit=100";
    return chatJson(url).then(function (payload) {
      state.chat.connected = true;
      state.chat.conversations = payload.conversations || [];
      renderConversationChoices();
      if (restoreConversation && state.chat.conversations.some(function (item) {
        return item.conversation_id === restoreConversation;
      })) {
        return loadChatConversation(restoreConversation);
      }
      if (state.chat.conversationId && !state.chat.conversations.some(function (item) {
        return item.conversation_id === state.chat.conversationId;
      })) {
        newChatConversation();
      }
      renderChatConnection();
      return null;
    }).catch(function (error) {
      state.chat.connected = false;
      renderChatConnection();
      chatError(error);
      throw error;
    });
  }

  function connectChat() {
    const token = $("chatToken").value.trim();
    if (!token) {
      chatError(new Error("请输入本地操作员 Token。"));
      return;
    }
    state.chat.token = token;
    chatSaveSession();
    loadChatConversations(state.chat.conversationId).catch(function () { /* rendered */ });
  }

  function sendChatMessage(explicitMessage) {
    if (state.chat.sending) return;
    const input = $("chatInput");
    const message = explicitMessage === undefined ? input.value.trim() : explicitMessage;
    if (!message) return;
    state.chat.sending = true;
    chatError(null);
    renderChatConnection();
    // ``project_id: null`` is the global LD: it identifies the project from the
    // sentence.  A bound conversation ignores the field on the server side, so
    // sending null here can never detach it from its project.
    const bound = state.chat.conversationProjectId;
    chatJson("/api/chat/messages", {
      method: "POST",
      body: {
        conversation_id: state.chat.conversationId,
        project_id: bound || null,
        message: message,
      },
    }).then(function (turn) {
      state.chat.conversationId = turn.conversation_id;
      state.chat.lastIntentSource = turn.intent_source;
      state.chat.identified = turn.project_routing || null;
      if (explicitMessage === undefined) input.value = "";
      chatSaveSession();
      return loadChatConversation(turn.conversation_id).then(function () {
        state.chat.lastIntentSource = turn.intent_source;
        state.chat.identified = turn.project_routing || state.chat.identified;
        renderChatConnection();
        return loadChatConversations(turn.conversation_id);
      });
    }).then(function () {
      // Goal and WorkItem truth remains on the read-only dashboard path.
      refresh(true);
    }).catch(function (error) {
      chatError(error);
    }).then(function () {
      state.chat.sending = false;
      renderChatConnection();
    });
  }

  function configureChatFromHealth(health) {
    state.health = health;
    state.chat.enabled = Boolean(health && health.chat_enabled);
    $("chatToggle").hidden = !state.chat.enabled;
    if (state.chat.enabled) {
      $("modeBadge").textContent = "全局调度对话";
      $("modeBadge").className = "badge";
      $("modeBadge").title = "页面查询仍为只读；仅 LD 对话端点可写";
      $("modeSubtitle").textContent = "项目驾驶舱 · 全局调度对话（LD）";
      $("chatToggle").textContent = "全局调度对话（LD）";
    } else {
      $("modeBadge").textContent = "只读";
      $("modeBadge").className = "badge badge-readonly";
      $("modeSubtitle").textContent = "只读项目驾驶舱（UI-0）";
    }
    renderChatRuntime();
    if (state.chat.enabled && state.overview && !state.chat.restored) {
      state.chat.restored = true;
      restoreChatState();
    }
    if (state.chat.token) loadPublishTargets();
    if (state.chat.enabled && !state.chat.autoOpened) {
      // The global chat is the default entry point, not a drawer behind a
      // project picker.  It does not create a conversation by itself: the
      // conversation starts with the first message.
      state.chat.autoOpened = true;
      openChatAsDefaultEntry();
    }
  }

  function restoreChatState() {
    if (!state.chat.enabled) return;
    try {
      state.chat.token = window.sessionStorage.getItem("ui1-chat-token") || "";
      state.chat.projectId = window.sessionStorage.getItem("ui1-chat-project") || null;
      state.chat.conversationId =
        window.sessionStorage.getItem("ui1-chat-conversation") || null;
    } catch (err) { /* ignore */ }
    $("chatToken").value = state.chat.token;
    syncChatProjects();
    if (state.chat.token) {
      loadPublishTargets();
      loadChatConversations(state.chat.conversationId).catch(function () { /* rendered */ });
    }
  }

  /**
   * Open the chat as the default entry point.
   *
   * The global LD chat is the primary interaction surface: an operator opens the
   * dashboard and talks to it without picking a project first.  It is only
   * opened automatically when chat is enabled and a token is already available
   * (the token is never fabricated or asked for by a popup).
   */
  function openChatAsDefaultEntry() {
    if (!state.chat.enabled) return;
    $("chatPanel").hidden = false;
    renderChatConnection();
    if (state.chat.connected) $("chatInput").focus();
    else $("chatToken").focus();
  }

  // ------------------------------------------------------------------ //
  // data flow
  // ------------------------------------------------------------------ //
  function refresh(force) {
    if (state.loading) return;
    state.loading = true;
    hideBanner();
    if (!state.overview) setViewState(loadingNode("正在读取账本…"));

    const overviewPromise = getJson("/api/overview");
    const detailPromise = state.selectedProjectId
      ? getJson("/api/projects/" + encodeURIComponent(state.selectedProjectId))
      : Promise.resolve(null);

    Promise.all([overviewPromise, detailPromise]).then(function (results) {
      state.overview = results[0];
      state.detail = results[1];
      state.connection = "ok";
      state.ledgerError = null;
      state.lastRefreshAt = new Date();
      setViewState(null);
      renderAll();
      updateFooter();
    }).catch(function (error) {
      handleFailure(error);
    }).then(function () {
      state.loading = false;
      if (force) updateFooter();
    });
  }

  function handleFailure(error) {
    const code = error.code || "";
    if (code === "project_not_found") {
      state.selectedProjectId = null;
      state.detail = null;
      banner("所选项目已不存在", "warn", "已回到总览。");
      state.loading = false;
      refresh(true);
      return;
    }
    if (["ledger_missing", "ledger_unreadable", "schema_incompatible",
         "ledger_busy"].indexOf(code) >= 0) {
      state.connection = "ledger-error";
      state.ledgerError = { code: code, message: error.message };
      state.overview = null;
      state.detail = null;
      renderSummary();
      renderNav();
      $("detailView").hidden = true;
      $("overviewView").hidden = false;
      clear($("overviewView"));
      banner("账本（Ledger）读取失败：" + error.message, "bad", "（" + code + "）");
      setViewState(ledgerErrorCard(code, error.message));
      updateFooter();
      return;
    }
    state.connection = "api-down";
    banner("接口（API）连接中断", "bad", error.message || "无法访问本地 Dashboard 服务。");
    if (!state.overview) {
      setViewState(emptyState("无法连接 Dashboard 服务",
        "请确认 dashboard_server.py 仍在运行，并检查终端日志。已保留上一次成功加载的数据。"));
    }
    updateFooter();
  }

  function renderAll() {
    renderSummary();
    renderNav();
    if (state.selectedProjectId) {
      $("overviewView").hidden = true;
      $("detailView").hidden = false;
      renderDetail();
    } else {
      $("detailView").hidden = true;
      $("overviewView").hidden = false;
      renderOverview();
    }
    if (state.chat.enabled) {
      syncChatProjects();
      if (!state.chat.restored) {
        state.chat.restored = true;
        restoreChatState();
      }
      renderPublishNotice();
    }
  }

  function updateFooter() {
    $("lastRefresh").textContent = state.lastRefreshAt
      ? "刷新于 " + fmtTime(state.lastRefreshAt.getTime() / 1000)
      : "尚未刷新";
    if (state.connection === "ok" && state.overview) {
      const ledger = state.overview.ledger || {};
      $("ledgerInfo").textContent = "账本可读 · " + num(ledger.projects) + " 个项目" +
        (ledger.has_goals_table ? "" : " · 无目标表");
    } else if (state.connection === "api-down") {
      $("ledgerInfo").textContent = "接口不可达";
    } else if (state.connection === "ledger-error" && state.ledgerError) {
      $("ledgerInfo").textContent = "账本错误：" + state.ledgerError.code;
    } else {
      $("ledgerInfo").textContent = "—";
    }
  }

  // ------------------------------------------------------------------ //
  // selection / hash routing
  // ------------------------------------------------------------------ //
  function writeHash(projectId) {
    if (!window.history || !window.history.replaceState) return;
    const hash = projectId ? "#project=" + encodeURIComponent(projectId) : "#";
    window.history.replaceState(null, "", hash);
  }

  function readHash() {
    const hash = window.location.hash || "";
    const match = /^#project=([^&]+)$/.exec(hash);
    return match ? decodeURIComponent(match[1]) : null;
  }

  function selectProject(projectId) {
    state.selectedProjectId = projectId || null;
    state.detail = null;
    state.goalFilter = "all";
    state.taskFilter = "all";
    writeHash(state.selectedProjectId);
    // Viewing another project never moves the LD Chat target, but it must make
    // the difference visible instead of leaving it implicit.
    renderPublishNotice();
    if (projectId) setViewState(loadingNode("正在加载项目详情…"));
    refresh(true);
  }

  // ------------------------------------------------------------------ //
  // timer + theme
  // ------------------------------------------------------------------ //
  function startTimer() {
    stopTimer();
    if (!state.autoRefresh) return;
    state.timer = setInterval(function () {
      if (document.hidden) return;
      refresh(false);
    }, REFRESH_MS);
  }

  function stopTimer() {
    if (state.timer) clearInterval(state.timer);
    state.timer = null;
  }

  function applyTheme(mode) {
    document.documentElement.setAttribute("data-theme", mode);
    try { window.localStorage.setItem("ui0-theme", mode); } catch (err) { /* ignore */ }
  }

  function cycleTheme() {
    const current = document.documentElement.getAttribute("data-theme") || "auto";
    const next = current === "auto" ? "dark" : current === "dark" ? "light" : "auto";
    applyTheme(next);
    $("themeBtn").textContent = "主题：" + I18N.theme(next);
    $("themeBtn").title = "当前主题：" + I18N.theme(next) + "（点击切换）";
  }

  // ------------------------------------------------------------------ //
  // bootstrap
  // ------------------------------------------------------------------ //
  function init() {
    let saved = null;
    try { saved = window.localStorage.getItem("ui0-theme"); } catch (err) { saved = null; }
    applyTheme(saved || "auto");

    $("refreshBtn").addEventListener("click", function () { refresh(true); });
    $("themeBtn").addEventListener("click", cycleTheme);
    $("autoRefresh").addEventListener("change", function (event) {
      state.autoRefresh = event.target.checked;
      if (state.autoRefresh) startTimer(); else stopTimer();
    });

    $("chatToggle").addEventListener("click", function () {
      $("chatPanel").hidden = false;
      renderChatConnection();
      if (state.chat.connected) $("chatInput").focus();
      else $("chatToken").focus();
    });
    $("chatClose").addEventListener("click", function () {
      $("chatPanel").hidden = true;
    });
    $("chatConnect").addEventListener("click", connectChat);
    $("publishBtn").addEventListener("click", publishObjective);
    $("publishObjective").addEventListener("keydown", function (event) {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        publishObjective();
      }
    });
    $("chatToken").addEventListener("keydown", function (event) {
      if (event.key === "Enter") connectChat();
    });
    $("chatProject").addEventListener("change", function (event) {
      // This selects the *publication target* only.  It never rebinds the
      // conversation and never starts a new one: switching what you are talking
      // about is a matter of what you say, not of a dropdown.
      state.chat.projectId = event.target.value || null;
      // A confirmation rendered for the previous project must never be reusable.
      resetPublishConfirm();
      publishStatus("");
      chatSaveSession();
      // The publish button is only usable with an explicit target.
      $("publishBtn").disabled = state.chat.publishing || !state.chat.projectId;
      renderIdentifiedProject();
      renderPublishNotice();
      loadPublishTargets();
    });

    $("goalHandlingClose").addEventListener("click", closeGoalHandling);
    $("goalHandlingStage").addEventListener("click", stageGoalResume);
    $("goalHandlingConfirmBtn").addEventListener("click", confirmGoalResume);
    $("goalHandlingCancelBtn").addEventListener("click", cancelGoalResume);
    $("goalHandling").addEventListener("click", function (event) {
      if (event.target === $("goalHandling")) closeGoalHandling();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !$("goalHandling").hidden) closeGoalHandling();
    });
    $("chatConversation").addEventListener("change", function (event) {
      loadChatConversation(event.target.value || null).catch(function () { /* rendered */ });
    });
    $("chatNew").addEventListener("click", newChatConversation);
    $("chatSend").addEventListener("click", function () { sendChatMessage(); });
    $("chatConfirm").addEventListener("click", function () {
      sendChatMessage("confirm");
    });
    $("chatCancel").addEventListener("click", function () {
      sendChatMessage("cancel");
    });
    $("chatInput").addEventListener("keydown", function (event) {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        sendChatMessage();
      }
    });

    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && state.autoRefresh) refresh(false);
    });

    window.addEventListener("hashchange", function () {
      const wanted = readHash();
      if (wanted !== state.selectedProjectId) {
        state.selectedProjectId = wanted;
        state.detail = null;
        refresh(true);
      }
    });

    const fromHash = readHash();
    if (fromHash) state.selectedProjectId = fromHash;

    getJson("/api/health").then(configureChatFromHealth).catch(function () {
      configureChatFromHealth({ chat_enabled: false });
    });
    refresh(true);
    startTimer();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
