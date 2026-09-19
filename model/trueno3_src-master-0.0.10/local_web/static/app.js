/* 本地推理网页的交互逻辑：配置加载、图片预览、ROI 绘制和结果渲染。 */

const state = {
  config: null,
  models: [],
  image: null,
  displayImage: null,
  imageDataUrl: "",
  roi: null,
  annotationMode: "roi",
  points: [],
  helperBoxes: [],
  boxAdjustments: [],
  dragging: false,
  dragStart: null,
  dragCurrent: null,
  adjustTargetIndex: null,
  lastResult: null,
  operatorsExpanded: false,
};

const elements = {};

document.addEventListener("DOMContentLoaded", () => {
  [
    "runtimeStatus",
    "abilitySelect",
    "modelSelect",
    "abilityMeta",
    "modelMeta",
    "stageCount",
    "stagePlan",
    "deviceValue",
    "imageInput",
    "imageMeta",
    "roiValue",
    "clearRoiButton",
    "clearAnnotationsButton",
    "roiMode",
    "pointMode",
    "boxMode",
    "adjustMode",
    "helperPointName",
    "helperStageSelect",
    "annotationValue",
    "inferButton",
    "errorMessage",
    "refreshConfigButton",
    "resetImageButton",
    "canvasSize",
    "emptyWorkspace",
    "imageCanvas",
    "canvasOverlay",
    "canvasOverlayText",
    "roiHint",
    "interactionState",
    "resultState",
    "resultSummary",
    "detectionCount",
    "detectionList",
    "elapsedValue",
    "stageList",
    "contextCount",
    "contextList",
    "operatorList",
    "operatorToggleButton",
    "confInput",
    "iouInput",
    "imgszInput",
  ].forEach((id) => {
    elements[id] = document.getElementById(id);
  });

  bindEvents();
  loadConfiguration();
});

function bindEvents() {
  elements.imageInput.addEventListener("change", handleImageUpload);
  elements.abilitySelect.addEventListener("change", handleAbilityChange);
  elements.modelSelect.addEventListener("change", handleModelChange);
  elements.clearRoiButton.addEventListener("click", clearRoi);
  elements.clearAnnotationsButton.addEventListener("click", clearAnnotations);
  elements.inferButton.addEventListener("click", runInference);
  elements.refreshConfigButton.addEventListener("click", loadConfiguration);
  elements.resetImageButton.addEventListener("click", resetImage);
  elements.operatorToggleButton.addEventListener("click", toggleOperators);
  document.querySelectorAll('input[name="annotationMode"]').forEach((input) => {
    input.addEventListener("change", () => {
      state.annotationMode = input.value;
      setInteraction(`标注模式：${annotationModeLabel(state.annotationMode)}`);
    });
  });

  const canvas = elements.imageCanvas;
  canvas.addEventListener("pointerdown", startRoi);
  canvas.addEventListener("pointermove", moveRoi);
  canvas.addEventListener("pointerup", finishRoi);
  canvas.addEventListener("pointercancel", finishRoi);
}

async function loadConfiguration() {
  clearError();
  setInteraction("读取本地配置...");
  try {
    const [configResponse, modelsResponse] = await Promise.all([
      fetch("/api/config", { cache: "no-store" }),
      fetch("/api/models", { cache: "no-store" }),
    ]);
    const config = await parseResponse(configResponse);
    const models = await parseResponse(modelsResponse);
    state.config = config;
    state.models = models.models || [];
    renderRuntime(config.health);
    renderAbilityOptions();
    renderModelOptions();
    updateAbilityMeta();
    renderStagePlan();
    setInteraction("就绪");
  } catch (error) {
    showError(error.message);
    renderRuntime({ ok: false, message: error.message });
    setInteraction("配置加载失败");
  }
}

function renderRuntime(health) {
  const status = elements.runtimeStatus;
  status.classList.remove("ready", "error");
  if (health && health.ok) {
    status.classList.add("ready");
    status.querySelector("span:last-child").textContent =
      `本地运行时 · ${health.device || "unknown"}`;
  } else {
    status.classList.add("error");
    status.querySelector("span:last-child").textContent =
      health?.message || "运行时不可用";
  }
  elements.deviceValue.textContent = health?.device || "unknown";
}

function renderAbilityOptions() {
  const entries = state.config?.entries || [];
  const recommended = state.config?.recommended_ability || entries[0]?.id;
  elements.abilitySelect.replaceChildren();
  entries.forEach((entry) => {
    const option = document.createElement("option");
    option.value = entry.id;
    const marker = entry.supported ? "" : " ⚠";
    option.textContent = `${entry.description} · ${entry.id}${marker}`;
    // 不再禁用不支持的能力：部分算法阶段缺失时仍允许选择，
    // 推理阶段会跳过并保留已支持阶段的结果（见调用日志）。
    elements.abilitySelect.appendChild(option);
  });
  if (entries.some((entry) => entry.id === recommended)) {
    elements.abilitySelect.value = recommended;
  }
}

function renderModelOptions() {
  elements.modelSelect.replaceChildren();
  const automatic = document.createElement("option");
  automatic.value = "";
  automatic.textContent = "按描述参数自动匹配";
  elements.modelSelect.appendChild(automatic);
  state.models.forEach((model) => {
    const option = document.createElement("option");
    option.value = model.relative_path;
    option.textContent = `${model.name} · ${formatBytes(model.size)}`;
    elements.modelSelect.appendChild(option);
  });
}

function handleAbilityChange() {
  updateAbilityMeta();
  updateDefaultControls();
  renderStagePlan();
}

function handleModelChange() {
  const selected = state.models.find(
    (model) => model.relative_path === elements.modelSelect.value,
  );
  elements.modelMeta.textContent = selected
    ? `${selected.suffix.toUpperCase()} · ${formatBytes(selected.size)} · 手动指定`
    : "按能力参数自动匹配，单模型时会使用本地回退";
}

function renderStagePlan() {
  const entry = selectedAbility();
  const stages = entry?.stages || [];
  elements.stageCount.textContent = `${stages.length} 段`;
  elements.stagePlan.replaceChildren();
  elements.helperStageSelect.replaceChildren();
  const allStage = document.createElement("option");
  allStage.value = "";
  allStage.textContent = "全部阶段";
  elements.helperStageSelect.appendChild(allStage);
  stages.forEach((stage) => {
    const row = document.createElement("div");
    row.className = "stage-config-row";
    const label = document.createElement("div");
    label.className = "stage-config-label";
    label.innerHTML = `<strong>P${stage.index}</strong><span>${escapeHtml(stage.patch || stage.base)}</span>`;
    const select = document.createElement("select");
    select.className = "field-control stage-model-select";
    select.dataset.stage = String(stage.index);
    addAutomaticOption(select, stage.model?.relative_path || "");
    state.models.forEach((model) => addModelOption(select, model));
    if (stage.model?.relative_path) select.value = stage.model.relative_path;
    select.addEventListener("change", updateStageMeta);
    row.append(label, select);
    const detail = document.createElement("div");
    detail.className = "stage-config-detail";
    detail.textContent = `${stage.executor || stage.base} · ${stage.ret_keys?.join(", ") || "检测结果"}`;
    row.appendChild(detail);
    elements.stagePlan.appendChild(row);
    const stageOption = document.createElement("option");
    stageOption.value = String(stage.index);
    stageOption.textContent = `P${stage.index} · ${stage.patch || stage.base}`;
    elements.helperStageSelect.appendChild(stageOption);
  });
  if (!stages.length) {
    elements.stagePlan.innerHTML = '<div class="field-hint">该能力没有流水线阶段</div>';
  }
}

function addAutomaticOption(select, selected) {
  const option = document.createElement("option");
  option.value = "";
  option.textContent = selected ? "按描述参数" : "自动匹配";
  select.appendChild(option);
}

function addModelOption(select, model) {
  const option = document.createElement("option");
  option.value = model.relative_path;
  option.textContent = model.name;
  select.appendChild(option);
}

function updateStageMeta() {
  const selected = [...document.querySelectorAll(".stage-model-select")]
    .filter((item) => item.value)
    .map((item) => `P${item.dataset.stage}=${item.value.split("/").pop()}`);
  elements.modelMeta.textContent = selected.length
    ? selected.join(" · ")
    : "各阶段按描述参数自动匹配，顶层模型作为回退";
}

function updateAbilityMeta() {
  const entry = selectedAbility();
  if (!entry) {
    elements.abilityMeta.textContent = "没有可用能力";
    return;
  }
  const stageText = entry.stages
    .map((stage) => `P${stage.index} ${stage.patch || stage.base}`)
    .join("  /  ");
  const reasons = entry.unsupported_reasons || [];
  const supportText = entry.supported
    ? "本地适配"
    : `暂不支持 ${reasons.length} 个阶段`;
  elements.abilityMeta.textContent = `${stageText || "无流水线"} · ${supportText}`;
  elements.abilityMeta.title =
    reasons.join("\n") ||
    "各阶段均可在本地执行；多段能力缺少的算法阶段会在推理时跳过。";
  elements.modelMeta.textContent = modelHintForEntry(entry);
}

function updateDefaultControls() {
  const entry = selectedAbility();
  if (!entry) return;
  const confidence = Number(entry.default_conf);
  if (Number.isFinite(confidence)) {
    elements.confInput.value = confidence;
  }
}

function modelHintForEntry(entry) {
  const models = (entry.stages || [])
    .map((stage) => stage.model?.name)
    .filter(Boolean);
  if (!models.length) return "没有找到匹配权重，请手动选择 model 目录中的模型";
  return `${models.join(", ")} · ${entry.stages[0]?.resolution || "自动匹配"}`;
}

function selectedAbility() {
  return (state.config?.entries || []).find(
    (entry) => entry.id === elements.abilitySelect.value,
  );
}

async function handleImageUpload(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  clearError();
  try {
    state.imageDataUrl = await readFileAsDataUrl(file);
    const image = new Image();
    image.onload = () => {
      state.image = image;
      state.displayImage = image;
      state.roi = null;
      state.points = [];
      state.helperBoxes = [];
      state.boxAdjustments = [];
      state.adjustTargetIndex = null;
      state.lastResult = null;
      elements.imageMeta.textContent = `${file.name} · ${formatBytes(file.size)}`;
      elements.canvasSize.textContent = `${image.naturalWidth} × ${image.naturalHeight}`;
      elements.inferButton.disabled = false;
      elements.emptyWorkspace.hidden = true;
      elements.imageCanvas.hidden = false;
      elements.canvasOverlay.hidden = false;
      resizeCanvasToImage();
      drawCanvas();
      updateRoiLabels();
      setInteraction("图片已载入");
    };
    image.onerror = () => showError("图片无法解码。");
    image.src = state.imageDataUrl;
  } catch (error) {
    showError(error.message);
  }
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("读取图片失败。"));
    reader.readAsDataURL(file);
  });
}

function resizeCanvasToImage() {
  if (!state.image) return;
  elements.imageCanvas.width = state.image.naturalWidth;
  elements.imageCanvas.height = state.image.naturalHeight;
}

function canvasPoint(event) {
  const canvas = elements.imageCanvas;
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvas.width / rect.width;
  const scaleY = canvas.height / rect.height;
  return {
    x: clamp(Math.round((event.clientX - rect.left) * scaleX), 0, canvas.width),
    y: clamp(Math.round((event.clientY - rect.top) * scaleY), 0, canvas.height),
  };
}

function startRoi(event) {
  if (!state.image) return;
  event.preventDefault();
  elements.imageCanvas.setPointerCapture(event.pointerId);
  if (state.annotationMode === "point") {
    const point = canvasPoint(event);
    state.points.push({
      id: elements.helperPointName.value.trim() || `point_${state.points.length + 1}`,
      x: point.x,
      y: point.y,
    });
    elements.helperPointName.value = "";
    updateAnnotationLabels();
    drawCanvas();
    setInteraction("已添加标定点");
    return;
  }
  if (state.annotationMode === "adjust") {
    const point = canvasPoint(event);
    state.adjustTargetIndex = hitDetection(point);
    if (state.adjustTargetIndex === null) {
      setInteraction("请从已有检测框内部开始拖拽");
      return;
    }
  }
  state.dragging = true;
  state.dragStart = canvasPoint(event);
  state.dragCurrent = state.dragStart;
  elements.canvasOverlayText.textContent = `绘制${annotationModeLabel(state.annotationMode)}`;
  setInteraction(`正在绘制${annotationModeLabel(state.annotationMode)}`);
  drawCanvas();
}

function moveRoi(event) {
  if (!state.dragging) return;
  state.dragCurrent = canvasPoint(event);
  drawCanvas();
}

function finishRoi(event) {
  if (!state.dragging) return;
  state.dragCurrent = canvasPoint(event);
  const box = normalizedRoi(state.dragStart, state.dragCurrent);
  if (state.annotationMode === "roi") {
    state.roi = box;
  } else if (state.annotationMode === "box" && box) {
    state.helperBoxes.push({
      id: `manual_box_${state.helperBoxes.length + 1}`,
      bbox: box,
      class_name: "manual",
      stage: elements.helperStageSelect.value || null,
      use_as_parent: true,
    });
  } else if (state.annotationMode === "adjust" && box) {
    state.boxAdjustments.push({
      bbox: box,
      stage: elements.helperStageSelect.value || null,
      index: state.adjustTargetIndex,
    });
  }
  state.dragging = false;
  state.adjustTargetIndex = null;
  elements.canvasOverlayText.textContent = "拖拽绘制标注";
  updateRoiLabels();
  updateAnnotationLabels();
  setInteraction(`${annotationModeLabel(state.annotationMode)} 已设置`);
  drawCanvas();
}

function normalizedRoi(start, end) {
  if (!start || !end) return null;
  const x1 = Math.min(start.x, end.x);
  const y1 = Math.min(start.y, end.y);
  const x2 = Math.max(start.x, end.x);
  const y2 = Math.max(start.y, end.y);
  return x2 - x1 >= 2 && y2 - y1 >= 2 ? [x1, y1, x2, y2] : null;
}

function clearRoi() {
  state.roi = null;
  state.dragStart = null;
  state.dragCurrent = null;
  updateRoiLabels();
  updateAnnotationLabels();
  drawCanvas();
  setInteraction("ROI 已清除");
}

function clearAnnotations() {
  state.roi = null;
  state.points = [];
  state.helperBoxes = [];
  state.boxAdjustments = [];
  state.dragStart = null;
  state.dragCurrent = null;
  state.adjustTargetIndex = null;
  updateRoiLabels();
  updateAnnotationLabels();
  drawCanvas();
  setInteraction("辅助标注已清除");
}

function hitDetection(point) {
  const detections = state.lastResult?.detections || [];
  for (let index = detections.length - 1; index >= 0; index -= 1) {
    const [x1, y1, x2, y2] = detections[index].bbox || [];
    if (point.x >= x1 && point.x <= x2 && point.y >= y1 && point.y <= y2) {
      return index;
    }
  }
  return null;
}

function drawCanvas() {
  if (!state.image) return;
  const canvas = elements.imageCanvas;
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.drawImage(state.displayImage || state.image, 0, 0);
  const roi = state.dragging
    ? normalizedRoi(state.dragStart, state.dragCurrent)
    : state.roi;
  if (roi) {
    const [x1, y1, x2, y2] = roi;
    context.fillStyle = "rgba(251, 191, 36, 0.10)";
    context.fillRect(x1, y1, x2 - x1, y2 - y1);
    context.strokeStyle = "#fbbf24";
    context.lineWidth = Math.max(3, canvas.width / 700);
    context.strokeRect(x1, y1, x2 - x1, y2 - y1);
  }
  const lineWidth = Math.max(2, canvas.width / 900);
  state.helperBoxes.forEach((item) => {
    const [x1, y1, x2, y2] = item.bbox;
    context.fillStyle = "rgba(52, 211, 153, 0.10)";
    context.fillRect(x1, y1, x2 - x1, y2 - y1);
    context.strokeStyle = "#34d399";
    context.lineWidth = lineWidth;
    context.strokeRect(x1, y1, x2 - x1, y2 - y1);
  });
  state.boxAdjustments.forEach((item) => {
    const [x1, y1, x2, y2] = item.bbox;
    context.strokeStyle = "#f472b6";
    context.setLineDash([8, 5]);
    context.lineWidth = lineWidth;
    context.strokeRect(x1, y1, x2 - x1, y2 - y1);
    context.setLineDash([]);
  });
  state.points.forEach((point) => {
    context.fillStyle = "#f87171";
    context.beginPath();
    context.arc(point.x, point.y, Math.max(5, canvas.width / 160), 0, Math.PI * 2);
    context.fill();
    context.fillStyle = "#fff";
    context.font = `${Math.max(12, canvas.width / 80)}px sans-serif`;
    context.fillText(point.id, point.x + 8, point.y - 8);
  });
}

function updateRoiLabels() {
  if (!state.roi) {
    elements.roiValue.textContent = "全图推理";
    elements.roiHint.textContent = "ROI: 全图";
    return;
  }
  const [x1, y1, x2, y2] = state.roi;
  const value = `[${x1}, ${y1}, ${x2}, ${y2}]`;
  elements.roiValue.textContent = value;
  elements.roiHint.textContent = `ROI: ${value}`;
}

function updateAnnotationLabels() {
  elements.annotationValue.textContent =
    `ROI ${state.roi ? "已设置" : "全图"} · 点 ${state.points.length} · 框 ${state.helperBoxes.length} · 修正 ${state.boxAdjustments.length}`;
}

async function runInference() {
  if (!state.imageDataUrl || !selectedAbility()) {
    showError("请先选择图片和识别能力。");
    return;
  }
  clearError();
  elements.inferButton.disabled = true;
  elements.resultState.textContent = "推理中";
  elements.resultState.className = "result-badge neutral";
  setInteraction("模型推理中...");
  appendLogs([
    `[网页] 开始调用 ${elements.abilitySelect.value}`,
    `[网页] ROI ${state.roi ? state.roi.join(",") : "全图"}`,
  ]);
  try {
    // 每次重新推理都从原图开始，避免把上一次标注结果再次送入模型。
    state.displayImage = state.image;
    const payload = {
      image: state.imageDataUrl,
      ability: elements.abilitySelect.value,
      model: elements.modelSelect.value,
      stage_models: collectStageModels(),
      roi: state.roi,
      helpers: {
        points: state.points,
        boxes: state.helperBoxes,
        box_adjustments: state.boxAdjustments,
      },
      conf: Number(elements.confInput.value),
      iou: Number(elements.iouInput.value),
      imgsz: Number(elements.imgszInput.value),
    };
    const response = await fetch("/api/infer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await parseResponse(response);
    state.lastResult = result;
    renderResult(result);
    appendLogs(result.logs || []);
    setInteraction("推理完成");
  } catch (error) {
    showError(error.message);
    elements.resultState.textContent = "失败";
    elements.resultState.className = "result-badge error";
    setInteraction("推理失败");
  } finally {
    elements.inferButton.disabled = false;
  }
}

function renderResult(result) {
  const detected = result.status === "detected";
  elements.resultState.textContent = detected ? "已检测" : "无结果";
  elements.resultState.className = `result-badge ${detected ? "success" : "empty"}`;
  elements.resultSummary.className = `result-summary ${detected ? "" : "empty-result"}`;
  elements.resultSummary.innerHTML = `
    <strong>${escapeHtml(result.desc || result.description || "推理完成")}</strong>
    <span>最高置信度 ${formatConfidence(result.confidence)} · 返回值 ${escapeHtml(String(result.value ?? "0"))}</span>
  `;
  elements.detectionCount.textContent = `${result.detections?.length || 0} 项`;
  renderDetections(result.detections || []);
  elements.elapsedValue.textContent = `${formatNumber(result.elapsed_ms)} ms`;
  renderStages(result.stages || []);
  renderContext(result.inference_context || {});
  renderOperators(result.models || []);
  if (result.annotated_image) {
    const annotated = new Image();
    annotated.onload = () => {
      state.displayImage = annotated;
      resizeCanvasToImage();
      drawCanvas();
    };
    annotated.src = result.annotated_image;
  }
}

function collectStageModels() {
  const values = {};
  document.querySelectorAll(".stage-model-select").forEach((select) => {
    if (select.value) values[select.dataset.stage] = select.value;
  });
  return values;
}

function renderDetections(detections) {
  if (!detections.length) {
    elements.detectionList.className = "detection-list empty-list";
    elements.detectionList.textContent = "暂无检测框";
    return;
  }
  elements.detectionList.className = "detection-list";
  elements.detectionList.innerHTML = detections
    .map((item) => `
      <div class="detection-item">
        <span class="detection-name" title="${escapeHtml(item.class_name)}">${item.stage != null ? `<em class="detection-stage">P${item.stage}</em>` : ""}${escapeHtml(item.display_name)}</span>
        <span class="detection-conf">${formatConfidence(item.confidence)}</span>
        <span class="detection-bbox">bbox ${item.bbox.map((value) => Math.round(value)).join(", ")}</span>
      </div>
    `)
    .join("");
}

function renderStages(stages) {
  if (!stages.length) {
    elements.stageList.className = "stage-list empty-list";
    elements.stageList.textContent = "等待推理";
    return;
  }
  elements.stageList.className = "stage-list";
  elements.stageList.innerHTML = stages
    .map((stage) => `
      <div class="stage-item">
        <div class="stage-topline">
          <span class="stage-label">P${stage.index} ${escapeHtml(stage.patch || stage.base || "stage")} · ${escapeHtml(stage.executor || "model")}</span>
          <span class="stage-time">${formatNumber(stage.elapsed_ms)} ms</span>
        </div>
        <div class="stage-detail">
          raw ${stage.raw_count ?? 0} · returned ${stage.returned_count ?? stage.detections?.length ?? 0}
          ${stage.model_path ? ` · ${escapeHtml(stage.model_path)}` : ""}
        </div>
      </div>
    `)
    .join("");
}

function renderContext(context) {
  const values = context.values || {};
  const entries = Object.entries(values).filter(([, value]) => value !== null && value !== "");
  elements.contextCount.textContent = `${entries.length} 项`;
  if (!entries.length) {
    elements.contextList.className = "context-list empty-list";
    elements.contextList.textContent = context.ret_keys?.length
      ? "阶段已执行，但没有返回可整合值"
      : "该能力未配置 ret_keys";
    return;
  }
  elements.contextList.className = "context-list";
  elements.contextList.innerHTML = entries.map(([key, value]) => `
    <div class="context-line"><span>${escapeHtml(key)}</span><strong>${escapeHtml(value)}</strong></div>
  `).join("");
}

function renderOperators(models) {
  if (!models.length) {
    elements.operatorList.className = "operator-list empty-list";
    elements.operatorList.textContent = "当前能力使用 OpenCV 或没有可展示模型";
    return;
  }
  const counts = {};
  models.forEach((model) => {
    Object.entries(model.operator_counts || {}).forEach(([name, count]) => {
      counts[name] = (counts[name] || 0) + Number(count);
    });
  });
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  elements.operatorList.className = `operator-list ${state.operatorsExpanded ? "" : "collapsed"}`;
  elements.operatorList.innerHTML = entries
    .map(([name, count]) => `
      <div class="operator-line">
        <span>${escapeHtml(name)}</span>
        <span>×${count}</span>
      </div>
    `)
    .join("");
  elements.operatorToggleButton.textContent = state.operatorsExpanded ? "收起" : "展开";
}

function toggleOperators() {
  state.operatorsExpanded = !state.operatorsExpanded;
  renderOperators(state.lastResult?.models || []);
}

function resetImage() {
  state.image = null;
  state.displayImage = null;
  state.imageDataUrl = "";
  state.roi = null;
  state.points = [];
  state.helperBoxes = [];
  state.boxAdjustments = [];
  state.adjustTargetIndex = null;
  state.lastResult = null;
  elements.imageInput.value = "";
  elements.imageCanvas.hidden = true;
  elements.canvasOverlay.hidden = true;
  elements.emptyWorkspace.hidden = false;
  elements.inferButton.disabled = true;
  elements.imageMeta.textContent = "未选择";
  elements.canvasSize.textContent = "等待图片";
  updateRoiLabels();
  updateAnnotationLabels();
  resetResult();
  setInteraction("就绪");
}

function resetResult() {
  elements.resultState.textContent = "待运行";
  elements.resultState.className = "result-badge neutral";
  elements.resultSummary.className = "result-summary empty-result";
  elements.resultSummary.innerHTML = "<strong>还没有推理结果</strong><span>选择能力和图片后开始</span>";
  elements.detectionCount.textContent = "0 项";
  elements.detectionList.className = "detection-list empty-list";
  elements.detectionList.textContent = "暂无检测框";
  elements.elapsedValue.textContent = "-";
  elements.stageList.className = "stage-list empty-list";
  elements.stageList.textContent = "等待推理";
  elements.contextCount.textContent = "0 项";
  elements.contextList.className = "context-list empty-list";
  elements.contextList.textContent = "推理后显示各阶段返回值";
  elements.operatorList.className = "operator-list empty-list";
  elements.operatorList.textContent = "推理后显示模型结构";
}

async function parseResponse(response) {
  const payload = await response.json();
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || `请求失败 (${response.status})`);
  }
  return payload;
}

function setInteraction(value) {
  elements.interactionState.textContent = value;
}

function showError(message) {
  elements.errorMessage.hidden = false;
  elements.errorMessage.textContent = message;
}

function clearError() {
  elements.errorMessage.hidden = true;
  elements.errorMessage.textContent = "";
}

function formatBytes(value) {
  if (!Number.isFinite(Number(value))) return "-";
  const units = ["B", "KB", "MB", "GB"];
  let size = Number(value);
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index ? 1 : 0)} ${units[index]}`;
}

function formatConfidence(value) {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(3) : "-";
}

function formatNumber(value) {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(2) : "-";
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function annotationModeLabel(mode) {
  return { roi: "ROI", point: "标定点", box: "辅助框", adjust: "框修正" }[mode] || "标注";
}
