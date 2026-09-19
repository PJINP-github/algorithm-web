#[derive(serde::Deserialize)]
// 允许从 HTML 表单的 POST body 反序列化
pub struct NewTodo {
    pub title: String,
}
// 新建待办只需要 title，id 由 SQLite 自增生成

#[derive(serde::Deserialize)]
pub struct Credentials {
    pub username: String,
    pub password: String,
    #[serde(default)]
    pub authorization_code: String,
}

#[derive(serde::Serialize)]
pub struct AuthResponse {
    pub success: bool,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub token: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub username: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub role: Option<String>,
}

#[derive(serde::Deserialize)]
pub struct ModelRunRequest {
    pub model: String,
    pub action: String,
    #[serde(default)]
    pub input: String,
}

#[derive(serde::Deserialize)]
pub struct ConfigUpdateRequest {
    pub model: String,
    #[serde(default)]
    pub path: String,
    pub content: String,
}

#[derive(serde::Deserialize)]
pub struct CreateCodeRequest {
    pub role: String,
}

#[derive(serde::Serialize, Clone)]
pub struct SessionUser {
    pub id: i64,
    pub username: String,
    pub role: String,
}

#[derive(serde::Serialize, Clone)]
pub struct JobInfo {
    pub id: String,
    pub model: String,
    pub owner_id: i64,
    pub label: String,
    pub status: String,
    pub log: String,
    pub exit_code: Option<i32>,
    pub generated_files: Vec<FileInfo>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub opened_url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<serde_json::Value>,
}

#[derive(serde::Serialize, Clone)]
pub struct FileInfo {
    pub path: String,
    pub name: String,
    pub display_name: String,
    #[serde(default)]
    pub kind: String,
    pub size: u64,
    pub modified: Option<String>,
}

#[derive(serde::Serialize)]
pub struct WorkspaceModel {
    pub id: String,
    pub name: String,
    pub description: String,
    pub actions: Vec<WorkspaceAction>,
    pub config_files: Vec<String>,
    pub log_visual_lines: usize,
    #[serde(default)]
    pub generic: bool,
    #[serde(default)]
    pub imports: Vec<crate::authority::GenericImport>,
    #[serde(default)]
    pub parameters: Vec<crate::authority::GenericParameter>,
    #[serde(default)]
    pub parameter_combinations: Vec<Vec<String>>,
    #[serde(default)]
    pub display_imported_folder: bool,
    #[serde(default)]
    pub latest_temporal_display_file: Option<crate::authority::LatestTemporalDisplay>,
}

#[derive(serde::Serialize)]
pub struct WorkspaceAction {
    pub id: String,
    pub name: String,
    pub description: String,
    pub min_role: String,
    pub allowed: bool,
}

#[derive(serde::Deserialize)]
pub struct FileRequest {
    pub model: String,
    pub path: String,
}

#[derive(serde::Deserialize)]
pub struct FileQuery {
    pub model: String,
    pub path: String,
}

#[derive(serde::Deserialize)]
pub struct FileContentQuery {
    pub model: String,
    #[serde(default)]
    pub path: String,
}

#[derive(serde::Deserialize)]
pub struct UploadQuery {
    pub model: String,
    #[serde(default)]
    pub slot: String,
}

#[derive(serde::Deserialize)]
pub struct LocalImportRequest {
    pub model: String,
    pub source_path: String,
    #[serde(default)]
    pub slot: String,
}

#[derive(serde::Serialize)]
pub struct ApiMessage {
    pub success: bool,
    pub message: String,
}

#[derive(serde::Serialize)]
pub struct JobStartResponse {
    pub job_id: String,
}

#[derive(serde::Serialize)]
pub struct UploadResponse {
    pub path: String,
    pub name: String,
    pub display_name: String,
    #[serde(default)]
    pub kind: String,
}

#[derive(serde::Serialize)]
pub struct FileContentResponse {
    pub path: String,
    pub content: String,
}

#[derive(serde::Deserialize)]
pub struct FileDescriptionQuery {
    #[serde(default = "default_file_description_kind")]
    pub kind: String,
}

#[derive(serde::Deserialize)]
pub struct FileDescriptionDeleteRequest {
    pub paths: Vec<String>,
    #[serde(default = "default_file_description_delete_action")]
    pub action: String,
}

fn default_file_description_kind() -> String {
    "file".to_owned()
}

fn default_file_description_delete_action() -> String {
    "delete".to_owned()
}

#[derive(serde::Serialize)]
pub struct CodeInfo {
    pub code: String,
    pub role: String,
    pub used: bool,
    pub created_at: String,
}
