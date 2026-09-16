# 模型目录约定

工作区把每个业务模型放在 `model/` 下的独立目录中。Rust 服务只负责统一登录、权限、上传、任务队列和结果展示；模型目录保留自己的 Python、配置、虚拟环境和运行时文件。

## 当前目录

- `Log_Review-tag-0.0.1+python`：日志转换和表计调试。
- `Model-Annotation-tag-0.1.1+python`：标注任务、模型标注和训练清单。
- `Offline_Package_Organization-tag-0.0.12+python`：离线包审核、整理和导出。
- `Weekly-Report-Print-tag-0.0.8+python`：网页采集、周报生成和待办更新。
- `trueno3_src-master-0.0.10`：Trueno 单图 YOLO/OCR 推理。

## 新增模型

新增模型时按下面顺序处理：

1. 在 `model/` 下创建带版本标签的独立目录，模型脚本和专用依赖只放在该目录。
2. 在 `src/handlers/workspace.rs` 的模型 ID映射中加入目录，并补充动作、上传类型、输出收集和权限键。
3. 在 `model/authority.yaml` 中补充标签页可见性、动作权限、浏览器路径或文件说明区根目录。
4. 为用户隔离输入、输出和临时文件，目录名使用当前用户和任务 ID；临时内容放入 `.portal_tmp`。
5. 在工作区前端补充模型面板和交互状态，避免把模型专用逻辑混入通用上传、任务轮询代码。
6. 更新 `Document/VERSION`，运行 `cargo fmt`、`cargo test`、`cargo check` 和前端语法检查。

## 路径约定

- 输入：模型目录下的用户隔离目录，例如 `portal_inputs/<user>`、`images/<user>`。
- 输出：模型目录下的 `portal_outputs/<user>/<job>`，或模型自身已有的结果目录。
- 临时文件：模型目录下的 `.portal_tmp/<user>/<job>`，文件说明区默认忽略。
- 回收站：项目根目录下的 `.portal_recycle`，文件说明区删除操作只移动到这里。

版本化模型目录不应依赖其他模型目录中的虚拟环境、浏览器用户数据或临时文件。跨模型共享的配置应放在项目级配置中，并通过权限和路径校验访问。
