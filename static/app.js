"use strict";

/** 保存页面会话、当前目录和上传任务状态。 */
const state = { user: null, csrf: "", path: "", maxBytes: 0, uploading: false, allChoice: null };
/** 按 ID 获取页面元素。 */
function el(id) { return document.getElementById(id); }
/** 显示中文操作结果或错误消息。 */
function notice(message, error = false) {
  el("notice").textContent = message;
  el("notice").className = error ? "error" : "";
  el("notice").hidden = !message;
}
/** 发起 JSON 接口请求，并统一处理登录失效。 */
async function api(url, options = {}) {
  // Step1：携带 CSRF 令牌，JSON 数据由调用方提供。
  const response = await fetch(url, { ...options, headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf, ...options.headers } });
  const data = await response.json();
  // Step2：失效后返回登录界面，其他错误交给操作入口显示。
  if (!response.ok) {
    if (response.status === 401) { state.user = null; renderView(); await refreshSession(false); }
    throw new Error(data.error || "请求失败");
  }
  return data;
}
/** 根据角色显示登录、文件管理或用户管理界面。 */
function renderView() {
  el("login-view").hidden = !!state.user;
  el("files-view").hidden = !state.user || state.user.role !== "user";
  el("users-view").hidden = !state.user || state.user.role !== "admin";
  el("account").hidden = !state.user;
  el("identity").textContent = state.user ? `${state.user.username} · ${state.user.role === "admin" ? "超级管理员" : "普通用户"}` : "";
}
/** 获取当前会话，可选加载对应角色的数据。 */
async function refreshSession(load = true) {
  // Step1：同步服务端会话、CSRF 令牌和上传限制。
  const response = await fetch("/api/session");
  if (!response.ok) throw new Error("无法连接服务器，请刷新重试");
  const data = await response.json();
  Object.assign(state, { user: data.user, csrf: data.csrf, maxBytes: data.max_file_bytes });
  renderView();
  // Step2：按角色加载首屏内容。
  if (load && state.user) await (state.user.role === "admin" ? loadUsers() : loadFiles());
}
/** 将字节数转换为可读的文件大小。 */
function sizeLabel(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let index = -1;
  do { bytes /= 1024; index++; } while (bytes >= 1024 && index < units.length - 1);
  return `${bytes.toFixed(1)} ${units[index]}`;
}
/** 创建仅通过 textContent 写入内容的表格单元格。 */
function cell(text = "") { const node = document.createElement("td"); node.textContent = text; return node; }
/** 创建操作按钮并统一显示异步错误。 */
function button(text, action, danger = false) {
  const node = document.createElement("button");
  node.textContent = text;
  node.className = `small${danger ? " danger" : ""}`;
  node.addEventListener("click", () => run(action));
  return node;
}
/** 执行界面操作，将错误显示为提示。 */
async function run(action) { try { await action(); } catch (error) { notice(error.message, true); } }
/** 拼接当前目录和单个相对路径。 */
function joinPath(parent, name) { return parent ? `${parent}/${name}` : name; }
/** 加载文件列表并生成面包屑导航。 */
async function loadFiles() {
  // Step1：读取当前目录，生成可点击的路径导航。
  const requestedPath = state.path;
  const data = await api(`/api/files?path=${encodeURIComponent(requestedPath)}`);
  if (requestedPath !== state.path) return;
  const breadcrumbs = el("breadcrumbs"); breadcrumbs.replaceChildren();
  breadcrumbs.append(button("根目录", () => navigate("")));
  let current = "";
  for (const part of state.path.split("/").filter(Boolean)) {
    current = joinPath(current, part); const target = current;
    const separator = document.createElement("span"); separator.textContent = "/";
    breadcrumbs.append(separator, button(part, () => navigate(target)));
  }
  // Step2：安全生成条目名称、属性和操作按钮。
  const list = el("file-list"); list.replaceChildren();
  for (const entry of data.entries) {
    const row = document.createElement("tr"); const nameCell = cell();
    const name = document.createElement("button"); name.className = "file-name";
    const icon = document.createElement("span"); icon.className = "file-icon"; icon.textContent = entry.is_dir ? "夹" : "文";
    name.append(icon, document.createTextNode(entry.name));
    const target = joinPath(state.path, entry.name);
    name.addEventListener("click", () => run(() => entry.is_dir ? navigate(target) : downloadFile(target)));
    nameCell.append(name);
    const operations = cell();
    if (!entry.is_dir) operations.append(button("下载", () => downloadFile(target)));
    operations.append(button("重命名", () => renameFile(target, entry.name)), button("删除", () => deleteFile(target, entry.is_dir), true));
    row.append(nameCell, cell(entry.is_dir ? "—" : sizeLabel(entry.size)), cell(new Date(entry.modified * 1000).toLocaleString("zh-CN", { hour12: false })), operations);
    list.append(row);
  }
  el("file-empty").hidden = data.entries.length > 0;
}
/** 切换目录；上传期间保持目录固定。 */
async function navigate(path) {
  if (state.uploading) throw new Error("上传进行中，请等待完成后切换目录");
  const previous = state.path; state.path = path;
  try { await loadFiles(); } catch (error) { state.path = previous; throw error; }
}
/** 校验会话后按用户下载根地址生成链接，留空时使用本网盘下载接口。 */
async function downloadFile(path) {
  // Step1：读取当前账号的最新下载配置，拒绝已经失效的会话。
  await refreshSession(false);
  if (!state.user) throw new Error("登录已失效，请重新登录");
  const anchor = document.createElement("a");
  // Step2：逐级编码相对路径，防止中文、空格、问号等改变 URL 含义。
  anchor.href = state.user.download_base_url
    ? state.user.download_base_url + path.split("/").map(encodeURIComponent).join("/")
    : `/api/download?path=${encodeURIComponent(path)}`;
  anchor.download = "";
  document.body.append(anchor); anchor.click(); anchor.remove();
}
/** 提示新名称并提交重命名操作。 */
async function renameFile(path, name) {
  if (state.uploading) throw new Error("请等待上传完成后再操作");
  const value = window.prompt("输入新名称", name);
  if (value === null || value === name) return;
  await api("/api/rename", { method: "POST", body: JSON.stringify({ path, name: value }) });
  await loadFiles(); notice("重命名成功");
}
/** 确认永久删除文件或整个非空文件夹。 */
async function deleteFile(path, isDir) {
  if (state.uploading) throw new Error("请等待上传完成后再操作");
  if (!window.confirm(`永久删除“${path}”${isDir ? "及其中全部内容" : ""}？此操作无法恢复。`)) return;
  await api("/api/files", { method: "DELETE", body: JSON.stringify({ path, confirmed: true }) });
  await loadFiles(); notice("删除成功");
}
/** 加载普通用户列表并生成管理操作。 */
async function loadUsers() {
  // Step1：获取普通用户配置。
  const data = await api("/api/users"); const list = el("user-list"); list.replaceChildren();
  // Step2：生成账号状态与编辑、停用、删除按钮。
  for (const user of data.users) {
    const row = document.createElement("tr"); const statusCell = cell();
    const badge = document.createElement("span"); badge.className = `badge${user.enabled ? "" : " off"}`; badge.textContent = user.enabled ? "已启用" : "已停用"; statusCell.append(badge);
    const operations = cell();
    operations.append(button("编辑 / 重置密码", () => openUser(user)), button(user.enabled ? "停用" : "启用", () => toggleUser(user)), button("删除", () => removeUser(user), true));
    row.append(cell(user.username), cell(user.root), statusCell, operations); list.append(row);
  }
  el("user-empty").hidden = data.users.length > 0;
  el("user-count").textContent = `${data.users.length} 个普通用户`;
}
/** 显示新增或编辑用户弹窗，编辑时密码留空表示保留原密码。 */
function openUser(user = null) {
  const form = el("user-form"); form.reset();
  form.elements.id.value = user ? user.id : "";
  form.elements.username.value = user ? user.username : "";
  form.elements.root.value = user ? user.root : "";
  form.elements.download_base_url.value = user ? user.download_base_url : "";
  form.elements.enabled.checked = user ? !!user.enabled : true;
  form.elements.password.required = !user;
  el("user-title").textContent = user ? "编辑用户" : "新增用户";
  el("password-help").textContent = user ? "留空保留原密码；输入新密码即可重置。至少 8 个字符。" : "至少 8 个字符。";
  el("user-error").hidden = true;
  el("user-dialog").showModal();
}
/** 切换普通用户启用状态，提交完整配置以复用更新接口。 */
async function toggleUser(user) {
  if (!window.confirm(`${user.enabled ? "停用" : "启用"}用户“${user.username}”？其已有登录将失效。`)) return;
  await api(`/api/users/${user.id}`, { method: "PUT", body: JSON.stringify({ username: user.username, root: user.root, enabled: !user.enabled, password: "" }) });
  await loadUsers(); notice("用户状态已更新");
}
/** 确认删除账号，明确保留用户文件。 */
async function removeUser(user) {
  if (!window.confirm(`删除用户“${user.username}”？账号将无法登录，服务器文件会保留。`)) return;
  await api(`/api/users/${user.id}`, { method: "DELETE" });
  await loadUsers(); notice("用户已删除，文件已保留");
}
/** 通过 XHR 上传单个文件，以获得上传进度及同名冲突结果。 */
function uploadOne(file, path, overwrite) {
  return new Promise((resolve, reject) => {
    // Step1：构造表单并附加会话 CSRF 令牌。
    const form = new FormData(); form.append("path", path); form.append("overwrite", overwrite ? "1" : "0"); form.append("file", file);
    const xhr = new XMLHttpRequest(); xhr.open("POST", "/api/upload"); xhr.setRequestHeader("X-CSRF-Token", state.csrf);
    // Step2：显示网络上传进度，接收服务器落盘结果。
    xhr.upload.onprogress = (event) => { if (event.lengthComputable) el("upload-progress").value = event.loaded / event.total * 100; };
    xhr.onload = () => {
      let data;
      try { data = JSON.parse(xhr.responseText); } catch { reject(new Error("服务器返回异常，请检查上传限制和服务日志")); return; }
      if (xhr.status === 422 && data.blocked) resolve({ blocked: true, reason: data.error });
      else if (xhr.status === 409 && data.conflict) resolve({ conflict: true });
      else if (xhr.status >= 200 && xhr.status < 300) resolve({ ok: true });
      else { const error = new Error(data.error || "上传失败"); error.status = xhr.status; reject(error); }
    };
    xhr.onerror = () => reject(new Error("网络连接中断，请重新上传"));
    xhr.onabort = () => reject(new Error("上传已取消"));
    xhr.send(form);
  });
}
/** 弹出同名文件处理选择，ESC 默认跳过。 */
function conflictChoice(path) {
  if (state.allChoice) return Promise.resolve(state.allChoice);
  return new Promise((resolve) => {
    const dialog = el("conflict-dialog"); el("conflict-name").textContent = path;
    // Step1：监听一次选择，关闭弹窗时统一移除监听器。
    const finish = (choice) => {
      dialog.removeEventListener("click", onClick); dialog.removeEventListener("cancel", onCancel); dialog.close();
      if (choice.endsWith("-all")) state.allChoice = choice.startsWith("overwrite") ? "overwrite" : "skip";
      resolve(choice.startsWith("overwrite") ? "overwrite" : "skip");
    };
    const onClick = (event) => { if (event.target.dataset.choice) finish(event.target.dataset.choice); };
    const onCancel = (event) => { event.preventDefault(); finish("skip"); };
    // Step2：打开选择窗口并等待结果。
    dialog.addEventListener("click", onClick); dialog.addEventListener("cancel", onCancel); dialog.showModal();
  });
}
/** 顺序上传文件，保留文件夹相对路径，汇总成功、跳过和失败条目。 */
async function uploadBatch(files, folderMode) {
  if (!files.length || state.uploading) return;
  // Step1：固定当前目录，初始化任务界面。
  state.uploading = true; state.allChoice = null;
  const target = state.path; let success = 0, skipped = 0, failed = 0;
  el("upload-panel").hidden = false; el("upload-errors").replaceChildren();
  const controls = ["upload-files", "upload-folder", "new-folder", "logout"];
  controls.forEach((id) => { el(id).disabled = true; });
  try {
    // Step2：逐文件上传，只有服务器报告冲突时才询问覆盖。
    for (let index = 0; index < files.length; index++) {
      const file = files[index]; const path = joinPath(target, folderMode ? file.webkitRelativePath : file.name);
      el("upload-label").textContent = `正在上传 ${index + 1}/${files.length}：${path}`; el("upload-progress").value = 0;
      try {
        if (file.size > state.maxBytes) throw new Error(`超过单文件限制 ${sizeLabel(state.maxBytes)}`);
        let result = await uploadOne(file, path, false);
        if (result.conflict) {
          const choice = await conflictChoice(path);
          if (choice === "skip") { skipped++; continue; }
          result = await uploadOne(file, path, true);
        }
        if (result.blocked) {
          skipped++; const item = document.createElement("li"); item.textContent = `${path}：已跳过，${result.reason}`; el("upload-errors").append(item);
          continue;
        }
        if (result.ok) success++;
      } catch (error) {
        failed++; const item = document.createElement("li"); item.textContent = `${path}：${error.message}`; el("upload-errors").append(item);
        if (error.status === 401 || error.status === 403) { failed += files.length - index - 1; break; }
      } finally {
        el("upload-summary").textContent = `成功 ${success} · 跳过 ${skipped} · 失败/未上传 ${failed}`;
      }
    }
    // Step3：完成后重新检查登录并刷新文件列表。
    el("upload-label").textContent = "上传完成";
    await refreshSession(false);
    if (state.user && state.user.role === "user") await loadFiles();
  } finally {
    state.uploading = false;
    controls.forEach((id) => { el(id).disabled = false; });
    el("files-input").value = ""; el("folder-input").value = "";
  }
}

// Step1：绑定登录和退出事件。
el("login-form").addEventListener("submit", (event) => {
  event.preventDefault(); const form = event.currentTarget;
  run(async () => {
    const submit = form.querySelector("button"); submit.disabled = true;
    try {
      const data = await api("/api/login", { method: "POST", body: JSON.stringify({ username: form.elements.username.value, password: form.elements.password.value }) });
      state.user = data.user; state.csrf = data.csrf; state.path = ""; form.reset(); notice(""); renderView();
      await (state.user.role === "admin" ? loadUsers() : loadFiles());
    } finally { submit.disabled = false; }
  });
});
el("logout").addEventListener("click", () => run(async () => {
  const data = await api("/api/logout", { method: "POST", body: "{}" });
  state.user = null; state.csrf = data.csrf; state.path = ""; notice(""); el("upload-panel").hidden = true; renderView();
}));
// Step2：绑定文件管理和上传入口。
el("new-folder").addEventListener("click", () => run(async () => {
  const name = window.prompt("输入新文件夹名称"); if (name === null) return;
  await api("/api/folders", { method: "POST", body: JSON.stringify({ path: state.path, name }) });
  await loadFiles(); notice("文件夹创建成功");
}));
el("refresh-files").addEventListener("click", () => run(loadFiles));
el("upload-files").addEventListener("click", () => el("files-input").click());
el("upload-folder").addEventListener("click", () => {
  if (!("webkitdirectory" in el("folder-input"))) { notice("当前浏览器不支持文件夹选择，请使用桌面 Chrome 或 Edge", true); return; }
  el("folder-input").click();
});
el("files-input").addEventListener("change", (event) => run(() => uploadBatch(Array.from(event.target.files), false)));
el("folder-input").addEventListener("change", (event) => run(() => uploadBatch(Array.from(event.target.files), true)));
window.addEventListener("beforeunload", (event) => { if (state.uploading) { event.preventDefault(); event.returnValue = ""; } });
// Step3：绑定账号管理表单。
el("new-user").addEventListener("click", () => openUser());
el("refresh-users").addEventListener("click", () => run(loadUsers));
el("close-user").addEventListener("click", () => el("user-dialog").close());
el("user-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget; const id = form.elements.id.value; const submit = form.querySelector("button[type=submit]");
  submit.disabled = true; el("user-error").hidden = true;
  try {
    await api(id ? `/api/users/${id}` : "/api/users", { method: id ? "PUT" : "POST", body: JSON.stringify({ username: form.elements.username.value, password: form.elements.password.value, root: form.elements.root.value, download_base_url: form.elements.download_base_url.value, enabled: form.elements.enabled.checked }) });
    el("user-dialog").close(); await loadUsers(); notice(id ? "用户已更新，原登录已失效" : "用户创建成功");
  } catch (error) { el("user-error").textContent = error.message; el("user-error").hidden = false; }
  finally { submit.disabled = false; }
});
// Step4：读取会话并展示首屏。
run(refreshSession);
