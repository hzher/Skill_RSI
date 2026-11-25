"use strict";

const $ = (id) => document.getElementById(id);
const labels = { dsh: "DSH", codex: "Codex", claude_code: "Claude Code", api: "已配置 LLM" };
const initials = { dsh: "D", codex: "C", claude_code: "A" };
const descriptions = {
  dsh: "让 DSH 的主调用按需读取工具结果，保留可展开的原文。",
  codex: "连接 Codex 的项目回调与 MCP 工具，按项目选择扩展。",
  claude_code: "连接 Claude Code 的工具与会话回调，管理当前项目的扩展。",
};
const policyDescriptions = {
  "tool-result-view": ["工具结果按需展开", "从长日志中取出错误附近的片段，需要时取回原文。", "≋"],
  "constraint-check": ["旧约束核验", "使用已记录的配置值前，核对来源文件与当前版本。", "⌁"],
};
const builtinUsage = {
  "tool-result-view": {
    category: "上下文整理", use_when: "成功的文本工具结果至少 60 行，并含有 error、failed 等错误词时，聚焦错误附近内容。",
    skip_when: "结果较短、没有相关焦点、读取失败或无法保留可展开的原文时跳过。",
  },
  "constraint-check": {
    category: "约束核验", use_when: "已经记录过某个字段的来源，下一次操作明确依赖该字段时，核对当前文件版本和值。",
    skip_when: "缺少明确的字段依赖、没有先前核验记录，或当前版本已经可见时跳过。",
  },
};
const gapLabels = {
  native_cli_unavailable: "尚未找到 CLI。填写本机可执行文件的完整路径，再保存接入。",
  native_cli_version_unavailable: "CLI 未返回版本，请在终端确认它可以启动。",
  awaiting_native_callback: "扩展配置已保存，等待宿主回调确认扩展加载。",
  registration_update_required: "当前安装包已更新，点击「保存并接入」刷新宿主配置。",
  native_registration_platform_unverified: "当前操作系统的原生注册仍待验证。",
};
const statusLabels = {
  queued: "等待执行", starting: "正在启动", running: "执行中", completed: "已完成", failed: "执行失败",
  cancelled: "已停止", canceled: "已停止", cancelling: "正在停止", interrupted: "已中断", unknown: "状态待确认", observing: "记录中",
  pending: "等待宿主确认", effective: "已生效", disabled: "已停用", rejected: "能力未匹配",
};
const hostOrder = ["dsh", "codex", "claude_code"];
const tokenHash = window.location.hash.slice(1);
const token = tokenHash.startsWith("token=") ? new URLSearchParams(tokenHash).get("token") : tokenHash;
let state = null;
let project = new URLSearchParams(window.location.search).get("project") || "";
let host = new URLSearchParams(window.location.search).get("host") || "codex";
let extensions = [];
let selectedTask = null;
let initialTask = new URLSearchParams(window.location.search).get("task");
let taskDetail = null;
let outputTab = "stdout";
let dirty = false;
let busy = false;
let loading = false;
let detailLoading = false;
let noticeTimer = null;
let generation = 0;
let desktopMessage = "";
const analyses = new Map();
const emergences = new Map();
let analysisLoading = false;
let emergenceLoading = false;
let emergenceSettingsDirty = false;
let emergenceSettingsLoading = false;
let emergenceCommand = "";
let llmSettings = null;
let llmDirty = false;
let llmLoading = false;
let codexHomeDirty = false;
let codexRecordingProject = "";

function element(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined && value !== null) node.textContent = String(value);
  return node;
}

function announce(text, error = false) {
  clearTimeout(noticeTimer);
  $("message").textContent = text;
  $("message").classList.toggle("error", error);
  $("message").hidden = false;
  if (!error) noticeTimer = setTimeout(() => { $("message").hidden = true; }, 5500);
}

async function api(path, data) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 60000);
  try {
    const response = await fetch(path, {
      method: data === undefined ? "GET" : "POST",
      cache: "no-store",
      credentials: "same-origin",
      headers: {
        "X-Skill-RSI-Token": token || "",
        ...(data === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: data === undefined ? undefined : JSON.stringify(data),
      signal: controller.signal,
    });
    const result = await response.json();
    if (!response.ok) {
      const error = result.error || result.message || `请求失败（${response.status}）`;
      throw new Error(typeof error === "string" ? error : JSON.stringify(error));
    }
    return result;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("本机服务响应超时，请刷新状态后重试。");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function markDirty() {
  dirty = true;
  updateControls();
}

function updateControls() {
  const configured = state?.hosts?.find((entry) => entry.id === host)?.configured;
  const desktop = host === "dsh" && $("connection-mode").value === "desktop";
  $("save").disabled = busy || loading || !project;
  $("save").textContent = busy ? "正在应用…" : desktop ? "接入桌面端 ↗" : dirty ? "应用更改 ↗" : "保存并接入 ↗";
  $("start").disabled = busy || loading || dirty || !project || !configured || desktop;
  const activeAnalysis = selectedTask && analysisRunning(analyses.get(selectedTask));
  $("analyze").disabled = busy || !selectedTask || activeAnalysis;
  $("analyze").textContent = activeAnalysis ? "正在分析…" : "运行高级分析";
  const emergence = selectedTask && emergences.get(selectedTask);
  const activeEmergence = emergenceRunning(emergence);
  $("emerge").disabled = busy || !selectedTask || activeEmergence;
  $("emerge").textContent = emergenceFinalizing(emergence) ? "正在校验打包…" : activeEmergence ? "Codex 正在分析…" : "一键分析生成";
  $("save-emergence-settings").disabled = busy || emergenceSettingsLoading;
  $("analysis-method").disabled = busy || activeAnalysis;
  $("analysis-host").disabled = busy || activeAnalysis;
  $("analysis-host-field").hidden = $("analysis-method").value !== "llm";
  const apiLLM = $("analysis-method").value === "llm" && $("analysis-host").value === "api";
  $("analysis-hint").textContent = $("analysis-method").value !== "llm"
    ? "在本机按已知规则检查记录，满足条件时生成候选扩展。"
    : apiLLM ? "调用已配置的 LLM 分析任务记录，结果和候选扩展保存到本机。"
      : "通过所选框架调用已配置的模型，分析任务记录并生成候选扩展。";
  $("llm-status").hidden = !apiLLM;
  $("llm-settings").hidden = !apiLLM;
  $("save-llm").disabled = busy || llmLoading;
  $("reload-llm").disabled = busy || llmLoading;
  const nativeRecording = state?.config?.codex_recording || {};
  $("codex-record-start").disabled = busy || loading || !project;
  $("codex-record-start").textContent = nativeRecording.enabled ? "保存目录" : "开始记录新数据";
  $("codex-sync").disabled = busy || loading || !project || !nativeRecording.enabled;
  $("codex-record-stop").disabled = busy || loading || !project;
  $("run-form").hidden = desktop;
  $("desktop-task-note").hidden = !desktop;
  $("desktop-note").hidden = !desktop;
  $("profile-field").hidden = host !== "dsh" || desktop;
  $("run-title").textContent = desktop ? "在桌面端运行任务" : "开始一个任务";
  if (desktop) $("desktop-note").textContent = desktopMessage || "接入后重新打开 DSH 桌面端，在当前项目中的会话活动会自动记录。";
  $("refresh").disabled = busy || loading;
  $("show-import").disabled = busy || loading;
  for (const control of document.querySelectorAll("#extensions input, #extensions select")) control.disabled = busy;
  $("run-hint").textContent = dirty ? "先应用配置，再启动任务。"
    : configured ? "执行时会调用你已配置的模型。" : "先保存并接入这个项目。";
  $("extension-footer").textContent = desktop ? "选择模式后，点击「接入桌面端」应用更改。"
    : dirty ? "有待应用的更改，点击上方「应用更改」。" : "选择模式后，点击「保存并接入」应用更改。";
}

function renderHosts() {
  const list = $("hosts");
  list.replaceChildren();
  for (const id of hostOrder) {
    const item = state?.hosts?.find((entry) => entry.id === id) || {};
    const button = element("button", `host-button${host === id ? " active" : ""}`);
    button.type = "button";
    button.setAttribute("aria-current", host === id ? "page" : "false");
    const found = item.command && !(item.missing_reasons || []).includes("native_cli_unavailable");
    button.append(element("span", "host-initial", initials[id]), element("span", "", labels[id]), element("span", `dot${found ? " ready" : ""}`));
    button.addEventListener("click", () => selectHost(id));
    list.append(button);
  }
}

function renderHeader() {
  $("heading").textContent = labels[host] || host;
  $("host-crumb").textContent = labels[host] || host;
  $("host-description").textContent = descriptions[host] || "管理当前项目的运行框架与扩展。";
  $("profile-field").hidden = host !== "dsh";
  $("connection-mode-field").hidden = host !== "dsh";
  $("codex-recording").hidden = host !== "codex";
  const current = state?.hosts?.find((entry) => entry.id === host) || {};
  const badge = $("connection-badge");
  const nativeRecording = host === "codex" ? state?.config?.codex_recording : null;
  const nativeRecorded = (nativeRecording?.session_count || 0) > 0;
  badge.className = "badge";
  if (nativeRecorded) {
    badge.textContent = "● 已记录本机会话";
    badge.classList.add("ready");
  } else if (nativeRecording?.enabled) {
    badge.textContent = nativeRecording.status === "initializing" ? "◌ 正在建立起录基线" : "◌ 等待新数据";
    badge.classList.add("waiting");
  } else if (current.loaded) {
    badge.textContent = "● 已收到宿主回调";
    badge.classList.add("ready");
  } else if (current.configured) {
    badge.textContent = "◌ 等待会话";
    badge.classList.add("waiting");
  } else {
    badge.textContent = current.command ? "待接入项目" : "待配置 CLI";
  }
  $("host-version").textContent = current.version || "CLI 版本待探测";
  $("connection-note").textContent = nativeRecorded ? "原生轨迹已同步，扩展加载以宿主回调为准。"
    : nativeRecording?.enabled ? "已开启新数据记录，等待 CLI 或桌面会话的新增内容。扩展加载以宿主回调为准。"
    : current.loaded ? "已观察到原生回调，扩展会在各自的触发点工作。" : "保存配置后，新的宿主会话会加载项目扩展。";
  const enabled = extensions.filter((item) => item.enabled);
  $("binding-note").textContent = `${enabled.length} 个扩展已选择`;
  const gaps = current.missing_reasons || [];
  $("host-gaps").hidden = gaps.length === 0;
  $("host-gaps").textContent = gaps.map((item) => gapLabels[item] || item).join("\n");
}

function extensionName(item) {
  return item.id || item.component_id || "extension";
}

function disclosureState(container) {
  return new Map([...container.querySelectorAll("details")].map((detail) => [
    detail.dataset.disclosureKey || detail.querySelector("summary")?.textContent, detail.open,
  ]));
}

function restoreDisclosures(container, states) {
  for (const detail of container.querySelectorAll("details")) {
    const key = detail.dataset.disclosureKey || detail.querySelector("summary")?.textContent;
    if (states.has(key)) detail.open = states.get(key);
  }
}

function renderExtensions() {
  const list = $("extensions");
  const disclosures = disclosureState(list);
  list.replaceChildren();
  $("extension-count").textContent = extensions.length;
  if (!extensions.length) {
    list.append(element("div", "empty", "添加一个内置策略，或导入团队发布的扩展包。"));
    return;
  }
  for (const item of extensions) {
    const id = extensionName(item);
    const [defaultName, description, icon] = policyDescriptions[id] || [id, "功能说明待补全", "◇"];
    const name = item.display_name || item.name || defaultName;
    const row = element("div", "extension-row");
    row.append(element("span", "extension-icon", icon));
    const body = element("div", "extension-body");
    const title = element("div", "extension-title");
    title.append(element("span", "extension-name", name), element("span", "version mono", item.version ? `v${item.version}` : ""));
    title.append(element("span", "extension-category", item.category || builtinUsage[id]?.category || "分类待补全"));
    const controls = element("div", "extension-controls");
    const select = element("select");
    select.setAttribute("aria-label", `${name}的运行模式`);
    const modes = [["shadow", "试跑 shadow"], ["suggest", "提示 suggest"], ["apply", "执行 apply"]];
    for (const [value, label] of modes) {
      if (host === "dsh" && value === "suggest") continue;
      if (item.modes && !item.modes.includes(value)) continue;
      const option = element("option", "", label);
      option.value = value;
      select.append(option);
    }
    select.value = item.mode || "shadow";
    select.addEventListener("change", () => { item.mode = select.value; markDirty(); });
    const status = item.enabled ? statusLabels[item.status] || item.status || "待应用" : "未启用";
    controls.append(select, element("span", `extension-status${item.enabled && item.status === "effective" ? " active" : ""}`, status));
    body.append(title, element("p", "extension-description", item.description || item.purpose || description), controls);
    if (item.error) body.append(element("p", "small analysis-error", outputText(item.error)));
    body.append(extensionDetails(item, true));
    row.append(body);
    const toggle = element("label", "switch");
    const checkbox = element("input");
    checkbox.type = "checkbox";
    checkbox.checked = Boolean(item.enabled);
    checkbox.setAttribute("aria-label", `启用${name} ${item.version || ""}`);
    checkbox.addEventListener("change", () => {
      item.enabled = checkbox.checked;
      if (item.enabled) {
        for (const other of extensions) {
          if (other !== item && extensionName(other) === id) other.enabled = false;
        }
      }
      markDirty();
      renderExtensions();
      renderHeader();
    });
    toggle.append(checkbox, element("span", "switch-track"));
    row.append(toggle);
    list.append(row);
  }
  restoreDisclosures(list, disclosures);
}

function taskId(task) { return task.task_id || task.id; }
function taskStatus(task) { return task.status || "unknown"; }
function isRunning(task) {
  return Boolean(task.cleanup_pending) || ["queued", "starting", "running", "cancelling"].includes(taskStatus(task));
}

function dateLabel(value) {
  if (!value) return "";
  const date = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
  return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}

function projectLabel(path) {
  if (!path) return "项目待识别";
  return path.replace(/[\\/]+$/, "").split(/[\\/]/).slice(-2).join("/");
}

function recordingStatus(recording) {
  if (!recording.enabled) return "未开启";
  const statuses = { syncing: "正在检查新数据", running: "正在记录新数据", collecting: "正在读取新增记录", initializing: "正在建立起录基线", new_only: "只记录新数据", waiting_data: "等待新数据", ready: "等待新数据", caught_up: "等待新数据", missing_source: "未找到会话目录", watching: "自动记录新数据", idle: "等待新数据", enabled: "已开启新数据记录", error: "记录遇到问题", failed: "记录遇到问题" };
  return recording.last_error ? "同步遇到问题" : statuses[recording.status] || "自动记录中";
}

function renderCodexRecording() {
  if (host !== "codex") return;
  const recording = state?.config?.codex_recording || {};
  if (project !== codexRecordingProject) {
    codexRecordingProject = project;
    codexHomeDirty = false;
  }
  if (!codexHomeDirty) $("codex-home").value = recording.codex_home || "~/.codex";
  $("codex-recording-status").textContent = recordingStatus(recording);
  const started = recording.recording_started_at || recording.started_at || recording.enabled_at;
  $("codex-recording-detail").textContent = `起录时间 ${dateLabel(started) || "—"} · 最近检查 ${dateLabel(recording.last_sync) || "—"} · 已录主会话 ${recording.session_count ?? 0}`;
  $("codex-record-stop").hidden = !recording.enabled;
  const error = recording.last_error || (recording.status === "missing_source" ? "未找到 sessions 或 archived_sessions，请核对 Codex 本机目录。" : "");
  $("codex-recording-error").hidden = !error;
  $("codex-recording-error").textContent = outputText(error);
}

function taskSource(task) {
  const source = task.origin || task.source;
  const value = typeof source === "string" ? source : source?.kind || source?.type;
  const names = { cli: "CLI", desktop: "桌面端", codex_cli: "CLI", codex_desktop: "桌面端", codex_rollout: "原生会话" };
  if (names[value]) return names[value];
  const originator = task.originator || task.source?.originator;
  if (originator === "Codex Desktop") return "桌面端";
  return typeof value === "string" && value.length < 80 ? names[value] || value : "";
}

function taskActivity(task) {
  const value = task.last_native_event_at || task.updated_at || task.created_at || task.started_at;
  const time = typeof value === "number" ? (value < 1e12 ? value * 1000 : value) : Date.parse(value || "");
  return Number.isFinite(time) ? time : 0;
}

function renderTasks() {
  const list = $("tasks");
  const currentOnly = $("current-project-only").checked;
  const terms = $("task-search").value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const tasks = (state?.tasks || []).filter((task) => {
    if (task.host !== host) return false;
    if (currentOnly && task.project !== project) return false;
    const text = [task.title, task.session_title, task.prompt, task.project, task.host, labels[task.host],
      taskSource(task), outputText(task.origin), outputText(task.source), task.originator].join(" ").toLocaleLowerCase();
    return terms.every((term) => text.includes(term));
  }).sort((left, right) => taskActivity(right) - taskActivity(left));
  list.replaceChildren();
  $("task-count").textContent = tasks.length;
  if (!tasks.length) list.append(element("div", "empty", terms.length ? "没有匹配的任务，试试其他主题或目录。" : currentOnly
    ? "当前项目还没有任务。取消筛选可查看其他项目。" : "本机记录的任务会显示在这里。"));
  for (const task of tasks) {
    const id = taskId(task);
    const button = element("button", `task-row${selectedTask === id ? " selected" : ""}`);
    button.type = "button";
    button.append(element("span", `task-dot ${taskStatus(task)}`));
    const body = element("div", "task-label");
    const meta = element("div", "task-meta");
    meta.append(element("span", "", labels[task.host] || task.host), element("span", "", statusLabels[taskStatus(task)] || taskStatus(task)), element("span", "", dateLabel(taskActivity(task))));
    if (taskSource(task)) meta.append(element("span", "", taskSource(task)));
    const taskProject = element("div", "task-project mono", projectLabel(task.project));
    taskProject.title = task.project || "";
    body.append(element("p", "task-title", task.title || task.session_title || task.prompt || id), meta, taskProject);
    button.append(body, element("span", "task-arrow", "↗"));
    button.addEventListener("click", () => showTask(id));
    list.append(button);
  }
  $("tasks-dir").textContent = state?.tasks_dir || "—";
}

function outputText(value) {
  if (typeof value === "string") return value;
  if (value === undefined || value === null) return "";
  return JSON.stringify(value, null, 2);
}

function analysisRunning(result) {
  return result && ["starting", "queued", "running", "cancelling"].includes(result.status);
}

function emergenceRunning(result) {
  return analysisRunning(result) || ["finalizing", "validating", "packaging"].includes(result?.status);
}

function emergenceFinalizing(result) {
  return emergenceRunning(result) && (result.worker_completed || result.worker_status === "completed"
    || result.job?.status === "completed" || ["finalizing", "validating", "packaging"].includes(result.phase || result.status));
}

function candidateInvalid(candidate) {
  return Boolean(candidate.error) || ["invalid", "failed", "rejected", "error"].includes(candidate.status || candidate.validation_status);
}

function renderAnalysis(container, result) {
  const box = element("section", "analysis-box");
  box.setAttribute("aria-live", "polite");
  const candidate = result.artifact_path;
  const heading = candidate ? "已生成候选扩展" : analysisRunning(result) ? "正在分析任务记录"
    : result.status === "failed" ? "分析失败" : "本次分析";
  box.classList.toggle("failed", result.status === "failed");
  box.append(element("h3", "", heading));
  if (result.summary) box.append(element("p", "small", outputText(result.summary)));
  if (result.reason) box.append(element("p", "small muted", outputText(result.reason)));
  if (result.error) box.append(element("p", "small analysis-error", outputText(result.error)));
  if (result.progress_error) box.append(element("p", "small analysis-error", result.progress_error));
  if (result.diagnostics) {
    const diagnostics = element("details", "analysis-diagnostics");
    diagnostics.open = result.status === "failed";
    diagnostics.append(element("summary", "small", "分析诊断"), element("pre", "output", outputText(result.diagnostics)));
    box.append(diagnostics);
  }
  const job = result.analysis_job;
  if (job && typeof job === "object") {
    const engine = result.backend === "http" ? `分析模型 ${result.model || "已配置 LLM"}` : `分析框架 ${labels[job.host] || job.host || "—"}`;
    box.append(element("p", "small muted", `${engine} · ${statusLabels[job.status] || job.status || "—"}`));
    if (job.path) {
      const jobPath = element("button", "path-button");
      jobPath.type = "button";
      jobPath.append(element("span", "mono small", job.path), element("span", "", "⧉"));
      jobPath.addEventListener("click", () => copyText(job.path));
      box.append(jobPath);
    }
  }
  if (candidate) {
    box.append(element("p", "small muted", "候选待验收。安装后，选择运行模式并显式启用，才能用于后续任务。"));
    const path = element("button", "path-button");
    path.type = "button";
    path.title = "复制候选扩展路径";
    path.append(element("span", "mono small", candidate), element("span", "", "⧉"));
    path.addEventListener("click", () => copyText(candidate));
    const install = element("button", "button secondary compact", "安装候选");
    install.type = "button";
    install.disabled = busy;
    install.addEventListener("click", () => perform(async () => {
      await api("/api/install", { source: candidate });
      await loadState({ preserve: true });
      announce("候选已安装。请在扩展列表中选择模式、打开开关并应用配置。");
    }));
    box.append(path, install);
  }
  if (result.analysis_path) {
    const report = element("button", "path-button small");
    report.type = "button";
    report.title = "复制分析记录路径";
    report.append(element("span", "mono small", result.analysis_path), element("span", "", "⧉"));
    report.addEventListener("click", () => copyText(result.analysis_path));
    box.append(element("p", "small muted", "分析记录"), report);
  }
  container.append(box);
}

function artifactPathActions(path) {
  const row = element("div", "artifact-path");
  row.append(element("code", "artifact-path-value", path));
  const actions = element("div", "artifact-path-actions");
  const copy = element("button", "button quiet compact", "复制路径");
  copy.type = "button";
  copy.addEventListener("click", () => copyText(path));
  const open = element("button", "button quiet compact", "打开位置");
  open.type = "button";
  open.disabled = busy;
  open.addEventListener("click", () => perform(async () => { await api("/api/open-path", { path }); }));
  actions.append(copy, open);
  row.append(actions);
  return row;
}

function extensionDetails(item, installed = false) {
  const detail = element("details", "extension-details");
  detail.dataset.disclosureKey = `${extensionName(item)}@${item.version}/${item.form}/${installed}`;
  detail.append(element("summary", "small", "适用范围与文件"));
  const usage = builtinUsage[extensionName(item)] || {};
  const hosts = item.hosts || item.scope?.hosts;
  const fields = element("dl", "extension-facts");
  for (const [label, value] of [
    ["身份", item.component_id || item.id], ["功能", item.purpose || item.description || policyDescriptions[extensionName(item)]?.[1]],
    ["适用场景", item.use_when || usage.use_when], ["跳过条件", item.skip_when || usage.skip_when],
    ["声明宿主", Array.isArray(hosts) ? hosts.map((name) => labels[name] || name).join("、") : hosts],
    ["校验状态", item.validation_status],
  ]) {
    const text = Array.isArray(value) ? value.map(outputText).join("\n") : outputText(value);
    fields.append(element("dt", "", label), element("dd", "", text || "待补全"));
  }
  detail.append(fields);
  const paths = [
    ["源码目录", item.source_path || (!installed ? item.path : null)],
    ["安装目录", item.storage_path || (installed ? item.path : null)],
    ["ZIP", item.artifact_path], ["分析报告", item.analysis_path || item.report_path],
  ].filter(([, path]) => path);
  for (const [label, path] of paths) detail.append(element("p", "small muted", label), artifactPathActions(path));
  if (!paths.length) detail.append(element("p", "small muted", installed ? "安装后可查看本机目录。" : "文件位置待补全"));
  if (item.readme) {
    const readme = element("details", "analysis-diagnostics");
    readme.dataset.disclosureKey = detail.dataset.disclosureKey + "/readme";
    readme.append(element("summary", "small", "阅读全文"), element("pre", "output emergence-report", outputText(item.readme)));
    detail.append(readme);
  }
  return detail;
}

function renderEmergence(container, result) {
  if (!result || result.status === "not_started") return;
  const box = element("section", "analysis-box emergence-result");
  box.setAttribute("aria-live", "polite");
  box.classList.toggle("failed", result.status === "failed");
  const candidates = result.candidates || [];
  const invalidCount = result.invalid_candidate_count ?? candidates.filter(candidateInvalid).length;
  const validCount = result.valid_candidate_count ?? candidates.length - invalidCount;
  const workerCompleted = result.worker_completed || result.worker_status === "completed" || result.job?.status === "completed";
  const title = emergenceFinalizing(result) ? "正在校验打包"
    : emergenceRunning(result) ? "Codex 正在读取轨迹并生成候选"
      : result.status === "failed" ? workerCompleted ? "模型已完成 · 收尾处理未通过" : "生成任务失败"
        : candidates.length ? `已生成候选 · 有效 ${validCount} 个，无效 ${invalidCount} 个` : `Codex 已完成 · 有效 ${validCount} 个，无效 ${invalidCount} 个`;
  box.append(element("h3", "", title));
  if (!emergenceRunning(result) && result.status === "failed") {
    box.append(element("p", "small muted", `有效候选 ${validCount} 个 · 无效候选 ${invalidCount} 个`));
  } else if (!emergenceRunning(result) && !candidates.length) {
    box.append(element("p", "small muted", "本次未生成候选，可在报告中查看分析结论。"));
  }
  if (result.summary) box.append(element("p", "small", outputText(result.summary)));
  if (result.error) box.append(element("p", result.status === "failed" ? "small analysis-error" : "small process-warning", outputText(result.error)));
  if (result.progress_error) box.append(element("p", "small analysis-error", result.progress_error));
  const activity = result.activity || result.process_diagnostics || {};
  const recent = activity.scope === "recent" || activity.log_truncated === true || result.log_truncated === true;
  const activityScope = recent ? "最近日志中的" : "";
  if (Number.isInteger(activity.commands_completed) || Number.isInteger(activity.commands_nonzero)) {
    const completed = activity.commands_completed ?? "—";
    const nonzero = activity.commands_nonzero ?? 0;
    box.append(element("p", "small process-activity", `${activityScope}过程命令已完成 ${completed} 次 · 非零退出 ${nonzero} 次`));
  }
  if (activity.commands_nonzero > 0) {
    const notice = element("details", "analysis-diagnostics process-warning");
    notice.dataset.disclosureKey = "emergence-command-exits";
    notice.append(element("summary", "small", "查看过程命令非零退出记录"));
    const latest = activity.latest_command_failure;
    if (latest) {
      const brief = typeof latest === "string" ? latest : latest.summary || latest.command || latest.message || outputText(latest);
      box.append(element("p", "small process-warning", `最近一次过程命令非零退出 ${outputText(brief).slice(0, 300)}`));
      notice.append(element("pre", "output", outputText(latest)));
    }
    notice.append(element("p", "small muted", "可在运行日志中查看这些命令的退出原因、重试和后续步骤。"));
    box.append(notice);
  }
  const warnings = activity.warnings || result.warnings;
  if (warnings?.length) {
    const notice = element("details", "analysis-diagnostics process-warning");
    notice.dataset.disclosureKey = "emergence-warnings";
    const values = Array.isArray(warnings) ? warnings : [warnings];
    notice.append(element("summary", "small", `运行提示 ${values.length} 条`), element("pre", "output", values.map(outputText).join("\n\n")));
    box.append(notice);
  }
  if (result.output_path) {
    box.append(element("p", "small muted", "本次生成目录"), artifactPathActions(result.output_path));
  }
  if (result.job) {
    const job = result.job;
    box.append(element("p", "small muted", `模型执行 ${statusLabels[job.status] || job.status || "状态待确认"}${job.model ? ` · ${job.model}` : ""}`));
  }
  if (result.diagnostics) {
    const diagnostics = element("details", "analysis-diagnostics");
    diagnostics.open = result.status === "failed";
    diagnostics.append(element("summary", "small", "运行日志与诊断"), element("pre", "output", outputText(result.diagnostics)));
    box.append(diagnostics);
  }
  if (result.report_path && (result.report_present || result.report)) box.append(element("p", "small muted", "REPORT.md"), artifactPathActions(result.report_path));
  if (result.report) {
    const report = element("details", "analysis-diagnostics");
    report.append(element("summary", "small", "阅读生成报告"), element("pre", "output emergence-report", outputText(result.report)));
    box.append(report);
  }
  if (candidates.length) box.append(element("p", "small muted", "请人工核对候选的功能、触发、跳过条件与效果。安装后，再手动选择宿主并启用。"));
  for (const candidate of candidates) {
    const card = element("article", "candidate-card");
    const heading = element("div", "candidate-heading");
    heading.append(element("h4", "", candidate.display_name || candidate.name || candidate.component_id || "候选扩展"), element("span", "small muted", `${candidate.form || "—"} · ${candidate.version || "—"}`));
    card.append(heading);
    card.append(element("p", "small candidate-category", candidate.category || "分类待补全"));
    card.append(element("p", "small", candidate.description || candidate.purpose || "功能说明待补全"));
    const validation = candidate.status === "valid" ? "valid" : candidate.status === "invalid" ? "invalid" : candidate.validation_status || "待校验";
    const invalid = ["invalid", "failed", "rejected", "error"].includes(String(validation).toLowerCase());
    const validity = { valid: "制品校验通过", validated: "制品校验通过", passed: "制品校验通过", invalid: "制品校验失败", failed: "制品校验失败", rejected: "制品校验失败", error: "制品校验失败" };
    card.append(element("p", invalid ? "small analysis-error" : "small muted", validity[validation] || outputText(validation)));
    if (candidate.error) card.append(element("p", "small analysis-error", outputText(candidate.error)));
    if (candidate.execution_note) card.append(element("p", "small muted", candidate.execution_note));
    if (candidate.readme) {
      const readme = outputText(candidate.readme);
      card.append(element("p", "small candidate-summary", readme.slice(0, 480)));
    }
    card.append(extensionDetails({ ...candidate, analysis_path: candidate.analysis_path || result.report_path }));
    if (candidate.artifact_path) {
      const install = element("button", "button secondary compact", "安装候选");
      install.type = "button";
      install.disabled = busy || invalid || Boolean(candidate.error);
      install.addEventListener("click", () => perform(async () => {
        await api("/api/install", { source: candidate.artifact_path });
        await loadState({ preserve: true });
        announce("候选已安装。请人工确认后，在目标宿主的扩展列表手动启用。");
      }));
      card.append(install);
    }
    box.append(card);
  }
  container.append(box);
}

function renderTaskDetail() {
  const container = $("task-detail");
  if (!taskDetail) {
    container.className = "detail-empty";
    container.replaceChildren(element("span", "detail-symbol", "⌘"), element("p", "", "从这里跟进任务"),
      element("span", "small muted", "启动新任务，或选择一条任务记录。"));
    $("cancel").hidden = true;
    return;
  }
  const task = taskDetail.task || taskDetail;
  const disclosures = container.dataset.taskId === taskId(task) ? disclosureState(container) : new Map();
  container.dataset.taskId = taskId(task);
  container.className = "task-detail-body";
  container.replaceChildren();
  container.append(element("p", "task-detail-heading", task.title || task.prompt || taskId(task)));
  const meta = element("div", "detail-metadata");
  meta.append(element("span", "", statusLabels[taskStatus(task)] || taskStatus(task)), element("span", "", labels[task.host] || task.host), element("span", "", dateLabel(task.started_at || task.created_at)));
  if (taskSource(task)) meta.append(element("span", "", taskSource(task)));
  container.append(meta);
  if (task.project) {
    const taskProject = element("p", "task-project mono", task.project);
    taskProject.title = task.project;
    container.append(taskProject);
  }
  if (Number.isInteger(task.subthread_count) && task.subthread_count > 0) {
    container.append(element("p", "small muted", `包含 ${task.subthread_count} 个内部子任务`));
  }
  const path = task.path || task.directory || "";
  if (path) {
    const copy = element("button", "path-button");
    copy.type = "button";
    copy.title = "复制本任务的轨迹目录";
    copy.append(element("span", "mono small", path), element("span", "", "⧉"));
    copy.addEventListener("click", () => copyText(path));
    container.append(copy);
  }
  const tabs = element("div", "output-tabs");
  const output = element("pre", "output");
  for (const [key, label] of [["stdout", "输出"], ["events", "轨迹事件"], ["stderr", "诊断"], ["files", "轨迹文件"]]) {
    const tab = element("button", `output-tab${outputTab === key ? " active" : ""}`, label);
    tab.type = "button";
    tab.addEventListener("click", () => { outputTab = key; renderTaskDetail(); });
    tabs.append(tab);
  }
  let value = taskDetail[outputTab] ?? task[outputTab];
  if (outputTab === "stderr") {
    value = [task.error, task.cleanup_error, task.missing_reason, taskDetail.stderr, taskDetail.worker_log]
      .filter(Boolean).join("\n\n");
  }
  if (outputTab === "files" && Array.isArray(value)) {
    value = value.map((entry) => typeof entry === "string" ? entry : entry.path || entry.name || JSON.stringify(entry)).join("\n");
  }
  output.textContent = outputText(value) || (isRunning(task) ? "等待框架返回记录…" : "暂无记录");
  container.append(tabs, output);
  const emergence = emergences.get(taskId(task)) || taskDetail.emergence;
  if (emergence) renderEmergence(container, emergence);
  const analysis = analyses.get(taskId(task)) || taskDetail.analysis || task.analysis;
  if (analysis) {
    const legacy = element("details", "legacy-result");
    legacy.append(element("summary", "small", "高级分析记录"));
    renderAnalysis(legacy, analysis);
    container.append(legacy);
  }
  $("cancel").hidden = !isRunning(task);
  $("cancel").disabled = busy || taskStatus(task) === "cancelling";
  restoreDisclosures(container, disclosures);
}

async function loadState({ preserve = false, quiet = false, refresh = false } = {}) {
  if (loading) return;
  loading = true;
  if (!quiet) updateControls();
  const currentGeneration = generation;
  const query = new URLSearchParams({ host });
  if (project) query.set("project", project);
  if (refresh) query.set("refresh", "1");
  try {
    const result = await api(`/api/state?${query}`);
    if (currentGeneration !== generation) return;
    state = result;
    project = result.project || project;
    host = result.host || host;
    if (document.activeElement !== $("project")) $("project").value = project;
    $("recent-projects").replaceChildren(...(state.projects || []).map((value) => {
      const option = element("option");
      option.value = typeof value === "string" ? value : value.path || value.project;
      return option;
    }));
    if (!preserve || !dirty) {
      extensions = (result.extensions || []).map((entry) => ({ ...entry, options: entry.options || {} }));
      const cfg = result.config || {};
      const active = result.hosts?.find((entry) => entry.id === host) || {};
      $("command").value = cfg.command || active.command || (host === "claude_code" ? "claude" : host);
      $("profile").value = cfg.profile || "sdk";
      $("connection-mode").value = cfg.connection_mode || (cfg.desktop_enabled ? "desktop" : "cli");
      $("observe").checked = cfg.observe !== false;
      $("advanced-config").value = JSON.stringify({
        mcp_paths: cfg.mcp_paths || [], checkpoint_paths: cfg.checkpoint_paths || [], constraints: cfg.constraints || [],
      }, null, 2);
      dirty = false;
      renderExtensions();
    } else {
      for (const item of result.extensions || []) {
        const current = extensions.find((entry) => extensionName(entry) === extensionName(item)
          && entry.version === item.version && entry.form === item.form);
        if (current) current.source = item.source;
        else extensions.push({ ...item, options: item.options || {} });
      }
      renderExtensions();
    }
    renderHosts();
    renderHeader();
    renderCodexRecording();
    renderTasks();
    renderCatalog();
  } catch (error) {
    if (!quiet) announce(error.message, true);
  } finally {
    loading = false;
    updateControls();
  }
}

async function selectHost(id) {
  if (host === id || busy) return;
  if (dirty && !window.confirm("当前配置尚未应用。切换框架并放弃这些更改？")) return;
  host = id;
  dirty = false;
  selectedTask = null;
  taskDetail = null;
  outputTab = "stdout";
  const location = new URL(window.location.href);
  location.searchParams.set("host", host);
  location.searchParams.delete("task");
  window.history.replaceState(null, "", location);
  renderTaskDetail();
  generation += 1;
  while (loading) await new Promise((resolve) => setTimeout(resolve, 50));
  await loadState();
}

function configuration() {
  let advanced;
  try { advanced = JSON.parse($("advanced-config").value || "{}"); }
  catch { throw new Error("项目文件与约束需要有效的 JSON，请检查逗号和引号。"); }
  if (!advanced || Array.isArray(advanced) || typeof advanced !== "object") throw new Error("项目配置需要一个 JSON 对象。");
  const result = {
    ...advanced, project, host, command: $("command").value.trim(), observe: $("observe").checked,
    extensions: extensions.filter((item) => item.enabled).map((item) => ({ source: item.source || item.path, mode: item.mode || "shadow", options: item.options || {} })),
  };
  if (host === "dsh") result.profile = $("profile").value.trim() || "sdk";
  if (host === "dsh") result.connection_mode = $("connection-mode").value;
  return result;
}

async function perform(action) {
  if (busy) return;
  busy = true;
  updateControls();
  try { await action(); }
  catch (error) { announce(error.message, true); }
  finally { busy = false; updateControls(); renderTaskDetail(); }
}

async function showTask(id, quiet = false) {
  if (detailLoading) return;
  detailLoading = true;
  const changed = selectedTask !== id;
  selectedTask = id;
  try {
    const detail = await api(`/api/task?${new URLSearchParams({ id })}`);
    if (selectedTask !== id) return;
    taskDetail = detail;
    const taskHost = (detail.task || detail).host;
    if (hostOrder.includes(taskHost) && taskHost !== host) {
      host = taskHost;
      generation += 1;
      await loadState();
      if (selectedTask !== id) return;
    }
    const location = new URL(window.location.href);
    location.searchParams.set("task", id);
    location.searchParams.set("host", host);
    window.history.replaceState(null, "", location);
    if (changed && taskStatus(taskDetail.task || taskDetail) === "observing") outputTab = "events";
    if (taskDetail.analysis_job && analysisRunning(taskDetail.analysis_job)) {
      analyses.set(id, { status: taskDetail.analysis_job.status, analysis_job: taskDetail.analysis_job });
    } else if (taskDetail.analysis && !analysisRunning(analyses.get(id))) {
      analyses.set(id, taskDetail.analysis);
    }
    renderTasks();
    renderTaskDetail();
    await refreshEmergence(id, quiet);
    if ($("legacy-analysis").open || analysisRunning(analyses.get(id))) await refreshAnalysis(id, quiet);
  } catch (error) { if (!quiet) announce(error.message, true); }
  finally { detailLoading = false; updateControls(); }
}

async function refreshEmergence(id, quiet = false) {
  if (emergenceLoading) return;
  emergenceLoading = true;
  try {
    const result = await api(`/api/emergence?${new URLSearchParams({ task_id: id })}`);
    if (result && result.status && result.status !== "not_started") emergences.set(id, result);
    if (id === selectedTask) renderTaskDetail();
  } catch (error) {
    if (emergenceRunning(emergences.get(id))) {
      emergences.set(id, { ...emergences.get(id), progress_error: `生成进度读取失败，正在重试。${error.message}` });
      if (id === selectedTask) renderTaskDetail();
    }
    if (!quiet) announce(error.message, true);
  } finally {
    emergenceLoading = false;
    updateControls();
  }
}

async function loadEmergenceSettings() {
  emergenceSettingsLoading = true;
  updateControls();
  try {
    const settings = await api("/api/emergence/settings");
    emergenceCommand = settings.command || "codex";
    if (!emergenceSettingsDirty) {
      $("emergence-command").value = settings.command || "codex";
      $("emergence-model").value = settings.model || "gpt-6.1-sol";
      $("emergence-effort").value = settings.effort || "ultra";
    }
    $("emergence-settings-status").textContent = "使用本机 Codex 已有的账号与模型服务配置。";
  } catch (error) {
    $("emergence-settings-status").textContent = `读取 Codex 生成设置失败。${error.message}`;
  } finally {
    emergenceSettingsLoading = false;
    updateControls();
  }
}

async function refreshAnalysis(id, quiet = false) {
  if (analysisLoading) return;
  analysisLoading = true;
  try {
    const result = await api(`/api/analysis?${new URLSearchParams({ task_id: id })}`);
    if (result && result.status && result.status !== "not_started") analyses.set(id, result);
    if (id === selectedTask) renderTaskDetail();
  } catch (error) {
    if (analysisRunning(analyses.get(id))) {
      analyses.set(id, { ...analyses.get(id), progress_error: `分析进度读取失败，正在重试。${error.message}` });
      if (id === selectedTask) renderTaskDetail();
    }
    if (!quiet) announce(error.message, true);
  } finally {
    analysisLoading = false;
    updateControls();
  }
}

function renderLLMStatus() {
  if (!llmSettings) return;
  const model = llmSettings.model || "模型未配置";
  const credential = llmSettings.has_api_key ? "密钥已配置" : "密钥待配置";
  $("llm-status").textContent = `${model} · ${credential}${llmDirty ? " · 有未保存更改" : ""}`;
}

async function loadLLMSettings() {
  if (llmLoading) return;
  llmLoading = true;
  updateControls();
  try {
    const result = await api("/api/llm");
    llmSettings = result;
    if (!llmDirty) {
      $("llm-protocol").value = result.protocol || "openai";
      $("llm-base-url").value = result.base_url || "";
      $("llm-model").value = result.model || "";
      $("llm-env-file").value = result.env_file || "/Users/user1/Desktop/memorax/Skill_RSI_hzh/.env";
      $("llm-env-key").value = result.env_key || "";
    }
    renderLLMStatus();
    return true;
  } catch (error) {
    $("llm-status").textContent = "LLM 配置读取失败，请展开配置重试。";
    announce(`读取 LLM 配置失败。${error.message}`, true);
    return false;
  } finally {
    llmLoading = false;
    updateControls();
  }
}

function renderCatalog() {
  const container = $("catalog");
  container.replaceChildren();
  for (const item of state?.catalog || []) {
    const id = extensionName(item);
    const button = element("button", "", policyDescriptions[id]?.[0] || id);
    button.type = "button";
    button.addEventListener("click", () => { $("source").value = `builtin:${id}`; });
    container.append(button);
  }
}

async function copyText(value) {
  if (!value || value === "—") return;
  try { await navigator.clipboard.writeText(value); announce("路径已复制。"); }
  catch { announce(value); }
}

$("project-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  if (dirty && !window.confirm("当前配置尚未应用。打开其他项目并放弃这些更改？")) return;
  project = $("project").value.trim();
  dirty = false;
  generation += 1;
  while (loading) await new Promise((resolve) => setTimeout(resolve, 50));
  await loadState();
});
for (const id of ["command", "profile", "observe", "advanced-config"]) $(id).addEventListener("input", markDirty);
$("connection-mode").addEventListener("change", () => { desktopMessage = ""; markDirty(); });
$("save").addEventListener("click", () => perform(async () => {
  const config = configuration();
  const desktop = host === "dsh" && config.connection_mode === "desktop";
  const result = await api(desktop ? "/api/desktop" : "/api/configure", config);
  dirty = false;
  await loadState();
  desktopMessage = desktop ? result.message || "桌面接入已保存。请重新打开 DSH 桌面端，并在当前项目中继续任务。" : "";
  updateControls();
  announce(desktopMessage || "配置已保存。新会话会加载扩展，实际生效以宿主回调为准。");
}));
$("refresh").addEventListener("click", () => loadState({ preserve: true, refresh: true }));
$("show-import").addEventListener("click", () => { $("import-dialog").showModal(); $("source").focus(); });
$("close-import").addEventListener("click", () => $("import-dialog").close());
$("import-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  $("install").disabled = true;
  await perform(async () => {
    await api("/api/install", { source: $("source").value.trim() });
    $("import-dialog").close();
    $("source").value = "";
    await loadState({ preserve: true });
    announce("扩展已导入。打开它的开关，再保存项目配置。");
  });
  $("install").disabled = false;
});
$("run-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (dirty) { announce("请先应用当前配置。", true); return; }
  perform(async () => {
    const result = await api("/api/start", { project, host, prompt: $("prompt").value.trim() });
    await loadState({ preserve: true });
    const id = result.task_id || result.id || taskId(result.task || {});
    if (id) await showTask(id);
    announce("任务已启动，原生输出和轨迹将保存到独立任务目录。");
  });
});
$("cancel").addEventListener("click", () => perform(async () => {
  await api("/api/cancel", { task_id: selectedTask });
  await showTask(selectedTask);
  await loadState({ preserve: true });
  announce("已请求停止任务。");
}));
$("emerge").addEventListener("click", () => perform(async () => {
  const id = selectedTask;
  if ($("emergence-command").value.trim() && $("emergence-command").value.trim() !== emergenceCommand) {
    throw new Error("Codex CLI 路径已修改，请先保存默认设置。");
  }
  const result = await api("/api/emergence", {
    task_id: id, instructions: $("emergence-instructions").value.trim(),
    model: $("emergence-model").value.trim(), effort: $("emergence-effort").value,
  });
  emergences.set(id, result);
  if (id === selectedTask) await showTask(id);
  announce(emergenceRunning(result) ? "Codex 已开始读取轨迹并生成候选，运行进度会自动更新。" : "Codex 生成结果已保存，请查看报告与候选。");
}));
for (const id of ["emergence-command", "emergence-model", "emergence-effort"]) {
  $(id).addEventListener("input", () => { emergenceSettingsDirty = true; });
}
$("save-emergence-settings").addEventListener("click", () => perform(async () => {
  await api("/api/emergence/settings", {
    command: $("emergence-command").value.trim(), model: $("emergence-model").value.trim(), effort: $("emergence-effort").value,
  });
  emergenceSettingsDirty = false;
  await loadEmergenceSettings();
  announce("Codex 生成组件的默认设置已保存。");
}));
$("legacy-analysis").addEventListener("toggle", () => {
  if ($("legacy-analysis").open && !llmSettings) loadLLMSettings();
});
$("analyze").addEventListener("click", () => perform(async () => {
  const id = selectedTask;
  const method = $("analysis-method").value;
  if (method === "llm" && $("analysis-host").value === "api" && llmDirty) {
    $("llm-settings").open = true;
    throw new Error("请先保存 LLM 配置，再启动模型分析。");
  }
  const result = await api("/api/analyze", { task_id: id, method, host: $("analysis-host").value });
  analyses.set(id, result);
  if (id === selectedTask) await showTask(id);
  announce(analysisRunning(result) ? "已启动模型分析，进度和诊断会自动更新。" : result.artifact_path
    ? "候选扩展已生成，请查看分析记录并决定是否安装。" : outputText(result.summary || result.reason) || "分析完成，当前记录尚不足以生成候选。");
}));
$("current-project-only").addEventListener("change", renderTasks);
$("task-search").addEventListener("input", renderTasks);
$("codex-home").addEventListener("input", () => { codexHomeDirty = true; });
$("codex-record-start").addEventListener("click", () => perform(async () => {
  await api("/api/codex/recording", { project, enabled: true, codex_home: $("codex-home").value.trim() || "~/.codex", scope: "all_projects" });
  codexHomeDirty = false;
  await loadState({ preserve: true });
  const error = state?.config?.codex_recording?.last_error;
  announce(error ? `记录遇到问题。${outputText(error)}` : "已开始记录本机所有项目的 Codex 新数据。可以继续在 CLI 或桌面端完成任务。", Boolean(error));
}));
$("codex-record-stop").addEventListener("click", () => perform(async () => {
  await api("/api/codex/recording", { project, enabled: false, scope: "all_projects" });
  await loadState({ preserve: true });
  announce("已停止本机所有项目的 Codex 自动记录。");
}));
$("codex-sync").addEventListener("click", () => perform(async () => {
  await api("/api/codex/sync", { project, scope: "all_projects" });
  await loadState({ preserve: true });
  const error = state?.config?.codex_recording?.last_error;
  announce(error ? `记录遇到问题。${outputText(error)}` : "已检查本机所有 Codex 主会话的新数据。", Boolean(error));
}));
$("analysis-method").addEventListener("change", updateControls);
$("analysis-host").addEventListener("change", updateControls);
for (const id of ["llm-protocol", "llm-base-url", "llm-model", "llm-env-file", "llm-env-key", "llm-api-key"]) {
  $(id).addEventListener("input", () => { llmDirty = true; renderLLMStatus(); });
}
$("reload-llm").addEventListener("click", () => loadLLMSettings());
$("save-llm").addEventListener("click", () => perform(async () => {
  const settings = {
    protocol: $("llm-protocol").value,
    base_url: $("llm-base-url").value.trim(), model: $("llm-model").value.trim(),
    env_file: $("llm-env-file").value.trim(), env_key: $("llm-env-key").value.trim(),
  };
  const apiKey = $("llm-api-key").value.trim();
  if (apiKey) settings.api_key = apiKey;
  try {
    await api("/api/llm", settings);
    $("llm-api-key").value = "";
    llmDirty = false;
    if (await loadLLMSettings()) announce("LLM 配置已保存。");
  } catch (error) {
    throw new Error(`保存 LLM 配置失败。${error.message}`);
  }
}));
$("copy-tasks-dir").addEventListener("click", () => copyText(state?.tasks_dir));
document.querySelector(".brand").addEventListener("click", (event) => event.preventDefault());
renderHosts();
updateControls();
if (!token) announce("请使用 skill-rsi ui 打开的完整本机地址，地址中包含本次访问凭据。", true);
else {
  loadState().then(() => {
    const id = initialTask;
    initialTask = null;
    if (id) return showTask(id);
  });
  loadEmergenceSettings();
}
setInterval(async () => {
  if (document.hidden || busy || loading) return;
  await loadState({ preserve: true, quiet: true });
  if (selectedTask) await showTask(selectedTask, true);
}, 5000);
