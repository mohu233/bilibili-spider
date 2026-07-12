"""
数据修正工具
=============
在人工审核 hit/ miss/ 完成后，自动做三件事：

1. 同步分类结果到 JSON 数据库
   - 图片在 hit/ → 更新 JSON 为已匹配
   - 图片在 miss/ + JSON 是关键词命中 → 图片移回 hit/（关键词优先级最高）
   - 图片在 miss/ + JSON 不是关键词命中 → 更新 JSON 为未匹配

2. 补齐缺失的头像图片
   - 扫描三个 JSON 库，对有 avatar_url 但本地没图片的 UID 下载头像

用法：
  python main.py reconcile          # 只做分类同步
  python main.py reconcile --download  # 分类同步 + 补齐图片
  python main.py reconcile --dry-run   # 试运行，只看不改
"""

import os
import sys
import json
import shutil
import time
import traceback
from datetime import datetime
from typing import Dict, List, Set, Optional

import requests

from config import settings
from bilibili.json_db import load_db, upsert_to_db, FileLock, _atomic_write, _load_raw
from bilibili.api import get_up_info


# ============================================================
# 1. 扫描目录
# ============================================================

def scan_image_dir(dir_path: str) -> Set[int]:
    """扫描目录下的 jpg 文件，返回 UID 集合"""
    if not os.path.exists(dir_path):
        return set()
    uids = set()
    for f in os.listdir(dir_path):
        if f.lower().endswith(".jpg"):
            try:
                uid = int(f.rsplit(".", 1)[0])
                uids.add(uid)
            except ValueError:
                continue
    return uids


# ============================================================
# 2. 分类同步
# ============================================================

def reconcile():
    """
    核心修正逻辑。

    Step 1: 扫描 hit/ miss/
    Step 2: 遍历三个 JSON 库的所有记录
    Step 3: 根据图片位置 + hit 值做决策
    """

    hit_dir = settings.HIT_DIR
    miss_dir = settings.MISS_DIR
    os.makedirs(hit_dir, exist_ok=True)
    os.makedirs(miss_dir, exist_ok=True)

    # 扫描图片
    hit_images = scan_image_dir(hit_dir)
    miss_images = scan_image_dir(miss_dir)
    print(f"  hit/  : {len(hit_images)} 张")
    print(f"  miss/ : {len(miss_images)} 张")

    # 加载三个 JSON 库
    db_paths = {
        "matched_done": settings.MATCHED_DONE_PATH,
        "matched_pending": settings.MATCHED_PENDING_PATH,
        "not_matched": settings.NOT_MATCHED_PATH,
    }

    # 读取所有记录到内存
    all_records: Dict[int, dict] = {}  # uid → record
    record_source: Dict[int, str] = {}  # uid → 文件名
    for name, path in db_paths.items():
        for rec in load_db(path):
            uid = rec.get("uid")
            if uid:
                all_records[uid] = rec
                record_source[uid] = name

    # 检查每张图片
    to_update_done: List[dict] = []
    to_update_pending: List[dict] = []
    to_update_skip: List[dict] = []
    to_move_to_hit: List[int] = []  # 需要从 miss 移回 hit 的 UID

    all_uids = hit_images | miss_images

    for uid in sorted(all_uids):
        in_hit = uid in hit_images
        in_miss = uid in miss_images
        rec = all_records.get(uid, {})
        hit_val = str(rec.get("hit", "")).lower() if rec else ""
        is_keyword = "keyword" in hit_val
        is_furry = "furry" in hit_val or is_keyword

        if in_hit:
            # 图片在 hit/ → 人工判定为福瑞
            if not is_furry:
                # 更新为 furry
                rec["hit"] = "furry-manual"
                rec["update_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"  ✓ UID {uid}: hit/有人工确认 → 更新为 furry-manual")
                to_update_done.append(rec)
            else:
                print(f"  - UID {uid}: hit/已确认，跳过")

        elif in_miss:
            # 图片在 miss/
            if is_keyword:
                # 关键词命中的 → 移回 hit/
                to_move_to_hit.append(uid)
                src = os.path.join(miss_dir, f"{uid}.jpg")
                dst = os.path.join(hit_dir, f"{uid}.jpg")
                print(f"  ★ UID {uid}: miss/有关键词命中({hit_val}) → 移回 hit/")
            elif is_furry:
                # YOLO 命中的但被移到 miss/ → 人工判定不是福瑞
                rec["hit"] = "none"
                rec["update_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"  △ UID {uid}: miss/人工判定为非福瑞 → 更新 hit=none")
                to_update_skip.append(rec)
            else:
                print(f"  - UID {uid}: miss/已确认非福瑞，跳过")

    # ===== 执行移动 =====
    for uid in to_move_to_hit:
        src = os.path.join(miss_dir, f"{uid}.jpg")
        dst = os.path.join(hit_dir, f"{uid}.jpg")
        try:
            shutil.move(src, dst)
            print(f"    → 已移动 {uid}.jpg: miss/ → hit/")
        except Exception as e:
            print(f"    [错误] 移动 {uid}.jpg 失败: {e}")

    # ===== 写回 JSON =====
    if to_update_done:
        n = upsert_to_db(settings.MATCHED_DONE_PATH, to_update_done)
        print(f"\n  >> matched_done.json 更新 {n} 条")

    if to_update_pending:
        n = upsert_to_db(settings.MATCHED_PENDING_PATH, to_update_pending)
        print(f"\n  >> matched_pending.json 更新 {n} 条")

    if to_update_skip:
        n = upsert_to_db(settings.NOT_MATCHED_PATH, to_update_skip)
        print(f"\n  >> not_matched.json 更新 {n} 条")

    print(f"\n  分类同步完成！")
    print(f"    移回 hit/: {len(to_move_to_hit)} 张")
    print(f"    更新 json: {len(to_update_done) + len(to_update_skip)} 条")


# ============================================================
# 3. 补齐缺失图片
# ============================================================

def download_missing_avatars(max_count: int = 100):
    """
    扫描 JSON 数据库，对有 avatar_url 但本地没有图片的 UID 下载头像。
    只扫描 matched_done / matched_pending 中已判定的福瑞用户，直接放进 hit/。
    不下载 not_matched.json。
    """
    hit_dir = settings.HIT_DIR
    miss_dir = settings.MISS_DIR
    os.makedirs(hit_dir, exist_ok=True)

    db_paths = [
        ("matched_done", settings.MATCHED_DONE_PATH),
        ("matched_pending", settings.MATCHED_PENDING_PATH),
    ]

    # 收集需要下载的 UID
    to_download: List[dict] = []
    for name, path in db_paths:
        records = load_db(path)
        for rec in records:
            uid = rec.get("uid")
            avatar_url = rec.get("avatar_url")
            if not uid or not avatar_url:
                continue
            if os.path.exists(os.path.join(hit_dir, f"{uid}.jpg")):
                continue
            if os.path.exists(os.path.join(miss_dir, f"{uid}.jpg")):
                continue
            to_download.append(rec)
            if len(to_download) >= max_count:
                break
        if len(to_download) >= max_count:
            break

    if not to_download:
        print("  ✓ 所有图片已存在，无需下载")
        return

    print(f"\n  需要下载: {len(to_download)} 张，直接放入 hit/")

    downloaded = 0
    errors = 0

    for i, rec in enumerate(to_download):
        uid = rec["uid"]
        avatar_url = rec["avatar_url"]
        name = rec.get("name", "")
        target_path = os.path.join(hit_dir, f"{uid}.jpg")

        print(f"  [{i+1}/{len(to_download)}] UID {uid} ({name})")

        try:
            resp = requests.get(avatar_url, timeout=15)
            if resp.status_code == 200:
                with open(target_path, "wb") as f:
                    f.write(resp.content)
                size_kb = os.path.getsize(target_path) / 1024
                print(f"    → hit/{uid}.jpg ({size_kb:.0f} KB)")
                downloaded += 1
            else:
                print(f"    [跳过] HTTP {resp.status_code}")
                errors += 1
        except Exception as e:
            print(f"    [错误] {e}")
            errors += 1

        time.sleep(0.5)

    print(f"\n  图片补齐完成: 成功 {downloaded}, 失败 {errors}")


# ============================================================
# 主入口
# ============================================================

def main():
    dry_run = "--dry-run" in sys.argv
    do_download = "--download" in sys.argv

    print("=" * 60)
    print("  数据修正工具")
    print("=" * 60)
    if dry_run:
        print("  模式: 试运行（只看不改）")
    if do_download:
        print("  模式: 分类同步 + 图片补齐")
    print()

    # Step 1: 分类同步
    print("[Step 1/{}] 分类同步".format("2" if do_download else "1"))
    print("-" * 40)
    if dry_run:
        print("  [试运行] 跳过同步操作")
    else:
        reconcile()

    # Step 2: 图片补齐
    if do_download:
        print(f"\n[Step 2/2] 图片补齐")
        print("-" * 40)
        if dry_run:
            print("  [试运行] 跳过下载")
        else:
            from main import load_cookies
            load_cookies()
            download_missing_avatars()

    print(f"\n{'=' * 60}")
    print(f"  完成！")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
