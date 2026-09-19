# 周报生成工具

这是一个基于 YAML 配置数据源的模板化周报生成工具，默认读取 `C:\tools\2.date_review\0Work\old.xlsx` 和 `C:\tools\2.date_review\0Work\result-2.xlsx`。

工具按照站点关键字筛选 Excel 数据，对比上周和本周的算法小类、点位数量、审核后准确率等指标，生成 Markdown 周报及表格图片资产，同时尝试复制到 Windows 剪贴板。

## 1. 目录结构

```text
13.weekly/
├─ weekly_report.py                 # 主程序
├─ config.yaml                      # 周报内容和计算阈值配置
├─ run.bat                          # Windows 快捷入口
├─ output/                           # 运行后生成的周报和表格资产，超过 7 天自动清理
│  ├─ 周报_站点_YYYYMMDD.md             # Markdown 周报
│  ├─ 周报_站点_YYYYMMDD_准确率明细.xlsx  # 整体准确率 Excel 表格
│  ├─ 周报_站点_YYYYMMDD_准确率明细.png   # 整体准确率表格截图
│  └─ 周报_站点_YYYYMMDD_*.png           # 各子列表表格截图
├─ table/                            # 可选的历史测试数据目录，默认不读取
└─ development/
   └─ development.txt               # 需求和早期模板记录
```

## 2. 环境要求

- Windows
- Python 3
- `openpyxl`
- `PyYAML`
- `Pillow`

安装依赖：

```powershell
pip install openpyxl pyyaml pillow
```

如果电脑中有多个 Python，可以使用实际运行 `weekly_report.py` 的 Python 安装依赖：

```powershell
python -m pip install openpyxl pyyaml pillow
```

## 3. 使用方法

### 3.1 使用批处理文件

在项目目录打开命令行，执行：

```bat
run.bat 马王
```

参数是站点关键字，使用子串匹配。例如 `马王` 可以匹配站点名称 `马王风电场 2024-09-30`。

启动后会依次询问准确率数据源和剪贴板模式：

- 第二层输入 `y`：使用“准确率”作为表格和计算数据源。
- 第二层输入 `n`：使用“审核后准确率”作为表格和计算数据源。
- 第二层直接回车：默认使用 `y`。

- 第三层输入 `y`：正文文字 + 表格图片。
- 第三层输入 `n`：整份周报作为一张纯图片。
- 第三层直接回车：默认使用 `y`。

### 3.2 直接运行 Python

```powershell
python weekly_report.py 马王
```

不传参数时，程序会进入交互模式：

```powershell
python weekly_report.py
```

然后输入站点关键字。

### 3.3 输出结果

程序会：

1. 从 `config.yaml` 的 `data_source.new_file` 查找指定站点。
2. 读取 `config.yaml` 的 `data_source.old_file` 作为旧数据，进行新旧对比。
3. 在屏幕打印周报。
4. 生成文件：

```text
output/周报_马王风电场_20260805.md
```

同时生成整体准确率明细：

```text
output/周报_马王风电场_20260805_准确率明细.xlsx
output/周报_马王风电场_20260805_准确率明细.png
```

Markdown 文件会嵌入 PNG 表格，不再输出额外的文本表格。

5. 清理 `output` 目录中超过 7 天的 `周报_*.md`、`周报_*.png` 和 `周报_*.xlsx` 产物。
6. 尝试将周报复制到 Windows 剪贴板，默认正文为文字、表格为内嵌图片。

日期使用运行当天的系统日期。

## 4. Excel 数据要求

程序读取两个 Excel 文件的第一个工作表，第一行为表头，数据从第二行开始。

表头需要与以下字段保持一致：

```text
业务线
项目名称
工程组
站点
服务器状态
算法状态
达标率
更新计划
算法小类
任务日期
点位数量
准确数量
审核后准确数量
拍照模糊数量
审核后拍照模糊数量
准确率
审核后准确率
误报率
审核后误报率
漏报率
审核后漏报率
当前版本
计划数据上传日期
计划训练日期
下一版本当前阶段
下一版本计划更新日期
```

关键要求：

- `站点` 用于关键字匹配。
- `项目名称` 用于输出周报中的项目名称。
- `算法小类` 用于算法分组、新增/移除对比和准确率明细。
- `点位数量`、`准确数量` 用于算法明细中的数量统计。
- `任务日期` 支持类似 `6-30`、`12-03` 的格式。
- 百分比字段支持 `96%`、`96`、`0.96`、`0.86` 等形式。
- `准确率`、`审核后准确率`、`误报率`、`审核后误报率` 四列中的 `-`，读取时按 `0%` 处理。
- 上述 `-` 只在程序内存中标准化，程序不会改写源 Excel 文件。

## 5. 软件运行逻辑

### 5.1 读取站点数据

程序分别读取旧数据和新数据，只保留 `站点` 中包含命令行关键字的行。

站点名称取新数据中的第一条站点记录，并去掉末尾的日期，例如：

```text
马王风电场 2024-09-30
```

会输出为：

```text
马王风电场
```

项目名称会收集新数据中的不同项目名称并合并输出。

### 5.2 算法小类分类

算法小类名称以 `静默` 结尾时，视为静默算法；其他算法视为巡视算法。

例如：

```text
火灾烟雾 静默     # 静默
人员闯入          # 巡视/非静默
```

### 5.3 整体准确率

整体准确率按第二层选择的数据源做算术平均，不把巡视和静默算法混在一起：

- 第二层选择 `y` 时，使用 `准确率` 计算。
- 第二层选择 `n` 时，使用 `审核后准确率` 计算。
- 某一类不存在时，不显示该类的整体统计。
- 四个指定百分比字段中的 `-` 按 `0%` 参加计算。

算法明细会生成带边框和合适列宽的 Excel 表格及 PNG 截图，支持按准确率排序和按阈值着色。

默认表格字段为：

```text
算法小类 | 点位数量 | 准确数量 | 误报/错误数量 | 准确率
```

选择 `n` 时，最后一列为 `审核后准确率`。Markdown 中嵌入 PNG 截图，例如：

```text
2、本站点达标率为 100%，整体平均巡视准确率 60%。
```

整体准确率平均值和明细表只统计 `点位数量` 为有效正数的算法；空值、`-` 和 `0` 不会进入统计。

点位数量支持 Excel 常见的千分位格式，例如 `4,440` 会按 4440 个点位处理。因此，只要算法小类的点位数量不为空、不是 `-` 且大于 0，就一定会保留在准确率明细表中。准确率低于阈值的列表也遵循相同规则。

明细中的数量计算规则：

```text
点位 = 点位数量
准确 = 准确数量
错误/误报 = max(0, 点位数量 - 准确数量)
```

- 静默算法显示 `误报`，即使误报为 0 也显示。
- 巡视算法显示 `错误`，错误为 0 时也显示。
- 巡视算法显示 `错误`，只有错误数量大于 0 时显示。

准确率低于 95% 时，仅准确率单元格字体变为黄色系；低于 50% 时，仅准确率单元格字体变为浅红色。

“下一步计划”中的 0% 准确率评估话术，只列出点位数量为有效正数且当前选择的数据源明确为 `0%` 的算法小类。点位为空、`-`、`0` 或选定准确率为空的算法不会进入括号内名单；选择原始准确率或审核后准确率时，分别只判断对应字段。

### 5.4 新旧数据对比

程序以 `算法小类` 为键进行新旧数据对比：

- 新数据有、旧数据没有：新增算法。
- 旧数据有、新数据没有：移除算法。
- 两边都有但配置指标发生变化：指标变化。

默认以“算法准确率变化”趋势表展示，表头会自动带上本周/上周日期区间，例如：

```yaml
changes:
  metric_mode: accuracy_trend
  show_point_change: false
  metric_sort: desc
```

百分比会统一格式化。例如：

```text
0.86 → 86%
96%  → 96%
```

趋势表采用微信窄版格式，例如：

```text
1、算法准确率变化：
算法名称 | 本周准确率(08.03-08.07) | 上周准确率(07.27~08.02) | 准确率变化
屏柜大指示灯状态 | 96% | 86% | +10%
```

趋势表里 `本周准确率` 和 `上周准确率` 两列已经加宽，日期会完整显示。

如果把 `metric_mode` 改回 `generic`，仍可继续使用 `compare_fields` 做旧式字段对比。

### 5.5 阈值和待办判断

程序根据 `config.yaml` 中的阈值生成：

- 准确率低于阈值的算法。
- 误报率高于阈值的算法。
- 漏报率高于阈值的算法。
- 持续 0 准确率算法。
- 误报率较高算法的下一步优化建议。
- 任务日期过久的数据失效风险。
- 点位数量为空或为 `-` 的数据风险，并以表格列出算法小类和点位数量；风险表中空点位统一显示为 `0`。

`下一版本当前阶段` 中的关键字还可以识别：

- `填写问题日志`：问题日志待办。
- `准确率上传`：准确率上传待办。

## 6. YAML 配置说明

配置文件为 [config.yaml](config.yaml)。

### 6.1 数据源配置

默认数据源：

```yaml
data_source:
  directory: 'C:\tools\2.date_review\0Work'
  old_file: old.xlsx
  new_file: result-2.xlsx
```

`directory`、`old_file` 和 `new_file` 都支持绝对路径。文件名或相对路径会基于 `directory` 解析；`directory` 为相对路径时基于程序目录解析。

### 6.2 算法小类控制

可以按算法小类控制显示和计算：

```yaml
algorithm_control:
  enabled: true
  display: false
  include_in_calculation: false
  algorithm_classes:
    - 数显油温表最高温度
    - 烟雾
  tables:
    accuracy_detail: true
    low_accuracy: true
    high_false_positive: true
    blur: true
    changes: true
    handled: true
    risks: true
```

`display: false` 会隐藏列表中的算法小类；`include_in_calculation: false` 会同时将其排除在统计、新旧对比和相关计算之外。`tables` 可单独控制哪些表格应用这组显示过滤。

### 6.3 阈值配置

```yaml
thresholds:
  accuracy: 95
  false_positive: 5
  stale_days: 15
```

| 配置项 | 含义 |
| --- | --- |
| `accuracy` | 准确率达标阈值，单位为百分数 |
| `false_positive` | 误报率阈值，超过该值视为偏高 |
| `stale_days` | 任务日期超过该天数视为数据失效风险 |

### 6.4 章节开关

```yaml
sections:
  site: true
  overview: true
  changes: true
  handled: false
  next_steps: true
  risks: true
```

对应周报章节：

| 配置项 | 章节 |
| --- | --- |
| `site` | 站点和项目名称 |
| `overview` | 算法运行实况 |
| `changes` | 本周变化 |
| `handled` | 已处理事项，默认关闭 |
| `next_steps` | 下一步计划 |
| `risks` | 问题及风险 |

章节编号会根据实际开启的章节自动连续编号。

### 6.5 算法运行实况配置

```yaml
overview:
  show_total_points: true
  show_avg_accuracy: true
  show_site_attainment: true
  show_accuracy_details: true
  accuracy_detail_display: raw
  accuracy_detail_sort: desc
  accuracy_detail_columns:
    show_points: true
    show_correct: true
    show_errors: true
    show_accuracy: true
  accuracy_detail_colors:
    warning_threshold: 95
    warning_text: C88700
    danger_threshold: 50
    danger_text: E57373
  show_low_accuracy: true
  show_high_false_positive: false
  show_blur: true
  show_idle: false
  show_empty_placeholder: false
```

| 配置项 | 说明 |
| --- | --- |
| `show_total_points` | 显示本周点位总数和算法小类数量 |
| `show_avg_accuracy` | 显示巡视/静默整体准确率 |
| `show_site_attainment` | 显示“本站点 达标率为 xxx” |
| `show_accuracy_details` | 显示各算法小类明细 |
| `accuracy_detail_display` | `audited` 只显示审核后；`raw` 只显示原始；`both` 两者都显示 |
| `accuracy_detail_sort` | `desc` 按准确率从高到低，`asc` 从低到高，`none` 保持原顺序 |
| `accuracy_detail_columns` | 控制准确率表里点位、准确数量、误报/错误数量、准确率列是否显示 |
| `accuracy_detail_colors` | 控制准确率表中准确率单元格的字体颜色和阈值 |
| `show_low_accuracy` | 显示低于准确率阈值的算法 |
| `show_high_false_positive` | 显示误报率偏高算法 |
| `show_blur` | 显示拍照模糊点位 |
| `show_idle` | 显示无触发/长期挂起算法 |
| `show_empty_placeholder` | 没有数据时是否输出“无”占位行 |

将 `show_site_attainment` 设置为 `no` 时，周报会隐藏“本站点 达标率为 xxx”，但不影响整体平均准确率和准确率明细表：

```yaml
overview:
  show_site_attainment: no
```

默认配置：

```yaml
accuracy_detail_display: raw
```

因此默认不会同时显示：

```text
准确率 0%，审核后 0%
```

而是显示：

```text
准确率 0%
```

### 6.6 子列表表格图片配置

报告中原本使用 `-` 展示的低准确率、拍照模糊、指标变化、已处理明细和风险明细，可统一改为 PNG 表格：

```yaml
table_layout:
  enabled: true
  split_long_tables: true
  split_ratio: 0.5
  column_gap: 0
```

| 配置项 | 说明 |
| --- | --- |
| `enabled` | 是否将 `-` 子列表转换为表格 PNG；关闭后恢复文本列表 |
| `split_long_tables` | 长表是否自动拆成左右两块 |
| `split_ratio` | 表格高度超过宽度该比例时触发左右拆分 |
| `column_gap` | 左右表块间距，`0` 表示两块紧贴 |

拆分后两块使用相同表头，左侧放前半部分数据，右侧放后半部分数据。

表格会自动隐藏整列为空、`-` 或 `--` 的列；如果只有某一行没有变化，该单元格保留 `-`，不会隐藏整列。
在 `changes.show_dash_for_unchanged: false` 时，指标变化表里未变化的字段会显示本周原值，例如点位数量还是原来的数字、准确率还是原来的百分比。

### 6.7 剪贴板输出配置

默认粘贴方案是“正文文字 + 表格图片”，适合微信群阅读。整份报告纯图片方案默认关闭：

```yaml
clipboard:
  pure_image: false
```

将 `pure_image` 改为 `true` 后，剪贴板只写入整份报告的原生图片格式。

### 6.8 本周变化配置

```yaml
changes:
  show_added: true
  show_removed: true
  show_metric_changes: true
  metric_mode: accuracy_trend
  show_point_change: false
  metric_sort: desc
  show_dash_for_unchanged: false
  compare_fields:
    - 点位数量
    - 审核后准确率
```

`metric_mode` 控制本周变化的展示方式：

- `accuracy_trend`：显示“算法准确率变化”趋势表，表头自动带日期区间。
- `generic`：沿用旧的字段对比表。

`compare_fields` 仅在 `generic` 模式下使用。若需要增加对比字段，可以直接添加，例如：

```yaml
compare_fields:
  - 点位数量
  - 审核后准确率
  - 审核后误报率
```

`show_dash_for_unchanged` 控制指标变化表里未变化字段的显示方式：

- `true`：保留 `-`
- `false`：显示本周原值

`show_point_change` 控制趋势表里是否额外展示点位数量及变化列。

`metric_sort` 控制趋势表排序：

- `desc`：准确率高的排前面
- `asc`：准确率低的排前面
- `none`：保持输入顺序

### 6.9 已处理配置

```yaml
handled:
  show_accuracy_improved: true
  show_fp_reduced: true
  show_leak_reduced: true
  show_problem_log_done: true
  show_upload_done: true
  show_date_updated: true
  show_empty_placeholder: false
```

该章节默认由 `sections.handled: false` 关闭。开启后会根据新旧数据输出：

- 准确率已达标。
- 误报率已降低。
- 漏报率已降低。
- 问题日志已填写完成。
- 准确率已上传。
- 任务日期已更新。

### 6.10 下一步计划配置

```yaml
next_steps:
  show_fp_optimization: true
  show_problem_log_deadline: false
  show_idle_evaluation: true
  templates:
    fp_optimization: 针对误报率较高的算法（{high_fp_names}）继续负样本采集与模型优化。
    idle_evaluation: 对正在使用但目前准确率为0%的算法（{idle_names}）评估继续投入或替换模型。
    problem_log_deadline: 推动工程组在 {deadline} 前完成问题日志填写。
```

| 配置项 | 说明 |
| --- | --- |
| `show_fp_optimization` | 有误报率偏高算法时生成负样本采集和模型优化建议 |
| `show_problem_log_deadline` | 有问题日志待办时生成限期完成建议 |
| `show_idle_evaluation` | 有 0 准确率算法时生成继续投入或替换评估建议 |
| `templates` | 可选，按条件用 YAML 文案替代代码内固定句子 |

### 6.11 问题及风险配置

```yaml
risks:
  show_blur_risk: true
  show_stale_risk: true
  show_idle_risk: false
  show_point_empty_risk: true
  detailed: true
  templates:
    blur:
      detailed: 模糊风险：算法存在拍照模糊点位，进而影响识别准确率：
      summary: 部分算法存在拍照模糊点位，可能影响识别准确率，需检查拍摄环境及设备状态。
    stale:
      detailed: 数据失效风险：以下算法任务日期久远（超过 {stale_days} 天），存在数据失效风险：
      summary: 部分算法任务日期久远，存在数据失效风险，需确认是否继续运行。
    idle:
      detailed: 数据质量风险：以下算法长期无有效准确率数据，需关注数据质量与算法运行情况：
      summary: 部分算法长期无有效准确率数据，需关注数据质量与算法运行情况。
    point_empty:
      detailed: 点位数据风险：以下算法点位数量为空或为“-”，暂无法纳入准确率统计：
      summary: 部分算法点位数量为空或为“-”，暂无法纳入准确率统计，需补充点位数据。
```

| 配置项 | 说明 |
| --- | --- |
| `show_blur_risk` | 存在拍照模糊点位时，在“问题及风险”中显示模糊风险 |
| `show_stale_risk` | 显示任务日期过久风险 |
| `show_idle_risk` | 显示长期无有效准确率数据风险 |
| `show_point_empty_risk` | 显示点位数量为空或为 `-` 的算法风险，并生成表格；表格中空点位显示为 `0` |
| `detailed` | `true` 列出算法、点位数量和日期；`false` 只输出概述 |
| `templates` | 可选，按条件用 YAML 文案替代代码内固定句子 |

### 6.12 字段级配置

```yaml
fields:
  准确率:
    display: audited
    priority: audited
    threshold: 95
```

字段级配置主要用于阈值判断、优先取值，以及误报率高于阈值时的标签显示。算法明细中的“准确率/审核后准确率”显示方式由 `overview.accuracy_detail_display` 控制。

| 配置项 | 说明 |
| --- | --- |
| `display` | `raw` 原始值、`audited` 审核后值、`both` 两者；当前主要影响误报率异常项的标签 |
| `priority` | 判断和单值展示时优先使用 `raw` 或 `audited` |
| `threshold` | 该字段对应的判断阈值 |

`下一版本当前阶段.keywords` 用于关键字到待办类型的映射：

```yaml
下一版本当前阶段:
  keywords:
    填写问题日志: problem_log
    准确率上传: upload_pending
```

## 7. 周报输出结构

默认输出结构如下：

```text
【日期】周报

一、站点
项目名称

二、算法运行实况
1、本周审核点位、算法数量、静默任务
2、巡视/静默整体准确率及表格化算法明细
3、低准确率算法
4、拍照模糊点位

三、本周变化
1、新增算法
2、移除算法
3、指标变化

四、问题及风险

五、下一步计划
```

某个章节或小项没有内容时，是否显示“无”，由对应的 `show_empty_placeholder` 控制。

## 8. 常见问题

### 找不到站点

确认：

1. 关键字确实出现在新 Excel 的 `站点` 列。
2. `config.yaml` 的 `data_source.directory`、`old_file` 和 `new_file` 指向正确文件。
3. 数据位于第一个工作表。
4. 站点关键字没有输入错误。

### 周报中审核后准确率显示为 0%

这是当前规则：百分比字段中的 `-` 会按 `0%` 计算。这样缺失的审核结果不会被平均值直接忽略。

### 想同时显示准确率和审核后准确率

修改 `config.yaml`：

```yaml
overview:
  accuracy_detail_display: both
```

### 想只显示原始准确率

```yaml
overview:
  accuracy_detail_display: raw
```

### 剪贴板复制失败

默认剪贴板输出为文字正文和表格图片；程序同时写入 HTML、RTF 和 Unicode 文本，兼容微信和企业微信的不同粘贴实现。只有 `clipboard.pure_image: true` 时，才会改为整份报告纯图片。若富文本复制失败，程序会降级复制普通文本；周报文件和 PNG 资产仍会正常保存。

## 9. 修改配置后的验证

修改 `weekly_report.py` 后，可以先检查语法：

```powershell
python -m py_compile weekly_report.py
```

然后运行一个站点进行验证：

```powershell
python weekly_report.py 俄公堡
```

重点检查：

- 项目名称是否输出。
- 巡视和静默准确率是否分开。
- 第二层选择的准确率数据源是否贯穿表格和计算。
- 静默是否显示 `误报：0`。
- 巡视错误为 0 时是否隐藏“错误”。
- 指标变化中的百分比是否统一为百分号格式。
