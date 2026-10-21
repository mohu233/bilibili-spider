# Bilibili Spider · Furry Lab

本地 B 站 UID 采集、证据评分与人工审核工作台。以候选 UID 为入口，结合头像、简介、近期视频标题和标签进行判断，再从已确认的种子逐步发现公开关系。

当前推荐入口是 **`panel.py` 可视化面板**。旧版 `main.py` 与 JSON 工具保留用于维护历史资料，两套任务与数据库相互独立。

## 主要功能

- **候选与种子分离**：发现的 UID 先判定，通过后进入种子池；完成遍历后退出待遍历队列，保留身份与证据。
- **两种采集顺序**：默认先处理全局候选积压；也可按“最高分种子 → 公开粉丝/关注 → 关联账号判定 → 下一个种子”运行。
- **证据可追溯**：保存头像模型结果、简介命中词、最多 3 个近期视频的标题与标签，以及获取失败和主动跳过原因。
- **逐条与图片批量审核**：支持人工确认、排除、暂缓；图片审核提供排除模式与选中模式，人工判定优先于自动结果。
- **多账号调度**：自动分配或手动设置 1–20 线程上限；每账号独立等待，头像匿名下载并缓存。
- **分页续采与连续运行**：逐页保存进度，区分公开列表结束、隐私受限、临时失败和平台风控。
- **本地数据与导出**：SQLite 保存状态，支持历史 UID 参考导入、搜索筛选及 JSON 导出。

模型分数反映头像内容，**不等于账号身份概率**。真实宠物、物品或普通插画也可能被误判，建议结合证据与人工复核使用。

## 快速开始

需要 Python 3.10+。以下命令在 Windows PowerShell、项目根目录执行。

### 1. 安装面板环境

```powershell
git clone https://github.com/mohu233/bilibili-spider.git
cd bilibili-spider
python -m venv .venv-panel
.\.venv-panel\Scripts\python.exe -m pip install -r requirements-panel.txt
```

面板 HTTP 服务与数据库使用 Python 标准库；头像校验和推理使用 Pillow、NumPy、ONNX Runtime。面板默认使用 CPU，无需 CUDA。旧 YOLO/Excel 工具依赖较多，建议使用独立环境，见 [旧版 CLI 手册](MANUAL.md)。

### 2. 准备头像模型

默认模型是 DeepGHS `mobilenetv3_v0.1_dist`。权重不随源码发布，需要同时准备 `model.onnx` 和 `meta.json`：

```powershell
$modelDir = 'models/mobilenetv3_v0.1_dist'
$revision = '8fceaaf12270ea680e81fd7984c96fb9abec7523'
$modelBase = "https://huggingface.co/deepghs/anime_furry/resolve/$revision/mobilenetv3_v0.1_dist"
New-Item -ItemType Directory -Force -Path $modelDir | Out-Null
Invoke-WebRequest "$modelBase/meta.json" -OutFile "$modelDir/meta.json"
Invoke-WebRequest "$modelBase/model.onnx" -OutFile "$modelDir/model.onnx"
```

下载来源为 [DeepGHS/anime_furry](https://huggingface.co/deepghs/anime_furry)，这里固定版本以便复现。其他模型路径、依赖和实验工具见 [模型说明](models/README.md)。

### 3. 启动

```powershell
.\.venv-panel\Scripts\python.exe panel.py --port 8767
```

浏览器打开 **[http://127.0.0.1:8767](http://127.0.0.1:8767)**。也可运行 `./start_panel.ps1`；需要指定解释器时，先设置 `BILIBILI_PANEL_PYTHON` 环境变量。

服务仅绑定本机地址。启动服务不会自动开始采集，服务重启后也需要在页面重新启动任务。

### 4. 首次使用

1. 在“Cookie 池与设置”添加完整 Cookie，点击验证。也可显式导入本机根目录的 `cookies.json`；不要使用包含省略号的截断值。
2. 添加 UID。默认进入候选池；只有已由你确认身份的账号才选择直接作为种子。
3. 选择采集顺序、证据模式、并发上限与请求等待时间。
4. 点击开始。需要连续处理时启用“连续采集”；人工待审核记录不会阻塞自动队列。

账号凭据以明文保存在本机 SQLite 中，列表、响应和日志不回显原文。Cookie 输入区可见原文，请勿将其录屏、截图或上传。

## 判定与采集流程

```text
录入 / 历史 UID / 关系发现
             ↓
          候选 UID
             ↓ 资料 + 头像 + 最多 3 个视频的标题/标签
     ┌───────┼────────┐
     ↓       ↓        ↓
 Furry Yes  待人工审核  Furry No
     ↓       ↓人工确认
 待遍历种子池 ←┘
     ↓ 选择匹配分最高的种子
 获取公开粉丝/关注 → 检查关联候选 → 结束本轮 → 下一种子
```

- **候选优先**：默认先按入库顺序清理全局自动待判定候选，积压清空后继续种子周期。
- **种子周期**：先完成当前种子的关系获取与关联账号自动检查，再选择下一位最高分种子。
- **快速判定**：简介或头像已给出强信号时跳过视频；需要视频时尽量减少后续非必要标签请求。
- **完整采样**：即使已命中，仍检查简介、头像与最多 3 个近期视频的标题及标签。

当前采用“任一强信号即可确认”的宽松规则，头像达到阈值也可自动确认。自动排除需要必要证据完整成功、文本无相关线索且头像低分；信息缺失或不明确的记录留待人工审核。人工结果始终优先。

Furry 匹配分为头像 40 分、简介 20 分、视频标题 20 分、标签 20 分，用于选择种子；另有独立的图谱发现收益分。完整规则见 [面板手册](PANEL.md#评分规则)。

## 图片批量审核

候选页和 Furry Yes 页均可进入。Yes 页可集中复核“仅凭头像自动确认、尚未人工判定”的记录。

| 模式 | 点击图片 | 未标记图片在提交时 |
| --- | --- | --- |
| 排除模式 | 深色遮罩、红框和 X，标记排除 | 确认 Furry |
| 选中模式 | 绿框和 ✓，标记确认 | 排除 |

只有拖入下方暂缓区并松开才会手动暂缓；右键与框外松开不改变判定。失败或超时图片会自动暂缓，暂缓项不保存判定。点击“确定并下一页”才统一提交本页；关闭窗口不提交。默认每页 32 张，可设 8–96 张。

**重启服务会使尚未提交的图片审核批次失效**，重启前先提交当前页。

## 默认配置与请求调度

以下是新数据库默认值，已有本机设置不会被这些默认值覆盖。

| 设置 | 默认值 | 范围 / 含义 |
| --- | --- | --- |
| 采集顺序 | 候选优先 | 可改为种子周期 |
| 证据模式 | 快速判定 | 可改为完整采样 |
| 每批 UID 上限 | 10 | 1–500；连续运行自动衔接批次 |
| 每方向新增页数 | 2 | 1–20；每页请求 50 条，进度保留 |
| 判定线程上限 | 1 | 0 自动，或 1–20；受可用账号数限制 |
| 每账号 API 前等待 | 1.5 秒 | 0.5–30 秒；含首次请求，网络耗时另计 |
| 连续采集 | 关闭 | 页面手动启动与暂停 |
| 头像模型 | MobileNetV3 | 可切换 CAFormer / 原 YOLO |
| 自动确认阈值 | 0.85 | 0.50–0.99 |

同一账号的 API 串行执行“完整等待 → 请求 → 响应”，不同账号可以并行。头像不带 Cookie、无固定等待，独立最多 4 并发。多份同一账号 Cookie 不会变成多条账号请求通道。

平台风控或登录失效会停止整轮，不自动换号继续。HTTP 412/429 等停止条件至少冷却 5 分钟，并遵守更长的 `Retry-After`。这些是项目策略，**不是 B 站公布或保证的安全频率**。详细耗时拆分和预算见 [PERFORMANCE.md](PERFORMANCE.md)。

## 数据与目录

```text
panel.py                    当前面板入口
start_panel.ps1             Windows 启动脚本
requirements-panel.txt      面板图像依赖
dashboard/
  web.py                    本机 HTTP API 与访问限制
  store.py                  SQLite、队列、证据、评分
  collector.py              采集调度、视频采样、模型推理
  transport.py              每账号等待、下载与错误处理
  image_review.py           图片批量审核与冲突处理
  static/                   原生 HTML / CSS / JavaScript
  test_*.py, tests.py        离线回归测试
models/                     本机权重；仓库只保留模型说明
data/panel/                 SQLite、头像、锁和备份（不入 Git）
main.py, bfs_crawler.py      独立的旧版 CLI
tools/avatar_benchmark.py   独立模型对比实验
```

面板数据保存在 `data/panel/panel.sqlite3`。备份时先暂停采集并停止服务，或使用 SQLite 在线备份接口；不要在运行中只复制主数据库而遗漏 WAL。

仓库不包含真实 Cookie、采集数据库、UID 数据集、头像、模型权重或本机实验结果。旧 JSON 仅可显式载入为参考，再提取 UID 重新判定；不会自动继承旧分类。导出只生成 JSON，不自动写入网站或排行榜。

## 开发验证

在装有 `requirements-panel.txt` 的环境执行，测试使用临时数据与模拟接口，不需要真实 Cookie：

```powershell
python -B -m unittest dashboard.tests dashboard.test_transport dashboard.test_fast_collection dashboard.test_candidate_priority dashboard.test_image_review tests.test_update_credentials
```

Cookie 输入交互另有 Node.js 测试：

```powershell
node dashboard/test_cookie_paste.cjs
```

## 更多文档

- [PANEL.md](PANEL.md)：页面操作、证据规则、评分、接口与数据边界。
- [功能说明.md](功能说明.md)：当前功能和状态概览。
- [PERFORMANCE.md](PERFORMANCE.md)：调度架构、性能与耗时估算。
- [MANUAL.md](MANUAL.md)：旧版 CLI / JSON 工具手册。
- [models/README.md](models/README.md)：模型文件、下载与实验边界。

项目尚未附带独立的源码 LICENSE 文件；第三方模型、依赖遵循各自的许可，请查阅对应模型卡与项目说明。

## English overview

Furry Lab is a local Bilibili UID collection and manual-review dashboard. Start with `panel.py`, install `requirements-panel.txt`, and download the default ONNX model as described above. The legacy `main.py` workflow uses a separate JSON dataset. Account credentials, collected data, avatars, and model weights are kept local. Model scores are evidence about images, not calibrated probabilities of a user's identity.
