"""
去重脚本：移除 matched_pending.json 中重复的 UID
保留 seed_weight 最高的那条
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import settings
from bilibili.json_db import load_db, FileLock, _atomic_write, _load_raw

path = settings.MATCHED_PENDING_PATH
with FileLock(path + ".lock"):
    db = _load_raw(path)
    uids = db.get("uids", [])
    before = len(uids)

    seen = {}
    for rec in uids:
        uid = rec.get("uid")
        if uid is None:
            continue
        if uid in seen:
            if (rec.get("seed_weight") or 0) > (seen[uid].get("seed_weight") or 0):
                seen[uid] = rec
        else:
            seen[uid] = rec

    db["uids"] = list(seen.values())
    db["meta"]["total_count"] = len(db["uids"])

    after = len(db["uids"])
    _atomic_write(path, db)

print(f"去重前: {before} 条，去重后: {after} 条，移除 {before - after} 条重复")
