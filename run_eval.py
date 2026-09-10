#!/usr/bin/env python3
"""
0109 · 自动回复质量评估流水线 —— 入口脚本

用法
----
    python run_eval.py                     # 默认 mock 判官，读当前目录数据
    python run_eval.py --mode llm          # 用真实 LLM 打分主观指标（需 OPENAI_API_KEY）
    python run_eval.py --data-dir /path    # 指定数据目录
    python run_eval.py --open               # 跑完自动打开 HTML 看板

产出（--out-dir，默认 outputs/）
    report.md       评估报告（结论 / 指标口径 / 分布 / 最差 case / 一致性回测 / 局限）
    dashboard.html  单文件可视化看板（内联 SVG，无依赖）
    scores.json     全量结构化结果（含逐条证据）
    scores.csv      逐条得分（便于 Excel 复核）
    claims.csv      逐条断言校验结果（准确性与无编造性的取数依据）
"""

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import kb as KB                     # noqa: E402
from src import report as R                  # noqa: E402
from src import scoring as S                 # noqa: E402
from src import taxonomy as T                # noqa: E402
from src.judge import build_judge            # noqa: E402

AUTO_FILE = "task3_auto_replies.json"
HUMAN_FILE = "task3_human_ref.json"


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def run(args):
    auto_path = os.path.join(args.data_dir, AUTO_FILE)
    human_path = os.path.join(args.data_dir, HUMAN_FILE)
    auto_replies = load_json(auto_path)
    human_list = load_json(human_path)
    human_ref = {h["id"]: h for h in human_list}

    try:
        judge = build_judge(args.mode)
    except RuntimeError as e:
        print(f"[pipeline] 无法启动 LLM 判官：{e}\n"
              f"           如需真实 LLM 打分，请先设置环境变量：\n"
              f"             export OPENAI_API_KEY=sk-xxx\n"
              f"             export OPENAI_BASE_URL=https://api.openai.com/v1   # 可选，兼容其它网关\n"
              f"             export OPENAI_MODEL=gpt-4o-mini                    # 可选",
              file=sys.stderr)
        return None
    print(f"[pipeline] judge={judge.name}  cases={len(auto_replies)}  "
          f"metrics={len(S.METRIC_KEYS)}")

    results, claims_map = [], {}
    for case in auto_replies:
        intent = KB.match_intent(case["user_question"])
        if intent is None:
            print(f"  ! {case['id']} 未匹配到意图，按通用意图处理")
        j = judge.judge(case, intent, human_ref.get(case["id"])) if args.mode == "llm" \
            else judge.judge(case, intent)
        scored = S.score_one(j)
        results.append(scored)
        claims_map[case["id"]] = j["facts"]["claims"]

    summary = S.summarize(results)
    comparison = S.compare_with_human(results)
    payload = {
        "summary": summary,
        "comparison": comparison,
        "cases": results,
        "claims": claims_map,
        "raw": {c["id"]: c for c in auto_replies},
        "human_ref": human_ref,
        "config": {
            "judge": judge.name,
            "data_dir": os.path.abspath(args.data_dir),
            "grade_bands": S.GRADE_BANDS,
            "metrics": [{"key": m["key"], "name": m["name"], "weight": m["weight"],
                         "gate": m["gate"], "business_word": m["business_word"]}
                        for m in T.METRICS],
        },
    }

    os.makedirs(args.out_dir, exist_ok=True)
    _write_outputs(args, payload)
    _print_console(payload, args.out_dir)
    return payload


def _write_outputs(args, payload):
    out = args.out_dir
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as f:
        f.write(R.build_report(payload, worst_n=args.worst))
    with open(os.path.join(out, "dashboard.html"), "w", encoding="utf-8") as f:
        f.write(R.build_html(payload))
    with open(os.path.join(out, "scores.json"), "w", encoding="utf-8") as f:
        json.dump({k: payload[k] for k in ("summary", "comparison", "cases", "config")},
                  f, ensure_ascii=False, indent=2)

    with open(os.path.join(out, "scores.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "intent", "score", "grade", "raw_score_before_gate",
                    "accuracy", "groundedness", "resolution", "completeness", "tone",
                    "n_supported", "n_contradicted", "n_unverifiable",
                    "human_verdict", "auto_verdict", "verdict_match", "human_note"])
        for c in sorted(payload["cases"], key=lambda x: x["score"]):
            w.writerow([c["case_id"], c["intent"], c["score"], c["grade"],
                        c["raw_score_before_gate"],
                        c["raw_metrics"]["accuracy"], c["raw_metrics"]["groundedness"],
                        c["raw_metrics"]["resolution"], c["raw_metrics"]["completeness"],
                        c["raw_metrics"]["tone"],
                        c["claim_stats"]["n_supported"], c["claim_stats"]["n_contradicted"],
                        c["claim_stats"]["n_unverifiable"],
                        c.get("human_verdict", ""), c.get("pred_verdict", ""),
                        c.get("verdict_match", ""), c.get("human_note", "")])

    with open(os.path.join(out, "claims.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "claim_kind", "claim_text", "verdict", "reason"])
        for cid, claims in payload["claims"].items():
            for cl in claims:
                w.writerow([cid, cl["kind"], cl["text"], cl["verdict"], cl["reason"]])


def _print_console(payload, out_dir="outputs"):
    s = payload["summary"]
    print("\n" + "=" * 78)
    print(f"{'case':<9}{'意图':<22}{'总分':>7}{'等级':>8}"
          f"{'准确':>7}{'无编造':>8}{'解决力':>8}{'完备':>6}{'语气':>6}  人工")
    print("-" * 78)
    for c in sorted(payload["cases"], key=lambda x: x["score"]):
        m = c["raw_metrics"]
        mark = " " if c.get("verdict_match") else "*"
        print(f"{c['case_id']:<9}{c['intent'][:10]:<22}{c['score']:>7.1f}{c['grade']:>8}"
              f"{m['accuracy']:>7.2f}{m['groundedness']:>8.2f}{m['resolution']:>8.1f}"
              f"{m['completeness']:>6.1f}{m['tone']:>6.1f}  {c.get('human_verdict',''):<10}{mark}")
    print("-" * 78)
    ov, cmp_ = s["overall"], payload["comparison"]
    print(f"整体总分 均值 {ov['mean']:.1f} | 中位 {ov['median']:.1f} | "
          f"{ov['min']:.1f}~{ov['max']:.1f}")
    print("等级分布 " + " / ".join(f"{k} {v}" for k, v in s["grades"].items()))
    print("指标均值 " + " | ".join(
        f"{s['metrics'][k]['name']} {s['metrics'][k]['dist']['mean']:.2f}" for k in S.METRIC_KEYS))
    print(f"断言统计 支持 {s['claim_totals']['n_supported']} / 冲突 "
          f"{s['claim_totals']['n_contradicted']} / 无法核实 "
          f"{s['claim_totals']['n_unverifiable']}；闸门命中 {s['gate_hits']} 条")
    print(f"人工一致性 三档命中 {cmp_['exact_agreement']:.0%} | ≤1档 "
          f"{cmp_['within_1_level']:.0%} | 成对排序一致率 {cmp_['pairwise_concordance']:.0%}"
          f"  （* 表示与人工档位不同向）")
    print("=" * 78)
    print(f"报告：{os.path.join(os.path.abspath(out_dir), 'report.md')}")
    print(f"看板：{os.path.join(os.path.abspath(out_dir), 'dashboard.html')}")


def main():
    p = argparse.ArgumentParser(description="自动回复质量评估流水线")
    p.add_argument("--data-dir", default=os.path.dirname(os.path.abspath(__file__)),
                   help="存放 task3_auto_replies.json / task3_human_ref.json 的目录")
    p.add_argument("--out-dir", default="outputs", help="输出目录")
    p.add_argument("--mode", choices=["mock", "llm"], default="mock",
                   help="mock=确定性规则判官；llm=调用 OpenAI 兼容接口")
    p.add_argument("--worst", type=int, default=3, help="报告中分析的 case 数量")
    args = p.parse_args()
    run(args)


if __name__ == "__main__":
    main()
