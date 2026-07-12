"""
数据完整性检查
===============
对比 Excel 原始数据 vs 三个 JSON 库，检查数据是否有丢失。
"""

import os
import sys
from collections import Counter

# 把项目根目录加入路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import settings
from bilibili.json_db import load_db


def check_file(name, path):
    if not os.path.exists(path):
        print(f"\n{name}: 文件不存在")
        return 0, Counter()

    records = load_db(path)

    total = len(records)
    has_name = sum(1 for r in records if r.get("name"))
    has_hit = sum(1 for r in records if r.get("hit"))
    has_avatar = sum(1 for r in records if r.get("avatar_url"))
    has_intro = sum(1 for r in records if r.get("intro"))
    has_fan = sum(1 for r in records if r.get("fan_count") is not None)
    has_follow = sum(1 for r in records if r.get("follow_count") is not None)
    has_update = sum(1 for r in records if r.get("update_time"))

    hit_dist = Counter()
    for r in records:
        hit = r.get("hit")
        if hit:
            hit_dist[hit] += 1
        else:
            hit_dist["(空)"] += 1

    print(f"\n{'=' * 50}")
    print(f"  {name}")
    print(f"{'=' * 50}")
    print(f"  总记录:      {total}")
    print(f"  有 name:     {has_name}")
    print(f"  有 hit:      {has_hit}")
    print(f"  有 avatar:   {has_avatar}")
    print(f"  有 intro:    {has_intro}")
    print(f"  有 fan:      {has_fan}")
    print(f"  有 follow:   {has_follow}")
    print(f"  有 update:   {has_update}")
    print(f"\n  hit 值分布:")
    for k, v in hit_dist.most_common():
        print(f"    [{k}] → {v}")

    return total, hit_dist


def main():
    print("=" * 60)
    print("  数据完整性检查")
    print("=" * 60)

    total_all = 0
    all_hit = Counter()

    for name, path in [
        ("matched_done.json", settings.MATCHED_DONE_PATH),
        ("matched_pending.json", settings.MATCHED_PENDING_PATH),
        ("not_matched.json", settings.NOT_MATCHED_PATH),
        ("error.json", settings.ERROR_PATH),
    ]:
        t, h = check_file(name, path)
        total_all += t
        all_hit += h

    print(f"\n{'=' * 50}")
    print(f"  四个库总计: {total_all} 条")
    print(f"  总 hit 分布:")
    for k, v in all_hit.most_common():
        print(f"    [{k}] → {v}")
    print(f"{'=' * 50}")

    # 检查 matched_done 是否大量丢失
    done_path = settings.MATCHED_DONE_PATH
    if os.path.exists(done_path):
        done_records = load_db(done_path)
        if len(done_records) < 30000:
            print(f"\n⚠ 注意: matched_done.json 只有 {len(done_records)} 条，")
            print(f"  原始 Excel 应有 ~36796 条，说明数据在 update 过程中被覆盖丢失！")
            print(f"  建议立即重新生成: python main.py organize")


if __name__ == "__main__":
    main()
