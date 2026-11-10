"""阶段六：把闭环里"已完成的任务"翻译成阶段五能认的成长成果。

它在闭环里的位置
----------------
::

    LoopStore(runs/loop.json)          GrowthStore(runs/growth.json)
    ┌────────────────────┐             ┌──────────────────────┐
    │ r001-l01 已完成 ✔   │  本模块 →   │ 成长成果 → 等级判定    │
    │ r001-p01 待完成     │  翻译       │ （阶段五的规则，不重写）│
    └────────────────────┘             └──────────────────────┘

``loop.json`` 记"学生做完没做完"，``growth.json`` 记"能力确认到什么程度"。
本模块是两者之间的**单向翻译器**：读 ``loop.json`` 里的任务、看一眼 ``growth.json``
的历史，把任务变成 ``growth_events`` 那种形状的成果。

三条硬边界
----------
1. **本模块不写 ``growth.json``。** 正常记账的唯一写入方是阶段五的 ``growth_node``
   （:func:`agents.growth_agent.growth_node`）；用户显式作废另走
   ``agents.growth_adjust`` 追加调整记录。在这里再写一次，同一份历史就会被写两遍，
   ``user_skills_after`` 快照与 ``history`` 条数随之错位。
   所以 :func:`evaluate_task_growth` 算出的是**真实计算、但不落盘**的升级计划：
   指纹集合、技能知识库、前置门禁全部取自阶段五的现状，只是由 workflow 那一侧去写。
2. **本模块只通过 ``LoopStore.mark_completed()`` 写 ``loop.json``。**
   而且**先写完成记录，再谈成长**：技能升不升得上去，绝不能反过来影响
   "这个任务做完了"这件事（业务规则 1）。
3. **不重实现任何阶段五的规则。** 等级阶梯、类型等级上限、单轮最多升一档、
   前置门禁、成果指纹 —— 全部调用 ``growth_rules`` / ``growth_agent`` 的真接口。

为什么去重不能靠 task_id
------------------------
``task_id`` 带轮次号（``r001-l01`` / ``r002-l01``），**每一轮都不一样**。
拿它当去重键，同一件成果换个轮次就会再升一档。所以这里只传 **任务标题 + 关联技能**
给阶段五，由 ``growth_rules.event_fingerprint()`` 按 ``type \\x1f title \\x1f 技能``
算指纹 —— 与 ``growth_node`` 走的是同一条去重路径（业务规则 3）。

没有证据就不构造成果
--------------------
``growth_rules.validate_event()`` 要求 ``evidence`` 非空（"仅凭标题不得升级"）。
:func:`build_growth_event` 在更早一步就返回 ``no_evidence``，**不伪造证据**、
**也不把空证据的事件塞进流水线**去污染历史（业务规则 6）。
"""

from __future__ import annotations

from agents.growth_agent import (
    OUTCOME_ALL_DUPLICATES,
    OUTCOME_BLOCKED,
    OUTCOME_GATE_UNAVAILABLE,
    OUTCOME_INVALID_EVENTS,
    OUTCOME_NO_UPGRADE,
    OUTCOME_STORE_UNREADABLE,
    OUTCOME_UPGRADED,
    # 下面两个是阶段五的私有符号，**刻意复用**：判定"哪些结果标签成立、按什么优先级挑一个"
    # 的规则只能有一份。阶段五若改名，这里必须同步（测试里有一条兼容性断言盯着）。
    _OUTCOME_PRIORITY,
    _classify,
    get_store as get_growth_store,
)
from agents.growth_rules import (
    EVENT_LEARNING,
    EVENT_PROJECT,
    EVIDENCE_KIND_GROWTH,
    EVIDENCE_KIND_SELF_REPORT,
    VALID_EVIDENCE_KINDS,
    event_fingerprint,
    normalize_skills,
    plan_upgrades,
)
from agents.growth_store import load_skill_base
from agents.loop_store import (
    COMPLETE_ALREADY,
    COMPLETE_OK,
    KIND_LEARNING,
    KIND_PROJECT,
    TASK_COMPLETED,
    LoopStore,
)
from agents.skill_dict import SkillVocabulary


class GrowthLoopError(RuntimeError):
    """调用方传了结构不合法的数据（不是字典、skills 不是列表之类）。"""


# 任务类型 → 阶段五的成果类型。两边字符串目前一致（learning / project），
# 但这个映射**故意写死**：真出现分歧，宁可在这里明确失效，也不要靠"字符串恰好一样"。
_EVENT_TYPE = {KIND_LEARNING: EVENT_LEARNING, KIND_PROJECT: EVENT_PROJECT}

# ── build_growth_event 的返回状态 ───────────────────────────────────────
EVENT_OK = "ok"
EVENT_NO_EVIDENCE = "no_evidence"        # 没证据 → 不构造、不伪造
EVENT_NO_SKILLS = "no_skills"            # 任务没关联任何技能
EVENT_UNKNOWN_KIND = "unknown_kind"      # kind 不在 loop_store.KINDS 里
EVENT_INVALID_TASK = "invalid_task"      # 不是字典 / 缺标题

# ── process_task_completion 的整体状态 ──────────────────────────────────
# 另外几种情况直接复用 loop_store 的状态码（already_completed / unknown_task /
# invalid_task_id / invalid_evidence / invalid_round / unknown_round / refused / write_failed）。
LOOP_COMPLETED_UPGRADED = "completed_upgraded"           # 完成 + 生成成果 + 有计划升级
LOOP_COMPLETED_NO_UPGRADE = "completed_no_upgrade"       # 完成 + 生成成果 + 未升级
LOOP_COMPLETED_NO_EVENT = "completed_no_growth_event"    # 完成，但构不成成果
LOOP_COMPLETION_FAILED = "completion_failed"             # 完成记录没写成

# 阶段五的结论标签 → 一句人话（本模块只做翻译，不改判）
_REASON_BY_OUTCOME = {
    OUTCOME_UPGRADED: "任务已记录完成，并按阶段五规则算出了升级计划"
                      "（本模块不落盘：成长历史由 growth_node 统一写）",
    OUTCOME_ALL_DUPLICATES: "任务已记录完成；同一成果历史上已处理过，不重复升级",
    OUTCOME_BLOCKED: "任务已记录完成；升级被前置技能门禁拦下",
    OUTCOME_GATE_UNAVAILABLE: "任务已记录完成；技能知识库不可用，前置门禁本次**没有执行**",
    OUTCOME_INVALID_EVENTS: "任务已记录完成；该任务构不成有效成果，未评估升级",
    OUTCOME_NO_UPGRADE: "任务已记录完成；按阶段五规则本次不产生升级",
    OUTCOME_STORE_UNREADABLE: "任务已记录完成；成长历史不可读，本次不评估升级",
}


# 可替换的闭环存储：默认指向项目下的 runs/。
# 与 growth_agent.set_store() 同款注入方式 —— 测试必须能把目录换成临时目录，
# 否则跑一次用例就会在项目里写出 runs/loop.json。
_loop_store: LoopStore | None = None


def set_loop_store(store: LoopStore | None) -> None:
    """注入闭环记录存储（测试用）。传 None 表示下次调用时重建默认存储。"""
    global _loop_store
    _loop_store = store


def get_loop_store() -> LoopStore:
    """取闭环记录存储；默认是项目下 runs/loop.json 的读写入口。"""
    global _loop_store
    if _loop_store is None:
        _loop_store = LoopStore()
    return _loop_store


def load_vocabulary() -> tuple[SkillVocabulary | None, str | None]:
    """取技能词表。**永不抛出**，返回 ``(词表, 告警)``。

    词表拿不到时返回 ``(None, 原因)``：此时不做归一，任务里的技能会**全部**落进
    未识别清单 —— 这是如实反映，不是"这些技能都不存在"。
    """
    try:
        return SkillVocabulary(), None
    except Exception as exc:  # noqa: BLE001 - 降级路径，不能中断流程
        return None, (
            f"技能词表加载失败（{type(exc).__name__}: {exc}）；"
            f"本次不归一技能名，任务技能会全部列进未识别清单"
        )


# ── 技能映射 ────────────────────────────────────────────────────────────
def _task_skill_names(task) -> list[str]:
    """取任务关联的原始技能名（保序去重）。结构不合法直接抛。"""
    if not isinstance(task, dict):
        raise GrowthLoopError(f"任务必须是字典，收到 {type(task).__name__}")
    raw = task.get("skills")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise GrowthLoopError(f"任务的 skills 必须是列表，收到 {type(raw).__name__}")
    names: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def map_task_skills(task, vocabulary=None) -> dict:
    """把任务关联技能折算成成长记账用的技能键。

    直接调用 ``growth_rules.normalize_skills()``，因此与阶段五同一口径：
    **唯一命中词表才折算成规范名**；词表没收录的（非 AI 岗位的缺口名）按原名记账
    并照样列进 ``unmapped``；沾上多个的（歧义）丢弃。**绝不自动映射到相似技能**，
    也**绝不悄悄丢掉**（业务规则 8）。

    返回 ``{raw, canonical, unmapped, vocabulary_available}``。
    """
    raw = _task_skill_names(task)
    canonical, unmapped = normalize_skills(raw, vocabulary)
    return {
        "raw": raw,
        "canonical": canonical,
        "unmapped": unmapped,
        "vocabulary_available": vocabulary is not None,
    }


# ── 成果构造 ────────────────────────────────────────────────────────────
def _event_failure(status: str, reason: str) -> dict:
    return {"status": status, "reason": reason, "event": None}


def build_growth_event(task, evidence=None, evidence_kind=None, note=None) -> dict:
    """从任务与完成证据构造阶段五可识别的成长成果。

    返回 ``{"status", "reason", "event"}``；``event`` 为 None 表示**不该升到这个流水线里去**。

    几条刻意的选择：

    - ``title`` 用**任务标题原文**，不拼轮次号、不拼 task_id。
      一旦拼了，指纹每轮都变，阶段五的去重就永远不生效（业务规则 3）。
    - ``skills`` 用**任务里的原始技能名**，不在这里预先归一。阶段五的
      ``plan_upgrades()`` 自己会归一、自己会报告未识别项；这里再折算一次，
      等于把同一个决策做两遍，还可能和阶段五的结果对不上。
    - ``evidence`` **原样透传**（只对"是不是空的"做判断，不做清洗/改写）。
      自述完成（``evidence_kind=self_report``）允许空证据：升级照规则走，
      但下游评估只按"未验证"处理；附了凭证（``growth``）则必须有证据。
    - ``note`` 是用户心得，单独保留，**绝不并入 evidence、也不当凭证**。
    - ``type`` 由 ``kind`` 映射并校验；认不出的类型返回 ``unknown_kind``，
      **不做默认假设**（与 ``growth_rules.validate_event`` 的口径一致）。
    """
    if not isinstance(task, dict):
        return _event_failure(EVENT_INVALID_TASK, f"任务必须是字典，收到 {type(task).__name__}")

    kind = str(task.get("kind") or "").strip()
    event_type = _EVENT_TYPE.get(kind)
    if event_type is None:
        return _event_failure(
            EVENT_UNKNOWN_KIND,
            f"任务类型非法：{kind!r}；只接受 {sorted(_EVENT_TYPE)}，不做默认假设",
        )

    title = str(task.get("title") or "").strip()
    if not title:
        return _event_failure(EVENT_INVALID_TASK, "任务没有标题，无法构成成果")

    skills = _task_skill_names(task)
    if not skills:
        return _event_failure(
            EVENT_NO_SKILLS,
            "任务没有关联任何技能 —— 没有涉及技能的成果无法用于升级",
        )

    if evidence is None:
        # 不传就取任务自己留下的那份 —— 已完成任务的证据就存在 loop.json 里
        evidence = task.get("evidence")
    text = "" if evidence is None else str(evidence)

    kind_text = str(evidence_kind or task.get("evidence_kind") or "").strip()
    if kind_text not in VALID_EVIDENCE_KINDS:
        # 旧任务没有该字段：有证据按 growth，没证据只能算自述
        kind_text = EVIDENCE_KIND_GROWTH if text.strip() else EVIDENCE_KIND_SELF_REPORT
    if kind_text != EVIDENCE_KIND_SELF_REPORT and not text.strip():
        return _event_failure(
            EVENT_NO_EVIDENCE,
            "附凭证完成必须有证据；仅凭标题不得宣称技能提升",
        )

    if note is None:
        note = task.get("note")
    note_text = "" if note is None else str(note)

    return {
        "status": EVENT_OK,
        "reason": None,
        "event": {
            "type": event_type,
            "title": title,
            "skills": skills,
            "evidence": text,                        # 原样保留
            "evidence_kind": kind_text,
            "note": note_text,
            "at": task.get("completed_at") or None,  # 真实来源时间，不编
        },
    }


def growth_events_from_tasks(tasks) -> dict:
    """把一批**已完成**的任务批量翻成成长成果（纯函数，不读盘不写盘）。

    未完成的任务**不翻**：没做完的事不该产生"能力提升"的证据。
    返回 ``{events, skipped}``，``skipped`` 逐条说明哪条为什么没翻。
    """
    events: list[dict] = []
    skipped: list[dict] = []
    for index, task in enumerate(tasks or [], start=1):
        if not isinstance(task, dict):
            skipped.append({"index": index, "task_id": None, "status": EVENT_INVALID_TASK,
                            "reason": f"任务必须是字典，收到 {type(task).__name__}"})
            continue
        task_id = task.get("task_id")
        if task.get("status") != TASK_COMPLETED:
            skipped.append({"index": index, "task_id": task_id, "status": "not_completed",
                            "reason": "任务尚未完成，不产生成长成果"})
            continue
        result = build_growth_event(task)
        if result["event"] is None:
            skipped.append({"index": index, "task_id": task_id,
                            "status": result["status"], "reason": result["reason"]})
            continue
        events.append(result["event"])
    return {"events": events, "skipped": skipped}


def pending_growth_events(loop_store=None, growth_store=None, vocabulary=None) -> dict:
    """列出"已完成、但还没由 ``growth_node`` 处理过"的成长成果 —— **跨会话可重建**。

    为什么需要它
    ------------
    用户提交完成证据后，成果先进内存队列，等点「下一轮计划」才交给 ``growth_node``
    落盘。如果提交完就退出，那条成果只留在 ``loop.json`` 的完成记录里，
    下一次会话不会再被回顾 —— 能力永远升不上去。

    判据只用**阶段五自己的指纹**，不新造"已处理"标记：

    - 把已完成任务翻成成果，按 ``normalize_skills`` 归一后算 ``event_fingerprint``；
    - 指纹**已在**成长历史里 → ``growth_node`` 处理过了，不再排队；
    - 指纹**不在** → 还没成功升级（或压根没交过），继续排队。

    即使这里的判断偏保守、把一条已处理的成果又喂了一次，``growth_node`` 也会拿
    同一个指纹判重，**不会重复升级** —— 安全性不依赖本函数的判断是否完美，
    而是依赖"指纹只有 ``growth_node`` 走正式写入路径时才会进历史"。

    注意：指纹只在成果**真的带来升级**时才写进历史（阶段五的 ``_events_for_history``）。
    所以"被前置拦下"的成果重启后会再次排队，这是**有意的** —— 前置补齐后它就该能升上去。

    返回 ``{events, tasks, skipped, warnings, seen, restored}``。
    成长历史不可读时**不自动排队**（拿不到指纹集合就没法保证不重复），只给告警。
    """
    store = loop_store if loop_store is not None else get_loop_store()
    gstore = growth_store if growth_store is not None else get_growth_store()
    warnings: list[str] = []

    if vocabulary is None:
        vocabulary, vocab_warning = load_vocabulary()
        if vocab_warning:
            warnings.append(vocab_warning)

    loaded = gstore.load()
    if loaded.warning and loaded.warning not in warnings:
        warnings.append(loaded.warning)
    if not loaded.writable:
        warnings.append(
            f"成长历史不可读（{loaded.status}），没法判断哪些成果已经处理过；"
            f"为避免重复提交，本次不自动恢复待办成果"
        )
        return {"events": [], "tasks": [], "skipped": [], "warnings": warnings,
                "seen": 0, "restored": False}

    seen = gstore.seen_fingerprints(loaded.data)
    events: list[dict] = []
    tasks: list[dict] = []
    skipped: list[dict] = []
    queued: set[str] = set()

    for task in store.tasks(status=TASK_COMPLETED):
        built = build_growth_event(task)
        if built["event"] is None:
            skipped.append({"task_id": task.get("task_id"), "status": built["status"],
                            "reason": built["reason"]})
            continue
        canonical, missed = normalize_skills(built["event"]["skills"], vocabulary)
        if not canonical:
            skipped.append({"task_id": task.get("task_id"), "status": "skills_unmapped",
                            "reason": f"技能名全部有歧义、无法确定对应哪个规范技能：{missed}"})
            continue
        fingerprint = event_fingerprint(built["event"]["type"],
                                        built["event"]["title"], canonical)
        if fingerprint in seen or fingerprint in queued:
            # 已经在成长历史里（growth_node 处理过），或本轮待办里已有同一条成果
            continue
        queued.add(fingerprint)
        events.append(built["event"])
        tasks.append({"task_id": task.get("task_id"), "title": task.get("title"),
                      "fingerprint": fingerprint})

    return {"events": events, "tasks": tasks, "skipped": skipped, "warnings": warnings,
            "seen": len(seen), "restored": True}


# ── 升级评估（真实计算，但不落盘）────────────────────────────────────────
def _evaluate_event(event: dict, growth_store, vocabulary, warnings: list,
                    profile_skills=None) -> dict:
    """按阶段五规则评估**单条已构造好的成果**。只读，不写任何存储。

    ``profile_skills`` 就是"本次运行 profile 提取到的能力"（``state["user_skills"]``）。
    给了它，基线走 ``GrowthStore.merge_with_profile()`` —— **与 growth_node 一模一样的
    构造方式**（历史 ∪ profile，取高者）。这一点非有不可：

        growth_node 只在"有候选成果"时才追加历史。所以首轮跑完、还没提交过任何成果时
        ``growth.json`` 根本不存在，``latest_skills()`` 会返回空表；拿这张空表当基线，
        连 Python / Embedding 这类**profile 里明明有**的技能的会算成"缺前置"，
        于是预览报"被门禁拦下"，而下一轮 growth_node 却照升不误 —— 预览就成了假话。

    不给 ``profile_skills``（历史里已经有完整能力表、或调用方拿不到 profile）时，
    退回 ``latest_skills()``：那是历史最后一次确认的能力表。

    成长历史不可读时**直接不评估**：那种情况下基线会退化成空表，
    算出来的"无 → 入门"是编的，不是事实。
    """
    store = growth_store if growth_store is not None else get_growth_store()
    loaded = store.load()
    if loaded.warning and loaded.warning not in warnings:
        warnings.append(loaded.warning)

    kb = load_skill_base()
    if not kb.gate_available and kb.warning and kb.warning not in warnings:
        warnings.append(kb.warning)

    result = {
        "evaluated": False,
        "event": event,
        "reason": None,
        "warnings": warnings,
        "updates": [], "blocked": [], "skipped": [], "invalid": [],
        "duplicates": [], "unmapped": [], "notes": [],
        "baseline_skills": [],
        "baseline_source": None,
        "store_status": loaded.status,
        "knowledge_base_status": kb.status,
        "signals": [OUTCOME_STORE_UNREADABLE],
        "outcome": OUTCOME_STORE_UNREADABLE,
    }

    if not loaded.writable:
        # 拿不到历史 → 拿不到指纹集合，也拿不到真实基线。不硬凑一个"看起来像升级"的结论。
        result["reason"] = (
            f"成长历史不可读（{loaded.status}），本次不评估升级："
            f"没有历史基线会算出虚假的「无 → 入门」"
        )
        return result

    if profile_skills is None:
        baseline = store.latest_skills(loaded.data)
        baseline_source = "history_only"
    else:
        # 与 growth_node 的 merge.user_skills 同一条路径
        baseline = store.merge_with_profile(profile_skills, vocabulary,
                                            data=loaded.data).user_skills
        baseline_source = "profile_merged"

    plan = plan_upgrades(
        [event],
        baseline,
        vocabulary,
        skill_base=kb.base,
        seen_fingerprints=store.seen_fingerprints(loaded.data),
        knowledge_base_status=kb.status,
    )
    for note in plan["notes"]:
        if note not in warnings:
            warnings.append(note)

    # 复用阶段五的分类器：同一批"结果标签"、同一套优先级，只是这里作用在单条成果上。
    # 传**输入事件**而不是 plan["events"]，否则一条被判非法的成果会被同时贴上
    # "没有事件"和"有非法事件"两个标签，而前者优先级更高、把后者盖掉。
    signals = _classify([event], plan, kb.status, loaded.status)
    outcome = next(label for label in _OUTCOME_PRIORITY if label in signals)

    result.update({
        "evaluated": True,
        "reason": _REASON_BY_OUTCOME.get(outcome),
        "updates": plan["updates"],
        "blocked": plan["blocked"],
        "skipped": plan["skipped"],
        "invalid": plan["invalid"],
        "duplicates": plan["duplicates"],
        "unmapped": plan["unmapped"],
        "notes": plan["notes"],
        "baseline_skills": baseline,
        "baseline_source": baseline_source,
        "signals": signals,
        "outcome": outcome,
    })
    return result


def evaluate_task_growth(task, growth_store=None, vocabulary=None,
                         profile_skills=None) -> dict:
    """从**任务**出发评估它能带来什么升级。纯读取，不写任何存储。

    ``vocabulary=None`` 表示自动加载词表；加载失败则退回"不归一"并在 warnings 里说明。
    ``profile_skills`` 见 :func:`_evaluate_event` —— 传了它，预览才和 growth_node 同一基线。
    """
    warnings: list[str] = []
    built = build_growth_event(task)
    if built["event"] is None:
        return {
            "evaluated": False, "event": None, "reason": built["reason"],
            "warnings": warnings,
            "updates": [], "blocked": [], "skipped": [], "invalid": [],
            "duplicates": [], "unmapped": [], "notes": [],
            "baseline_skills": [], "baseline_source": None,
            "store_status": None, "knowledge_base_status": None,
            "signals": [], "outcome": None,
        }

    if vocabulary is None:
        vocabulary, vocab_warning = load_vocabulary()
        if vocab_warning:
            warnings.append(vocab_warning)

    return _evaluate_event(built["event"], growth_store, vocabulary, warnings,
                           profile_skills)


# ── 对外主流程 ──────────────────────────────────────────────────────────
def process_task_completion(
    task_id,
    evidence="",
    round_no=None,
    loop_store=None,
    growth_store=None,
    vocabulary=None,
    now=None,
    profile_skills=None,
    evidence_kind=None,
    note=None,
) -> dict:
    """完成一个任务，并把它带来的成长算清楚。

    顺序是**刻意的**（业务规则 1）::

        1. 查询任务（只读）
        2. LoopStore.mark_completed()  ← 先持久化完成记录
        3. 构造成长成果
        4. 按阶段五规则评估升级（只读，不落盘）

    第 3、4 步无论成败都**不会回滚**第 2 步：任务完成状态不依赖技能升级结果
    （业务规则 2、5）。反过来，第 2 步没写成时**不做**第 3、4 步 —— 完成记录是闭环的
    唯一凭据，凭据没落盘就生成成长成果，等于给一件没有记录的事发升级。

    ``task_id`` 非法 / 任务不存在 / 已经完成，都由 ``LoopStore.mark_completed()`` 判定，
    本函数原样转达它的状态码，**不重复触发成长**（业务规则 4）。

    关键返回字段：``status``、``already_completed``、``growth_event``、
    ``growth_evaluated``、``upgraded``、``growth_outcome``、``unmapped_skills``、``warnings``。
    """
    store = loop_store if loop_store is not None else get_loop_store()
    warnings: list[str] = []

    text = "" if evidence is None else str(evidence)
    kind_text = str(evidence_kind or "").strip()
    if kind_text not in VALID_EVIDENCE_KINDS:
        kind_text = EVIDENCE_KIND_GROWTH if text.strip() else EVIDENCE_KIND_SELF_REPORT

    # 1) 查询（只读）—— 只用于报告，判定权在 mark_completed
    existing = store.find_task(task_id)

    # 2) 先持久化完成记录
    completion = store.mark_completed(
        task_id, evidence=text, round_no=round_no, now=now,
        evidence_kind=kind_text, note="" if note is None else str(note),
    )
    completion_status = completion.get("status")

    result = {
        "task_id": task_id,
        "status": completion_status,
        "reason": None,
        "task": completion.get("task") or existing,
        "completion_status": completion_status,
        "completion_written": bool(completion.get("written")),
        "already_completed": completion_status == COMPLETE_ALREADY,
        # 技能映射（任务层）
        "skills_raw": [],
        "skills_canonical": [],
        "unmapped_skills": [],
        # 成长成果
        "growth_event": None,
        "growth_event_status": None,
        # 成长评估
        "growth_evaluated": False,
        "growth_outcome": None,
        "growth_signals": [],
        "upgraded": False,
        "updates": [],
        "blocked": [],
        "skipped": [],
        "invalid_events": [],
        "duplicate_events": [],
        "unmapped_events": [],
        "notes": [],
        "baseline_skills": [],
        "baseline_source": None,
        "store_status": None,
        "knowledge_base_status": None,
        "warnings": warnings,
    }

    # 2a) 完成记录没写成（非法 ID / 找不到任务 / 存储损坏 / 写盘失败）
    if completion_status not in (COMPLETE_OK, COMPLETE_ALREADY):
        result["status"] = LOOP_COMPLETION_FAILED
        reason = completion.get("warning") or f"任务完成未记录（{completion_status}）"
        result["reason"] = reason
        warnings.append(reason)
        return result

    # 2b) 此前已完成：不再触发成长（业务规则 4）
    if completion_status == COMPLETE_ALREADY:
        result["reason"] = completion.get("warning") or "任务此前已完成，不重复触发成长"
        return result

    task = completion.get("task") or existing or {}

    # 3) 技能映射 + 构造成长成果（词表必须先就位，否则映射会全落到未识别清单）
    if vocabulary is None:
        vocabulary, vocab_warning = load_vocabulary()
        if vocab_warning:
            warnings.append(vocab_warning)

    mapping = map_task_skills(task, vocabulary)
    result["skills_raw"] = mapping["raw"]
    result["skills_canonical"] = mapping["canonical"]
    result["unmapped_skills"] = mapping["unmapped"]

    built = build_growth_event(task)
    result["growth_event_status"] = built["status"]
    if built["event"] is None:
        result["status"] = LOOP_COMPLETED_NO_EVENT
        result["reason"] = built["reason"]
        warnings.append(built["reason"])
        return result
    result["growth_event"] = built["event"]

    # 4) 按阶段五规则评估升级（只读）
    evaluation = _evaluate_event(built["event"], growth_store, vocabulary, warnings,
                                 profile_skills)
    result["growth_evaluated"] = evaluation["evaluated"]
    result["growth_outcome"] = evaluation["outcome"]
    result["growth_signals"] = evaluation["signals"]
    result["updates"] = evaluation["updates"]
    result["blocked"] = evaluation["blocked"]
    result["skipped"] = evaluation["skipped"]
    result["invalid_events"] = evaluation["invalid"]
    result["duplicate_events"] = evaluation["duplicates"]
    result["unmapped_events"] = evaluation["unmapped"]
    result["notes"] = evaluation["notes"]
    result["baseline_skills"] = evaluation["baseline_skills"]
    result["baseline_source"] = evaluation["baseline_source"]
    result["store_status"] = evaluation["store_status"]
    result["knowledge_base_status"] = evaluation["knowledge_base_status"]
    result["upgraded"] = bool(evaluation["updates"])

    result["status"] = LOOP_COMPLETED_UPGRADED if result["upgraded"] else LOOP_COMPLETED_NO_UPGRADE
    result["reason"] = evaluation["reason"] or _REASON_BY_OUTCOME.get(evaluation["outcome"])
    return result
