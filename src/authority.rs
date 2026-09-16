use std::{
    collections::HashMap,
    fs,
    path::{Path, PathBuf},
};

#[derive(Clone, Debug, serde::Deserialize)]
pub struct AuthorityConfig {
    #[serde(rename = "Feature_Tab_Visibility", default)]
    feature_tab_visibility: HashMap<String, HashMap<String, bool>>,
    #[serde(rename = "Feature_File_Display", default)]
    feature_file_display: HashMap<String, Vec<String>>,
    #[serde(rename = "Log_Display", default)]
    log_display: LogDisplayConfig,
    #[serde(rename = "Browser_Path", default)]
    browser_path: Option<BrowserPathConfig>,
    #[serde(default = "default_rows_downloaded_file_algorithm")]
    rows_downloaded_file_algorithm: usize,
    #[serde(rename = "Document_display", default)]
    document_display: Vec<DocumentDisplay>,
    #[serde(rename = "File_Description", default)]
    file_description: FileDescriptionConfig,
    #[serde(rename = "Administrator_Rights", default)]
    administrator: HashMap<String, bool>,
    #[serde(rename = "Standard_Level_1_User_Privileges", default)]
    standard_level_1: HashMap<String, bool>,
    #[serde(rename = "Standard_Level_2_User_Privileges", default)]
    standard_level_2: HashMap<String, bool>,
}

#[derive(Clone, Debug, serde::Deserialize)]
#[serde(untagged)]
enum BrowserPathConfig {
    Single(String),
    PerModel(HashMap<String, String>),
}

const ANNOTATION_BROWSER_KEYS: &[&str] = &[
    "annotation",
    "Model-Annotation",
    "Model_Annotation",
    "Model-Annotation-tag-0.1.1+python",
];
const WEEKLY_BROWSER_KEYS: &[&str] = &[
    "weekly",
    "Weekly-Report-Print",
    "Weekly_Report_Print",
    "Weekly-Report-Print-tag-0.0.8+python",
];

#[derive(Clone, Debug, serde::Deserialize)]
pub struct DocumentDisplay {
    pub file: String,
    #[serde(default)]
    pub desc: String,
    #[serde(rename = "class", default)]
    pub category: String,
    #[serde(default)]
    pub notes: String,
}

#[derive(Clone, Debug, serde::Deserialize)]
pub struct FileDescriptionConfig {
    #[serde(default)]
    pub enabled: bool,
    #[serde(
        default = "default_file_description_page_rows",
        alias = "rows_per_page",
        alias = "line_limit"
    )]
    pub page_rows: usize,
    #[serde(default, alias = "allowed_extensions")]
    pub display_extensions: Vec<String>,
    #[serde(
        default = "default_file_description_stale_days",
        alias = "cleanup_days"
    )]
    pub stale_days: u64,
    #[serde(default = "default_file_description_recycle_bin")]
    pub recycle_bin: String,
    #[serde(default)]
    pub roots: Vec<FileDescriptionRoot>,
}

impl Default for FileDescriptionConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            page_rows: default_file_description_page_rows(),
            display_extensions: Vec::new(),
            stale_days: default_file_description_stale_days(),
            recycle_bin: default_file_description_recycle_bin(),
            roots: Vec::new(),
        }
    }
}

impl FileDescriptionConfig {
    pub fn page_rows(&self) -> usize {
        self.page_rows.clamp(1, 500)
    }

    pub fn stale_days(&self) -> u64 {
        self.stale_days.clamp(1, 3650)
    }

    pub fn recycle_bin(&self) -> &str {
        let value = self.recycle_bin.trim();
        if value.is_empty() {
            ".portal_recycle"
        } else {
            value
        }
    }

    pub fn file_extension_allowed(&self, path: &Path) -> bool {
        if self.display_extensions.is_empty() {
            return true;
        }
        let extension = path
            .extension()
            .and_then(|value| value.to_str())
            .unwrap_or_default()
            .trim_start_matches('.')
            .to_ascii_lowercase();
        if extension.is_empty() {
            return false;
        }
        self.display_extensions.iter().any(|configured| {
            configured
                .trim()
                .trim_start_matches('.')
                .eq_ignore_ascii_case(&extension)
        })
    }
}

#[derive(Clone, Debug, serde::Deserialize)]
pub struct FileDescriptionRoot {
    pub path: String,
    #[serde(default)]
    pub desc: String,
    #[serde(default = "default_true")]
    pub recursive: bool,
}

fn default_true() -> bool {
    true
}

fn default_file_description_page_rows() -> usize {
    15
}

fn default_file_description_stale_days() -> u64 {
    15
}

fn default_file_description_recycle_bin() -> String {
    ".portal_recycle".to_owned()
}

#[derive(Clone, Debug, serde::Deserialize)]
pub struct LogDisplayConfig {
    #[serde(default = "default_log_visual_lines")]
    max_visual_lines: usize,
}

impl Default for LogDisplayConfig {
    fn default() -> Self {
        Self {
            max_visual_lines: default_log_visual_lines(),
        }
    }
}

fn default_log_visual_lines() -> usize {
    27
}

fn default_rows_downloaded_file_algorithm() -> usize {
    8
}

impl AuthorityConfig {
    pub fn load(project_root: &Path) -> Self {
        let path = project_root.join("model").join("authority.yaml");
        match fs::read_to_string(&path) {
            Ok(content) => match serde_yaml::from_str(&content) {
                Ok(config) => config,
                Err(error) => {
                    eprintln!(
                        "读取权限配置失败，使用内置权限：{}：{error}",
                        path.display()
                    );
                    Self::default_policy()
                }
            },
            Err(error) => {
                eprintln!(
                    "打开权限配置失败，使用内置权限：{}：{error}",
                    path.display()
                );
                Self::default_policy()
            }
        }
    }

    pub fn allows(&self, role: &str, permission: &str) -> bool {
        self.permissions_for_role(role)
            .and_then(|permissions| permissions.get(permission))
            .copied()
            .unwrap_or(false)
    }

    pub fn model_action(model: &str, action: &str) -> String {
        format!("model.{model}.{action}")
    }

    pub fn model_visible(&self, role: &str, model: &str) -> bool {
        let Some(role_key) = Self::role_config_key(role) else {
            return false;
        };
        let Some(feature_key) = Self::model_feature_key(model) else {
            return false;
        };
        self.feature_tab_visibility
            .get(role_key)
            .and_then(|features| features.get(feature_key))
            .copied()
            .unwrap_or(false)
    }

    pub fn visible_file_names(&self, model: &str) -> Option<&[String]> {
        let feature_key = Self::model_feature_key(model)?;
        self.feature_file_display
            .get(feature_key)
            .filter(|names| !names.is_empty())
            .map(Vec::as_slice)
    }

    pub fn log_visual_lines(&self) -> usize {
        self.log_display.max_visual_lines.clamp(1, 200)
    }

    pub fn browser_path(&self, project_root: &Path, model: &str) -> Option<PathBuf> {
        let value = match self.browser_path.as_ref()? {
            BrowserPathConfig::Single(value) => Some(value.as_str()),
            BrowserPathConfig::PerModel(paths) => {
                let aliases: &[&str] = match model {
                    "annotation" => ANNOTATION_BROWSER_KEYS,
                    "weekly" => WEEKLY_BROWSER_KEYS,
                    _ => &[],
                };
                aliases
                    .iter()
                    .find_map(|key| paths.get(*key).map(String::as_str))
                    .or_else(|| paths.get(model).map(String::as_str))
            }
        }?
        .trim();
        if value.is_empty() {
            return None;
        }
        let path = PathBuf::from(value);
        if path.is_absolute() {
            return None;
        }
        Some(project_root.join(path))
    }

    pub fn rows_downloaded_file_algorithm(&self) -> usize {
        self.rows_downloaded_file_algorithm.clamp(1, 200)
    }

    pub fn document_display(&self) -> &[DocumentDisplay] {
        &self.document_display
    }

    pub fn file_description(&self) -> &FileDescriptionConfig {
        &self.file_description
    }

    fn permissions_for_role(&self, role: &str) -> Option<&HashMap<String, bool>> {
        match role {
            "管理员" => Some(&self.administrator),
            "普通二级" => Some(&self.standard_level_2),
            "普通一级" => Some(&self.standard_level_1),
            _ => None,
        }
    }

    fn role_config_key(role: &str) -> Option<&'static str> {
        match role {
            "管理员" => Some("Administrator_Rights"),
            "普通二级" => Some("Standard_Level_2_User_Privileges"),
            "普通一级" => Some("Standard_Level_1_User_Privileges"),
            _ => None,
        }
    }

    fn model_feature_key(model: &str) -> Option<&'static str> {
        match model {
            "log" => Some("Log_Review"),
            "annotation" => Some("Model-Annotation"),
            "offline" => Some("Offline_Package_Organization"),
            "weekly" => Some("Weekly-Report-Print"),
            "trueno" => Some("Trueno-Inference"),
            _ => None,
        }
    }

    fn default_policy() -> Self {
        let mut common = HashMap::new();
        for permission in [
            "auth.login",
            "auth.register",
            "auth.logout",
            "workspace.view",
            "files.list",
            "files.upload",
            "files.download",
            "files.open",
            "files.content",
        ] {
            common.insert(permission.to_owned(), true);
        }

        let model_permissions = [
            "model.log.convert",
            "model.log.debug_meter",
            "model.annotation.task",
            "model.annotation.model",
            "model.annotation.sum",
            "model.annotation.sort",
            "model.annotation.summary",
            "model.offline.mode1",
            "model.offline.mode2",
            "model.offline.mode3",
            "model.offline.mode4",
            "model.offline.mode5",
            "model.weekly.refresh",
            "model.weekly.generate",
            "model.weekly.todo",
            "model.trueno.infer",
        ];

        let mut administrator = common.clone();
        for permission in model_permissions {
            administrator.insert(permission.to_owned(), true);
        }
        administrator.extend([
            ("files.description.view".to_owned(), true),
            ("files.description.delete".to_owned(), true),
            ("config.view".to_owned(), true),
            ("config.edit".to_owned(), true),
            ("view_verification_codes".to_owned(), true),
            ("get_verification_code".to_owned(), true),
        ]);

        let mut standard_level_2 = common.clone();
        for permission in model_permissions {
            standard_level_2.insert(permission.to_owned(), true);
        }
        standard_level_2.extend([
            ("files.description.view".to_owned(), false),
            ("files.description.delete".to_owned(), false),
        ]);

        let mut standard_level_1 = common;
        standard_level_1.insert("model.log.convert".to_owned(), true);
        standard_level_1.insert("model.trueno.infer".to_owned(), false);

        let mut feature_tab_visibility = HashMap::new();
        feature_tab_visibility.insert(
            "Administrator_Rights".to_owned(),
            HashMap::from([
                ("Log_Review".to_owned(), true),
                ("Model-Annotation".to_owned(), true),
                ("Offline_Package_Organization".to_owned(), true),
                ("Weekly-Report-Print".to_owned(), true),
                ("Trueno-Inference".to_owned(), true),
            ]),
        );
        feature_tab_visibility.insert(
            "Standard_Level_1_User_Privileges".to_owned(),
            HashMap::from([
                ("Log_Review".to_owned(), true),
                ("Model-Annotation".to_owned(), false),
                ("Offline_Package_Organization".to_owned(), false),
                ("Weekly-Report-Print".to_owned(), false),
                ("Trueno-Inference".to_owned(), false),
            ]),
        );
        feature_tab_visibility.insert(
            "Standard_Level_2_User_Privileges".to_owned(),
            HashMap::from([
                ("Log_Review".to_owned(), true),
                ("Model-Annotation".to_owned(), true),
                ("Offline_Package_Organization".to_owned(), true),
                ("Weekly-Report-Print".to_owned(), true),
                ("Trueno-Inference".to_owned(), true),
            ]),
        );

        let feature_file_display = HashMap::from([
            (
                "Model-Annotation".to_owned(),
                vec![
                    "数字-9-模型标注训练清单.xlsx".to_owned(),
                    "数字-1-标注任务明细.txt".to_owned(),
                    "数字-4-模型训练记录.txt".to_owned(),
                    "数字-0-h模式历史学习.json".to_owned(),
                ],
            ),
            (
                "Weekly-Report-Print".to_owned(),
                vec![
                    "old.xlsx".to_owned(),
                    "result-2.xlsx".to_owned(),
                    "upload-2.xlsx".to_owned(),
                ],
            ),
        ]);

        Self {
            feature_tab_visibility,
            feature_file_display,
            log_display: LogDisplayConfig::default(),
            browser_path: None,
            rows_downloaded_file_algorithm: default_rows_downloaded_file_algorithm(),
            document_display: Vec::new(),
            file_description: FileDescriptionConfig::default(),
            administrator,
            standard_level_1,
            standard_level_2,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::AuthorityConfig;

    #[test]
    fn default_policy_matches_current_roles() {
        let policy = AuthorityConfig::default_policy();
        assert!(policy.allows("管理员", "get_verification_code"));
        assert!(policy.allows("管理员", "model.weekly.generate"));
        assert!(policy.allows("普通二级", "model.annotation.model"));
        assert!(policy.allows("普通一级", "model.log.convert"));
        assert!(!policy.allows("普通一级", "model.weekly.generate"));
        assert!(!policy.allows("普通二级", "config.edit"));
        assert!(policy.model_visible("管理员", "weekly"));
        assert!(policy.model_visible("普通二级", "offline"));
        assert!(policy.model_visible("普通一级", "log"));
        assert!(!policy.model_visible("普通一级", "weekly"));
        assert_eq!(
            policy.visible_file_names("weekly").unwrap(),
            ["old.xlsx", "result-2.xlsx", "upload-2.xlsx"]
        );
        assert_eq!(policy.log_visual_lines(), 27);
        assert_eq!(policy.rows_downloaded_file_algorithm(), 8);
    }

    #[test]
    fn repository_yaml_is_loaded() {
        let root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let policy = AuthorityConfig::load(&root);
        assert!(policy.allows("管理员", "get_verification_code"));
        assert!(!policy.allows("普通一级", "get_verification_code"));
        assert!(policy.model_visible("管理员", "annotation"));
        assert!(!policy.model_visible("普通一级", "annotation"));
        assert!(
            policy
                .visible_file_names("annotation")
                .unwrap()
                .iter()
                .any(|name| name == "数字-9-模型标注训练清单.xlsx")
        );
        assert_eq!(policy.log_visual_lines(), 19);
        assert_eq!(policy.rows_downloaded_file_algorithm(), 8);
    }

    #[test]
    fn downloaded_file_rows_default_to_eight() {
        let policy: AuthorityConfig = serde_yaml::from_str("").expect("empty config is valid");
        assert_eq!(policy.rows_downloaded_file_algorithm(), 8);
    }

    #[test]
    fn file_description_config_has_cleanup_defaults_and_filters_extensions() {
        let policy: AuthorityConfig = serde_yaml::from_str("").expect("empty config is valid");
        let config = policy.file_description();
        assert_eq!(config.page_rows(), 15);
        assert_eq!(config.stale_days(), 15);
        assert_eq!(config.recycle_bin(), ".portal_recycle");
        assert!(config.file_extension_allowed(std::path::Path::new("a.tmp")));

        let policy: AuthorityConfig = serde_yaml::from_str(
            r#"
File_Description:
  enabled: true
  rows_per_page: 20
  allowed_extensions: [.txt, zip]
  cleanup_days: 30
"#,
        )
        .unwrap();
        let config = policy.file_description();
        assert_eq!(config.page_rows(), 20);
        assert_eq!(config.stale_days(), 30);
        assert!(config.file_extension_allowed(std::path::Path::new("a.TXT")));
        assert!(config.file_extension_allowed(std::path::Path::new("a.zip")));
        assert!(!config.file_extension_allowed(std::path::Path::new("a.png")));
    }

    #[test]
    fn browser_paths_can_be_selected_per_model() {
        let policy: AuthorityConfig = serde_yaml::from_str(
            r#"
Browser_Path:
  annotation: model/annotation/chrome.exe
  weekly: model/weekly/chrome.exe
"#,
        )
        .unwrap();
        assert_eq!(
            policy
                .browser_path(std::path::Path::new("project"), "annotation")
                .unwrap(),
            std::path::PathBuf::from("project/model/annotation/chrome.exe")
        );
        assert_eq!(
            policy
                .browser_path(std::path::Path::new("project"), "weekly")
                .unwrap(),
            std::path::PathBuf::from("project/model/weekly/chrome.exe")
        );
    }

    #[test]
    fn legacy_single_browser_path_is_still_supported() {
        let policy: AuthorityConfig =
            serde_yaml::from_str(r#"Browser_Path: 'shared/chrome.exe'"#).unwrap();
        assert_eq!(
            policy
                .browser_path(std::path::Path::new("project"), "annotation")
                .unwrap(),
            std::path::PathBuf::from("project/shared/chrome.exe")
        );
    }
}
