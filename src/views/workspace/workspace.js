const state = {
    token: localStorage.getItem("portal_token") || "",
    user: localStorage.getItem("portal_user") || "",
    role: localStorage.getItem("portal_role") || "",
    models: [],
    activeModel: "log",
    files: [],
    filePage: 0,
    logLines: ["等待任务运行。"],
    logPage: 0,
    currentJob: null,
    runningJobs: [],
    runningRefreshTimer: null,
    modelRequestId: 0,
    modelLoading: false,
    stopHoldTimer: null,
    stopArmTimer: null,
    stopAllArmed: false,
    stopHoldFired: false,
    suppressStopClick: false,
    currentConfigPath: "",
    selectedUploads: {},
    genericUploads: {},
    genericParameterCombination: {},
    latestReportPath: "",
    latestWeeklyFiles: {},
    truenoCatalog: null,
    truenoCatalogLoading: false,
    truenoCatalogPromise: null,
    truenoImageDataUrl: "",
    truenoCalibrationPoints: [],
    truenoCalibrationPolygons: [],
    truenoCalibrationDrawing: false,
    truenoPointHelpers: [],
    truenoSavedPointHelpers: [],
    truenoPointSelectionKey: "",
    truenoPointPage: 0,
    truenoPointAutosaveTimer: null,
    truenoPointLastSaved: "",
    truenoPointDrawing: false,
    truenoNeedsPointHelpers: false,
    truenoResult: null,
    truenoZoom: 1,
    truenoZoomOrigin: { x: 50, y: 50 },
    truenoMode: localStorage.getItem("portal_trueno_mode") || "auto",
    truenoInferenceMode: "local",
    truenoSelection: null,
    startingAction: false,
    codes: [],
    logVisualLines: 19,
};

const FILE_LIST_LIMIT = 5;
const LOG_MAX_PAGES = 9;

const $ = selector => document.querySelector(selector);

document.addEventListener("DOMContentLoaded", async () => {
    if (!state.token) {
        window.location.assign("/");
        return;
    }
    const roleIcon = {
        "管理员": "管理员",
        "普通一级": "普通一级",
        "普通二级": "普通二级",
    }[state.role] || "普通二级";
    $("#session-user-icon").src = `/user-icon.svg?role=${encodeURIComponent(roleIcon)}`;
    $("#logout-button").addEventListener("click", logout);
    $("#log-prev").addEventListener("click", () => turnLog(-1));
    $("#log-next").addEventListener("click", () => turnLog(1));
    $("#config-menu-button").addEventListener("click", toggleConfigMenu);
    $("#config-save").addEventListener("click", saveActiveSidePanel);
    $("#trueno-point-previous").addEventListener("click", () => {
        state.truenoPointPage = Math.max(0, state.truenoPointPage - 1);
        renderTruenoPointPanel();
    });
    $("#trueno-point-next").addEventListener("click", () => {
        const pageCount = Math.max(1, Math.ceil(state.truenoPointHelpers.length / 4));
        state.truenoPointPage = Math.min(pageCount - 1, state.truenoPointPage + 1);
        renderTruenoPointPanel();
    });
    setupStopControls();
    document.addEventListener("click", event => {
        if (!event.target.closest("#config-picker")) closeConfigMenu();
        if (!event.target.closest(".trueno-model-picker")) closeTruenoModelMenu();
    });
    document.addEventListener("keydown", event => {
        if (event.key === "Escape") {
            closeConfigMenu();
            closeTruenoModelMenu();
        }
    });
    window.addEventListener("resize", () => {
        renderLog();
        fitGenericRows();
    });
    await loadCatalog();
    await refreshRunningJobs();
    state.runningRefreshTimer = window.setInterval(refreshRunningJobs, 1500);
});

async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Authorization", `Bearer ${state.token}`);
    if (options.body && !(options.body instanceof FormData)) {
        headers.set("Content-Type", "application/json");
    }
    const response = await fetch(path, { ...options, headers });
    if (response.status === 401) {
        localStorage.removeItem("portal_token");
        window.location.assign("/");
        throw new Error("未登录");
    }
    return response;
}

async function loadCatalog() {
    const response = await api("/api/workspace");
    if (!response.ok) {
        $("#workspace-panel").textContent = "载入模型入口失败。";
        return;
    }
    state.models = await response.json();
    state.logVisualLines = configuredLogVisualLines();
    applyLogDisplaySettings();
    if (!activeModel()) {
        state.activeModel = state.models[0] ? state.models[0].id : "";
    }
    renderTabs();
    await selectModel(state.activeModel);
}

function renderTabs() {
    const tabs = $("#model-tabs");
    tabs.innerHTML = "";
    for (const model of state.models) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = model.name;
        button.className = model.id === state.activeModel ? "active" : "";
        button.disabled = state.modelLoading && model.id === state.activeModel;
        button.addEventListener("click", () => selectModel(model.id));
        tabs.appendChild(button);
    }
}

async function selectModel(modelId) {
    const requestId = ++state.modelRequestId;
    const previousModelId = state.activeModel;
    state.modelLoading = true;
    state.activeModel = modelId;
    if (modelId !== "trueno" || previousModelId !== "trueno") {
        state.truenoInferenceMode = "local";
    }
    state.filePage = 0;
    state.currentConfigPath = "";
    resetSidePanelForModel(modelId);
    renderTabs();
    const model = activeModel();
    $("#model-description").textContent = model ? model.description : "";
    renderPanel();
    configureConfigBox();
    try {
        if (modelId === "auth") {
            await loadCodes();
            return;
        }
        if (modelId === "trueno") {
            await loadTruenoCatalog();
            return;
        }
        await refreshFiles();
        if (requestId !== state.modelRequestId || state.activeModel !== modelId) return;
        if (shouldShowConfigBox()) {
            await loadConfig();
        }
    } finally {
        if (requestId === state.modelRequestId) {
            state.modelLoading = false;
            renderTabs();
        }
    }
}

function resetSidePanelForModel(modelId) {
    closeConfigMenu();
    saveTruenoPoints(true);
    cancelTruenoPointsAutosave();
    state.truenoPointDrawing = false;
    state.truenoCalibrationDrawing = false;
    state.truenoCalibrationPoints = [];
    state.truenoCalibrationPolygons = [];
    updateTruenoCalibrationButton();
    if (modelId !== "trueno") {
        state.truenoNeedsPointHelpers = false;
        state.truenoPointPage = 0;
        if (state.truenoPointAutosaveTimer) {
            window.clearTimeout(state.truenoPointAutosaveTimer);
            state.truenoPointAutosaveTimer = null;
        }
    }
    const editor = $("#config-editor");
    const pointPanel = $("#trueno-point-panel");
    const pointPager = $("#trueno-point-pager");
    const message = $("#config-message");
    if (editor) {
        editor.value = "";
        editor.classList.add("hidden");
    }
    if (pointPanel) {
        pointPanel.innerHTML = "";
        pointPanel.classList.add("hidden");
    }
    if (pointPager) {
        pointPager.classList.add("hidden");
    }
    if (message) {
        message.textContent = "";
    }
}

function activeModel() {
    return state.models.find(model => model.id === state.activeModel);
}

function configuredLogVisualLines() {
    const lines = state.models
        .map(model => Number(model.log_visual_lines))
        .find(value => Number.isFinite(value) && value > 0);
    return Math.min(200, Math.max(1, Math.round(lines || 19)));
}

function applyLogDisplaySettings() {
    document.documentElement.style.setProperty("--job-log-lines", String(state.logVisualLines));
    document.documentElement.style.setProperty("--job-log-height", `calc(${(state.logVisualLines * 1.55).toFixed(2)}em + 26px)`);
}

function renderPanel() {
    const model = activeModel();
    const panel = $("#workspace-panel");
    if (!model) {
        panel.classList.remove("generic-panel");
        panel.textContent = "模型不存在。";
        return;
    }
    panel.classList.toggle("generic-panel", Boolean(model.generic));
    if (model.id === "auth") {
        renderAuthPanel(model);
        return;
    }
    if (model.id === "trueno") {
        renderTruenoPanel();
        return;
    }
    if (model.generic) {
        renderGenericPanel(model);
        return;
    }
    panel.innerHTML = `
        <div class="section-head">
            <h2>${model.name}</h2>
            <p class="status-line" id="status-line">请选择文件或运行模式。</p>
        </div>
        <div class="tool-row" id="tool-row"></div>
        <div class="action-grid" id="action-grid"></div>
        <div class="file-box">
            <div class="section-head">
                <h3>处理文件</h3>
            </div>
            <div class="file-list" id="file-list"></div>
            <a class="review-link primary" id="review-link" target="_blank" rel="noreferrer">打开标注界面</a>
        </div>
    `;
    renderTools();
    renderActions();
}

function renderAuthPanel(model) {
    $("#workspace-panel").innerHTML = `
        <div class="section-head">
            <h2>${model.name}</h2>
            <p class="status-line" id="status-line">创建或查看注册授权码。</p>
        </div>
        <div class="tool-row auth-tools">
            <select id="code-role">
                <option value="普通一级">普通一级授权码</option>
                <option value="普通二级">普通二级授权码</option>
                <option value="管理员">管理员授权码</option>
            </select>
            <button type="button" id="create-code-button" class="primary">创建授权码</button>
        </div>
        <div class="file-box auth-box">
            <div class="section-head">
                <h3>授权码</h3>
                <button type="button" id="refresh-codes-button">刷新</button>
            </div>
            <div class="code-list" id="code-list"></div>
        </div>
    `;
    $("#create-code-button").addEventListener("click", createCode);
    $("#refresh-codes-button").addEventListener("click", loadCodes);
}

function renderGenericPanel(model) {
    $("#workspace-panel").innerHTML = `
        <div class="section-head">
            <h2>${escapeHtml(model.name)}</h2>
            <p class="status-line" id="status-line">选择输入、处理模式和参数后运行。</p>
        </div>
        <div class="generic-control-row" id="generic-control-row">
            <div class="generic-imports" id="generic-imports"></div>
            <div class="generic-parameter-bar" id="generic-parameter-bar"></div>
            <div class="generic-parameters" id="generic-parameters"></div>
        </div>
        <div class="action-grid generic-action-grid" id="action-grid"></div>
        <div class="file-box">
            <div class="section-head">
                <h3>处理文件</h3>
            </div>
            <div class="file-list" id="file-list"></div>
            <a class="review-link primary" id="review-link" target="_blank" rel="noreferrer">打开输出目录</a>
        </div>
    `;
    renderGenericImports(model);
    renderGenericParameters(model);
    renderActions();
    fitGenericRows();
}

function renderGenericImports(model) {
    const container = $("#generic-imports");
    if (!container) return;
    const imports = Array.isArray(model.imports) ? model.imports : [];
    const uploads = state.genericUploads[state.activeModel] || {};
    container.innerHTML = imports.map(item => {
        const isFolder = String(item.kind || "file").toLowerCase() === "folder";
        const selected = uploads[item.id];
        return `
            <label class="upload-label-button generic-import-control">
                ${escapeHtml(item.name)}
                <input type="file"
                    data-generic-import="${escapeHtml(item.id)}"
                    accept="${escapeHtml(item.accept || "")}"
                    ${isFolder ? "webkitdirectory directory multiple" : ""}
                    ${item.required ? "required" : ""}>
            </label>
            <span class="selected-file generic-import-name" data-generic-import-name="${escapeHtml(item.id)}">
                ${escapeHtml(selected?.name || (isFolder ? "未选择文件夹" : "未选择文件"))}
            </span>
        `;
    }).join("");
    for (const input of container.querySelectorAll("[data-generic-import]")) {
        const item = imports.find(candidate => candidate.id === input.dataset.genericImport);
        input.addEventListener("change", () => uploadGenericImport(item, input));
    }
}

async function uploadGenericImport(item, input) {
    if (!item || !input?.files?.length) {
        setStatus("请选择要导入的文件或文件夹。");
        return;
    }
    const files = [...input.files];
    const data = new FormData();
    data.set("slot", item.id);
    for (const file of files) {
        data.append("file", file, file.webkitRelativePath || file.name);
    }
    setStatus(`正在导入${item.name}... 0%`);
    const result = await uploadWithProgress(
        `/api/files/upload?model=${encodeURIComponent(state.activeModel)}&slot=${encodeURIComponent(item.id)}`,
        data,
        percent => setStatus(`正在导入${item.name}... ${percent}%`),
    ).catch(error => ({ ok: false, result: { message: error.message || "导入失败" } }));
    if (!result.ok) {
        setStatus(result.result.message || "导入失败。");
        return;
    }
    if (!state.genericUploads[state.activeModel]) {
        state.genericUploads[state.activeModel] = {};
    }
    state.genericUploads[state.activeModel][item.id] = {
        path: result.result.path,
        name: result.result.display_name || result.result.name || item.name,
        kind: result.result.kind || item.kind || "file",
    };
    const label = document.querySelector(
        `[data-generic-import-name="${cssEscape(item.id)}"]`,
    );
    if (label) {
        label.textContent = state.genericUploads[state.activeModel][item.id].name;
    }
    input.value = "";
    setStatus(`已导入：${state.genericUploads[state.activeModel][item.id].name}`);
    fitGenericRows();
}

function cssEscape(value) {
    if (window.CSS?.escape) return window.CSS.escape(String(value));
    return String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
}

function renderGenericParameters(model) {
    const bar = $("#generic-parameter-bar");
    const panel = $("#generic-parameters");
    if (!bar || !panel) return;
    const combinations = Array.isArray(model.parameter_combinations)
        ? model.parameter_combinations
        : [];
    if (combinations.length > 1) {
        const selected = Number.isInteger(state.genericParameterCombination[state.activeModel])
            ? state.genericParameterCombination[state.activeModel]
            : 0;
        state.genericParameterCombination[state.activeModel] =
            Math.min(Math.max(selected, 0), combinations.length - 1);
        bar.innerHTML = `
            <label>参数组合
                <select id="generic-parameter-combination">
                    ${combinations.map((_, index) =>
                        `<option value="${index}" ${index === state.genericParameterCombination[state.activeModel] ? "selected" : ""}>组合 ${index + 1}</option>`
                    ).join("")}
                </select>
            </label>
        `;
        $("#generic-parameter-combination").addEventListener("change", event => {
            state.genericParameterCombination[state.activeModel] = Number(event.target.value) || 0;
            renderGenericParameters(model);
            fitGenericRows();
        });
    } else {
        bar.innerHTML = "";
    }
    const visibleIds = genericVisibleParameterIds(model);
    const parameters = (model.parameters || []).filter(parameter =>
        visibleIds.includes(parameter.id),
    );
    panel.innerHTML = parameters.map(parameterControlHtml).join("");
    fitGenericRows();
}

function fitGenericRows() {
    const panel = $("#workspace-panel");
    if (!panel?.classList.contains("generic-panel")) return;
    const rows = document.querySelectorAll(".generic-control-row, .generic-action-grid");
    for (const row of rows) {
        row.classList.remove("is-compact", "is-ultra-compact");
        if (row.scrollWidth > row.clientWidth + 1) {
            row.classList.add("is-compact");
        }
        if (row.scrollWidth > row.clientWidth + 1) {
            row.classList.add("is-ultra-compact");
        }
    }
}

function genericVisibleParameterIds(model) {
    const combinations = Array.isArray(model.parameter_combinations)
        ? model.parameter_combinations
        : [];
    if (!combinations.length) return (model.parameters || []).map(parameter => parameter.id);
    const index = Math.min(
        Math.max(Number(state.genericParameterCombination[state.activeModel]) || 0, 0),
        combinations.length - 1,
    );
    return combinations[index]
        .map(value => {
            const raw = String(value);
            if ((model.parameters || []).some(parameter => parameter.id === raw)) return raw;
            const numeric = Number(raw);
            return Number.isInteger(numeric) ? model.parameters?.[numeric - 1]?.id : null;
        })
        .filter(Boolean);
}

function parameterControlHtml(parameter) {
    const type = String(parameter.parameter_type || "text").toLowerCase();
    const value = parameter.default ?? "";
    const options = Array.isArray(parameter.options) ? parameter.options : [];
    if (type === "checkbox") {
        return `
            <label class="generic-parameter generic-checkbox">
                <input type="checkbox" data-generic-parameter="${escapeHtml(parameter.id)}" ${value === true ? "checked" : ""}>
                <span>${escapeHtml(parameter.name)}</span>
            </label>
        `;
    }
    if (type === "select" || type === "multi_select") {
        return `
            <label class="generic-parameter">
                <span>${escapeHtml(parameter.name)}</span>
                <select data-generic-parameter="${escapeHtml(parameter.id)}" ${type === "multi_select" ? "multiple" : ""}>
                    ${options.map(option => {
                        const optionValue = option.value ?? "";
                        const selected = Array.isArray(value)
                            ? value.map(String).includes(String(optionValue))
                            : String(value) === String(optionValue);
                        return `<option value="${escapeHtml(optionValue)}" ${selected ? "selected" : ""}>${escapeHtml(option.label || optionValue)}</option>`;
                    }).join("")}
                </select>
            </label>
        `;
    }
    return `
        <label class="generic-parameter">
            <span>${escapeHtml(parameter.name)}</span>
            <input type="${type === "number" ? "number" : "text"}"
                data-generic-parameter="${escapeHtml(parameter.id)}"
                value="${escapeHtml(value)}">
        </label>
    `;
}

function renderTruenoPanel() {
    normalizeTruenoMode();
    $("#workspace-panel").innerHTML = `
        <div class="section-head">
            <h2>模型推理</h2>
            <p class="status-line" id="status-line">选择图片和模型后开始单图推理。</p>
        </div>
        <div class="tool-row trueno-toolbar">
            ${uploadControl(".jpg,.jpeg,.png,.webp,.bmp", "导入图片")}
            <button type="button" id="trueno-mode-button" class="trueno-mode-button"></button>
            ${truenoModelControl()}
            <button type="button" class="primary" id="trueno-infer-button">模型推理</button>
            <button type="button" id="trueno-inference-mode-button">推理链路</button>
            <button type="button" id="trueno-calibration-button">标定框</button>
            <button type="button" id="trueno-points-button" class="hidden">点绘制</button>
            <span class="selected-file" id="selected-file-name">${selectedUploadLabel()}</span>
        </div>
        <div class="trueno-image-area">
            <div class="trueno-image-viewport" id="trueno-image-viewport">
                <canvas id="trueno-image-canvas"></canvas>
            </div>
        </div>
        <div id="trueno-result-panel" class="trueno-result-panel hidden"></div>
    `;
    $("#upload-file").addEventListener("change", uploadFile);
    updateTruenoModeButton();
    $("#trueno-mode-button").addEventListener("click", cycleTruenoMode);
    updateTruenoInferenceButton();
    $("#trueno-inference-mode-button").addEventListener("click", toggleTruenoInferenceMode);
    const modelUpload = $("#trueno-model-upload");
    if (modelUpload) {
        modelUpload.addEventListener("change", uploadTruenoModels);
    } else {
        $("#trueno-model-button").addEventListener("click", toggleTruenoModelMenu);
    }
    $("#trueno-infer-button").addEventListener("click", () => startAction("infer"));
    $("#trueno-calibration-button").addEventListener("click", toggleTruenoCalibration);
    $("#trueno-points-button").addEventListener("click", toggleTruenoPoints);
    $("#trueno-image-canvas").addEventListener("click", handleTruenoCanvasClick);
    $("#trueno-image-canvas").addEventListener("contextmenu", finishTruenoCalibration);
    $("#trueno-image-viewport").addEventListener("wheel", zoomTruenoImage, { passive: false });
    renderTruenoModelOptions();
    renderTruenoImage();
    renderTruenoResult();
}

function normalizeTruenoMode() {
    if (!["auto", "local", "import"].includes(state.truenoMode)) {
        state.truenoMode = "auto";
    }
}

function normalizeTruenoInferenceMode() {
    if (!["local", "src"].includes(state.truenoInferenceMode)) {
        state.truenoInferenceMode = "local";
    }
}

function updateTruenoInferenceButton() {
    const button = $("#trueno-inference-mode-button");
    if (!button) return;
    normalizeTruenoInferenceMode();
    button.textContent = "推理链路";
    button.classList.toggle("mode-src", state.truenoInferenceMode === "src");
    button.classList.toggle("mode-local", state.truenoInferenceMode === "local");
    button.title = state.truenoInferenceMode === "src"
        ? "当前使用 algorithm/src 推理，点击切换为本地自推理"
        : "当前使用本地自推理，点击切换为 algorithm/src 推理";
}

function toggleTruenoInferenceMode() {
    normalizeTruenoInferenceMode();
    state.truenoInferenceMode = state.truenoInferenceMode === "local" ? "src" : "local";
    updateTruenoInferenceButton();
    state.truenoResult = null;
    renderTruenoImage();
    renderTruenoResult();
    setStatus(state.truenoInferenceMode === "src"
        ? "已切换为 algorithm/src 推理。"
        : "已切换为本地自推理。");
}

function truenoModeLabel(mode = state.truenoMode) {
    return {
        auto: "自动",
        local: "本地",
        import: "导入",
    }[mode] || "自动";
}

function updateTruenoModeButton() {
    const button = $("#trueno-mode-button");
    if (!button) return;
    normalizeTruenoMode();
    button.textContent = "模型调用";
    button.classList.remove("mode-auto", "mode-local", "mode-import");
    button.classList.add(`mode-${state.truenoMode}`);
    button.title = `单击切换模型调用模式（${truenoModeLabel()}）`;
}

function truenoModelControl() {
    if (state.truenoMode === "import") {
        return `
            <label class="upload-label-button trueno-model-upload">
                上传模型
                <input type="file" id="trueno-model-upload"
                    accept=".pt,.onnx,.torchscript,.engine" multiple>
            </label>
        `;
    }
    return `
        <div class="trueno-model-picker">
            <button type="button" id="trueno-model-button">选择模型</button>
            <div class="trueno-model-menu hidden" id="trueno-model-menu">
                <label>类别<select id="trueno-ability-select"></select></label>
                <label>模型<select id="trueno-model-select"></select></label>
            </div>
        </div>
    `;
}

function cycleTruenoMode() {
    const modes = ["auto", "local", "import"];
    const current = modes.indexOf(state.truenoMode);
    state.truenoMode = modes[(current + 1) % modes.length];
    localStorage.setItem("portal_trueno_mode", state.truenoMode);
    if (state.truenoMode !== "import") {
        state.truenoCatalog = null;
    }
    closeTruenoModelMenu();
    renderTruenoPanel();
    if (state.truenoMode !== "import") {
        loadTruenoCatalog();
    }
    setStatus("模型调用模式已切换。");
}

async function toggleTruenoModelMenu(event) {
    event.stopPropagation();
    const menu = $("#trueno-model-menu");
    if (!menu) return;
    if (!menu.classList.contains("hidden")) {
        const ability = $("#trueno-ability-select")?.value || "";
        const model = $("#trueno-model-select")?.value || "";
        if (!ability || !model) {
            setStatus("请选择类别和模型后再登记。");
            return;
        }
        const previousSelection = state.truenoSelection;
        saveTruenoPoints(true);
        cancelTruenoPointsAutosave();
        state.truenoSelection = { ability, model };
        if (!previousSelection ||
            previousSelection.ability !== ability ||
            previousSelection.model !== model) {
            state.truenoPointHelpers = [];
            state.truenoSavedPointHelpers = [];
            state.truenoPointSelectionKey = "";
        }
        localStorage.setItem("portal_trueno_selection", JSON.stringify(state.truenoSelection));
        menu.classList.add("hidden");
        renderTruenoModelOptions();
        setStatus("模型选择已登记，后续推理固定使用该模型。");
        return;
    }
    const button = event.currentTarget;
    button.disabled = true;
    const refreshed = await loadTruenoCatalog();
    button.disabled = false;
    if (!refreshed || state.activeModel !== "trueno") {
        menu.classList.add("hidden");
        return;
    }
    menu.classList.remove("hidden");
    setStatus(state.truenoSelection
        ? "可更换类别或模型，再次点击按钮登记。"
        : "请选择类别和模型，再次点击按钮登记。");
}

async function loadTruenoCatalog() {
    const modelId = state.activeModel;
    if (state.truenoCatalog) {
        renderTruenoModelOptions();
        return true;
    }
    if (state.truenoCatalogPromise) return state.truenoCatalogPromise;
    state.truenoCatalogLoading = true;
    const request = (async () => {
        try {
            const response = await api(`/api/trueno/catalog?refresh=${Date.now()}`, {
                cache: "no-store",
            });
            if (state.activeModel !== modelId) {
                return false;
            }
            if (!response.ok) {
                setStatus((await response.json().catch(() => ({}))).message || "读取 Trueno 模型目录失败。");
                return false;
            }
            state.truenoCatalog = await response.json();
            renderTruenoModelOptions();
            return true;
        } catch (_error) {
            if (state.activeModel === modelId) {
                setStatus("读取 Trueno 模型目录失败。");
            }
            return false;
        } finally {
            state.truenoCatalogLoading = false;
            state.truenoCatalogPromise = null;
        }
    })();
    state.truenoCatalogPromise = request;
    return request;
}

function renderTruenoModelOptions() {
    const abilities = $("#trueno-ability-select");
    const models = $("#trueno-model-select");
    if (!abilities || !models) return;
    normalizeTruenoMode();
    const entries = (state.truenoCatalog?.entries || []).filter(entry =>
        entry.supported !== false &&
        (entry.stages || []).some(stage =>
            stage.executable ||
            stage.source_chain ||
            stage.executor === "src"
        )
    );
    abilities.innerHTML = entries.map(entry =>
        `<option value="${escapeHtml(entry.id)}">${escapeHtml(entry.description || "")}</option>`
    ).join("");
    const candidates = state.truenoCatalog?.models || [];
    models.innerHTML = `<option value="">按能力默认模型</option>` + candidates.map(model =>
        `<option value="${escapeHtml(model.relative_path)}" title="${escapeHtml(model.name)}">${escapeHtml(truenoModelOptionLabel(model))}</option>`
    ).join("");
    models.disabled = state.truenoMode === "auto";
    const abilityById = new Map(entries.map(entry => [entry.id, entry]));
    const applyMappedModel = () => {
        const mapped = abilityById.get(abilities.value)?.mapped_model?.relative_path;
        if (mapped && [...models.options].some(option => option.value === mapped)) {
            models.value = mapped;
        } else {
            models.value = candidates[0]?.relative_path || "";
        }
    };
    const resetPointState = () => {
        saveTruenoPoints(true);
        cancelTruenoPointsAutosave();
        state.truenoPointHelpers = [];
        state.truenoSavedPointHelpers = [];
        state.truenoPointSelectionKey = "";
        state.truenoPointPage = 0;
        state.truenoPointLastSaved = "";
    };
    abilities.onchange = () => {
        resetPointState();
        applyMappedModel();
        if (state.truenoMode === "auto" && abilities.value && models.value) {
            state.truenoSelection = {
                ability: abilities.value,
                model: models.value,
            };
            localStorage.setItem("portal_trueno_selection", JSON.stringify(state.truenoSelection));
        }
        updateTruenoPointPanel();
    };
    models.onchange = () => {
        resetPointState();
        updateTruenoPointPanel();
    };
    let saved = null;
    try {
        saved = JSON.parse(localStorage.getItem("portal_trueno_selection") || "null");
    } catch (_error) {
        localStorage.removeItem("portal_trueno_selection");
    }
    const savedIsValid = saved?.ability &&
        saved?.model &&
        entries.some(entry => entry.id === saved.ability) &&
        [...models.options].some(option => option.value === saved.model);
    if (!state.truenoSelection && savedIsValid) state.truenoSelection = saved;
    if (state.truenoSelection && (
        !entries.some(entry => entry.id === state.truenoSelection.ability) ||
        ![...models.options].some(option => option.value === state.truenoSelection.model)
    )) {
        state.truenoSelection = null;
    }
    const recommended = entries.some(entry => entry.id === state.truenoCatalog?.recommended_ability)
        ? state.truenoCatalog.recommended_ability
        : entries[0]?.id || "";
    abilities.value = state.truenoSelection?.ability ||
        (savedIsValid ? saved.ability : recommended);
    applyMappedModel();
    if (state.truenoMode === "auto" && abilities.value && models.value) {
        state.truenoSelection = {
            ability: abilities.value,
            model: models.value,
        };
        localStorage.setItem("portal_trueno_selection", JSON.stringify(state.truenoSelection));
    } else if (state.truenoSelection) {
        abilities.value = state.truenoSelection.ability;
        models.value = state.truenoSelection.model;
    } else if (saved && !savedIsValid) {
        localStorage.removeItem("portal_trueno_selection");
    }
    if (state.truenoSelection) {
        abilities.value = state.truenoSelection.ability;
        models.value = state.truenoSelection.model;
        const button = $("#trueno-model-button");
        button?.classList.add("selected");
        if (button) {
            button.textContent = state.truenoMode === "auto" ? "自动模型" : "更换模型";
            button.title = `已登记：${truenoSelectedModelLabel(state.truenoSelection.model)}`;
        }
    } else {
        const button = $("#trueno-model-button");
        button?.classList.remove("selected");
        if (button) {
            button.textContent = "选择模型";
            button.title = "选择能力和模型";
        }
    }
    updateTruenoPointPanel();
}

function truenoCatalogModel(path) {
    const normalized = String(path || "").replace(/\\/g, "/");
    return (state.truenoCatalog?.models || []).find(model =>
        String(model.relative_path || "").replace(/\\/g, "/") === normalized
    );
}

function truenoModelOptionLabel(model) {
    if (state.truenoMode === "auto") {
        return model.display_name || model.name || model.relative_path || "";
    }
    return model.name || model.relative_path || "";
}

function truenoSelectedModelLabel(path) {
    const model = truenoCatalogModel(path);
    return state.truenoMode === "auto"
        ? (model?.display_name || model?.name || path)
        : (model?.name || path);
}

function selectedTruenoEntry() {
    const ability = state.truenoSelection?.ability || $("#trueno-ability-select")?.value || "";
    return (state.truenoCatalog?.entries || []).find(entry => entry.id === ability);
}

function updateTruenoPointPanel() {
    const model = state.truenoSelection?.model ||
        $("#trueno-model-select")?.value || "";
    state.truenoNeedsPointHelpers = /at-pmcnc-single\.m/i.test(model);
    prepareTruenoPointState();
    const button = $("#trueno-points-button");
    if (button) {
        button.classList.toggle("hidden", !state.truenoNeedsPointHelpers);
        if (!state.truenoNeedsPointHelpers) {
            state.truenoPointDrawing = false;
            button.textContent = "点绘制";
            button.classList.remove("active");
        }
    }
    configureConfigBox();
    renderTruenoPointPanel();
}

function truenoPointSelectionKey() {
    const selection = state.truenoSelection;
    return selection ? `${selection.ability}::${selection.model}` : "";
}

function prepareTruenoPointState() {
    const key = truenoPointSelectionKey();
    if (state.truenoPointSelectionKey === key) return;
    state.truenoPointSelectionKey = key;
    state.truenoPointHelpers = [];
    state.truenoSavedPointHelpers = [];
    state.truenoPointPage = 0;
    state.truenoPointLastSaved = "";
    if (!key) return;
    try {
        const saved = JSON.parse(localStorage.getItem("portal_trueno_points") || "null");
        if (saved?.key !== key || !Array.isArray(saved.points)) return;
        state.truenoPointHelpers = normalizeTruenoPoints(saved.points);
        state.truenoSavedPointHelpers = normalizeTruenoPoints(saved.points);
        state.truenoPointLastSaved = JSON.stringify(state.truenoSavedPointHelpers);
    } catch (_error) {
        localStorage.removeItem("portal_trueno_points");
    }
}

function normalizeTruenoPoints(points) {
    return (Array.isArray(points) ? points : []).map((point, index) => ({
        id: String(index + 1),
        x: Number(point.x || 0),
        y: Number(point.y || 0),
        value: String(point.value ?? ""),
        stage: 3,
    }));
}

function syncTruenoPointInputs() {
    const panel = $("#trueno-point-panel");
    if (!panel) return;
    panel.querySelectorAll("[data-point-field]").forEach(input => {
        const index = Number(input.dataset.pointIndex);
        const field = input.dataset.pointField;
        if (!state.truenoPointHelpers[index]) return;
        state.truenoPointHelpers[index][field] =
            field === "value" ? input.value : Number(input.value || 0);
    });
}

function hasUnsavedTruenoPoints() {
    return JSON.stringify(normalizeTruenoPoints(state.truenoPointHelpers)) !==
        JSON.stringify(normalizeTruenoPoints(state.truenoSavedPointHelpers));
}

function renderTruenoPointPanel() {
    const panel = $("#trueno-point-panel");
    if (!panel || !state.truenoNeedsPointHelpers) return;
    const pageCount = Math.max(1, Math.ceil(state.truenoPointHelpers.length / 4));
    state.truenoPointPage = Math.min(state.truenoPointPage, pageCount - 1);
    const pageStart = state.truenoPointPage * 4;
    const visiblePoints = state.truenoPointHelpers.slice(pageStart, pageStart + 4);
    const rows = visiblePoints.map((point, pageIndex) => {
        const index = pageStart + pageIndex;
        return `
        <div class="trueno-point-row">
            <span class="trueno-point-index">${index + 1}</span>
            <input type="number" data-point-index="${index}" data-point-field="x" value="${point.x}" aria-label="X坐标">
            <input type="number" data-point-index="${index}" data-point-field="y" value="${point.y}" aria-label="Y坐标">
            <input type="text" data-point-index="${index}" data-point-field="value" value="${escapeHtml(point.value || "")}" placeholder="点数值" aria-label="点数值">
            <button type="button" class="icon-button" data-point-delete="${index}" title="删除点">×</button>
        </div>
        `;
    }).join("");
    panel.innerHTML = `
        <div class="trueno-point-intro">在图片上点击添加刻度点，输入对应数值；右键结束绘制。</div>
        <div class="trueno-point-header"><span>序号</span><span>X</span><span>Y</span><span>数值</span><span></span></div>
        <div class="trueno-point-list">${rows || '<p class="trueno-point-empty">尚未添加点</p>'}</div>
    `;
    panel.querySelectorAll("[data-point-field]").forEach(input => {
        const updatePoint = event => {
            const index = Number(event.target.dataset.pointIndex);
            const field = event.target.dataset.pointField;
            if (!state.truenoPointHelpers[index]) return;
            state.truenoPointHelpers[index][field] =
                field === "value" ? event.target.value : Number(event.target.value || 0);
            renderTruenoImage();
            scheduleTruenoPointsAutosave();
        };
        input.addEventListener("input", updatePoint);
        input.addEventListener("change", updatePoint);
        input.addEventListener("blur", () => scheduleTruenoPointsAutosave(0));
    });
    panel.querySelectorAll("[data-point-delete]").forEach(button => {
        button.addEventListener("click", () => {
            state.truenoPointHelpers.splice(Number(button.dataset.pointDelete), 1);
            state.truenoPointPage = Math.min(
                state.truenoPointPage,
                Math.max(0, Math.ceil(state.truenoPointHelpers.length / 4) - 1),
            );
            renderTruenoPointPanel();
            renderTruenoImage();
            scheduleTruenoPointsAutosave();
        });
    });
    renderTruenoPointPager();
}

function renderTruenoPointPager() {
    const pager = $("#trueno-point-pager");
    const pageLabel = $("#trueno-point-page");
    const previous = $("#trueno-point-previous");
    const next = $("#trueno-point-next");
    if (!pager || !pageLabel || !previous || !next) return;
    const pageCount = Math.max(1, Math.ceil(state.truenoPointHelpers.length / 4));
    pager.classList.toggle("hidden", pageCount <= 1);
    pageLabel.textContent = `${state.truenoPointPage + 1}/${pageCount}`;
    previous.disabled = state.truenoPointPage <= 0;
    next.disabled = state.truenoPointPage >= pageCount - 1;
}

function scheduleTruenoPointsAutosave(delay = 2500) {
    if (!state.truenoNeedsPointHelpers) return;
    cancelTruenoPointsAutosave();
    state.truenoPointAutosaveTimer = window.setTimeout(() => {
        state.truenoPointAutosaveTimer = null;
        saveTruenoPoints(true);
    }, delay);
}

function cancelTruenoPointsAutosave() {
    if (state.truenoPointAutosaveTimer) {
        window.clearTimeout(state.truenoPointAutosaveTimer);
        state.truenoPointAutosaveTimer = null;
    }
}

function toggleTruenoPoints() {
    if (!state.truenoNeedsPointHelpers) return;
    state.truenoPointDrawing = !state.truenoPointDrawing;
    if (state.truenoPointDrawing) {
        state.truenoCalibrationDrawing = false;
        state.truenoCalibrationPoints = [];
        updateTruenoCalibrationButton();
    }
    const button = $("#trueno-points-button");
    if (button) {
        button.textContent = state.truenoPointDrawing ? "点绘制中" : "点绘制";
        button.classList.toggle("active", state.truenoPointDrawing);
    }
    if (!state.truenoPointDrawing) scheduleTruenoPointsAutosave(0);
    setStatus(state.truenoPointDrawing ? "请在图片上逐点添加刻度点，右键结束。" : "点绘制已暂停。");
}

function handleTruenoCanvasClick(event) {
    if (state.truenoPointDrawing) addTruenoPoint(event);
    else addTruenoCalibrationPoint(event);
}

function addTruenoPoint(event) {
    const canvas = event.currentTarget;
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    state.truenoPointHelpers.push({
        id: String(state.truenoPointHelpers.length + 1),
        x: Math.round((event.clientX - rect.left) * scaleX),
        y: Math.round((event.clientY - rect.top) * scaleY),
        value: String(state.truenoPointHelpers.length + 1),
        stage: 3,
    });
    state.truenoPointPage = Math.floor((state.truenoPointHelpers.length - 1) / 4);
    renderTruenoPointPanel();
    renderTruenoImage();
    scheduleTruenoPointsAutosave();
    const valueInput = $("#trueno-point-panel")
        ?.querySelector(`[data-point-index="${state.truenoPointHelpers.length - 1}"][data-point-field="value"]`);
    valueInput?.focus();
    valueInput?.select();
}

function saveActiveSidePanel() {
    if (state.activeModel === "trueno" && state.truenoNeedsPointHelpers) {
        saveTruenoPoints();
        return;
    }
    saveConfig();
}

function saveTruenoPoints(silent = false) {
    syncTruenoPointInputs();
    const points = normalizeTruenoPoints(state.truenoPointHelpers);
    const serialized = JSON.stringify(points);
    if (serialized === state.truenoPointLastSaved) {
        return false;
    }
    state.truenoPointHelpers = points;
    state.truenoSavedPointHelpers = normalizeTruenoPoints(points);
    state.truenoPointSelectionKey = truenoPointSelectionKey();
    localStorage.setItem("portal_trueno_points", JSON.stringify({
        key: state.truenoPointSelectionKey,
        points: state.truenoSavedPointHelpers,
    }));
    state.truenoPointLastSaved = serialized;
    if (!silent) {
        setStatus(`点绘制已保存：${state.truenoSavedPointHelpers.length} 个点。`);
        renderTruenoPointPanel();
        renderTruenoImage();
    }
    return true;
}

function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, character => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[character]));
}

function toggleTruenoCalibration() {
    if (
        state.truenoCalibrationDrawing ||
        state.truenoCalibrationPoints.length ||
        state.truenoCalibrationPolygons.length
    ) {
        state.truenoCalibrationDrawing = false;
        state.truenoCalibrationPoints = [];
        state.truenoCalibrationPolygons = [];
        state.truenoResult = null;
        updateTruenoCalibrationButton();
        renderTruenoImage();
        renderTruenoResult();
        setStatus("标定框已清除。");
        return;
    }
    state.truenoPointDrawing = false;
    scheduleTruenoPointsAutosave(0);
    const pointsButton = $("#trueno-points-button");
    if (pointsButton) {
        pointsButton.textContent = "点绘制";
        pointsButton.classList.remove("active");
    }
    state.truenoCalibrationPoints = [];
    state.truenoCalibrationPolygons = [];
    state.truenoCalibrationDrawing = true;
    state.truenoResult = null;
    updateTruenoCalibrationButton();
    renderTruenoImage();
    renderTruenoResult();
    setStatus("左键添加标定点，右键闭合当前标定框；每个标定框至少需要 4 个点。");
}

function updateTruenoCalibrationButton() {
    const button = $("#trueno-calibration-button");
    if (!button) return;
    const active = state.truenoCalibrationDrawing ||
        state.truenoCalibrationPoints.length ||
        state.truenoCalibrationPolygons.length;
    button.classList.toggle("active", active);
    button.textContent = "标定框";
    button.title = active
        ? "点击清除标定框"
        : "点击开始绘制标定框";
}

function truenoCanvasPoint(event) {
    const canvas = event.currentTarget;
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    return [
        Math.round((event.clientX - rect.left) * scaleX),
        Math.round((event.clientY - rect.top) * scaleY),
    ];
}

function addTruenoCalibrationPoint(event) {
    if (!state.truenoCalibrationDrawing) return;
    state.truenoCalibrationPoints.push(truenoCanvasPoint(event));
    renderTruenoImage();
}

function finishTruenoCalibration(event) {
    event.preventDefault();
    if (state.truenoPointDrawing) {
        state.truenoPointDrawing = false;
        const pointsButton = $("#trueno-points-button");
        if (pointsButton) {
            pointsButton.textContent = "点绘制";
            pointsButton.classList.remove("active");
        }
        scheduleTruenoPointsAutosave(0);
        setStatus("点绘制已完成，可在右侧填写点数值。");
    }
    if (!state.truenoCalibrationDrawing) return;
    if (state.truenoCalibrationPoints.length < 4) {
        const point = truenoCanvasPoint(event);
        const previous = state.truenoCalibrationPoints[
            state.truenoCalibrationPoints.length - 1
        ];
        if (!previous || previous[0] !== point[0] || previous[1] !== point[1]) {
            state.truenoCalibrationPoints.push(point);
        }
    }
    if (state.truenoCalibrationPoints.length < 4) {
        updateTruenoCalibrationButton();
        setStatus("标定框至少需要 4 个点，当前未闭合。");
        renderTruenoImage();
        return;
    }
    state.truenoCalibrationPolygons.push([...state.truenoCalibrationPoints]);
    state.truenoCalibrationPoints = [];
    updateTruenoCalibrationButton();
    setStatus(`已闭合第 ${state.truenoCalibrationPolygons.length} 个标定框，可继续绘制。`);
    renderTruenoImage();
}

function renderTruenoImage() {
    const canvas = $("#trueno-image-canvas");
    if (!canvas) return;
    const imageSource = state.truenoResult?.annotated_image || state.truenoImageDataUrl;
    canvas.style.transform = `scale(${state.truenoZoom})`;
    canvas.style.transformOrigin =
        `${state.truenoZoomOrigin.x}% ${state.truenoZoomOrigin.y}%`;
    if (!imageSource) {
        canvas.width = 1;
        canvas.height = 1;
        canvas.classList.add("empty");
        return;
    }
    const image = new Image();
    image.onload = () => {
        canvas.width = image.naturalWidth;
        canvas.height = image.naturalHeight;
        canvas.classList.remove("empty");
        canvas.getContext("2d").drawImage(image, 0, 0);
        const context = canvas.getContext("2d");
        if (!state.truenoResult?.annotated_image) {
            const polygons = [
                ...state.truenoCalibrationPolygons.map(points => ({
                    points,
                    closed: true,
                })),
                ...(state.truenoCalibrationPoints.length
                    ? [{ points: state.truenoCalibrationPoints, closed: false }]
                    : []),
            ];
            polygons.forEach(({ points, closed }) => {
                context.beginPath();
                points.forEach((point, index) => {
                    if (index === 0) context.moveTo(point[0], point[1]);
                    else context.lineTo(point[0], point[1]);
                });
                if (closed) context.closePath();
                context.strokeStyle = "#f5b700";
                context.lineWidth = Math.max(2, canvas.width / 500);
                context.stroke();
                points.forEach(point => {
                    context.beginPath();
                    context.arc(point[0], point[1], Math.max(4, canvas.width / 180), 0, Math.PI * 2);
                    context.fillStyle = "#f5b700";
                    context.fill();
                });
            });
        }
        if (state.truenoPointHelpers.length) {
            state.truenoPointHelpers.forEach((point, index) => {
                context.beginPath();
                context.arc(
                    point.x,
                    point.y,
                    Math.max(5, canvas.width / 220),
                    0,
                    Math.PI * 2,
                );
                context.fillStyle = "#ff6b35";
                context.fill();
                context.fillStyle = "#ffffff";
                context.font = `${Math.max(14, canvas.width / 180)}px Arial`;
                context.fillText(String(index + 1), point.x + 8, point.y - 8);
            });
        }
    };
    image.src = imageSource;
}

function renderTruenoResult() {
    const panel = $("#trueno-result-panel");
    if (!panel) return;
    const result = state.truenoResult;
    if (!result) {
        panel.classList.add("hidden");
        panel.innerHTML = "";
        return;
    }
    const categories = Array.isArray(result.categories)
        ? result.categories
        : (Array.isArray(result.detections) ? result.detections : []);
    const rows = categories.length
        ? categories.map((category, index) => {
            const displayName = category.display_name ||
                category.translated_name ||
                category.class_name ||
                category.value ||
                "未命名类别";
            const rawName = category.class_name && category.class_name !== displayName
                ? `（${category.class_name}）`
                : "";
            const confidenceValue = category.confidence;
            const confidence = confidenceValue !== null &&
                confidenceValue !== undefined &&
                confidenceValue !== "" &&
                Number.isFinite(Number(confidenceValue))
                ? `${(Number(confidenceValue) * 100).toFixed(1)}%`
                : "-";
            const source = category.source_key || category.key || `结果 ${index + 1}`;
            return `
                <div class="trueno-result-row">
                    <span class="trueno-result-key">${escapeHtml(source)}</span>
                    <strong>${escapeHtml(displayName)}${escapeHtml(rawName)}</strong>
                    <span>${escapeHtml(confidence)}</span>
                </div>
            `;
        }).join("")
        : `<div class="trueno-result-empty">${escapeHtml(result.desc || "未返回分类结果。")}</div>`;
    const status = result.status === "detected" ? "已识别" :
        result.status === "empty" ? "未识别" : (result.status || "完成");
    const chain = result.inference_mode === "src" ? "algorithm/src" : "本地自推理";
    panel.classList.remove("hidden");
    panel.innerHTML = `
        <div class="trueno-result-head">
            <strong>推理结果</strong>
            <span>${escapeHtml(status)} · ${escapeHtml(chain)}</span>
        </div>
        <div class="trueno-result-list">${rows}</div>
        ${result.desc ? `<p class="trueno-result-desc">${escapeHtml(result.desc)}</p>` : ""}
    `;
}

function zoomTruenoImage(event) {
    const canvas = $("#trueno-image-canvas");
    if (!canvas || canvas.classList.contains("empty")) return;
    event.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const x = Math.max(0, Math.min(100, ((event.clientX - rect.left) / rect.width) * 100));
    const y = Math.max(0, Math.min(100, ((event.clientY - rect.top) / rect.height) * 100));
    const factor = event.deltaY < 0 ? 1.12 : 1 / 1.12;
    state.truenoZoom = Math.max(1, Math.min(4, state.truenoZoom * factor));
    state.truenoZoomOrigin = { x, y };
    renderTruenoImage();
}

function renderTools() {
    const row = $("#tool-row");
    if (state.activeModel === "trueno") {
        return;
    } else if (state.activeModel === "weekly") {
        row.innerHTML = `
            ${uploadControl(".xlsx")}
            <span class="selected-file" id="selected-file-name">${selectedUploadLabel()}</span>
            <input type="text" id="weekly-site" placeholder="站点关键字">
            <select id="weekly-accuracy">
                <option value="raw" selected>原始准确率</option>
                <option value="audited">审核后准确率</option>
            </select>
            <select id="weekly-clipboard">
                <option value="text">文字+表格</option>
                <option value="image">整页图片</option>
            </select>
            <button type="button" id="copy-report-button">复制最新周报</button>
        `;
        $("#copy-report-button").addEventListener("click", copyLatestReport);
    } else if (state.activeModel === "annotation") {
        row.innerHTML = `<span class="hint">标注任务通过后台浏览器采集和处理，不需要上传文件。</span>`;
    } else {
        const accept = state.activeModel === "log" ? ".txt" : ".zip";
        row.innerHTML = `
            ${uploadControl(accept)}
            <span class="selected-file" id="selected-file-name">${selectedUploadLabel()}</span>
            <span class="hint">${uploadHint(state.activeModel)}</span>
            ${state.activeModel === "offline" ? offlineConfigControl() : ""}
        `;
    }
    const uploadInput = $("#upload-file");
    if (uploadInput) {
        uploadInput.addEventListener("change", uploadFile);
    }
    const offlineConfig = $("#offline-config");
    if (offlineConfig) {
        offlineConfig.addEventListener("change", () => {
            state.currentConfigPath = offlineConfig.value;
        });
    }
}

function uploadControl(accept, label = "上传") {
    return `
        <label class="upload-label-button">
            ${label}
            <input type="file" id="upload-file" accept="${accept}">
        </label>
    `;
}

function selectedUploadLabel() {
    return state.selectedUploads[state.activeModel]?.name || "未选择任务文件";
}

function offlineConfigControl() {
    if (state.role !== "管理员") {
        state.currentConfigPath = "config-3.yaml";
        return `<span class="config-choice">配置：config.yaml</span>`;
    }
    const value = state.currentConfigPath || "config-1.yaml";
    state.currentConfigPath = value;
    return `
        <label>配置</label>
        <select id="offline-config">
            ${["config-1.yaml", "config-2.yaml", "config-3.yaml"].map(path => `
                <option value="${path}" ${path === value ? "selected" : ""}>${path}</option>
            `).join("")}
        </select>
    `;
}

function uploadHint(modelId) {
    if (modelId === "log") return "日志上传到 portal_inputs，转换后在 portal_outputs 下载。";
    if (modelId === "offline") return "离线包上传到 sources，模式输出在 output/newwrap。";
    if (modelId === "annotation") return "标注文件上传到 Work-txt，可点文件名本机打开。";
    return "";
}

function renderActions() {
    if (state.activeModel === "trueno") return;
    const model = activeModel();
    const actions = Array.isArray(model?.actions) ? model.actions : [];
    const grid = $("#action-grid");
    grid.innerHTML = "";
    grid.classList.toggle("generic-action-grid", Boolean(model?.generic));
    if (model?.generic) {
        grid.style.setProperty(
            "--generic-action-count",
            String(Math.max(actions.length, 1)),
        );
    } else {
        grid.style.removeProperty("--generic-action-count");
    }
    for (const action of actions) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "action-tile";
        button.disabled = !action.allowed;
        button.title = action.allowed ? action.name : "当前角色没有执行权限";
        button.innerHTML = `<strong>${action.name}</strong><span>${action.description}</span>`;
        if (action.allowed) {
            button.addEventListener("click", () => startAction(action.id));
        }
        grid.appendChild(button);
    }
}

async function uploadFile() {
    const input = $("#upload-file");
    if (!input || !input.files.length) {
        setStatus("请选择要上传的文件。");
        return;
    }
    const file = input.files[0];
    const slot = $("#upload-slot");
    const slotValue = slot ? slot.value : "";
    if (state.activeModel === "trueno") {
        state.truenoImageDataUrl = await readFileAsDataUrl(file);
        state.truenoResult = null;
        state.truenoPointHelpers = [];
        state.truenoSavedPointHelpers = [];
        state.truenoPointPage = 0;
        state.truenoPointLastSaved = "";
        state.truenoCalibrationPoints = [];
        state.truenoCalibrationPolygons = [];
        state.truenoCalibrationDrawing = false;
        localStorage.removeItem("portal_trueno_points");
        state.truenoPointDrawing = false;
        state.truenoZoom = 1;
        state.truenoZoomOrigin = { x: 50, y: 50 };
        updateTruenoCalibrationButton();
        renderTruenoPointPanel();
        renderTruenoImage();
        renderTruenoResult();
    }
    let uploadResult = null;
    const localPath = typeof file.path === "string" ? file.path.trim() : "";
    if (isAbsoluteLocalPath(localPath)) {
        setStatus("正在从本机导入文件...");
        const response = await api("/api/files/local-import", {
            method: "POST",
            body: JSON.stringify({
                model: state.activeModel,
                source_path: localPath,
                slot: slotValue,
            }),
        }).catch(() => null);
        if (response?.ok) {
            uploadResult = {
                ok: true,
                result: await response.json().catch(() => ({})),
            };
        }
    }
    if (!uploadResult) {
        const data = new FormData();
        data.set("file", file);
        if (slotValue) data.set("slot", slotValue);
        setStatus("正在上传文件... 0%");
        uploadResult = await uploadWithProgress(
            `/api/files/upload?model=${encodeURIComponent(state.activeModel)}`,
            data,
            percent => setStatus(`正在上传文件... ${percent}%`),
        ).catch(error => ({ ok: false, result: { message: error.message || "上传失败" } }));
    }
    const { ok, result } = uploadResult;
    if (!ok) {
        setStatus(result.message || "上传失败。");
        return;
    }
    state.selectedUploads[state.activeModel] = {
        path: result.path,
        name: result.display_name || result.name || result.path,
    };
    const label = $("#selected-file-name");
    if (label) label.textContent = state.selectedUploads[state.activeModel].name;
    setStatus(`已上传：${state.selectedUploads[state.activeModel].name}`);
    input.value = "";
}

async function uploadTruenoModels(event) {
    const input = event.currentTarget;
    const files = [...(input.files || [])];
    if (!files.length) return;
    const uploaded = [];
    const failed = [];
    for (const file of files) {
        let result = null;
        const localPath = typeof file.path === "string" ? file.path.trim() : "";
        if (isAbsoluteLocalPath(localPath)) {
            result = await api("/api/files/local-import", {
                method: "POST",
                body: JSON.stringify({
                    model: "trueno",
                    source_path: localPath,
                    slot: "model",
                }),
            }).then(async response => ({
                ok: response.ok,
                result: await response.json().catch(() => ({})),
            })).catch(() => null);
        }
        if (!result) {
            const data = new FormData();
            data.set("file", file);
            data.set("slot", "model");
            result = await uploadWithProgress(
                "/api/files/upload?model=trueno&slot=model",
                data,
                percent => setStatus(`正在上传模型 ${file.name}... ${percent}%`),
            ).catch(error => ({
                ok: false,
                result: { message: error.message || "上传失败" },
            }));
        }
        if (result.ok) uploaded.push(result.result.display_name || file.name);
        else failed.push(`${file.name}：${result.result.message || "上传失败"}`);
    }
    input.value = "";
    state.truenoCatalog = null;
    if (uploaded.length) {
        await loadTruenoCatalog();
        setStatus(`已导入 ${uploaded.length} 个模型，可切换到自动或本地模式使用。`);
    }
    if (failed.length) {
        setStatus(`模型导入完成，失败 ${failed.length} 个：${failed.join("；")}`);
    }
}

function isAbsoluteLocalPath(value) {
    return Boolean(value) &&
        !/fakepath/i.test(value) &&
        /^(?:[A-Za-z]:[\\/]|\\\\|\/)/.test(value);
}

function readFileAsDataUrl(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ""));
        reader.onerror = () => reject(new Error("读取图片预览失败"));
        reader.readAsDataURL(file);
    });
}

function uploadWithProgress(path, data, onProgress) {
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", path);
        xhr.setRequestHeader("Authorization", `Bearer ${state.token}`);
        xhr.upload.addEventListener("progress", event => {
            if (!event.lengthComputable || !event.total) return;
            const percent = Math.min(100, Math.round((event.loaded / event.total) * 100));
            onProgress(percent);
        });
        xhr.addEventListener("load", () => {
            if (xhr.status === 401) {
                localStorage.removeItem("portal_token");
                window.location.assign("/");
                reject(new Error("未登录"));
                return;
            }
            let result = {};
            try {
                result = xhr.responseText ? JSON.parse(xhr.responseText) : {};
            } catch (_error) {
                result = { message: xhr.responseText || "上传失败" };
            }
            if (xhr.status >= 200 && xhr.status < 300) onProgress(100);
            resolve({ ok: xhr.status >= 200 && xhr.status < 300, result });
        });
        xhr.addEventListener("error", () => reject(new Error("上传网络失败")));
        xhr.addEventListener("abort", () => reject(new Error("上传已取消")));
        xhr.send(data);
    });
}

async function startAction(actionId) {
    if (state.startingAction) return;
    state.startingAction = true;
    const input = actionInput(actionId);
    if (input === null) {
        state.startingAction = false;
        return;
    }
    try {
        if (state.activeModel === "offline") {
            const reviewLink = $("#review-link");
            if (reviewLink) {
                delete reviewLink.dataset.opened;
                reviewLink.classList.remove("visible");
                reviewLink.removeAttribute("href");
            }
        }
        if (state.activeModel === "trueno") {
            state.logLines = ["等待模型推理。"];
            state.logPage = 0;
            state.truenoResult = null;
            renderLog();
            renderTruenoImage();
            renderTruenoResult();
        }
        setStatus("任务已提交。");
        const response = await api("/api/model/run", {
            method: "POST",
            body: JSON.stringify({
                model: state.activeModel,
                action: actionId,
                input,
            }),
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) {
            setStatus(result.message || "任务提交失败。");
            return;
        }
        state.currentJob = result.job_id;
        pollJob(result.job_id);
        await refreshRunningJobs();
    } catch (_error) {
        setStatus("任务提交失败，请检查服务连接。");
    } finally {
        state.startingAction = false;
    }
}

function setupStopControls() {
    const select = $("#running-job-select");
    const button = $("#stop-job-button");
    if (!select || !button) return;
    select.addEventListener("change", () => {
        state.currentJob = select.value;
        if (select.value) pollJob(select.value);
    });
    button.addEventListener("pointerdown", startStopHold);
    button.addEventListener("pointerup", clearStopHold);
    button.addEventListener("pointerleave", clearStopHold);
    button.addEventListener("pointercancel", clearStopHold);
    button.addEventListener("click", handleStopClick);
}

function startStopHold() {
    clearStopHold();
    if (!state.runningJobs.length) return;
    state.stopHoldFired = false;
    state.stopHoldTimer = window.setTimeout(() => {
        state.stopHoldTimer = null;
        state.stopAllArmed = true;
        state.stopHoldFired = true;
        updateStopButton();
        if (state.stopArmTimer) window.clearTimeout(state.stopArmTimer);
        state.stopArmTimer = window.setTimeout(resetStopAllMode, 5000);
    }, 700);
}

function clearStopHold() {
    if (state.stopHoldTimer) {
        window.clearTimeout(state.stopHoldTimer);
        state.stopHoldTimer = null;
    }
    if (state.stopHoldFired) {
        state.suppressStopClick = true;
        state.stopHoldFired = false;
        window.setTimeout(() => {
            state.suppressStopClick = false;
        }, 0);
    }
}

function resetStopAllMode() {
    state.stopAllArmed = false;
    state.stopHoldFired = false;
    state.suppressStopClick = false;
    if (state.stopArmTimer) {
        window.clearTimeout(state.stopArmTimer);
        state.stopArmTimer = null;
    }
    updateStopButton();
}

function updateStopButton() {
    const button = $("#stop-job-button");
    if (!button) return;
    button.textContent = state.stopAllArmed ? "全部终止" : "终止";
    button.classList.toggle("danger", state.stopAllArmed);
    button.title = state.stopAllArmed ? "再次点击将终止全部运行任务" : "长按切换为全部终止";
}

async function handleStopClick() {
    clearStopHold();
    if (state.suppressStopClick) {
        state.suppressStopClick = false;
        return;
    }
    const stopAll = state.stopAllArmed;
    if (stopAll) resetStopAllMode();
    await cancelRunningJob(stopAll);
}

async function cancelRunningJob(all) {
    const select = $("#running-job-select");
    const jobId = select ? select.value : state.currentJob;
    if (!all && !jobId) {
        setStatus("没有正在运行的任务可终止。");
        return;
    }
    const response = await api("/api/job-control/cancel", {
        method: "POST",
        body: JSON.stringify({ job_id: jobId || "", all: Boolean(all) }),
    });
    const result = await response.json().catch(() => ({ message: "终止请求已发送" }));
    setStatus(result.message || "终止请求已发送。");
    await refreshRunningJobs();
    if (!all && jobId) pollJob(jobId);
    if (all && state.currentJob) pollJob(state.currentJob);
}

function actionInput(actionId) {
    if (activeModel()?.generic) {
        return genericActionInput(actionId);
    }
    if (state.activeModel === "weekly" && actionId === "generate") {
        const site = $("#weekly-site").value.trim();
        if (!site) {
            setStatus("请输入站点关键字。");
            return null;
        }
        return JSON.stringify({
            site,
            clipboard: $("#weekly-clipboard").value,
            accuracy: $("#weekly-accuracy").value,
        });
    }
    if (state.activeModel === "log") {
        const upload = state.selectedUploads.log;
        if (!upload) {
            setStatus("请先上传 txt 日志文件。");
            return null;
        }
        return upload.path;
    }
    if (state.activeModel === "offline") {
        const upload = state.selectedUploads.offline;
        if (!upload) {
            setStatus("请先上传并选择离线包 zip 文件。");
            return null;
        }
        const configSelect = $("#offline-config");
        return JSON.stringify({
            file: upload.path,
            config: configSelect ? configSelect.value : "config-3.yaml",
        });
    }
    if (state.activeModel === "trueno" && actionId === "infer") {
        const upload = state.selectedUploads.trueno;
        const ability = state.truenoSelection?.ability || "";
        const mode = state.truenoMode || "auto";
        const model = mode === "auto" ? "" : (state.truenoSelection?.model || "");
        if (!upload) {
            setStatus("请先导入图片。");
            return null;
        }
        if (mode === "import") {
            setStatus("导入模式用于上传模型，请切换到自动或本地模式后再推理。");
            return null;
        }
        if (!ability) {
            setStatus("请先点击“选择模型”登记能力和模型。");
            return null;
        }
        if (state.truenoNeedsPointHelpers && hasUnsavedTruenoPoints()) {
            setStatus("点绘制列表已修改，请先点击“保存”再推理。");
            return null;
        }
        if (state.truenoCalibrationPoints.length) {
            setStatus("当前标定框尚未闭合，请右键闭合后再推理。");
            return null;
        }
        return JSON.stringify({
            image: upload.path,
            ability,
            model,
            mode,
            inference_mode: state.truenoInferenceMode || "local",
            calibration: state.truenoCalibrationPolygons.length
                ? state.truenoCalibrationPolygons
                : null,
            helpers: state.truenoSavedPointHelpers.length
                ? { points: state.truenoSavedPointHelpers }
                : null,
        });
    }
    return "";
}

function genericActionInput(actionId) {
    const model = activeModel();
    const uploads = state.genericUploads[state.activeModel] || {};
    for (const item of model.imports || []) {
        if (item.required && !uploads[item.id]) {
            setStatus(`请先导入：${item.name}`);
            return null;
        }
    }
    const parameters = {};
    const parameterMap = new Map((model.parameters || []).map(parameter => [parameter.id, parameter]));
    for (const id of genericVisibleParameterIds(model)) {
        const parameter = parameterMap.get(id);
        const control = document.querySelector(`[data-generic-parameter="${cssEscape(id)}"]`);
        if (!parameter || !control) continue;
        const type = String(parameter.parameter_type || "text").toLowerCase();
        if (type === "checkbox") {
            parameters[id] = control.checked;
        } else if (type === "multi_select") {
            parameters[id] = [...control.selectedOptions].map(option => option.value);
        } else if (type === "number") {
            const value = control.value.trim();
            if (parameter.required && !value) {
                setStatus(`请填写：${parameter.name}`);
                return null;
            }
            parameters[id] = value === "" ? null : Number(value);
        } else {
            const value = control.value.trim();
            if (parameter.required && !value) {
                setStatus(`请填写：${parameter.name}`);
                return null;
            }
            parameters[id] = value;
        }
    }
    return JSON.stringify({
        uploads: Object.fromEntries(
            Object.entries(uploads).map(([id, upload]) => [id, upload.path]),
        ),
        parameters,
        parameter_combination: Number(state.genericParameterCombination[state.activeModel]) || 0,
    });
}

async function pollJob(jobId) {
    const response = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    if (!response.ok) {
        setStatus("读取任务状态失败。");
        return;
    }
    const job = await response.json();
    if (job.model === "trueno" && job.result) {
        state.truenoResult = job.result;
        renderTruenoImage();
        renderTruenoResult();
    }
    if (state.currentJob === jobId) {
        const lines = String(job.log || "").split(/\r?\n/);
        state.logLines = retainLatestLogLines(lines.length ? lines : ["等待任务运行。"]);
        state.logPage = Math.max(0, buildLogPages().length - 1);
        renderLog();
        setStatus(`${job.label}：${job.status}`);
        detectReviewLink(job.opened_url || job.log || "");
        rememberGeneratedFiles(job);
        if (job.model === "log" && job.result?.output_dir) {
            const link = $("#review-link");
            if (link) {
                link.href = "#";
                link.textContent = "打开表计输出目录";
                link.classList.add("visible");
                link.onclick = async event => {
                    event.preventDefault();
                    const result = await api("/api/files/open-directory", {
                        method: "POST",
                        body: JSON.stringify({
                            model: "log",
                            path: job.result.output_dir,
                        }),
                    });
                    const message = await result.json().catch(() => ({}));
                    setStatus(message.message || "已请求打开输出目录。");
                };
            }
        }
    }
    if (["queued", "running", "cancelling"].includes(job.status) && state.currentJob === jobId) {
        setTimeout(() => pollJob(jobId), 1000);
    } else {
        await refreshRunningJobs();
        await refreshFiles();
    }
}

async function refreshRunningJobs() {
    try {
        const response = await api("/api/job-control/running");
        state.runningJobs = response.ok ? await response.json() : [];
    } catch (_error) {
        state.runningJobs = [];
    }
    renderJobControls();
}

function renderJobControls() {
    const control = $("#job-control");
    const select = $("#running-job-select");
    if (!control || !select) return;
    const jobs = Array.isArray(state.runningJobs) ? state.runningJobs : [];
    control.classList.toggle("hidden", jobs.length === 0);
    if (!jobs.length) {
        select.innerHTML = "";
        resetStopAllMode();
        return;
    }

    const previous = select.value || state.currentJob || "";
    const counters = new Map();
    select.innerHTML = "";
    for (const job of jobs) {
        const name = jobActionName(job);
        const count = (counters.get(name) || 0) + 1;
        counters.set(name, count);
        const option = document.createElement("option");
        option.value = job.id;
        option.textContent = `${name}·${String(count).padStart(2, "0")}`;
        option.title = job.label || job.id;
        select.appendChild(option);
    }
    if (jobs.some(job => job.id === previous)) {
        select.value = previous;
    } else {
        select.value = jobs[0].id;
    }
    if (!state.currentJob || !jobs.some(job => job.id === state.currentJob)) {
        state.currentJob = select.value;
    }
    updateStopButton();
}

function jobActionName(job) {
    const label = String(job.label || "");
    const action = label.split("/").pop().trim();
    return action || label || job.id || "任务";
}

function rememberGeneratedFiles(job) {
    const files = Array.isArray(job.generated_files) ? job.generated_files : [];
    if (job.model === "weekly") {
        for (const file of files) {
            if (file && file.path) state.latestWeeklyFiles[file.path] = file;
        }
    }
    const latestReport = files
        .filter(file => /^output\//i.test(file.path) && /\.(md|txt)$/i.test(file.path))
        .sort((left, right) => Number(right.modified || 0) - Number(left.modified || 0))[0];
    if (latestReport) {
        state.latestReportPath = latestReport.path;
    }
}

function detectReviewLink(log) {
    const match = log.match(/https?:\/\/(?:localhost|127\.0\.0\.1|(?:\d{1,3}\.){3}\d{1,3}):\d+\/?/i);
    const link = $("#review-link");
    if (!link || !match) return;
    link.href = match[0];
    link.classList.add("visible");
    link.textContent = "打开标注界面";
    if (!link.dataset.opened) {
        link.dataset.opened = "1";
        window.open(match[0], "_blank", "noopener,noreferrer");
    }
}

async function refreshFiles() {
    if (state.activeModel === "auth") return;
    const modelId = state.activeModel;
    const response = await api(`/api/files?model=${encodeURIComponent(modelId)}`);
    if (state.activeModel !== modelId) {
        return;
    }
    if (!response.ok) {
        state.files = [];
    } else {
        state.files = await response.json();
    }
    renderFiles();
}

function renderConfigOptions() {
    const button = $("#config-menu-button");
    const menu = $("#config-menu");
    if (!button || !menu) return;
    menu.innerHTML = "";
    const configFiles = activeConfigFiles();
    if (!configFiles.includes(state.currentConfigPath)) {
        state.currentConfigPath = configFiles[0] || "";
    }
    button.disabled = !configFiles.length;
    button.title = state.currentConfigPath || "没有可用 YAML 配置";
    button.setAttribute("aria-expanded", "false");
    menu.classList.add("hidden");
    for (const file of configFiles) {
        const option = document.createElement("button");
        option.type = "button";
        option.className = file === state.currentConfigPath ? "active" : "";
        option.textContent = file;
        option.title = file;
        option.setAttribute("role", "menuitem");
        option.addEventListener("click", async event => {
            event.stopPropagation();
            state.currentConfigPath = file;
            closeConfigMenu();
            renderConfigOptions();
            await loadConfig(file);
        });
        menu.appendChild(option);
    }
}

function toggleConfigMenu(event) {
    event.stopPropagation();
    const button = $("#config-menu-button");
    const menu = $("#config-menu");
    if (!button || !menu || button.disabled) return;
    const open = menu.classList.toggle("hidden");
    button.setAttribute("aria-expanded", String(!open));
}

function closeConfigMenu() {
    const button = $("#config-menu-button");
    const menu = $("#config-menu");
    if (!button || !menu) return;
    menu.classList.add("hidden");
    button.setAttribute("aria-expanded", "false");
}

function closeTruenoModelMenu() {
    const menu = $("#trueno-model-menu");
    if (menu) menu.classList.add("hidden");
}

function activeConfigFiles() {
    return Array.isArray(activeModel()?.config_files) ? activeModel().config_files : [];
}

function renderFiles() {
    const list = $("#file-list");
    if (!list) return;
    list.innerHTML = "";
    const files = state.files.slice(0, FILE_LIST_LIMIT);
    if (!files.length) {
        return;
    }
    for (const file of files) {
        const row = document.importNode($("#file-row-template").content, true);
        const nameButton = row.querySelector(".file-name");
        nameButton.textContent = file.display_name || file.name || file.path;
        nameButton.title = file.path;
        row.querySelector(".file-size").textContent = formatSize(file.size);
        const downloadButton = row.querySelector(".download-button");
        if (file.kind === "directory") {
            nameButton.textContent = `文件夹：${file.display_name || file.name || file.path}`;
            nameButton.title = `${file.path}（双击使用文件管理器打开）`;
            nameButton.addEventListener("dblclick", () => openLocal(file.path));
            downloadButton.disabled = true;
            downloadButton.title = "文件夹不可直接下载";
        } else {
            nameButton.addEventListener("click", () => openLocal(file.path));
            downloadButton.addEventListener("click", () => downloadFile(file.path));
        }
        list.appendChild(row);
    }
}

function renderLog() {
    const logPages = buildLogPages();
    const pages = Math.max(1, logPages.length);
    state.logPage = Math.min(Math.max(state.logPage, 0), pages - 1);
    $("#job-log").textContent = logPages[state.logPage].join("\n") || "等待任务运行。";
    $("#log-page").textContent = state.logLines.length ? `${state.logPage + 1}/${pages}` : "0/0";
}

function turnLog(delta) {
    const pages = Math.max(1, buildLogPages().length);
    state.logPage = Math.min(Math.max(state.logPage + delta, 0), pages - 1);
    renderLog();
}

function buildLogPages() {
    const pages = [[]];
    let visualLines = 0;
    for (const line of state.logLines) {
        for (const wrapped of wrapLogLine(line)) {
            if (visualLines >= state.logVisualLines) {
                pages.push([]);
                visualLines = 0;
            }
            pages[pages.length - 1].push(wrapped);
            visualLines += 1;
        }
    }
    return pages.slice(-LOG_MAX_PAGES);
}

function retainLatestLogLines(lines) {
    const maxLines = Math.max(1, state.logVisualLines * LOG_MAX_PAGES);
    return lines.length > maxLines ? lines.slice(-maxLines) : lines;
}

function wrapLogLine(line) {
    const text = String(line ?? "");
    if (!text) return [""];
    const maxWidth = estimateLogWrapWidth();
    const context = logMeasureContext();
    const chunks = [];
    let current = "";
    let width = 0;
    for (const char of text) {
        const charWidth = Math.max(1, context.measureText(char).width);
        if (current && width + charWidth > maxWidth) {
            chunks.push(current);
            current = "";
            width = 0;
        }
        current += char;
        width += charWidth;
    }
    if (current || !chunks.length) chunks.push(current);
    return chunks;
}

function estimateLogWrapWidth() {
    const log = $("#job-log");
    if (!log) return 80;
    const style = window.getComputedStyle(log);
    const padding = parseFloat(style.paddingLeft || "0") + parseFloat(style.paddingRight || "0");
    return Math.max(80, log.clientWidth - padding - 24);
}

function logMeasureContext() {
    const log = $("#job-log");
    const style = log ? window.getComputedStyle(log) : null;
    const canvas = logMeasureContext.canvas || (logMeasureContext.canvas = document.createElement("canvas"));
    const context = canvas.getContext("2d");
    context.font = style?.font || "12px Arial";
    return context;
}

function configureConfigBox() {
    const box = document.querySelector(".config-box");
    if (!box) return;
    const title = $("#config-title");
    const actions = $("#config-actions");
    const picker = $("#config-picker");
    const editor = $("#config-editor");
    const pointPanel = $("#trueno-point-panel");
    const pointPager = $("#trueno-point-pager");
    const message = $("#config-message");
    const showPoints = state.activeModel === "trueno" && state.truenoNeedsPointHelpers;
    if (showPoints) {
        box.classList.remove("hidden");
        if (title) title.textContent = "点绘制";
        if (actions) actions.classList.remove("hidden");
        if (picker) picker.classList.add("hidden");
        if ($("#config-save")) $("#config-save").classList.remove("hidden");
        if (editor) editor.classList.add("hidden");
        if (message) message.classList.add("hidden");
        if (pointPanel) pointPanel.classList.remove("hidden");
        if (pointPager) pointPager.classList.remove("hidden");
        renderTruenoPointPanel();
        return;
    }
    if (title) title.textContent = "YAML 配置";
    if (actions) actions.classList.remove("hidden");
    if (picker) picker.classList.remove("hidden");
    if (editor) editor.classList.remove("hidden");
    if (message) message.classList.remove("hidden");
    if (pointPanel) pointPanel.classList.add("hidden");
    if (pointPager) pointPager.classList.add("hidden");
    const visible = shouldShowConfigBox();
    box.classList.toggle("hidden", !visible);
    if (!visible) {
        if (editor) editor.value = "";
        if (message) message.textContent = "";
        state.currentConfigPath = "";
        return;
    }
    renderConfigOptions();
}

function shouldShowConfigBox() {
    return state.role === "管理员"
        && state.activeModel !== "auth"
        && state.activeModel !== "trueno"
        && activeConfigFiles().length > 0;
}

async function downloadFile(path) {
    const response = await api(`/api/files/download?model=${encodeURIComponent(state.activeModel)}&path=${encodeURIComponent(path)}`);
    if (!response.ok) {
        const result = await response.json().catch(() => ({ message: "下载失败" }));
        setStatus(result.message || "下载失败。");
        return;
    }
    const blob = await response.blob();
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = path.split("/").pop() || "download";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
}

async function openLocal(path) {
    const response = await api("/api/files/open", {
        method: "POST",
        body: JSON.stringify({ model: state.activeModel, path }),
    });
    const result = await response.json().catch(() => ({ message: "打开请求已发送" }));
    setStatus(result.message || "打开请求已发送。");
}

async function loadConfig(selectedPath) {
    const modelId = state.activeModel;
    const selected = selectedPath || state.currentConfigPath || activeConfigFiles()[0] || "";
    if (!selected) {
        return;
    }
    const path = `&path=${encodeURIComponent(selected)}`;
    const response = await api(`/api/config?model=${encodeURIComponent(modelId)}${path}`);
    if (state.activeModel !== modelId) {
        return;
    }
    const editor = $("#config-editor");
    const message = $("#config-message");
    if (!response.ok) {
        editor.value = "";
        message.textContent = "该模型没有可在线编辑的 YAML 配置。";
        return;
    }
    const result = await response.json();
    state.currentConfigPath = result.path;
    editor.value = result.content;
    renderConfigOptions();
    message.textContent = `已载入：${result.path}`;
}

async function saveConfig() {
    if (!state.currentConfigPath) {
        const message = $("#config-message");
        if (message) message.textContent = "没有可保存的 YAML 配置。";
        return;
    }
    const response = await api("/api/config", {
        method: "PUT",
        body: JSON.stringify({
            model: state.activeModel,
            path: state.currentConfigPath,
            content: $("#config-editor").value,
        }),
    });
    const result = await response.json().catch(() => ({ message: "保存失败" }));
    const message = $("#config-message");
    if (message) message.textContent = result.message || "保存完成。";
}

async function copyLatestReport() {
    if (($("#weekly-clipboard")?.value || "text") === "image") {
        await copyLatestReportImage();
        return;
    }
    const reportPath = state.latestReportPath;
    if (!reportPath) {
        setStatus("没有找到可复制的周报文本。");
        return;
    }
    setStatus("正在准备图文周报...");
    const response = await api(`/api/files/content?model=weekly&path=${encodeURIComponent(reportPath)}`);
    if (!response.ok) {
        const result = await response.json().catch(() => ({ message: "读取周报失败" }));
        setStatus(result.message || "读取周报失败。");
        return;
    }
    const result = await response.json();
    const markdown = result.content || "";
    const plainText = reportToPlainClipboardText(markdown);
    const html = await reportMarkdownToClipboardHtml(markdown, reportPath);
    const mode = await writeRichClipboard(html, plainText);
    setStatus(mode === "rich" ? "已复制图文周报，可粘贴到微信/企微。" : "已复制文字周报，图文复制被浏览器拦截。");
}

async function copyLatestReportImage() {
    const file = latestWeeklyFile(file => /_整页周报\.png$/i.test(file.path || file.name || ""));
    if (!file) {
        setStatus("没有找到整页周报图片，请先选择“整页图片”并生成周报。");
        return;
    }
    setStatus("正在准备整页周报图片...");
    const blob = await weeklyImageBlob(file.path);
    if (!blob) {
        setStatus("读取整页周报图片失败。");
        return;
    }
    const mode = await writeImageClipboard(blob, file.display_name || file.name || "整页周报图片");
    if (mode === "image") {
        setStatus("已复制整页周报图片，可粘贴到微信/企微。");
    } else if (mode === "rich") {
        setStatus("已复制整页图片内容，可粘贴到微信/企微。");
    } else {
        setStatus("浏览器未允许复制图片，请从处理文件中下载后发送。");
    }
}

function latestWeeklyFile(predicate) {
    return Object.values(state.latestWeeklyFiles)
        .filter(file => file && file.path && predicate(file))
        .sort((left, right) => Number(right.modified || 0) - Number(left.modified || 0))[0];
}

function reportToPlainClipboardText(markdown) {
    const imagePattern = /^!\[(.*?)\]\((.*?)\)\s*$/;
    return String(markdown || "")
        .split(/\r?\n/)
        .map(line => {
            const match = imagePattern.exec(line.trim());
            if (!match) return line;
            const alt = (match[1] || "整体准确率明细").trim() || "整体准确率明细";
            return `[${alt}图片]`;
        })
        .join("\n");
}

async function reportMarkdownToClipboardHtml(markdown, reportPath) {
    const parts = [];
    const imagePattern = /^!\[(.*?)\]\((.*?)\)\s*$/;
    for (const line of String(markdown || "").split(/\r?\n/)) {
        const trimmed = line.trim();
        const imageMatch = imagePattern.exec(trimmed);
        if (imageMatch) {
            const alt = (imageMatch[1] || "表格图片").trim() || "表格图片";
            const assetPath = resolveWeeklyAssetPath(reportPath, imageMatch[2]);
            const dataUri = assetPath ? await weeklyImageDataUri(assetPath) : "";
            if (dataUri) {
                parts.push(`<div style="margin:8px 0 14px;"><img alt="${escapeHtml(alt)}" src="${dataUri}" style="max-width:100%;height:auto;display:block;"></div>`);
            } else {
                parts.push(`<div style="margin:6px 0;color:#555;">[${escapeHtml(alt)}图片]</div>`);
            }
            continue;
        }
        if (!trimmed) {
            parts.push("<br>");
            continue;
        }
        if (trimmed.startsWith("#")) {
            const level = Math.min(3, trimmed.length - trimmed.replace(/^#+/, "").length);
            const text = trimmed.slice(level).trim() || trimmed;
            parts.push(`<h${level} style="margin:8px 0 6px;font-size:${level === 1 ? 18 : 16}px;line-height:1.45;">${escapeHtml(text)}</h${level}>`);
            continue;
        }
        parts.push(`<div style="margin:0 0 6px;white-space:pre-wrap;">${escapeHtml(line)}</div>`);
    }
    return `<div style="font-family:'Microsoft YaHei',Arial,sans-serif;font-size:14px;line-height:1.55;color:#000;background:#fff;">${parts.join("\n")}</div>`;
}

function resolveWeeklyAssetPath(reportPath, rawPath) {
    const value = String(rawPath || "").trim().replace(/^['"]|['"]$/g, "");
    if (!value || /^data:/i.test(value) || /^https?:\/\//i.test(value)) return value;
    const withoutQuery = value.split(/[?#]/)[0];
    const normalized = withoutQuery.replace(/\\/g, "/");
    if (/^[a-zA-Z]:\//.test(normalized) || normalized.startsWith("/")) {
        const name = basenameOfPath(normalized);
        return findWeeklyGeneratedPathByName(name);
    }
    return normalizePortalPath(`${dirnameOfPath(reportPath)}/${normalized}`);
}

function findWeeklyGeneratedPathByName(name) {
    if (!name) return "";
    const target = name.toLowerCase();
    return Object.keys(state.latestWeeklyFiles).find(path => basenameOfPath(path).toLowerCase() === target) || "";
}

function dirnameOfPath(path) {
    const normalized = String(path || "").replace(/\\/g, "/");
    const index = normalized.lastIndexOf("/");
    return index >= 0 ? normalized.slice(0, index) : "";
}

function basenameOfPath(path) {
    const normalized = String(path || "").replace(/\\/g, "/");
    const clean = normalized.split(/[?#]/)[0];
    const index = clean.lastIndexOf("/");
    return index >= 0 ? clean.slice(index + 1) : clean;
}

function normalizePortalPath(path) {
    const parts = [];
    for (const part of String(path || "").replace(/\\/g, "/").split("/")) {
        if (!part || part === ".") continue;
        if (part === "..") {
            parts.pop();
            continue;
        }
        parts.push(part);
    }
    return parts.join("/");
}

async function weeklyImageDataUri(path) {
    if (/^data:/i.test(path) || /^https?:\/\//i.test(path)) return path;
    const blob = await weeklyImageBlob(path);
    return blob ? blobToDataUri(blob) : "";
}

async function weeklyImageBlob(path) {
    const response = await api(`/api/files/download?model=weekly&path=${encodeURIComponent(path)}`);
    if (!response.ok) return null;
    const blob = await response.blob();
    if (!blob.type.startsWith("image/")) return null;
    return blob;
}

function blobToDataUri(blob) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ""));
        reader.onerror = () => reject(reader.error || new Error("图片编码失败"));
        reader.readAsDataURL(blob);
    });
}

async function writeRichClipboard(html, plainText) {
    if (navigator.clipboard && window.ClipboardItem) {
        try {
            const item = new ClipboardItem({
                "text/html": new Blob([html], { type: "text/html;charset=utf-8" }),
                "text/plain": new Blob([plainText], { type: "text/plain;charset=utf-8" }),
            });
            await navigator.clipboard.write([item]);
            return "rich";
        } catch (_error) {
            // LAN HTTP pages may not get async clipboard permission; fall back to selection copy.
        }
    }
    if (copyHtmlBySelection(html)) return "rich";
    if (navigator.clipboard) {
        await navigator.clipboard.writeText(plainText);
        return "text";
    }
    copyTextBySelection(plainText);
    return "text";
}

async function writeImageClipboard(blob, alt) {
    const imageType = blob.type || "image/png";
    if (navigator.clipboard && window.ClipboardItem) {
        try {
            await navigator.clipboard.write([new ClipboardItem({ [imageType]: blob })]);
            return "image";
        } catch (_error) {
            // Some browsers allow HTML copy but block direct image clipboard writes.
        }
    }
    const dataUri = await blobToDataUri(blob).catch(() => "");
    if (dataUri && copyHtmlBySelection(`<img alt="${escapeHtml(alt)}" src="${dataUri}" style="max-width:100%;height:auto;display:block;">`)) {
        return "rich";
    }
    return "none";
}

function copyHtmlBySelection(html) {
    const container = document.createElement("div");
    container.contentEditable = "true";
    container.style.position = "fixed";
    container.style.left = "-10000px";
    container.style.top = "0";
    container.style.width = "900px";
    container.innerHTML = html;
    document.body.appendChild(container);
    const range = document.createRange();
    range.selectNodeContents(container);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    let ok = false;
    try {
        ok = document.execCommand("copy");
    } catch (_error) {
        ok = false;
    }
    selection.removeAllRanges();
    container.remove();
    return ok;
}

function copyTextBySelection(text) {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.style.position = "fixed";
    textarea.style.left = "-10000px";
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    try {
        document.execCommand("copy");
    } catch (_error) {}
    textarea.remove();
}

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

async function createCode() {
    const role = $("#code-role").value;
    const response = await api("/api/admin/codes", {
        method: "POST",
        body: JSON.stringify({ role }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
        setStatus(result.message || "创建授权码失败。");
        return;
    }
    await navigator.clipboard.writeText(result.code).catch(() => {});
    setStatus(`已创建并复制授权码：${result.code}`);
    await loadCodes();
}

async function loadCodes() {
    if (state.activeModel !== "auth") return;
    const modelId = state.activeModel;
    const response = await api("/api/admin/codes");
    if (state.activeModel !== modelId) {
        return;
    }
    if (!response.ok) {
        const result = await response.json().catch(() => ({ message: "读取授权码失败" }));
        setStatus(result.message || "读取授权码失败。");
        return;
    }
    state.codes = await response.json();
    renderCodes();
}

function renderCodes() {
    const list = $("#code-list");
    if (!list) return;
    list.innerHTML = "";
    if (!state.codes.length) {
        list.textContent = "暂无授权码。";
        return;
    }
    for (const code of state.codes) {
        const row = document.createElement("div");
        row.className = "code-row";
        row.innerHTML = `
            <button type="button" class="code-value" title="点击复制">${code.code}</button>
            <span>${code.role}</span>
            <span>${code.used ? "已使用" : "未使用"}</span>
            <span>${code.created_at || ""}</span>
        `;
        row.querySelector(".code-value").addEventListener("click", async () => {
            await navigator.clipboard.writeText(code.code).catch(() => {});
            setStatus(`已复制授权码：${code.code}`);
        });
        list.appendChild(row);
    }
}

async function logout() {
    window.location.assign("/features");
}

function setStatus(text) {
    const line = $("#status-line");
    if (line) line.textContent = text;
}

function formatSize(value) {
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / 1024 / 1024).toFixed(1)} MB`;
}
