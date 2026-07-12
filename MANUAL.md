# Bilibili Spider 操作手册

## 项目概述

BFS 广度优先爬虫 + YOLO 头像识别 + 关键词匹配，自动从 B 站用户关系网络中识别福瑞用户。

**核心流程：** 种子 UID → 获取粉丝/关注列表 → YOLO+关键词检测 → 命中的加入待遍历队列 → 继续扩散

```
种子 UID → API 获取关注/粉丝 → YOLO 头像检测 + 关键词匹配
    ├─ 命中且有数据 → matched_done.json（可做种子，去重库）
    ├─ 命中但无数据 → matched_pending.json（待 BFS 遍历）
    └─ 未命中       → not_matched.json（跳过标记）
```

---

## 环境准备

### 依赖安装

```bash
pip install -r requirements.txt
```

核心依赖：`requests` `openpyxl` `ultralytics` `opencv-python` `pillow` `numpy`

### Cookie 配置

1. 在浏览器登录 B 站，打开开发者工具 → Application → Cookies → 复制完整的 Cookie 字符串
2. 在项目根目录创建 `cookies.json`（参考 `cookies.example.json`）：

```json
["完整的Cookie字符串1", "完整的Cookie字符串2"]
```

Cookie 数量决定并行线程数。每个 Cookie 字符串必须包含 `SESSDATA` 字段。

### 模型文件

确保 `models/furry1500x200.pt` 存在（YOLO 训练好的福瑞头像识别模型）。

---

## 新项目首次启动

```
第 1 步：整理 Excel → JSON       python main.py organize
第 2 步：检查数据完整性           python check_stats.py
第 3 步：补充缺失字段             python main.py update
第 4 步：统一字段格式             python main.py backfill
第 5 步：初始化种子权重 + 重建种子库  python main.py seed-weight
第 6 步：启动 BFS 爬虫            python main.py crawl
```

### 各步说明

**第 1 步 — `python main.py organize`**
从 `bilibili_up_export.xlsx` 读取 UID 数据，按是否有粉丝/关注数分入 `matched_done.json` 和 `matched_pending.json`。

**第 2 步 — `python check_stats.py`**
对比四个 JSON 数据库的字段覆盖率，检查数据是否丢失。

**第 3 步 — `python main.py update`**
扫描 `matched_done.json` 和 `matched_pending.json`，调用 B 站 API 补充缺失的 `avatar_url` / `fan_count` / `follow_count`，多线程并行。

**第 4 步 — `python main.py backfill`**
统一所有 JSON 记录为 11 个标准字段，缺失的填 `null`。

**第 5 步 — `python main.py seed-weight`**
为每条记录添加 `seed_weight` / `parent_seed` / `discovered_uids` 字段，清理 `matched_pending.json` 中的重复和已处理 UID，按权重排序。

**第 6 步 — `python main.py crawl`**
启动 BFS 爬虫，自动从种子库扩散。会持续运行，按 `Ctrl+C` 停止。

---

## 工作流与典型场景

### 日常爬取

```bash
python main.py crawl          # 持续 BFS 扩散
```

爬虫每次会：
1. 从 `matched_pending.json` 取权重最高的种子
2. 获取该 UID 的关注和粉丝列表
3. 逐个检测头像（YOLO）和简介/昵称（关键词）
4. 命中 → 写入 `matched_done.json` + 加入队列继续扩散
5. 不命中 → 写入 `not_matched.json`
6. 遇到错误 → 写入 `error.json`，不再重试

### 人工审核后同步

当你在 `hit/` 或 `miss/` 目录下人工移动图片后：

```bash
python main.py reconcile                  # 同步分类结果到 JSON
python main.py reconcile --download       # 同时补全缺失的图片
python main.py reconcile --dry-run        # 试运行，只看不改
```

`reconcile` 的行为：
- 图片在 `hit/` → JSON 更新为 `furry-manual`
- 图片在 `miss/` + 关键词命中 → 移回 `hit/`（关键词优先级最高）
- 图片在 `miss/` + 仅 YOLO 命中 → JSON 更新为 `none`（人工判定非福瑞）

### 数据维护

```bash
python main.py update             # 扫描补全缺失字段
python main.py update --force     # 强制刷新所有数据
python main.py update --days 7    # 仅更新 7 天前的记录
python main.py update --threads 2 # 手动指定线程数
python main.py merge              # 从 Excel 恢复基础数据 + 合并 BFS 新发现
python main.py mark-intro         # 标记简介命中的图片（重命名为 {uid}简介.jpg）
```

### 图片目录管理

- `hit/` — 命中（福瑞）头像
- `miss/` — 未命中头像（人工审核后移走）
- `_cache/` — 下载缓存（不入 Git）

**标注简介命中图片：**
```bash
python main.py mark-intro
# 将 hit/ 里关键词命中的图片重命名为 uid简介.jpg，方便人工区分
```

### 种子权重体系

```bash
python main.py seed-weight           # 初始化权重字段
python main.py seed-weight --reset   # 重置所有权重为 0
python main.py seed-weight --repair  # 从 discovered_uids 重建权重
```

权重规则：种子 A 找到 10 个福瑞 → A 的权重 = 10，发现的每个 B 初始权重 = 1。BFS 优先遍历权重高的种子。

### 数据合并

如果旧版 Excel 有完整的基础数据，而 JSON 数据库经过了多次 BFS 更新，用 `merge` 安全合并：

```bash
python main.py merge
# 以 Excel 为基础骨架，JSON 的非空字段覆盖上去
# BFS 新发现的 UID（不在 Excel 中）追加保留
# not_matched.json / error.json 完全不动
```

---

## 完整命令速查

| 命令 | 说明 |
|------|------|
| `python main.py crawl` | 启动 BFS 爬虫 |
| `python main.py fetch <UID>` | 单次获取指定 UID 信息 |
| `python main.py webcam` | 摄像头实时 YOLO 检测（测试模型） |
| `python main.py organize` | Excel → JSON 数据库初始化 |
| `python main.py update` | 扫描补全缺失字段（多线程） |
| `python main.py update --force` | 强制刷新全部数据 |
| `python main.py update --days N` | 仅更新超过 N 天的记录 |
| `python main.py reconcile` | 人工审核后分类同步 |
| `python main.py reconcile --download` | 同步 + 下载缺失头像 |
| `python main.py reconcile --dry-run` | 试运行，只看不改 |
| `python main.py backfill` | 统一 JSON 字段格式 |
| `python main.py merge` | Excel + JSON 安全合并 |
| `python main.py mark-intro` | 标记简介命中图片 |
| `python main.py seed-weight` | 初始化种子权重 + 重建种子库 |
| `python main.py seed-weight --reset` | 重置所有权重 |
| `python main.py seed-weight --repair` | 修复权重 |
| `python check_stats.py` | 检查数据完整性 |

---

## 独立工具脚本

| 脚本 | 说明 |
|------|------|
| `python check_stats.py` | 四个 JSON 库的字段覆盖率统计 |
| `python dedup_pending.py` | `matched_pending.json` 去重 |
| `python test_yolo.py <图片>` | YOLO 模型单元测试 |
| `python tests/test_yolo.py <图片>` | 同上（tests 目录版） |

---

## 数据文件说明

### JSON 数据库（运行时生成于 `data/`）

| 文件 | 内容 |
|------|------|
| `matched_done.json` | 已确认福瑞 + 有粉丝/关注数据（种子库 + 去重库） |
| `matched_pending.json` | 已确认福瑞但暂无数据（待 BFS 遍历队列） |
| `not_matched.json` | 非福瑞用户（跳过标记，防止重复消耗） |
| `error.json` | API 请求永久失败的 UID（404 封号等，不再重试） |

### 每条记录的 11 个标准字段

```json
{
  "uid": 123456,
  "name": "昵称",
  "hit": "furry" | "furry-manual" | "keyword-简介" | "none" | null,
  "avatar_url": "https://...",
  "intro": "个人简介",
  "fan_count": 1000,
  "follow_count": 200,
  "seed_weight": 5,
  "parent_seed": 789,
  "discovered_uids": [111, 222, 333],
  "update_time": "2026-07-12 14:30:00"
}
```

### `hit` 字段含义

| 值 | 来源 |
|----|------|
| `furry` | YOLO 模型检测命中 |
| `keyword-昵称` / `keyword-简介` / `keyword-昵称 简介` | 关键词匹配命中 |
| `furry-manual` | 人工审核确认（reconcile） |
| `none` | 人工审核判定非福瑞 |
| `null` | 尚未检测 |

---

## 目录结构

```
bilibili-spider/
├── main.py              # 入口，命令路由
├── bfs_crawler.py       # BFS 爬虫核心（多线程）
├── bilibili/            # B站 API 封装
│   ├── api.py           # get_up_info / get_up_stat / 关注粉丝列表
│   ├── json_db.py       # JSON 数据库读写（带文件锁）
│   ├── wbi_auth.py      # WBI 签名算法
│   └── excel.py         # Excel 辅助
├── classifier/          # 识别模块
│   ├── yolo_detector.py # YOLO 头像检测
│   └── keyword_matcher.py # 关键词匹配
├── config/
│   └── settings.py      # 全局配置（路径/阈值/关键词列表）
├── models/
│   └── furry1500x200.pt # YOLO 模型权重文件
├── data/                # JSON 数据库（运行时生成，不入 Git）
├── tests/               # 单元测试
├── fetch/               # 批量抓取脚本（旧版工具集）
├── update/              # 数据更新脚本（旧版工具集）
├── requirements.txt     # Python 依赖
├── cookies.example.json # Cookie 配置模板
├── .gitignore           # Git 忽略规则
├── README.md            # 项目说明
└── MANUAL.md            # 本操作手册
```

---

## 常见问题

### Q: `cookies.json` 格式不对？
确保是标准 JSON 数组，每个元素是完整的 Cookie 字符串。参考 `cookies.example.json`。

### Q: 提示 Cookie 过期？
B 站 Cookie 中的 `SESSDATA` 有效期通常数周到数月。重新从浏览器复制即可。

### Q: YOLO 模型加载失败？
确认 `models/furry1500x200.pt` 存在，且 `ultralytics` 已安装：`pip install ultralytics`。

### Q: 被限流（-352 错误）？
爬虫内置了 0.5~1.0 秒随机延迟。如果仍被限流：
- 减少 `MAX_CRAWL_PER_ROUND`（`config/settings.py`）默认 50
- 增大 `REQUEST_INTERVAL_MIN` / `REQUEST_INTERVAL_MAX`
- 增加更多 Cookie 分散请求

### Q: 用户不存在（-404 错误）？
该 UID 已注销或封号。爬虫会自动移入 `error.json`，不再重试。

### Q: `matched_done.json` 数据大量丢失？
可能是 `update` 过程中的覆盖问题。用 `python main.py organize` 重新从 Excel 生成，再用 `python main.py merge` 合并。

### Q: 如何只爬取不下载图片？
在 `config/settings.py` 中设置相关参数或修改 BFS 爬虫逻辑。默认会在 YOLO 检测时下载头像。

---

## 技术架构

```
main.py（命令路由）
    ├─ bfs_crawler.py    ← BFS 爬虫主逻辑
    │   ├─ bilibili.api  ← B站 API（WBI 签名 + Cookie）
    │   ├─ classifier.yolo_detector   ← YOLO 模型推理
    │   ├─ classifier.keyword_matcher ← 文本关键词匹配
    │   └─ bilibili.json_db ← 读写 JSON 数据库（文件锁）
    │
    ├─ update_uids.py    ← 补全数据（多线程 API 请求）
    ├─ reconcile.py      ← 人工审核后同步
    ├─ organize_uids.py  ← Excel → JSON
    ├─ merge_from_excel.py ← 安全合并
    ├─ backfill_schema.py ← 字段格式统一
    ├─ seed_weight.py    ← 种子权重管理
    └─ mark_intro_hits.py ← 简介图片标记
```
