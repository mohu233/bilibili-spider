"""
BFS 广度优先爬虫（多线程版）
============================
从 matched_pending.json 的待遍历 UID 开始，逐层扩散。
有几个 Cookie 开几个线程，共享一个 BFS 队列。

数据流向：
  队列 → 获取信息 → YOLO + 关键词
    ├── 是福瑞且有粉丝数据  → matched_done.json
    ├── 是福瑞但无粉丝数据  → matched_pending.json
    └── 非福瑞              → not_matched.json

图片存储：
  hit/{uid}.jpg / hit/{uid}简介.jpg    ← 福瑞头像
  miss/{uid}.jpg                       ← 非福瑞头像
"""

import os
import itertools
import queue
import shutil
import threading
from datetime import datetime
from typing import Set, List, Optional, Dict

import requests

from config import settings
from bilibili.api import (
    get_up_info, get_up_stat, get_followings, get_followers,
    CookieExpiredError, UserNotFoundError, RateLimitedError,
)
from bilibili.json_db import load_db, upsert_to_db, remove_from_db, FileLock, _load_raw, _atomic_write
from classifier.yolo_detector import FurryYOLODetector
from classifier.keyword_matcher import KeywordMatcher


# ============================================================
# 全局 YOLO 检测器（线程安全，带锁）
# ============================================================
_yolo_lock = threading.Lock()
_yolo_detector = None


def _get_yolo():
    global _yolo_detector
    if _yolo_detector is None:
        with _yolo_lock:
            if _yolo_detector is None:
                _yolo_detector = FurryYOLODetector()
    return _yolo_detector


# ============================================================
# 爬虫主类
# ============================================================

class BFSCrawler:

    def __init__(self):
        self.done_path = settings.MATCHED_DONE_PATH
        self.pending_path = settings.MATCHED_PENDING_PATH
        self.skip_path = settings.NOT_MATCHED_PATH
        self.error_path = settings.ERROR_PATH

        # 内存集合（线程安全用锁保护）
        self._lock = threading.Lock()
        self._done_set: Set[int] = set()
        self._pending_set: Set[int] = set()
        self._skip_set: Set[int] = set()
        self._error_set: Set[int] = set()
        self._known_furry_set: Set[int] = set()
        self._queued_set: Set[int] = set()
        self._processing_set: Set[int] = set()

        # BFS 共享优先队列（线程安全）：seed_weight 越高越先处理
        self._task_queue = queue.PriorityQueue()
        self._queue_counter = itertools.count()
        self._pending_records_by_uid: Dict[int, dict] = {}
        self._candidate_parents: Dict[int, List[int]] = {}

        # 关键词匹配器（每个线程独立创建）
        self.keyword = KeywordMatcher()

        # 统计数据（线程安全用锁保护）
        self._stats = {
            "round": 0,
            "crawled": 0,
            "furry": 0,
            "not_furry": 0,
            "yolo_hit": 0,
            "keyword_hit": 0,
            "pinned": 0,  # 队列剩余
        }

        # 图片目录
        self.cache_dir = os.path.join(settings.PROJECT_ROOT, "_cache")
        self.hit_dir = settings.HIT_DIR
        self.miss_dir = settings.MISS_DIR
        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(self.hit_dir, exist_ok=True)
        os.makedirs(self.miss_dir, exist_ok=True)

        # 线程运行标志
        self._running = True

    # ------------------------------------------------------------
    # 数据库加载（在主线程执行）
    # ------------------------------------------------------------
    def load_databases(self):
        print("=" * 60)
        print("加载 UID 数据库")
        print("=" * 60)

        done_records = load_db(self.done_path)
        self._done_set = {r["uid"] for r in done_records if r.get("uid")}
        print(f"  matched_done.json    : {len(self._done_set)} 个")

        pending_records = load_db(self.pending_path)
        self._pending_set = {r["uid"] for r in pending_records if r.get("uid")}
        print(f"  matched_pending.json : {len(self._pending_set)} 个")

        skip_records = load_db(self.skip_path)
        self._skip_set = {r["uid"] for r in skip_records if r.get("uid")}
        print(f"  not_matched.json     : {len(self._skip_set)} 个")

        error_records = load_db(self.error_path)
        self._error_set = {r["uid"] for r in error_records if r.get("uid")}
        if self._error_set:
            print(f"  error.json           : {len(self._error_set)} 个")

        # 已确认福瑞的 UID。重复发现这些 UID 时也要给当前种子加权。
        self._known_furry_set = self._done_set | self._pending_set

        # 加载 pending 完整记录（含权重），按权重降序入队
        init_count = 0
        pending_records_by_uid: Dict[int, dict] = {}
        for r in pending_records:
            uid = r.get("uid")
            if uid:
                pending_records_by_uid[uid] = r

        # 按 seed_weight 降序排序，权重高的先处理
        sorted_pending = sorted(
            [r for r in pending_records if r.get("uid") not in self._skip_set and r.get("uid") not in self._error_set],
            key=lambda r: -(r.get("seed_weight") or 0)
        )
        for rec in sorted_pending:
            uid = rec["uid"]
            self._put_task(uid, rec.get("seed_weight") or 0)
            self._queued_set.add(uid)
            init_count += 1
        print(f"  队列初始大小: {init_count}（按权重降序）")
        print()

        # 存储 pending 完整记录供权重查询
        self._pending_records_by_uid = pending_records_by_uid

    # ------------------------------------------------------------
    # 判断是否已处理
    # ------------------------------------------------------------
    def _is_blocked(self, uid: int) -> bool:
        """不应继续作为候选处理的 UID。"""
        return uid in self._skip_set or uid in self._error_set

    def _put_task(self, uid: int, seed_weight: float):
        """按权重入队；PriorityQueue 小值优先，所以取负权重。"""
        self._task_queue.put((-(seed_weight or 0), next(self._queue_counter), uid))

    def _record_seed_hit(self, seed_uid: int, found_uid: int,
                         weight_updates: Dict[int, int],
                         discovery_updates: Dict[int, List[int]]):
        """当前种子发现了一个已确认福瑞：权重 +1，关系列表追加。"""
        seed_rec = self._pending_records_by_uid.get(seed_uid)
        existing = set(seed_rec.get("discovered_uids") or []) if seed_rec else set()
        pending = set(discovery_updates.get(seed_uid, []))
        if found_uid in existing or found_uid in pending:
            return
        weight_updates[seed_uid] = weight_updates.get(seed_uid, 0) + 1
        discovery_updates.setdefault(seed_uid, []).append(found_uid)

    def _requeue_seed(self, uid: int):
        """风控/临时失败时把当前种子放回 pending 队列，不移动到 done。"""
        with self._lock:
            self._processing_set.discard(uid)
            if uid not in self._skip_set and uid not in self._error_set and uid not in self._done_set:
                rec = self._pending_records_by_uid.get(uid, {"uid": uid})
                self._queued_set.add(uid)
                self._put_task(uid, rec.get("seed_weight") or 0)

    def _enqueue_candidate(self, parent_uid: int, child_uid: int, child_weight: float,
                           weight_updates: Dict[int, int],
                           discovery_updates: Dict[int, List[int]]) -> str:
        """
        处理当前种子发现的 UID。

        返回值：
          known   已确认福瑞，立即给当前种子加权
          queued  新候选，加入队列等待关键词/YOLO 判定
          pending 候选已在队列中，记录额外父级，等判定为福瑞后再加权
          blocked 已排除/报错，忽略
        """
        if not child_uid or child_uid == parent_uid:
            return "blocked"

        with self._lock:
            if child_uid in self._known_furry_set:
                self._record_seed_hit(parent_uid, child_uid, weight_updates, discovery_updates)
                return "known"
            if self._is_blocked(child_uid):
                return "blocked"

            if child_uid in self._queued_set or child_uid in self._processing_set:
                parents = self._candidate_parents.setdefault(child_uid, [])
                parents.append(parent_uid)
                return "pending"

            self._queued_set.add(child_uid)
            self._pending_records_by_uid[child_uid] = {
                "uid": child_uid,
                "seed_weight": child_weight,
                "parent_seed": parent_uid,
                "discovered_uids": [],
            }
            self._candidate_parents[child_uid] = [parent_uid]

        self._put_task(child_uid, child_weight)
        return "queued"

    def _credit_candidate_parents(self, uid: int,
                                  weight_updates: Dict[int, int],
                                  discovery_updates: Dict[int, List[int]]):
        """候选 UID 被确认福瑞后，给本轮发现它的所有父级种子加权。"""
        with self._lock:
            parents = list(self._candidate_parents.pop(uid, []))
        for parent_uid in parents:
            self._record_seed_hit(parent_uid, uid, weight_updates, discovery_updates)

    # ------------------------------------------------------------
    # 下载头像
    # ------------------------------------------------------------
    def _download_to_cache(self, uid: int, url: str) -> Optional[str]:
        if not url:
            return None
        cache_path = os.path.join(self.cache_dir, f"{uid}.jpg")
        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
            return cache_path
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                with open(cache_path, "wb") as f:
                    f.write(resp.content)
                return cache_path
        except Exception:
            return None

    def _move_to(self, cache_path: str, uid: int, target_dir: str, suffix: str = "") -> str:
        dest = os.path.join(target_dir, f"{uid}{suffix}.jpg")
        shutil.copy2(cache_path, dest)
        return dest

    # ------------------------------------------------------------
    # 工作线程函数
    # ------------------------------------------------------------
    def _worker(self, cookie: str, thread_id: int):
        """单个工作线程：从共享队列取 UID，爬取并识别"""
        tag = f"T{thread_id + 1}"
        keyword = KeywordMatcher()
        yolo = _get_yolo()

        # 批次缓存（每个线程独立）
        done_batch: List[dict] = []       # 已种完的种子 → matched_done.json
        pending_batch: List[dict] = []    # BFS 新发现的种子 → matched_pending.json
        skip_batch: List[dict] = []
        error_batch: List[dict] = []
        # 权重更新缓存 {parent_uid: +1 次数}
        weight_updates: Dict[int, int] = {}
        # 发现记录缓存 {parent_uid: [discovered_uid, ...]}
        discovery_updates: Dict[int, List[int]] = {}
        batch_counter = 0
        BATCH_FLUSH = 10  # 每 10 条写一次盘

        while self._running:
            try:
                _, _, uid = self._task_queue.get(timeout=3)
            except queue.Empty:
                if self._task_queue.empty():
                    self._flush_batches(done_batch, pending_batch, skip_batch, error_batch)
                    self._flush_weight_updates(weight_updates, discovery_updates)
                    break
                continue

            with self._lock:
                self._queued_set.discard(uid)
                if uid in self._done_set or uid in self._skip_set or uid in self._error_set:
                    self._task_queue.task_done()
                    continue
                self._processing_set.add(uid)
                self._stats["pinned"] = self._task_queue.qsize()

            seq = 0
            with self._lock:
                self._stats["crawled"] += 1
                seq = self._stats["crawled"]

            print(f"\n[{tag}][{seq}] UID {uid}（队列剩余{self._task_queue.qsize()}）")

            # ===== 1. 获取用户信息 =====
            try:
                info = get_up_info(uid, cookie=cookie)
                if not info:
                    print(f"      └ [跳过] 获取信息失败")
                    with self._lock:
                        self._processing_set.discard(uid)
                    self._task_queue.task_done()
                    continue
            except UserNotFoundError:
                print(f"      └ -404 用户不存在 → error.json")
                self._move_to_error(uid, error_batch)
                with self._lock:
                    self._processing_set.discard(uid)
                self._task_queue.task_done()
                continue
            except CookieExpiredError:
                print(f"\n  [{tag}][停止] Cookie 过期！")
                self._running = False
                with self._lock:
                    self._processing_set.discard(uid)
                self._task_queue.task_done()
                break
            except RateLimitedError as e:
                print(f"      └ [风控] {e}，当前种子保留在 matched_pending.json，稍后重试")
                self._requeue_seed(uid)
                self._task_queue.task_done()
                continue
            except Exception as e:
                print(f"      └ [错误] {e}")
                with self._lock:
                    self._processing_set.discard(uid)
                self._task_queue.task_done()
                continue

            name = info.get("name", "")
            avatar_url = info.get("face", "")
            intro = info.get("sign", "")

            print(f"      ├ 昵称: {name}")
            if intro:
                print(f"      ├ 简介: {intro[:120]}{'...' if len(intro) > 120 else ''}")

            # ===== 2. 简介关键词匹配 =====
            kw_result = keyword.full_check(intro=intro)
            keyword_hit = kw_result["is_furry"]
            intro_matched = "intro" in kw_result.get("matched_by", [])
            if keyword_hit:
                print(f"      ├ 简介关键词: ✓ 命中 → {kw_result['keywords']}")
            else:
                print(f"      ├ 简介关键词: × 未命中")

            # ===== 3. 下载头像 =====
            cache_path = self._download_to_cache(uid, avatar_url)
            if cache_path:
                print(f"      ├ 头像: 已下载")
            else:
                print(f"      ├ 头像: 无")

            # ===== 4. YOLO 检测（简介未命中时才启动）=====
            yolo_hit = False
            yolo_conf = 0.0
            if keyword_hit:
                print(f"      ├ YOLO: 跳过（简介已命中）")
            elif cache_path:
                with _yolo_lock:
                    is_furry, yolo_conf, class_id = yolo.predict(cache_path)
                yolo_hit = is_furry
                label = "✓ 福瑞" if is_furry else "× 非福瑞"
                print(f"      ├ YOLO: {label} (conf={yolo_conf:.3f})")
            else:
                print(f"      ├ YOLO: 跳过（无头像）")

            # ===== 5. 综合判断 =====
            is_furry = yolo_hit or keyword_hit
            if yolo_hit and keyword_hit:
                hit_label = "furry-both"
            elif yolo_hit:
                hit_label = "furry-yolo"
            elif keyword_hit:
                hit_label = "furry-keyword"
            else:
                hit_label = "none"

            # ===== 6. 统计 =====
            print(f"      ├ 统计: 查询粉丝/关注数...")
            try:
                stat = get_up_stat(uid, cookie=cookie)
            except RateLimitedError as e:
                print(f"      └ [风控] {e}，当前种子保留在 matched_pending.json，稍后重试")
                self._requeue_seed(uid)
                self._task_queue.task_done()
                continue
            follower = stat.get("follower") if stat else None
            following = stat.get("following") if stat else None

            # ===== 7. 构建记录（含权重）=====
            # 从 pending 中读取已有的权重信息
            current_weight = 0
            current_parent = None
            current_discovered: list = []
            if uid in self._pending_records_by_uid:
                old = self._pending_records_by_uid[uid]
                current_weight = old.get("seed_weight") or 0
                current_parent = old.get("parent_seed")
                current_discovered = old.get("discovered_uids") or []

            rec = {
                "uid": uid,
                "name": name,
                "hit": hit_label,
                "avatar_url": avatar_url or None,
                "intro": intro or None,
                "fan_count": follower,
                "follow_count": following,
                "seed_weight": current_weight,
                "parent_seed": current_parent,
                "discovered_uids": list(current_discovered),
            }

            # ===== 7b. 如果是当前线程处理的福瑞，记录父级种子权重增长 =====
            # （由调用者在外层维护 weight_updates / discovery_updates）

            # ===== 8. 保存图片 =====
            if cache_path:
                suffix = "简介" if intro_matched else ""
                if is_furry:
                    self._move_to(cache_path, uid, self.hit_dir, suffix=suffix)
                    print(f"      ├ 图片 → hit/{uid}{suffix}.jpg")
                else:
                    self._move_to(cache_path, uid, self.miss_dir)
                    print(f"      ├ 图片 → miss/{uid}.jpg")

            # ===== 9. 归类 + BFS 扩散 =====
            with self._lock:
                self._stats["furry" if is_furry else "not_furry"] += 1
                if yolo_hit:
                    self._stats["yolo_hit"] += 1
                if keyword_hit:
                    self._stats["keyword_hit"] += 1

            if is_furry:
                self._credit_candidate_parents(uid, weight_updates, discovery_updates)
                with self._lock:
                    self._known_furry_set.add(uid)

                # 计算子级种子基础权重 = 当前权重的十分之一
                child_base_weight = current_weight / 10 if current_weight else 0

                # BFS 扩散
                rate_limited = False
                try:
                    print(f"      ├ BFS: 获取关注列表...")
                    followings = get_followings(uid, cookie=cookie, max_pages=2)
                    queued_f = 0
                    known_f = 0
                    pending_f = 0
                    for f_uid in followings:
                        state = self._enqueue_candidate(uid, f_uid, child_base_weight, weight_updates, discovery_updates)
                        if state == "queued":
                            queued_f += 1
                        elif state == "known":
                            known_f += 1
                        elif state == "pending":
                            pending_f += 1
                    print(f"      │     ↑ 关注: 新候选{queued_f}，已知福瑞{known_f}，待判定重复{pending_f}（子基础权重={child_base_weight}）")

                    print(f"      ├ BFS: 获取粉丝列表...")
                    followers = get_followers(uid, cookie=cookie, max_pages=2)
                    queued_f2 = 0
                    known_f2 = 0
                    pending_f2 = 0
                    for f_uid in followers:
                        state = self._enqueue_candidate(uid, f_uid, child_base_weight, weight_updates, discovery_updates)
                        if state == "queued":
                            queued_f2 += 1
                        elif state == "known":
                            known_f2 += 1
                        elif state == "pending":
                            pending_f2 += 1
                    print(f"      │     ↑ 粉丝: 新候选{queued_f2}，已知福瑞{known_f2}，待判定重复{pending_f2}（子基础权重={child_base_weight}）")
                except RateLimitedError as e:
                    print(f"      │     [风控] {e}，当前种子保留在 matched_pending.json，稍后重试")
                    rate_limited = True
                except Exception as e:
                    print(f"      │     [BFS错误] {e}")

                if rate_limited:
                    self._requeue_seed(uid)
                    self._task_queue.task_done()
                    continue

                # BFS 全部完成后，种子才移出
                # discovered_uids 由 _flush_weight_updates 统一写入，这里不动
                # seed_weight 也由 _flush_weight_updates 统一累加，这里只保留原始值
                with self._lock:
                    self._done_set.add(uid)
                done_batch.append(rec)
                has_data = follower is not None or following is not None
                tag = "有数据" if has_data else "无数据"
                print(f"      └ ✓ 福瑞（{tag}）→ matched_done.json（BFS完成，已从种子库移出）")

            else:
                with self._lock:
                    self._skip_set.add(uid)
                    self._candidate_parents.pop(uid, None)
                skip_batch.append(rec)
                print(f"      └ × 非福瑞 → not_matched.json")

            batch_counter += 1

            # 攒批写盘
            if batch_counter >= BATCH_FLUSH:
                self._flush_batches(done_batch, pending_batch, skip_batch, error_batch)
                self._flush_weight_updates(weight_updates, discovery_updates)
                batch_counter = 0

            # 随机延时
            settings.random_delay()
            with self._lock:
                self._processing_set.discard(uid)
            self._task_queue.task_done()

        # 线程结束前写盘剩余
        self._flush_batches(done_batch, pending_batch, skip_batch, error_batch)
        self._flush_weight_updates(weight_updates, discovery_updates)
        print(f"\n  [{tag}] 线程结束")

    def _move_to_error(self, uid: int, error_batch: List[dict]):
        """从原库找完整记录，放入 error_batch"""
        full_rec = {"uid": uid}
        for src_path in [self.pending_path, self.done_path]:
            records = load_db(src_path)
            for r in records:
                if r.get("uid") == uid:
                    full_rec = dict(r)
                    break
            remove_from_db(src_path, [uid])
        error_batch.append(full_rec)
        with self._lock:
            self._error_set.add(uid)
        print(f"      ├ → error.json（完整记录保留）")

    def _flush_weight_updates(self, weight_updates: Dict[int, int], discovery_updates: Dict[int, List[int]]):
        """将父级种子的权重增长和发现记录写入 JSON"""
        if not weight_updates and not discovery_updates:
            return

        all_uids = set(weight_updates.keys()) | set(discovery_updates.keys())
        if not all_uids:
            return

        touched = 0
        try:
            for src_path, label in [(self.done_path, "done"), (self.pending_path, "pending")]:
                with FileLock(src_path + ".lock"):
                    db = _load_raw(src_path)
                    records = db.get("uids", [])
                    changed = False
                    for rec in records:
                        uid = rec.get("uid")
                        if uid not in all_uids:
                            continue
                        rec["seed_weight"] = (rec.get("seed_weight") or 0) + weight_updates.get(uid, 0)
                        existing = rec.get("discovered_uids") or []
                        found = discovery_updates.get(uid, [])
                        if found:
                            rec["discovered_uids"] = existing + found
                        changed = True
                        touched += 1

                        with self._lock:
                            if uid in self._pending_records_by_uid:
                                self._pending_records_by_uid[uid]["seed_weight"] = rec["seed_weight"]
                                self._pending_records_by_uid[uid]["discovered_uids"] = rec.get("discovered_uids", [])

                    if changed:
                        db["meta"]["total_count"] = len(records)
                        db["meta"]["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        db["uids"] = records
                        _atomic_write(src_path, db)

            if touched:
                print(f"  >> 权重更新: {touched} 个种子")
            weight_updates.clear()
            discovery_updates.clear()
        except Exception as e:
            print(f"\n  [权重存盘错误] {e}")

    def _flush_batches(self, done_batch, pending_batch, skip_batch, error_batch):
        """线程安全写盘（FileLock 保护）"""
        # 收集所有已种完、需要从 pending 移除的 UID
        remove_uids = set()

        if done_batch:
            n = upsert_to_db(self.done_path, done_batch)
            self._done_set.update(r["uid"] for r in done_batch if r.get("uid"))
            remove_uids.update(r["uid"] for r in done_batch if r.get("uid"))
            if n:
                print(f"\n  >> matched_done.json +{n}（已种完的种子）")
            done_batch.clear()
        if pending_batch:
            n = upsert_to_db(self.pending_path, pending_batch)
            self._pending_set.update(r["uid"] for r in pending_batch if r.get("uid"))
            self._known_furry_set.update(r["uid"] for r in pending_batch if r.get("uid"))
            if n:
                print(f"\n  >> matched_pending.json +{n}（新种子）")
            pending_batch.clear()
        if skip_batch:
            n = upsert_to_db(self.skip_path, skip_batch)
            remove_uids.update(r["uid"] for r in skip_batch if r.get("uid"))
            if n:
                print(f"\n  >> not_matched.json +{n}")
            skip_batch.clear()
        if error_batch:
            n = upsert_to_db(self.error_path, error_batch)
            remove_uids.update(r["uid"] for r in error_batch if r.get("uid"))
            if n:
                print(f"\n  >> error.json +{n}")
            error_batch.clear()

        # 从 pending 中移除已处理完的种子（防止重复加载）
        if remove_uids:
            try:
                rn = remove_from_db(self.pending_path, list(remove_uids))
                if rn:
                    self._pending_set.difference_update(remove_uids)
            except Exception as e:
                print(f"\n  [移除种子失败] {e}")

    # ------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------
    def run(self):
        print("=" * 60)
        print("  B站 福瑞 BFS 爬虫（多线程版）")
        print("=" * 60)

        self.load_databases()

        cookies = settings.COOKIES
        n_threads = len(cookies)

        if n_threads == 0:
            print("[错误] 没有有效的 Cookie！")
            return

        if self._task_queue.qsize() == 0:
            print("[停止] 队列为空")
            return

        print(f"  Cookie: {n_threads} 个 → {n_threads} 线程")
        print(f"  队列: {self._task_queue.qsize()} 个 UID")
        print(f"  YOLO 模型: {settings.YOLO_MODEL_PATH}")
        print(f"  置信度: {settings.YOLO_CONF_THRESHOLD}")
        print()

        # 启动线程
        threads = []
        for tid, cookie in enumerate(cookies):
            t = threading.Thread(
                target=self._worker,
                args=(cookie, tid),
                daemon=True,
                name=f"crawl-{tid}",
            )
            threads.append(t)
            t.start()
            print(f"  线程{tid + 1} 已启动")

        # 等待所有线程结束
        for t in threads:
            t.join()

        # 最终统计
        s = self._stats
        print(f"\n{'=' * 60}")
        print(f"  爬取完成!")
        print(f"  总爬取: {s['crawled']}")
        print(f"  福瑞:   {s['furry']}")
        print(f"  非福瑞: {s['not_furry']}")
        print(f"  YOLO:   {s['yolo_hit']}  关键词: {s['keyword_hit']}")
        print(f"{'=' * 60}")


def main():
    BFSCrawler().run()


if __name__ == "__main__":
    main()
