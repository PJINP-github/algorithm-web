const token = localStorage.getItem("portal_token") || "";
const state = { files: [], category: "", page: 0, rowsPerPage: 8 };
if (!token) window.location.assign("/");
document.addEventListener("DOMContentLoaded", async () => {
    document.querySelector("#session-user").textContent = "";
    document.querySelector("#back-button").addEventListener("click", () => window.location.assign("/features"));
    document.querySelector("#prev-button").addEventListener("click", () => changePage(-1));
    document.querySelector("#next-button").addEventListener("click", () => changePage(1));
    await loadFiles();
});
async function loadFiles() {
    const response = await api("/api/dependencies");
    if (!response.ok) return;
    const payload = await response.json();
    state.files = Array.isArray(payload) ? payload : (payload?.files || []);
    if (!Array.isArray(payload)) {
        const configuredRows = Number(payload?.rows_downloaded_file_algorithm);
        if (Number.isFinite(configuredRows) && configuredRows > 0) {
            state.rowsPerPage = Math.floor(configuredRows);
        }
    }
    const categories = [...new Set(state.files.map(file => file.category).filter(Boolean))];
    state.category = categories[0] || "";
    renderCategories(categories);
    renderFiles();
}
function renderCategories(categories) {
    const bar = document.querySelector("#category-bar");
    bar.innerHTML = "";
    categories.forEach(category => {
        const button = document.createElement("button");
        button.textContent = category;
        button.className = category === state.category ? "active" : "";
        button.addEventListener("click", () => {
            state.category = category;
            state.page = 0;
            renderCategories(categories);
            renderFiles();
        });
        bar.appendChild(button);
    });
}
function renderFiles() {
    const list = document.querySelector("#file-list");
    const files = state.files.filter(file => file.category === state.category);
    const pageSize = Math.max(1, state.rowsPerPage || 8);
    const pages = Math.max(1, Math.ceil(files.length / pageSize));
    state.page = Math.min(state.page, pages - 1);
    list.innerHTML = "";
    files.slice(state.page * pageSize, state.page * pageSize + pageSize).forEach(file => {
        const row = document.createElement("div");
        row.className = "file-row";
        row.title = "单击查看说明，双击使用默认方式打开";
        const displayName = file.desc || file.name;
        row.innerHTML = `<span title="${escapeHtml(displayName)}">${escapeHtml(displayName)}</span><span title="${escapeHtml(file.desc)}">${escapeHtml(file.desc)}</span><span>${formatSize(file.size)}</span><span>${new Date(Number(file.modified || 0) * 1000).toLocaleString()}</span><button type="button">下载</button>`;
        row.addEventListener("click", () => {
            document.querySelector("#notes").textContent = file.notes || "";
        });
        row.addEventListener("dblclick", () => openDependency(file.path));
        row.querySelector("button").addEventListener("click", event => {
            event.stopPropagation();
            download(file.path, file.name);
        });
        list.appendChild(row);
    });
    document.querySelector("#page-label").textContent = pages > 1 ? `${state.page + 1}/${pages}` : "";
    document.querySelector("#pager").classList.toggle("hidden", pages <= 1);
    document.querySelector("#notes").textContent = "";
}
function changePage(delta) { state.page += delta; renderFiles(); }
async function download(path, name) {
    const response = await api(`/api/dependencies/download?path=${encodeURIComponent(path)}`);
    if (!response.ok) return;
    const link = document.createElement("a");
    link.href = URL.createObjectURL(await response.blob());
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
}
async function openDependency(path) {
    await api("/api/dependencies/open", {
        method: "POST",
        body: JSON.stringify({ path }),
    });
}
async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Authorization", `Bearer ${token}`);
    if (options.body && !(options.body instanceof FormData)) {
        headers.set("Content-Type", "application/json");
    }
    return fetch(path, { ...options, headers });
}
function formatSize(size) {
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / 1024 / 1024).toFixed(1)} MB`;
}
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
