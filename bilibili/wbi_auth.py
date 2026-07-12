"""
Bilibili WBI 签名算法
======================
从 bilibili.py 中拆分出独立的 WBI 签名模块。

功能：
  - 从 Nav API 获取 wbi_img 密钥（带 12 小时缓存）
  - 对 API 请求参数进行 WBI 签名（w_rid + wts）
"""

import os
import json
import time
import hashlib
import requests
from datetime import datetime, timedelta
from typing import Tuple, Dict

from config import settings

# session 级标志：是否已经在本 session 内尝试过刷新 WBI 密钥
_wbi_refreshed_this_session = False

# Bilibili WBI 签名混合密钥的 64 位置换表
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52
]


def get_wbi_keys(force_refresh: bool = False) -> Tuple[str, str]:
    """
    从 Bilibili Nav API 获取 wbi_img 的 img_key 和 sub_key。
    优先从本地缓存读取，缓存过期则重新请求。

    Args:
        force_refresh: 是否强制刷新（用于 -352 重试）

    Returns:
        (img_key, sub_key)
    """
    global _wbi_refreshed_this_session
    cache_path = settings.WBI_CACHE_FILE

    # 尝试从缓存读取
    if not force_refresh and os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
            expire_at = datetime.fromisoformat(cache.get("expire_at", "2000-01-01"))
            if datetime.now() < expire_at:
                return cache["img_key"], cache["sub_key"]
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    # 本 session 已经刷新过一次还不行？直接用旧缓存降级
    if _wbi_refreshed_this_session:
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cache = json.load(f)
                return cache["img_key"], cache["sub_key"]
            except Exception:
                pass
        raise RuntimeError("WBI 密钥已在本 session 刷新过一次仍失败，放弃重试")

    # 请求 Nav API 获取最新密钥
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
    }
    try:
        resp = requests.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        nav_data = data.get("data")
        if not nav_data or "wbi_img" not in nav_data:
            raise RuntimeError(f"Nav API 返回中没有 wbi_img 字段: {data}")

        wbi_img = nav_data["wbi_img"]
        img_key = wbi_img["img_url"].rsplit("/", 1)[1].split(".")[0]
        sub_key = wbi_img["sub_url"].rsplit("/", 1)[1].split(".")[0]

        # 写入缓存（密钥每天变化，缓存 12 小时）
        cache = {
            "img_key": img_key,
            "sub_key": sub_key,
            "expire_at": (datetime.now() + timedelta(hours=12)).isoformat(),
        }
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)

        _wbi_refreshed_this_session = True
        return img_key, sub_key

    except Exception as e:
        # 如果有缓存但过期，降级使用
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cache = json.load(f)
                return cache["img_key"], cache["sub_key"]
            except Exception:
                pass
        raise RuntimeError(f"获取 WBI 密钥失败: {e}")


def get_mixin_key(img_key: str, sub_key: str) -> str:
    """根据 img_key 和 sub_key 生成混合密钥（取前 32 位）"""
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def enc_wbi(params: Dict[str, str], mixin_key: str) -> Tuple[str, int]:
    """
    为参数字典生成 WBI 签名。

    Returns:
        (w_rid, wts) — w_rid 为 MD5 签名字符串，wts 为 UNIX 时间戳
    """
    wts = int(time.time())
    params["wts"] = wts

    params = {
        k: "".join(c for c in str(v) if c not in "!'()*")
        for k, v in params.items()
    }

    query = "&".join(
        f"{k}={requests.utils.quote(str(v), safe='')}"
        for k, v in sorted(params.items())
    )

    w_rid = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return w_rid, wts


def sign_api(params: Dict[str, str]) -> Dict[str, str]:
    """
    对 API 请求参数进行 WBI 签名，返回添加了 w_rid 和 wts 的参数字典。
    """
    img_key, sub_key = get_wbi_keys()
    mixin_key = get_mixin_key(img_key, sub_key)
    w_rid, wts = enc_wbi(params.copy(), mixin_key)
    params["w_rid"] = w_rid
    params["wts"] = wts
    return params
