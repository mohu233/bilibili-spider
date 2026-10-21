# 旧版 CLI 与 JSON 数据操作手册

本文适用于 `main.py`、`bfs_crawler.py` 及旧版 JSON 整理脚本。当前 Furry Lab 面板的安装和操作见 [README](README.md) 与 [PANEL](PANEL.md)。

两套流程独立：CLI 读取 `config/settings.py` 和根目录 `cookies.json`，使用 `data/*.json`；面板使用 `data/panel/panel.sqlite3`。CLI 不会修改面板中的人工判定、账号设置或采集任务；面板导入历史参考也不会自动继承旧分类。避免同时运行两套采集，以免请求叠加。

## 1. 环境与文件

在项目根目录创建独立环境并安装旧版依赖。以下为 Windows PowerShell 示例：

```powershell
python -m venv .venv-legacy
.\.venv-legacy\Scripts\python.exe -m pip install -r requirements.txt
.\.venv-legacy\Scripts\python.exe main.py help
```

后续示例中的 `python` 指此环境的解释器。可激活环境后使用，或替换成 `.\.venv-legacy\Scripts\python.exe`。旧版依赖包括 Ultralytics、OpenCV、requests、Pillow、NumPy 和 openpyxl；Excel 整理与合并脚本另需：

```powershell
python -m pip install pandas
```

YOLO 功能需要 `models/furry1500x200.pt`。权重的来源、许可和模型准备说明见 [README](README.md)；安装 Python 依赖不会自动准备该文件。模型检测的是头像内容，分数不能证明账号身份。

从 `cookies.example.json` 复制一份为 `cookies.json`，在本机填写完整 Cookie。旧 CLI 只接受字符串数组，且每项须包含 `SESSDATA`；面板支持的分组对象格式不适用于此处。不要把真实凭据提交到仓库或公开日志。旧 CLI 加载凭据时会打印末尾片段，分享终端输出前应去除这些片段。

## 2. 配置与数据

旧版配置集中在 `config/settings.py`：

| 配置 | 用途 |
| --- | --- |
| `COOKIES_FILE` | 根目录 Cookie 文件 |
| `YOLO_MODEL_PATH`、`YOLO_CONF_THRESHOLD` | YOLO 权重位置与检测阈值 |
| `FURRY_KEYWORDS` | 简介关键词 |
| `EXCEL_FILE` | Excel 整理、合并的输入文件 |
| `MATCHED_DONE_PATH`、`MATCHED_PENDING_PATH` | 已匹配记录与待遍历种子 |
| `NOT_MATCHED_PATH`、`ERROR_PATH` | 排除记录与失败记录 |
| `HIT_DIR`、`MISS_DIR`、`AVATAR_DIR` | 图片目录 |
| `REQUEST_INTERVAL_MIN`、`REQUEST_INTERVAL_MAX` | 旧流程调用的随机等待设置 |

这些配置不等同于面板的请求调度设置。旧脚本有各自的请求、等待与错误处理实现；不能将当前面板“每次 API 前等待、风控整轮暂停”的保证套用到所有旧命令，也没有任何配置值能保证不受平台限流。

旧 JSON 库采用对象结构，包含 `meta` 和 `uids` 数组：

| 文件 | 旧流程用途 |
| --- | --- |
| `data/matched_pending.json` | 待遍历的已匹配 UID，作为 BFS 初始队列 |
| `data/matched_done.json` | 旧流程已处理的匹配记录 |
| `data/not_matched.json` | 不匹配、后续跳过的记录 |
| `data/error.json` | 失败后进入跳过集合的记录 |
| `hit/`、`miss/` | 旧分类对应的本地图片 |
| `avatars/`、`_cache/` | 图片或临时缓存 |

常用记录字段为 `uid`、`name`、`hit`、`avatar_url`、`intro`、`fan_count`、`follow_count`、`seed_weight`、`parent_seed`、`discovered_uids`、`update_time`。其中人数统计与实际获取的关系条数不同；旧文件中的 `done` 也不能作为完整关系网络的证明。

整理、合并、字段补齐、权重重建和分类同步会写回旧数据。先停止 CLI 任务并备份相关 JSON、Excel 和图片，再执行这些命令。文件工具提供的锁与轮转备份不替代独立备份。

## 3. 采集与更新命令

| 命令 | 实际行为 |
| --- | --- |
| `python main.py help` | 打印主入口说明 |
| `python main.py fetch <UID>` | 查询指定 UID 的资料与粉丝/关注统计，不执行 BFS |
| `python main.py crawl` | 加载 `cookies.json`，从旧 `matched_pending.json` 建立队列并持续处理 |
| `python main.py update` | 扫描旧匹配库与排除库，补充缺失的资料字段 |
| `python main.py update --force` | 对扫描到的记录强制重新获取资料 |
| `python main.py update --days 7` | 在缺失项之外，也更新超过指定天数未更新的记录 |
| `python main.py webcam` | 使用摄像头预览 YOLO 检测，按 `q` 退出 |

`crawl` 没有新增种子或指定种子的命令行参数，需要先准备旧 JSON 队列，例如从已有 Excel 执行 `organize`。空队列不会自动从面板取得候选。

旧 BFS 读取简介进行关键词匹配，未命中时尝试 YOLO 头像识别；确认匹配后才获取关系并继续发现 UID。关键词匹配器虽然提供昵称和标签方法，当前 BFS 实际传入的是简介，不能描述成已采集近期视频标题与标签。关系调用对粉丝、关注分别使用 `max_pages=2`，每页请求 50 条，隐私和接口限制可能使实际结果更少。

旧 BFS 和更新器按已加载 Cookie 条目创建线程，不使用面板的 20 线程上限或每账号调度器。`update` 实际解析 `--force` 和 `--days`，**不支持 `--threads`**；源码旧注释中的该参数未实现。增加凭据数量不保证速度、权限或接口可用性。

运行中的采集可按 `Ctrl+C` 停止；异常退出后先核对已保存 JSON 与失败记录，再决定如何恢复。网络错误、权限不足、隐私限制及列表为空应结合实际日志判断，不能互相替代。

## 4. 数据整理命令

| 命令 | 写入范围与注意事项 |
| --- | --- |
| `python main.py organize` | 读取配置的 Excel，重建旧 JSON 分类文件；需要 pandas |
| `python main.py merge` | 合并 Excel 基础资料与已有旧 JSON 数据；需要 pandas |
| `python main.py backfill` | 对四个旧 JSON 库补齐并整理标准字段，缺失信息仍为 `null` 或默认值 |
| `python main.py seed-weight` | 补充旧权重字段，重建待遍历种子库 |
| `python main.py seed-weight --repair` | 根据保存的发现 UID 去重并修复旧权重 |
| `python main.py seed-weight --reset` | 清零旧权重及关联权重字段，并重建种子库 |
| `python main.py mark-intro` | 将 `hit` 字段含“简介”的对应图片标记为带“简介”后缀的文件名 |

`organize` 和 `merge` 优先使用 `EXCEL_FILE`，并有历史文件名回退查找。对新资料应明确设置输入路径，不依赖本机旧文件名。`seed-weight` 使用的是旧版权重体系，不是面板的 Furry 匹配分或有上限的图谱收益分。

## 5. 旧图片分类同步

```powershell
python main.py reconcile --dry-run
python main.py reconcile
python main.py reconcile --download
```

- `--dry-run` 跳过同步和下载步骤，不写判定；当前实现只报告执行模式，不生成逐 UID 差异预览。
- `reconcile` 扫描 `hit/`、`miss/` 的纯数字文件名 JPEG，依据图片位置和旧 `hit` 字段写回旧 JSON。
- 旧逻辑保留关键词优先行为：被识别为关键词命中的记录放入 `miss/` 后，可能被移动回 `hit/`，不能视为人工排除始终优先。
- `--download` 在同步后补充匹配库中缺失的头像；当前函数默认最多下载 100 张，不包含排除库。

带“简介”后缀的图片不属于同步扫描器支持的纯数字文件名。需要可靠人工确认、排除及批量审核时，使用 [面板审核](PANEL.md)；其结果保存在 SQLite 中，与移动旧图片的操作相互独立。

## 6. 当前推荐入口

新采集、分页续采、失败状态区分、20 线程设置、每账号请求前等待、完整证据展示和图片批量审核均以当前面板为准。完整功能边界见 [功能说明](功能说明.md)，速度与请求预算见 [PERFORMANCE](PERFORMANCE.md)。旧脚本保留用于维护历史资料，不会自动同步网站或榜单。

## 7. 独立 Excel 更新脚本

`update/bilibili.py` 保留旧 Excel 更新流程，输入文件位于 `update/` 目录。凭据优先读取本机 `BILIBILI_COOKIE` 环境变量，否则读取项目根目录 `cookies.json` 字符串数组；不要编辑源码填入真实 Cookie。此脚本不使用面板账号池或面板调度器。
