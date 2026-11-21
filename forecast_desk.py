"""按决策点还原可用预报、同场评分与值班采用。

原则（与运营约定一一对应）：
- 评分目标统一为入馆人数（entered）；寄存等候区（waiting）单列，不计入。
- 每个决策点（早班排工 / 当日增援）只统计当时切实可用的预报版本：
  出具提前量不早于该场对应 cut，且不是结果公开后补写的。
- 满员截断的场不用确认进场人数给点误差满分；迟报、取消、未开场分别说明
  观察能支持哪些评价。
- 来源比较只落在共同支撑的场次上（统一目标、统一提前量、重叠场次）；
  没有共同支撑时，各自能说明的范围分开保留。
- 拥挤线修订后，旧的直接超阈概率失效，只能由分位数推算，并重新判断该
  来源在哪些场次还能采用。
- 已通知的早班人力不可释放为增援能力；真实余量 = 可用 - 已通知 - 最低保留；
  同一 pool 同时只能分给一个场次。
"""
import argparse
from collections import defaultdict
from pathlib import Path

from source_book import load

DECISIONS = ("早班", "增援")
CUT_FIELD = {"早班": "early_cut", "增援": "intraday_cut"}

DIRECT, DERIVED, DERIVED_EDGE, MISSING = "直接", "推算", "推算(边界)", "缺分位数"
FULL_SUPPORT = ("点误差", "区间覆盖", "超阈判定")

REINFORCE_PROB = 0.5  # 综合超阈概率达到该值即建议加派


# ---------- 信息时点 ----------

def known_seq_ceiling(book):
    """册中出现的最大公开序号，作为“现在”的默认信息时点。"""
    seqs = [r["known_seq"] for r in book["gate"] if r["known_seq"] is not None]
    seqs += [r["known_seq"] for r in book["revisions"] if r["known_seq"] is not None]
    seqs += [r["seq"] for r in book["commitments"] if r["seq"] is not None]
    seqs += [r["seq"] for r in book["predictions"] if r["seq"] is not None]
    return max(seqs) if seqs else 0


def sessions_by_slot(book):
    return {row["slot"]: row for row in book["sessions"]}


def gate_rows(book, slot):
    return [r for r in book["gate"] if r["slot"] == slot]


def gate_at(book, slot, at_seq):
    """at_seq 时点已知的最新进场确认。"""
    known = [r for r in gate_rows(book, slot) if r["known_seq"] <= at_seq]
    return max(known, key=lambda r: r["known_seq"]) if known else None


def threshold_at(book, slot, at_seq):
    """at_seq 时点有效的拥挤线：(值, '原始'|'修订')。后补发布只在公开后生效。"""
    value = sessions_by_slot(book)[slot]["peak_threshold"]
    origin, best = "原始", -1
    for rev in book["revisions"]:
        if rev["slot"] == slot and rev["known_seq"] <= at_seq and rev["known_seq"] > best:
            value, origin, best = rev["peak_threshold"], "修订", rev["known_seq"]
    return value, origin


# ---------- 决策点可用性 ----------

def is_backfilled(pred, gates):
    """结果开始公开（最早一条 gate 的 known_seq）之后才出具的版本视为补写。"""
    first = min((g["known_seq"] for g in gates), default=None)
    return first is not None and pred["seq"] > first


def usable_predictions(book, slot, decision, at_seq):
    """决策点切实可用的版本：{source: row}；被排除的版本连同原因另列。

    规则：出具序号不超过信息时点；不是结束后补写；出具提前量不晚于该场
    对应该决策点的 cut；同一来源取 seq 最大的版本。
    """
    session = sessions_by_slot(book)[slot]
    cut = session[CUT_FIELD[decision]]
    gates = gate_rows(book, slot)
    usable, dropped = {}, []
    for pred in book["predictions"]:
        if pred["slot"] != slot:
            continue
        if pred["seq"] > at_seq:
            dropped.append((pred, "信息时点之后出具"))
            continue
        if is_backfilled(pred, gates):
            dropped.append((pred, "结束后补写，不参加评分"))
            continue
        if pred["ahead_hours"] < cut:
            dropped.append((pred, f"出具距开场{pred['ahead_hours']}h，晚于{decision}决策点{cut}h"))
            continue
        current = usable.get(pred["source"])
        if current is None or pred["seq"] > current["seq"]:
            if current is not None:
                dropped.append((current, "被更新版本取代"))
            usable[pred["source"]] = pred
        else:
            dropped.append((pred, "被更新版本取代"))
    return usable, dropped


def exceedance_probability(pred, threshold):
    """超阈概率：(概率, 方式)。

    出具阈值与当前拥挤线一致且有出具值 → 直接；否则由 p10/p50/p90 分段
    线性推算；超出分位范围给边界值。没有相应阈值就没有直接概率。
    """
    if threshold is None:
        return None, MISSING
    if pred["threshold"] == threshold and pred["probability_above"] is not None:
        return pred["probability_above"], DIRECT
    p10, p50, p90 = pred["p10"], pred["p50"], pred["p90"]
    if p10 is None or p50 is None or p90 is None:
        return None, MISSING
    if threshold <= p10:
        return 0.9, DERIVED_EDGE
    if threshold >= p90:
        return 0.1, DERIVED_EDGE
    if threshold <= p50:
        span = p50 - p10
        cdf = 0.1 + 0.4 * (threshold - p10) / span if span else 0.5
    else:
        span = p90 - p50
        cdf = 0.5 + 0.4 * (threshold - p50) / span if span else 0.5
    return round(1 - cdf, 4), DERIVED


def decision_snapshot(book, slot, decision, at_seq):
    """该场在该决策点切实可用的概率区间与超出拥挤线的风险。"""
    threshold, origin = threshold_at(book, slot, at_seq)
    usable, dropped = usable_predictions(book, slot, decision, at_seq)
    sources = {}
    for source, pred in usable.items():
        prob, how = exceedance_probability(pred, threshold)
        sources[source] = {
            "version": pred["seq"], "ahead_hours": pred["ahead_hours"],
            "p10": pred["p10"], "p50": pred["p50"], "p90": pred["p90"],
            "prob_above": prob, "prob_kind": how,
        }
    return {"slot": slot, "decision": decision, "threshold": threshold,
            "threshold_origin": origin, "sources": sources,
            "dropped": [(p["source"], p["seq"], why) for p, why in dropped]}


# ---------- 实际观察能支持什么 ----------

def actual_support(book, slot, at_seq):
    """该场观察能支持哪些评价。等候区永远单列，不计入入馆。"""
    gate = gate_at(book, slot, at_seq)
    if gate is None:
        return {"status": "未开场", "entered": None, "waiting": None,
                "support": (), "note": "新开或未开场，只能盘点可用预报"}
    closed, entered = gate["closed"], gate["entered"]
    if closed == "取消":
        return {"status": "取消", "entered": None, "waiting": gate["waiting"],
                "support": (), "note": "取消场需求未兑现，不评价准确性"}
    if closed == "迟报" or entered is None:
        return {"status": "迟报", "entered": None, "waiting": gate["waiting"],
                "support": (), "note": "进场数未确认，待补报"}
    if closed == "满员":
        return {"status": "满员", "entered": entered, "waiting": gate["waiting"],
                "support": ("区间覆盖(截断)", "超阈判定(截断)"),
                "note": "满员截断：真实需求不低于进场数，不用确认进场给点误差满分"}
    return {"status": closed, "entered": entered, "waiting": gate["waiting"],
            "support": FULL_SUPPORT, "note": ""}


def censored_coverage(pred, entered):
    """截断场的区间判定：进场数已高出 p90 → 确定漏报；否则需求被截断，不可判定。"""
    if entered > pred["p90"]:
        return "确定漏报(进场已高出区间上限)"
    return "不可判定(需求被截断)"


def censored_above(entered, threshold):
    """截断场的超阈判定：进场已超线 → 确定超阈；否则不可判定。"""
    if entered > threshold:
        return True
    return None


def segment_of(p50, threshold):
    """按预测中位数相对拥挤线划分需求段。"""
    if threshold in (None, 0) or p50 is None:
        return "未知段"
    ratio = p50 / threshold
    if ratio < 0.8:
        return "低需求段"
    if ratio < 1.0:
        return "中需求段"
    return "高需求段"


# ---------- 评分 ----------

def score_source(book, source, decision, at_seq):
    """按统一目标（入馆人数）给来源在决策点的可用版本逐场评分。

    返回 scored（点误差/覆盖/超阈）、censored（满员截断判定）、
    excluded（迟报/取消/未开场及原因）三栏，分开说明。
    """
    sessions = sessions_by_slot(book)
    scored, censored, excluded = {}, {}, {}
    for slot, session in sessions.items():
        usable, _ = usable_predictions(book, slot, decision, at_seq)
        pred = usable.get(source)
        if pred is None:
            continue
        actual = actual_support(book, slot, at_seq)
        threshold, _ = threshold_at(book, slot, at_seq)
        if "点误差" in actual["support"]:
            if pred["p50"] is None:
                excluded[slot] = "预报缺中位数"
                continue
            entered = actual["entered"]
            prob, how = exceedance_probability(pred, threshold)
            scored[slot] = {
                "abs_err": abs(pred["p50"] - entered),
                "covered": pred["p10"] <= entered <= pred["p90"],
                "prob_above": prob, "prob_kind": how,
                "actual_above": entered > threshold,
                "segment": segment_of(pred["p50"], threshold),
                "school_kind": session["school_kind"], "shift": session["shift"],
                "version": pred["seq"], "ahead_hours": pred["ahead_hours"],
                "threshold": threshold, "p50": pred["p50"], "entered": entered,
            }
        elif actual["status"] == "满员":
            censored[slot] = {
                "coverage": censored_coverage(pred, actual["entered"]),
                "actual_above": censored_above(actual["entered"], threshold),
                "note": actual["note"],
            }
        else:
            excluded[slot] = actual["note"]
    return {"source": source, "decision": decision,
            "scored": scored, "censored": censored, "excluded": excluded}


def summarize(scores):
    """聚合点误差、区间覆盖与分需求段失准。"""
    scored = scores["scored"]
    n = len(scored)
    mae = sum(s["abs_err"] for s in scored.values()) / n if n else None
    coverage = sum(1 for s in scored.values() if s["covered"]) / n if n else None
    segments = {}
    for s in scored.values():
        agg = segments.setdefault(s["segment"], {"n": 0, "prob_sum": 0.0, "probs": 0,
                                                 "actual": 0, "interval_miss": 0})
        agg["n"] += 1
        if s["prob_above"] is not None:
            agg["prob_sum"] += s["prob_above"]
            agg["probs"] += 1
        agg["actual"] += 1 if s["actual_above"] else 0
        agg["interval_miss"] += 0 if s["covered"] else 1
    for agg in segments.values():
        agg["mean_prob"] = agg["prob_sum"] / agg["probs"] if agg["probs"] else None
        agg["actual_rate"] = agg["actual"] / agg["n"]
        agg["失准"] = (abs(agg["mean_prob"] - agg["actual_rate"])
                      if agg["mean_prob"] is not None else None)
        del agg["prob_sum"], agg["probs"]
    return {"n": n, "mae": mae, "coverage": coverage, "segments": segments,
            "slots": set(scored),
            "scope": {(s["school_kind"], s["shift"]) for s in scored.values()}}


def compare_sources(book, decision, at_seq):
    """同场比较：只在共同支撑（重叠场次、统一目标、同一决策点提前量）上比误差
    与覆盖；没有共同支撑的来源各自范围分开保留。"""
    sources = sorted({p["source"] for p in book["predictions"]})
    scores = {s: score_source(book, s, decision, at_seq) for s in sources}
    summary = {s: summarize(sc) for s, sc in scores.items()}
    pairs = []
    for i, a in enumerate(sources):
        for b in sources[i + 1:]:
            common = summary[a]["slots"] & summary[b]["slots"]
            if not common:
                pairs.append({"pair": (a, b), "common": set(),
                              "note": "无共同支撑，各自范围分开保留",
                              "only": {a: summary[a]["slots"], b: summary[b]["slots"]}})
                continue
            mae = {s: sum(scores[s]["scored"][slot]["abs_err"] for slot in common) / len(common)
                   for s in (a, b)}
            cov = {s: sum(1 for slot in common if scores[s]["scored"][slot]["covered"]) / len(common)
                   for s in (a, b)}
            better = a if mae[a] < mae[b] else (b if mae[b] < mae[a] else None)
            pairs.append({"pair": (a, b), "common": common, "mae": mae,
                          "coverage": cov, "better": better})
    return {"scores": scores, "summary": summary, "pairs": pairs}


def compare_rounds(scores_a, scores_b, label_a, label_b):
    """同一来源两轮评分对照：交集上的变化才是真变好/变差；场次集不同则
    整体均值不可比（可能换了一批场次）。版本变化的场次单列误差对照。"""
    a, b = set(scores_a["scored"]), set(scores_b["scored"])
    common = a & b
    out = {"labels": (label_a, label_b), "common": common,
           "only_a": a - b, "only_b": b - a, "version_changes": {}}
    for slot in sorted(common):
        va, vb = scores_a["scored"][slot], scores_b["scored"][slot]
        if va["version"] != vb["version"]:
            out["version_changes"][slot] = (va["version"], vb["version"],
                                            va["abs_err"], vb["abs_err"])
    if common:
        mae_a = sum(scores_a["scored"][s]["abs_err"] for s in common) / len(common)
        mae_b = sum(scores_b["scored"][s]["abs_err"] for s in common) / len(common)
        out["mae"] = {label_a: mae_a, label_b: mae_b}
        out["verdict"] = "真变好" if mae_b < mae_a else ("变差" if mae_b > mae_a else "持平")
    if a != b:
        out["note"] = "两轮场次集不同，整体均值不可比（可能换了一批场次）"
    return out


# ---------- 采用权重 ----------

def adoption_weights(book, decision, at_seq):
    """下一轮采用建议：在共同支撑最大的来源组合内按逆误差给权重；
    落单的来源不进统一组合，其可说明范围（场次类型、需求段）单独保留。"""
    compared = compare_sources(book, decision, at_seq)
    scores, summary = compared["scores"], compared["summary"]
    sources = sorted(summary)
    common_of = {frozenset(p["pair"]): p["common"] for p in compared["pairs"]}

    def common_all(group):
        inter = None
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                common = common_of.get(frozenset((a, b)), set())
                inter = common if inter is None else inter & common
        return inter if inter is not None else summary[group[0]]["slots"]

    # 以两两共同支撑为边，从最大的边出发贪心扩张成组合
    cliques = []
    seen = set()
    edges = sorted((p for p in compared["pairs"] if p["common"]),
                   key=lambda p: (-len(p["common"]), p["pair"]))
    for pair in edges:
        a, b = pair["pair"]
        if frozenset((a, b)) in seen:
            continue
        members, common = [a, b], set(pair["common"])
        for other in sources:
            if other in members:
                continue
            inter = common_all(members + [other])
            if inter:
                members.append(other)
                common = inter
        cliques.append((members, common))
        seen.update(frozenset((x, y)) for i, x in enumerate(members) for y in members[i + 1:])
    cliques.sort(key=lambda mc: -len(mc[1]))

    weights = {}
    for members, common in cliques:
        free = [m for m in members if m not in weights]
        if len(free) < 2:
            continue
        raw = {}
        for s in free:
            mae = sum(scores[s]["scored"][slot]["abs_err"] for slot in common) / len(common)
            raw[s] = 1 / (1 + mae)
        total = sum(raw.values())
        for s in free:
            weights[s] = {"weight": raw[s] / total, "common": common,
                          "mae_on_common": 1 / raw[s] - 1, "note": ""}
    has_common = {s for p in compared["pairs"] if p["common"] for s in p["pair"]}
    for s in sources:
        if s in weights:
            pass
        elif s in has_common:
            weights[s] = {"weight": None, "note": "共同支撑被更大组合吸收，暂不单独给权重"}
        else:
            weights[s] = {"weight": None, "note": "无共同支撑，不进统一组合"}
    for s in sources:
        weights[s].update({"scope": summary[s]["scope"],
                           "segments": set(summary[s]["segments"]),
                           "slots": summary[s]["slots"], "mae": summary[s]["mae"]})
        weights[s].setdefault("common", set())
        weights[s].setdefault("mae_on_common", None)
    return weights


# ---------- 班表 ----------

def combine_snapshot(snapshot, session, weights, scope=None, override=False):
    """按权重合成一个场次的区间与超阈概率。

    权重落空的来源（该决策点不可用）记注记；给了 scope 且未 override 时，
    来源在该类场次（学龄/班次）无点误差核对支撑的，权重不采用；在该需求段
    无支撑的，保留权重但提示需人工确认。
    返回 (combined or None, used {source: weight}, notes [...])。
    """
    usable = snapshot["sources"]
    notes, used = [], {}
    for source, w in (weights or {}).items():
        if not w:
            continue
        if source not in usable:
            notes.append(f"{source} 在此决策点不可用，权重落空")
            continue
        if scope and not override and source in scope:
            info = scope[source]
            kind = (session["school_kind"], session["shift"])
            if info.get("scope") is not None and kind not in info["scope"]:
                notes.append(f"{source} 在（{kind[0]}，{kind[1]}）无点误差核对支撑，权重不采用")
                continue
            seg = segment_of(usable[source]["p50"], snapshot["threshold"])
            if info.get("segments") and seg not in info["segments"]:
                notes.append(f"{source} 在{seg}无核对支撑，权重保留但需人工确认")
        used[source] = w
    if not used:
        return None, {}, notes
    total = sum(used.values())
    combined = {"p10": 0.0, "p50": 0.0, "p90": 0.0, "prob_above": 0.0, "prob_n": 0.0}
    for source, w in used.items():
        info = usable[source]
        for key in ("p10", "p50", "p90"):
            combined[key] += w * (info[key] or 0)
        if info["prob_above"] is not None:
            combined["prob_above"] += w * info["prob_above"]
            combined["prob_n"] += w
    for key in ("p10", "p50", "p90"):
        combined[key] = round(combined[key] / total, 2)
    combined["prob_above"] = (round(combined["prob_above"] / combined["prob_n"], 4)
                              if combined["prob_n"] else None)
    combined.pop("prob_n")
    return combined, used, notes


def roster_plan(book, decision, at_seq, weights=None, scope=None, override=False):
    """对 at_seq 时仍未知结果的场给出班组建议。

    约束：同一 (date, shift) 组内同一 pool 只能分给一个场次；每个 pool 至少
    保留 min_keep；已通知（commitments）的人力锁定在原场次，不可释放为增援；
    余量按 可用 - 已通知 - 最低保留 计算并明示。
    """
    sessions = sessions_by_slot(book)
    upcoming = [s for s in sessions if gate_at(book, s, at_seq) is None]
    groups = defaultdict(list)
    for slot in upcoming:
        sess = sessions[slot]
        groups[(sess["date"], sess["shift"])].append(slot)

    plans = {}
    for (date, shift), slots in sorted(groups.items()):
        pools = {p["pool"]: dict(p) for p in book["crews"] if p["shift"] == shift}
        state = {p: {"available": pools[p]["available"], "min_keep": pools[p]["min_keep"],
                     "type": pools[p]["type"], "used_by": None, "committed": 0, "assigned": 0}
                 for p in pools}
        warnings, assignments, unmet = [], [], []

        committed_of = defaultdict(int)
        for c in book["commitments"]:
            if c["seq"] > at_seq or c["slot"] not in slots or c["pool"] not in pools:
                continue
            committed_of[c["slot"]] += c["amount"]
            st = state[c["pool"]]
            st["committed"] += c["amount"]
            if st["used_by"] not in (None, c["slot"]):
                warnings.append(f"{c['pool']} 被通知给多个场次（{st['used_by']}、{c['slot']}），与独占约束冲突")
            st["used_by"] = c["slot"]

        need_of = {}
        for slot in slots:
            session = sessions[slot]
            snap = decision_snapshot(book, slot, decision, at_seq)
            combined, used, notes = combine_snapshot(snapshot=snap, session=session,
                                                     weights=weights, scope=scope,
                                                     override=override)
            warnings.extend(f"{slot}: {n}" for n in notes)
            need, why = 1, []
            if combined is None:
                warnings.append(f"{slot}: 无可用合成预报，按基准 1 单位，需人工确认")
                why.append("无可用合成预报")
            else:
                prob, p50 = combined["prob_above"], combined["p50"]
                if prob is not None and prob >= REINFORCE_PROB:
                    need += 1
                    why.append(f"综合超阈 {prob:.2f} ≥ {REINFORCE_PROB}，加派 1 单位")
                elif p50 is not None and p50 >= snap["threshold"]:
                    need += 1
                    why.append(f"综合中位数 {p50} 达到拥挤线 {snap['threshold']}，加派 1 单位")
                else:
                    why.append(f"综合超阈 {prob if prob is not None else '—'}、中位数 {p50} 低于线 {snap['threshold']}")
            need_of[slot] = {"need": need, "met_by_commit": min(committed_of[slot], need),
                             "over_committed": committed_of[slot] > need,
                             "combined": combined, "used": used, "why": why,
                             "threshold": snap["threshold"]}

        order = sorted(slots, key=lambda s: (
            -(need_of[s]["need"] - need_of[s]["met_by_commit"]),
            -((need_of[s]["combined"] or {}).get("prob_above") or -1),
            -((need_of[s]["combined"] or {}).get("p50") or -1), s))
        for slot in order:
            remaining = need_of[slot]["need"] - need_of[slot]["met_by_commit"]
            prefer_child = sessions[slot]["school_kind"] == "低龄"
            while remaining > 0:
                candidates = [p for p, st in state.items()
                              if st["used_by"] in (None, slot)
                              and st["available"] - st["committed"] - st["assigned"] - st["min_keep"] > 0]
                if not candidates:
                    break

                def rank(p, _prefer_child=prefer_child):
                    st = state[p]
                    match = 0 if (_prefer_child and st["type"] == "儿童讲解") or \
                                 (not _prefer_child and st["type"] != "儿童讲解") else 1
                    free = st["available"] - st["committed"] - st["assigned"] - st["min_keep"]
                    return (match, -free)

                pool = min(candidates, key=rank)
                state[pool]["assigned"] += 1
                state[pool]["used_by"] = slot
                assignments.append({"slot": slot, "pool": pool, "type": state[pool]["type"]})
                remaining -= 1
            if remaining > 0:
                unmet.append({"slot": slot, "short": remaining})

        margins = []
        for p, st in sorted(state.items()):
            free = st["available"] - st["committed"] - st["assigned"] - st["min_keep"]
            note = ""
            if st["committed"] > st["available"] - st["min_keep"]:
                note = "最低保留已被已通知占用"
                warnings.append(f"{p}: 已通知 {st['committed']} 使最低保留 {st['min_keep']} 被占用")
            margins.append({"pool": p, "type": st["type"], "available": st["available"],
                            "committed": st["committed"], "min_keep": st["min_keep"],
                            "real_free": max(0, free), "used_by": st["used_by"], "note": note})
        plans[(date, shift)] = {"slots": slots, "needs": need_of, "assignments": assignments,
                                "unmet": unmet, "margins": margins, "warnings": warnings}
    return plans


# ---------- 值班员报告 ----------

def _pct(x):
    return "—" if x is None else f"{x:.2f}"


def _num(x):
    return "—" if x is None else f"{x:.1f}"


def _slots(slots):
    return "、".join(sorted(slots)) if slots else "无"


def render_report(book, at_seq):
    """值班员查询：当时能用什么、为什么推荐这份班表、核对结果怎样影响下一轮。"""
    sessions = sessions_by_slot(book)
    lines = [f"信息时点 seq={at_seq}（只纳入此序号前出具的预报、修订、承诺与进场确认）", ""]

    # 一、未开场次的决策点可用性
    lines.append("一、未开场次：各决策点当时能用什么")
    upcoming = [s for s in sessions if gate_at(book, s, at_seq) is None]
    if not upcoming:
        lines.append("  （无未开场次）")
    for slot in sorted(upcoming):
        sess = sessions[slot]
        threshold, origin = threshold_at(book, slot, at_seq)
        base = sess["peak_threshold"]
        rev = f"（{origin}；原始 {base}）" if origin == "修订" else f"（{origin}）"
        lines.append(f"场次 {slot}（{sess['date']} {sess['shift']} {sess['school_kind']}，"
                     f"座位 {sess['seats']}）拥挤线 {threshold}{rev}")
        for decision in DECISIONS:
            snap = decision_snapshot(book, slot, decision, at_seq)
            cut = sess[CUT_FIELD[decision]]
            lines.append(f"  {decision}决策点（提前 {cut}h）：")
            if not snap["sources"]:
                lines.append("    无可用预报")
            for source, info in sorted(snap["sources"].items()):
                lines.append(f"    {source} v{info['version']}（提前{info['ahead_hours']}h）"
                             f" 区间[{info['p10']},{info['p50']},{info['p90']}]"
                             f" 超阈 {_pct(info['prob_above'])}（{info['prob_kind']}）")
            for source, seq, why in snap["dropped"]:
                lines.append(f"    × {source} v{seq}：{why}")
        notified = [c for c in book["commitments"] if c["slot"] == slot and c["seq"] <= at_seq]
        if notified:
            desc = "、".join(f"{c['pool']}×{c['amount']}" for c in notified)
            by = notified[0]["started_by"]
            lines.append(f"  已通知（不可释放为增援）：{desc}（{by}）")
    lines.append("")

    # 二、同场评分
    lines.append("二、同场评分（目标=入馆人数；等候区单列不计入；按决策点提前量）")
    compared = {d: compare_sources(book, d, at_seq) for d in DECISIONS}
    for decision in DECISIONS:
        lines.append(f"  [{decision}决策点]")
        cmp = compared[decision]
        for source in sorted(cmp["scores"]):
            sc, sm = cmp["scores"][source], cmp["summary"][source]
            if not sc["scored"] and not sc["censored"] and not sc["excluded"]:
                continue
            lines.append(f"  {source}：可评 {sm['n']} 场，MAE {_num(sm['mae'])}，"
                         f"区间覆盖 {_pct(sm['coverage'])}")
            for slot, c in sorted(sc["censored"].items()):
                above = c["actual_above"]
                verdict = "确定超阈" if above else ("不可判定" if above is None else "未超阈")
                lines.append(f"    截断 {slot}：{c['coverage']}；超阈判定 {verdict}（不给点误差满分）")
            if sc["excluded"]:
                reasons = "；".join(f"{s} {r}" for s, r in sorted(sc["excluded"].items()))
                lines.append(f"    不参评：{reasons}")
            for seg, agg in sorted(sm["segments"].items()):
                lines.append(f"    {seg}：n={agg['n']} 平均超阈概率 {_pct(agg['mean_prob'])}"
                             f" 实际超阈率 {_pct(agg['actual_rate'])} 失准 {_pct(agg['失准'])}")
        for pair in cmp["pairs"]:
            a, b = pair["pair"]
            if not pair["common"]:
                lines.append(f"  {a} × {b}：{pair['note']}"
                             f"（{a}：{_slots(pair['only'][a])}；{b}：{_slots(pair['only'][b])}）")
            else:
                better = pair["better"] or "持平"
                lines.append(f"  {a} × {b}：共同支撑 {_slots(pair['common'])}，"
                             f"MAE {_num(pair['mae'][a])} vs {_num(pair['mae'][b])}，"
                             f"覆盖 {_pct(pair['coverage'][a])} vs {_pct(pair['coverage'][b])} → {better}")
        unusable = []
        for slot in sorted(sessions):
            if slot in upcoming:
                continue
            _, dropped = usable_predictions(book, slot, decision, at_seq)
            for pred, why in dropped:
                if "被更新版本取代" in why or "信息时点之后" in why:
                    continue
                unusable.append(f"{slot} {pred['source']} v{pred['seq']}：{why}")
        if unusable:
            lines.append("  该决策点不可用版本（不参评）：" + "；".join(unusable))
    lines.append("")

    # 三、版本对照：真变好还是换了一批场次
    lines.append("三、版本对照（同一来源：早班可用版 vs 增援可用版）")
    for source in sorted(compared["早班"]["scores"]):
        rounds = compare_rounds(compared["早班"]["scores"][source],
                                compared["增援"]["scores"][source], "早班", "增援")
        if not rounds["common"] and not rounds["only_a"] and not rounds["only_b"]:
            continue
        parts = []
        if rounds["common"]:
            parts.append(f"共同场 {_slots(rounds['common'])} 上 MAE "
                         f"{_num(rounds['mae']['早班'])} → {_num(rounds['mae']['增援'])}"
                         f"（{rounds['verdict']}）")
        for slot, (va, vb, ea, eb) in rounds["version_changes"].items():
            parts.append(f"{slot} 版本 v{va}→v{vb} 误差 {_num(ea)}→{_num(eb)}")
        if rounds.get("note"):
            parts.append(f"{rounds['note']}（早班独有 {_slots(rounds['only_a'])}；"
                         f"增援独有 {_slots(rounds['only_b'])}）")
        lines.append(f"  {source}：" + "；".join(parts))
    lines.append("")

    # 四、班表建议
    adoption = {d: adoption_weights(book, d, at_seq) for d in DECISIONS}
    lines.append("四、班表建议（已通知不可释放；余量=可用-已通知-最低保留）")
    for decision in DECISIONS:
        weights = {s: w["weight"] for s, w in adoption[decision].items() if w["weight"]}
        plans = roster_plan(book, decision, at_seq, weights=weights, scope=adoption[decision])
        lines.append(f"  [{decision}]")
        if not plans:
            lines.append("    （无待排场次）")
        for (date, shift), plan in plans.items():
            lines.append(f"    {date} {shift}：")
            for slot in plan["slots"]:
                need = plan["needs"][slot]
                used = "、".join(f"{s}×{w:.2f}" for s, w in sorted(need["used"].items())) or "无"
                lines.append(f"    {slot} 需要 {need['need']} 单位（{'；'.join(need['why'])}；采用 {used}）"
                             f"，已通知抵充 {need['met_by_commit']}"
                             + ("，已通知超出当前需要但不可释放" if need["over_committed"] else ""))
            for a in plan["assignments"]:
                lines.append(f"    → 派 {a['pool']}（{a['type']}）→ {a['slot']}")
            for u in plan["unmet"]:
                lines.append(f"    → {u['slot']} 尚缺 {u['short']} 单位，未安排（同组 pool 已分完或受保留线限制）")
            for m in plan["margins"]:
                note = f"，{m['note']}" if m["note"] else ""
                user = f"，分给 {m['used_by']}" if m["used_by"] else ""
                lines.append(f"    余量 {m['pool']}（{m['type']}）：可用 {m['available']}"
                             f" 已通知 {m['committed']}（不可释放）保留 {m['min_keep']}"
                             f" → 真实可派 {m['real_free']}{user}{note}")
            for w in plan["warnings"]:
                lines.append(f"    ！{w}")
    lines.append("")

    # 五、核对结果怎样影响下一轮采用
    lines.append("五、下一轮采用（由第二节核对结果推出）")
    for decision in DECISIONS:
        lines.append(f"  [{decision}决策点]")
        for source, w in sorted(adoption[decision].items()):
            scope = "、".join(f"({k}，{s})" for k, s in sorted(w["scope"])) or "无"
            segs = "、".join(sorted(w["segments"])) or "无"
            if w["weight"] is not None:
                lines.append(f"  {source}：建议权重 {w['weight']:.2f}"
                             f"（共同支撑 {_slots(w['common'])}，MAE {_num(w['mae_on_common'])}）；"
                             f"核对范围 {scope} / {segs}")
            else:
                lines.append(f"  {source}：{w['note']}；可说明范围 {scope} / {segs}"
                             f"（场次 {_slots(w['slots'])}，MAE {_num(w['mae'])}）")
        revised = [s for s in sessions if threshold_at(book, s, at_seq)[1] == "修订"]
        if revised:
            lines.append(f"  阈值已修订：{_slots(revised)} —— 旧的直接超阈概率失效，"
                         "当前为分位数推算，这些场该来源能否采用需重新判断")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="按决策点还原可用预报、同场评分与值班采用")
    parser.add_argument("path", type=Path)
    parser.add_argument("--at-seq", type=int, default=None,
                        help="信息时点序号；默认取册中最大公开序号")
    args = parser.parse_args()
    book = load(args.path)
    at_seq = args.at_seq if args.at_seq is not None else known_seq_ceiling(book)
    print(render_report(book, at_seq))


if __name__ == "__main__":
    main()
