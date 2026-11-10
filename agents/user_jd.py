"""把用户当场粘贴的招聘要求原文，整理成与检索证据同构的记录。

为什么要"伪装成证据"
--------------------
``job_agent`` 与 ``jd_match`` 的整套来源判定都长在 ``job_evidence`` 这个数组上：
证据编号回查、从原文措辞推能力等级、"有证据归属的要求才计入匹配度分母"。
而用户粘贴的原文本来就是**最硬的那份证据** —— 把它切成句子塞进同一个数组，
这些判定逻辑一行都不用改就全部复用，比另起一套来源标注便宜得多。

切出来的句子带 ``provenance="user_pasted"``，供下游按来源分档展示。

刻意不做的事
------------
- **不调模型**：切句是纯文本操作，不额外花一次推理。
- **不做技能归一**：把"熟练 SQL"归一到知识库规范技能名是内置画像的特权，
  用户原文没有规范名可言。归一不到 → ``job_agent`` 的规则层拿不到结果 →
  自然落到"模型引用编号"那条分支 → 得到 ``partially_supported``，
  语义正是「原文支撑，但非规范技能名」，无需特殊照顾。
- **不猜 importance**：原文没给重要性就不假装给过，交给模型值并标
  ``model_generated``。
"""

from __future__ import annotations

import re

__all__ = [
    "DEFAULT_MAX_CHUNKS", "USER_JD_PROVENANCE", "USER_JD_SOURCE_ID",
    "chunk_user_jd", "split_sentences",
]

#: 句子来源标记；job_agent 与前端都按它把"你粘贴的原文"与"内置资料"分开。
USER_JD_PROVENANCE = "user_pasted"

#: 固定来源 ID：它表示"用户当场粘贴的原文"，不对应仓库里的任何资料文件。
USER_JD_SOURCE_ID = "user_pasted"

#: 句子条数上限。一句一条，超出部分丢弃并如实告知 —— 一份超长原文若被静默截断，
#: 用户会以为"后半段它也读过了"。上限要够放提示词，40 句 ≈ 一份完整 JD 的正文。
DEFAULT_MAX_CHUNKS = 40

#: 低于这个长度的片段多是"岗位职责""任职要求"这类小标题或序号残留，撑不起一条要求。
_MIN_SENTENCE_CHARS = 8

#: 行首的项目符号 / 序号。中文 JD 的排版花样基本都在这几类里。
_LEADING_BULLET = re.compile(
    r"^\s*(?:[•·‣◦\-*+▪·]|[-–—]|\(?\d{1,2}\)?|[（(][一二三四五六七八九十][）)])[.、)）]?\s*"
)

#: 句子边界。中文句号/分号/顿号式列举的分号，以及英文句点分号。
#: 用 [。；;] 而不是逗号 —— 逗号切开会破坏"负责 A，要求 B"这类因果表述。
_SENTENCE_SPLIT = re.compile(r"[。；;]")

#: 换行类分隔（含 markdown 列表与全角空格残留）。
_LINE_SPLIT = re.compile(r"\r?\n|[│\t]")


def split_sentences(text: str) -> list[str]:
    """把原文拆成可用的句子。纯函数，不改文字内容。"""
    sentences: list[str] = []
    seen: set[str] = set()
    for raw_line in _LINE_SPLIT.split(text or ""):
        line = _LEADING_BULLET.sub("", raw_line).strip()
        if not line:
            continue
        for piece in _SENTENCE_SPLIT.split(line):
            cleaned = piece.strip(" \t-–—、,.")
            if len(cleaned) < _MIN_SENTENCE_CHARS or cleaned in seen:
                continue
            seen.add(cleaned)
            sentences.append(cleaned)
    return sentences


def chunk_user_jd(jd_text: str, max_chunks: int = DEFAULT_MAX_CHUNKS) -> tuple[list[dict], int]:
    """用户原文 → 证据记录列表。返回 ``(records, dropped_count)``。

    ``dropped_count`` 是超出 ``max_chunks`` 被丢弃的句子数 —— 必须如实带出去，
    否则一份超长原文会让人误以为"它全都读过了"。
    """
    sentences = split_sentences(jd_text)
    kept = sentences[:max_chunks]
    dropped = len(sentences) - len(kept)
    records = [
        {
            "job_id": USER_JD_SOURCE_ID,
            "source_id": USER_JD_SOURCE_ID,
            "company_slug": None,
            "skill": None,
            "requirement_type": USER_JD_PROVENANCE,
            "context": "用户粘贴的招聘要求",
            "importance": None,
            "level": None,
            "provenance": USER_JD_PROVENANCE,
            "note": sentence,
        }
        for sentence in kept
    ]
    return records, dropped
