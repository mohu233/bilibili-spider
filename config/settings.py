"""
B站爬虫 + YOLO 福瑞识别 —— 全局配置
======================================
所有可调参数集中在这里。
"""

import os
import random

# ============================================================
# 路径配置
# ============================================================

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# JSON 数据库目录（由 organize_uids.py 生成）
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

# 三个 UID 数据库
MATCHED_DONE_PATH = os.path.join(DATA_DIR, "matched_done.json")     # 已匹配+已遍历
MATCHED_PENDING_PATH = os.path.join(DATA_DIR, "matched_pending.json")  # 已匹配+待遍历
NOT_MATCHED_PATH = os.path.join(DATA_DIR, "not_matched.json")       # 不符合（跳过库）
ERROR_PATH = os.path.join(DATA_DIR, "error.json")                   # API 请求失败的 UID（跳过不再重试）

# Excel 数据文件
EXCEL_FILE = os.path.join(PROJECT_ROOT, "bilibili_up_export.xlsx")

# WBI 密钥缓存
WBI_CACHE_FILE = os.path.join(PROJECT_ROOT, ".wbi_cache.json")

# YOLO 模型文件
YOLO_MODEL_PATH = os.path.join(PROJECT_ROOT, "models", "furry1500x200.pt")

# 头像缓存目录
AVATAR_DIR = os.path.join(PROJECT_ROOT, "avatars")

# 人工审核图片目录
HIT_DIR = os.path.join(PROJECT_ROOT, "hit")      # 命中的福瑞头像
MISS_DIR = os.path.join(PROJECT_ROOT, "miss")    # 未命中的头像（待人工审核）

# Cookie 文件（JSON 数组，支持多账号并行）
COOKIES_FILE = os.path.join(PROJECT_ROOT, "cookies.json")

# ============================================================
# B站 API 配置
# ============================================================

COOKIES = []  # 运行时由 main.py 从 headers.txt 加载

# 请求间隔（秒），实际会在 [MIN, MAX] 之间随机，避免被限流
REQUEST_INTERVAL_MIN = 0.5
REQUEST_INTERVAL_MAX = 1.0


def random_delay():
    """随机延时 0.5~1.0 秒，防止被 B站 限流"""
    import time
    time.sleep(random.uniform(REQUEST_INTERVAL_MIN, REQUEST_INTERVAL_MAX))

# ============================================================
# BFS 爬虫配置
# ============================================================

# 每轮 BFS 最大爬取节点数
MAX_CRAWL_PER_ROUND = 50

# BFS 最大轮数（0 = 无限循环）
MAX_ROUNDS = 0

# ============================================================
# YOLO 识别配置
# ============================================================

YOLO_CONF_THRESHOLD = 0.5

FURRY_KEYWORDS = [
    "福瑞", "兽设", "Furry", "furry", "FURRY",
    "Fursuit", "fursuit", "FURSUIT", "兽装", "兽兽",
    "兽聚", "装师", "毛装", "毛毛",
]
