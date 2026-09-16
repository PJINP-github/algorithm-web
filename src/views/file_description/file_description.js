const token = localStorage.getItem("portal_token") || "";
const role = localStorage.getItem("portal_role") || "";
const state = {
    kind: "file",
    entries: [],
    selected: new Set(),
    page: 0,
    pageRows: 15,
    staleDays: 15,
    stalePaths: [],
    staleSummary: { folders: [], files: [] },
    cleanupBusy: false,
};
const STALE_PROMPT_KEY = "portal_file_description_stale_prompted";

if (!token || role !== "管理员") window.location.assign("/features");

document.addEventListener("DOMContentLoaded", async () => {
    document.querySelector("#back-button").addEventListener("click", () => {
        window.location.assign("/features");
    });
    document.querySelector("#select-page-button").addEventListener("click", toggleSelectCurrentPage);
    document.querySelector("#delete-button").addEventListener("click", deleteSelected);
    document.querySelector("#stale-delete").addEventListener("click", () => handleStaleAction("delete"));
    document.querySelector("#stale-move").addEventListener("click", () => handleStaleAction("move"));
    document.querySelector("#stale-cancel").addEventListener("click", closeStaleDialog);
    document.querySelector("#previous-page").addEventListener("click", () => {
        state.page = Math.max(0, state.page - 1);
        renderEntries();
    });
    document.querySelector("#next-page").addEventListener("click", () => {
        state.page += 1;
        renderEntries();
    });
    document.querySelectorAll("[data-kind]").forEach(button => {
        button.addEventListener("click", () => {
            state.kind = button.dataset.kind;
            state.page = 0;
            state.selected.clear();
            document.querySelectorAll("[data-kind]").forEach(item =>
                item.classList.toggle("active", item === button)
            );
            loadEntries();
        });
    });
    await loadEntries();
});

async function loadEntries() {
    const list = document.querySelector("#entry-list");
    list.textContent = "正在刷新...";
    const response = await api(`/api/file-description?kind=${state.kind}&refresh=${Date.now()}`);
    if (!response.ok) {
        list.textContent = (await response.json().catch(() => ({}))).message || "读取文件说明失败。";
        updateDeleteButton();
        return;
    }
    const payload = await response.json();
    state.entries = Array.isArray(payload.entries) ? payload.entries : [];
    state.pageRows = Math.max(1, Number(payload.page_rows) || 15);
    state.staleDays = Math.max(1, Number(payload.stale_days) || 15);
    state.stalePaths = Array.isArray(payload.stale_paths) ? payload.stale_paths : [];
    state.staleSummary = payload.summary || { folders: [], files: [] };
    state.page = 0;
    state.selected.clear();
    renderEntries();
    promptStaleEntries();
    document.querySelector("#refresh-time").textContent =
        `刷新于 ${new Date().toLocaleString()}`;
}

function renderEntries() {
    const list = document.querySelector("#entry-list");
    list.innerHTML = "";
    if (!state.entries.length) {
        list.textContent = "暂无符合条件的文件。";
        document.querySelector("#summary").textContent = "0 项";
        updatePager();
        updateDeleteButton();
        return;
    }
    document.querySelector("#summary").textContent = `${state.entries.length} 项`;
    const pageCount = Math.max(1, Math.ceil(state.entries.length / state.pageRows));
    state.page = Math.min(state.page, pageCount - 1);
    const start = state.page * state.pageRows;
    for (const entry of state.entries.slice(start, start + state.pageRows)) {
        const row = document.createElement("div");
        row.className = `entry-row urgency-${entry.urgency || 0}${entry.stale ? " stale" : ""}`;
        row.innerHTML = `
            <label class="select-cell">
                <input type="checkbox" data-path="${escapeHtml(entry.path)}">
            </label>
            <div class="name-cell" title="${escapeHtml(entry.path)}">
                <strong>${escapeHtml(entry.name)}</strong>
                <span>${escapeHtml(entry.path)}</span>
            </div>
            <span class="description-cell" title="${escapeHtml(entry.desc)}">${escapeHtml(entry.desc)}</span>
            <span class="age-cell">${escapeHtml(entry.stale ? `超过${state.staleDays}天` : entry.urgency_label)}<small>${entry.age_days} 天</small></span>
            <span class="size-cell">${formatSize(entry.size)}</span>
            <span class="time-cell">${formatTime(entry.modified)}</span>
        `;
        const checkbox = row.querySelector("input");
        checkbox.checked = state.selected.has(entry.path);
        checkbox.addEventListener("change", () => {
            if (checkbox.checked) state.selected.add(entry.path);
            else state.selected.delete(entry.path);
            updateDeleteButton();
        });
        list.appendChild(row);
    }
    updatePager();
    updateDeleteButton();
}

function currentPageEntries() {
    const start = state.page * state.pageRows;
    return state.entries.slice(start, start + state.pageRows);
}

function toggleSelectCurrentPage() {
    const entries = currentPageEntries();
    if (!entries.length) return;
    const allSelected = entries.every(entry => state.selected.has(entry.path));
    entries.forEach(entry => {
        if (allSelected) state.selected.delete(entry.path);
        else state.selected.add(entry.path);
    });
    renderEntries();
}

function updatePager() {
    const pager = document.querySelector("#pager");
    const pageLabel = document.querySelector("#page-label");
    const previous = document.querySelector("#previous-page");
    const next = document.querySelector("#next-page");
    const pageCount = Math.max(1, Math.ceil(state.entries.length / state.pageRows));
    pager.classList.toggle("hidden", state.entries.length <= state.pageRows);
    pageLabel.textContent = `${state.page + 1}/${pageCount}`;
    previous.disabled = state.page <= 0;
    next.disabled = state.page >= pageCount - 1;
}

function updateDeleteButton() {
    const selectButton = document.querySelector("#select-page-button");
    const button = document.querySelector("#delete-button");
    const pageEntries = currentPageEntries();
    const allSelected = pageEntries.length > 0 &&
        pageEntries.every(entry => state.selected.has(entry.path));
    selectButton.disabled = pageEntries.length === 0;
    selectButton.textContent = allSelected ? "取消全选" : "全选";
    selectButton.setAttribute("aria-pressed", String(allSelected));
    button.disabled = state.selected.size === 0;
    button.textContent = state.selected.size ? `批量删除 (${state.selected.size})` : "批量删除";
}

async function deleteSelected() {
    if (!state.selected.size) return;
    const count = state.selected.size;
    if (!window.confirm(`确定将选中的 ${count} 项移动到回收站吗？文件夹将递归移动。`)) return;
    const response = await api("/api/file-description/delete", {
        method: "POST",
        body: JSON.stringify({ paths: [...state.selected], action: "delete" }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok && response.status !== 206) {
        window.alert(result.message || "批量删除失败。");
        return;
    }
    if (result.message) window.alert(result.message);
    await loadEntries();
}

async function promptStaleEntries() {
    if (!isStalePromptWindow()) return;
    const promptKey = `${localDateKey()}-${stalePromptSlot()}`;
    if (localStorage.getItem(STALE_PROMPT_KEY) === promptKey) return;
    if (!state.stalePaths.length) return;
    localStorage.setItem(STALE_PROMPT_KEY, promptKey);
    renderStaleDialog();
}

function isStalePromptWindow() {
    return Boolean(stalePromptSlot());
}

function stalePromptSlot() {
    const now = new Date();
    const minutes = now.getHours() * 60 + now.getMinutes();
    if (minutes >= 12 * 60 && minutes <= 13 * 60 + 15) return "midday";
    if (minutes >= 17 * 60 + 30 && minutes <= 18 * 60 + 15) return "evening";
    return "";
}

function localDateKey() {
    const now = new Date();
    return [
        now.getFullYear(),
        String(now.getMonth() + 1).padStart(2, "0"),
        String(now.getDate()).padStart(2, "0"),
    ].join("-");
}

function renderStaleDialog() {
    const overlay = document.querySelector("#stale-dialog");
    const folderList = document.querySelector("#stale-folders");
    const fileList = document.querySelector("#stale-files");
    const total = document.querySelector("#stale-total");
    const folders = state.staleSummary.folders || [];
    const files = state.staleSummary.files || [];
    document.querySelector("#stale-folder-title").textContent =
        `文件夹--文件夹内总大小（${formatSize(state.staleSummary.folders_total_size || 0)}）`;
    document.querySelector("#stale-file-title").textContent =
        `文件---文件总大小（${formatSize(state.staleSummary.files_total_size || 0)}）`;
    folderList.innerHTML = folders.map(staleSummaryRow).join("") ||
        '<li><span>暂无文件夹</span><strong>0 B</strong></li>';
    fileList.innerHTML = files.map(staleSummaryRow).join("") ||
        '<li><span>暂无文件</span><strong>0 B</strong></li>';
    total.textContent = `${state.stalePaths.length} 项`;
    overlay.classList.remove("hidden");
}

function staleSummaryRow(entry) {
    return `<li><span title="${escapeHtml(entry.path)}">${escapeHtml(entry.name)}</span><strong>${formatSize(entry.size || 0)}</strong></li>`;
}

function closeStaleDialog() {
    document.querySelector("#stale-dialog").classList.add("hidden");
}

async function handleStaleAction(action) {
    if (state.cleanupBusy) return;
    const stalePaths = state.stalePaths.slice();
    if (!stalePaths.length) {
        closeStaleDialog();
        return;
    }
    state.cleanupBusy = true;
    document.querySelectorAll(".stale-actions button").forEach(button => {
        button.disabled = true;
    });
    const response = await api("/api/file-description/delete", {
        method: "POST",
        body: JSON.stringify({ paths: stalePaths, action }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok && response.status !== 206) {
        state.cleanupBusy = false;
        document.querySelectorAll(".stale-actions button").forEach(button => {
            button.disabled = false;
        });
        window.alert(result.message || "移动遗留内容失败。");
        return;
    }
    if (result.message) window.alert(result.message);
    state.cleanupBusy = false;
    state.stalePaths = [];
    closeStaleDialog();
    await loadEntries();
}

async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Authorization", `Bearer ${token}`);
    if (options.body && !(options.body instanceof FormData)) {
        headers.set("Content-Type", "application/json");
    }
    const response = await fetch(path, { ...options, headers });
    if (response.status === 401) {
        localStorage.removeItem("portal_token");
        window.location.assign("/");
    }
    return response;
}

function formatSize(size) {
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
    return `${(size / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function formatTime(value) {
    const timestamp = Number(value || 0);
    return timestamp ? new Date(timestamp * 1000).toLocaleString() : "未知";
}

function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, character => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[character]));
}
