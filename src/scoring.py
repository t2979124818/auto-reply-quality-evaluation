"""
聚合层：单条得分 → 总分 → 整体统计 → 与人工标注的一致性回测。

三个刻意的设计：
1. 归一化后再加权，避免 1-5 分制和 0-1 分制混算。
2. 事实类指标走**闸门降级**而不是纯加权：加权求和会让"语气很好"掩盖"编造政策"。
3. 总分 ≠ 结论。报告必须同时给各指标分布，因为业务方要的是"该改哪里"，
   而单个总分只能回答"整体行不行"。
"""

from . import kb as KB
from . import taxonomy as T

METRIC_KEYS = [m["key"] for m in T.METRICS]
METRIC_BY_KEY = {m["key"]: m for m in T.METRICS}
RUBRIC_METRICS = {"resolution", "completeness", "tone"}

# 等级阈值：按本数据集校准（见 README「阈值怎么定的」）
GRADE_BANDS = [(85, 100, "良好"), (70, 85, "需改进"), (0, 70, "不合格")]
GRADE_LABEL = " / ".join(
    f"{'≥'+str(lo) if hi>=100 else str(lo)+'~'+str(hi-0.1)} {nm}" for lo, hi, nm in GRADE_BANDS)


def normalize(judgement):
    """把各指标统一到 0-1。"""
    facts = judgement["facts"]
    out = {
        "accuracy": facts["accuracy"],
        "groundedness": facts["groundedness"],
    }
    for k in RUBRIC_METRICS:
        out[k] = judgement[k] / 5.0
    return out


def _kind_counts(claims):
    out = {}
    for c in claims:
        out[c["kind"]] = out.get(c["kind"], 0) + 1
    return out


def score_one(judgement):
    norm = normalize(judgement)
    raw = sum(METRIC_BY_KEY[k]["weight"] * norm[k] for k in METRIC_KEYS)

    gates = []
    cap = 100.0
    for k in ("groundedness", "accuracy"):
        g = METRIC_BY_KEY[k].get("gate")
        if g and norm[k] < g["threshold"] and g["cap"] < cap:
            cap = g["cap"]
            gates.append({
                "metric": k,
                "value": round(norm[k], 3),
                "threshold": g["threshold"],
                "cap": g["cap"],
                "reason": g["reason"],
            })

    # 合规红线：命中即强制判为不合格区间
    if judgement["facts"].get("red_lines"):
        cap = min(cap, 40.0)
        gates.append({"metric": "red_line", "value": 0, "threshold": 1, "cap": 40.0,
                      "reason": "命中合规红线：" + "；".join(judgement["facts"]["red_lines"])})

    score = round(min(raw * 100.0, cap), 1)
    grade = next(name for lo, _hi, name in GRADE_BANDS if score >= lo)
    return {
        "case_id": judgement["case_id"],
        "intent": judgement["intent"],
        "score": score,
        "grade": grade,
        "gates_applied": gates,
        "capped": score < round(raw * 100.0, 1) - 1e-9,
        "raw_score_before_gate": round(raw * 100.0, 1),
        "normalized": {k: round(v, 4) for k, v in norm.items()},
        "raw_metrics": {
            "accuracy": round(judgement["facts"]["accuracy"], 4),
            "groundedness": round(judgement["facts"]["groundedness"], 4),
            "resolution": judgement["resolution"],
            "completeness": judgement["completeness"],
            "tone": judgement["tone"],
        },
        "claim_stats": {k: judgement["facts"][k] for k in
                        ("n_supported", "n_contradicted", "n_unverifiable")},
        "accuracy_denom": judgement["facts"].get("accuracy_denom", 0),
        "claim_kind_counts": _kind_counts(judgement["facts"]["claims"]),
        "evidence": judgement["evidence"],
        "judge": judgement["judge"],
    }


def _dist(values):
    if not values:
        return {}
    vs = sorted(values)

    def q(p):
        idx = min(len(vs) - 1, max(0, int(round(p * (len(vs) - 1)))))
        return vs[idx]

    return {
        "n": len(vs),
        "mean": round(sum(vs) / len(vs), 3),
        "min": vs[0],
        "p25": q(0.25),
        "median": q(0.5),
        "p75": q(0.75),
        "max": vs[-1],
    }


def _histogram(values, bins):
    """bins 是 len(counts)+1 个边界；最后一个桶右闭，用于收纳最大值。"""
    counts = [0] * (len(bins) - 1)
    for v in values:
        placed = False
        for i in range(len(bins) - 1):
            if bins[i] <= v < bins[i + 1]:
                counts[i] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    return counts


def summarize(results):
    """整体统计：总分分布 + 各指标分布 + 闸门命中情况。"""
    scores = [r["score"] for r in results]
    summary = {
        "n_cases": len(results),
        "overall": _dist(scores),
        "overall_hist": {
            "bins": [0, 50, 60, 70, 80, 90, 101],
            "counts": _histogram(scores, [0, 50, 60, 70, 80, 90, 101]),
        },
        "grades": {name: sum(1 for r in results if r["grade"] == name)
                   for _lo, _hi, name in GRADE_BANDS},
        "metrics": {},
        "gate_hits": sum(1 for r in results if r["gates_applied"]),
        "claim_totals": {
            k: sum(r["claim_stats"][k] for r in results)
            for k in ("n_supported", "n_contradicted", "n_unverifiable")
        },
    }
    # 断言抽取的覆盖情况：用于暴露 M1/M2 的分辨率天花板（判了多少条 / 抽了多少条）
    kinds = {}
    for r in results:
        for k, v in r.get("claim_kind_counts", {}).items():
            kinds[k] = kinds.get(k, 0) + v
    judged = sum(summary["claim_totals"].values())
    summary["claim_kind_counts"] = kinds
    summary["claims_per_case"] = round(judged / len(results), 2) if results else 0
    summary["extraction_coverage"] = round(
        judged / sum(kinds.values()), 3) if sum(kinds.values()) else 0
    # accuracy 的分母是「可判定真伪的断言数」。分母为 0 说明这条回复根本没有可核实的断言，
    # 此时 accuracy=1.0 是**空真**（vacuous truth），必须在报告里标明，
    # 否则会把「什么都没说」误读成「说得都对」。
    summary["accuracy_vacuous_cases"] = sum(1 for r in results if r.get("accuracy_denom", 0) == 0)
    for m in T.METRICS:
        k = m["key"]
        if k in ("accuracy", "groundedness"):
            vals = [r["raw_metrics"][k] for r in results]
            summary["metrics"][k] = {
                "name": m["name"], "weight": m["weight"], "scale": "0-1",
                "dist": _dist(vals),
                "hist": {"bins": [0, .6, .7, .8, .9, .95, 1.01],
                         "counts": _histogram(vals, [0, .6, .7, .8, .9, .95, 1.01])},
            }
        else:
            vals = [r["raw_metrics"][k] for r in results]
            summary["metrics"][k] = {
                "name": m["name"], "weight": m["weight"], "scale": "1-5",
                "dist": _dist(vals),
                "hist": {"bins": [1, 2, 3, 4, 4.5, 5.01],
                         "counts": _histogram(vals, [1, 2, 3, 4, 4.5, 5.01])},
            }
    # 各指标对总分方差的"解释力"（近似：权重 × 该指标相对其均值的离散度）
    means = {k: summary["metrics"][k]["dist"]["mean"] for k in METRIC_KEYS}
    spread = {}
    for k in METRIC_KEYS:
        vals = [r["normalized"][k] for r in results]
        m = sum(vals) / len(vals)
        var = sum((v - m) ** 2 for v in vals) / len(vals)
        spread[k] = round(METRIC_BY_KEY[k]["weight"] * (var ** 0.5), 4)
    summary["metric_means"] = means
    summary["discriminating_power"] = spread
    return summary


# ---------------------------------------------------------------------------
# 与人工标注的一致性回测
# ---------------------------------------------------------------------------
# 注意：HUMAN_VERDICT 是从 annotator_notes 里**归纳**出来的三档标签，
# 不是独立的第三方标注。所以这个回测只能说明"我的指标和人工笔记是否同向"，
# 不能证明"我的指标是对的"。README 的局限一节会强调这一点。
def compare_with_human(results):
    order = KB.VERDICT_ORDER
    pairs_total = pairs_agree = 0
    per_case = []
    for r in results:
        verdict, note = KB.HUMAN_VERDICT.get(r["case_id"], ("unknown", ""))
        r["human_verdict"] = verdict
        r["human_note"] = note
        pred = r["grade"]
        if verdict == "unknown":
            continue
        # 等级 → 三档：良好=good，需改进=acceptable，不合格=weak
        pred3 = {"良好": "good", "需改进": "acceptable", "不合格": "weak"}[pred]
        r["pred_verdict"] = pred3
        r["verdict_match"] = (order[pred3] == order[verdict])
        r["verdict_gap"] = abs(order[pred3] - order[verdict])
        per_case.append(r)

    # 成对排序一致率（Pairwise Accuracy）：比较任意两条 case 的得分高低是否与人工判断一致
    concordant = discordant = 0
    for i in range(len(per_case)):
        for j in range(i + 1, len(per_case)):
            a, b = per_case[i], per_case[j]
            ho = order[a["human_verdict"]] - order[b["human_verdict"]]
            so = a["score"] - b["score"]
            if ho == 0 or so == 0:
                continue
            pairs_total += 1
            if (ho > 0) == (so > 0):
                concordant += 1
                pairs_agree += 1
            else:
                discordant += 1

    exact = sum(1 for r in per_case if r["verdict_gap"] == 0)
    within1 = sum(1 for r in per_case if r["verdict_gap"] <= 1)
    return {
        "n": len(per_case),
        "exact_agreement": round(exact / len(per_case), 3) if per_case else 0,
        "within_1_level": round(within1 / len(per_case), 3) if per_case else 0,
        "pairwise_concordance": round(concordant / pairs_total, 3) if pairs_total else 0,
        "n_pairs": pairs_total,
        "confusion": {
            h: {p: sum(1 for r in per_case if r["human_verdict"] == h and r["pred_verdict"] == p)
                for p in ("weak", "acceptable", "good")}
            for h in ("weak", "acceptable", "good")
        },
        "per_case": per_case,
    }
