"""
Bilibili API 封装
=================
从 bilibili.py 中拆分出的 API 调用模块。

功能：
  - get_up_info(uid)    — 获取 UP 主空间信息（含头像 URL）
  - get_up_stat(uid)    — 获取关系统计（粉丝数/关注数）
  - get_followings()    — 获取用户的关注列表（BFS 用）
  - get_followers()     — 获取用户的粉丝列表（BFS 用）
"""

import sys
import time
import json
from typing import Optional, Dict, List


class CookieExpiredError(RuntimeError):
    """Cookie 过期，所有 API 请求均无法正常返回"""
    pass


class UserNotFoundError(RuntimeError):
    """用户不存在/已注销（-404），该 UID 永久失效"""
    pass


class RateLimitedError(RuntimeError):
    """B站风控/限速（-352），当前请求应延后重试"""
    pass

import requests

from config import settings
from .wbi_auth import sign_api, get_wbi_keys

# 全局监控：连续 -352 次数（Cookie 过期检测）
_consecutive_wbi_failures = 0

RATE_LIMIT_SLEEP_SECONDS = 60
RATE_LIMIT_MAX_RETRIES = 3


def _clean_cookie(raw: str) -> str:
    """清理 Cookie 字符串，去除 non-latin-1 字符"""
    return raw.encode("latin-1", errors="ignore").decode("latin-1")


def get_common_headers(cookie: Optional[str] = None) -> Dict[str, str]:
    """返回通用的请求头，包含 Cookie 和必要标识"""
    cookie_str = cookie if cookie else (settings.COOKIES[0] if settings.COOKIES else "")
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
        "Cookie": _clean_cookie(cookie_str),
    }


def _sleep_after_352(uid: int, label: str, attempt: int):
    print(
        f"    [风控] UID {uid}: {label} 返回 -352，"
        f"休眠 {RATE_LIMIT_SLEEP_SECONDS} 秒后重试（第 {attempt}/{RATE_LIMIT_MAX_RETRIES} 次）"
    )
    time.sleep(RATE_LIMIT_SLEEP_SECONDS)


def _api_get(url: str, params: dict, headers: dict, uid: int, label: str = "",
             timeout: int = 15) -> Optional[dict]:
    """
    发送 B站 API GET 请求，遇到 -352（WBI 过期）自动刷新密钥重试一次。

    Returns:
        响应的 data 字段，失败返回 None
    """
    global _consecutive_wbi_failures

    for attempt in range(1, RATE_LIMIT_MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") == 0 and data.get("data"):
                # 成功请求重置计数器
                _consecutive_wbi_failures = 0
                return data["data"]
            elif data.get("code") == -412:
                print(f"    [警告] UID {uid}: 请求被拦截（-412），暂停 10 秒…")
                time.sleep(10)
                return None
            elif data.get("code") == -404:
                print(f"    [警告] UID {uid}: 用户不存在/已注销（-404）")
                raise UserNotFoundError(f"UID {uid} 不存在/已注销")
            elif data.get("code") == -352:
                _consecutive_wbi_failures += 1
                try:
                    get_wbi_keys(force_refresh=True)
                    key = "mid" if "acc/info" in url else "vmid"
                    params = sign_api({key: str(uid)})
                except Exception:
                    pass
                _sleep_after_352(uid, label or "API", attempt)
                continue
            else:
                print(f"    [警告] UID {uid}: {label}API 返回 code={data.get('code')}")
                return None

        except requests.exceptions.Timeout:
            print(f"    [错误] UID {uid}: 请求超时")
            return None
        except requests.exceptions.RequestException as e:
            print(f"    [错误] UID {uid}: 请求失败 — {e}")
            return None
        except json.JSONDecodeError:
            print(f"    [错误] UID {uid}: 响应 JSON 解析失败")
            return None

    raise RateLimitedError(f"UID {uid}: {label}连续 -352，延后当前种子")


def get_up_info(uid: int, cookie: Optional[str] = None) -> Optional[dict]:
    """获取 UP 主空间信息（含头像 URL）。自动处理 WBI 过期重试。"""
    params = sign_api({"mid": str(uid)})
    headers = get_common_headers(cookie)
    return _api_get("https://api.bilibili.com/x/space/wbi/acc/info",
                    params, headers, uid, label="space ")


def get_up_stat(uid: int, cookie: Optional[str] = None) -> Optional[dict]:
    """获取 UP 主统计数据（粉丝/关注）。自动处理 WBI 过期重试。"""
    params = sign_api({"vmid": str(uid)})
    headers = get_common_headers(cookie)
    return _api_get("https://api.bilibili.com/x/relation/stat",
                    params, headers, uid, label="relation ")


def get_followings(uid: int, cookie: Optional[str] = None, max_pages: int = 5) -> List[int]:
    """
    获取用户的关注列表（BFS 用）。

    API: GET https://api.bilibili.com/x/relation/followings?vmid={uid}&pn={page}&ps=50

    Args:
        uid: 用户 UID
        cookie: Cookie
        max_pages: 最大翻页数（每页 50 条）

    Returns:
        关注对象的 UID 列表
    """
    headers = get_common_headers(cookie)
    results = []

    for page in range(1, max_pages + 1):
        try:
            resp = requests.get(
                "https://api.bilibili.com/x/relation/followings",
                params={"vmid": uid, "pn": page, "ps": 50, "order": "desc"},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") == -352:
                _sleep_after_352(uid, f"关注列表 page={page}", 1)
                for retry in range(2, RATE_LIMIT_MAX_RETRIES + 1):
                    resp = requests.get(
                        "https://api.bilibili.com/x/relation/followings",
                        params={"vmid": uid, "pn": page, "ps": 50, "order": "desc"},
                        headers=headers,
                        timeout=15,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    if data.get("code") != -352:
                        break
                    _sleep_after_352(uid, f"关注列表 page={page}", retry)
                if data.get("code") == -352:
                    raise RateLimitedError(f"UID {uid} 关注列表连续 -352")

            if data.get("code") != 0:
                print(f"    [警告] UID {uid} 关注列表 page={page}: code={data.get('code')}")
                break

            items = data.get("data", {}).get("list", [])
            if not items:
                break

            for item in items:
                results.append(item.get("mid", 0))

            # 如果没满页说明到头了
            if len(items) < 50:
                break

            settings.random_delay()

        except RateLimitedError:
            raise
        except Exception as e:
            print(f"    [错误] UID {uid} 关注列表 page={page}: {e}")
            break

    return results


def get_followers(uid: int, cookie: Optional[str] = None, max_pages: int = 3) -> List[int]:
    """
    获取用户的粉丝列表（BFS 用）。

    API: GET https://api.bilibili.com/x/relation/followers?vmid={uid}&pn={page}&ps=50

    Args:
        uid: 用户 UID
        cookie: Cookie
        max_pages: 最大翻页数

    Returns:
        粉丝的 UID 列表
    """
    headers = get_common_headers(cookie)
    results = []

    for page in range(1, max_pages + 1):
        try:
            resp = requests.get(
                "https://api.bilibili.com/x/relation/followers",
                params={"vmid": uid, "pn": page, "ps": 50},
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") == -352:
                _sleep_after_352(uid, f"粉丝列表 page={page}", 1)
                for retry in range(2, RATE_LIMIT_MAX_RETRIES + 1):
                    resp = requests.get(
                        "https://api.bilibili.com/x/relation/followers",
                        params={"vmid": uid, "pn": page, "ps": 50},
                        headers=headers,
                        timeout=15,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    if data.get("code") != -352:
                        break
                    _sleep_after_352(uid, f"粉丝列表 page={page}", retry)
                if data.get("code") == -352:
                    raise RateLimitedError(f"UID {uid} 粉丝列表连续 -352")

            if data.get("code") != 0:
                print(f"    [警告] UID {uid} 粉丝列表 page={page}: code={data.get('code')}")
                break

            items = data.get("data", {}).get("list", [])
            if not items:
                break

            for item in items:
                results.append(item.get("mid", 0))

            if len(items) < 50:
                break

            settings.random_delay()

        except RateLimitedError:
            raise
        except Exception as e:
            print(f"    [错误] UID {uid} 粉丝列表 page={page}: {e}")
            break

    return results
