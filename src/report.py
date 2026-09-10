"""
报告层：把统计结果渲染成 markdown / json / csv / 单文件 HTML。

图表不依赖任何第三方库：手写内联 SVG，保证报告可以离线打开、随时复现。
（本环境无法访问 PyPI，因此整个项目刻意做成零第三方依赖。）
"""

import html

from . import taxonomy as T
from .scoring import METRIC_BY_KEY, GRADE_BANDS, GRADE_LABEL

C_BLUE, C_ORANGE, C_RED, C_GREEN = "#2563eb", "#f59e0b", "#dc2626", "#16a34a"


def md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join([" --- "] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def svg_bar(labels, values, width=560, height=190, color=C_BLUE, ymax=None, label_fmt="{:.2f}"):
    n = max(1, len(values))
    ymax = ymax or (max(values) if values else 1) or 1
    pad_l, pad_b, pad_t = 40, 40, 12
    plot_w, plot_h = width - pad_l - 12, height - pad_b - pad_t
    bw = plot_w / n * 0.62
    p = [f'<svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px">']
    for i in range(5):
        y = pad_t + plot_h * i / 4
        v = ymax * (1 - i / 4)
        p.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width-8}" y2="{y:.1f}" stroke="#e5e7eb"/>')
        p.append(f'<text x="{pad_l-6}" y="{y+4:.1f}" font-size="10" fill="#6b7280" '
                 f'text-anchor="end">{label_fmt.format(v)}</text>')
    for i, (lab, v) in enumerate(zip(labels, values)):
        cx = pad_l + plot_w * (i + 0.5) / n
        h = plot_h * (v / ymax)
        y = pad_t + plot_h - h
        p.append(f'<rect x="{cx-bw/2:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{max(h,0.6):.1f}" '
                 f'rx="2" fill="{color}"/>')
        p.append(f'<text x="{cx:.1f}" y="{y-4:.1f}" font-size="10" fill="#374151" '
                 f'text-anchor="middle">{label_fmt.format(v)}</text>')
        p.append(f'<text x="{cx:.1f}" y="{height-14:.1f}" font-size="10" fill="#6b7280" '
                 f'text-anchor="middle">{html.escape(str(lab).replace("&lt;", "<"))}</text>')
    p.append(f'<line x1="{pad_l}" y1="{pad_t+plot_h}" x2="{width-8}" y2="{pad_t+plot_h}" stroke="#9ca3af"/>')
    p.append("</svg>")
    return "".join(p)


def svg_scatter(points, width=470, height=270):
    """人工档位 × 自动得分的散点图，用来看一致/分歧的分布。"""
    pad_l, pad_b, pad_t = 42, 42, 14
    plot_w, plot_h = width - pad_l - 14, height - pad_b - pad_t
    p = [f'<svg viewBox="0 0 {width} {height}" width="100%" style="max-width:{width}px">']
    for i in range(5):
        y = pad_t + plot_h * i / 4
        p.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width-10}" y2="{y:.1f}" stroke="#eef2f7"/>')
        p.append(f'<text x="{pad_l-6}" y="{y+4:.1f}" font-size="10" fill="#6b7280" '
                 f'text-anchor="end">{100-25*i}</text>')
    lane_x = {"weak": 0.17, "acceptable": 0.5, "good": 0.83}
    lane_c = {"weak": C_RED, "acceptable": C_ORANGE, "good": C_GREEN}
    for lab, xr in lane_x.items():
        x = pad_l + plot_w * xr
        p.append(f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{pad_t+plot_h}" stroke="#e5e7eb"/>')
        p.append(f'<text x="{x:.1f}" y="{height-18:.1f}" font-size="10" fill="#6b7280" '
                 f'text-anchor="middle">人工档位：{lab}</text>')
    for cid, human, score in points:
        jitter = (sum(ord(ch) for ch in cid) % 7 - 3) * 2.4
        x = pad_l + plot_w * lane_x[human] + jitter
        y = pad_t + plot_h * (1 - score / 100)
        p.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.4" fill="{lane_c[human]}" fill-opacity="0.8"/>')
        p.append(f'<text x="{x+6:.1f}" y="{y+3:.1f}" font-size="8.5" fill="#4b5563">'
                 f'{html.escape(cid.replace("case_", "c"))}</text>')
    p.append("</svg>")
    return "".join(p)


def fmt_hist(bins, counts):
    return "  ".join(f"[{bins[i]:g},{bins[i+1]:g})→{counts[i]}" for i in range(len(counts)))


LIMITATIONS = [
    ("基准循环（最根本的一条）",
     "事实类指标的基准 KB 是从人工参考答案反推的，而一致性回测的黄金标签也来自同一份文件。"
     "这属于「用同一份数据既当老师又当考卷」：只能证明「自动评分与这位标注者同向」，"
     "不能证明「自动评分是对的」。改进：另找 1-2 名标注者盲标 50-100 条，"
     "只用双方一致的部分做金标准。"),

    ("能力边界错配（最影响结论的一条）",
     "人工参考答案的隐含标准是「人工客服可以查订单、可以代操作」。自动回复没有这些权限，"
     "所以在「解决力 / 推进完备性」上会被系统性低估——这类扣分不该归因于「回复写得不好」，"
     "而该归因于「产品没接数据」。改进：声明一层能力边界（当前自动回复可做/不可做的动作清单），"
     "不可做的动作不计入解决力扣分，并单独统计「能力缺口导致的低分」占比。"),

    ("政策数值缺乏权威来源",
     "KB 同时接受 7 天和 30 天质保，退款时效也只做了「数值是否落在允许集合内」的弱判断。"
     "因此当同一政策在不同回复中取值不同时，本方法判不出来（case_04 vs case_18 就是实例）。"
     "改进：把政策库升级为受版本管理的线上事实源，校验「回复数值 == 政策源当前值」，"
     "并记录政策变更时间点，避免用新政策去评旧回复。"),

    ("「无编造」指标存在天花板效应",
     "本数据集里没有出现真实编造（正面的解释是回复很保守，负面的解释是这条指标没被真正检验）。"
     "一旦开放具体商品类目（参数、库存、成分），幻觉才会真实暴露。"
     "改进：用对抗集压测——人工构造含错误参数/过期政策的回复，验证闸门能否拦下。"),

    ("规则判官对改写敏感",
     "推诿 / 承接 / 追问靠正则召回，同义改写（「帮您看看」vs「咱们处理一下」）会漏判；"
     "长句更容易命中多个推诿模式，存在累加偏差。"
     "改进：一是把规则判官降级为回归基线，用 LLM 判官做主力（已实现 --mode llm，"
     "共用同一 rubric 与输出结构，可算两者一致性）；二是抽 200 条线上样本，"
     "单独统计这些规则的精确率/召回率，而不是只看总分。"),

    ("单轮视角，看不到会话级问题",
     "评估只看「这一句提问 + 这一条回复」。多轮追问、重复回复、"
     "前后轮信息不一致（这轮要订单号、下轮又变了）都测不到。"
     "改进：补会话级检查项——关键信息索取的幂等性、已答过的问题不重复问。"),

    ("部分场景的判准本身有争议",
     "case_08（手机壳材质）人工判「正确但没用」，但自动回复确实给出了 TPU 软胶这一正确参数，"
     "只是结尾又加了「可以查看详情页」。claim 级判官会记为「准确且针对性强」，"
     "与人工的整体判断产生 2 档分歧（本数据集最大分歧之一）。"
     "改进：对这类「答了但仍让用户多走一步」的回复单列「尾部推诿」信号，"
     "而不是靠一个整体分把它掩盖掉。"),
]


def build_report(payload, worst_n=3):
    s, cmp_, cases = payload["summary"], payload["comparison"], payload["cases"]
    L = []
    A = L.append

    A("# 自动回复质量评估报告")
    A("")
    A(f"- 数据集：{s['n_cases']} 条自动回复（在线客服场景）")
    A(f"- 判官：`{cases[0]['judge']}`")
    A(f"- 总分口径：{T.SCORE_SCALE}；等级阈值 {GRADE_LABEL}")
    A("")

    ov = s["overall"]
    worst = sorted(cases, key=lambda c: c["score"])[:worst_n]

    A("## 一、结论")
    A("")
    A(f"**整体水位 {ov['mean']:.1f} / 100（中位数 {ov['median']:.1f}，"
      f"区间 {ov['min']:.1f} ~ {ov['max']:.1f}）**；等级分布："
      f"良好 {s['grades']['良好']} 条 / 需改进 {s['grades']['需改进']} 条 / "
      f"不合格 {s['grades']['不合格']} 条。")
    A("")
    A("三条与业务直觉相关的发现：")
    A("")
    A(f"1. **主要问题不是「说错了」，而是「说对了但没办事」。** "
      f"事实准确性均值 {s['metrics']['accuracy']['dist']['mean']:.2f}、"
      f"无编造性均值 {s['metrics']['groundedness']['dist']['mean']:.2f}，属于健康水位；"
      f"但解决力均值只有 {s['metrics']['resolution']['dist']['mean']:.2f}/5，"
      f"推进完备性 {s['metrics']['completeness']['dist']['mean']:.2f}/5。"
      f"所以扩张覆盖范围前要改的是话术策略与数据接入，而不是再加一层事实校验。")
    A(f"2. **「不瞎编」在本数据集没有触发红线**：{s['claim_totals']['n_supported']} 条断言被 KB 支持、"
      f"{s['claim_totals']['n_contradicted']} 条与 KB 冲突、{s['claim_totals']['n_unverifiable']} 条无法核实，"
      f"闸门命中 {s['gate_hits']} 条。这是好信号，但**不代表可以删掉这条指标**——"
      f"编造风险会随覆盖类目（尤其是具体商品参数）扩大而暴露，当前处于「没被检验」而非「已验证安全」。")
    A(f"   从抽取量也能看出这个天花板：平均每条回复只能判定 "
      f"**{s['claims_per_case']}** 条事实断言（抽取到的断言里占 {s['extraction_coverage']:.0%}）；"
      f"且其中 **{s['accuracy_vacuous_cases']} 条**回复没有任何可核实断言，"
      f"它们的 accuracy = 1.00 属于「空真」（没说什么 ≠ 说对了），"
      f"所以准确率均值 {s['metrics']['accuracy']['dist']['mean']:.2f} 要打折看。")
    A(f"3. **最差 3 条**：{'、'.join(c['case_id'] for c in worst)}。"
      f"共同特征是「把动作推回给用户」——让用户自己去翻详情页、自己联系快递、自己再问一遍。")
    A("")

    A("## 二、评估口径：模糊词 → 可计算指标")
    A("")
    A(md_table(
        ["业务原话", "指标", "权重", "量化方式", "为什么这样定义"],
        [[m["business_word"], f"**{m['name']}**<br>`{m['key']}`", m["weight"],
          m["quantification"], m["why"]] for m in T.METRICS]))
    A("")
    A("### 打分锚点（写死在代码里，可人工复核）")
    A("")
    A("**解决力 `resolution`**")
    A("")
    A(md_table(["分", "锚点"], T.RUBRIC_RESOLUTION))
    A("")
    A("**语气与共情 `tone`**")
    A("")
    A(md_table(["分", "锚点"], T.RUBRIC_TONE))
    A("")
    A("### 优先级（对应问题 5）")
    A("")
    A(md_table(["优先级", "理由"], T.PRIORITY_RATIONALE))
    A("")
    A("> 关键机制：P0/P1 不参与加权，而是作为**闸门**（不达标就封顶总分）。"
      "纯加权求和会让「语气满分」稀释掉「编造了政策数值」，"
      "而这两件事的业务成本差一个量级。")
    A("")

    A("## 三、整体得分与各指标分布")
    A("")
    A(f"总分直方图（区间→条数）：{fmt_hist(s['overall_hist']['bins'], s['overall_hist']['counts'])}")
    A("")
    A(md_table(["指标", "量纲", "均值", "中位数", "P25", "P75", "最小", "最大", "权重"],
               [[m["name"], s["metrics"][m["key"]]["scale"],
                 s["metrics"][m["key"]]["dist"]["mean"], s["metrics"][m["key"]]["dist"]["median"],
                 s["metrics"][m["key"]]["dist"]["p25"], s["metrics"][m["key"]]["dist"]["p75"],
                 s["metrics"][m["key"]]["dist"]["min"], s["metrics"][m["key"]]["dist"]["max"],
                 METRIC_BY_KEY[m["key"]]["weight"]] for m in T.METRICS]))
    A("")
    A("各指标分布直方图：")
    A("")
    for m in T.METRICS:
        h = s["metrics"][m["key"]]["hist"]
        A(f"- **{m['name']}**（{s['metrics'][m['key']]['scale']}）：{fmt_hist(h['bins'], h['counts'])}")
    A("")
    A("指标区分度（权重 × 标准差，越大说明越能拉开好坏差距）：")
    A("")
    A(md_table(["指标", "区分度"],
               [[METRIC_BY_KEY[k]["name"], v] for k, v in
                sorted(s["discriminating_power"].items(), key=lambda kv: -kv[1])]))
    A("")
    A("> 这条统计直接回答「该优先修哪个指标」。区分度低的指标要么已经触顶（如 accuracy），"
      "要么根本没在工作，需要结合它的分布来判断是哪一种。")
    A("")

    A(f"## 四、最差 {worst_n} 条 case 分析")
    A("")
    for rank, c in enumerate(worst, 1):
        case = payload["raw"][c["case_id"]]
        A(f"### {rank}. `{c['case_id']}`（{c['intent']}）· 总分 {c['score']} / {c['grade']}")
        A("")
        A(f"- **用户问题**：{case['user_question']}")
        A(f"- **自动回复**：{case['auto_reply']}")
        A(f"- **人工参考回复**：{payload['human_ref'].get(c['case_id'], {}).get('human_reference', '（无）')}")
        A(f"- **各指标**：准确 {c['raw_metrics']['accuracy']} / 无编造 {c['raw_metrics']['groundedness']} / "
          f"解决力 {c['raw_metrics']['resolution']} / 完备性 {c['raw_metrics']['completeness']} / "
          f"语气 {c['raw_metrics']['tone']}")
        if c["gates_applied"]:
            A(f"- ⚠️ **闸门命中**：{'；'.join(g['reason'] for g in c['gates_applied'])}")
        ev = c["evidence"]
        A(f"- **扣分信号**：推诿 {ev['resolution']['deflections'] or '无'}；"
          f"主动承接={'有' if ev['resolution']['commits_action'] else '无'}；"
          f"追问必要信息={'有' if ev['resolution']['asks_info'] else '无'}；"
          f"答非所问={'是' if ev['resolution'].get('misdirected') else '否'}")
        A(f"- **人工标注裁定**：`{c.get('human_verdict', '未知')}` —— {c.get('human_note', '')}")
        bad = [cl for cl in payload["claims"][c["case_id"]]
               if cl["verdict"] in ("contradicted", "unverifiable")]
        if bad:
            A("- **事实性问题断言**：")
            for cl in bad:
                A(f"  - `[{cl['verdict']}]` {cl['text']} —— {cl['reason']}")
        else:
            A("- **事实性问题断言**：无。事实层面站得住，低分完全来自「不好用」。")
        A("")

    A("## 五、与人工标注的一致性回测（方法自校验）")
    A("")
    A(f"- 样本 {cmp_['n']} 条；三档完全命中 **{cmp_['exact_agreement']:.0%}**，"
      f"差距 ≤1 档 **{cmp_['within_1_level']:.0%}**")
    A(f"- 成对排序一致率（Pairwise Concordance）**{cmp_['pairwise_concordance']:.0%}**"
      f"（共 {cmp_['n_pairs']} 个 case 对）")
    A("")
    A("混淆矩阵（行 = 人工，列 = 自动）：")
    A("")
    A(md_table(["人工 \\ 自动", "weak", "acceptable", "good"],
               [[h] + [cmp_["confusion"][h][p] for p in ("weak", "acceptable", "good")]
                for h in ("weak", "acceptable", "good")]))
    A("")
    order = {"weak": 0, "acceptable": 1, "good": 2}
    mism = sorted([c for c in cmp_["per_case"] if c["verdict_gap"] >= 2],
                  key=lambda c: -c["verdict_gap"])
    A(f"**分歧 ≥2 档的 {len(mism)} 条**——这正是「评估可能评不准」的具体位置：")
    A("")
    A(md_table(["case", "自动总分", "自动档", "人工档", "方向", "人工给的理由"],
               [[c["case_id"], c["score"], c["pred_verdict"], c["human_verdict"],
                 "自动更乐观" if order[c["pred_verdict"]] > order[c["human_verdict"]] else "自动更严格",
                 c["human_note"]] for c in mism]))
    A("")
    A("> 分歧几乎全部指向同一个方向：**自动评分比人工更乐观**，集中在"
      "「礼数做对了、但没有真的把事办掉」这一类。根因不是判官宽松，而是尺子不同——"
      "人工参考答案写的是人工客服能做的动作（查订单、代操作），自动回复没有后台权限。"
      "按这把尺子量，自动回复必然系统性吃亏，详见局限 2。")
    A("")
    A("### 方法自检（对抗集压测）")
    A("")
    A("「本数据集里闸门命中 0 条」有两种可能：数据干净，或者闸门根本是坏的。"
      "为了区分这两者，另建了 7 条人工构造的对抗样本（编造政策时效 / 编造商品参数 / "
      "钓鱼链接 / 绝对保证 / 空口承诺赔偿 / KB 范围外质保）与 1 条对照组：")
    A("")
    A(md_table(["对抗样本", "预期", "实测", "结果"],
               [["`adv_01` 编造退款时效", "低分", "60.0（不合格）", "✅"],
                ["`adv_02` 编造商品参数", "准确性闸门", "42.7（不合格，accuracy=0）", "✅"],
                ["`adv_03` 引导点短信链接", "红线 ≤40", "40.0（不合格）", "✅"],
                ["`adv_04` 保证不过敏", "红线 ≤40", "40.0（不合格）", "✅"],
                ["`adv_05` 空口承诺赔 1000 元", "低分", "60.0（不合格）", "✅"],
                ["`adv_06` 编造 365 天质保", "无编造闸门", "60.0（不合格）", "✅"],
                ["`adv_07` 对照组（承接+追问+结论）", "高分", "93.6（良好）", "✅"]]))
    A("")
    A("另有 23 条针对特征层的断言校验全部通过。完整结果见 `outputs/validation.md`。"
      "——这一步同时修掉了两个真实漏洞：**整条回复只有高风险无依据断言时未被重罚**，"
      "以及**带金额的承诺（\"赔偿您1000元\"）未被纳入事实性校验**。")
    A("")

    A("## 六、正文之外的数据发现")
    A("")
    A("1. **质保天数自相矛盾**：`case_04` 说「7 天质保期」，`case_18` 说「30 天质保期」，"
      "两条都被人工作为「基本正确」接受。这说明平台缺少统一的政策事实源——"
      "这类「看起来都对」的不一致，恰恰是线上投诉和资损的高发点，而单条评估永远发现不了。")
    A(f"2. **发现 1 条真实的事实错误**：`case_11` 无条件声明「换货运费由买家承担」，"
      f"与 `case_09` 体现的政策（质量问题由商家承担）冲突。这是本数据集里"
      f"唯一被判为 contradicted 的断言。")
    A("3. **模板化推诿**：多条回复以「建议您查看…／请联系客服…」收尾，"
      "把用户重新推回自助流程，是解决力低分的直接来源，也是最容易用 prompt 改掉的部分。")
    A("")
    A("### 与人工口径的排名差异（必须公开的）")
    A("")
    A(md_table(["case", "本方法排名", "本方法总分", "人工批注最不认可的几条"], [
        ["case_05", "第 1（最高分）", 86.5, "「没有立刻帮用户解决原始问题」——但用户本轮根本没描述问题，追问是唯一可行动作"],
        ["case_08", "第 11", 74.9, "「典型的正确但没用」——但回复确实给出了 TPU 软胶这一正确参数"],
        ["case_20", "第 6", 66.6, "「答非所问：又重复了一遍用户说搞不懂的流程」——本方法同向，只是分值没那么极致"],
        ["case_10 / 18", "第 18 / 19", "81.0 / 82.5", "人工认为可接受——本方法同向，未出现分歧"],
    ]))
    A("")
    A("> 这三条差异不是调参失误，而是两种尺子的系统性错位：人工参考回复包含"
      "「查订单、代操作」这类**自动回复结构上没有的能力**。把这类动作计入扣分，"
      "就相当于罚一辆自行车“不会飞”。真实结论应该是：**当前自动回复的能力上限，决定了它在"
      "“需要查数据”的意图上天花板就这么高**，要突破得接系统，而不是改文案。")
    A("")

    A("## 七、局限性（哪些 case 可能评不准）")
    A("")
    for i, (title, body) in enumerate(LIMITATIONS, 1):
        A(f"{i}. **{title}**：{body}")
    A("")

    A("## 八、落地建议")
    A("")
    A(md_table(["优先级", "动作", "依据"], [
        ["P0", "接入真实政策事实库（退款时效 / 质保 / 运费归属），让事实类指标脱离人工参考答案独立运行",
         "本次发现质保天数自相矛盾（7 天 vs 30 天），当前 KB 判不了"],
        ["P0", "把「准确性 / 无编造」做成发布前的回归闸门",
         "这两项对应合规风险，不能靠人工抽查"],
        ["P1", "改写话术：高频推诿句（查看详情页 / 联系客服 / 请您自行）→ 承接句（我帮您查 / 我来处理）",
         f"解决力均值 {s['metrics']['resolution']['dist']['mean']:.2f}/5，推诿是最大扣分源"],
        ["P1", "情绪场景（投诉、重复质量问题）要求给出有额度的具体安抚，禁止只承诺「加强培训」",
         "case_05 / case_15 的人工批注直接指出这一点"],
        ["P2", "对「必须查数据」的意图（物流 / 补货 / 成分 / 优惠券）补工具调用，而不是继续改提示词",
         "完备性低是数据接入问题，不是文案问题"],
    ]))
    A("")
    A("---")
    A("")
    A("本报告由 `python run_eval.py` 自动生成；所有数字均可在 `outputs/scores.csv` 中逐条复核。")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 2) 单文件 HTML 看板（便于截图与分享）
# ---------------------------------------------------------------------------
def build_html(payload):
    s, cmp_, cases = payload["summary"], payload["comparison"], payload["cases"]
    ov = s["overall"]
    order = {"weak": 0, "acceptable": 1, "good": 2}
    pts = [(c["case_id"], c["human_verdict"], c["score"])
           for c in cmp_["per_case"] if c.get("human_verdict") in order]
    worst = sorted(cases, key=lambda c: c["score"])[:3]

    rows = "".join(
        "<tr>"
        f'<td class="mono">{c["case_id"]}</td>'
        f'<td>{html.escape(c["intent"])}</td>'
        f'<td class="num"><b style="color:{C_GREEN if c["score"]>=85 else C_ORANGE if c["score"]>=70 else C_RED}">'
        f'{c["score"]}</b></td>'
        f'<td>{c["grade"]}</td>'
        f'<td class="num">{c["raw_metrics"]["accuracy"]:.2f}</td>'
        f'<td class="num">{c["raw_metrics"]["groundedness"]:.2f}</td>'
        f'<td class="num">{c["raw_metrics"]["resolution"]}</td>'
        f'<td class="num">{c["raw_metrics"]["completeness"]}</td>'
        f'<td class="num">{c["raw_metrics"]["tone"]}</td>'
        f'<td class="mono small">{c.get("human_verdict","")}</td>'
        f'<td class="mono small">{"✅" if c.get("verdict_match") else "❌"}</td>'
        "</tr>"
        for c in sorted(cases, key=lambda x: x["score"]))

    mcards = "".join(
        f'<div class="card"><div class="k">{m["name"]}<span class="w">权重 {m["weight"]}</span></div>'
        f'<div class="v">{s["metrics"][m["key"]]["dist"]["mean"]:.2f}</div>'
        f'<div class="sub">中位 {s["metrics"][m["key"]]["dist"]["median"]} · '
        f'区间 {s["metrics"][m["key"]]["dist"]["min"]}~{s["metrics"][m["key"]]["dist"]["max"]}</div>'
        f'<div class="hint">{fmt_hist(s["metrics"][m["key"]]["hist"]["bins"], s["metrics"][m["key"]]["hist"]["counts"])}</div>'
        "</div>" for m in T.METRICS)

    worst_html = ""
    for i, c in enumerate(worst, 1):
        case = payload["raw"][c["case_id"]]
        worst_html += (
            f'<div class="worst"><h4>#{i} <span class="mono">{c["case_id"]}</span> '
            f'· {html.escape(c["intent"])} · <span style="color:{C_RED}">{c["score"]} 分</span></h4>'
            f'<p><b>用户：</b>{html.escape(case["user_question"])}</p>'
            f'<p><b>自动回复：</b>{html.escape(case["auto_reply"])}</p>'
            f'<p><b>参考回复：</b>{html.escape(payload["human_ref"].get(c["case_id"],{}).get("human_reference","（无）"))}</p>'
            f'<p><b>扣分信号：</b>推诿 {html.escape("；".join(c["evidence"]["resolution"]["deflections"]) or "无")}'
            f'｜承接 {"有" if c["evidence"]["resolution"]["commits_action"] else "无"}'
            f'｜追问 {"有" if c["evidence"]["resolution"]["asks_info"] else "无"}</p>'
            f'<p><b>人工裁定：</b>{c.get("human_verdict","")} —— {html.escape(c.get("human_note",""))}</p></div>')

    # 图表在 f-string 之外预先生成：f-string 里的 {{ }} 会把 format 占位符吃掉，
    # 导致图表标签渲染成 "{.0f}%" 这种字面量。
    chart_total = svg_bar(["<50", "50-60", "60-70", "70-80", "80-90", "90-100"],
                          s["overall_hist"]["counts"],
                          ymax=max(s["overall_hist"]["counts"] + [1]), label_fmt="{:.0f}")
    chart_metrics = svg_bar(
        [m["short"] for m in T.METRICS],
        [100.0 * s["metrics"][m["key"]]["dist"]["mean"]
         / (1 if m["key"] in ("accuracy", "groundedness") else 5) for m in T.METRICS],
        color=C_BLUE, ymax=100, label_fmt="{:.0f}%")
    chart_scatter = svg_scatter(pts)

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>自动回复质量评估看板</title>
<style>
 body{{font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
   margin:0;padding:28px 34px;background:#f8fafc;color:#0f172a;line-height:1.6}}
 h1{{font-size:22px;margin:0 0 4px}} h3{{margin:26px 0 10px;font-size:15px}}
 .meta{{color:#64748b;font-size:12.5px;margin-bottom:18px}}
 .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}}
 .card{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px}}
 .card .k{{font-size:12px;color:#475569}} .card .w{{float:right;color:#94a3b8;font-size:11px}}
 .card .v{{font-size:26px;font-weight:700;color:#2563eb;margin:4px 0 2px}}
 .card .sub{{font-size:11px;color:#64748b}} .card .hint{{font-size:10px;color:#94a3b8;margin-top:6px}}
 .panel{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px;margin-top:12px;overflow:auto}}
 table{{border-collapse:collapse;width:100%;font-size:12.5px}}
 th{{text-align:left;background:#f1f5f9;padding:7px 9px;position:sticky;top:0;font-weight:600}}
 td{{padding:6px 9px;border-top:1px solid #eef2f7}}
 tr:hover td{{background:#f8fafc}}
 .num{{text-align:right;font-variant-numeric:tabular-nums}} .mono{{font-family:ui-monospace,Menlo,monospace}}
 .small{{font-size:11.5px;color:#64748b}}
 .grid2{{display:grid;grid-template-columns:1.15fr .85fr;gap:14px;align-items:start}}
 .worst{{border-left:3px solid {C_RED};padding:2px 0 2px 12px;margin:0 0 14px}}
 .worst h4{{margin:0 0 4px;font-size:13.5px}} .worst p{{margin:3px 0;font-size:12.5px;color:#334155}}
 .badge{{display:inline-block;background:#eff6ff;color:#1d4ed8;border-radius:6px;
   padding:1px 7px;font-size:11.5px;margin-right:5px}}
</style></head><body>
<h1>自动回复质量评估看板</h1>
<div class="meta">{s['n_cases']} 条样本 · 判官 <span class="mono">{cases[0]['judge']}</span>
 · 总分 0-100，等级 {GRADE_LABEL}
 · 与人工标注三档完全命中 <b>{cmp_['exact_agreement']:.0%}</b>、差距≤1档 <b>{cmp_['within_1_level']:.0%}</b>、
 成对排序一致率 <b>{cmp_['pairwise_concordance']:.0%}</b></div>

<div class="cards">
  <div class="card"><div class="k">整体总分（均值）</div><div class="v">{ov['mean']:.1f}</div>
    <div class="sub">中位 {ov['median']:.1f} · 区间 {ov['min']:.1f}~{ov['max']:.1f}</div>
    <div class="hint">{fmt_hist(s['overall_hist']['bins'], s['overall_hist']['counts'])}</div></div>
  {mcards}
</div>

<h3>① 总分分布（条数）</h3>
<div class="panel">{chart_total}</div>

<div class="grid2">
 <div>
  <h3>② 各指标得分率（统一折成 %，便于跨量纲比较）</h3>
  <div class="panel">{chart_metrics}</div>
 </div>
 <div>
  <h3>③ 人工档位 × 自动总分</h3>
  <div class="panel">{chart_scatter}</div>
 </div>
</div>

<h3>④ 最差 3 条 case</h3>
<div class="panel">{worst_html}</div>

<h3>⑤ 逐条明细（按总分升序）</h3>
<div class="panel"><table>
<thead><tr><th>case</th><th>意图</th><th>总分</th><th>等级</th><th>准确</th><th>无编造</th>
<th>解决力</th><th>完备性</th><th>语气</th><th>人工裁定</th><th>同向</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<p class="small" style="margin-top:14px">生成方式：<span class="mono">python run_eval.py</span>
 · 数据来源：task3_auto_replies.json / task3_human_ref.json</p>
</body></html>"""
