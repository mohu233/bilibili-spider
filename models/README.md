# 模型准备

此目录只随仓库发布说明，权重、下载清单与实验缓存保留在本机。安装 Python 依赖不会自动下载模型。

## 面板支持的模型

| 设置值 | 需要的本地文件 | 依赖 |
| --- | --- | --- |
| `mobilenetv3_v0.1_dist`（默认） | `models/mobilenetv3_v0.1_dist/model.onnx` 与 `meta.json` | requirements-panel.txt |
| `caformer_s36_v0.1` | `models/caformer_s36_v0.1/model.onnx` 与 `meta.json` | requirements-panel.txt |
| `yolo` | `models/furry1500x200.pt` | 另需 Ultralytics 等旧版依赖 |

DeepGHS 模型来自 [deepghs/anime_furry](https://huggingface.co/deepghs/anime_furry)。默认 MobileNetV3 的下载命令见 [项目 README](../README.md#2-准备头像模型)。CAFormer 可使用相同的固定版本，将 URL 与本地目录中的模型名都替换为 `caformer_s36_v0.1`。请同时下载元数据，分类标签顺序由 `meta.json` 决定。

面板也会在 `data/avatar_benchmark_*/models/<模型名>/` 查找已有 DeepGHS 模型，优先使用本目录。没有权重或依赖时，页面显示实际可用状态；采集不会把模型加载失败当作非 Furry。

原 YOLO 权重需从自己的本机备份或已获授权的来源另行准备。当前源码提交停止跟踪该文件，不删除已有本机副本，也不重写以前的 Git 历史。旧版 `main.py` 仍使用此 YOLO 路径。

## 实验模型

`tools/avatar_benchmark.py` 是独立实验工具，不会自动改变面板判定。其 `models` 命令会下载两个 DeepGHS 模型以及 Google SigLIP 2 的一整套文件，**不是只下载默认小模型**。

SigLIP 2 来源为 [google/siglip2-base-patch16-224](https://huggingface.co/google/siglip2-base-patch16-224)，仅用于实验对比，当前不能在面板中作为自动判定模型选择。运行实验前先查看 `python tools/avatar_benchmark.py --help`，并显式指定自己的实验目录和数据源。

模型分数不能直接等同于账号身份概率，不同模型的分数也未做联合校准。第三方模型使用各自模型卡中的许可证，不因放在本项目目录而改变许可。
