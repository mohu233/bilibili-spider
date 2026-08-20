# Bilibili Spider — Furry User Identification Crawler

> 📢 **Language**: [English](#english) | [中文](#chinese)

---

<h2 id="english">🎯 English Documentation</h2>

## Overview

**Bilibili Spider** is an automated crawler that identifies furry users on Bilibili (Chinese video platform) using BFS graph traversal, YOLO-based avatar detection, and keyword matching.

## Features

- **BFS Crawler**: Automatically expands user relationship networks starting from seed UIDs
- **YOLO Detection**: Uses YOLO model to detect furry characteristics in user avatars
- **Keyword Filtering**: Multi-dimensional keyword matching against user bio and nickname
- **Multi-threading**: Supports parallel crawling with multiple cookies, automatic thread pool management
- **Data Management**: JSON database storage with data update, reorganization, and synchronization support

## Quick Start

```bash
pip install -r requirements.txt
# Add cookies to cookies.json (JSON array format)
python main.py crawl
```

## Project Structure

```
bilibili-spider/
├── bilibili/          # Bilibili API wrapper
├── classifier/        # YOLO + keyword classification
├── config/            # Global configuration
├── models/            # YOLO model files
├── data/              # JSON database (generated at runtime)
├── tests/             # Unit tests
├── fetch/             # Batch fetching scripts
├── update/            # Data update scripts
├── main.py            # Entry point
├── bfs_crawler.py     # BFS crawler core
└── requirements.txt   # Dependencies
```

## Requirements

- Python 3.10+
- PyTorch + Ultralytics (YOLO)
- opencv-python
- requests
- openpyxl

## Installation

1. Clone the repository:
```bash
git clone https://github.com/mohu233/bilibili-spider.git
cd bilibili-spider
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Configure cookies:
   - Open `cookies.json`
   - Add your Bilibili cookies in JSON array format

4. Run the crawler:
```bash
python main.py crawl
```

## Configuration

Edit `config/` files to customize:
- Crawling parameters (depth, max users)
- YOLO model threshold
- Keyword dictionaries
- Thread pool size

## API Methods

### Crawler
- `crawl()` - Start BFS crawl from seed UIDs
- `update()` - Update existing user data
- `classify()` - Run classification on user data

### Data Management
- `export()` - Export data to Excel/CSV
- `sync()` - Synchronize data across multiple formats

## Testing

```bash
python -m pytest tests/
```

## License

MIT License - See LICENSE file for details

## Disclaimer

This tool is for educational and research purposes only. Users are responsible for complying with Bilibili's Terms of Service and local laws.

---

<h2 id="chinese">🎯 中文文档</h2>

## 项目介绍

**Bilibili Spider** 是一个自动化爬虫工具，使用 BFS 图遍历、YOLO 头像检测和关键词匹配，自动识别 B 站福瑞用户。

## 功能

- **BFS 爬虫**: 从种子 UID 开始，自动扩散爬取用户关系网络
- **YOLO 识别**: 使用 YOLO 模型检测头像，识别福瑞用户特征
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

## 安装步骤

1. 克隆仓库:
```bash
git clone https://github.com/mohu233/bilibili-spider.git
cd bilibili-spider
```

2. 安装依赖:
```bash
pip install -r requirements.txt
```

3. 配置 Cookie:
   - 打开 `cookies.json`
   - 按 JSON 数组格式添加 B 站 Cookie

4. 运行爬虫:
```bash
python main.py crawl
```

## 配置说明

编辑 `config/` 目录下的文件进行自定义配置:
- 爬虫参数（深度、最大用户数）
- YOLO 模型阈值
- 关键词字典
- 线程池大小

## API 方法

### 爬虫操作
- `crawl()` - 从种子 UID 开始 BFS 爬取
- `update()` - 更新已爬取的用户数据
- `classify()` - 对用户数据进行分类识别

### 数据管理
- `export()` - 导出数据为 Excel/CSV 格式
- `sync()` - 同步多个格式的数据

## 测试

```bash
python -m pytest tests/
```

## 许可证

MIT License - 详见 LICENSE 文件

## 免责声明

本工具仅供学习和研究使用。使用者需遵守 B 站服务条款和当地法律法规。
