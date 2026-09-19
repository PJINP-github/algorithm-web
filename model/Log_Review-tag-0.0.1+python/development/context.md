# logs-trueno3-0818.txt 结构化理解

## 文件定位
- 源日志：`logs-trueno3-0818.txt`
- 指导文件：`AGENTS.md`、`development/Guidance.md`
- 本上下文文件用途：沉淀本次日志阅读结论，后续对话优先依赖这里，避免重复读取完整日志。

## 统计口径
- 日志总行数：3125 行。
- 时间范围：2026-08-18 08:08:52 到 2026-08-18 08:17:36，跨度约 8 分 44 秒。
- 可解析的 `trueno3 - DEBUG` Request/Response JSON：165 条，解析失败 0 条。
- `[POST_RET]` 回调：167 条，`sync-return 200`：167 条；所有回调目标均为 `192.168.3.109:9080`，回调体均为 `{"code":200}`。
- `[T3E]` 错误：2 条；因此 167 次回调 = 165 条正常 JSON 响应 + 2 条错误回调。

## 日志主要结构
- `[T3I] objectId // type@model : stage`：算法阶段入口。`stage` 出现 1、2、3，仪表类任务会有多阶段链路。
- `base yolo infer_core results ...`：基础检测模型原始框、置信度、类别及交集区域。
- `base yolo infer_core results_filter ...`：按交集区域或规则过滤后的检测结果。
- `[WARN] detect without intersection region, return full results`：缺少交集区域时返回全量检测结果。
- `[WARN] crop without ROI region, use full image`：缺少 ROI 时使用整图。
- `analyse_ret @ 0 : ((x1, y1), (x2, y2)) conf`：最终分析区域及置信度。
- `trueno3 - DEBUG - {"Request": ..., "Response": ...}`：完整请求和响应。
- `[POST_RET] ...` / `sync-return 200`：结果回调及同步返回状态。

## Request/Response 结构
- Request 关键字段：
  - `objectId`：同一对象或任务链路标识。
  - `typeList` / `imageRecogType`：识别类型。
  - `imageUrlList`：图片路径，形如 `/image/task/{taskId}/2026/08/18/10/{cameraIp}_1_{requestId}.jpg`。
  - `parameter`：可能包含 `value_map`、`marks`、`needle`、`on_intersection_output`、`roi`。
  - `imageBase64` / `imageNormalBase64`：日志中存在，但上下文分析主要依赖结构字段和图片路径。
- Response 关键字段：
  - `results[].type`：识别类型。
  - `results[].value`：识别值或分类值。
  - `results[].code`：业务返回码，绝大多数为 `2000`。
  - `results[].desc`：中文结果描述，如“正常”、读数、缺陷名称。
  - `results[].pos`：结果区域。
  - `results[].conf`：置信度。

## 请求类型分布（DEBUG JSON）
| 类型 | 数量 | 含义概括 |
| --- | ---: | --- |
| `all_qxsb` | 62 | 外观缺陷识别 |
| `sly_bjbmyw` | 27 | 避雷器表计表面异物/缺陷类识别；日志中全部返回正常 |
| `meter_digital_dlb` | 23 | 数显电流表读数 |
| `meter_sf6_qy` | 21 | SF6 压力表读数 |
| `meter_gsywj` | 18 | 变压器管式油位计读数 |
| `meter_digital_ylb` | 6 | 数显压力表读数 |
| `meter_kgg_envTemp` | 5 | 数显温度读数 |
| `light_pgxzsd_mn` | 3 | 灯光/状态类识别 |

## 算法阶段分布（T3I）
| 类型 | T3I 数量 | 主要模型/阶段说明 |
| --- | ---: | --- |
| `defect` | 88 | `at-det-sub-wgsb.m-yolov8det-sub-wgsb_20251020.1`，对应 `sly_bjbmyw` 相关检测 |
| `meter_digit_p1` | 34 | 仪表数字识别前处理/分割阶段 |
| `meter_digit_p2` | 33 | `crnn_dm_ocr-0.1.0`，OCR 阶段 |
| `meter_p1` | 21 | 指针/仪表区域定位 |
| `meter_p2` | 21 | 指针定位 |
| `meter_p3` | 21 | 刻度定位 |
| `txj_ywj` | 20 | 油位计检测 |
| `light_pgzsd` | 3 | 灯光状态检测 |

## 返回结果概览
- Response code：
  - `2000`：164 条。
  - `2002`：1 条，类型为 `meter_digital_ylb`，`objectId=43fdbaee49a34e08859f2ed79b4c4e8e`，`desc=正常`，需要业务方确认 `2002` 与“正常”描述是否一致。
- `all_qxsb`：62 条，其中 57 条正常，5 条缺陷：
  - `外观识别: 导电接头锈蚀`：4 条。
  - `外观识别: 引线断股松股`：1 条。
- `sly_bjbmyw`：27 条，全部 `value=0`、`desc=正常`。
- 仪表类结果主要是读数：
  - `meter_digital_dlb`：常见读数 `000.0`、`00.0`，也有 `015.1`、`016.3`、`014.6`、`010.0` 等。
  - `meter_sf6_qy`：读数多在 `0.13` 到 `0.70` 区间，常见 `0.56`、`0.54`、`0.65`。
  - `meter_gsywj`：读数包含 `0`、`11`、`15`、`25`、`27`、`34`、`40`、`44`、`46`、`54`、`60`、`72`、`92`、`95`。
  - `meter_kgg_envTemp`：读数包含 `0.8`、`9.7`、`17`、`18.`、`47.1`。

## 图片与设备分布
- DEBUG JSON 中图片主要来自任务 `37837`：138 条；其余早段样本来自任务 `14` 到 `26` 等小编号任务。
- 出现最多的相机 IP：
  - `192.168.3.62`：38 条。
  - `192.168.3.63`：10 条。
  - `192.168.3.48`、`192.168.3.49`、`192.168.3.50`：各 9 条。
- `analyse_ret` 中可解析区域 165 条：
  - 整图结果 86 条。
  - 裁剪/局部区域结果 79 条。
  - 置信度约 `0.2108` 到 `1.0`，平均约 `0.9122`。

## 警告与异常
- `detect without intersection region, return full results`：133 次。说明不少请求未提供或未命中交集区域，算法退回全量检测结果。
- `crop without ROI region, use full image`：34 次。说明不少裁剪阶段未拿到 ROI，退回整图处理。
- `results_filter`：61 次。说明存在检测结果被区域/规则过滤。
- `detect a break signal`：85 次。通常出现在 `defect` 类型阶段之后，可能表示该阶段完成后中断后续检测链路或提前返回。
- 明确错误 2 次：
  - `requestId=b235ee4c-3cb0-484a-a547-4183f85031a9`
  - `requestId=348473c8-b3a5-42dc-af06-c05f032f920e`
  - 错误描述均为：`ERROR: list index out of range @ algorithm/txj_ywj/txj_ywj.py:74#patch_infer`
  - 错误前日志显示 `txj_ywj` 有过滤后结果并输出 `index, final_score 0 0`，疑似在油位计后处理读取列表元素时越界。
  - 这 2 次错误仍然触发 `[POST_RET] ... {"code":200}` 和 `sync-return 200`，说明传输层回调成功，但业务结果为空或错误。

## 当前判断
- 这是一段 Trueno3 算法服务的短时间集中推理日志，覆盖外观缺陷、避雷器/表计缺陷、数显表、指针/SF6 表、油位计、温度和灯光状态等多类识别。
- 通信回调整体稳定：所有回调均同步返回 200。
- 业务侧主要风险不在 HTTP 回调，而在算法输入区域缺失导致的整图兜底，以及 `txj_ywj.py:74#patch_infer` 的 2 次列表越界。
- 如果后续要定位问题，优先看：
  1. `algorithm/txj_ywj/txj_ywj.py:74#patch_infer` 对过滤后结果为空、长度不足或分数为 0 的处理。
  2. 上游是否稳定传入 `on_intersection_output` 和 `roi`。
  3. `meter_digital_ylb` 中 `code=2002` 但 `desc=正常` 的业务含义。
