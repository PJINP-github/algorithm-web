# Trueno 3

## 本机模型验证

仓库同时提供一个不依赖服务器硬件运行时的本地验证入口。它读取
`storage/model_descriptor.yaml`，扫描根目录 `model/` 中的权重，并使用
PyTorch + Ultralytics 在 CPU 或 CUDA 上推理。

```powershell
python -m pip install -r local_requirements.txt
python run_local.py --device cpu
```

浏览器打开 `http://127.0.0.1:8765/`，可以导入图片、选择能力或模型、
拖拽绘制 ROI，并查看检测分类、置信度、流水线、模型算子和调用日志。
完整的本地实现说明见 [LOCAL_README.md](LOCAL_README.md)。

本机入口与 Docker、服务器 `requirements.txt`、`model_requirements.txt`
和依赖远程服务的 `tests/test_algo/` 相互独立。

## Feature List
[x] Automatically Return Label
[x] Code Authentication
[x] Cross-platform Compile
[ ] Model Encryption/Decryption
[ ] Rockchip / Atlas / Cambricon Support
[ ] Add a remote artifacts automatically download

## How to Use
#### Through Docker
#### Through Host

# How to Dev
1. Install PyTorch
2. Install requirements.txt
3. Install model_requirements.txt

4. make build
5. docker build
6. docker run

#### Install PyTorch
* To find CUDA version, use `nvcc --version`
* To install correct PyTorch version, use:
    * CUDA 11.8
        * Conda: `conda install pytorch==2.1.0 torchvision==0.16.0 torchaudio==2.1.0 pytorch-cuda=11.8 -c pytorch -c nvidia`
        * Pip: `pip install torch==2.1.0 torchvision==0.16.0 torchaudio==2.1.0 --index-url https://download.pytorch.org/whl/cu118`
    * CUDA 12.1
        * Conda: `conda install pytorch==2.1.0 torchvision==0.16.0 torchaudio==2.1.0 pytorch-cuda=12.1 -c pytorch -c nvidia`
        * Pip: `pip install torch==2.1.0 torchvision==0.16.0 torchaudio==2.1.0 --index-url https://download.pytorch.org/whl/cu121`
