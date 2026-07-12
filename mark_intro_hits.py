"""
标记简介命中的图片
==================
扫描 JSON 数据库中 hit 字段包含「简介」的记录，到 hit/ 目录下找到对应图片，
重命名为 {uid}简介.jpg，方便人工筛选时区分哪些是关键词误判。

为什么需要这个功能：
  关键词匹配（简介/昵称）的优先级 > YOLO 图片检测。
  简介命中可能会误判（比如简介写了"不是福瑞"但包含了"福瑞"关键词），
  重命名后可以在文件管理器里快速筛选。

用法：
  python main.py mark-intro
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import settings
from bilibili.json_db import load_db


def main():
    hit_dir = settings.HIT_DIR
    if not os.path.exists(hit_dir):
        print(f"[错误] hit/ 目录不存在: {hit_dir}")
        return

    print("=" * 60)
    print("  标记简介命中图片")
    print("=" * 60)

    # 扫描 JSON 库
    db_paths = [
        ("matched_done.json", settings.MATCHED_DONE_PATH),
        ("matched_pending.json", settings.MATCHED_PENDING_PATH),
    ]

    matched = 0
    renamed = 0
    skipped_no_file = 0
    already_renamed = 0

    for name, path in db_paths:
        records = load_db(path)
        for rec in records:
            hit = rec.get("hit", "") or ""
            uid = rec.get("uid")
            if not uid:
                continue
            # 检查 hit 是否包含「简介」
            if "简介" not in hit:
                continue

            matched += 1
            new_style = os.path.join(hit_dir, f"{uid}简介.jpg")
            old_style = os.path.join(hit_dir, f"{uid}.jpg")

            # 如果已经是 简介.jpg 命名（BFS新爬的），跳过
            if os.path.exists(new_style):
                already_renamed += 1
                continue

            # 只有 uid.jpg 存在才需要重命名（旧版数据）
            if not os.path.exists(old_style):
                skipped_no_file += 1
                continue

            try:
                os.rename(old_style, new_style)
                print(f"  ✓ {uid}.jpg → {uid}简介.jpg  (hit: {hit})")
                renamed += 1
            except OSError as e:
                print(f"  ✗ {uid}.jpg 重命名失败: {e}")

    print(f"\n{'=' * 60}")
    print(f"  完成！")
    print(f"  简介命中记录:  {matched} 条")
    print(f"  已重命名:      {renamed} 张")
    print(f"  无图片文件:    {skipped_no_file} 张（不在 hit/ 中）")
    print(f"  已存在跳过:    {already_renamed} 张")
    print(f"{'=' * 60}")
    print(f"\n提示: 在 hit/ 目录下按名称排序，带「简介」的就是关键词命中的图片")
    print(f"      人工确认不是福瑞 → 移到 miss/，再运行 python main.py reconcile")


if __name__ == "__main__":
    main()
