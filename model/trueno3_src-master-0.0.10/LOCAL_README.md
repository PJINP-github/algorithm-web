# Trueno 3 本地实现说明

## 已实现

- 读取 `storage/model_descriptor.yaml` 中的能力、流水线、类别和阈值配置。
- 从根目录 `model/` 扫描 `.pt`、`.onnx`、`.torchscript` 和 `.engine` 权重。
- 使用 Ultralytics + PyTorch 在 CPU 或 CUDA 上进行 YOLO 检测。
- 支持网页导入 JPG、PNG、WEBP 图片。
- 支持在原图上拖拽矩形 ROI，并将 ROI 裁剪后送入模型；检测框坐标会还原到原图。
- 支持多阶段流水线：第一阶段检测目标，后续阶段对目标框裁剪后继续推理。
- 流水线中缺少本地执行器的阶段会在推理时跳过并在调用日志中记录原因，其余阶段照常执行。
- 网页调用日志直接读写 `src/logs/trueno3.log`，与服务器共用同一份 WeeklyLogger 日志。
- 网页检测框标签使用项目 `src/SimHei.ttf` 中文字体绘制，中文不再乱码。
- 支持 `select_cls`、`trans_cls`、`cls_conf`、`conf`、`iou`、`imgsz` 配置。
- 网页显示检测框、类别、置信度、流水线耗时、模型算子统计和调用日志。
- `image_black` 能力使用 OpenCV 本地判断，不要求加载 YOLO 权重。
- 通过 `tools/local_docs/generate_code_docs.py` 为 Python 文件和目录生成中文说明。

## 安装

本机实现不需要启动 Docker，也不要求使用服务器专用的 `requirements.txt`、
`model_requirements.txt` 或 `tests/test_algo` 远程测试配置。

```powershell
python -m pip install -r local_requirements.txt
```

当前工作区已验证的运行时包括：

- Python 3.14.6
- PyTorch 2.13.0+cpu
- Ultralytics 8.4.144
- OpenCV 4.10.0

## 启动网页

在项目根目录执行：

```powershell
python run_local.py --device cpu
```

浏览器打开 `http://127.0.0.1:8765/`。

可选参数：

```text
--host       监听地址，默认 127.0.0.1
--port       监听端口，默认 8765
--device     auto、cpu 或 cuda:0
--descriptor 自定义模型描述文件
--model-dir  自定义模型目录
```

## 命令行验证

```powershell
python -m local_inference.cli `
  --image images\verify_point01585_ret_img.jpg `
  --ability light_pgdzsd `
  --device cpu `
  --conf 0.1 `
  --output local_result.jpg
```

## HTTP API

网页服务提供以下接口：

```text
GET  /api/health
GET  /api/config
GET  /api/models
GET  /api/model-info?model=<文件名>
GET  /api/logs
POST /api/infer
```

`POST /api/infer` 的最小请求：

```json
{
  "ability": "light_pgdzsd",
  "image": "data:image/jpeg;base64,...",
  "roi": [100, 100, 1000, 700],
  "conf": 0.1,
  "iou": 0.7,
  "imgsz": 640
}
```

## 模型匹配说明

服务器描述文件中的 `param` 指向服务器的 `storage/params` 文件名。
本机模型放在根目录 `model/`，引擎依次尝试精确匹配、模糊匹配；当前
只有一个本地模型时，会使用单模型回退，并在网页调用日志中明确记录。

当前仓库中的模型为：

```text
model\E2025070221.E2025070221-007.at-det-pg.m-yolov8det-pg+20260904.67.pt
```

已验证它可以在 CPU 上加载，任务为 `detect`，包含 18 个模型类别和
约 43.6M 个参数。它与描述文件中的部分能力共享类别体系，因此网页中
选择的能力应优先选择类别与该权重匹配的能力，例如 `light_pgdzsd`。

## 测试和文档

本地适配器测试：

```powershell
python -m pytest -q tests\test_local_inference.py
```

生成或更新所有目录说明：

```powershell
python tools\local_docs\generate_code_docs.py
```

部署服务器和原始远程验证流程仍保留在 `src/`、`docker/` 和
`tests/test_algo/` 中，本地入口不会加载它们的编译授权和远程 FTP 配置。
