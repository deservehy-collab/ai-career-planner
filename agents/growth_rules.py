"""阶段五：动态能力成长的**纯规则**模块。

本模块只做确定性判定：不调模型、不读磁盘、不写 state。
所有外部依赖（技能词表、技能知识库）一律**从参数注入**，便于用内存替身做单元测试。

分工（与阶段三、四一致）
------------------------
- 模型只负责"这条成果涉及哪些技能"（在 growth_agent 里完成）；
- 等级、上限、前置门禁、去重、差异**全部由本模块的代码判定**。

规则口径（逐条对应已确认的设计）
--------------------------------
R1 等级合法值只有 入门 / 初级 / 中级 / 高级。缺席按序数 0 处理，
   **绝不把"无"写进 ``user_skills[].level``** —— 缺席用 None 表示。
R2 每次运行最多升一档；只升不降。同一轮里多条成果命中同一技能**不叠加**。
R3 上限只由**成果类型**决定（learning → 初级，project → 中级；高级不授予），
   **不读 level_guides** —— 这样未收录等级标准的技能也能套用同一条规则。
   evidence 必须非空，仅凭标题不得升级。
R4 技能库**已收录**的技能执行严格前置门禁：前置缺失或等级无法确认 → 记入 blocked_upgrades。
R5 技能库**未收录**的技能跳过前置门禁，其余规则照旧，并标注"未收录等级标准"。
R6 复用 ``_LEVEL_RANK`` / ``_ABSENT_LEVELS`` / 技能名归一函数，不另立常量。
R7 指纹 = sha256(type \x1f title \x1f 排序后的技能键) 取前 12 位，**不含时间戳**。
   技能键 = 规范技能名，或词表没收录时的原名（见 :func:`normalize_skills`）。
R8 缺口变化按**规范技能名**配对，禁止按下标 zip；兼容新增、消失、状态变化。
R9 非法事件显式记录并跳过；**非法 type 不默认成 learning**。
"""

from __future__ import annotations

import hashlib

from agents.assessment_agent import _LEVEL_RANK, _norm_name
from agents.project_agent import SATISFIED_STATUS
from agents.project_knowledge import _ABSENT_LEVELS
from agents.utils import SKILL_LEVELS, canonical_matches, resolve_canonical

# ── 成果类型 ────────────────────────────────────────────────────────────
EVENT_LEARNING = "learning"
EVENT_PROJECT = "project"
VALID_EVENT_TYPES = (EVENT_LEARNING, EVENT_PROJECT)

# 证据类型：self_report 只是"我做了"，升级照规则走，但不算直接证据；
# growth / project 才算有可回查的凭证。
EVIDENCE_KIND_GROWTH = "growth"
EVIDENCE_KIND_PROJECT = "project"
EVIDENCE_KIND_SELF_REPORT = "self_report"
VALID_EVIDENCE_KINDS = (
    EVIDENCE_KIND_GROWTH,
    EVIDENCE_KIND_PROJECT,
    EVIDENCE_KIND_SELF_REPORT,
)
_STRONG_EVIDENCE_KINDS = (EVIDENCE_KIND_GROWTH, EVIDENCE_KIND_PROJECT)

# R3：等级上限只由成果类型决定，不依赖知识库（level_guides 只用于"未收录"标注）
# 注意"高级"刻意不在表里 —— 阶段五不授予高级。
_TYPE_LEVEL_CAP = {
    EVENT_LEARNING: "初级",
    EVENT_PROJECT: "中级",
}

NOTE_UNCOVERED = "该技能未收录等级标准"

# ── 知识库状态："未收录" 与 "加载失败" 是两回事 ─────────────────────────
# 取值必须与 growth_store.KB_STATUS_* 一致。这里**只声明取值、不 import**：
# growth_store 依赖本模块（它 import 了 level_rank / pair_key），反向 import 会成环。
# 为了不让"两处写法不一致"变成隐患，_validate_kb_status() 对认不出的取值
# 一律**安全失败**（拒绝升级），而不是默认放行；临时测试里也钉住了两边取值相等。
KB_STATUS_OK = "ok"
KB_STATUS_LOAD_FAILED = "load_failed"
VALID_KB_STATUSES = (KB_STATUS_OK, KB_STATUS_LOAD_FAILED)

# ── 门禁结论（四态，互不混淆）──────────────────────────────────────────
GATE_PASSED = "passed"              # 已收录 + 前置齐备：门禁真的跑过并放行
GATE_NOT_COVERED = "not_covered"    # 技能明确未收录：按 R5 跳过门禁
GATE_UNAVAILABLE = "unavailable"    # 知识库加载失败：门禁**根本没执行**
GATE_BLOCKED = "blocked"            # 已收录但前置缺失：按 R4 拦截

NOTE_GATE_UNAVAILABLE = (
    "前置门禁未执行（技能知识库加载失败）—— 本次升级没有经过前置技能校验，"
    "不代表已通过门禁，也不代表该技能未收录"
)

_FINGERPRINT_LEN = 12
_FINGERPRINT_SEP = "\x1f"

# 序数 → 合法等级名（由 _LEVEL_RANK + SKILL_LEVELS 派生，不复制常量）
_RANK_TO_LEVEL = {
    rank: name for name, rank in _LEVEL_RANK.items() if name in SKILL_LEVELS
}


# ── R1：等级与序数 ──────────────────────────────────────────────────────
def level_rank(level) -> int:
    """等级 → 序数。缺席、"无"、以及任何无法识别的写法一律返回 0。

    复用 assessment 的 ``_LEVEL_RANK``（含"无"=0）与 project_knowledge 的
    ``_ABSENT_LEVELS``（"无经验"、"n/a" 等写法），口径与既有模块一致。
    """
    text = str(level or "").strip()
    if not text or text in _ABSENT_LEVELS:
        return 0
    return _LEVEL_RANK.get(text, 0)


def level_name(rank: int) -> str | None:
    """序数 → 合法等级名；0 或越界返回 ``None``（表示缺席）。

    **永不返回"无"** —— 这是 R1 的落点：调用方拿到的要么是四档之一，要么是 None，
    不会有第三种写法漏进 ``user_skills[].level``。
    """
    if not isinstance(rank, int) or rank <= 0:
        return None
    return _RANK_TO_LEVEL.get(rank)


def pair_key(name, vocabulary=None) -> str:
    """配对用的技能键：优先规范技能名，归一不了退回"小写去空格"写法。

    绝不因为归一失败就把一项丢掉 —— 那样缺口会"凭空消失"。
    """
    raw = str(name or "").strip()
    if not raw:
        return ""
    if vocabulary is not None:
        canonical = resolve_canonical(raw, vocabulary)
        if canonical:
            return canonical
    return _norm_name(raw)


def normalize_skills(names, vocabulary=None) -> tuple[list[str], list[str]]:
    """把技能名列表归一成成长记账用的技能键（保序去重）。

    返回 ``(canonical, unmapped)``。三种写法三种下场，**任何一种都不猜**：

    - **唯一命中词表** → 用规范技能名；
    - **一个词表技能都没沾上** → 按原名建键，照常参与记账，同时列入 ``unmapped``。
      词表只收录「AI 应用工程师」那十几项技能，电气、护理这类岗位的缺口名
      天然不在表里；把它们丢掉会让这些用户的每一条成果都判成"没关联技能"，
      成长记录永远写不进去。技能知识库查不到某项技能时按 R5 跳过门禁、照常升档
      （见 :func:`resolve_gate`），所以保留原名**不会**绕过任何校验。
      纯数字除外 —— 那是模型抄的缺口编号，不是技能名，不给它记一档；
    - **沾上两个以上**（歧义，如"大模型调用与提示词工程"）→ **丢弃**，只进
      ``unmapped``。这条红线不动：按原始写法升级等于放行一个我们认不准的技能。

    ``vocabulary`` 为 None（词表加载失败）时全部按"未能归一"处理，与以往一致。
    """
    canonical: list[str] = []
    unmapped: list[str] = []
    for raw in names or []:
        name = str(raw or "").strip()
        if not name or name in unmapped:
            continue
        if vocabulary is None:
            unmapped.append(name)        # 词表拿不到：不放宽，全部如实列进未识别清单
            continue
        matches = canonical_matches(name, vocabulary)
        if len(matches) == 1:
            key = matches[0]
        elif matches:
            unmapped.append(name)       # 歧义：认不准就不记账
            continue
        else:
            if name.isdigit():
                unmapped.append(name)   # 纯数字是缺口编号，不是技能名，不给它记一档
                continue
            key = pair_key(name)        # 词表没收录：按原名记账，并如实报告
            unmapped.append(name)
        if key and key not in canonical:
            canonical.append(key)
    return canonical, unmapped


# ── R7：成果指纹 ────────────────────────────────────────────────────────
def event_fingerprint(event_type, title, canonical_skills) -> str:
    """成果指纹：sha256(type \\x1f title \\x1f 排序后的规范技能名) 前 12 位。

    **不含时间戳** —— 同一条成果换个日期重复提交，也应被识别为重复，而不是再升一档。
    """
    parts = [str(event_type or "").strip(), str(title or "").strip()]
    parts.extend(sorted({str(s).strip() for s in (canonical_skills or []) if str(s).strip()}))
    payload = _FINGERPRINT_SEP.join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_FINGERPRINT_LEN]


# ── R9：事件校验 ────────────────────────────────────────────────────────
def validate_event(event) -> tuple[dict | None, str | None]:
    """校验单条成长事件。

    返回 ``(清洗后的事件, 非法原因)``；原因非 None 表示这条事件**非法、必须跳过**。
    非法 type **不做默认假设**（绝不悄悄当成 learning），否则一条写错类型的高级成果
    就能绕过 type 对应的等级上限。
    """
    if not isinstance(event, dict):
        return None, "事件不是对象"

    event_type = str(event.get("type") or "").strip()
    if event_type not in VALID_EVENT_TYPES:
        return None, (
            f"type 非法：{event_type!r}；只接受 {list(VALID_EVENT_TYPES)}，"
            f"不做默认假设（不按 learning 处理）"
        )

    title = str(event.get("title") or "").strip()
    if not title:
        return None, "缺少 title"

    evidence = str(event.get("evidence") or "").strip()
    raw_kind = str(event.get("evidence_kind") or "").strip()
    if not raw_kind:
        # 旧记录没有 evidence_kind：有凭证按 growth，没凭证一律不能升级
        raw_kind = EVIDENCE_KIND_GROWTH if evidence else EVIDENCE_KIND_SELF_REPORT
    if raw_kind not in VALID_EVIDENCE_KINDS:
        return None, (
            f"evidence_kind 非法：{raw_kind!r}；只接受 {list(VALID_EVIDENCE_KINDS)}"
        )
    if not evidence and raw_kind in _STRONG_EVIDENCE_KINDS:
        return None, "evidence 为空 —— 仅凭标题不得升级；自述完成请标注 self_report"

    raw_skills = event.get("skills")
    if not isinstance(raw_skills, list):
        return None, "skills 必须是列表"
    skills = [str(s).strip() for s in raw_skills if str(s or "").strip()]
    if not skills:
        return None, "skills 为空 —— 没有涉及任何技能，无法升级"

    return {
        "type": event_type,
        "title": title,
        "skills": skills,
        "evidence": evidence,
        "evidence_kind": raw_kind,
        "note": str(event.get("note") or "").strip(),
        "at": str(event.get("at") or "").strip() or None,
    }, None


# ── R4 / R5：前置门禁 ───────────────────────────────────────────────────
def _validate_kb_status(knowledge_base_status) -> str | None:
    """校验知识库状态取值；认不出就返回原因（调用方据此**安全失败**）。

    宁可拒绝升级，也不要把"不知道知识库到底怎样"当成"没收录、放行"。
    """
    status = str(knowledge_base_status or "").strip()
    if status in VALID_KB_STATUSES:
        return None
    return (
        f"知识库状态无法识别：{knowledge_base_status!r}；"
        f"只接受 {list(VALID_KB_STATUSES)}。按安全失败处理：本次不执行任何升级，"
        f"避免把「无法判断」当成「未收录」而默认放行"
    )


def resolve_gate(
    skill: str,
    level_ranks: dict,
    skill_base,
    knowledge_base_status: str = KB_STATUS_OK,
) -> dict:
    """前置门禁的**四态**结论。纯计算，不抛异常（状态取值非法除外由调用方处理）。

    返回 ``{gate_status, passed, covered, missing, gate_unavailable, reason}``：

    ==================  ========  ========  ======================
    gate_status          passed    covered   含义
    ==================  ========  ========  ======================
    ``passed``           True      True      已收录且前置齐备
    ``not_covered``      True      False     技能明确未收录（R5 跳过门禁）
    ``unavailable``      True      None      知识库加载失败，门禁没执行
    ``blocked``          False     True      已收录但前置缺失（R4 拦截）
    ==================  ========  ========  ======================

    **``covered=None`` 是"不知道"，与 ``covered=False``（"明确未收录"）严格区分**；
    ``unavailable`` 也绝不能被读成"已通过门禁"（``gate_unavailable`` 为 True）。
    """
    status_error = _validate_kb_status(knowledge_base_status)
    if status_error:
        return {
            "gate_status": GATE_UNAVAILABLE,
            "passed": False,
            "covered": None,
            "missing": [],
            "gate_unavailable": True,
            "reason": status_error,
        }

    if knowledge_base_status == KB_STATUS_LOAD_FAILED:
        return {
            "gate_status": GATE_UNAVAILABLE,
            "passed": True,                     # 允许继续处理成长事件
            "covered": None,                    # 但"是否收录"无从得知
            "missing": [],
            "gate_unavailable": True,
            "reason": NOTE_GATE_UNAVAILABLE,
        }

    # 状态正常：沿用原有行为
    if skill_base is None or not skill_base.has(skill):
        return {
            "gate_status": GATE_NOT_COVERED,
            "passed": True,
            "covered": False,
            "missing": [],
            "gate_unavailable": False,
            "reason": NOTE_UNCOVERED,
        }

    entry = skill_base.get(skill) or {}
    prerequisites = [str(p).strip() for p in (entry.get("prerequisites") or []) if str(p).strip()]
    missing = [p for p in prerequisites if _rank_of(level_ranks.get(p)) <= 0]
    if missing:
        return {
            "gate_status": GATE_BLOCKED,
            "passed": False,
            "covered": True,
            "missing": missing,
            "gate_unavailable": False,
            "reason": f"前置技能尚未具备：{'、'.join(missing)}",
        }
    return {
        "gate_status": GATE_PASSED,
        "passed": True,
        "covered": True,
        "missing": [],
        "gate_unavailable": False,
        "reason": "已收录且前置技能齐备",
    }


def check_prerequisites(skill: str, level_ranks: dict, skill_base) -> tuple[bool, list[str], bool]:
    """前置门禁：判断 ``skill`` 的前置技能是否都已具备。

    返回 ``(是否通过, 缺失的前置技能, 该技能是否收录于知识库)``。

    **``level_ranks`` 是"规范技能名 → 序数(int)"的映射**，不是等级名字典 ——
    调用方应先用 :func:`level_rank` 把等级转成序数再传进来。这里刻意不再次转换，
    避免把已经算好的序数误当成等级名再解析一遍（那会让所有前置恒判为缺失）。

    - 技能知识库**未收录**该技能 → 通过（跳过门禁），``covered=False``（R5）。
    - ``skill_base`` 为 None（拿不到知识库）→ 同样视为未收录、放行，
      **宁可不过滤，也不要凭空把人挡在门外**（与 project_knowledge 的口径一致）。
    - 前置技能不在映射里、或序数为 0（等级无法确认）→ 记为缺失（R4）。

    这是 :func:`resolve_gate` 在"知识库状态正常"下的三元组视图，签名与语义保持不变，
    既有调用方不需要改动。需要区分"未收录"与"加载失败"时请改用 :func:`resolve_gate`。
    """
    gate = resolve_gate(skill, level_ranks, skill_base, KB_STATUS_OK)
    return gate["passed"], gate["missing"], gate["covered"]


def _rank_of(value) -> int:
    """把"可能是序数、也可能是等级名"的值统一成序数。

    门禁的入参约定是序数，但真实数据里偶尔会混进等级名（例如上游直接把
    ``user_skills`` 加工了一份），这里两种都认，避免因为写法不同就误判成"缺失"。
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return level_rank(value)


# ── 核心：把成果事件算成等级更新计划 ────────────────────────────────────
def plan_upgrades(
    events,
    user_skills,
    vocabulary=None,
    skill_base=None,
    seen_fingerprints=(),
    knowledge_base_status: str = KB_STATUS_OK,
) -> dict:
    """把一批成长成果算成等级更新计划。**纯函数**，不改动入参、不做 IO。

    参数
    ----
    events : 成长成果列表（``growth_events``），逐条校验，非法项跳过并记录
    user_skills : **基线**能力表 [{skill, level, evidence}]（已合并历史成长结果）
    vocabulary : ``SkillVocabulary``，用于技能名归一；None 表示不归一
    skill_base : ``SkillKnowledgeBase``，用于前置门禁；None 表示拿不到知识库
    seen_fingerprints : 历史已处理过的指纹集合，命中的事件直接跳过（避免重复升级）
    knowledge_base_status : 知识库的**加载状态**，默认 ``ok``（行为与以往一致）：

        - ``ok``：技能未收录 → 按 R5 跳过门禁（``gate_status=not_covered``）。
          ``skill_base is None`` 在这个状态下仍按既有契约处理为"未收录"。
        - ``load_failed``：**门禁根本没执行**。升级照常进行（成长事件不该被知识库
          故障卡住），但每条升级会带 ``gate_unavailable=True``、
          ``gate_status=unavailable``、``covered=None`` 和可读原因 ——
          **既不能被读成"已通过门禁"，也不能被读成"技能未收录"**。
        - 其他取值：**安全失败**。所有候选进 ``blocked`` 并说明原因，本次不升级，
          绝不默认放行。

    返回
    ----
    ``{events, updates, blocked, skipped, invalid, duplicates, unmapped, notes,
    knowledge_base_status}``

    ``updates`` 的每条都带门禁标记，供 ``growth_agent`` 直接消费：
    ``gate_status``（passed / not_covered / unavailable）、``gate_unavailable``（bool）、
    ``covered_by_knowledge_base``（True / False / None）、``gate_reason``。
    """
    seen = {str(f).strip() for f in (seen_fingerprints or ()) if str(f).strip()}
    status_error = _validate_kb_status(knowledge_base_status)

    # 基线等级：规范技能名 → 序数（同一规范名取最高档，避免归一冲突拉低能力）
    user_levels: dict[str, int] = {}
    for item in user_skills or []:
        if not isinstance(item, dict):
            continue
        key = pair_key(item.get("skill"), vocabulary)
        if not key:
            continue
        rank = level_rank(item.get("level"))
        if rank > user_levels.get(key, 0):
            user_levels[key] = rank

    updates: dict[str, dict] = {}   # 规范技能名 → 升级条目（同一轮不叠加）
    blocked: list[dict] = []
    skipped: list[dict] = []
    duplicates: list[dict] = []
    invalid: list[dict] = []
    unmapped: list[dict] = []
    notes: list[str] = []
    accepted_events: list[dict] = []

    # 知识库状态认不出来时，把原因放进 notes，让上层一定能看到（并拒绝所有升级）
    if status_error:
        notes.append(status_error)

    for index, raw_event in enumerate(events or [], start=1):
        cleaned, reason = validate_event(raw_event)
        if cleaned is None:
            invalid.append({"index": index, "reason": reason, "event": _safe_repr(raw_event)})
            continue

        canonical, missed = normalize_skills(cleaned["skills"], vocabulary)
        if missed:
            unmapped.append({"index": index, "title": cleaned["title"], "skills": missed})
        if not canonical:
            # 走到这里只剩两种可能：技能名全部**有歧义**（沾上了两个以上规范技能），
            # 或成果本来就没带技能。前者不能凭原始写法升级，否则等于放行认错的技能。
            invalid.append({
                "index": index,
                "reason": f"技能名全部有歧义、无法确定对应哪个规范技能：{missed}",
                "event": cleaned["title"],
            })
            continue

        fingerprint = event_fingerprint(cleaned["type"], cleaned["title"], canonical)
        record = {**cleaned, "skills": canonical, "fingerprint": fingerprint}
        accepted_events.append(record)

        if fingerprint in seen:
            duplicates.append({"fingerprint": fingerprint, "title": cleaned["title"],
                               "reason": "该成果历史上已处理过，跳过以避免重复升级"})
            continue

        cap_rank = level_rank(_TYPE_LEVEL_CAP[cleaned["type"]])
        for skill in canonical:
            current = user_levels.get(skill, 0)
            candidate = min(current + 1, cap_rank)

            if candidate <= current:
                skipped.append({
                    "skill": skill,
                    "type": cleaned["type"],
                    "title": cleaned["title"],
                    "current_level": level_name(current),
                    "reason": (
                        f"已达该成果类型的等级上限（{_TYPE_LEVEL_CAP[cleaned['type']]}），不再提升"
                        if current >= cap_rank
                        else "当前等级已不低于可提升到的档位，只升不降"
                    ),
                })
                continue

            gate = resolve_gate(skill, user_levels, skill_base, knowledge_base_status)
            if not gate["passed"]:
                blocked.append({
                    "skill": skill,
                    "type": cleaned["type"],
                    "title": cleaned["title"],
                    "current_level": level_name(current),
                    "candidate_level": level_name(candidate),
                    "missing_prerequisites": gate["missing"],
                    "gate_status": gate["gate_status"],
                    "gate_unavailable": gate["gate_unavailable"],
                    "reason": (
                        gate["reason"] + f"；必须先满足它，才谈得上提升 {skill}"
                        if gate["missing"]
                        else gate["reason"]
                    ),
                })
                continue

            entry = {
                "skill": skill,
                "from": level_name(current),          # 缺席为 None，绝不写"无"
                "to": level_name(candidate),
                "reason": (
                    f"{'项目' if cleaned['type'] == EVENT_PROJECT else '学习'}成果佐证，"
                    f"按一档封顶提升；该成果类型的等级上限为 {_TYPE_LEVEL_CAP[cleaned['type']]}"
                ),
                "source": cleaned["title"],
                "evidence": cleaned["evidence"],
                "evidence_kind": cleaned["evidence_kind"],
                "note": cleaned.get("note", ""),
                # 门禁标记：True=已收录且齐备 / False=明确未收录 / None=无从得知
                "covered_by_knowledge_base": gate["covered"],
                "gate_status": gate["gate_status"],
                "gate_unavailable": gate["gate_unavailable"],
                "gate_reason": gate["reason"],
            }
            entry_note = None
            if gate["gate_status"] == GATE_UNAVAILABLE:
                entry_note = NOTE_GATE_UNAVAILABLE
            elif gate["gate_status"] == GATE_NOT_COVERED:
                entry_note = NOTE_UNCOVERED
            if entry_note:
                entry["notes"] = [entry_note]
                if entry_note not in notes:
                    notes.append(entry_note)

            previous = updates.get(skill)
            if previous is None:
                updates[skill] = entry
            elif level_rank(entry["to"]) > level_rank(previous["to"]):
                # 同一轮内多条成果命中同一技能：取提升幅度大的那条，仍然只升一档
                updates[skill] = entry
            else:
                skipped.append({
                    "skill": skill,
                    "type": cleaned["type"],
                    "title": cleaned["title"],
                    "reason": "同一轮内已有其它成果覆盖该技能，按「每次最多升一档」不叠加",
                })

    ordered = [updates[key] for key in sorted(updates)]
    return {
        "events": accepted_events,
        "updates": ordered,
        "blocked": blocked,
        "skipped": skipped,
        "invalid": invalid,
        "duplicates": duplicates,
        "unmapped": unmapped,
        "notes": notes,
        # 回显本次采用的知识库状态，供 growth_agent 判断要不要告警
        "knowledge_base_status": knowledge_base_status,
    }


def _safe_repr(event) -> str:
    """非法事件的简要描述 —— 只用于记录，避免把整个对象塞进报告。"""
    if isinstance(event, dict):
        pieces = []
        for key in ("type", "title"):
            value = str(event.get(key) or "").strip()
            if value:
                pieces.append(f"{key}={value}")
        return " / ".join(pieces) or "<空对象>"
    return f"<{type(event).__name__}>"


# ── 把升级写回能力表 ────────────────────────────────────────────────────
def apply_upgrades(user_skills, updates) -> list[dict]:
    """把升级结果写回能力表，返回**新的列表**（不改动入参）。

    - 已有技能：只替换 ``level``，**保留原有 evidence**（原始判断依据不该被覆盖，
      本次升级的依据记在 ``skill_updates[].source`` 里）；位置不变。
      本次升级由成长事件产生，``evidence_kind`` 跟随升级条目：附了凭证是
      ``growth``，自述完成是 ``self_report``；**自述不得覆盖已有的强证据**。
    - 缺席技能：追加到末尾，``evidence`` 取该成果的证据，``evidence_kind``
      同样跟随升级条目（缺省按有无证据推断）。
    - ``to`` 一定是四档之一；若上游传来非法值，这里直接跳过，绝不写进能力表。
    """
    result = [dict(item) for item in (user_skills or []) if isinstance(item, dict)]
    index = {pair_key(item.get("skill")): i for i, item in enumerate(result)}

    for update in updates or []:
        skill = str(update.get("skill") or "").strip()
        target = level_name(level_rank(update.get("to")))
        if not skill or target is None:
            continue  # 非法等级：宁可不升，也不写坏能力表

        source = str(update.get("source") or "").strip()
        evidence = str(update.get("evidence") or "").strip()
        kind = str(update.get("evidence_kind") or "").strip()
        if kind not in VALID_EVIDENCE_KINDS:
            kind = EVIDENCE_KIND_GROWTH if evidence else EVIDENCE_KIND_SELF_REPORT
        key = pair_key(skill)
        position = index.get(key)
        if position is None:
            result.append({"skill": skill, "level": target,
                           "evidence": evidence,
                           "evidence_kind": kind,
                           "evidence_ref": source})
            index[key] = len(result) - 1
        else:
            result[position]["level"] = target
            current_kind = str(result[position].get("evidence_kind") or "").strip()
            if kind in _STRONG_EVIDENCE_KINDS or current_kind not in _STRONG_EVIDENCE_KINDS:
                result[position]["evidence_kind"] = kind
            if source:
                result[position]["evidence_ref"] = source

    return result


# ── R8：缺口变化 ────────────────────────────────────────────────────────
def gap_change_report(gaps_before, gaps_after, vocabulary=None) -> dict:
    """对比前后两套 skill_gaps，按**规范技能名**配对。

    绝不按下标 zip —— ``judge_gaps`` 会按"优先级 → 重要度 → 技能名"重排，
    两侧顺序不同是常态，用下标配对会把 A 的状态安到 B 头上。

    返回 ``{changes, newly_satisfied, newly_appeared, resolved}``，四项都按技能名排序。
    """
    before = _index_gaps(gaps_before, vocabulary)
    after = _index_gaps(gaps_after, vocabulary)

    changes: list[dict] = []
    newly_satisfied: list[str] = []
    newly_appeared: list[str] = []
    resolved: list[str] = []

    for key in sorted(set(before) | set(after)):
        item_before = before.get(key)
        item_after = after.get(key)

        if item_before is None:
            newly_appeared.append(key)
            continue
        if item_after is None:
            resolved.append(key)
            continue

        entry = {
            "skill": key,
            "status_before": item_before.get("status"),
            "status_after": item_after.get("status"),
            # 对外结论（叠加证据后的状态）：从「未验证」变成「已满足」也是真实变化
            "display_status_before": item_before.get("display_status") or item_before.get("status"),
            "display_status_after": item_after.get("display_status") or item_after.get("status"),
            "verification_before": item_before.get("verification"),
            "verification_after": item_after.get("verification"),
            "priority_before": item_before.get("priority"),
            "priority_after": item_after.get("priority"),
            "change": "无变化",
        }
        status_changed = entry["display_status_before"] != entry["display_status_after"]
        priority_changed = entry["priority_before"] != entry["priority_after"]
        if status_changed or priority_changed:
            entry["change"] = "状态变化" if status_changed else "优先级变化"
            changes.append(entry)

        if (
            entry["display_status_before"] != SATISFIED_STATUS
            and entry["display_status_after"] == SATISFIED_STATUS
        ):
            newly_satisfied.append(key)

    return {
        "changes": changes,
        "newly_satisfied": sorted(newly_satisfied),
        "newly_appeared": sorted(newly_appeared),
        "resolved": sorted(resolved),
    }


def _index_gaps(gaps, vocabulary=None) -> dict[str, dict]:
    """把缺口列表按配对键索引；同名取第一条，保持与 judge_gaps 的去重口径一致。"""
    indexed: dict[str, dict] = {}
    for gap in gaps or []:
        if not isinstance(gap, dict):
            continue
        key = pair_key(gap.get("skill"), vocabulary)
        if key and key not in indexed:
            indexed[key] = gap
    return indexed


def compute_growth_diff(
    user_skills_before,
    user_skills_after,
    gaps_before=None,
    gaps_after=None,
    vocabulary=None,
    updates=None,
    blocked=None,
    match_rate_before=None,
    match_rate_after=None,
) -> dict:
    """汇总本次运行的成长差异。纯函数，不调模型。

    ``match_rate_*`` 只有在**同一份 target_job / JD**下才可比；调用方负责保证这一点，
    不一致时应当传 None 而不是硬凑对比。
    """
    gaps = gap_change_report(gaps_before, gaps_after, vocabulary)
    after_rank = {pair_key(i.get("skill"), vocabulary): level_rank(i.get("level"))
                  for i in (user_skills_after or []) if isinstance(i, dict)}
    before_rank = {pair_key(i.get("skill"), vocabulary): level_rank(i.get("level"))
                   for i in (user_skills_before or []) if isinstance(i, dict)}

    skills_added = sorted(set(after_rank) - set(before_rank))
    skills_removed = sorted(set(before_rank) - set(after_rank))

    delta = None
    if match_rate_before is not None and match_rate_after is not None:
        delta = round(float(match_rate_after) - float(match_rate_before), 4)

    return {
        "level_changes": [dict(u) for u in (updates or [])],
        "blocked_upgrades": [dict(b) for b in (blocked or [])],
        "gap_changes": gaps["changes"],
        "newly_satisfied": gaps["newly_satisfied"],
        "newly_appeared": gaps["newly_appeared"],
        "resolved_gaps": gaps["resolved"],
        "skills_added": skills_added,
        "skills_removed": skills_removed,
        "match_rate_before": match_rate_before,
        "match_rate_after": match_rate_after,
        "match_rate_delta": delta,
    }
