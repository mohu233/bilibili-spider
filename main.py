#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
B站福瑞 BFS 爬虫 — 入口
========================

功能：
  1. BFS 广度优先爬取 B站用户关系网络
  2. YOLO 头像检测 + 关键词匹配，识别福瑞用户
  3. 结果写入 data/ 下的三个 JSON 数据库

使用方式：
  # 启动全自动 BFS 爬虫（从 JSON 数据库加载种子，自动扩散）
  python main.py crawl

  # 单次抓取（给指定 UID 补充数据）
  python main.py fetch 3189735122

  # 加载 YOLO 测试摄像头
  python main.py webcam

  # 扫描三个 JSON 库，补充缺失的 avatar_url / fan_count / follow_count
  python main.py update

  # 强制重新获取所有数据（多线程，自动用所有 Cookie）
  python main.py update --force

  # 只更新超过 7 天没更新的
  python main.py update --days 7

  # 手动指定线程数
  python main.py update --threads 4

  # 人工审核后同步分类（移图片后运行）
  python main.py reconcile

  # 分类同步 + 补齐缺失的图片
  python main.py reconcile --download

  # 试运行，只看结果不改动
  python main.py reconcile --dry-run

  # 统一 JSON 字段格式（补全缺失字段为 null）
  python main.py backfill

  # 按 8 字段标准格式重建 JSON 数据库
  python main.py organize

  # 标记简介命中的图片（重命名为 {uid}简介.jpg）
  python main.py mark-intro
"""

import sys
import os
import json
import time

# 把项目根目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import settings
from bilibili.api import get_up_info, get_up_stat
from classifier.yolo_detector import FurryYOLODetector


def load_cookies():
    """从 cookies.json 加载所有 Cookie 到 settings.COOKIES（支持多账号）"""
    cookie_file = settings.COOKIES_FILE
    if not os.path.exists(cookie_file):
        print(f"[警告] 未找到 {cookie_file}")
        print(f"       请创建 cookies.json，格式：[\"cookie1\", \"cookie2\", ...]")
        return False

    try:
        with open(cookie_file, "r", encoding="utf-8") as f:
            cookies = json.load(f)
    except (json.JSONDecodeError, Exception) as e:
        print(f"[错误] cookies.json 解析失败: {e}")
        return False

    if not isinstance(cookies, list):
        print(f"[错误] cookies.json 必须是一个 JSON 数组")
        return False

    # 过滤空字符串
    valid = [c for c in cookies if c and isinstance(c, str) and "SESSDATA" in c]
    if not valid:
        print(f"[警告] cookies.json 中没有找到有效的 Cookie（需要包含 SESSDATA）")
        return False

    settings.COOKIES = valid
    print(f"[Cookie] 已加载 {len(valid)} 个 Cookie（共 {len(cookies)} 个条目）")
    for i, c in enumerate(valid):
        print(f"        账号{i + 1}: ...{c[-20:]}")
    return True


def cmd_crawl():
    """启动 BFS 爬虫（多线程，自动按 Cookie 数量开线程）"""
    load_cookies()
    if not settings.COOKIES:
        print("[错误] 没有有效的 Cookie！多线程爬虫需要 Cookie")
        print(f"  请在 {settings.COOKIES_FILE} 中填入 Cookie")
        return

    from bfs_crawler import BFSCrawler
    crawler = BFSCrawler()
    crawler.run()


def cmd_fetch():
    """单次抓取指定 UID 的信息"""
    if len(sys.argv) < 3:
        print("用法: python main.py fetch <UID>")
        return

    uid = int(sys.argv[2])
    print(f"\n正在抓取 UID {uid}…\n")

    info = get_up_info(uid)
    if info:
        print(f"  昵称: {info.get('name')}")
        print(f"  头像: {info.get('face')}")
        print(f"  简介: {info.get('sign')}")
    else:
        print("  [失败] 获取用户信息失败")

    stat = get_up_stat(uid)
    if stat:
        print(f"  粉丝: {stat.get('follower')}")
        print(f"  关注: {stat.get('following')}")


def cmd_webcam():
    """摄像头实时检测（测试 YOLO 模型用）"""
    detector = FurryYOLODetector()

    import cv2
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[错误] 无法打开摄像头")
        return

    print("[YOLO] 摄像头实时检测已启动，按 q 退出")
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # YOLO 推理
        results = detector.model(frame)

        # 绘制标注
        annotated = results[0].plot()
        cv2.imshow("Furry YOLO Detection", annotated)

        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()


def cmd_organize():
    """运行 organize_uids.py 整理 Excel → JSON"""
    os.system(f'"{sys.executable}" organize_uids.py')


def cmd_update():
    """扫描三个 JSON 库，补充缺失的 avatar_url / fan_count / follow_count"""
    from update_uids import main as update_main
    update_main()


def cmd_reconcile():
    """人工审核后同步分类 + 补齐缺失图片"""
    from reconcile import main as reconcile_main
    reconcile_main()


def cmd_backfill():
    """统一所有 JSON 为 8 字段格式，缺失的填 null"""
    from backfill_schema import main as backfill_main
    backfill_main()


def cmd_merge():
    """从 Excel 恢复数据 + 合并 BFS 新发现，不丢失任何数据"""
    from merge_from_excel import main as merge_main
    merge_main()


def cmd_mark_intro():
    """标记简介命中的图片，重命名为 {uid}简介.jpg"""
    from mark_intro_hits import main as mark_main
    mark_main()


def cmd_seed_weight():
    """初始化权重字段 + 重建种子库"""
    from seed_weight import main as seed_main
    seed_main()


def print_help():
    print(__doc__)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print_help()
        sys.exit(1)

    command = sys.argv[1]
    commands = {
        "crawl": cmd_crawl,
        "fetch": cmd_fetch,
        "webcam": cmd_webcam,
        "organize": cmd_organize,
        "update": cmd_update,
        "reconcile": cmd_reconcile,
        "backfill": cmd_backfill,
        "merge": cmd_merge,
        "mark-intro": cmd_mark_intro,
        "seed-weight": cmd_seed_weight,
        "help": print_help,
    }

    if command in commands:
        commands[command]()
    else:
        print(f"未知命令: {command}\n")
        print_help()
        sys.exit(1)
