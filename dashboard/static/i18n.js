/* Dashboard 中文展示层（zh-CN）。
 *
 * 这是**只读的展示映射**，不是协议的一部分：
 *   - 状态判断、筛选、跳转、二次确认、ID 校验一律仍用原始枚举值；
 *   - 本文件只负责把枚举/字段转成给人看的文字，从不改写数据；
 *   - 没有大型 i18n 框架，只有一个显式的查表 + 一个"未知状态"回退。
 *
 * 关键区分（题面明确要求）：同一个字符串在不同字段里含义不同，
 * 例如 `succeeded` 是 WorkItem 的"执行成功"，不是"验收通过"；
 * `verified` 才是 Verifier 的验收结论。所以映射按**字段语境**分开，
 * 不做一张万能表。
 */
(function (global) {
  "use strict";

  // ---- 字段级映射 ----------------------------------------------------- //
  //: 工作目标（Goal）的状态。
  const GOAL_STATUS = {
    active: "进行中",
    completed: "已完成",
    needs_human: "待人工处理",
    failed: "执行失败",
    cancelled: "已取消",
    waiting: "等待中",
    paused: "已暂停",
  };

  //: 工作项目（Project）的启用状态。active 在这里是"已启用"而非"进行中"。
  const PROJECT_STATUS = {
    active: "已启用",
    inactive: "已停用",
  };

  //: 工作任务（WorkItem）的执行状态——描述 Worker 有没有跑完，不代表验收。
  const TASK_STATE = {
    queued: "排队中",
    starting: "正在启动",
    running: "运行中",
    dispatching: "派发中",
    waiting_for_approval: "等待审批",
    succeeded: "执行成功",
    failed: "执行失败",
    cancelled: "已取消",
    lost: "已失联",
    dispatch_unknown: "派发状态不明",
    unknown: "状态未知",
  };

  //: 验收结论（Verifier 的 verdict）。与执行状态是两件事。
  const VERDICT = {
    verified: "验收通过",
    rejected: "验收未通过",
    inconclusive: "结果不确定",
    pending: "尚未验收",
  };

  //: 队列状态。
  const QUEUE_STATUS = {
    ready: "可派发",
    running: "已派发",
    complete: "已完成",
    needs_approval: "待审批",
    needs_human: "待人工处理",
    checkpoint_unprotected: "检查点未保护",
    blocked: "已阻塞",
    cancelled: "已取消",
  };

  //: 人工介入标记。permission_blocked 与 worker_not_authenticated 的补救方式不同。
  const ATTENTION_FLAG = {
    permission_blocked: "执行权限被拒绝",
    worker_not_authenticated: "Worker 未登录",
    needs_approval: "待审批",
    dispatch_unknown: "派发状态不明",
    suspected_lost: "疑似失联",
    session_binding_conflict: "会话绑定冲突",
    needs_human: "待人工处理",
  };

  //: 工作区绑定状态。
  const BINDING_STATUS = {
    bound: "已绑定",
    unbound: "未绑定",
    released: "已释放",
    conflict: "冲突",
    unknown: "未知",
  };

  //: Project Memory 的验证等级。
  const VERIFICATION = {
    verified: "已验证",
    candidate: "待确认",
    unverified: "未验证",
  };

  //: ContextEntry 类型。
  const ENTRY_KIND = {
    background: "背景",
    constraints: "约束",
    progress: "进展",
    decision: "决策",
    open_question: "待解决问题",
    note: "备注",
  };

  //: 安全域。
  const SECURITY_DOMAIN = {
    local: "本地",
    shareable: "可共享",
  };

  //: 聊天消息角色 / 意图来源。
  const CHAT_ROLE = {
    user: "我",
    ld: "LD",
    system: "系统",
  };

  const INTENT_SOURCE = {
    model: "模型判定",
    deterministic: "本地确定性路由",
    fallback: "回退路由",
  };

  //: 主题。
  const THEME_LABEL = { auto: "自动", light: "浅色", dark: "深色" };

  const TABLES = {
    goalStatus: GOAL_STATUS,
    projectStatus: PROJECT_STATUS,
    taskState: TASK_STATE,
    verdict: VERDICT,
    queueStatus: QUEUE_STATUS,
    attentionFlag: ATTENTION_FLAG,
    bindingStatus: BINDING_STATUS,
    verification: VERIFICATION,
    entryKind: ENTRY_KIND,
    securityDomain: SECURITY_DOMAIN,
    chatRole: CHAT_ROLE,
    intentSource: INTENT_SOURCE,
  };

  const UNKNOWN_SUFFIX = "（未知状态）";

  /**
   * 按语境翻译一个枚举值。
   *
   * 返回 `{ text, known, raw }`：
   *   - `known === false` 时 `text` 是“原始值（未知状态）”，绝不猜成某个已知类别；
   *   - `raw` 始终是原始值，调用方可原样展示或保留在 title 里。
   *
   * 空值统一显示 em dash，不算未知状态。
   */
  function translate(kind, value) {
    const raw = (value === null || value === undefined) ? "" : String(value);
    if (!raw) return { text: "—", known: true, raw: "" };
    const table = TABLES[kind];
    if (!table) return { text: raw, known: false, raw: raw };
    // 精确匹配优先（枚举是大小写敏感的）；未命中再试小写，容忍后端大小写漂移。
    if (Object.prototype.hasOwnProperty.call(table, raw)) {
      return { text: table[raw], known: true, raw: raw };
    }
    const lower = raw.toLowerCase();
    if (Object.prototype.hasOwnProperty.call(table, lower)) {
      return { text: table[lower], known: true, raw: raw };
    }
    return { text: raw + UNKNOWN_SUFFIX, known: false, raw: raw };
  }

  /** 只要文字，最常用的形式。 */
  function t(kind, value) {
    return translate(kind, value).text;
  }

  // ---- 复合/带标签的展示 ---------------------------------------------- //
  //: 首次出现时给出中文含义；技术标识本身（Goal/WorkItem/…) 保持原样。
  const TERMS = {
    Goal: "工作目标（Goal）",
    WorkItem: "工作任务（WorkItem）",
    Project: "工作项目（Project）",
    LD: "调度器（LD）",
    Agent: "执行代理（Agent）",
    Ledger: "账本（Ledger）",
    Verifier: "验收器（Verifier）",
    Worker: "执行器（Worker）",
    SHA: "提交号（SHA）",
  };

  /** 首次出现时展开术语，之后只写标识本身。 */
  function term(name, first) {
    if (first && TERMS[name]) return TERMS[name];
    return name;
  }

  /** 计数单位：`3 个工作任务`。 */
  function count(n, unit) {
    const value = (typeof n === "number") ? n : Number(n) || 0;
    return value + " " + unit;
  }

  /** 中文来源说明：`模型判定 / LD`。 */
  function intentSource(value) {
    if (!value) return "";
    return translate("intentSource", value).text;
  }

  /** 主题按钮文字。 */
  function theme(value) {
    const raw = String(value || "auto").toLowerCase();
    return THEME_LABEL[raw] || raw;
  }

  global.ACPI18N = {
    GOAL_STATUS: GOAL_STATUS,
    PROJECT_STATUS: PROJECT_STATUS,
    TASK_STATE: TASK_STATE,
    VERDICT: VERDICT,
    QUEUE_STATUS: QUEUE_STATUS,
    ATTENTION_FLAG: ATTENTION_FLAG,
    translate: translate,
    t: t,
    term: term,
    count: count,
    intentSource: intentSource,
    theme: theme,
    TERMS: TERMS,
    UNKNOWN_SUFFIX: UNKNOWN_SUFFIX,
  };
})(window);
