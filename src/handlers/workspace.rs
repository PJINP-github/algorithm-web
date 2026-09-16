use axum::{
    extract::{ConnectInfo, Json, Multipart, Path as AxumPath, Query, State},
    http::{HeaderMap, HeaderValue, StatusCode, header},
    response::{IntoResponse, Response},
};
use std::{
    collections::HashMap,
    fs,
    io::{BufRead, BufReader},
    net::IpAddr,
    path::{Component, Path, PathBuf},
    process::{Command, Stdio},
    sync::mpsc,
    thread,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::io::AsyncWriteExt;

use crate::{
    handlers::auth::require_session,
    log_converter,
    models::{
        ApiMessage, CodeInfo, ConfigUpdateRequest, FileContentQuery, FileContentResponse,
        FileDescriptionDeleteRequest, FileDescriptionQuery, FileInfo, FileQuery, FileRequest,
        JobInfo, JobStartResponse, LocalImportRequest, ModelRunRequest, SessionUser, UploadQuery,
        UploadResponse, WorkspaceAction, WorkspaceModel,
    },
    state::{AppState, JobControl},
};

const MAX_UPLOAD_BYTES: usize = 2 * 1024 * 1024 * 1024;
const MAX_TEXT_BYTES: u64 = 2 * 1024 * 1024;
const JOB_CANCELLED_MESSAGE: &str = "任务已终止";
const JOB_LOG_MAX_PAGES: usize = 9;

#[derive(Default, serde::Deserialize)]
struct ActionInput {
    #[serde(default)]
    file: String,
    #[serde(default)]
    config: String,
    #[serde(default)]
    site: String,
    #[serde(default)]
    clipboard: String,
    #[serde(default)]
    accuracy: String,
}

#[derive(Default, serde::Deserialize)]
struct TruenoInput {
    image: String,
    ability: String,
    #[serde(default)]
    model: String,
    #[serde(default = "default_trueno_mode")]
    mode: String,
    #[serde(default)]
    roi: serde_json::Value,
    #[serde(default)]
    helpers: serde_json::Value,
}

fn default_trueno_mode() -> String {
    "auto".to_owned()
}

#[derive(Default, serde::Deserialize)]
pub struct JobCancelRequest {
    #[serde(default)]
    job_id: String,
    #[serde(default)]
    all: bool,
}

#[derive(serde::Serialize)]
struct JobCancelResponse {
    success: bool,
    message: String,
    cancelled: Vec<String>,
}

fn message(status: StatusCode, success: bool, text: impl Into<String>) -> Response {
    (
        status,
        Json(ApiMessage {
            success,
            message: text.into(),
        }),
    )
        .into_response()
}

fn authenticated(headers: &HeaderMap, state: &AppState) -> Result<SessionUser, Response> {
    require_session(headers, state).map_err(|(status, text)| message(status, false, text))
}

fn require_permission(
    headers: &HeaderMap,
    state: &AppState,
    permission: &str,
) -> Result<SessionUser, Response> {
    let user = authenticated(headers, state)?;
    if state.authority.allows(&user.role, permission) {
        Ok(user)
    } else {
        Err(message(
            StatusCode::FORBIDDEN,
            false,
            "当前角色没有执行此操作的权限",
        ))
    }
}

fn action(id: &str, name: &str, description: &str, min_role: &str) -> WorkspaceAction {
    WorkspaceAction {
        id: id.to_owned(),
        name: name.to_owned(),
        description: description.to_owned(),
        min_role: min_role.to_owned(),
        allowed: false,
    }
}

fn model_actions(model: &str) -> Option<Vec<WorkspaceAction>> {
    Some(match model {
        "log" => vec![
            action(
                "convert",
                "转换日志",
                "上传 TXT 日志并生成 XLSX",
                "普通一级",
            ),
            action(
                "debug_meter",
                "调试表计",
                "分析 TXT 表计日志并生成 meter_output 文件夹",
                "普通一级",
            ),
        ],
        "annotation" => vec![
            action("task", "获取任务", "浏览器后台采集标注任务明细", "普通二级"),
            action("model", "模型标注", "执行模型标注交互流程", "普通二级"),
            action("sum", "训练展望分组", "生成训练展望原始分组", "普通二级"),
            action("sort", "训练展望排序", "生成训练展望最终排序", "普通二级"),
            action(
                "summary",
                "生成训练清单",
                "导出模型标注训练清单 XLSX",
                "普通二级",
            ),
        ],
        "offline" => vec![
            action("mode1", "模式 1 / M", "打开本地离线包审核界面", "普通二级"),
            action(
                "mode2",
                "模式 2 / H",
                "打开带历史数据库的审核界面",
                "普通二级",
            ),
            action("mode3", "模式 3 / Y", "整合并输出单个离线包", "普通二级"),
            action("mode4", "模式 4 / N", "按配置拆分离线包", "普通二级"),
            action("mode5", "模式 5 / R", "生成采集点位清单", "普通二级"),
        ],
        "weekly" => vec![
            action(
                "refresh",
                "刷新数据",
                "后台完成网页采集和 HTML 转 XLSX",
                "普通二级",
            ),
            action(
                "generate",
                "生成周报",
                "按站点、准确率和剪贴板模式生成周报",
                "普通二级",
            ),
            action(
                "todo",
                "更新待办",
                "运行 b 模式生成 upload-2.xlsx",
                "普通二级",
            ),
        ],
        "trueno" => vec![action(
            "infer",
            "模型推理",
            "按选择的 YOLO/OCR 模型执行单图推理",
            "普通二级",
        )],
        _ => return None,
    })
}

fn model_name(model: &str) -> &'static str {
    match model {
        "log" => "日志转换",
        "annotation" => "标注任务",
        "offline" => "离线包工具",
        "weekly" => "周报主流程",
        "trueno" => "模型推理",
        _ => "未知模型",
    }
}

fn workspace_models() -> Vec<WorkspaceModel> {
    [
        ("log", "日志转换", "Rust 原生解析 TXT 日志并输出 XLSX。"),
        (
            "annotation",
            "标注任务",
            "隐藏式调用浏览器自动化和后处理脚本。",
        ),
        (
            "offline",
            "离线包工具",
            "按模式执行离线包整理、审核和导出。",
        ),
        ("weekly", "周报主流程", "刷新数据、生成周报并复制报告内容。"),
        (
            "trueno",
            "模型推理",
            "调用 Trueno 本地 YOLO/OCR 单模型推理链路。",
        ),
    ]
    .into_iter()
    .map(|(id, name, description)| WorkspaceModel {
        id: id.to_owned(),
        name: name.to_owned(),
        description: description.to_owned(),
        actions: model_actions(id).unwrap_or_default(),
        config_files: Vec::new(),
        log_visual_lines: 19,
    })
    .collect()
}

pub async fn catalog(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let user = match require_permission(&headers, &state, "workspace.view") {
        Ok(user) => user,
        Err(response) => return response,
    };
    let mut models = workspace_models();
    models.retain(|model| state.authority.model_visible(&user.role, &model.id));
    for model in &mut models {
        model.log_visual_lines = state.authority.log_visual_lines();
        for action in &mut model.actions {
            let permission = crate::authority::AuthorityConfig::model_action(&model.id, &action.id);
            action.allowed = state.authority.allows(&user.role, &permission);
        }
        if state.authority.allows(&user.role, "config.view") {
            if let Some(root) = model_root(&state, &model.id) {
                model.config_files = config_files_for_user(&user, &model.id, &root);
            }
        }
    }
    if user.role == "管理员" {
        models.push(WorkspaceModel {
            id: "auth".to_owned(),
            name: "授权管理".to_owned(),
            description: "创建并查看注册授权码。".to_owned(),
            actions: Vec::new(),
            config_files: Vec::new(),
            log_visual_lines: state.authority.log_visual_lines(),
        });
    }
    Json(models).into_response()
}

pub async fn start_job(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<ModelRunRequest>,
) -> Response {
    let user = match authenticated(&headers, &state) {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(actions) = model_actions(&request.model) else {
        return message(StatusCode::NOT_FOUND, false, "模型不存在");
    };
    let Some(selected) = actions.iter().find(|item| item.id == request.action) else {
        return message(StatusCode::BAD_REQUEST, false, "动作不存在");
    };
    let permission =
        crate::authority::AuthorityConfig::model_action(&request.model, &request.action);
    if !state.authority.allows(&user.role, &permission) {
        return message(StatusCode::FORBIDDEN, false, "当前角色没有执行此动作的权限");
    }

    let job_id = state.next_id("job");
    let label = format!("{} / {}", model_name(&request.model), selected.name);
    let job = JobInfo {
        id: job_id.clone(),
        model: request.model.clone(),
        owner_id: user.id,
        label: label.clone(),
        status: "queued".to_owned(),
        log: format!("已排队：{label}"),
        exit_code: None,
        generated_files: Vec::new(),
        opened_url: None,
        result: None,
    };
    state.jobs.lock().unwrap().insert(job_id.clone(), job);
    state
        .job_controls
        .lock()
        .unwrap()
        .insert(job_id.clone(), JobControl::default());

    let worker_state = state.clone();
    let worker_job_id = job_id.clone();
    let worker_user = user.clone();
    tokio::spawn(async move {
        let permit = match worker_state.job_semaphore.clone().acquire_owned().await {
            Ok(permit) => permit,
            Err(_) => {
                set_job_status(
                    &worker_state,
                    &worker_job_id,
                    "failed",
                    "任务并发池已关闭",
                    Some(1),
                );
                remove_job_control(&worker_state, &worker_job_id);
                return;
            }
        };
        let job_model = request.model;
        let job_action = request.action;
        let job_input = request.input;
        let _ = tokio::task::spawn_blocking(move || {
            let started_at = SystemTime::now();
            if job_cancel_requested(&worker_state, &worker_job_id) {
                set_job_status(
                    &worker_state,
                    &worker_job_id,
                    "cancelled",
                    JOB_CANCELLED_MESSAGE,
                    Some(130),
                );
                remove_job_control(&worker_state, &worker_job_id);
                drop(permit);
                return;
            }
            set_job_status(&worker_state, &worker_job_id, "running", "开始执行", None);
            let result = execute_action(
                &worker_state,
                &worker_job_id,
                &worker_user,
                &job_model,
                &job_action,
                &job_input,
            );
            let cancelled = job_cancel_requested(&worker_state, &worker_job_id);
            let cancelled_result = matches!(&result, Err(error) if error == JOB_CANCELLED_MESSAGE);
            if !cancelled && !cancelled_result {
                if let Some(root) = model_root(&worker_state, &job_model) {
                    let generated = collect_generated_outputs(
                        &job_model,
                        &job_action,
                        &root,
                        &worker_user,
                        &worker_job_id,
                        started_at,
                    );
                    set_job_generated_files(&worker_state, &worker_job_id, generated);
                }
            }
            match result {
                Ok(code) if !cancelled => set_job_status(
                    &worker_state,
                    &worker_job_id,
                    "completed",
                    "处理完成",
                    Some(code),
                ),
                Ok(_) => set_job_status(
                    &worker_state,
                    &worker_job_id,
                    "cancelled",
                    JOB_CANCELLED_MESSAGE,
                    Some(130),
                ),
                Err(error) if error == JOB_CANCELLED_MESSAGE || cancelled => set_job_status(
                    &worker_state,
                    &worker_job_id,
                    "cancelled",
                    JOB_CANCELLED_MESSAGE,
                    Some(130),
                ),
                Err(error) => {
                    set_job_status(&worker_state, &worker_job_id, "failed", &error, Some(1))
                }
            }
            remove_job_control(&worker_state, &worker_job_id);
            drop(permit);
        })
        .await;
    });

    Json(JobStartResponse { job_id }).into_response()
}

pub async fn job(
    AxumPath(id): AxumPath<String>,
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Response {
    let user = match require_permission(&headers, &state, "workspace.view") {
        Ok(user) => user,
        Err(response) => return response,
    };
    match state.jobs.lock().unwrap().get(&id).cloned() {
        Some(job) if job.owner_id == user.id => Json(job).into_response(),
        Some(_) => message(StatusCode::FORBIDDEN, false, "不能查看其他用户的任务"),
        None => message(StatusCode::NOT_FOUND, false, "任务不存在"),
    }
}

pub async fn running_jobs(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let user = match require_permission(&headers, &state, "workspace.view") {
        Ok(user) => user,
        Err(response) => return response,
    };
    let mut jobs = state
        .jobs
        .lock()
        .unwrap()
        .values()
        .filter(|job| {
            job.owner_id == user.id
                && matches!(job.status.as_str(), "queued" | "running" | "cancelling")
        })
        .cloned()
        .collect::<Vec<_>>();
    jobs.sort_by(|left, right| left.id.cmp(&right.id));
    Json(jobs).into_response()
}

pub async fn cancel_jobs(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<JobCancelRequest>,
) -> Response {
    let user = match require_permission(&headers, &state, "workspace.view") {
        Ok(user) => user,
        Err(response) => return response,
    };

    let targets = {
        let jobs = state.jobs.lock().unwrap();
        if request.all {
            jobs.values()
                .filter(|job| {
                    job.owner_id == user.id
                        && matches!(job.status.as_str(), "queued" | "running" | "cancelling")
                })
                .map(|job| job.id.clone())
                .collect::<Vec<_>>()
        } else {
            match jobs.get(&request.job_id) {
                Some(job)
                    if job.owner_id == user.id
                        && matches!(job.status.as_str(), "queued" | "running" | "cancelling") =>
                {
                    vec![job.id.clone()]
                }
                Some(job) if job.owner_id == user.id => Vec::new(),
                Some(_) => {
                    return message(StatusCode::FORBIDDEN, false, "不能终止其他用户的任务");
                }
                None => {
                    return message(StatusCode::NOT_FOUND, false, "任务不存在");
                }
            }
        }
    };

    for id in &targets {
        if let Some(process_id) = request_job_cancel(&state, id) {
            match terminate_process(process_id) {
                Ok(()) => append_job_log(&state, id, "已发送终止信号"),
                Err(error) => append_job_log(&state, id, &format!("发送终止信号失败：{error}")),
            }
        } else {
            append_job_log(&state, id, "已请求终止任务，等待流程退出");
        }
        set_job_status(&state, id, "cancelling", "正在终止任务", None);
    }

    let count = targets.len();
    Json(JobCancelResponse {
        success: true,
        message: if count == 0 {
            "没有正在运行的任务需要终止".to_owned()
        } else if request.all {
            format!("已请求终止 {count} 个任务")
        } else {
            "已请求终止所选任务".to_owned()
        },
        cancelled: targets,
    })
    .into_response()
}

pub async fn list_files(
    Query(query): Query<UploadQuery>,
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Response {
    let user = match require_permission(&headers, &state, "files.list") {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(root) = model_root(&state, &query.model) else {
        return message(StatusCode::NOT_FOUND, false, "模型不存在");
    };
    Json(visible_generated_files(&state, &user, &query.model, &root)).into_response()
}

pub async fn trueno_catalog(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let user = match require_permission(&headers, &state, "workspace.view") {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(root) = model_root(&state, "trueno") else {
        return message(StatusCode::NOT_FOUND, false, "Trueno 模型目录不存在");
    };
    let python = python_for(&root);
    let mut command = Command::new(&python);
    command
        .current_dir(&root)
        .arg("portal_bridge.py")
        .arg("--catalog")
        .arg("--import-dir")
        .arg(path_env(
            &root.join("sources").join("temp").join(user_key(&user)),
        ))
        .env("PYTHONUNBUFFERED", "1")
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8");
    apply_python_runtime_env(&mut command, &root);
    let output = command.output();
    let output = match output {
        Ok(output) => output,
        Err(error) => {
            return message(
                StatusCode::INTERNAL_SERVER_ERROR,
                false,
                format!("启动 Trueno 目录查询失败：{error}"),
            );
        }
    };
    if !output.status.success() {
        return message(
            StatusCode::INTERNAL_SERVER_ERROR,
            false,
            format!(
                "读取 Trueno 模型目录失败：{}",
                String::from_utf8_lossy(&output.stderr).trim()
            ),
        );
    }
    let response = String::from_utf8_lossy(&output.stdout);
    let json_line = response
        .lines()
        .rev()
        .find(|line| line.trim_start().starts_with('{'));
    match json_line.and_then(|line| serde_json::from_str::<serde_json::Value>(line.trim()).ok()) {
        Some(value) => Json(value).into_response(),
        None => message(
            StatusCode::INTERNAL_SERVER_ERROR,
            false,
            "解析 Trueno 模型目录失败",
        ),
    }
}

#[derive(serde::Serialize)]
struct DependencyFile {
    path: String,
    name: String,
    desc: String,
    category: String,
    notes: String,
    size: u64,
    modified: Option<String>,
}

#[derive(serde::Serialize)]
struct DependencyCatalogResponse {
    files: Vec<DependencyFile>,
    rows_downloaded_file_algorithm: usize,
}

#[derive(Clone, serde::Serialize)]
struct FileDescriptionEntry {
    path: String,
    name: String,
    kind: String,
    desc: String,
    size: u64,
    modified: Option<String>,
    age_days: u64,
    urgency: u8,
    urgency_label: String,
    stale: bool,
}

#[derive(serde::Serialize)]
struct FileDescriptionSummaryEntry {
    path: String,
    name: String,
    size: u64,
}

#[derive(serde::Serialize)]
struct FileDescriptionSummary {
    folders: Vec<FileDescriptionSummaryEntry>,
    files: Vec<FileDescriptionSummaryEntry>,
    folders_total_size: u64,
    files_total_size: u64,
}

#[derive(serde::Serialize)]
struct FileDescriptionCatalogResponse {
    enabled: bool,
    kind: String,
    entries: Vec<FileDescriptionEntry>,
    page_rows: usize,
    stale_days: u64,
    stale_paths: Vec<String>,
    summary: FileDescriptionSummary,
}

#[derive(Clone)]
struct ResolvedFileDescriptionRoot {
    path: PathBuf,
    desc: String,
    recursive: bool,
    include_root: bool,
}

#[derive(serde::Deserialize)]
pub(crate) struct DependencyPathRequest {
    path: String,
}

pub async fn dependency_catalog(State(state): State<AppState>, headers: HeaderMap) -> Response {
    if let Err(response) = require_permission(&headers, &state, "workspace.view") {
        return response;
    }
    let mut files = Vec::new();
    for item in state.authority.document_display() {
        let Some(path) = relative_project_path_buf(&state.project_root, &item.file) else {
            continue;
        };
        let Ok(metadata) = fs::metadata(&path) else {
            continue;
        };
        if !metadata.is_file() {
            continue;
        }
        files.push(DependencyFile {
            path: relative_project_path(&state.project_root, &path),
            name: path
                .file_name()
                .and_then(|value| value.to_str())
                .unwrap_or_default()
                .to_owned(),
            desc: item.desc.clone(),
            category: item.category.clone(),
            notes: item.notes.clone(),
            size: metadata.len(),
            modified: metadata.modified().ok().and_then(format_time),
        });
    }
    Json(DependencyCatalogResponse {
        files,
        rows_downloaded_file_algorithm: state.authority.rows_downloaded_file_algorithm(),
    })
    .into_response()
}

pub async fn download_dependency(
    Query(query): Query<DependencyPathRequest>,
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Response {
    if let Err(response) = require_permission(&headers, &state, "files.download") {
        return response;
    }
    let Some(path) = configured_dependency_path(&state, &query.path) else {
        return message(StatusCode::FORBIDDEN, false, "该文件未在依赖区配置");
    };
    let bytes = match tokio::fs::read(&path).await {
        Ok(bytes) => bytes,
        Err(error) => {
            return message(
                StatusCode::NOT_FOUND,
                false,
                format!("读取依赖文件失败：{error}"),
            );
        }
    };
    let mut response_headers = HeaderMap::new();
    response_headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static(mime_type(&path)),
    );
    response_headers.insert(
        header::CONTENT_DISPOSITION,
        HeaderValue::from_static("attachment"),
    );
    (response_headers, bytes).into_response()
}

pub async fn open_dependency(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<DependencyPathRequest>,
) -> Response {
    if let Err(response) = require_permission(&headers, &state, "files.open") {
        return response;
    }
    let Some(path) = configured_dependency_path(&state, &request.path) else {
        return message(StatusCode::FORBIDDEN, false, "该文件未在依赖区配置");
    };
    match tokio::task::spawn_blocking(move || open_with_default_app(&path)).await {
        Ok(Ok(())) => message(StatusCode::OK, true, "已请求本机默认程序打开文件"),
        Ok(Err(error)) => message(StatusCode::BAD_REQUEST, false, error),
        Err(error) => message(
            StatusCode::INTERNAL_SERVER_ERROR,
            false,
            format!("打开依赖文件任务失败：{error}"),
        ),
    }
}

pub async fn file_description_catalog(
    Query(query): Query<FileDescriptionQuery>,
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Response {
    if let Err(response) = require_permission(&headers, &state, "files.description.view") {
        return response;
    }
    let kind = match query.kind.as_str() {
        "directory" => "directory",
        _ => "file",
    };
    let mut all_entries = Vec::new();
    let mut size_cache = HashMap::new();
    let roots = resolved_file_description_roots(&state);
    for root in &roots {
        collect_file_description_entries(
            &state.project_root,
            root,
            "all",
            &mut all_entries,
            &mut size_cache,
        );
    }
    all_entries.sort_by(|left, right| {
        right
            .urgency
            .cmp(&left.urgency)
            .then_with(|| right.age_days.cmp(&left.age_days))
            .then_with(|| left.modified.cmp(&right.modified))
            .then_with(|| left.path.cmp(&right.path))
    });
    all_entries.dedup_by(|left, right| left.path == right.path);
    let file_description = state.authority.file_description();
    let stale_days = file_description.stale_days();
    for entry in &mut all_entries {
        entry.stale = entry.age_days > stale_days;
    }
    all_entries.retain(|entry| {
        entry.kind == "directory" || file_description.file_extension_allowed(Path::new(&entry.name))
    });
    let entries = all_entries
        .iter()
        .filter(|entry| entry.kind == kind)
        .cloned()
        .collect();
    let stale_paths = all_entries
        .iter()
        .filter(|entry| entry.stale)
        .map(|entry| entry.path.clone())
        .collect();
    let stale_entries = all_entries
        .iter()
        .filter(|entry| entry.stale)
        .cloned()
        .collect::<Vec<_>>();
    let summary = file_description_summary(&stale_entries);
    Json(FileDescriptionCatalogResponse {
        enabled: state.authority.file_description().enabled,
        kind: kind.to_owned(),
        entries,
        page_rows: file_description.page_rows(),
        stale_days: file_description.stale_days(),
        stale_paths,
        summary,
    })
    .into_response()
}

pub async fn delete_file_description(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<FileDescriptionDeleteRequest>,
) -> Response {
    if let Err(response) = require_permission(&headers, &state, "files.description.delete") {
        return response;
    }
    let action = match request.action.trim().to_ascii_lowercase().as_str() {
        "delete" | "recycle" => "recycle",
        "move" => "temp",
        _ => return message(StatusCode::BAD_REQUEST, false, "文件处理动作无效"),
    };
    let mut moved = 0usize;
    let mut failures = Vec::new();
    let mut seen = std::collections::HashSet::new();
    let mut resolved = Vec::new();
    for requested in request.paths {
        if !seen.insert(requested.clone()) {
            continue;
        }
        let Some(path) = configured_file_description_path(&state, &requested) else {
            failures.push(format!("{requested}：不在文件说明区配置范围内"));
            continue;
        };
        resolved.push((requested, path));
    }
    resolved.sort_by_key(|(_, path)| path.components().count());
    let mut filtered = Vec::new();
    for (requested, path) in resolved {
        if filtered
            .iter()
            .any(|(_, parent): &(String, PathBuf)| path == *parent || path.starts_with(parent))
        {
            continue;
        }
        filtered.push((requested, path));
    }
    for (requested, path) in filtered {
        let result = if action == "temp" {
            move_to_sources_temp(&state, &path)
        } else {
            move_to_recycle_bin(&state, &path)
        };
        match result {
            Ok(()) => {
                moved += 1;
            }
            Err(error) => failures.push(format!("{requested}：{error}")),
        }
    }
    let destination = if action == "temp" {
        "sources/temp"
    } else {
        "回收站"
    };
    if failures.is_empty() {
        return message(
            StatusCode::OK,
            true,
            format!("已移动 {moved} 项到 {destination}"),
        );
    }
    message(
        StatusCode::PARTIAL_CONTENT,
        moved > 0,
        format!(
            "已移动 {moved} 项到 {destination}；失败 {} 项：{}",
            failures.len(),
            failures.join("；")
        ),
    )
}

pub async fn local_import(
    State(state): State<AppState>,
    ConnectInfo(peer): ConnectInfo<std::net::SocketAddr>,
    headers: HeaderMap,
    Json(request): Json<LocalImportRequest>,
) -> Response {
    let user = match require_permission(&headers, &state, "files.upload") {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(root) = model_root(&state, &request.model) else {
        return message(StatusCode::NOT_FOUND, false, "模型不存在");
    };
    if request.model == "annotation" || request.model == "auth" {
        return message(StatusCode::BAD_REQUEST, false, "该模块不需要上传文件");
    }
    if !is_local_import_request(peer.ip()) {
        return message(
            StatusCode::FORBIDDEN,
            false,
            "本机复制只允许由本机浏览器请求，请使用普通上传",
        );
    }

    let source = match fs::canonicalize(request.source_path.trim()) {
        Ok(path) if path.is_file() => path,
        _ => return message(StatusCode::BAD_REQUEST, false, "本机源文件不存在"),
    };
    let filename = source
        .file_name()
        .and_then(|value| value.to_str())
        .map(safe_filename)
        .unwrap_or_else(|| "uploaded-file".to_owned());
    if let Err(text) = validate_upload_file(&request.model, &request.slot, &filename) {
        return message(StatusCode::BAD_REQUEST, false, text);
    }
    if request.model == "trueno" {
        cleanup_trueno_inference_artifacts(&root, &user);
    }
    let destination =
        unique_upload_destination(&request.model, &root, &user, &request.slot, &filename);
    if let Some(parent) = destination.parent() {
        if let Err(error) = tokio::fs::create_dir_all(parent).await {
            return message(
                StatusCode::INTERNAL_SERVER_ERROR,
                false,
                format!("创建本机导入目录失败：{error}"),
            );
        }
    }
    if let Err(error) = tokio::fs::copy(&source, &destination).await {
        return message(
            StatusCode::INTERNAL_SERVER_ERROR,
            false,
            format!("本机复制文件失败：{error}"),
        );
    }
    if request.model == "weekly" {
        if let Err(error) = prepare_weekly_uploaded_result(&root, &user, &destination) {
            return message(
                StatusCode::INTERNAL_SERVER_ERROR,
                false,
                format!("准备周报新数据失败：{error}"),
            );
        }
    }
    let relative = relative_path(&root, &destination);
    Json(UploadResponse {
        path: relative,
        name: filename.clone(),
        display_name: filename,
    })
    .into_response()
}

pub async fn upload(
    Query(query): Query<UploadQuery>,
    State(state): State<AppState>,
    headers: HeaderMap,
    mut multipart: Multipart,
) -> Response {
    let user = match require_permission(&headers, &state, "files.upload") {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(root) = model_root(&state, &query.model) else {
        return message(StatusCode::NOT_FOUND, false, "模型不存在");
    };
    if query.model == "annotation" || query.model == "auth" {
        return message(StatusCode::BAD_REQUEST, false, "该模块不需要上传文件");
    }
    if query.model == "trueno" {
        cleanup_trueno_inference_artifacts(&root, &user);
    }

    let mut slot = query.slot.clone();
    let mut uploaded: Option<(PathBuf, String)> = None;
    loop {
        let mut field = match multipart.next_field().await {
            Ok(Some(field)) => field,
            Ok(None) => break,
            Err(error) => {
                return message(
                    StatusCode::BAD_REQUEST,
                    false,
                    format!("读取上传内容失败：{error}"),
                );
            }
        };
        let name = field.name().unwrap_or("").to_owned();
        if name == "slot" {
            match field.text().await {
                Ok(value) => slot = value,
                Err(error) => {
                    return message(
                        StatusCode::BAD_REQUEST,
                        false,
                        format!("读取上传选项失败：{error}"),
                    );
                }
            }
            continue;
        }
        if name != "file" || uploaded.is_some() {
            continue;
        }

        let filename = field.file_name().unwrap_or("uploaded-file").to_owned();
        let safe_name = safe_filename(&filename);
        if let Err(text) = validate_upload_file(&query.model, &slot, &safe_name) {
            return message(StatusCode::BAD_REQUEST, false, text);
        }
        let destination = unique_upload_destination(&query.model, &root, &user, &slot, &safe_name);
        if let Some(parent) = destination.parent() {
            if let Err(error) = tokio::fs::create_dir_all(parent).await {
                return message(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    false,
                    format!("创建上传目录失败：{error}"),
                );
            }
        }
        let mut output = match tokio::fs::File::create(&destination).await {
            Ok(file) => file,
            Err(error) => {
                return message(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    false,
                    format!("创建上传文件失败：{error}"),
                );
            }
        };
        let mut size = 0usize;
        loop {
            let chunk = match field.chunk().await {
                Ok(Some(chunk)) => chunk,
                Ok(None) => break,
                Err(error) => {
                    let _ = tokio::fs::remove_file(&destination).await;
                    return message(
                        StatusCode::BAD_REQUEST,
                        false,
                        format!("读取文件失败：{error}"),
                    );
                }
            };
            size = match size.checked_add(chunk.len()) {
                Some(value) => value,
                None => MAX_UPLOAD_BYTES + 1,
            };
            if size > MAX_UPLOAD_BYTES {
                let _ = tokio::fs::remove_file(&destination).await;
                return message(StatusCode::PAYLOAD_TOO_LARGE, false, "文件不能超过 2 GB");
            }
            if let Err(error) = output.write_all(&chunk).await {
                let _ = tokio::fs::remove_file(&destination).await;
                return message(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    false,
                    format!("保存上传文件失败：{error}"),
                );
            }
        }
        if let Err(error) = output.flush().await {
            let _ = tokio::fs::remove_file(&destination).await;
            return message(
                StatusCode::INTERNAL_SERVER_ERROR,
                false,
                format!("写入上传文件失败：{error}"),
            );
        }
        uploaded = Some((destination, safe_name));
    }

    let Some((destination, safe_name)) = uploaded else {
        return message(StatusCode::BAD_REQUEST, false, "请选择要上传的文件");
    };
    if query.model == "weekly" {
        if let Err(error) = prepare_weekly_uploaded_result(&root, &user, &destination) {
            return message(
                StatusCode::INTERNAL_SERVER_ERROR,
                false,
                format!("准备周报新数据失败：{error}"),
            );
        }
    }
    let relative = relative_path(&root, &destination);
    Json(UploadResponse {
        path: relative,
        name: destination
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or(&safe_name)
            .to_owned(),
        display_name: destination
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or(&safe_name)
            .to_owned(),
    })
    .into_response()
}

pub async fn download(
    Query(query): Query<FileQuery>,
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Response {
    let user = match require_permission(&headers, &state, "files.download") {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(root) = model_root(&state, &query.model) else {
        return message(StatusCode::NOT_FOUND, false, "模型不存在");
    };
    if !user_generated_file_allowed(&state, &user, &query.model, &root, &query.path) {
        return message(
            StatusCode::FORBIDDEN,
            false,
            "该文件不是当前用户本次处理产物",
        );
    }
    let path = match safe_existing_path(&root, &query.path) {
        Ok(path) => path,
        Err(text) => return message(StatusCode::BAD_REQUEST, false, text),
    };
    let bytes = match tokio::fs::read(&path).await {
        Ok(bytes) => bytes,
        Err(error) => {
            return message(
                StatusCode::NOT_FOUND,
                false,
                format!("读取文件失败：{error}"),
            );
        }
    };
    let content_type = mime_type(&path);
    let mut response_headers = HeaderMap::new();
    response_headers.insert(header::CONTENT_TYPE, HeaderValue::from_static(content_type));
    response_headers.insert(
        header::CONTENT_DISPOSITION,
        HeaderValue::from_static("attachment"),
    );
    (response_headers, bytes).into_response()
}

pub async fn content(
    Query(query): Query<FileContentQuery>,
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Response {
    let user = match require_permission(&headers, &state, "files.content") {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(root) = model_root(&state, &query.model) else {
        return message(StatusCode::NOT_FOUND, false, "模型不存在");
    };
    if !user_generated_file_allowed(&state, &user, &query.model, &root, &query.path) {
        return message(
            StatusCode::FORBIDDEN,
            false,
            "该文件不是当前用户本次处理产物",
        );
    }
    let path = match safe_existing_path(&root, &query.path) {
        Ok(path) => path,
        Err(text) => return message(StatusCode::BAD_REQUEST, false, text),
    };
    let metadata = match tokio::fs::metadata(&path).await {
        Ok(metadata) => metadata,
        Err(error) => {
            return message(
                StatusCode::NOT_FOUND,
                false,
                format!("读取文件信息失败：{error}"),
            );
        }
    };
    if metadata.len() > MAX_TEXT_BYTES || !is_text_path(&path) {
        return message(
            StatusCode::BAD_REQUEST,
            false,
            "该文件不是可在线读取的小型文本文件",
        );
    }
    let text = match tokio::fs::read_to_string(&path).await {
        Ok(text) => text,
        Err(error) => {
            return message(
                StatusCode::BAD_REQUEST,
                false,
                format!("读取文本失败：{error}"),
            );
        }
    };
    Json(FileContentResponse {
        path: query.path,
        content: text,
    })
    .into_response()
}

pub async fn open_file(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<FileRequest>,
) -> Response {
    let user = match require_permission(&headers, &state, "files.open") {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(root) = model_root(&state, &request.model) else {
        return message(StatusCode::NOT_FOUND, false, "模型不存在");
    };
    if !user_generated_file_allowed(&state, &user, &request.model, &root, &request.path) {
        return message(
            StatusCode::FORBIDDEN,
            false,
            "该文件不是当前用户本次处理产物",
        );
    }
    let path = match safe_existing_path(&root, &request.path) {
        Ok(path) => path,
        Err(text) => return message(StatusCode::BAD_REQUEST, false, text),
    };
    let result = tokio::task::spawn_blocking(move || open_with_default_app(&path)).await;
    match result {
        Ok(Ok(())) => message(StatusCode::OK, true, "已请求本机默认程序打开文件"),
        Ok(Err(error)) => message(StatusCode::BAD_REQUEST, false, error),
        Err(error) => message(
            StatusCode::INTERNAL_SERVER_ERROR,
            false,
            format!("打开文件任务失败：{error}"),
        ),
    }
}

pub async fn get_config(
    Query(query): Query<FileContentQuery>,
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Response {
    let user = match require_permission(&headers, &state, "config.view") {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(root) = model_root(&state, &query.model) else {
        return message(StatusCode::NOT_FOUND, false, "模型不存在");
    };
    let path = if query.path.trim().is_empty() {
        match first_config_path_for_user(&user, &query.model, &root) {
            Some(path) => path,
            None => {
                return message(
                    StatusCode::NOT_FOUND,
                    false,
                    "该模型没有可在线编辑的 YAML 配置",
                );
            }
        }
    } else {
        match safe_existing_path(&root, &query.path) {
            Ok(path) => path,
            Err(text) => return message(StatusCode::BAD_REQUEST, false, text),
        }
    };
    if !config_path_allowed(&user, &query.model, &root, &path) {
        return message(StatusCode::FORBIDDEN, false, "当前角色不能读取该配置文件");
    }
    if !is_yaml_path(&path) {
        return message(StatusCode::BAD_REQUEST, false, "这里只允许编辑 YAML 配置");
    }
    match tokio::fs::read_to_string(&path).await {
        Ok(content) => Json(FileContentResponse {
            path: relative_path(&root, &path),
            content,
        })
        .into_response(),
        Err(error) => message(
            StatusCode::NOT_FOUND,
            false,
            format!("配置文件不存在或无法读取：{error}"),
        ),
    }
}

pub async fn update_config(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<ConfigUpdateRequest>,
) -> Response {
    let user = match require_permission(&headers, &state, "config.edit") {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(root) = model_root(&state, &request.model) else {
        return message(StatusCode::NOT_FOUND, false, "模型不存在");
    };
    let path = if request.path.trim().is_empty() {
        match first_config_path_for_user(&user, &request.model, &root) {
            Some(path) => path,
            None => {
                return message(
                    StatusCode::NOT_FOUND,
                    false,
                    "该模型没有可在线编辑的 YAML 配置",
                );
            }
        }
    } else {
        match safe_existing_path(&root, &request.path) {
            Ok(path) => path,
            Err(text) => return message(StatusCode::BAD_REQUEST, false, text),
        }
    };
    if !config_path_allowed(&user, &request.model, &root, &path) {
        return message(StatusCode::FORBIDDEN, false, "当前角色不能保存该配置文件");
    }
    if !is_yaml_path(&path) {
        return message(StatusCode::BAD_REQUEST, false, "这里只允许编辑 YAML 配置");
    }
    if let Err(error) = tokio::fs::write(&path, request.content).await {
        return message(
            StatusCode::INTERNAL_SERVER_ERROR,
            false,
            format!("保存配置失败：{error}"),
        );
    }
    message(StatusCode::OK, true, "配置已保存")
}

pub async fn list_codes(State(state): State<AppState>, headers: HeaderMap) -> Response {
    if let Err(response) = require_permission(&headers, &state, "view_verification_codes") {
        return response;
    }
    let result = crate::db::call(state, |connection| {
        prune_authorization_codes(connection)?;
        let mut statement = connection
            .prepare(
                "SELECT code, role, used, created_at
                 FROM authorization_codes ORDER BY created_at DESC, rowid DESC LIMIT 5",
            )
            .map_err(|error| error.to_string())?;
        let rows = statement
            .query_map([], |row| {
                Ok(CodeInfo {
                    code: row.get(0)?,
                    role: row.get(1)?,
                    used: row.get::<_, i64>(2)? != 0,
                    created_at: row.get(3)?,
                })
            })
            .map_err(|error| error.to_string())?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| error.to_string())?;
        Ok::<_, String>(rows)
    })
    .await;
    match result {
        Ok(codes) => Json(codes).into_response(),
        Err(error) => message(
            StatusCode::INTERNAL_SERVER_ERROR,
            false,
            format!("读取授权码失败：{error}"),
        ),
    }
}

pub async fn create_code(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<crate::models::CreateCodeRequest>,
) -> Response {
    if let Err(response) = require_permission(&headers, &state, "get_verification_code") {
        return response;
    }
    if !["管理员", "普通二级", "普通一级"].contains(&request.role.as_str()) {
        return message(
            StatusCode::BAD_REQUEST,
            false,
            "角色必须是管理员、普通二级或普通一级",
        );
    }
    let code = state.next_id("AUTH").to_uppercase();
    let role = request.role;
    let insert_code = code.clone();
    let insert_role = role.clone();
    let result = crate::db::call(state.clone(), move |connection| {
        connection
            .execute(
                "INSERT INTO authorization_codes (code, role, used) VALUES (?1, ?2, 0)",
                rusqlite::params![insert_code, insert_role],
            )
            .map_err(|error| error.to_string())?;
        prune_authorization_codes(connection)
    })
    .await;
    match result {
        Ok(()) => Json(CodeInfo {
            code,
            role,
            used: false,
            created_at: current_timestamp(),
        })
        .into_response(),
        Err(error) => message(
            StatusCode::INTERNAL_SERVER_ERROR,
            false,
            format!("创建授权码失败：{error}"),
        ),
    }
}

fn prune_authorization_codes(connection: &rusqlite::Connection) -> Result<(), String> {
    connection
        .execute(
            "DELETE FROM authorization_codes
             WHERE rowid NOT IN (
                 SELECT rowid FROM authorization_codes
                 ORDER BY created_at DESC, rowid DESC
                 LIMIT 5
             )",
            [],
        )
        .map(|_| ())
        .map_err(|error| error.to_string())
}

fn execute_action(
    state: &AppState,
    job_id: &str,
    user: &SessionUser,
    model: &str,
    action: &str,
    input: &str,
) -> Result<i32, String> {
    match (model, action) {
        ("log", "convert") => execute_log_conversion(state, job_id, user, input),
        ("log", "debug_meter") => execute_meter_debug(state, job_id, user, input),
        ("annotation", "task") => execute_annotation(state, job_id, user, "src/1.task_id.py"),
        ("annotation", "model") => execute_annotation(state, job_id, user, "src/2.model.py"),
        ("annotation", "sum") => execute_annotation(state, job_id, user, "src/3.sum_up.py"),
        ("annotation", "sort") => execute_annotation(state, job_id, user, "src/4.outlook_sort.py"),
        ("annotation", "summary") => execute_annotation_training_summary(state, job_id, user),
        ("offline", "mode1") => execute_offline(state, job_id, user, "m", input),
        ("offline", "mode2") => execute_offline(state, job_id, user, "h", input),
        ("offline", "mode3") => execute_offline(state, job_id, user, "y", input),
        ("offline", "mode4") => execute_offline(state, job_id, user, "n", input),
        ("offline", "mode5") => execute_offline(state, job_id, user, "r", input),
        ("weekly", "refresh") => execute_weekly_refresh(state, job_id, user),
        ("weekly", "generate") => execute_weekly_generate(state, job_id, user, input),
        ("weekly", "todo") => execute_weekly_todo(state, job_id, user),
        ("trueno", "infer") => execute_trueno_infer(state, job_id, user, input),
        _ => Err("未实现的模型动作".to_owned()),
    }
}

fn execute_trueno_infer(
    state: &AppState,
    job_id: &str,
    user: &SessionUser,
    input: &str,
) -> Result<i32, String> {
    let request: TruenoInput =
        serde_json::from_str(input).map_err(|error| format!("推理参数无效：{error}"))?;
    if request.ability.trim().is_empty() {
        return Err("请选择推理能力".to_owned());
    }
    let mode = request.mode.trim().to_ascii_lowercase();
    if !matches!(mode.as_str(), "auto" | "local" | "import") {
        return Err("模型调用模式无效".to_owned());
    }
    let root = model_root(state, "trueno").ok_or("Trueno 模型目录不存在")?;
    let image = safe_existing_path(&root, &request.image)?;
    ensure_in_user_upload_dir(
        &image,
        &root.join("images").join(user_key(user)),
        "只能推理当前用户上传的图片",
    )?;
    if mode == "local" && request.model.trim().is_empty() {
        return Err("本地模式请选择模型文件".to_owned());
    }
    if mode == "import" {
        return Err("导入模式请切换到本地模式选择已上传的模型".to_owned());
    }
    if !request.model.trim().is_empty() {
        let model = resolve_trueno_model_path(&root, user, &request.model)?;
        if !model.is_file() || !has_extension(&model, &["pt", "onnx", "torchscript", "engine"]) {
            return Err("选择的模型文件类型不支持".to_owned());
        }
        let in_native_dir = ensure_in_user_upload_dir(
            &model,
            &root.join("model"),
            "模型路径不在 Trueno model 目录中",
        )
        .is_ok();
        let in_user_import_dir = ensure_in_user_upload_dir(
            &model,
            &root.join("sources").join("temp").join(user_key(user)),
            "模型路径不在当前用户导入目录中",
        )
        .is_ok();
        if (mode == "auto" && !in_native_dir)
            || (mode == "local" && !in_native_dir && !in_user_import_dir)
        {
            return Err(if mode == "auto" {
                "自动模式只能使用 Trueno model 目录中的模型".to_owned()
            } else {
                "本地模式只能选择内置模型或当前用户导入的模型".to_owned()
            });
        }
    }
    let output_dir = job_output_dir(&root, user, job_id, "portal_outputs");
    fs::create_dir_all(&output_dir).map_err(|error| format!("创建推理输出目录失败：{error}"))?;
    let output = output_dir.join("inference.png");
    let result_file = output_dir.join("inference.json");
    let mut args = vec![
        "--image".to_owned(),
        path_env(&image),
        "--ability".to_owned(),
        request.ability.clone(),
        "--mode".to_owned(),
        mode,
        "--import-dir".to_owned(),
        path_env(&root.join("sources").join("temp").join(user_key(user))),
        "--output".to_owned(),
        path_env(&output),
        "--result".to_owned(),
        path_env(&result_file),
    ];
    if !request.model.trim().is_empty() {
        args.extend(["--model".to_owned(), request.model]);
    }
    if !request.roi.is_null() {
        args.extend([
            "--roi".to_owned(),
            serde_json::to_string(&request.roi).map_err(|error| error.to_string())?,
        ]);
    }
    if !request.helpers.is_null() {
        args.extend([
            "--helpers".to_owned(),
            serde_json::to_string(&request.helpers).map_err(|error| error.to_string())?,
        ]);
    }
    let response = run_trueno_bridge(state, job_id, &root, &args)?;
    let mut result: serde_json::Value = serde_json::from_str(response.trim())
        .map_err(|error| format!("解析推理返回值失败：{error}"))?;
    if let Some(logs) = result.get("logs").and_then(|value| value.as_array()) {
        for line in logs.iter().filter_map(|value| value.as_str()) {
            append_job_log(state, job_id, line);
        }
    }
    if let Some(object) = result.as_object_mut() {
        object.insert(
            "output_path".to_owned(),
            serde_json::Value::String(relative_path(&root, &output)),
        );
    }
    set_job_result(state, job_id, result);
    append_job_log(
        state,
        job_id,
        &format!("推理图片已输出：{}", output.display()),
    );
    Ok(0)
}

fn execute_log_conversion(
    state: &AppState,
    job_id: &str,
    user: &SessionUser,
    input: &str,
) -> Result<i32, String> {
    let root = model_root(state, "log").ok_or("日志模型目录不存在")?;
    let input_path = find_input_file(&root, input, &["txt"])?;
    ensure_in_user_upload_dir(
        &input_path,
        &root.join("portal_inputs").join(user_key(user)),
        "只能转换当前用户上传的日志文件",
    )?;
    let output_dir = job_output_dir(&root, user, job_id, "portal_outputs");
    let output = log_converter::output_path_for(&input_path, &output_dir);
    append_job_log(
        state,
        job_id,
        &format!(
            "Rust 原生转换：{} -> {}",
            input_path.display(),
            output.display()
        ),
    );
    let count = log_converter::convert_txt_to_xlsx(&input_path, &output)?;
    append_job_log(state, job_id, &format!("已处理 {count} 行日志"));
    Ok(0)
}

fn execute_meter_debug(
    state: &AppState,
    job_id: &str,
    user: &SessionUser,
    input: &str,
) -> Result<i32, String> {
    let root = model_root(state, "log").ok_or("日志模型目录不存在")?;
    let input_path = find_input_file(&root, input, &["txt"])?;
    ensure_in_user_upload_dir(
        &input_path,
        &root.join("portal_inputs").join(user_key(user)),
        "只能调试当前用户上传的日志文件",
    )?;
    let output_dir = root.join(format!("meter_output_{}", job_key(job_id)));
    fs::create_dir_all(&output_dir).map_err(|error| format!("创建表计输出目录失败：{error}"))?;
    append_job_log(
        state,
        job_id,
        &format!(
            "调试表计：{} -> {}",
            input_path.display(),
            output_dir.display()
        ),
    );
    run_python_with_env(
        state,
        job_id,
        &root,
        "identification/plot_meter_image.py",
        &[path_env(&input_path)],
        &[
            ("MPLBACKEND".to_owned(), "Agg".to_owned()),
            ("RUST_PORTAL_METER_OUTPUT".to_owned(), path_env(&output_dir)),
        ],
    )?;
    set_job_result(
        state,
        job_id,
        serde_json::json!({"output_dir": relative_path(&root, &output_dir)}),
    );
    append_job_log(
        state,
        job_id,
        &format!("表计输出目录：{}", output_dir.display()),
    );
    Ok(0)
}

pub async fn open_directory(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<FileRequest>,
) -> Response {
    let user = match require_permission(&headers, &state, "files.open") {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(root) = model_root(&state, &request.model) else {
        return message(StatusCode::NOT_FOUND, false, "模型不存在");
    };
    let path = match safe_existing_path(&root, &request.path) {
        Ok(path) => path,
        Err(text) => return message(StatusCode::BAD_REQUEST, false, text),
    };
    if request.model != "log"
        || !path.is_dir()
        || !path
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or_default()
            .starts_with("meter_output_")
        || !path.starts_with(root.join("meter_output_"))
        || !state.jobs.lock().unwrap().values().any(|job| {
            job.owner_id == user.id
                && job.model == "log"
                && job
                    .result
                    .as_ref()
                    .and_then(|value| value.get("output_dir"))
                    .and_then(|value| value.as_str())
                    == Some(request.path.as_str())
        })
    {
        return message(
            StatusCode::FORBIDDEN,
            false,
            "该目录不是当前用户的表计调试产物",
        );
    }
    match tokio::task::spawn_blocking(move || open_with_default_app(&path)).await {
        Ok(Ok(())) => message(StatusCode::OK, true, "已请求文件管理器打开输出目录"),
        Ok(Err(error)) => message(StatusCode::BAD_REQUEST, false, error),
        Err(error) => message(StatusCode::INTERNAL_SERVER_ERROR, false, error.to_string()),
    }
}

fn execute_annotation(
    state: &AppState,
    job_id: &str,
    user: &SessionUser,
    script: &str,
) -> Result<i32, String> {
    let root = model_root(state, "annotation").ok_or("标注模型目录不存在")?;
    let work_dir = annotation_work_dir(&root, user);
    seed_annotation_work_dir(&root, &work_dir)?;
    let profile_dir = root.join("chromium_user_data").join(user_key(user));
    run_python_with_env(
        state,
        job_id,
        &root,
        script,
        &[],
        &[
            ("RUST_PORTAL_WORK_DIR".to_owned(), path_env(&work_dir)),
            (
                "RUST_PORTAL_USER_DATA_DIR".to_owned(),
                path_env(&profile_dir),
            ),
            (
                "RUST_PORTAL_DEBUG_PORT".to_owned(),
                debug_port_for_user(user, 9322),
            ),
        ],
    )
}

fn execute_annotation_training_summary(
    state: &AppState,
    job_id: &str,
    user: &SessionUser,
) -> Result<i32, String> {
    for (action, script, label) in [
        ("sum", "src/3.sum_up.py", "训练展望分组"),
        ("sort", "src/4.outlook_sort.py", "训练展望排序"),
        ("summary", "src/5.summary_table.py", "生成训练清单"),
    ] {
        let permission = crate::authority::AuthorityConfig::model_action("annotation", action);
        if !state.authority.allows(&user.role, &permission) {
            return Err(format!("当前角色没有执行依赖动作“{label}”的权限"));
        }
        append_job_log(state, job_id, &format!("开始执行：{label}"));
        let code = execute_annotation(state, job_id, user, script)?;
        if code != 0 {
            return Ok(code);
        }
    }
    Ok(0)
}

fn execute_offline(
    state: &AppState,
    job_id: &str,
    user: &SessionUser,
    mode: &str,
    input: &str,
) -> Result<i32, String> {
    let root = model_root(state, "offline").ok_or("离线包模型目录不存在")?;
    let parsed = parse_action_input(input);
    let selected_config = resolve_offline_config(user, &root, &parsed.config)?;
    let package = latest_or_selected_upload(&root, user, &parsed.file, &["zip"])?;
    append_job_log(
        state,
        job_id,
        &format!("本任务使用离线包：{}", package.display()),
    );
    let job_sources = job_output_dir(&root, user, job_id, ".portal_tmp/sources");
    fs::create_dir_all(&job_sources).map_err(|error| format!("创建任务输入目录失败：{error}"))?;
    let job_package = job_sources.join(
        package
            .file_name()
            .ok_or_else(|| "上传压缩包文件名无效".to_owned())?,
    );
    fs::copy(&package, &job_package).map_err(|error| format!("准备离线包输入失败：{error}"))?;
    append_job_log(
        state,
        job_id,
        &format!("任务隔离副本：{}", job_package.display()),
    );

    let output_dir = job_output_dir(&root, user, job_id, "output");
    let newwrap_dir = job_output_dir(&root, user, job_id, "newwrap");
    let log_path = job_output_dir(&root, user, job_id, ".portal_tmp/logs").join("rebuild.log");
    let mut args = vec![
        mode.to_owned(),
        "--no-browser".to_owned(),
        "--sources".to_owned(),
        path_env(&job_sources),
        "--output".to_owned(),
        path_env(&output_dir),
        "--newwrap".to_owned(),
        path_env(&newwrap_dir),
        "--config".to_owned(),
        selected_config,
        "--log".to_owned(),
        path_env(&log_path),
    ];
    if mode == "m" || mode == "h" {
        args.extend([
            "--package".to_owned(),
            path_env(&job_package),
            "--interaction-mode".to_owned(),
            "local".to_owned(),
            "--host".to_owned(),
            "127.0.0.1".to_owned(),
        ]);
    }
    if mode == "y" {
        args.push("--no-split".to_owned());
    }
    run_python(state, job_id, &root, "rebuild_sources.py", &args)
}

fn execute_weekly_refresh(
    state: &AppState,
    job_id: &str,
    user: &SessionUser,
) -> Result<i32, String> {
    let root = model_root(state, "weekly").ok_or("周报模型目录不存在")?;
    let data_dir = weekly_data_dir(&root, user);
    fs::create_dir_all(&data_dir).map_err(|error| format!("创建周报数据目录失败：{error}"))?;
    seed_weekly_data_dir(&root, &data_dir)?;
    let profile_dir = root.join("date_review/chrome/Data").join(user_key(user));
    let first = run_python_with_env(
        state,
        job_id,
        &root,
        "date_review/src/1.open.py",
        &[],
        &[
            ("RUST_PORTAL_DATA_DIR".to_owned(), path_env(&data_dir)),
            (
                "RUST_PORTAL_USER_DATA_DIR".to_owned(),
                path_env(&profile_dir),
            ),
            (
                "RUST_PORTAL_DEBUG_PORT".to_owned(),
                debug_port_for_user(user, 9822),
            ),
        ],
    )?;
    if first != 0 {
        return Ok(first);
    }
    run_python_with_env(
        state,
        job_id,
        &root,
        "date_review/run_html2xlsx.py",
        &[
            path_env(&data_dir.join("page.html")),
            "n".to_owned(),
            path_env(&data_dir.join("result-2.xlsx")),
            "--no-pause".to_owned(),
        ],
        &[("RUST_PORTAL_DATA_DIR".to_owned(), path_env(&data_dir))],
    )
}

fn execute_weekly_generate(
    state: &AppState,
    job_id: &str,
    user: &SessionUser,
    input: &str,
) -> Result<i32, String> {
    let root = model_root(state, "weekly").ok_or("周报模型目录不存在")?;
    let parsed = parse_action_input(input);
    let (site, clipboard, accuracy) = if parsed.site.is_empty() {
        let mut values = input.split('|').map(str::trim);
        (
            values.next().unwrap_or_default().to_owned(),
            values.next().unwrap_or("text").to_owned(),
            values.next().unwrap_or("audited").to_owned(),
        )
    } else {
        (parsed.site, parsed.clipboard, parsed.accuracy)
    };
    if site.is_empty() {
        return Err("生成周报需要站点关键字".to_owned());
    }
    let clipboard = match clipboard.as_str() {
        "image" => "image",
        _ => "text",
    };
    let accuracy = match accuracy.as_str() {
        "raw" => "raw",
        _ => "audited",
    };
    let data_dir = weekly_data_dir(&root, user);
    fs::create_dir_all(&data_dir).map_err(|error| format!("创建周报数据目录失败：{error}"))?;
    seed_weekly_data_dir(&root, &data_dir)?;
    let output_dir = job_output_dir(&root, user, job_id, "output");
    let lan_session_dir = job_output_dir(&root, user, job_id, ".portal_tmp/weekly_lan");
    fs::create_dir_all(&output_dir).map_err(|error| format!("创建周报输出目录失败：{error}"))?;
    run_python_with_env(
        state,
        job_id,
        &root,
        "weekly_report.py",
        &[
            site.to_owned(),
            clipboard.to_owned(),
            accuracy.to_owned(),
            "--no-clipboard".to_owned(),
            "--lan-share".to_owned(),
            "--lan-session-dir".to_owned(),
            path_env(&lan_session_dir),
        ],
        &[
            ("RUST_PORTAL_DATA_DIR".to_owned(), path_env(&data_dir)),
            (
                "RUST_PORTAL_WEEKLY_OUTPUT_DIR".to_owned(),
                path_env(&output_dir),
            ),
            ("RUST_PORTAL_NO_LAN".to_owned(), "0".to_owned()),
        ],
    )
}

fn execute_weekly_todo(state: &AppState, job_id: &str, user: &SessionUser) -> Result<i32, String> {
    let root = model_root(state, "weekly").ok_or("周报模型目录不存在")?;
    let data_dir = weekly_data_dir(&root, user);
    let page_path = if data_dir.join("page.html").is_file() {
        data_dir.join("page.html")
    } else {
        root.join("date_review/0Work/page.html")
    };
    if !page_path.is_file() {
        return Err("缺少 page.html，请先刷新数据".to_owned());
    }
    let todo_dir = job_output_dir(&root, user, job_id, "date_review/portal_work").join("todo");
    fs::create_dir_all(&todo_dir).map_err(|error| format!("创建待办输出目录失败：{error}"))?;
    let output_base = todo_dir.join("upload.xlsx");
    let code = run_python(
        state,
        job_id,
        &root,
        "date_review/html_to_xlsx-abnm.py",
        &[path_env(&page_path), path_env(&output_base), "b".to_owned()],
    )?;
    let expected = todo_dir.join("upload-2.xlsx");
    if !expected.is_file() && output_base.is_file() {
        fs::copy(&output_base, &expected)
            .map_err(|error| format!("生成 upload-2.xlsx 失败：{error}"))?;
    }
    Ok(code)
}

fn run_python(
    state: &AppState,
    job_id: &str,
    model_root: &Path,
    script: &str,
    args: &[String],
) -> Result<i32, String> {
    run_python_with_env(state, job_id, model_root, script, args, &[])
}

fn run_python_with_env(
    state: &AppState,
    job_id: &str,
    model_root: &Path,
    script: &str,
    args: &[String],
    extra_env: &[(String, String)],
) -> Result<i32, String> {
    if job_cancel_requested(state, job_id) {
        return Err(JOB_CANCELLED_MESSAGE.to_owned());
    }
    let python = python_for(model_root);
    let temp_dir = model_root.join(".portal_tmp");
    fs::create_dir_all(&temp_dir).map_err(|error| format!("创建临时目录失败：{error}"))?;
    let headless = portal_headless();
    let mut command = Command::new(&python);
    command
        .current_dir(model_root)
        .arg(script)
        .args(args)
        .env("PYTHONUNBUFFERED", "1")
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .env("RUST_PORTAL_HEADLESS", if headless { "1" } else { "0" })
        .env("RUST_PORTAL_AUTOMATED", "1")
        .env("RUST_PORTAL_DEBUG_PORT", debug_port_for(job_id))
        .env("RUST_PORTAL_DATA_DIR", model_root.join("date_review/0Work"))
        .env("RUST_PORTAL_NO_LAN", "1")
        .env("TMPDIR", &temp_dir)
        .env("TMP", &temp_dir)
        .env("TEMP", &temp_dir)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    apply_python_runtime_env(&mut command, model_root);
    if let Some(chrome_path) = chrome_for(state, model_root)? {
        command.env("RUST_PORTAL_CHROME_PATH", chrome_path);
    }
    for (name, value) in extra_env {
        command.env(name, value);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    append_job_log(
        state,
        job_id,
        &format!("调用 Python：{} {}", python.display(), script),
    );
    let mut child = command
        .spawn()
        .map_err(|error| format!("启动 Python 失败：{error}"))?;
    let child_id = child.id();
    set_job_process_id(state, job_id, Some(child_id));
    let stdout = child.stdout.take().ok_or("无法读取 Python 标准输出")?;
    let stderr = child.stderr.take().ok_or("无法读取 Python 错误输出")?;
    let (sender, receiver) = mpsc::channel::<String>();
    spawn_output_reader(stdout, sender.clone());
    spawn_output_reader(stderr, sender);

    let exit_code = loop {
        while let Ok(line) = receiver.try_recv() {
            append_job_log(state, job_id, &line);
        }
        if job_cancel_requested(state, job_id) {
            append_job_log(state, job_id, "检测到终止请求，正在关闭子流程...");
            let _ = terminate_process(child_id);
            let _ = child.kill();
            let _ = child.wait();
            set_job_process_id(state, job_id, None);
            return Err(JOB_CANCELLED_MESSAGE.to_owned());
        }
        match child.try_wait() {
            Ok(Some(status)) => {
                while let Ok(line) = receiver.try_recv() {
                    append_job_log(state, job_id, &line);
                }
                break status.code().unwrap_or(1);
            }
            Ok(None) => {
                thread::sleep(Duration::from_millis(100));
            }
            Err(error) => return Err(format!("等待 Python 结束失败：{error}")),
        }
    };
    set_job_process_id(state, job_id, None);
    while let Ok(line) = receiver.recv_timeout(Duration::from_millis(50)) {
        append_job_log(state, job_id, &line);
    }
    if exit_code == 0 {
        Ok(0)
    } else {
        Err(format!("Python 子流程结束，退出码 {exit_code}"))
    }
}

fn run_trueno_bridge(
    state: &AppState,
    job_id: &str,
    model_root: &Path,
    args: &[String],
) -> Result<String, String> {
    if job_cancel_requested(state, job_id) {
        return Err(JOB_CANCELLED_MESSAGE.to_owned());
    }
    let python = python_for(model_root);
    let mut command = Command::new(&python);
    command
        .current_dir(model_root)
        .arg("portal_bridge.py")
        .args(args)
        .env("PYTHONUNBUFFERED", "1")
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    append_job_log(state, job_id, "调用 Trueno LocalInferenceEngine");
    let mut child = command
        .spawn()
        .map_err(|error| format!("启动 Trueno 推理失败：{error}"))?;
    let child_id = child.id();
    set_job_process_id(state, job_id, Some(child_id));
    let stdout = child.stdout.take().ok_or("无法读取 Trueno 标准输出")?;
    let stderr = child.stderr.take().ok_or("无法读取 Trueno 错误输出")?;
    let (sender, receiver) = mpsc::channel::<(bool, String)>();
    spawn_trueno_reader(stdout, sender.clone(), true);
    spawn_trueno_reader(stderr, sender, false);
    let mut stdout_lines = Vec::new();
    let exit_code = loop {
        while let Ok((is_stdout, line)) = receiver.try_recv() {
            if is_stdout {
                stdout_lines.push(line);
            } else {
                append_job_log(state, job_id, &line);
            }
        }
        if job_cancel_requested(state, job_id) {
            append_job_log(state, job_id, "检测到终止请求，正在关闭 Trueno 推理...");
            let _ = terminate_process(child_id);
            let _ = child.kill();
            let _ = child.wait();
            set_job_process_id(state, job_id, None);
            return Err(JOB_CANCELLED_MESSAGE.to_owned());
        }
        match child.try_wait() {
            Ok(Some(status)) => {
                while let Ok((is_stdout, line)) = receiver.try_recv() {
                    if is_stdout {
                        stdout_lines.push(line);
                    } else {
                        append_job_log(state, job_id, &line);
                    }
                }
                break status.code().unwrap_or(1);
            }
            Ok(None) => thread::sleep(Duration::from_millis(100)),
            Err(error) => return Err(format!("等待 Trueno 推理结束失败：{error}")),
        }
    };
    set_job_process_id(state, job_id, None);
    if exit_code != 0 {
        return Err(format!("Trueno 推理结束，退出码 {exit_code}"));
    }
    stdout_lines
        .into_iter()
        .rev()
        .find(|line| line.trim_start().starts_with('{'))
        .ok_or_else(|| "Trueno 未返回推理结果".to_owned())
}

fn spawn_trueno_reader<R>(reader: R, sender: mpsc::Sender<(bool, String)>, is_stdout: bool)
where
    R: std::io::Read + Send + 'static,
{
    thread::spawn(move || {
        let mut reader = BufReader::new(reader);
        let mut bytes = Vec::new();
        loop {
            bytes.clear();
            match reader.read_until(b'\n', &mut bytes) {
                Ok(0) => break,
                Ok(_) => {
                    let line = String::from_utf8_lossy(&bytes)
                        .trim_end_matches(['\r', '\n'])
                        .to_owned();
                    if !line.trim().is_empty() {
                        let _ = sender.send((is_stdout, line));
                    }
                }
                Err(error) => {
                    let _ = sender.send((false, format!("读取 Trueno 输出失败：{error}")));
                    break;
                }
            }
        }
    });
}

fn spawn_output_reader<R>(reader: R, sender: mpsc::Sender<String>)
where
    R: std::io::Read + Send + 'static,
{
    thread::spawn(move || {
        for line in BufReader::new(reader).lines() {
            match line {
                Ok(line) if !line.trim().is_empty() => {
                    let _ = sender.send(line);
                }
                Ok(_) => {}
                Err(error) => {
                    let _ = sender.send(format!("读取子流程输出失败：{error}"));
                    break;
                }
            }
        }
    });
}

fn set_job_status(state: &AppState, id: &str, status: &str, text: &str, exit_code: Option<i32>) {
    let max_visual_lines = state.authority.log_visual_lines();
    let mut jobs = state.jobs.lock().unwrap();
    if let Some(job) = jobs.get_mut(id) {
        job.status = status.to_owned();
        job.exit_code = exit_code;
        if !text.is_empty() {
            append_log_text(
                &mut job.log,
                text,
                max_visual_lines.saturating_mul(JOB_LOG_MAX_PAGES),
            );
        }
    }
}

fn set_job_generated_files(state: &AppState, id: &str, generated_files: Vec<FileInfo>) {
    let mut jobs = state.jobs.lock().unwrap();
    if let Some(job) = jobs.get_mut(id) {
        job.generated_files = generated_files;
    }
}

fn set_job_result(state: &AppState, id: &str, result: serde_json::Value) {
    let mut jobs = state.jobs.lock().unwrap();
    if let Some(job) = jobs.get_mut(id) {
        job.result = Some(result);
    }
}

fn job_control(state: &AppState, id: &str) -> Option<JobControl> {
    state.job_controls.lock().unwrap().get(id).cloned()
}

fn job_cancel_requested(state: &AppState, id: &str) -> bool {
    job_control(state, id)
        .map(|control| control.is_cancelled())
        .unwrap_or(false)
}

fn request_job_cancel(state: &AppState, id: &str) -> Option<u32> {
    let control = job_control(state, id)?;
    control.cancel();
    control.process_id()
}

fn set_job_process_id(state: &AppState, id: &str, process_id: Option<u32>) {
    if let Some(control) = job_control(state, id) {
        control.set_process_id(process_id);
    }
}

fn remove_job_control(state: &AppState, id: &str) {
    state.job_controls.lock().unwrap().remove(id);
}

#[cfg(windows)]
fn terminate_process(process_id: u32) -> Result<(), String> {
    let process_id = process_id.to_string();
    let status = Command::new("taskkill")
        .args(["/PID", process_id.as_str(), "/F"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map_err(|error| format!("调用 taskkill 失败：{error}"))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("taskkill 返回状态 {status}"))
    }
}

#[cfg(not(windows))]
fn terminate_process(process_id: u32) -> Result<(), String> {
    let process_id = process_id.to_string();
    let status = Command::new("kill")
        .args(["-TERM", process_id.as_str()])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map_err(|error| format!("调用 kill 失败：{error}"))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("kill 返回状态 {status}"))
    }
}

fn append_job_log(state: &AppState, id: &str, text: &str) {
    let text = text.trim();
    if text.is_empty() {
        return;
    }
    let max_visual_lines = state.authority.log_visual_lines();
    let mut jobs = state.jobs.lock().unwrap();
    if let Some(job) = jobs.get_mut(id) {
        if let Some(url) = extract_local_url(text) {
            if job.opened_url.is_none() {
                job.opened_url = Some(url.clone());
                thread::spawn(move || {
                    let _ = open_with_default_app(Path::new(&url));
                });
            }
        }
        append_log_text(
            &mut job.log,
            text,
            max_visual_lines.saturating_mul(JOB_LOG_MAX_PAGES),
        );
    }
}

fn append_log_text(log: &mut String, text: &str, max_lines: usize) {
    if !log.is_empty() {
        log.push('\n');
    }
    log.push_str(text);
    retain_latest_log_lines(log, max_lines);
}

fn retain_latest_log_lines(log: &mut String, max_lines: usize) {
    if max_lines == 0 {
        log.clear();
        return;
    }
    let mut lines = log.split('\n').collect::<Vec<_>>();
    if lines.len() <= max_lines {
        return;
    }
    let first_kept = lines.len() - max_lines;
    *log = lines.drain(first_kept..).collect::<Vec<_>>().join("\n");
}

fn extract_local_url(text: &str) -> Option<String> {
    let start = text.find("http://127.0.0.1:")?;
    let tail = &text[start..];
    let end = tail
        .find(|character: char| character.is_whitespace())
        .unwrap_or(tail.len());
    Some(tail[..end].trim_end_matches(['.', ',', ')']).to_owned())
}

fn model_root(state: &AppState, model: &str) -> Option<PathBuf> {
    let folder = match model {
        "log" => "Log_Review-tag-0.0.1+python",
        "annotation" => "Model-Annotation-tag-0.1.1+python",
        "offline" => "Offline_Package_Organization-tag-0.0.12+python",
        "weekly" => "Weekly-Report-Print-tag-0.0.8+python",
        "trueno" => "trueno3_src-master-0.0.10",
        _ => return None,
    };
    Some(state.project_root.join("model").join(folder))
}

fn model_id_for_root(root: &Path) -> Option<&'static str> {
    match root.file_name()?.to_str()? {
        "Model-Annotation-tag-0.1.1+python" => Some("annotation"),
        "Weekly-Report-Print-tag-0.0.8+python" => Some("weekly"),
        "Log_Review-tag-0.0.1+python" => Some("log"),
        "Offline_Package_Organization-tag-0.0.12+python" => Some("offline"),
        "trueno3_src-master-0.0.10" => Some("trueno"),
        _ => None,
    }
}

fn python_for(root: &Path) -> PathBuf {
    if let Ok(value) = std::env::var("PYTHON") {
        if !value.trim().is_empty() {
            return PathBuf::from(value);
        }
    }
    let candidates = if cfg!(windows) {
        vec![
            root.join("venv/Scripts/python.exe"),
            root.join("date_review/venv/Scripts/python.exe"),
            root.join("date_review/python3.14_install/python.exe"),
            root.join("date_review/python3.14_install/Scripts/python.exe"),
        ]
    } else {
        vec![
            root.join("venv/bin/python"),
            root.join("venv/bin/python3"),
            root.join("venv_linux/bin/python"),
            root.join("venv_linux/bin/python3"),
            root.join("date_review/venv/bin/python"),
            root.join("date_review/venv/bin/python3"),
            root.join("date_review/venv_linux/bin/python"),
            root.join("date_review/venv_linux/bin/python3"),
        ]
    };
    for candidate in candidates {
        if candidate.is_file() {
            return candidate;
        }
    }
    if cfg!(windows) {
        PathBuf::from("python")
    } else {
        PathBuf::from("python3")
    }
}

fn apply_python_runtime_env(command: &mut Command, root: &Path) {
    let cache_root = root.join(".runtime-cache");
    let _ = fs::create_dir_all(&cache_root);
    let cache = cache_root.to_string_lossy().into_owned();
    command
        .env("HOME", &cache)
        .env("PADDLE_HOME", &cache)
        .env("PADDLE_CACHE_HOME", &cache)
        .env("XDG_CACHE_HOME", &cache);
    #[cfg(not(windows))]
    command.env("USERPROFILE", &cache);
}

fn portal_headless() -> bool {
    match std::env::var("RUST_PORTAL_HEADLESS")
        .ok()
        .map(|value| value.trim().to_ascii_lowercase())
        .as_deref()
    {
        Some("1" | "true" | "yes" | "on") => true,
        Some("0" | "false" | "no" | "off") => false,
        _ => !cfg!(target_os = "windows"),
    }
}

fn chrome_for(state: &AppState, root: &Path) -> Result<Option<PathBuf>, String> {
    if let Some(model) = model_id_for_root(root) {
        if let Some(path) = state.authority.browser_path(&state.project_root, model) {
            if path.is_file() {
                return Ok(Some(path));
            }
            return Err(format!(
                "authority.yaml 的 Browser_Path.{model} 不存在或不是文件：{}",
                path.display()
            ));
        }
    }
    Ok([
        root.join("chrome/chrome.exe"),
        root.join("date_review/chrome/chrome.exe"),
        root.join("chrome/chrome"),
        root.join("date_review/chrome/chrome"),
    ]
    .into_iter()
    .find(|path| path.is_file()))
}

fn parse_action_input(input: &str) -> ActionInput {
    let trimmed = input.trim();
    if trimmed.starts_with('{') {
        serde_json::from_str(trimmed).unwrap_or_default()
    } else {
        ActionInput {
            config: trimmed.to_owned(),
            ..ActionInput::default()
        }
    }
}

fn path_env(path: &Path) -> String {
    path.to_string_lossy().to_string()
}

fn debug_port_for(job_id: &str) -> String {
    let hash = job_id.bytes().fold(0u16, |acc, value| {
        acc.wrapping_mul(31).wrapping_add(value as u16)
    });
    (9322u16 + (hash % 500)).to_string()
}

fn debug_port_for_user(user: &SessionUser, base: u16) -> String {
    let offset = (user.id.rem_euclid(300)) as u16;
    base.saturating_add(offset).to_string()
}

fn safe_segment(value: &str) -> String {
    let cleaned: String = value
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() || matches!(character, '-' | '_' | '.') {
                character
            } else {
                '_'
            }
        })
        .collect();
    let cleaned = cleaned.trim_matches(['.', '_']).to_owned();
    if cleaned.is_empty() {
        "user".to_owned()
    } else {
        cleaned
    }
}

fn user_key(user: &SessionUser) -> String {
    format!("u{}_{}", user.id, safe_segment(&user.username))
}

fn job_key(job_id: &str) -> String {
    safe_segment(job_id)
}

fn job_output_dir(root: &Path, user: &SessionUser, job_id: &str, folder: &str) -> PathBuf {
    root.join(Path::new(folder))
        .join(user_key(user))
        .join(job_key(job_id))
}

fn annotation_work_dir(root: &Path, user: &SessionUser) -> PathBuf {
    root.join("Work-txt").join(user_key(user))
}

fn weekly_data_dir(root: &Path, user: &SessionUser) -> PathBuf {
    root.join("date_review/portal_data").join(user_key(user))
}

fn seed_annotation_work_dir(root: &Path, work_dir: &Path) -> Result<(), String> {
    fs::create_dir_all(work_dir).map_err(|error| format!("创建标注工作目录失败：{error}"))?;
    for name in [
        "数字-7-类别标准化映射规则.yaml",
        "数字-7-类别标准化映射规则.txt",
        "数字-8-原始标注类别清单.txt",
        "数字-0-h模式历史学习.json",
    ] {
        let source = root.join("Work-txt").join(name);
        let target = work_dir.join(name);
        if source.is_file() && !target.exists() {
            fs::copy(&source, &target).map_err(|error| format!("准备标注基础文件失败：{error}"))?;
        }
    }
    Ok(())
}

fn seed_weekly_data_dir(root: &Path, data_dir: &Path) -> Result<(), String> {
    fs::create_dir_all(data_dir).map_err(|error| format!("创建周报数据目录失败：{error}"))?;
    for name in ["old.xlsx", "result-2.xlsx", "page.html"] {
        let source = root.join("date_review/0Work").join(name);
        let target = data_dir.join(name);
        if source.is_file() && !target.exists() {
            fs::copy(&source, &target).map_err(|error| format!("准备周报基础数据失败：{error}"))?;
        }
    }
    Ok(())
}

fn prepare_weekly_uploaded_result(
    root: &Path,
    user: &SessionUser,
    uploaded: &Path,
) -> Result<(), String> {
    let data_dir = weekly_data_dir(root, user);
    seed_weekly_data_dir(root, &data_dir)?;
    let current_result = data_dir.join("result-2.xlsx");
    let current_old = data_dir.join("old.xlsx");
    if current_result.is_file() {
        fs::copy(&current_result, &current_old)
            .map_err(|error| format!("备份周报旧数据失败：{error}"))?;
    }
    fs::copy(uploaded, current_result).map_err(|error| format!("保存周报新数据失败：{error}"))?;
    Ok(())
}

fn validate_upload_file(model: &str, slot: &str, filename: &str) -> Result<(), String> {
    let extension = Path::new(filename)
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    match model {
        "log" if extension == "txt" => Ok(()),
        "log" => Err("日志转换只能上传 txt 文件".to_owned()),
        "offline" if extension == "zip" => Ok(()),
        "offline" => Err("离线包工具只能上传 zip 压缩包".to_owned()),
        "weekly" if extension == "xlsx" => Ok(()),
        "weekly" => Err("周报只能上传 result-2.xlsx 格式的 xlsx 文件".to_owned()),
        "trueno"
            if slot.eq_ignore_ascii_case("model")
                && matches!(extension.as_str(), "pt" | "onnx" | "torchscript" | "engine") =>
        {
            Ok(())
        }
        "trueno" if slot.eq_ignore_ascii_case("model") => {
            Err("Trueno 模型只能上传 pt、onnx、torchscript 或 engine 文件".to_owned())
        }
        "trueno" if matches!(extension.as_str(), "jpg" | "jpeg" | "png" | "webp" | "bmp") => Ok(()),
        "trueno" => Err("模型推理只能上传图片文件".to_owned()),
        _ => Err("该模块不支持上传文件".to_owned()),
    }
}

fn unique_upload_destination(
    model: &str,
    root: &Path,
    user: &SessionUser,
    slot: &str,
    filename: &str,
) -> PathBuf {
    let user = user_key(user);
    let base = match model {
        "log" => root.join("portal_inputs").join(user).join(filename),
        "offline" => return unique_numbered_path(root.join("sources").join(filename), 2),
        "weekly" => root
            .join("date_review/portal_uploads")
            .join(user)
            .join(filename),
        "trueno" if slot.eq_ignore_ascii_case("model") => {
            root.join("sources").join("temp").join(user).join(filename)
        }
        "trueno" => root.join("images").join(user).join(filename),
        _ => root.join(filename),
    };
    unique_path(base)
}

fn unique_path(path: PathBuf) -> PathBuf {
    unique_numbered_path(path, 1)
}

fn unique_numbered_path(path: PathBuf, width: usize) -> PathBuf {
    if !path.exists() {
        return path;
    }
    let parent = path.parent().map(Path::to_path_buf).unwrap_or_default();
    let stem = path
        .file_stem()
        .and_then(|value| value.to_str())
        .unwrap_or("uploaded-file");
    let extension = path
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or("");
    for index in 1..1000 {
        let suffix = if width > 1 {
            format!("{index:0width$}")
        } else {
            index.to_string()
        };
        let candidate_name = if extension.is_empty() {
            format!("{stem}-{suffix}")
        } else {
            format!("{stem}-{suffix}.{extension}")
        };
        let candidate = parent.join(candidate_name);
        if !candidate.exists() {
            return candidate;
        }
    }
    path
}

fn latest_or_selected_upload(
    root: &Path,
    _user: &SessionUser,
    selected: &str,
    extensions: &[&str],
) -> Result<PathBuf, String> {
    if !selected.trim().is_empty() {
        let path = safe_existing_path(root, selected)?;
        if has_extension(&path, extensions) {
            ensure_in_user_upload_dir(
                &path,
                &root.join("sources"),
                "只能处理 sources 目录中的离线包",
            )?;
            return Ok(path);
        }
        return Err("选择的处理文件类型不支持".to_owned());
    }
    let input_dir = root.join("sources");
    let mut candidates = fs::read_dir(&input_dir)
        .map_err(|_| "请先上传离线包 zip 文件".to_owned())?
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| path.is_file() && has_extension(path, extensions))
        .collect::<Vec<_>>();
    candidates.sort_by_key(|path| {
        fs::metadata(path)
            .and_then(|metadata| metadata.modified())
            .unwrap_or(UNIX_EPOCH)
    });
    candidates
        .pop()
        .ok_or_else(|| "请先上传离线包 zip 文件".to_owned())
}

fn ensure_in_user_upload_dir(path: &Path, dir: &Path, message: &str) -> Result<(), String> {
    let allowed = dir.canonicalize().map_err(|_| message.to_owned())?;
    if path.starts_with(&allowed) {
        Ok(())
    } else {
        Err(message.to_owned())
    }
}

fn resolve_trueno_model_path(
    root: &Path,
    user: &SessionUser,
    requested: &str,
) -> Result<PathBuf, String> {
    let normalized = requested.trim().replace('\\', "/");
    if normalized.starts_with("sources/temp/") {
        let path = safe_existing_path(root, &normalized)?;
        ensure_in_user_upload_dir(
            &path,
            &root.join("sources").join("temp").join(user_key(user)),
            "只能使用当前用户导入的 Trueno 模型",
        )?;
        return Ok(path);
    }
    safe_existing_path(&root.join("model"), &normalized)
}

fn has_extension(path: &Path, extensions: &[&str]) -> bool {
    path.extension()
        .and_then(|value| value.to_str())
        .map(|value| {
            extensions
                .iter()
                .any(|item| value.eq_ignore_ascii_case(item))
        })
        .unwrap_or(false)
}

fn resolve_offline_config(
    user: &SessionUser,
    root: &Path,
    requested: &str,
) -> Result<String, String> {
    let default_config = if user.role == "管理员" {
        "config-1.yaml"
    } else {
        "config-3.yaml"
    };
    let selected = if requested.trim().is_empty() {
        default_config
    } else {
        requested.trim()
    };
    let allowed = if user.role == "管理员" {
        vec!["config-1.yaml", "config-2.yaml", "config-3.yaml"]
    } else {
        vec!["config-3.yaml"]
    };
    if !allowed.contains(&selected) {
        return Err("离线工具配置文件选择无效".to_owned());
    }
    if !root.join(selected).is_file() {
        return Err(format!("配置文件不存在：{selected}"));
    }
    Ok(selected.to_owned())
}

fn config_path_allowed(user: &SessionUser, model: &str, root: &Path, path: &Path) -> bool {
    let allowed = config_candidate_paths_for_user(user, model, root);
    let Ok(candidate) = path.canonicalize() else {
        return false;
    };
    allowed
        .into_iter()
        .filter_map(|path| path.canonicalize().ok())
        .any(|path| path == candidate)
}

fn first_config_path_for_user(user: &SessionUser, model: &str, root: &Path) -> Option<PathBuf> {
    config_candidate_paths_for_user(user, model, root)
        .into_iter()
        .find(|path| path.is_file() && is_yaml_path(path) && !is_ignored_path(path))
}

fn config_files_for_user(user: &SessionUser, model: &str, root: &Path) -> Vec<String> {
    config_candidate_paths_for_user(user, model, root)
        .into_iter()
        .filter(|path| path.is_file() && is_yaml_path(path) && !is_ignored_path(path))
        .map(|path| relative_path(root, &path))
        .collect()
}

fn config_candidate_paths_for_user(user: &SessionUser, model: &str, root: &Path) -> Vec<PathBuf> {
    if user.role != "管理员" {
        return Vec::new();
    }
    match model {
        "offline" => vec![
            root.join("config-1.yaml"),
            root.join("config-2.yaml"),
            root.join("config-3.yaml"),
        ],
        "annotation" => vec![root.join("config.yaml")],
        "weekly" => vec![
            root.join("config.yaml"),
            root.join("date_review/config.yaml"),
        ],
        _ => vec![root.join("config.yaml")],
    }
}

fn collect_generated_outputs(
    model: &str,
    action: &str,
    root: &Path,
    user: &SessionUser,
    job_id: &str,
    started_at: SystemTime,
) -> Vec<FileInfo> {
    let roots = match (model, action) {
        ("log", "convert") => vec![job_output_dir(root, user, job_id, "portal_outputs")],
        ("annotation", _) => vec![annotation_work_dir(root, user)],
        ("offline", _) => vec![
            job_output_dir(root, user, job_id, "output"),
            job_output_dir(root, user, job_id, "newwrap"),
        ],
        ("weekly", "generate") => vec![job_output_dir(root, user, job_id, "output")],
        ("weekly", "todo") => {
            vec![job_output_dir(root, user, job_id, "date_review/portal_work").join("todo")]
        }
        ("trueno", "infer") => vec![job_output_dir(root, user, job_id, "portal_outputs")],
        _ => Vec::new(),
    };
    let cutoff = started_at
        .checked_sub(Duration::from_secs(2))
        .unwrap_or(started_at);
    let mut files = Vec::new();
    for candidate in roots {
        collect_recent_files_recursive(root, &candidate, 8, cutoff, &mut files);
    }
    files.retain(|file| !(model == "annotation" && is_annotation_seed_file(&file.name)));
    files.sort_by(|left, right| {
        modified_sort_key(right)
            .cmp(&modified_sort_key(left))
            .then_with(|| left.path.cmp(&right.path))
    });
    files.dedup_by(|left, right| left.path == right.path);
    files
}

fn visible_generated_files(
    state: &AppState,
    user: &SessionUser,
    model: &str,
    root: &Path,
) -> Vec<FileInfo> {
    let mut files = state
        .jobs
        .lock()
        .unwrap()
        .values()
        .filter(|job| job.owner_id == user.id && job.model == model)
        .flat_map(|job| job.generated_files.iter().cloned())
        .filter(|file| visible_in_file_list(state.authority.as_ref(), model, &file.path))
        .filter(|file| root.join(Path::new(&file.path)).is_file())
        .collect::<Vec<_>>();
    if model == "weekly" {
        let data_dir = weekly_data_dir(root, user);
        if let Some(names) = state.authority.visible_file_names(model) {
            for name in names {
                let path = data_dir.join(name);
                if let Ok(metadata) = fs::metadata(&path) {
                    if metadata.is_file() {
                        if let Some(info) = file_info(root, &path, &metadata) {
                            files.push(info);
                        }
                    }
                }
            }
        }
    }
    files.sort_by(|left, right| {
        modified_sort_key(right)
            .cmp(&modified_sort_key(left))
            .then_with(|| left.path.cmp(&right.path))
    });
    files.dedup_by(|left, right| left.path == right.path);
    files.truncate(5);
    files
}

fn resolved_file_description_roots(state: &AppState) -> Vec<ResolvedFileDescriptionRoot> {
    if !state.authority.file_description().enabled {
        return Vec::new();
    }
    let mut roots = Vec::new();
    for configured in &state.authority.file_description().roots {
        let Some(relative) = relative_project_path_buf(&state.project_root, &configured.path)
        else {
            continue;
        };
        let parts = relative
            .strip_prefix(&state.project_root)
            .unwrap_or(&relative)
            .components()
            .collect::<Vec<_>>();
        let mut matches = Vec::new();
        expand_file_description_pattern(&state.project_root, &parts, 0, &mut matches);
        for path in matches {
            roots.push(ResolvedFileDescriptionRoot {
                include_root: true,
                path,
                desc: configured.desc.clone(),
                recursive: configured.recursive,
            });
        }
    }
    roots
}

fn expand_file_description_pattern(
    base: &Path,
    parts: &[Component<'_>],
    index: usize,
    output: &mut Vec<PathBuf>,
) {
    if index == parts.len() {
        if base.exists() {
            output.push(base.to_path_buf());
        }
        return;
    }
    let Component::Normal(component) = parts[index] else {
        return;
    };
    let text = component.to_string_lossy();
    if !text.contains('*') {
        expand_file_description_pattern(&base.join(component), parts, index + 1, output);
        return;
    }
    let Ok(entries) = fs::read_dir(base) else {
        return;
    };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        if wildcard_match(&text, &name) {
            expand_file_description_pattern(&entry.path(), parts, index + 1, output);
        }
    }
}

fn wildcard_match(pattern: &str, value: &str) -> bool {
    let mut parts = pattern.split('*');
    let prefix = parts.next().unwrap_or_default();
    let suffix = parts.next().unwrap_or_default();
    parts.next().is_none()
        && value.starts_with(prefix)
        && value.ends_with(suffix)
        && value.len() >= prefix.len() + suffix.len()
}

fn collect_file_description_entries(
    project_root: &Path,
    root: &ResolvedFileDescriptionRoot,
    kind: &str,
    output: &mut Vec<FileDescriptionEntry>,
    size_cache: &mut HashMap<PathBuf, u64>,
) {
    if !root.path.exists() || is_ignored_path(&root.path) {
        return;
    }
    if root.include_root {
        collect_file_description_entry(
            project_root,
            &root.path,
            &root.desc,
            kind,
            output,
            size_cache,
        );
    }
    if !root.path.is_dir() || !root.recursive {
        return;
    }
    let Ok(entries) = fs::read_dir(&root.path) else {
        return;
    };
    for entry in entries.flatten() {
        collect_file_description_tree(
            project_root,
            &entry.path(),
            &root.desc,
            kind,
            output,
            size_cache,
        );
    }
}

fn collect_file_description_tree(
    project_root: &Path,
    path: &Path,
    desc: &str,
    kind: &str,
    output: &mut Vec<FileDescriptionEntry>,
    size_cache: &mut HashMap<PathBuf, u64>,
) {
    if is_ignored_path(path) {
        return;
    }
    collect_file_description_entry(project_root, path, desc, kind, output, size_cache);
    if path.is_dir() {
        let Ok(entries) = fs::read_dir(path) else {
            return;
        };
        for entry in entries.flatten() {
            collect_file_description_tree(
                project_root,
                &entry.path(),
                desc,
                kind,
                output,
                size_cache,
            );
        }
    }
}

fn collect_file_description_entry(
    project_root: &Path,
    path: &Path,
    desc: &str,
    kind: &str,
    output: &mut Vec<FileDescriptionEntry>,
    size_cache: &mut HashMap<PathBuf, u64>,
) {
    let Ok(metadata) = fs::metadata(path) else {
        return;
    };
    let actual_kind = if metadata.is_dir() {
        "directory"
    } else if metadata.is_file() {
        "file"
    } else {
        return;
    };
    if kind != "all" && actual_kind != kind {
        return;
    }
    let modified = metadata.modified().ok().and_then(format_time);
    let modified_seconds = modified
        .as_deref()
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or_default();
    let age_days = current_timestamp()
        .parse::<u64>()
        .unwrap_or_default()
        .saturating_sub(modified_seconds)
        / 86_400;
    let (urgency, urgency_label) = if age_days > 7 {
        (3, "超过7天")
    } else if age_days > 3 {
        (2, "超过3天")
    } else if age_days > 1 {
        (1, "超过1天")
    } else {
        (0, "正常")
    };
    let name = path
        .file_name()
        .map(|value| value.to_string_lossy().to_string())
        .unwrap_or_else(|| path.to_string_lossy().to_string());
    let size = if metadata.is_dir() {
        directory_size(path, size_cache)
    } else {
        metadata.len()
    };
    output.push(FileDescriptionEntry {
        path: relative_project_path(project_root, path),
        name,
        kind: actual_kind.to_owned(),
        desc: desc.to_owned(),
        size,
        modified,
        age_days,
        urgency,
        urgency_label: urgency_label.to_owned(),
        stale: false,
    });
}

fn directory_size(path: &Path, cache: &mut HashMap<PathBuf, u64>) -> u64 {
    if is_ignored_path(path) {
        return 0;
    }
    if let Some(size) = cache.get(path) {
        return *size;
    }
    let Ok(metadata) = fs::symlink_metadata(path) else {
        return 0;
    };
    if metadata.file_type().is_symlink() {
        return 0;
    }
    if metadata.is_file() {
        return metadata.len();
    }
    if !metadata.is_dir() {
        return 0;
    }
    let size = fs::read_dir(path)
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .map(|entry| directory_size(&entry.path(), cache))
        .sum();
    cache.insert(path.to_path_buf(), size);
    size
}

fn file_description_summary(entries: &[FileDescriptionEntry]) -> FileDescriptionSummary {
    let folders_total_size = entries
        .iter()
        .filter(|entry| entry.kind == "directory")
        .map(|entry| entry.size)
        .sum();
    let files_total_size = entries
        .iter()
        .filter(|entry| entry.kind == "file")
        .map(|entry| entry.size)
        .sum();
    let mut folders = entries
        .iter()
        .filter(|entry| entry.kind == "directory")
        .map(|entry| FileDescriptionSummaryEntry {
            path: entry.path.clone(),
            name: entry.name.clone(),
            size: entry.size,
        })
        .collect::<Vec<_>>();
    let mut files = entries
        .iter()
        .filter(|entry| entry.kind == "file")
        .map(|entry| FileDescriptionSummaryEntry {
            path: entry.path.clone(),
            name: entry.name.clone(),
            size: entry.size,
        })
        .collect::<Vec<_>>();
    folders.sort_by(|left, right| right.size.cmp(&left.size));
    files.sort_by(|left, right| right.size.cmp(&left.size));
    folders.truncate(3);
    files.truncate(3);
    FileDescriptionSummary {
        folders,
        files,
        folders_total_size,
        files_total_size,
    }
}

fn move_to_sources_temp(state: &AppState, source: &Path) -> Result<(), String> {
    let project_root = state
        .project_root
        .canonicalize()
        .map_err(|_| "项目目录不存在".to_owned())?;
    let temp_root = project_root.join("sources").join("temp");
    move_path_to_directory(&project_root, source, &temp_root, "sources/temp")
}

fn move_path_to_directory(
    project_root: &Path,
    source: &Path,
    destination_root: &Path,
    destination_label: &str,
) -> Result<(), String> {
    let source = source
        .canonicalize()
        .map_err(|_| "待处理路径不存在".to_owned())?;
    if !source.starts_with(project_root) {
        return Err("待处理路径不在项目目录内".to_owned());
    }
    let destination_root =
        fs::canonicalize(destination_root).unwrap_or_else(|_| destination_root.to_path_buf());
    if source == destination_root || source.starts_with(&destination_root) {
        return Err(format!("不能移动 {destination_label}中的内容"));
    }
    let name = source
        .file_name()
        .ok_or_else(|| "待处理路径名称无效".to_owned())?;
    let target = unique_path(destination_root.join(name));
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("创建 {destination_label} 目录失败：{error}"))?;
    }
    fs::rename(&source, &target)
        .map_err(|error| format!("移动到 {destination_label} 失败：{error}"))?;
    Ok(())
}

fn move_to_recycle_bin(state: &AppState, source: &Path) -> Result<(), String> {
    let project_root = state
        .project_root
        .canonicalize()
        .map_err(|_| "项目目录不存在".to_owned())?;
    let recycle_relative = state.authority.file_description().recycle_bin();
    let recycle_root = relative_project_path_buf(&state.project_root, recycle_relative)
        .ok_or_else(|| "回收站目录配置无效".to_owned())?;
    move_path_to_directory(&project_root, source, &recycle_root, "回收站")
}

fn cleanup_trueno_inference_artifacts(root: &Path, user: &SessionUser) {
    let output_dir = root.join("portal_outputs").join(user_key(user));
    let _ = fs::remove_dir_all(output_dir);
}

fn is_local_import_request(peer_ip: IpAddr) -> bool {
    if peer_ip.is_loopback() {
        return true;
    }
    let Ok(socket) = std::net::UdpSocket::bind("0.0.0.0:0") else {
        return false;
    };
    if socket.connect("8.8.8.8:80").is_err() {
        return false;
    }
    socket
        .local_addr()
        .map(|address| address.ip() == peer_ip)
        .unwrap_or(false)
}

fn configured_file_description_path(state: &AppState, requested: &str) -> Option<PathBuf> {
    let candidate = safe_project_existing_path(&state.project_root, requested).ok()?;
    let candidate_canonical = candidate.canonicalize().ok()?;
    for root in resolved_file_description_roots(state) {
        let Ok(root_canonical) = root.path.canonicalize() else {
            continue;
        };
        if candidate_canonical == root_canonical && !root.include_root {
            continue;
        }
        if candidate_canonical.starts_with(&root_canonical) {
            return Some(candidate_canonical);
        }
    }
    None
}

fn configured_dependency_path(state: &AppState, requested: &str) -> Option<PathBuf> {
    let requested_path = relative_project_path_buf(&state.project_root, requested)?;
    let requested_canonical = requested_path.canonicalize().ok();
    for item in state.authority.document_display() {
        let configured = relative_project_path_buf(&state.project_root, &item.file)?;
        if relative_project_path(&state.project_root, &configured) == requested
            && configured.is_file()
        {
            return Some(configured);
        }
        let Some(requested_canonical) = requested_canonical.as_ref() else {
            continue;
        };
        if let Ok(configured_canonical) = configured.canonicalize() {
            if configured_canonical == *requested_canonical && configured_canonical.is_file() {
                return Some(configured_canonical);
            }
        }
    }
    None
}

fn user_generated_file_allowed(
    state: &AppState,
    user: &SessionUser,
    model: &str,
    root: &Path,
    requested: &str,
) -> bool {
    let requested = requested.replace('\\', "/");
    state
        .jobs
        .lock()
        .unwrap()
        .values()
        .filter(|job| job.owner_id == user.id && job.model == model)
        .flat_map(|job| job.generated_files.iter())
        .any(|file| file.path == requested && root.join(Path::new(&file.path)).is_file())
        || current_weekly_data_file_allowed(state.authority.as_ref(), user, model, root, &requested)
}

fn visible_in_file_list(
    authority: &crate::authority::AuthorityConfig,
    model: &str,
    path: &str,
) -> bool {
    if let Some(names) = authority.visible_file_names(model) {
        let filename = Path::new(path)
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or_default();
        return names.iter().any(|name| name.eq_ignore_ascii_case(filename));
    }
    if model == "offline" {
        let filename = Path::new(path)
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or_default()
            .to_ascii_lowercase();
        let extension = Path::new(path)
            .extension()
            .and_then(|value| value.to_str())
            .unwrap_or_default();
        if extension.eq_ignore_ascii_case("zip")
            && (filename.starts_with("accuracy_") || filename.starts_with("image_"))
        {
            return false;
        }
        return matches!(
            extension.to_ascii_lowercase().as_str(),
            "zip" | "txt" | "xlsx"
        );
    }
    if model == "trueno" {
        return matches!(
            Path::new(path)
                .extension()
                .and_then(|value| value.to_str())
                .unwrap_or_default()
                .to_ascii_lowercase()
                .as_str(),
            "png" | "jpg" | "jpeg"
        );
    }
    true
}

fn current_weekly_data_file_allowed(
    authority: &crate::authority::AuthorityConfig,
    user: &SessionUser,
    model: &str,
    root: &Path,
    requested: &str,
) -> bool {
    if model != "weekly" {
        return false;
    }
    let Some(names) = authority.visible_file_names(model) else {
        return false;
    };
    let candidate = root.join(Path::new(requested));
    let data_dir = weekly_data_dir(root, user);
    let Ok(candidate) = candidate.canonicalize() else {
        return false;
    };
    let Ok(data_dir) = data_dir.canonicalize() else {
        return false;
    };
    let filename = candidate
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or_default();
    candidate.starts_with(data_dir)
        && candidate.is_file()
        && names.iter().any(|name| name.eq_ignore_ascii_case(filename))
}

fn collect_recent_files_recursive(
    root: &Path,
    candidate: &Path,
    depth: usize,
    cutoff: SystemTime,
    output: &mut Vec<FileInfo>,
) {
    if is_ignored_path(candidate) || !candidate.exists() {
        return;
    }
    let metadata = match fs::metadata(candidate) {
        Ok(metadata) => metadata,
        Err(_) => return,
    };
    if metadata.is_file() {
        if metadata
            .modified()
            .map(|time| time < cutoff)
            .unwrap_or(false)
        {
            return;
        }
        if let Some(info) = file_info(root, candidate, &metadata) {
            output.push(info);
        }
        return;
    }
    if depth == 0 {
        return;
    }
    let Ok(entries) = fs::read_dir(candidate) else {
        return;
    };
    for entry in entries.flatten() {
        collect_recent_files_recursive(root, &entry.path(), depth - 1, cutoff, output);
    }
}

fn file_info(root: &Path, path: &Path, metadata: &fs::Metadata) -> Option<FileInfo> {
    let name = path.file_name()?.to_string_lossy().to_string();
    Some(FileInfo {
        path: relative_path(root, path),
        display_name: name.clone(),
        name,
        size: metadata.len(),
        modified: metadata.modified().ok().and_then(format_time),
    })
}

fn modified_sort_key(file: &FileInfo) -> u64 {
    file.modified
        .as_deref()
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or_default()
}

fn is_annotation_seed_file(name: &str) -> bool {
    matches!(
        name,
        "数字-7-类别标准化映射规则.yaml"
            | "数字-7-类别标准化映射规则.txt"
            | "数字-8-原始标注类别清单.txt"
    )
}

fn is_ignored_path(path: &Path) -> bool {
    path.components().any(|component| {
        let value = component.as_os_str().to_string_lossy();
        matches!(
            value.as_ref(),
            "venv"
                | "venv_linux"
                | "chrome"
                | "chromium_user_data"
                | ".portal_tmp"
                | ".portal_recycle"
                | "__pycache__"
                | ".git"
        )
    })
}

fn safe_filename(value: &str) -> String {
    let basename = Path::new(value)
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("uploaded-file");
    let cleaned: String = basename
        .chars()
        .map(|value| {
            if value.is_control() || matches!(value, '/' | '\\' | ':' | '*') {
                '_'
            } else {
                value
            }
        })
        .collect();
    if cleaned.trim().is_empty() {
        "uploaded-file".to_owned()
    } else {
        cleaned
    }
}

fn find_input_file(root: &Path, input: &str, extensions: &[&str]) -> Result<PathBuf, String> {
    if !input.trim().is_empty() {
        let direct = safe_existing_path(root, input)?;
        if direct.is_file() {
            return Ok(direct);
        }
        return Err("指定输入文件不存在".to_owned());
    }
    let input_dir = root.join("portal_inputs");
    let mut candidates = fs::read_dir(&input_dir)
        .map_err(|_| "请先上传 TXT 日志文件".to_owned())?
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| {
            path.is_file()
                && path
                    .extension()
                    .and_then(|value| value.to_str())
                    .map(|value| {
                        extensions
                            .iter()
                            .any(|item| value.eq_ignore_ascii_case(item))
                    })
                    .unwrap_or(false)
        })
        .collect::<Vec<_>>();
    if candidates.is_empty() {
        return Err("请先上传 TXT 日志文件".to_owned());
    }
    candidates.sort_by_key(|path| {
        fs::metadata(path)
            .and_then(|metadata| metadata.modified())
            .unwrap_or(UNIX_EPOCH)
    });
    candidates.pop().ok_or_else(|| "找不到输入文件".to_owned())
}

fn safe_existing_path(root: &Path, relative: &str) -> Result<PathBuf, String> {
    let candidate = safe_relative_path(root, relative, "文件路径不合法")?;
    if is_ignored_path(&candidate) {
        return Err("不允许访问受保护目录".to_owned());
    }
    let canonical_root = root
        .canonicalize()
        .map_err(|_| "模型目录不存在".to_owned())?;
    let canonical = candidate
        .canonicalize()
        .map_err(|_| "文件不存在".to_owned())?;
    if !canonical.starts_with(&canonical_root) || (!canonical.is_file() && !canonical.is_dir()) {
        return Err("文件路径不在模型目录内".to_owned());
    }
    Ok(canonical)
}

fn safe_project_existing_path(root: &Path, relative: &str) -> Result<PathBuf, String> {
    let candidate = safe_relative_path(root, relative, "项目路径不合法")?;
    let canonical_root = root
        .canonicalize()
        .map_err(|_| "项目目录不存在".to_owned())?;
    let canonical = candidate
        .canonicalize()
        .map_err(|_| "项目路径不存在".to_owned())?;
    if !canonical.starts_with(&canonical_root) || (!canonical.is_file() && !canonical.is_dir()) {
        return Err("项目路径不在项目目录内".to_owned());
    }
    Ok(canonical)
}

fn safe_relative_path(root: &Path, relative: &str, message: &str) -> Result<PathBuf, String> {
    let relative_path = Path::new(relative.trim());
    if relative.trim().is_empty()
        || relative_path.is_absolute()
        || relative_path.components().any(|component| {
            matches!(
                component,
                Component::ParentDir | Component::RootDir | Component::Prefix(_)
            )
        })
    {
        return Err(message.to_owned());
    }
    Ok(root.join(relative_path))
}

fn relative_project_path_buf(root: &Path, value: &str) -> Option<PathBuf> {
    let relative = value.trim().replace('\\', "/");
    let path = Path::new(&relative);
    if relative.is_empty()
        || path.is_absolute()
        || path.components().any(|component| {
            matches!(
                component,
                Component::ParentDir | Component::RootDir | Component::Prefix(_)
            )
        })
    {
        return None;
    }
    Some(root.join(path))
}

fn relative_project_path(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/")
}

fn relative_path(root: &Path, path: &Path) -> String {
    relative_project_path(root, path)
}

fn is_yaml_path(path: &Path) -> bool {
    matches!(
        path.extension().and_then(|value| value.to_str()),
        Some("yaml" | "yml")
    )
}

fn is_text_path(path: &Path) -> bool {
    matches!(
        path.extension()
            .and_then(|value| value.to_str())
            .map(|value| value.to_ascii_lowercase())
            .as_deref(),
        Some("txt" | "md" | "yaml" | "yml" | "log" | "html" | "json" | "csv")
    )
}

fn mime_type(path: &Path) -> &'static str {
    match path.extension().and_then(|value| value.to_str()) {
        Some("txt" | "md" | "log" | "csv") => "text/plain; charset=utf-8",
        Some("html") => "text/html; charset=utf-8",
        Some("json") => "application/json",
        Some("yaml" | "yml") => "text/yaml; charset=utf-8",
        Some("xlsx") => "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        Some("zip") => "application/zip",
        Some("png") => "image/png",
        _ => "application/octet-stream",
    }
}

fn format_time(value: SystemTime) -> Option<String> {
    value
        .duration_since(UNIX_EPOCH)
        .ok()
        .map(|duration| duration.as_secs().to_string())
}

fn current_timestamp() -> String {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_secs().to_string())
        .unwrap_or_else(|_| "0".to_owned())
}

fn open_with_default_app(path: &Path) -> Result<(), String> {
    let status = if cfg!(target_os = "windows") {
        Command::new("cmd")
            .args(["/C", "start", "", &path.to_string_lossy()])
            .status()
    } else if cfg!(target_os = "macos") {
        Command::new("open").arg(path).status()
    } else {
        Command::new("xdg-open").arg(path).status()
    }
    .map_err(|error| format!("调用本机默认打开方式失败：{error}"))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("本机默认打开方式返回状态 {status}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn user(id: i64, role: &str) -> SessionUser {
        SessionUser {
            id,
            username: format!("user{id}"),
            role: role.to_owned(),
        }
    }

    fn temp_root(name: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "algorithm_web_{name}_{}_{}",
            std::process::id(),
            current_timestamp()
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        root
    }

    fn authority() -> crate::authority::AuthorityConfig {
        crate::authority::AuthorityConfig::load(&PathBuf::from(env!("CARGO_MANIFEST_DIR")))
    }

    #[test]
    fn non_admin_offline_config_is_limited_to_config3() {
        let root = temp_root("offline_config");
        for name in ["config-1.yaml", "config-2.yaml", "config-3.yaml"] {
            fs::write(root.join(name), "recognition: []\n").unwrap();
        }
        let standard = user(7, "普通二级");
        assert_eq!(
            resolve_offline_config(&standard, &root, "").unwrap(),
            "config-3.yaml"
        );
        assert!(resolve_offline_config(&standard, &root, "config-1.yaml").is_err());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn admin_offline_config_can_select_all_configs() {
        let root = temp_root("admin_config");
        for name in ["config-1.yaml", "config-2.yaml", "config-3.yaml"] {
            fs::write(root.join(name), "recognition: []\n").unwrap();
        }
        let admin = user(1, "管理员");
        assert_eq!(
            resolve_offline_config(&admin, &root, "config-2.yaml").unwrap(),
            "config-2.yaml"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn config_file_list_only_returns_existing_admin_yaml() {
        let root = temp_root("config_list");
        fs::write(root.join("config-2.yaml"), "recognition: []\n").unwrap();
        fs::write(root.join("notes.txt"), "not yaml\n").unwrap();
        let admin = user(1, "管理员");
        let standard = user(2, "普通二级");
        assert_eq!(
            config_files_for_user(&admin, "offline", &root),
            vec!["config-2.yaml"]
        );
        assert!(config_files_for_user(&standard, "offline", &root).is_empty());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn prune_authorization_codes_keeps_latest_five() {
        let connection = rusqlite::Connection::open_in_memory().unwrap();
        connection
            .execute(
                "CREATE TABLE authorization_codes (
                    code TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )",
                [],
            )
            .unwrap();
        for index in 1..=7 {
            connection
                .execute(
                    "INSERT INTO authorization_codes (code, role, created_at) VALUES (?1, '普通二级', ?2)",
                    rusqlite::params![format!("AUTH{index}"), format!("2026-01-01 00:00:0{index}")],
                )
                .unwrap();
        }
        prune_authorization_codes(&connection).unwrap();
        let count: i64 = connection
            .query_row("SELECT COUNT(*) FROM authorization_codes", [], |row| {
                row.get(0)
            })
            .unwrap();
        let oldest_count: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM authorization_codes WHERE code IN ('AUTH1', 'AUTH2')",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 5);
        assert_eq!(oldest_count, 0);
    }

    #[test]
    fn duplicate_upload_names_keep_extension() {
        let root = temp_root("unique_upload");
        let original = root.join("1.txt");
        fs::write(&original, "x").unwrap();
        assert_eq!(unique_path(original).file_name().unwrap(), "1-1.txt");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn offline_uploads_go_to_sources_with_two_digit_suffix() {
        let root = temp_root("offline_upload");
        fs::create_dir_all(root.join("sources")).unwrap();
        fs::write(root.join("sources/package.zip"), "x").unwrap();
        let destination =
            unique_upload_destination("offline", &root, &user(9, "普通二级"), "", "package.zip");
        assert_eq!(destination.file_name().unwrap(), "package-01.zip");
        assert_eq!(destination.parent().unwrap(), root.join("sources"));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn trueno_model_uploads_go_to_current_user_temp_dir() {
        let root = temp_root("trueno_model_upload");
        let standard = user(9, "普通二级");
        assert!(validate_upload_file("trueno", "model", "weights.pt").is_ok());
        assert!(validate_upload_file("trueno", "", "sample.png").is_ok());
        assert!(validate_upload_file("trueno", "model", "sample.png").is_err());
        let destination =
            unique_upload_destination("trueno", &root, &standard, "model", "weights.pt");
        assert_eq!(
            destination.parent().unwrap(),
            root.join("sources").join("temp").join(user_key(&standard))
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn trueno_model_resolution_allows_native_and_own_import_only() {
        let root = temp_root("trueno_model_resolution");
        let owner = user(7, "普通二级");
        let other = user(8, "普通二级");
        let native_dir = root.join("model");
        let own_dir = root.join("sources").join("temp").join(user_key(&owner));
        let other_dir = root.join("sources").join("temp").join(user_key(&other));
        fs::create_dir_all(&native_dir).unwrap();
        fs::create_dir_all(&own_dir).unwrap();
        fs::create_dir_all(&other_dir).unwrap();
        fs::write(native_dir.join("local.pt"), "x").unwrap();
        fs::write(own_dir.join("import.pt"), "x").unwrap();
        fs::write(other_dir.join("import.pt"), "x").unwrap();

        assert_eq!(
            resolve_trueno_model_path(&root, &owner, "local.pt")
                .unwrap()
                .file_name()
                .unwrap(),
            "local.pt"
        );
        assert!(
            resolve_trueno_model_path(
                &root,
                &owner,
                &format!("sources/temp/{}/import.pt", user_key(&owner))
            )
            .is_ok()
        );
        assert!(
            resolve_trueno_model_path(
                &root,
                &owner,
                &format!("sources/temp/{}/import.pt", user_key(&other))
            )
            .is_err()
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn weekly_file_list_only_exposes_todo_workbook() {
        let authority = authority();
        assert!(visible_in_file_list(
            &authority,
            "weekly",
            "date_review/portal_data/u/old.xlsx"
        ));
        assert!(visible_in_file_list(
            &authority,
            "weekly",
            "date_review/portal_data/u/result-2.xlsx"
        ));
        assert!(visible_in_file_list(
            &authority,
            "weekly",
            "date_review/portal_work/u/job/todo/upload-2.xlsx"
        ));
        assert!(!visible_in_file_list(
            &authority,
            "weekly",
            "output/u/job/report.md"
        ));
        assert!(visible_in_file_list(
            &authority,
            "log",
            "portal_outputs/u/job/log.xlsx"
        ));
    }

    #[test]
    fn offline_file_list_shows_only_review_outputs() {
        let authority = authority();
        assert!(visible_in_file_list(
            &authority,
            "offline",
            "newwrap/job/result.zip"
        ));
        assert!(visible_in_file_list(
            &authority,
            "offline",
            "newwrap/job/checklist.xlsx"
        ));
        assert!(visible_in_file_list(
            &authority,
            "offline",
            "newwrap/job/notes.txt"
        ));
        assert!(!visible_in_file_list(
            &authority,
            "offline",
            "newwrap/job/accuracy_1.zip"
        ));
        assert!(!visible_in_file_list(
            &authority,
            "offline",
            "newwrap/job/image_1.zip"
        ));
        assert!(!visible_in_file_list(
            &authority,
            "offline",
            "newwrap/job/image.png"
        ));
        assert!(!visible_in_file_list(
            &authority,
            "offline",
            "newwrap/job/state.json"
        ));
    }

    #[test]
    fn annotation_file_list_uses_authority_whitelist() {
        let authority = authority();
        assert!(visible_in_file_list(
            &authority,
            "annotation",
            "Work-txt/u1/数字-9-模型标注训练清单.xlsx"
        ));
        assert!(visible_in_file_list(
            &authority,
            "annotation",
            "Work-txt/u1/数字-0-h模式历史学习.json"
        ));
        assert!(!visible_in_file_list(
            &authority,
            "annotation",
            "Work-txt/u1/数字-8-原始标注类别清单.txt"
        ));
    }

    #[test]
    fn job_log_retains_latest_nine_pages_of_configured_lines() {
        let mut log = String::new();
        for index in 1..=(JOB_LOG_MAX_PAGES * 2 + 2) {
            append_log_text(&mut log, &format!("line-{index}"), 2 * JOB_LOG_MAX_PAGES);
        }

        let lines = log.lines().collect::<Vec<_>>();
        assert_eq!(lines.len(), 2 * JOB_LOG_MAX_PAGES);
        assert_eq!(lines.first(), Some(&"line-3"));
        assert_eq!(lines.last(), Some(&"line-20"));
    }
}
