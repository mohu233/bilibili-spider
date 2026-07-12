"""
关键词匹配器
============
通过昵称、简介、视频标签等文本信息判断是否为福瑞圈用户。

与 YOLO 头像检测互补使用。
"""

import re
from typing import Optional, List

from config import settings


# 默认福瑞关键词（从 settings 读取）
DEFAULT_KEYWORDS = settings.FURRY_KEYWORDS


class KeywordMatcher:
    """
    关键词匹配器。

    用于在昵称、简介、签名等文本中搜索福瑞相关关键词。
    支持精确匹配和模糊匹配。
    """

    def __init__(self, keywords: Optional[List[str]] = None):
        """
        Args:
            keywords: 关键词列表，默认使用 settings.FURRY_KEYWORDS
        """
        self.keywords = keywords or DEFAULT_KEYWORDS

    def match(self, text: str) -> tuple:
        """
        检测文本中是否包含福瑞关键词。

        Args:
            text: 待检测文本

        Returns:
            (matched, matched_keywords)
            - matched: True/False
            - matched_keywords: 命中的关键词列表
        """
        if not text:
            return False, []

        matched_keywords = []
        for keyword in self.keywords:
            if keyword in text:
                matched_keywords.append(keyword)

        return len(matched_keywords) > 0, matched_keywords

    def match_name(self, name: Optional[str]) -> tuple:
        """检测昵称"""
        return self.match(name or "")

    def match_intro(self, intro: Optional[str]) -> tuple:
        """检测简介/签名"""
        return self.match(intro or "")

    def match_tags(self, tags: Optional[str]) -> tuple:
        """检测视频标签（逗号/空格分隔的标签字符串）"""
        return self.match(tags or "")

    def full_check(self, name: Optional[str] = None,
                   intro: Optional[str] = None,
                   tags: Optional[str] = None) -> dict:
        """
        综合检测昵称 + 简介 + 标签。

        Returns:
            {
                "is_furry": True/False,
                "matched_by": ["name", "intro", "tags"],  # 哪些维度命中
                "keywords": [...]  # 命中的关键词
            }
        """
        result = {
            "is_furry": False,
            "matched_by": [],
            "keywords": [],
        }

        # 检测昵称
        matched, keywords = self.match_name(name)
        if matched:
            result["is_furry"] = True
            result["matched_by"].append("name")
            result["keywords"].extend(keywords)

        # 检测简介
        matched, keywords = self.match_intro(intro)
        if matched:
            result["is_furry"] = True
            result["matched_by"].append("intro")
            result["keywords"].extend(keywords)

        # 检测标签
        matched, keywords = self.match_tags(tags)
        if matched:
            result["is_furry"] = True
            result["matched_by"].append("tags")
            result["keywords"].extend(keywords)

        result["keywords"] = list(set(result["keywords"]))  # 去重
        return result
