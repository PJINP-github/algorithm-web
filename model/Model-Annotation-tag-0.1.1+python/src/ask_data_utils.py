import json
import os
import re
import platform
import shutil
from dataclasses import dataclass
from copy import deepcopy
from pathlib import Path


ASK_FIELD_SEP = "      "
PRE_LABEL_DONE = "完成"
WORK_DIR_NAME = "Work-txt"

ASK_DATA_FILE = "数字-1-标注任务明细.txt"
ASK_DATA_T2_FILE = "数字-2-二次补查标注任务.txt"
ASK_DATA_TEMP_FILE = "数字-3-待补查Task编号.txt"
MODEL_FILE = "数字-4-模型训练记录.txt"
OUTLOOK_FILE = "数字-5-训练展望原始分组.txt"
OUTLOOK_SORTED_FILE = "数字-6-训练展望最终排序.txt"
MAPPING_FILE = "数字-7-类别标准化映射规则.yaml"
MAPPING_LEGACY_FILE = "数字-7-类别标准化映射规则.txt"
DATA_CLASS_FILE = "数字-8-原始标注类别清单.txt"
CLASS_OUT_FILE = "数字-9-标准训练类别清单.txt"
H_MODE_HISTORY_FILE = "数字-0-h模式历史学习.json"


def resolve_work_dir(base_dir):
    configured = os.environ.get("RUST_PORTAL_WORK_DIR", "").strip()
    if configured:
        return Path(configured)
    return Path(base_dir).parent / WORK_DIR_NAME


def resolve_project_root(start=None):
    """Find the repository root by walking up to the directory containing Cargo.toml."""
    start_path = Path(start or __file__).resolve()
    if start_path.is_file():
        start_path = start_path.parent
    for directory in (start_path, *start_path.parents):
        if (directory / "Cargo.toml").is_file():
            return directory
    return None


def resolve_existing_work_file(work_dir, filename, base_dir=None):
    """Use the per-user work directory, then the model's shared Work-txt directory."""
    work_path = Path(work_dir) / filename
    if work_path.is_file():
        return work_path
    if base_dir is not None:
        shared_path = Path(base_dir).parent / WORK_DIR_NAME / filename
        if shared_path.is_file():
            return shared_path
    return work_path


def resolve_mapping_file(work_dir):
    """Return the preferred category mapping rule file, with txt fallback."""
    work_dir = Path(work_dir)
    preferred = work_dir / MAPPING_FILE
    if preferred.exists():
        return preferred
    legacy = work_dir / MAPPING_LEGACY_FILE
    if legacy.exists():
        return legacy
    return preferred

DEFAULT_CONFIG = {
    "collection": {
        "max_pages": 5,
    },
    "task_filter": {
        "enabled": False,
        "logic": "any",
        "rules": [],
    },
    "model": {
        "training_note": {
            "enabled": True,
            "blocked_substrings": ["测试"],
            "case_sensitive": False,
            "max_rows_to_check": 100,
        },
        "category_match": {
            "mode": "longest_substring",
            "case_sensitive": False,
        },
    },
    "summary": {
        "site_generalize_substrings": [],
    },
}


def resolve_chrome_path(base_dir):
    """
    Resolve the configured browser first, then the bundled browser path.

    The Rust portal passes RUST_PORTAL_CHROME_PATH for managed jobs. The
    authority.yaml lookup keeps standalone helper scripts on the same browser
    without requiring a duplicated command-line setting.
    """
    base_path = Path(base_dir)
    configured = os.environ.get("RUST_PORTAL_CHROME_PATH", "").strip()
    if configured and Path(configured).is_file():
        return str(Path(configured))
    if platform.system() == "Windows":
        configured_path = _resolve_authority_browser_path(base_path, "annotation")
        if configured_path is not None:
            if configured_path.is_file():
                return str(configured_path)
            raise RuntimeError(
                f"authority.yaml 中 annotation 的 Browser_Path 不存在或不是文件: "
                f"{configured_path}"
            )
        candidates = [
            base_path.parent / "chrome" / "chrome.exe",
            base_path / "chrome" / "chrome.exe",
            Path.cwd() / "chrome" / "chrome.exe",
        ]
        for path in candidates:
            if path.exists():
                return str(path)
        raise RuntimeError(
            "未找到Chrome浏览器，请将 chrome.exe 放到 "
            f"{candidates[0]}"
        )

    linux_chrome_paths = [Path("/usr/lib/chromium/chromium")] + [
        Path(found)
        for name in (
            "google-chrome-stable",
            "google-chrome",
            "chromium",
            "chromium-browser",
            "brave-browser",
            "microsoft-edge",
            "microsoft-edge-stable",
        )
        if (found := shutil.which(name))
    ] + [
        Path("/usr/bin/google-chrome-stable"),
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/chromium"),
        Path("/snap/bin/chromium"),
        Path("/usr/local/bin/google-chrome"),
        Path("/usr/bin/chromium-browser"),
    ]
    for path in linux_chrome_paths:
        if path.is_file():
            return str(path.resolve())
    raise RuntimeError("未找到Chrome/Chromium浏览器，请安装或指定路径")


def resolve_chrome_user_data_dir(base_dir, model_key="annotation"):
    """
    Resolve a project-isolated browser profile.

    Linux copies the system Chromium Default profile once, then keeps using
    the project copy so daily Chromium can remain open independently.
    """
    if platform.system() == "Linux":
        configured = os.environ.get("RUST_PORTAL_USER_DATA_DIR", "").strip()
        profile_root = (
            Path(os.path.expandvars(configured)).expanduser().resolve()
            if configured
            else Path(base_dir).resolve().parent / ".chromium_profile_linux"
        )
        if _seed_linux_chromium_profile(profile_root):
            return str(profile_root)

    configured_path = _resolve_authority_config_path(
        Path(base_dir),
        "Browser_User_Data_Dir",
        model_key,
    )
    if configured_path is not None:
        configured_path.mkdir(parents=True, exist_ok=True)
        return str(configured_path)

    configured = os.environ.get("RUST_PORTAL_USER_DATA_DIR", "").strip()
    if configured:
        path = Path(os.path.expandvars(configured)).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    path = Path(base_dir).parent / "chromium_user_data"
    path.mkdir(parents=True, exist_ok=True)
    return str(path.resolve())


def _seed_linux_chromium_profile(profile_root):
    """Copy the system Default profile once into the project profile root."""
    configured_source = os.environ.get("RUST_PORTAL_CHROMIUM_SOURCE_DIR", "").strip()
    source_root = (
        Path(os.path.expandvars(configured_source)).expanduser().resolve()
        if configured_source
        else Path.home() / ".config" / "chromium"
    )
    source_profile = source_root / "Default"
    target_profile = profile_root / "Default"
    if not source_profile.is_dir():
        return False

    try:
        profile_root.mkdir(parents=True, exist_ok=True)
        if not target_profile.exists():
            shutil.copytree(
                source_profile,
                target_profile,
                ignore=_chromium_profile_copy_ignore,
            )
        local_state = source_root / "Local State"
        target_state = profile_root / "Local State"
        if local_state.is_file() and not target_state.exists():
            shutil.copy2(local_state, target_state)
        return True
    except OSError as exc:
        print(f"复制系统 Chromium profile 失败，将使用现有 profile：{exc}")
        return target_profile.is_dir()


def _chromium_profile_copy_ignore(_directory, names):
    ignored = {
        "Cache",
        "Code Cache",
        "GPUCache",
        "ShaderCache",
        "GrShaderCache",
        "DawnCache",
        "Service Worker",
    }
    return {name for name in names if name in ignored}


def _resolve_authority_browser_path(base_path, model_key):
    return _resolve_authority_config_path(base_path, "Browser_Path", model_key)


def _resolve_authority_config_path(base_path, section_name, model_key):
    current = Path(base_path).resolve()
    for directory in (current, *current.parents):
        authority_path = directory / "authority.yaml"
        if not authority_path.is_file():
            continue
        try:
            loaded = _load_yaml_mapping(
                authority_path.read_text(encoding="utf-8"),
                authority_path,
            )
        except (OSError, ValueError):
            continue
        if not isinstance(loaded, dict):
            continue
        value = _browser_path_value(loaded.get(section_name), model_key)
        if not value:
            continue
        path = Path(os.path.expandvars(value).replace("\\", "/")).expanduser()
        if not path.is_absolute():
            project_root = resolve_project_root(authority_path.parent)
            path = (project_root or authority_path.parent) / path
        return path
    return None


def _browser_path_value(configured, model_key):
    if isinstance(configured, dict):
        aliases = {
            "annotation": (
                "annotation",
                "Model-Annotation",
                "Model_Annotation",
                "Model-Annotation-tag-0.1.1+python",
            ),
            "weekly": (
                "weekly",
                "Weekly-Report-Print",
                "Weekly_Report_Print",
                "Weekly-Report-Print-tag-0.0.8+python",
            ),
        }.get(model_key, (model_key,))
        for alias in aliases:
            value = str(configured.get(alias) or "").strip()
            if value:
                return value
        return ""
    return str(configured or "").strip()


@dataclass
class AskRecord:
    task_id: str
    task_status: str
    pre_label_status: str
    category: str
    project: str
    note: str = ""
    task_name: str = ""
    image_count: int = 0

    @property
    def task_id_int(self):
        return int(self.task_id) if self.task_id.isdigit() else -1

    @property
    def tag(self):
        return f"{self.project}&" if self.project else "&"

    @property
    def flow_text(self):
        return " ".join(part for part in (self.category, self.tag) if part)

    def to_line(self):
        fields = [
            f"Task {self.task_id}",
            self.task_status,
            self.pre_label_status,
            self.category,
            self.tag,
        ]
        if self.note:
            fields.append(self.note)
        fields.append(f"图片数量: {self.image_count}")
        return ASK_FIELD_SEP.join(fields)


def normalize_text(value):
    return " ".join((value or "").split())


def normalize_image_count(value):
    if isinstance(value, int):
        return max(value, 0)
    match = re.search(r"\d[\d,]*", str(value or ""))
    if not match:
        return 0
    try:
        return max(int(match.group().replace(",", "")), 0)
    except ValueError:
        return 0


def _merge_config(base, override):
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_config(base[key], value)
        else:
            base[key] = value
    return base


def load_config(config_path=None):
    """Load config.yaml and merge it onto the built-in defaults."""
    if config_path is None:
        config_path = Path(__file__).resolve().parents[1] / "config.yaml"
    else:
        config_path = Path(config_path)

    config = deepcopy(DEFAULT_CONFIG)
    if not config_path.exists():
        return config

    with config_path.open("r", encoding="utf-8") as config_file:
        loaded = _load_yaml_mapping(config_file.read(), config_path)
    if not isinstance(loaded, dict):
        raise ValueError(f"配置文件顶层必须是对象: {config_path}")
    return _merge_config(config, loaded)


def _load_yaml_mapping(text, config_path):
    try:
        import yaml
        return yaml.safe_load(text) or {}
    except ImportError:
        return _parse_simple_yaml(text, config_path)


def _parse_scalar(value):
    value = value.strip()
    if value in ("", "null", "Null", "NULL", "~"):
        return None
    if value in ("true", "True", "TRUE"):
        return True
    if value in ("false", "False", "FALSE"):
        return False
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in ("'", '"')
    ):
        return value[1:-1]
    return value


def _strip_yaml_comment(line):
    in_quote = None
    for index, char in enumerate(line):
        if char in ("'", '"'):
            if in_quote == char:
                in_quote = None
            elif in_quote is None:
                in_quote = char
        elif char == "#" and in_quote is None:
            return line[:index]
    return line


def _next_significant_yaml_line(lines, start_index):
    for index in range(start_index, len(lines)):
        raw = _strip_yaml_comment(lines[index]).rstrip()
        if raw.strip():
            return raw
    return ""


def _parse_simple_yaml(text, config_path):
    """
    Parse the small config.yaml subset used by this project.

    This keeps packaged runs working when PyYAML is not installed. It supports
    nested mappings, lists of scalars, and lists of mapping items.
    """
    root = {}
    stack = [(-1, root)]
    lines = text.splitlines()

    for index, original in enumerate(lines):
        line = _strip_yaml_comment(original).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise ValueError(f"配置文件缩进不正确: {config_path}:{index + 1}")
        parent = stack[-1][1]

        if stripped.startswith("- "):
            if not isinstance(parent, list):
                raise ValueError(f"配置文件列表位置不正确: {config_path}:{index + 1}")
            item_text = stripped[2:].strip()
            if not item_text:
                item = {}
                parent.append(item)
                stack.append((indent, item))
            elif ":" in item_text:
                key, value = item_text.split(":", 1)
                item = {}
                parent.append(item)
                key = key.strip()
                value = value.strip()
                if value:
                    item[key] = _parse_scalar(value)
                else:
                    nested = [] if _next_significant_yaml_line(lines, index + 1).strip().startswith("- ") else {}
                    item[key] = nested
                stack.append((indent, item))
                if not value:
                    stack.append((indent + 2, nested))
            else:
                parent.append(_parse_scalar(item_text))
            continue

        if ":" not in stripped:
            raise ValueError(f"配置文件行格式不正确: {config_path}:{index + 1}")
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not isinstance(parent, dict):
            raise ValueError(f"配置文件映射位置不正确: {config_path}:{index + 1}")
        if value:
            parent[key] = _parse_scalar(value)
        else:
            nested = [] if _next_significant_yaml_line(lines, index + 1).strip().startswith("- ") else {}
            parent[key] = nested
            stack.append((indent, nested))

    return root


def _mapping_targets_from_value(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _mapping_entries_from_mapping_node(node):
    entries = []
    if isinstance(node, dict):
        for source, value in node.items():
            targets = []
            if isinstance(value, dict):
                targets = _mapping_targets_from_value(
                    value.get("targets", value.get("target", value.get("to")))
                )
            else:
                targets = _mapping_targets_from_value(value)
            for target in targets:
                entries.append((len(entries) + 1, str(source).strip(), target))
        return entries

    if isinstance(node, list):
        for item in node:
            if isinstance(item, str):
                if "-" not in item:
                    continue
                source, target = item.split("-", 1)
                targets = [target.strip()]
                sources = [source.strip()]
            elif isinstance(item, dict):
                sources = _mapping_targets_from_value(
                    item.get(
                        "sources",
                        item.get("source", item.get("match", item.get("pattern"))),
                    )
                )
                targets = _mapping_targets_from_value(
                    item.get("targets", item.get("target", item.get("to")))
                )
            else:
                continue
            for source in sources:
                for target in targets:
                    if source and target:
                        entries.append((len(entries) + 1, source, target))
        return entries

    return entries


def _mapping_entries_from_yaml(text, mapping_path):
    data = _load_yaml_mapping(text, mapping_path)
    if not isinstance(data, dict):
        return []
    node = (
        data.get("rules")
        or data.get("mappings")
        or data.get("mapping")
        or data.get("category_mapping")
        or data
    )
    return _mapping_entries_from_mapping_node(node)


def _mapping_association_from_value(value):
    if isinstance(value, bool):
        return "strong" if value else "reusable"
    text = str(value or "").strip().casefold()
    if text in ("strong", "exclusive", "unique", "only", "强关联", "唯一", "唯一归属"):
        return "strong"
    return "reusable"


def _mapping_item_association(item):
    if not isinstance(item, dict):
        return "reusable"
    for key in ("association", "relation", "mode", "match_type"):
        if key in item:
            return _mapping_association_from_value(item.get(key))
    for key in ("strong", "exclusive", "unique"):
        if key in item:
            return _mapping_association_from_value(item.get(key))
    return "reusable"


def _mapping_sources_from_item(item):
    if not isinstance(item, dict):
        return []
    return _mapping_targets_from_value(
        item.get(
            "matches",
            item.get(
                "match",
                item.get(
                    "sources",
                    item.get("source", item.get("pattern")),
                ),
            ),
        )
    )


def _mapping_task_sources_from_item(item, default_sources):
    if not isinstance(item, dict):
        return default_sources
    value = item.get(
        "task_matches",
        item.get("task_match", item.get("task_sources", item.get("task_source"))),
    )
    sources = _mapping_targets_from_value(value)
    return sources or default_sources


def _mapping_model_target_from_item(item, target):
    if not isinstance(item, dict):
        return target
    value = item.get(
        "model_match",
        item.get("model", item.get("model_target", item.get("model_keyword"))),
    )
    targets = _mapping_targets_from_value(value)
    return targets[0] if targets else target


def _mapping_remove_sources_from_item(item):
    if not isinstance(item, dict):
        return []
    return _mapping_targets_from_value(
        item.get(
            "remove",
            item.get("removes", item.get("exclude_matches", item.get("exclude_match"))),
        )
    )


def _mapping_rule_items_from_mapping_node(node):
    items = []

    def append_rule(source, target, association, model_target=None, remove_sources=None):
        source = str(source).strip()
        target = str(target).strip()
        if source and target:
            items.append(
                {
                    "lineno": len(items) + 1,
                    "source": source,
                    "task_source": source,
                    "target": target,
                    "model_target": str(model_target or target).strip(),
                    "association": association,
                    "remove_sources": list(remove_sources or []),
                }
            )

    if isinstance(node, dict):
        for source, value in node.items():
            association = _mapping_item_association(value)
            if isinstance(value, dict):
                targets = _mapping_targets_from_value(
                    value.get("targets", value.get("target", value.get("to")))
                )
            else:
                targets = _mapping_targets_from_value(value)
            for target in targets:
                append_rule(source, target, association)
        return items

    if isinstance(node, list):
        for item in node:
            association = _mapping_item_association(item)
            if isinstance(item, str):
                if "-" not in item:
                    continue
                source, target = item.split("-", 1)
                sources = [source]
                task_sources = sources
                targets = [target]
                model_target = target
            elif isinstance(item, dict):
                sources = _mapping_sources_from_item(item)
                task_sources = _mapping_task_sources_from_item(item, sources)
                remove_sources = _mapping_remove_sources_from_item(item)
                targets = _mapping_targets_from_value(
                    item.get("targets", item.get("target", item.get("to")))
                )
                model_target = None
            else:
                continue
            for source in task_sources:
                for target in targets:
                    append_rule(
                        source,
                        target,
                        association,
                        model_target=_mapping_model_target_from_item(item, target),
                        remove_sources=remove_sources,
                    )
        return items

    return items


def _mapping_rule_items_from_yaml(text, mapping_path):
    data = _load_yaml_mapping(text, mapping_path)
    if not isinstance(data, dict):
        return []
    node = (
        data.get("rules")
        or data.get("mappings")
        or data.get("mapping")
        or data.get("category_mapping")
        or data
    )
    return _mapping_rule_items_from_mapping_node(node)


def _mapping_exclude_substrings_from_yaml(text, mapping_path):
    data = _load_yaml_mapping(text, mapping_path)
    if not isinstance(data, dict):
        return []
    values = (
        data.get("exclude_substrings")
        or data.get("global_exclude_substrings")
        or data.get("exclude")
        or data.get("excludes")
        or []
    )
    return _mapping_targets_from_value(values)


def _mapping_entries_from_legacy_text(text):
    entries = []
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#") or "-" not in line:
            continue
        source, target = line.split("-", 1)
        source = source.strip()
        target = target.strip()
        if source and target:
            entries.append((lineno, source, target))
    return entries


def _mapping_rule_items_from_legacy_text(text):
    items = []
    for lineno, source, target in _mapping_entries_from_legacy_text(text):
        items.append(
            {
                "lineno": lineno,
                "source": source,
                "target": target,
                "association": "reusable",
            }
        )
    return items


def load_category_mapping_rule_items(mapping_path):
    """Load category rules with association metadata preserved."""
    mapping_path = Path(mapping_path)
    if not mapping_path.exists() and mapping_path.name == MAPPING_FILE:
        legacy_path = mapping_path.with_name(MAPPING_LEGACY_FILE)
        if legacy_path.exists():
            mapping_path = legacy_path
    if not mapping_path.exists():
        return []

    text = mapping_path.read_text(encoding="utf-8", errors="replace")
    if mapping_path.suffix.lower() in (".yaml", ".yml"):
        return _mapping_rule_items_from_yaml(text, mapping_path)
    stripped = text.lstrip()
    if stripped.startswith(("rules:", "mappings:", "mapping:", "category_mapping:")):
        return _mapping_rule_items_from_yaml(text, mapping_path)
    return _mapping_rule_items_from_legacy_text(text)


def load_category_mapping_exclude_substrings(mapping_path):
    """Load global full-line exclusion substrings from yaml mapping rules."""
    mapping_path = Path(mapping_path)
    if not mapping_path.exists() and mapping_path.name == MAPPING_FILE:
        legacy_path = mapping_path.with_name(MAPPING_LEGACY_FILE)
        if legacy_path.exists():
            mapping_path = legacy_path
    if not mapping_path.exists():
        return []

    text = mapping_path.read_text(encoding="utf-8", errors="replace")
    if mapping_path.suffix.lower() in (".yaml", ".yml"):
        return _mapping_exclude_substrings_from_yaml(text, mapping_path)
    stripped = text.lstrip()
    if stripped.startswith(("rules:", "mappings:", "mapping:", "category_mapping:")):
        return _mapping_exclude_substrings_from_yaml(text, mapping_path)
    return []


def load_category_mapping_rules(mapping_path):
    """
    Load category standardization rules.

    YAML is preferred and supports duplicate sources / multiple targets. The
    legacy one-rule-per-line txt format remains readable for existing folders.
    """
    return [
        (item["lineno"], item["source"], item["target"])
        for item in load_category_mapping_rule_items(mapping_path)
    ]


def _item_matches_value(value, item, match_config=None, field="source"):
    match_config = match_config or {}
    pattern = item.get(field) or item.get("source", "")
    if not pattern:
        return False
    mode = match_config.get("mode", "longest_substring")
    case_sensitive = match_config.get("case_sensitive", False)
    return _configured_text_match(
        value,
        pattern,
        mode="exact" if mode == "exact" else ("regex" if mode == "regex" else "contains"),
        case_sensitive=case_sensitive,
    )


def match_category_mapping_items(
    value,
    mapping_items,
    match_config=None,
    field="task_source",
    reusable_all=False,
):
    """
    Match mapping items with association semantics.

    Strong matches preempt reusable matches. Reusable task matching can keep all
    hits so one Task can flow into multiple target groups.
    """
    match_config = match_config or {}
    matched = [
        item
        for item in (mapping_items or [])
        if _item_matches_value(value, item, match_config, field=field)
        and not any(remove and remove in value for remove in item.get("remove_sources", []))
    ]
    if not matched:
        return []

    strong_matches = [
        item for item in matched
        if item.get("association", "reusable") == "strong"
    ]
    candidates = strong_matches or matched
    if (
        match_config.get("mode", "longest_substring") == "longest_substring"
        and (strong_matches or not reusable_all)
    ):
        max_length = max(len(item.get(field) or item.get("source", "")) for item in candidates)
        candidates = [
            item for item in candidates
            if len(item.get(field) or item.get("source", "")) == max_length
        ]

    results = []
    seen = set()
    for item in candidates:
        key = (
            item.get(field) or item.get("source", ""),
            item.get("target", ""),
            item.get("model_target", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        results.append(item)
    return results


def load_h_mode_history(history_path):
    """Load h-mode task decisions without making callers duplicate JSON parsing."""
    history_path = Path(history_path)
    if not history_path.exists():
        return []
    try:
        data = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    items = data.get("items", [])
    return items if isinstance(items, list) else []


def history_assignment_for_task(task_id, history_items, current_line=None):
    """
    Return explicit h-mode ownership for one Task.

    Accepted strong assignments are exclusive and suppress substring matches.
    Accepted reusable assignments are retained but allow other normal matches.
    A rejected decision is only applied to the same serialized line when the
    caller provides current_line, preserving h-mode's changed-line behavior.
    """
    task_id = str(task_id)
    latest_by_target = {}
    for item in history_items or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("task_id", "")) != task_id:
            continue
        target = str(item.get("target", "")).strip()
        if not target:
            continue
        decision = str(item.get("decision", "")).strip().casefold()
        if decision == "rejected" and current_line is not None:
            recorded_line = str(item.get("line", ""))
            if recorded_line and recorded_line != str(current_line):
                continue
        if decision in ("accepted", "rejected"):
            latest_by_target[target] = item

    accepted_items = [
        item for item in latest_by_target.values()
        if str(item.get("decision", "")).strip().casefold() == "accepted"
    ]
    rejected_targets = {
        str(item.get("target", "")).strip()
        for item in latest_by_target.values()
        if str(item.get("decision", "")).strip().casefold() == "rejected"
    }
    strong_items = [
        item for item in accepted_items
        if _mapping_association_from_value(item.get("association")) == "strong"
    ]
    accepted_targets = {
        str(item.get("target", "")).strip()
        for item in (strong_items or accepted_items)
        if str(item.get("target", "")).strip()
    }
    return {
        "accepted": accepted_targets,
        "rejected": rejected_targets,
        "exclusive": bool(strong_items),
        "has_instruction": bool(accepted_items or rejected_targets),
    }


def history_priority_targets(
    task_id,
    candidate_targets,
    history_items,
    current_line=None,
):
    """Apply explicit h-mode ownership before ordinary substring results."""
    assignment = history_assignment_for_task(
        task_id,
        history_items,
        current_line=current_line,
    )
    candidates = list(candidate_targets or [])
    if not assignment["has_instruction"]:
        return list(dict.fromkeys(candidates)), assignment

    if assignment["exclusive"]:
        return sorted(assignment["accepted"]), assignment

    targets = list(assignment["accepted"])
    targets.extend(
        target
        for target in candidates
        if target not in assignment["rejected"]
    )
    return list(dict.fromkeys(targets)), assignment


def mapping_rules_to_first_target_mapping(rules):
    mapping = {}
    for _lineno, source, target in rules:
        if source and source not in mapping:
            mapping[source] = target
    return mapping


def _configured_text_match(value, pattern, mode="contains", case_sensitive=False):
    value = "" if value is None else str(value)
    pattern = "" if pattern is None else str(pattern)
    if not case_sensitive:
        value = value.casefold()
        pattern = pattern.casefold()
    if not pattern:
        return False
    if mode == "exact":
        return value == pattern
    if mode == "regex":
        flags = 0 if case_sensitive else re.IGNORECASE
        return re.search(pattern, str(value), flags) is not None
    return pattern in value


def text_matches_any(value, patterns, mode="contains", case_sensitive=False):
    if isinstance(patterns, str):
        patterns = [patterns]
    return any(
        _configured_text_match(
            value,
            pattern,
            mode=mode,
            case_sensitive=case_sensitive,
        )
        for pattern in (patterns or [])
    )


def training_note_is_blocked(note, config):
    note_config = ((config or {}).get("model") or {}).get("training_note", {})
    if not note_config.get("enabled", True):
        return False
    return text_matches_any(
        note,
        note_config.get("blocked_substrings", []),
        mode=note_config.get("mode", "contains"),
        case_sensitive=note_config.get("case_sensitive", False),
    )


def training_note_max_rows(config):
    """读取训练记录扫描上限，优先使用顶层 model.training_note_max_rows。"""
    try:
        model_config = (config or {}).get("model", {})
        note_config = model_config.get("training_note", {})
        value = note_config.get(
            "max_rows_to_check",
            None,
        )
        value = model_config.get("training_note_max_rows", value)
        if value is None:
            value = 3
        value = int(value)
    except (TypeError, ValueError):
        value = 3
    return max(value, 1)


def extract_model_training_note(model_line):
    """从模型训练记录中按字段标记提取完整训练备注。"""
    text = normalize_text(model_line)
    match = re.search(
        r"(?:^|\s)备注\s+(.*?)(?=\s+训练开始时间\s+|\s+开始时间\s+|\s+完整Task:|$)",
        text,
    )
    if match:
        return match.group(1).strip()
    return ""


def model_line_matches_keyword(model_line, keyword, case_sensitive=False):
    """判断模型训练记录的完整训练备注是否命中指定关键词。"""
    note = extract_model_training_note(model_line)
    if case_sensitive:
        return str(keyword or "") in note
    return str(keyword or "").casefold() in note.casefold()


def record_field_value(record, column):
    """Return a configurable task-table field from an AskRecord."""
    aliases = {
        "task_id": "task_id",
        "Task ID": "task_id",
        "任务状态": "task_status",
        "task_status": "task_status",
        "预标注状态": "pre_label_status",
        "pre_label_status": "pre_label_status",
        "标注说明": "note",
        "note": "note",
        "类型": "category",
        "类别": "category",
        "category": "category",
        "项目": "project",
        "项目名称": "project",
        "project": "project",
        "标注任务名称": "task_name",
        "task_name": "task_name",
        "图片数量": "image_count",
        "image_count": "image_count",
    }
    field = aliases.get(column, column)
    if field == "task_name":
        return " ".join(part for part in (record.project, record.category) if part)
    return getattr(record, field, "")


def record_matches_task_filters(record, config):
    """Whether a task row should be excluded by configured column rules."""
    task_filter = (config or {}).get("task_filter", {})
    if not task_filter.get("enabled", False):
        return False

    rules = task_filter.get("rules", [])
    if not rules:
        return False
    if isinstance(rules, dict):
        rules = [rules]

    matched_rules = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        values = rule.get("contains", rule.get("values", []))
        if isinstance(values, str):
            values = [values]
        if not values:
            continue
        field_value = record_field_value(record, rule.get("column", "note"))
        mode = rule.get("mode", "contains")
        case_sensitive = rule.get(
            "case_sensitive",
            task_filter.get("case_sensitive", False),
        )
        matched_rules.append(
            any(
                _configured_text_match(
                    field_value,
                    value,
                    mode=mode,
                    case_sensitive=case_sensitive,
                )
                for value in values
            )
        )

    if not matched_rules:
        return False
    return all(matched_rules) if task_filter.get("logic", "any") == "all" else any(matched_rules)


def _normalized_mapping_rules(mapping):
    if isinstance(mapping, dict):
        rules = []
        for index, (pattern, output) in enumerate(mapping.items(), 1):
            for target in _mapping_targets_from_value(output):
                rules.append((index, str(pattern).strip(), target))
        return rules

    rules = []
    for index, item in enumerate(mapping or [], 1):
        if len(item) == 3:
            lineno, pattern, output = item
        elif len(item) == 2:
            pattern, output = item
            lineno = index
        else:
            continue
        pattern = str(pattern).strip()
        output = str(output).strip()
        if pattern and output:
            rules.append((lineno, pattern, output))
    return rules


def find_text_mappings(value, mapping, match_config=None):
    """Find all best mapping outputs according to the configured semantics."""
    match_config = match_config or {}
    mode = match_config.get("mode", "longest_substring")
    case_sensitive = match_config.get("case_sensitive", False)
    matches = []
    for index, (_lineno, pattern, output) in enumerate(_normalized_mapping_rules(mapping)):
        if _configured_text_match(
            value,
            pattern,
            mode="exact" if mode == "exact" else ("regex" if mode == "regex" else "contains"),
            case_sensitive=case_sensitive,
        ):
            matches.append((index, pattern, output))

    if not matches:
        return []
    if mode == "longest_substring":
        max_length = max(len(pattern) for _index, pattern, _output in matches)
        matches = [item for item in matches if len(item[1]) == max_length]
    elif mode in ("contains", "exact", "regex"):
        matches = matches[:1]

    results = []
    seen = set()
    for _index, pattern, output in matches:
        key = (pattern, output)
        if key in seen:
            continue
        seen.add(key)
        results.append((pattern, output))
    return results


def find_text_mapping(value, mapping, match_config=None):
    """Find the first best mapping entry according to configurable semantics."""
    matches = find_text_mappings(value, mapping, match_config)
    return matches[0] if matches else None


def extract_task_dataset_ids(content):
    """Return numeric task ids from a training dataset text."""
    return [
        match.group(1)
        for match in re.finditer(
            r"(?:autoedge|cvat)@(\d+)",
            content or "",
            flags=re.IGNORECASE,
        )
    ]


def latest_task_summary(content, limit=3):
    ids = [int(task_id) for task_id in extract_task_dataset_ids(content)]
    return " ".join(str(task_id) for task_id in sorted(ids, reverse=True)[:limit])


def split_task_name(name):
    parts = [p.strip() for p in (name or "").split("/") if p.strip()]
    if len(parts) >= 2:
        return parts[1], parts[0]
    if parts:
        return parts[0], ""
    return "", ""


def build_record(
    task_name,
    task_id,
    task_status,
    pre_label_status,
    note,
    image_count=0,
):
    category, project = split_task_name(task_name)
    task_name = normalize_text(task_name)
    return AskRecord(
        task_id=normalize_text(task_id),
        task_status=normalize_text(task_status),
        pre_label_status=normalize_text(pre_label_status),
        category=normalize_text(category),
        project=normalize_text(project),
        note=normalize_text(note),
        task_name=task_name,
        image_count=normalize_image_count(image_count),
    )


def parse_ask_line(line):
    raw = line.strip()
    if not raw or not raw.startswith("Task "):
        return None

    fields = [field.strip() for field in re.split(r"\s{2,}", raw) if field.strip()]
    if len(fields) >= 5:
        task_parts = fields[0].split()
        if len(task_parts) >= 2 and task_parts[1].isdigit():
            project = fields[4].rstrip("&")
            extra_fields = fields[5:]
            image_count = 0
            if extra_fields:
                image_match = re.fullmatch(
                    r"图片数量\s*[:：]\s*(.+)",
                    extra_fields[-1],
                )
                if image_match:
                    image_count = normalize_image_count(image_match.group(1))
                    extra_fields = extra_fields[:-1]
            return AskRecord(
                task_id=task_parts[1],
                task_status=fields[1],
                pre_label_status=fields[2],
                category=fields[3],
                project=project,
                note=ASK_FIELD_SEP.join(extra_fields),
                task_name=f"{project}/{fields[3]}" if project else fields[3],
                image_count=image_count,
            )

    # Backward compatibility for old lines:
    # Task 5803      进行中      连续表计 中广核安徽&
    parts = raw.split()
    if len(parts) >= 5 and parts[0] == "Task" and parts[1].isdigit():
        project = parts[-1].rstrip("&")
        category = " ".join(parts[3:-1])
        return AskRecord(
            task_id=parts[1],
            task_status=parts[2],
            pre_label_status=PRE_LABEL_DONE,
            category=category,
            project=project,
            note="",
            task_name=f"{project}/{category}" if project else category,
        )
    return None


def parse_ask_lines(lines):
    records = []
    for line in lines:
        record = parse_ask_line(line)
        if record:
            records.append(record)
    return records


def text_exclude_reason(value, exclude_substrings):
    """Return the first configured global exclusion substring found in value."""
    text = str(value or "")
    for substring in exclude_substrings or []:
        substring = str(substring or "")
        if substring and substring in text:
            return substring
    return ""


def record_exclude_reason(record, exclude_substrings):
    """Check every meaningful Task field, including its serialized source line."""
    values = (
        getattr(record, "to_line", lambda: "")(),
        getattr(record, "task_name", ""),
        getattr(record, "category", ""),
        getattr(record, "project", ""),
        getattr(record, "note", ""),
    )
    for value in values:
        reason = text_exclude_reason(value, exclude_substrings)
        if reason:
            return reason
    return ""


TASK_REF_RE = re.compile(r"\b(?:autoedge|cvat)@(\d+)\b", re.IGNORECASE)


def excluded_task_ids(records, exclude_substrings):
    return {
        str(record.task_id)
        for record in records
        if record_exclude_reason(record, exclude_substrings)
        and str(record.task_id).isdigit()
    }


def filter_task_refs(refs, excluded_ids=None):
    excluded_ids = {str(task_id) for task_id in (excluded_ids or set())}
    return [
        ref
        for ref in refs
        if not (
            TASK_REF_RE.fullmatch(str(ref).strip())
            and TASK_REF_RE.fullmatch(str(ref).strip()).group(1) in excluded_ids
        )
    ]


def remove_excluded_task_refs(value, excluded_ids=None):
    excluded_ids = {str(task_id) for task_id in (excluded_ids or set())}

    def replace(match):
        return "" if match.group(1) in excluded_ids else match.group(0)

    return TASK_REF_RE.sub(replace, str(value or ""))


def sort_unique_records(records):
    unique = {}
    for record in records:
        if not record.task_id.isdigit():
            continue
        current = unique.get(record.task_id)
        if current is None or _record_rank(record) > _record_rank(current):
            unique[record.task_id] = record
    return sorted(unique.values(), key=lambda item: item.task_id_int, reverse=True)


def _record_rank(record):
    return (
        1 if record.task_status == "已完成" else 0,
        1 if record.pre_label_status == PRE_LABEL_DONE else 0,
        1 if record.note else 0,
    )


def flow_records(records, config=None, exclude_substrings=None):
    """Return completed records that are not excluded by configuration."""
    return [
        record
        for record in records
        if record.task_status == "已完成"
        and not record_matches_task_filters(record, config or {})
        and not record_exclude_reason(record, exclude_substrings)
    ]


def missing_task_ids(records, ignored_ids=None):
    ids = sorted({record.task_id_int for record in records if record.task_id_int > 0})
    ids.extend(
        int(task_id)
        for task_id in (ignored_ids or [])
        if str(task_id).isdigit() and int(task_id) > 0
    )
    ids = sorted(set(ids))
    if not ids:
        return []
    present = set(ids)
    return [str(task_id) for task_id in range(ids[0], ids[-1] + 1) if task_id not in present]


def prioritize_missing_task_ids(missing_ids, present_ids, threshold=15):
    """
    根据相邻 Task ID 筛选补充候选。

    缺失数量不超过 threshold 时全部保留；超过 threshold 时，只保留
    上下至少一个 ID 已存在的缺失项。上下两个 ID 都存在的项优先。
    返回 (候选 ID 列表, 是否没有合格候选)。
    """
    normalized_missing = sorted(
        {int(task_id) for task_id in missing_ids if str(task_id).isdigit()}
    )
    present = {
        int(task_id) for task_id in present_ids if str(task_id).isdigit()
    }
    ranked = []
    for task_id in normalized_missing:
        has_previous = task_id - 1 in present
        has_next = task_id + 1 in present
        rank = 0 if has_previous and has_next else 1
        if has_previous or has_next:
            ranked.append((rank, task_id))

    if len(normalized_missing) <= threshold:
        # 数量较少时全部搜索，但仍优先检查上下两个 ID 均存在的项。
        candidate_ids = [
            str(task_id)
            for _, task_id in sorted(
                [(0 if task_id - 1 in present and task_id + 1 in present else 1, task_id)
                 for task_id in normalized_missing]
            )
        ]
        return candidate_ids, False

    ranked.sort()
    candidate_ids = [str(task_id) for _, task_id in ranked]
    return candidate_ids, not candidate_ids


def category_key(record):
    return record.category or "unknown"
