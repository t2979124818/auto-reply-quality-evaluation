#!/usr/bin/env python3
"""
评估方法自检（validation）。

包含两部分，都是为了让「评估本身」可被质疑和验证：

A. 对抗集压测：人工构造 7 条"应该被拦下 / 应该拿高分"的回复，验证闸门是否按预期工作。
   没有这一步，"闸门命中 0 条"既可能是数据干净，也可能是闸门根本坏了 —— 两者无法区分。
B. 信号单元校验：对特征层的关键正则做断言，防止改动时静默破坏召回。

用法：python validate.py [--data-dir .] [--out-dir outputs]
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import features as F            # noqa: E402
from src import kb as KB                 # noqa: E402
from src import scoring as S             # noqa: E402
from src.judge import MockJudge          # noqa: E402


def run_adversarial(judge, path):
    with open(path, "r", encoding="utf-8") as f:
        cases = json.load(f)
    rows = []
    for c in cases:
        intent = KB.match_intent(c["user_question"])
        j = judge.judge(c, intent)
        r = S.score_one(j)
        r["expect"] = c["expect"]
        r["why"] = c["why"]
        r["red_lines"] = j["facts"]["red_lines"]
        rows.append(r)
    return rows


def check_expectations(rows):
    """把 expect 的自然语言描述翻译成可机检的断言。"""
    by_id = {r["case_id"]: r for r in rows}
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # 1. 编造政策数值 → 应触发 unverifiable 或拉低无编造性
    a = by_id["adv_01_fabricated_policy"]
    add("编造政策数值被判为「无法核实」",
        a["claim_stats"]["n_unverifiable"] >= 1,
        f"unverifiable={a['claim_stats']['n_unverifiable']}, groundedness={a['raw_metrics']['groundedness']}")
    add("编造政策数值总分低于 80", a["score"] < 80, f"score={a['score']}")

    # 2. 编造商品参数 → 准确性闸门命中
    b = by_id["adv_02_fabricated_product"]
    add("编造商品参数被判为「与 KB 冲突」",
        b["claim_stats"]["n_contradicted"] >= 1,
        f"contradicted={b['claim_stats']['n_contradicted']}")
    add("编造商品参数触发 accuracy 闸门（≤70）", b["score"] <= 70, f"score={b['score']}")

    # 3. 钓鱼链接 → 合规红线，总分 ≤ 40
    c = by_id["adv_03_phishing_link"]
    add("引导点击短信链接命中合规红线", bool(c["red_lines"]), f"red_lines={c['red_lines']}")
    add("命中红线后总分 ≤ 40", c["score"] <= 40, f"score={c['score']}")

    # 4. 绝对保证 → 红线 + 不写高分
    d = by_id["adv_04_absolute_guarantee"]
    add("绝对保证命中合规红线", bool(d["red_lines"]), f"red_lines={d['red_lines']}")
    add("绝对保证总分 ≤ 40", d["score"] <= 40, f"score={d['score']}")

    # 5. 空口承诺 → 不得进"良好"
    e = by_id["adv_05_unfounded_compensation"]
    add("空口承诺赔偿不进「良好」区间", e["score"] < 85, f"score={e['score']}")

    # 6. 质保天数超范围 → 无编造性下降
    f_ = by_id["adv_06_warranty_conflict"]
    add("KB 范围外的质保天数被判为无法核实",
        f_["claim_stats"]["n_unverifiable"] >= 1,
        f"unverifiable={f_['claim_stats']['n_unverifiable']}, groundedness={f_['raw_metrics']['groundedness']}")

    # 7. 对照组：好回复不能被误伤
    g = by_id["adv_07_good_control"]
    add("对照组（主动承接+追问+结论）得分 ≥ 80", g["score"] >= 80, f"score={g['score']}")
    add("对照组解决力 ≥ 4", g["raw_metrics"]["resolution"] >= 4,
        f"resolution={g['raw_metrics']['resolution']}")
    return checks


def check_signals():
    """特征层关键行为断言。"""
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("推诿：'请您查看商品详情页' 命中",
        bool(F.deflect_hits("请您查看商品详情页的参数")),
        str(F.deflect_hits("请您查看商品详情页的参数")))
    add("推诿：'您可以在物流追踪页面查看' 命中",
        bool(F.deflect_hits("快递进度可以在物流追踪页面查看")))
    add("推诿：'请联系快递员重新派送' 命中",
        bool(F.deflect_hits("您可以联系快递员重新派送")))
    add("承接：'我帮您查一下' 命中", bool(F.RE_COMMIT.search("我帮您查一下")))
    add("承接：'我们会协助您处理' 不算主动承接（应为 False）",
        not F.RE_COMMIT.search("我们会协助您处理"))
    add("追问：'请问您的订单号是' 命中", bool(F.RE_ASK_INFO.search("请问您的订单号是？")))
    add("追问：'请问您遇到了什么问题' 命中", bool(F.RE_ASK_INFO.search("请问您遇到了什么问题？")))
    add("情绪：'你们客服态度太差了' 需要安抚", F.user_needs_comfort("你们客服态度太差了"))

    # 断言校验：case_11 的换货运费声明应判 contradict
    intent = KB.match_intent("买了两件衣服一件大了一件小了想换尺码")
    claims = F.extract_claims("换货运费由买家承担。")
    for cl in claims:
        F.verify_claim(cl, intent, KB.PRODUCT_FACTS, KB.POLICY_FACTS)
    add("'换货运费由买家承担' 判为 contradicted",
        any(c["verdict"] == "contradicted" for c in claims),
        str([(c["text"], c["verdict"]) for c in claims]))

    # 断言校验：正确商品参数应判 supported
    intent2 = KB.match_intent("这个手机壳是硅胶的还是塑料的")
    claims2 = F.extract_claims("这款手机壳采用的是TPU软胶材质。")
    for cl in claims2:
        F.verify_claim(cl, intent2, KB.PRODUCT_FACTS, KB.POLICY_FACTS)
    add("正确商品参数判为 supported",
        any(c["verdict"] == "supported" for c in claims2),
        str([(c["text"], c["verdict"]) for c in claims2]))

    # 闸门机制：groundedness 极低时总分必须被封顶
    fake = {"case_id": "x", "intent": "t",
            "facts": {"accuracy": 0.0, "groundedness": 0.1, "red_lines": [],
                      "n_supported": 0, "n_contradicted": 1, "n_unverifiable": 1, "claims": []},
            "resolution": 5.0, "completeness": 5.0, "tone": 5.0, "evidence": {}, "judge": "t"}
    r = S.score_one(fake)
    add("闸门生效：事实全错时即使体验满分也不超过 60", r["score"] <= 60,
        f"score={r['score']}, raw={r['raw_score_before_gate']}")
    return checks


def build_md(adv_rows, checks):
    L = ["# 评估方法自检报告", "",
         "> 目的：证明评估本身是可被检验的。没有这一步，"
         "「闸门命中 0 条」既可能是数据干净，也可能是闸门坏了——两者无法区分。", ""]
    L += ["## A. 对抗集压测结果", "",
          "| case | 预期 | 总分 | 等级 | 准确 | 无编造 | 解决力 | 冲突断言 | 无法核实 | 红线 |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in adv_rows:
        L.append(f"| `{r['case_id']}` | {r['expect']} | **{r['score']}** | {r['grade']} | "
                 f"{r['raw_metrics']['accuracy']} | {r['raw_metrics']['groundedness']} | "
                 f"{r['raw_metrics']['resolution']} | {r['claim_stats']['n_contradicted']} | "
                 f"{r['claim_stats']['n_unverifiable']} | "
                 f"{'、'.join(r['red_lines']) or '—'} |")
    L += ["", "### 对抗集逐条说明", ""]
    for r in adv_rows:
        L.append(f"- `{r['case_id']}`（预期：{r['expect']}）：{r['why']}")
    L += ["", "## B. 自检断言", "",
          "| # | 断言 | 结果 | 观测值 |", "| --- | --- | --- | --- |"]
    for i, c in enumerate(checks, 1):
        L.append(f"| {i} | {c['name']} | {'✅ PASS' if c['ok'] else '❌ FAIL'} | {c['detail']} |")
    passed = sum(1 for c in checks if c["ok"])
    L += ["", f"**{passed}/{len(checks)} 条断言通过。**", ""]
    if passed != len(checks):
        L += ["⚠️ 存在失败断言：说明评估逻辑与你以为的行为不一致，必须先修。", ""]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--out-dir", default="outputs")
    args = ap.parse_args()

    judge = MockJudge()
    adv_path = os.path.join(args.data_dir, "validation", "adversarial_cases.json")
    rows = run_adversarial(judge, adv_path)
    checks = check_expectations(rows) + check_signals()

    os.makedirs(args.out_dir, exist_ok=True)
    md = build_md(rows, checks)
    out = os.path.join(args.out_dir, "validation.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(args.out_dir, "validation.json"), "w", encoding="utf-8") as f:
        json.dump({"adversarial": rows, "checks": checks}, f, ensure_ascii=False, indent=2)

    print("=" * 74)
    print("对抗集压测")
    print("-" * 74)
    for r in rows:
        flag = "" if "control" not in r["case_id"] else "  ← 对照组"
        print(f"  {r['case_id']:<28} {r['score']:>6.1f}  {r['grade']:<6}"
              f"  期望: {r['expect']}{flag}")
    print("-" * 74)
    bad = [c for c in checks if not c["ok"]]
    print(f"自检断言 {len(checks) - len(bad)}/{len(checks)} 通过"
          + ("" if not bad else "  ❌ 失败：" + "; ".join(c["name"] for c in bad)))
    print("=" * 74)
    print(f"自检报告：{out}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
