# 通用 Model 目录结构模板

本模板适用于通过 `model/authority.yaml` 的 `Tab_content` 新增网页标签页。
固定标签页不需要迁移到该结构。

## 推荐目录

```text
model/<业务目录>/
  portal_entry.py          # 固定网页入口，必须支持 --request-file
  config.yaml              # 业务默认配置，可选
  requirements.txt         # 模型独立依赖，可选
  README.md                # 业务说明和运行依赖
  src/                     # 业务代码
  model/                   # 权重或算法资源
  portal_inputs/           # Rust 按用户保存的导入副本
  portal_outputs/          # Rust 按用户和任务保存的结果
  .portal_tmp/             # 请求文件和任务临时文件
```

发布时不要在模型目录内依赖其他模型目录的虚拟环境、缓存、临时目录或绝对路径。

## authority.yaml 配置

```yaml
Tab_content:
  - id: stable_ascii_id
    name: 网页显示名称
    description: 标签页说明
    model_dir: stable-model-folder
    entry: portal_entry.py
    Component_elements:
      import:
        - id: input_folder
          name: 导入文件夹
          kind: folder
          accept: .png,.jpg
          required: true
        - id: input_file
          name: 导入文件
          kind: file
          accept: .txt,.csv
          required: false
      changes:
        - id: mode_a
          name: 模式 A
          description: 模式说明
          action: mode_a
          # permission 可选；默认使用 model.<tab-id>.<change-id>
      parameters:
        - id: threshold
          name: 阈值
          type: number
          default: 0.5
          required: true
        - id: algorithm
          name: 算法
          type: select
          default: default
          options:
            - value: default
              label: 默认算法
        - id: enabled
          name: 启用后处理
          type: checkbox
          default: true
        - id: tags
          name: 标签
          type: multi_select
          options:
            - value: a
              label: A
      # 可以使用参数 ID，也兼容 [1, 2, 3] 这种按参数顺序引用。
    parameter_combinations:
      - [algorithm, threshold, enabled]
      - [algorithm, tags]
    Log_Display: true
    Latest_temporal_display_file:
      base: project
      file: Document/VERSION
      folder: Document
      recursive: false
      extensions: [md, yaml, yml, txt]
    Display_imported_folder: true
```

角色可见性和动作权限仍使用现有权限表：

```yaml
Feature_Tab_Visibility:
  Administrator_Rights:
    stable_ascii_id: true

Administrator_Rights:
  model.stable_ascii_id.mode_a: true
```

## portal_entry.py 协议

Rust 会创建任务请求文件，并执行：

```text
python portal_entry.py --request-file <absolute-request-file>
```

请求 JSON 的核心字段：

```json
{
  "model": "stable_ascii_id",
  "action": "mode_a",
  "parameters": {
    "threshold": 0.5
  },
  "uploads": {
    "input_folder": "C:\\...\\portal_inputs\\u1\\upload\\"
  },
  "input_path": "portal_inputs/u1/upload",
  "output_dir": "C:\\...\\portal_outputs\\u1\\job",
  "result_path": "C:\\...\\portal_outputs\\u1\\job\\portal_result.json",
  "project_root": "C:\\...",
  "model_root": "C:\\...\\model\\stable-model-folder",
  "user": {
    "id": 1,
    "username": "user",
    "role": "普通二级"
  }
}
```

入口必须遵守：

1. 只读取请求中的导入副本，不访问其他用户目录。
2. 需要修改输入时只修改 `portal_inputs/<用户>/<上传任务>` 副本。
3. 结果写入 `output_dir`，不要写入项目外路径。
4. 日志写 stdout 或 stderr，每行一条可读信息。
5. 成功时将 JSON 写入 `result_path`，至少包含 `status` 和 `summary`。
6. 成功退出码为 `0`，失败退出码为非零。

结果示例：

```json
{
  "status": "completed",
  "summary": "处理完成。",
  "files": ["report.xlsx", "result.yaml"],
  "folders": ["processed"]
}
```

Rust 会按任务目录收集实际文件，并按当前用户权限展示。结果 JSON 中的 `files` 和
`folders` 用于业务说明，不应替代路径安全校验。

## 新增模型步骤

1. 复制本模板目录结构，确定稳定的 ASCII `id`。
2. 实现 `portal_entry.py --request-file`。
3. 在 `authority.yaml` 增加 `Tab_content`、角色可见性和动作权限。
4. 先运行 `cargo check` 和入口 Python 编译检查。
5. 使用小型测试文件夹验证导入、日志、结果文件和输入副本隔离。
6. 更新 `Document/VERSION` 后再执行发布脚本。
