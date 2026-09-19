# 18.algorithm-web

一个基于 Rust + Axum 的本地业务工作区网页。Rust 负责登录、权限、上传、任务管理、日志和结果展示；各业务模型保留在 `model/` 下，由 Rust 按模型 ID 启动对应的 Python 或 Rust 处理逻辑。

## Agent 快速入口

首次接手只按以下顺序阅读：

1. `Document/AGENTS.md`：本项目协作和编辑规则。
2. `README.md`：系统边界、入口和扩展方式。
3. `Document/VERSION`：当前迭代说明。
4. `src/routes.rs`、`src/handlers/workspace.rs`：网页路由、模型标签页、任务分发。
5. 要修改哪个业务，再只读对应的 `model/<业务目录>/README.md`、配置和脚本。

不要首先扫描 `target/`、`.git/`、`.cache/`、`.portal_tmp/`、`.runtime-cache/`、`__pycache__/`、`images/`、`portal_outputs/`、`sources/temp/` 和模型权重；这些是编译产物、缓存或运行数据，不是系统结构。

## 系统流程

```text
浏览器
  -> Rust Axum 路由
  -> 登录/权限校验
  -> 上传文件或提交模型动作
  -> JobInfo 任务表 + 后台任务
  -> Rust 原生处理，或启动对应 Python 脚本
  -> 用户/任务隔离的输出目录
  -> 浏览器轮询 /api/jobs/{id}，显示日志和结果
```

前端没有独立构建工程。HTML、CSS、JavaScript 位于 `src/views/`，由 Rust 使用 `include_str!` 编译进服务并通过路由返回。

### Rust 重要文件

| 文件 | 职责 |
| --- | --- |
| `src/main.rs` | 解析 `RUST_PORTAL_PROJECT_ROOT`、`DB_PATH`、`PORT`，初始化状态并启动服务。 |
| `src/routes.rs` | 注册页面、静态资源和全部 `/api/*` 路由。 |
| `src/state.rs` | 共享数据库、会话、任务、取消控制、权限和项目根目录。 |
| `src/db.rs` | SQLite 表初始化、管理员配置和数据库调用封装。 |
| `src/authority.rs` | 读取 `model/authority.yaml`，提供标签页、按钮、文件和配置权限。 |
| `src/models.rs` | HTTP 请求/响应、工作区模型、任务和文件数据结构。 |
| `src/handlers/auth.rs` | 注册、登录、退出和 Bearer 会话。 |
| `src/handlers/workspace.rs` | 五个业务标签页、上传路径、任务执行、模型调用和结果文件。 |
| `src/log_converter.rs` | Rust 原生日志 TXT -> XLSX 转换。 |
| `src/views/workspace/workspace.html` | 工作区页面骨架和侧栏。 |
| `src/views/workspace/workspace.js` | 标签页状态、上传、模型选择、推理、标定框和任务轮询。 |
| `src/views/workspace/workspace.css` | 工作区布局和控件样式。 |

当前工作区模型 ID 在 `src/handlers/workspace.rs` 中固定为：

| ID | 目录 | 主要功能 |
| --- | --- | --- |
| `log` | `model/Log_Review-tag-0.0.1+python` | 日志转换、表计调试。 |
| `annotation` | `model/Model-Annotation-tag-0.1.1+python` | 标注任务、模型标注和训练清单。 |
| `offline` | `model/Offline_Package_Organization-tag-0.0.12+python` | 离线包审核、整理和导出。 |
| `weekly` | `model/Weekly-Report-Print-tag-0.0.8+python` | 数据采集、周报和待办。 |
| `trueno` | `model/trueno3_src-master-0.0.10` | 单图 YOLO/OCR 模型推理。 |

五个固定标签页仍由 Rust/JavaScript 保持现有实现。新增模型优先使用下面的通用协议，只需新增模型目录、实现 `portal_entry.py` 并修改 `model/authority.yaml`；只有需要特殊网页交互或特殊后端生命周期时，才扩展固定代码。

### 通用模型标签页

`authority.yaml` 的 `Tab_content` 支持直接声明新标签页、文件/文件夹导入、动作按钮、参数控件、参数组合、日志开关和项目级最新文件显示。通用模型目录必须提供：

```text
model/<业务目录>/
  portal_entry.py
  config.yaml
  业务代码和资源
```

Rust 会按当前用户和任务创建隔离的 `portal_inputs/<用户>/`、`portal_outputs/<用户>/<任务>/` 与 `.portal_tmp/<用户>/<任务>/`，并固定执行：

```text
python portal_entry.py --request-file <absolute-request-file>
```

入口协议、完整 YAML 示例和新模型接入步骤见 [`Document/Structural_Template.md`](Document/Structural_Template.md)。通用组件当前支持 `file`、`folder` 导入，以及 `select`、`multi_select`、`text`、`number`、`checkbox`、`file`、`folder` 参数控件；参数组合可使用参数 ID 或按顺序使用 `1`、`2`、`3` 等编号。

## model 目录约定

每个一级目录是一个相对独立的业务模块。业务脚本、依赖说明、配置、模型权重和运行数据应留在自己的目录内，禁止依赖其他模型目录的虚拟环境、临时目录或绝对路径。

典型运行目录：

```text
model/<业务>/
  脚本和 README
  config*.yaml              业务参数
  portal_inputs/<用户>/     用户输入
  portal_outputs/<用户>/<任务>/ 任务输出
  .portal_tmp/<用户>/<任务>/    临时文件
```

## Trueno 推理结构

Trueno 是当前网页“模型推理”子标签页。Rust 不直接写死类别和推理阶段，网页目录和调用链以以下文件为准：

```text
model/trueno3_src-master-0.0.10/
  portal_bridge.py                 Rust 与 Python 推理桥接入口
  local_inference/engine.py        描述文件驱动的本地推理引擎
  storage/model_descriptor.yaml    能力、流水线、类别和参数的唯一配置入口
  model/                           原生模型权重
  src/base/                        基础模型执行器
  src/algorithm/                   业务后处理和多阶段 patch
  images/<用户>/                   网页导入图片
  sources/temp/<用户>/             用户导入模型
  portal_outputs/<用户>/<任务>/    推理图和 JSON 结果
```

一次推理的调用链：

```text
workspace.js
  -> /api/model/run
  -> Rust execute_trueno_infer()
  -> python portal_bridge.py
  -> LocalInferenceEngine.infer()
  -> model_descriptor.yaml 的 ability.pipeline
  -> base + model param + algorithm patch
  -> detections/categories/annotated_image
```

推理链路按钮有两种模式：

- `local`：使用 `local_inference/engine.py` 的本地 YOLO/OCR 适配器。
- `src`：按描述文件加载 `src/base` 或 `src/algorithm/base` 的 base，再绑定 `src/algorithm/<patch>` 的 `patch_*` 函数，复用原多阶段代码。

`src` 模块、依赖、模型或执行过程不可用时，日志会记录具体原因，然后自动回退到 `local`。模型类别显示使用 `select_cls`，最终中文结果使用 `trans_cls`。网页标注使用“标定框”多边形；后端按检测框中心过滤，并把多边形传给原算法的 `on_intersection_output`。历史请求中的 `roi` 字段仅作为兼容别名保留。

### 新增 Trueno 能力或模型

1. 在 `storage/model_descriptor.yaml` 的 `abilities` 增加能力，配置 `desc`、`pipeline`、`select_cls`、`trans_cls` 和阶段参数。
2. `pipeline` 的 `base`、`param`、`patch` 必须对应实际的 base 模块、权重文件和 `src/algorithm` 目录。
3. YOLO/OCR 权重放入 `model/`；用户导入权重由网页放入当前用户的 `sources/temp/<用户>/`。
4. `base` 应提供 `create_base_core_object`；有 patch 时，算法目录提供可绑定的 `patch_*` 函数。
5. 只要能力已在描述文件中，网页下拉框通常会自动读取；需要改变显示顺序或权限时再修改 `model/authority.yaml`。
6. 用 `portal_bridge.py --catalog` 检查目录和模型匹配，再做真实图片推理。

不要为了新增一个 Trueno 类别去修改 Rust 中的类别列表；Rust 只负责权限、任务和桥接。

## YAML 配置索引

| 配置文件或目录 | 作用 | 修改时机 |
| --- | --- | --- |
| `model/authority.yaml` | 全局权限、标签页可见性、按钮权限、浏览器路径、文件白名单、日志行数、依赖区和文件说明区。 | 增加页面、按钮权限、展示文件或管理员配置时。 |
| `model/trueno3_src-master-0.0.10/storage/model_descriptor.yaml` | Trueno 能力定义、pipeline、base、模型参数、类别筛选、中文翻译、阈值和多阶段关系；当前网页推理的权威配置。 | 新增/调整 Trueno 能力、类别、模型链路时优先修改。 |
| `model/trueno3_src-master-0.0.10/config.yaml` | Trueno 旧入口或兼容入口使用的模型映射、算法顺序和多模型说明。 | 只有确认调用入口读取它时才修改；不能替代 `model_descriptor.yaml`。 |
| `model/trueno3_src-master-0.0.10/src/config.yaml` | 原始 `src` 代码的运行配置。 | `src/base` 或 `src/algorithm` 明确读取时修改。 |
| `model/trueno3_src-master-0.0.10/src/base/**/config/*.yaml` | CRNN、PaddleOCR 等基础执行器的模型/预处理参数。 | 对应 base 执行器报错或需要调整输入时。 |
| `model/trueno3_src-master-0.0.10/src/algorithm/**/*.yaml` | 某个算法、训练、OCR 或姿态模块的专用参数。 | 只有对应算法代码引用时修改。 |
| `model/Model-Annotation-tag-0.1.1+python/config.yaml` | 标注采集页数、训练说明筛选、类别匹配和汇总规则。 | 标注业务规则改变时。 |
| `model/Offline_Package_Organization-tag-0.0.12+python/config-1.yaml` | 离线包 M 模式拆包阈值和算法名称兼容映射。 | M 模式拆包或名称兼容规则改变时。 |
| `model/Offline_Package_Organization-tag-0.0.12+python/config-2.yaml` | 离线包 H 模式拆包/历史数据处理规则。 | H 模式业务规则改变时。 |
| `model/Offline_Package_Organization-tag-0.0.12+python/config-3.yaml` | 离线包 Y/N/R 审核、排序、问题图片、快捷键和导出规则。 | Web 审核和导出行为改变时。 |
| `model/Weekly-Report-Print-tag-0.0.8+python/config.yaml` | 周报数据源、阈值、章节开关、算法统计和展示规则。 | 周报统计或章节改变时。 |
| `model/Weekly-Report-Print-tag-0.0.8+python/date_review/config.yaml` | 周报日期审核的站点分组、排序和显示规则。 | 日期审核页面行为改变时。 |

如果某个 YAML 位于模型的训练、Docker、测试或第三方子目录，默认只把它当作该子系统配置，不要把它当作网页全局配置。

## 新增网页或业务标签页

1. 在 `src/views/<page>/` 添加 HTML、CSS、JS。
2. 在 `src/routes.rs` 注册页面和静态资源路由。
3. 通用模型在 `authority.yaml` 增加 `Tab_content`、角色可见性和动作权限，并实现 `portal_entry.py`。
4. 只有固定标签页或通用协议无法表达的交互，才在 `src/handlers/workspace.rs`、`src/views/workspace/` 和 `src/authority.rs` 增加专用逻辑。
5. 所有输入、输出和临时路径都从 `AppState.project_root` 计算，并按用户和任务隔离。
6. 页面状态应放在自己的 JS 中；通用任务轮询和文件列表逻辑不要复制多份。

## 启动、测试和发布

开发检查：

```powershell
cargo fmt --check
cargo check
cargo test
python -m py_compile model\trueno3_src-master-0.0.10\portal_bridge.py model\trueno3_src-master-0.0.10\local_inference\engine.py
```

开发服务：

```powershell
$env:OPEN_BROWSER = '0'
cargo run
```

发布目录 `D:\AlgorithmWeb` 使用 `run.bat` 启动，底层为 `cargo run --release`，默认端口 `3000`。测试目录发布到目标目录：

```powershell
powershell -ExecutionPolicy Bypass -NoProfile -File .\tools\publish.ps1 -Preview
powershell -ExecutionPolicy Bypass -NoProfile -File .\tools\publish.ps1
```

首次需要复制业务数据时才显式添加 `-IncludeData`。发布脚本会备份目标、生成 SHA256 清单并在失败时自动回滚。详细规则见 `Document/DEPLOYMENT.md`。

## 路径和环境变量

- `RUST_PORTAL_PROJECT_ROOT`：显式指定项目根目录，发布端必须指向自身目录。
- `DB_PATH`：SQLite 路径；相对路径相对于项目根目录，默认是根目录 `todo.db`。
- `PORT`：监听端口，默认 `3000`，占用时自动尝试后续端口。
- `OPEN_BROWSER`：不为 `0` 时启动后打开浏览器。
- `PYTHON`：可选，强制指定 Python；未设置时按模型目录虚拟环境和系统 Python 选择。

任何新增功能都应保证测试目录和发布目录不共享数据库、上传目录、模型导入目录、输出目录、日志或临时目录，也不能写入测试目录绝对路径。
