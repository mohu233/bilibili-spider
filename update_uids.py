"""
数据更新模块（多线程版）
========================
扫描三个 JSON 数据库，对缺少 avatar_url / fan_count / follow_count 的记录
调用 Bilibili API 补充数据，并写入 update_time。

自动根据 cookies.json 中 Cookie 数量开启对应线程数。

用法：
  python main.py update                  # 全量更新（多线程）
  python main.py update --force          # 强制更新所有
  python main.py update --days 7         # 只更新 7 天前抓的
  python main.py update --threads 2      # 手动指定线程数
"""

import os
import sys
import json
import threading
import traceback
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from config import settings
from bilibili.api import get_up_info, get_up_stat, CookieExpiredError, UserNotFoundError
from bilibili.json_db import load_db, upsert_to_db, remove_from_db, load_uid_set


def scan_missing_records(max_days: Optional[int] = None, force: bool = False) -> List[dict]:
    """扫描 JSON 数据库，找出需要更新的记录。"""
    paths = [
        ("matched_done.json", settings.MATCHED_DONE_PATH),
        ("matched_pending.json", settings.MATCHED_PENDING_PATH),
    ]

    now = datetime.now()
    need_update = []

    error_set = load_uid_set(settings.ERROR_PATH)
    if error_set:
        print(f"  error.json: {len(error_set)} 个报错 UID 已跳过")

    for name, path in paths:
        records = load_db(path)
        print(f"\n扫描 {name} ({len(records)} 条)")

        count = 0
        for rec in records:
            uid = rec.get("uid")
            if not uid:
                continue
            if uid in error_set:
                continue

            missing = []
            if force:
                missing = ["avatar_url", "fan_count", "follow_count"]
            else:
                if not rec.get("avatar_url"):
                    missing.append("avatar_url")
                if rec.get("fan_count") is None:
                    missing.append("fan_count")
                if rec.get("follow_count") is None:
                    missing.append("follow_count")

            if max_days is not None and not missing:
                update_time = rec.get("update_time")
                if update_time:
                    try:
                        dt = datetime.strptime(str(update_time)[:19], "%Y-%m-%d %H:%M:%S")
                        if now - dt < timedelta(days=max_days):
                            continue
                        missing = ["avatar_url", "fan_count", "follow_count"]
                    except (ValueError, TypeError):
                        missing = ["avatar_url", "fan_count", "follow_count"]

            if missing:
                need_update.append({
                    "filepath": path,
                    "uid": uid,
                    "missing": missing,
                    "record": rec,
                })
                count += 1

        print(f"  → 需要更新: {count} 条")

    return need_update


def _worker(chunk: List[dict], cookie: str, thread_id: int, total: int,
            results: dict, lock: threading.Lock, interval: float):
    """
    单线程工作函数：处理分配给自己的 UID 列表。

    Args:
        chunk: 该线程的 UID 列表
        cookie: 该线程使用的 Cookie
        thread_id: 线程编号
        total: 总记录数
        results: 共享结果字典 {"success": int, "error": int}
        lock: 用于线程安全计数
        interval: 请求间隔
    """
    # 按文件路径分组，攒一批写一次盘
    file_batches: Dict[str, List[dict]] = {}
    write_counter = 0

    for i, item in enumerate(chunk):
        uid = item["uid"]
        missing = item["missing"]
        filepath = item["filepath"]
        idx = results.get("_global_idx", 0) + 1
        with lock:
            results["_global_idx"] = idx

        print(f"\n[T{thread_id + 1}][{idx}/{total}] UID {uid}")
        print(f"      缺: {', '.join(missing)}")

        try:
            info = None
            if "avatar_url" in missing:
                info = get_up_info(uid, cookie=cookie)

            stat = None
            if "fan_count" in missing or "follow_count" in missing:
                stat = get_up_stat(uid, cookie=cookie)

            rec = {"uid": uid}
            got_data = False

            if info:
                got_data = True
                rec["name"] = info.get("name", "")
                rec["avatar_url"] = info.get("face") or None
                rec["intro"] = info.get("sign") or None
                print(f"      ├ 昵称: {rec['name']}")
                if rec.get("avatar_url"):
                    print(f"      ├ 头像: ✓ 已获取")
                else:
                    print(f"      ├ 头像: 无")

            if stat:
                got_data = True
                rec["fan_count"] = stat.get("follower")
                rec["follow_count"] = stat.get("following")
                print(f"      ├ 粉丝: {rec['fan_count']}  关注: {rec['follow_count']}")

            if not got_data:
                # 非 -404 的失败（-352、网络错误等），跳过本次，不记录 error.json
                print(f"      └ [跳过] API 无返回，保留数据，下次重试")
                with lock:
                    results["error"] += 1
                continue

            # 按文件路径分组
            if filepath not in file_batches:
                file_batches[filepath] = []
            file_batches[filepath].append(rec)
            write_counter += 1
            with lock:
                results["success"] += 1

            print(f"      └ ✓ 已缓存")

        except UserNotFoundError as e:
            # -404：永久错误，完整记录移到 error.json，从原库移除
            print(f"      └ 用户不存在（-404）→ error.json，并从 {os.path.basename(filepath)} 移除")
            with lock:
                results["error"] += 1
            _move_to_error(uid, filepath)

        except CookieExpiredError:
            print(f"\n  [T{thread_id + 1}][停止] Cookie 已过期！")
            _flush_worker(file_batches)
            with lock:
                results["_cookie_expired"] = True
            return

        except Exception as e:
            # 其他临时错误，跳过本次，不下结论
            print(f"      └ [跳过] {e}，保留数据，下次重试")
            with lock:
                results["error"] += 1

        from config import settings as _s
        _s.random_delay()

        # 每 20 条写一次盘（成功记录）
        if write_counter >= 20:
            _flush_worker(file_batches)
            write_counter = 0

    # 写盘剩下的成功记录
    _flush_worker(file_batches)


def _move_to_error(uid: int, source_filepath: str):
    """
    将永久失效的 UID（-404）从原库完整移到 error.json。

    步骤：
      1. 从源库找出完整记录（保留 name/hit/avatar_url 等所有字段）
      2. 整条记录写入 error.json
      3. 从源库移除该 UID（下次不再加载）
    """
    try:
        all_records = load_db(source_filepath)
        full_record = None
        for r in all_records:
            if r.get("uid") == uid:
                full_record = dict(r)
                break

        if not full_record:
            full_record = {"uid": uid}

        upsert_to_db(settings.ERROR_PATH, [full_record],
                     description="API 请求失败的 UID（自动跳过不再重试）")

        n = remove_from_db(source_filepath, [uid])
        tid = threading.get_ident() % 1000
        fname = os.path.basename(source_filepath)
        has_fields = sum(1 for k in ["name", "hit", "avatar_url"] if full_record.get(k))
        print(f"  [T{tid}] >> error.json +1（{has_fields}个字段），{fname} -{n}（已移除 {uid}）")

    except Exception as e:
        print(f"\n  [操作错误] UID {uid} 处理失败: {e}")


def _flush_worker(file_batches: Dict[str, List[dict]]):
    """将线程缓存的记录写入 JSON"""
    for filepath, records in file_batches.items():
        if records:
            try:
                n = upsert_to_db(filepath, records)
                fname = os.path.basename(filepath)
                tid = threading.get_ident() % 1000
                if n:
                    print(f"\n  [T{tid}] >> 存盘 {fname} 更新 {n} 条")
            except Exception as e:
                print(f"\n  [存盘错误] {e}")
    file_batches.clear()


def update_records_mt(need_update: List[dict], interval: float = 0.5):
    """
    多线程更新入口。自动根据 Cookie 数量决定线程数。

    Args:
        need_update: scan_missing_records 的返回
        interval: 请求间隔
    """
    total = len(need_update)
    if total == 0:
        print("\n所有数据都是最新的，无需更新！")
        return

    cookies = settings.COOKIES
    n_threads = len(cookies)

    if n_threads == 0:
        print("[错误] 没有有效的 Cookie，无法更新")
        return

    print(f"\n{'=' * 50}")
    print(f"开始更新 {total} 条数据 — {n_threads} 线程并行")
    print(f"{'=' * 50}")

    # 把 need_update 均匀分给各线程
    chunks = [[] for _ in range(n_threads)]
    for idx, item in enumerate(need_update):
        chunks[idx % n_threads].append(item)

    for tid, chunk in enumerate(chunks):
        print(f"  线程{tid + 1}: {len(chunk)} 个 UID")

    # 共享结果
    results = {
        "_global_idx": 0,
        "success": 0,
        "error": 0,
        "_cookie_expired": False,
    }
    lock = threading.Lock()

    threads = []
    for tid, (chunk, cookie) in enumerate(zip(chunks, cookies)):
        if not chunk:
            continue
        t = threading.Thread(
            target=_worker,
            args=(chunk, cookie, tid, total, results, lock, interval),
            daemon=True,
        )
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # 报告
    print(f"\n{'=' * 50}")
    if results.get("_cookie_expired"):
        print(f"  [停止] Cookie 过期！部分数据未完成")
    print(f"  更新完成!")
    print(f"  成功: {results['success']}")
    print(f"  失败: {results['error']}")
    print(f"{'=' * 50}")


def main():
    """命令行入口"""
    force = "--force" in sys.argv
    days = None

    for i, arg in enumerate(sys.argv):
        if arg == "--days" and i + 1 < len(sys.argv):
            try:
                days = int(sys.argv[i + 1])
            except ValueError:
                pass

    print("=" * 60)
    print("  数据更新器（多线程版）")
    print("=" * 60)
    print(f"  强制更新: {'是' if force else '否'}")
    print(f"  过期天数: {days if days else '不限'}")
    print()

    from main import load_cookies
    load_cookies()

    if not settings.COOKIES:
        print("[错误] 没有有效的 Cookie！")
        print(f"  请在 {settings.COOKIES_FILE} 中填入 Cookie")
        return

    print(f"  可用 Cookie: {len(settings.COOKIES)} 个 → {len(settings.COOKIES)} 线程")
    print()

    need = scan_missing_records(max_days=days, force=force)
    update_records_mt(need)


if __name__ == "__main__":
    main()
