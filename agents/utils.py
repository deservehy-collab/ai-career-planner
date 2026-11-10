"""Agent 之间共用的小工具：技能等级归一化、列表去重、输出校验。

放在这里是为了避免每个 Agent 各写一份（小模型输出不稳定，兜底逻辑要统一）。
"""

SKILL_LEVELS = ("入门", "初级", "中级", "高级")

# 模型可能给出的各种等同说法，统一映射到四档
_LEVEL_SYNONYMS = {
    "入门": "入门",
    "初级": "初级",
    "中级": "中级",
    "高级": "高级",
    "了解": "入门",
    "听说过": "入门",
    "会用": "初级",
    "熟悉": "中级",
    "掌握": "中级",
    "熟练": "高级",
    "精通": "高级",
    "专家": "高级",
    "中等": "中级",
    "高": "高级",
    "中": "中级",
}


def normalize_level(value) -> str:
    """把模型输出的等级说法统一成 入门/初级/中级/高级 四档之一。

    无法识别时退回"初级"：比"入门"保守但不夸大，也不会让 pydantic 校验直接崩掉。
    """
    text = str(value or "").strip()
    if text in _LEVEL_SYNONYMS:
        return _LEVEL_SYNONYMS[text]

    # 含修饰语的说法，如 "比较熟练"、"中等偏上"
    if any(k in text for k in ("精通", "熟练", "高")):
        return "高级"
    if any(k in text for k in ("中", "熟悉", "掌握")):
        return "中级"
    if "初" in text:
        return "初级"
    if any(k in text for k in ("入门", "了解", "听说")):
        return "入门"
    return "初级"


def resolve_canonical(name, vocabulary) -> str | None:
    """把技能名归一成词表里的**规范技能名**，只有唯一命中才采用。

    判定顺序：字面命中优先于别名命中。像"审计程序"这种完整规范名，会同时
    字面命中自己、又通过"审计"这个别名沾到"审计基础"；此时必须取字面命中的
    那一个，否则新增技能名一多，原本能识别的名字会集体变成"有歧义"。

    字面命中多于一个、没有字面命中时别名多于一个，仍一律返回 None：模型很容易
    把岗位能力分类标签当成技能名，名字里同时沾到多个规范技能时不能猜，
    否则会把"大模型调用与提示词工程"这类含混说法错判成某一个具体技能。

    未命中或有歧义一律返回 None，交给调用方显式处理（记录、告警、降级），
    绝不悄悄替换成别的技能。

    `vocabulary` 为 None（词表不可用）时同样返回 None。
    """
    text = str(name or "").strip()
    if not text or vocabulary is None:
        return None
    matches = vocabulary.match_detailed(text)
    literal = [name for name, kind in matches if kind == "literal"]
    if literal:
        return literal[0] if len(literal) == 1 else None
    if len(matches) != 1:
        return None
    return matches[0][0]


def canonical_matches(name, vocabulary) -> list[str]:
    """词表能沾上边的规范技能名列表（保序去重）。

    与 :func:`resolve_canonical` 用的是同一套命中口径，区别只在于**把几种命中摊开**：

    - 空列表 —— 词表里压根没有这项技能（非 AI 岗位的缺口名都是这种情况）；
    - 一个 —— 唯一命中，可以放心归一；
    - 两个以上 —— 歧义，不能猜。

    ``resolve_canonical`` 把后两种情况都压成 ``None``，需要区分"没收录"与"认不准"
    的调用方（如成长记账）用这个函数。``vocabulary`` 为 None 时返回空列表。
    """
    text = str(name or "").strip()
    if not text or vocabulary is None:
        return []
    return list(dict.fromkeys(canonical for canonical, _ in vocabulary.match_detailed(text)))


def build_skill_mapping(names, vocabulary) -> tuple[dict, list]:
    """批量归一技能名。

    返回 `({原始名: 规范名}, [无法唯一归一的原始名])`，两边都保序去重。
    "无法唯一归一"包含**没命中**与**有歧义**两种情况，按同一口径记录，
    不做任何猜测。
    """
    mapping: dict[str, str] = {}
    unmapped: list[str] = []
    for raw in names or []:
        name = str(raw or "").strip()
        if not name or name in mapping or name in unmapped:
            continue
        resolved = resolve_canonical(name, vocabulary)
        if resolved is None:
            unmapped.append(name)
        else:
            mapping[name] = resolved
    return mapping, unmapped


def dedupe_by(items: list, key: str) -> list:
    """按 key 去重（忽略大小写、空格），保留第一次出现的那条。

    同时丢掉 key 为空白的条目——这类条目下游没法用。
    """
    seen = set()
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get(key, "") or "").strip()
        marker = name.lower().replace(" ", "")
        if not name or marker in seen:
            continue
        seen.add(marker)
        result.append({**item, key: name})
    return result


def check_non_empty(items: list, what: str, minimum: int = 1) -> list:
    """校验列表确实有内容，否则抛出可读的错误，避免脏数据流向下游。"""
    if len(items) < minimum:
        raise ValueError(f"{what} 只解析出 {len(items)} 条，少于要求的 {minimum} 条，模型输出可能异常")
    return items


def log(agent_name: str, message: str = "completed") -> None:
    """节点执行日志：用来确认 LangGraph 到底跑到了哪几个节点。"""
    print(f"[{agent_name}] {message}", flush=True)
