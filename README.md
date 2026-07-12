# Bilibili Spider — B站 福瑞用户识别爬虫

BFS 广度优先爬取 B站用户关系网络，结合 YOLO 头像检测 + 关键词匹配，自动识别福瑞用户。

## 功能

- **BFS 爬虫**: 从种子 UID 开始，自动扩散爬取用户关系网络
- **YOLO 识别**: 使用 YOLO 模型检测头像，识别福瑞用户
- **关键词过滤**: 多维度匹配用户简介/昵称中的关键词，补充 YOLO 识别
- **多线程**: 支持多 Cookie 并行爬取，自动管理线程池
- **数据管理**: JSON 数据库存储，支持数据更新、重新整理、分类同步

## 快速开始

```bash
pip install -r requirements.txt
# 把 Cookie 放到 cookies.json (JSON 数组格式)
python main.py crawl
```

## 目录结构

```
bilibili-spider/
├── bilibili/          # B站 API 封装
├── classifier/        # YOLO + 关键词识别
├── config/            # 全局配置
├── models/            # YOLO 模型文件
├── data/              # JSON 数据库 (运行时生成)
├── tests/             # 单元测试
├── fetch/             # 批量抓取脚本
├── update/            # 数据更新脚本
├── main.py            # 入口
├── bfs_crawler.py     # BFS 爬虫核心
└── requirements.txt   # 依赖
```

## 依赖

- Python 3.10+
- PyTorch + Ultralytics (YOLO)
- opencv-python
- requests
- openpyxl

