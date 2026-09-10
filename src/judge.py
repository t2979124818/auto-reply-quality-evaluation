"""
判官层：把「特征」变成「指标得分」。

两种后端共用同一套 rubric（taxonomy.RUBRIC_*）和同一份输出结构：

  MockJudge —— 确定性规则判官，零依赖、可离线跑、结果可复现
  LLMJudge  —— 调用 OpenAI 兼容接口，只负责主观类指标（1-5 rubric 打分），
               事实类指标（准确/无编造）**始终**由确定性校验给出

为什么事实类指标不交给 LLM
--------------------------
让 LLM 判"这条回复有没有编造"是循环论证：它和生成回复的模型共享同样的知识盲区，
既判不出幻觉，也无法解释扣分点。所以事实性一律走 KB 比对，可溯源、可回归。
"""

import json
import os
import re
import urllib.error
import urllib.request

from . import features as F
from . import kb as KB
from . import taxonomy as T

# 断言类型 → 该类型"无法核实"时的可信分（0=完全不可信，1=完全可信）
# contradicted 一律记 0 分，unverifiable 按类型给部分分：
#   - 政策数值 / 商品参数 这类"编造后果严重"的断言，不确定就重罚（0.5）
#   - 系统能力断言这类"后果轻"的断言，不确定给较高分（0.85）
CLAIM_CREDIT = {
    "policy_amount": {"supported": 1.0, "unverifiable": 0.50, "contradicted": 0.0},
    "product_attribute": {"supported": 1.0, "unverifiable": 0.50, "contradicted": 0.0},
    "commitment_amount": {"supported": 1.0, "unverifiable": 0.40, "contradicted": 0.0},
    "policy_statement": {"supported": 1.0, "unverifiable": 0.70, "contradicted": 0.0},
    "capability_claim": {"supported": 1.0, "unverifiable": 0.85, "contradicted": 0.20},
}
# 高风险断言类型：无法核实时要重罚，因为用户会照着行动
HIGH_RISK_KINDS = {"policy_amount", "product_attribute", "commitment_amount"}
# 先验：假设"一条没被判官抓到问题的回复"整体可信 0.85，权重 2 条虚拟断言。
# 作用是给小样本（只抽到 1 条断言）做平滑，避免"1 条不确定断言 → 幻觉率 100%"。
PRIOR_CREDIT, PRIOR_WEIGHT = 0.85, 2.0

RE_STEPS = re.compile(r"1\.|2\.|3\.|流程(一般)?包括|步骤|依次")
RE_TROUBLESHOOT = re.compile(r"建议您尝试|尝试以下操作|排查|重新配对|重启")


def _clamp(x, lo=1.0, hi=5.0):
    return max(lo, min(hi, x))


class MockJudge:
    """确定性规则判官。"""

    name = "mock-rules-v1"

    def __init__(self, data_dir=None):
        self.policy_facts = KB.POLICY_FACTS
        self.product_facts = KB.PRODUCT_FACTS

    # -- 事实类指标（确定性，两种后端共用） -------------------------------
    def judge_facts(self, case, intent):
        claims = F.extract_claims(case["auto_reply"])
        for c in claims:
            F.verify_claim(c, intent, self.product_facts, self.policy_facts)

        judged = [c for c in claims if c["verdict"] in ("supported", "contradicted", "unverifiable")]
        n_sup = sum(1 for c in judged if c["verdict"] == "supported")
        n_con = sum(1 for c in judged if c["verdict"] == "contradicted")
        n_unv = sum(1 for c in judged if c["verdict"] == "unverifiable")

        denom = n_sup + n_con
        accuracy = 1.0 if denom == 0 else n_sup / denom

        n_unv_high = sum(1 for c in judged
                         if c["verdict"] == "unverifiable" and c["kind"] in HIGH_RISK_KINDS)

        credits = [CLAIM_CREDIT[c["kind"]][c["verdict"]] for c in judged
                   if c["kind"] in CLAIM_CREDIT]
        groundedness = (PRIOR_WEIGHT * PRIOR_CREDIT + sum(credits)) / (PRIOR_WEIGHT + len(credits))

        # "整条回复没有任何有据可依的断言，只有高风险的无从核实断言" → 必须重罚。
        # 否则会出现：一句编造的时效数值，反而靠先验平滑拿到 0.85 的无编造分。
        if n_sup == 0 and n_unv_high > 0:
            groundedness = min(groundedness, 0.50)

        # 全局合规红线单独判定（一票否决性质）
        red_lines = [reason for pat, reason in KB.GLOBAL_RED_LINES
                     if re.search(pat, case["auto_reply"])]

        return {
            "claims": claims,
            "accuracy": round(accuracy, 4),
            "accuracy_denom": denom,          # 0 表示这条回复没有任何可判定真伪的断言
            "groundedness": round(groundedness, 4),
            "n_supported": n_sup,
            "n_contradicted": n_con,
            "n_unverifiable": n_unv,
            "n_unverifiable_high_risk": n_unv_high,
            "red_lines": red_lines,
        }

    # -- 主观类指标（1-5 rubric） ------------------------------------------
    def judge_resolution(self, case, intent):
        reply = case["auto_reply"]
        ev = {}
        ev["commits_action"] = bool(F.RE_COMMIT.search(reply))
        ev["asks_info"] = bool(F.RE_ASK_INFO.search(reply))
        ev["gives_conclusion"] = bool(F.RE_CONCLUSION.search(reply))
        ev["deflections"] = F.deflect_hits(reply)
        ev["internal_focus"] = bool(F.RE_INTERNAL.search(reply)) and bool(
            (intent or {}).get("internal_focus_penalty"))
        ev["misdirected"] = bool(
            (intent or {}).get("misdirected_trigger")) and not ev["asks_info"] and bool(RE_STEPS.search(reply))
        ev["delayed_remedy"] = bool((intent or {}).get("delayed_remedy")) and bool(
            RE_TROUBLESHOOT.search(reply))
        ev["feedback_ack"] = bool(F.RE_FEEDBACK_ACK.search(reply))
        ev["offer_help"] = bool(F.RE_OFFER_HELP.search(reply))
        # 「首句即推诿」：回复的主干动作就是把用户指向别处，
        # 后面的条件性承诺（如"如果能提供订单号，我可以帮您查"）无法赎回这条回复。
        first_sent = (F.split_sentences(reply) or [reply])[0]
        ev["deflection_primary"] = bool(F.deflect_hits(first_sent))

        score = 3.0
        if ev["commits_action"]:
            score += 1.0
        if ev["asks_info"]:
            score += 0.5
        if ev["gives_conclusion"]:
            score += 0.5
        if ev["feedback_ack"]:
            score += 0.8     # 建议类场景里，"感谢+承诺转达"就是那条最有用的动作
        if ev["offer_help"]:
            score += 0.4
        score -= min(1.5, 0.6 * len(ev["deflections"]))
        if ev["internal_focus"]:
            score -= 0.5
        if ev["misdirected"]:
            score -= 1.0
        if ev["delayed_remedy"]:
            score -= 0.5

        if ev["deflection_primary"]:
            score = min(score, 3.0)

        return round(_clamp(score), 2), ev

    def judge_completeness(self, case, intent):
        reply = case["auto_reply"]
        intent = intent or {}
        asks = bool(F.RE_ASK_INFO.search(reply))
        commits = bool(F.RE_COMMIT.search(reply))
        conclusion = bool(F.RE_CONCLUSION.search(reply))
        deflect_n = len(F.deflect_hits(reply))
        is_policy = bool(re.search(r"退款|退货|换货|质保|运费|邮费|取消|补货|携带", reply))
        has_instance_fact = False
        key = intent.get("product_fact_key")
        if key and key in self.product_facts:
            has_instance_fact = any(re.search(p, reply) for p in self.product_facts[key]["patterns"])
        # case_14 这类感谢+承接，也算把推进工作接住了
        thanks_commit = bool(F.RE_THANKS.search(reply)) and bool(
            re.search(r"转达|反馈|记录|尽力|协助", reply))

        if intent.get("needs_info"):
            if asks and commits and not F.deflect_hits((F.split_sentences(reply) or [reply])[0]):
                score = 5.0
            elif asks and commits:
                score = 3.5          # 有承接但主要动作仍推给了用户
            elif asks:
                score = 4.0
            elif commits:
                score = 3.5
            else:
                score = 2.0 if conclusion else 1.5
        elif intent.get("needs_instance_answer"):
            score = 4.5 if has_instance_fact else 2.0
        else:
            if asks or thanks_commit:
                score = 4.5
            elif is_policy and conclusion:
                score = 4.5
            elif is_policy:
                score = 4.0
            else:
                score = 3.0
        ev = {"asks_info": asks, "commits_action": commits, "gives_conclusion": conclusion,
              "has_instance_fact": has_instance_fact, "deflection_count": deflect_n,
              "mode": "needs_info" if intent.get("needs_info")
                      else "needs_instance_answer" if intent.get("needs_instance_answer")
                      else "policy/advisory"}
        return round(_clamp(score), 2), ev

    def judge_tone(self, case, intent):
        reply = case["auto_reply"]
        needed = F.user_needs_comfort(case["user_question"]) or \
            (intent or {}).get("severity") == "high"
        ev = {
            "empathy": bool(F.RE_EMPATHY.search(reply)),
            "greeting": bool(F.RE_GREET.search(reply)),
            "thanks": bool(F.RE_THANKS.search(reply)),
            "blame": bool(F.RE_BLAME.search(reply)),
            "over_promise": bool(F.RE_OVER_PROMISE.search(reply)),
            "comfort_needed": needed,
            "internal_focus": bool(F.RE_INTERNAL.search(reply)),
            "vague_compensation": bool((intent or {}).get("vague_compensation"))
            and bool(re.search(r"补偿|优惠券", reply))
            and not re.search(r"\d+\s*元", reply),
        }
        score = 3.0
        if ev["greeting"]:
            score += 0.3
        if ev["empathy"]:
            score += 0.8
            if ev["comfort_needed"]:
                score += 0.4
        elif ev["comfort_needed"]:
            score -= 1.0
        if ev["thanks"]:
            score += 0.4
        if ev["blame"]:
            score -= 1.0
        if ev["over_promise"]:
            score -= 0.8
        if ev["internal_focus"]:
            score -= 0.3
        if ev["vague_compensation"]:
            score -= 0.5   # 情绪场景下给出"可申请额外补偿"这类没有额度的模糊承诺
        return round(_clamp(score), 2), ev

    # -- 统一入口 ---------------------------------------------------------
    def judge(self, case, intent):
        facts = self.judge_facts(case, intent)
        res, res_ev = self.judge_resolution(case, intent)
        comp, comp_ev = self.judge_completeness(case, intent)
        tone, tone_ev = self.judge_tone(case, intent)
        return {
            "case_id": case["id"],
            "intent": (intent or {}).get("label", "未分类"),
            "facts": facts,
            "resolution": res,
            "completeness": comp,
            "tone": tone,
            "evidence": {"resolution": res_ev, "completeness": comp_ev, "tone": tone_ev},
            "judge": self.name,
        }


# ---------------------------------------------------------------------------
# LLM 判官（可选后端）
# ---------------------------------------------------------------------------
LLM_SYSTEM_PROMPT = """你是一名客服质检专家。请严格按给定的 1-5 分锚点给自动回复打分，只输出 JSON。
不要因为"回复很有礼貌"而给高分，也不要因为"回复很长"而给高分。
重点判断：用户看完这条回复，下一步的负担有没有降低。"""

LLM_USER_TEMPLATE = """# 用户问题
{question}

# 人工参考答案
{ref}

# 人工标注者对该 case 的分析（仅用于理解场景，不要照抄结论）
{notes}

# 待评估的自动回复
{reply}

# 必须做到（该场景的最低要求）
{minimum}

# 打分锚点
解决力（用户下一步的负担是否降低）：
5=给出可执行解法并主动承接后续动作；4=给出明确可执行路径由用户自行完成；
3=停在通用说明层面；2=把问题推回给用户；1=答非所问。

推进完备性（该追问时有没有追问）：
该场景需要补充信息才能推进：{mode}；需要追问的信息类型：{info_need}

语气与共情（情绪场景下有没有先接住情绪）：
用户是否需要安抚：{needs_comfort}
5=先安抚并给具体方案；4=有礼貌且有歉意/理解；3=公事公办；2=冷漠或把内部事项当答复；1=指责用户/过度承诺。

只输出如下 JSON（不要有其它文字）：
{{"resolution": <1-5>, "completeness": <1-5>, "tone": <1-5>,
  "resolution_reason": "<20字内>", "completeness_reason": "<20字内>", "tone_reason": "<20字内>"}}"""


class LLMJudge(MockJudge):
    """调用 OpenAI 兼容接口的判官。

    只替换三个主观指标的取分方式；事实类指标沿用确定性校验。
    环境变量：OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL
    """

    name = "llm-openai-compatible"

    def __init__(self, model=None, base_url=None, api_key=None, timeout=60):
        super().__init__()
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.base_url = (base_url or os.environ.get(
            "OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.timeout = timeout
        if not self.api_key:
            raise RuntimeError("未设置 OPENAI_API_KEY；可改用 --mode mock")

    def _call(self, prompt):
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": LLM_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return json.loads(body["choices"][0]["message"]["content"])

    def judge(self, case, intent, human_ref=None):
        facts = self.judge_facts(case, intent)
        intent = intent or {}
        ref = (human_ref or {}).get("human_reference", "（无）")
        notes = (human_ref or {}).get("annotator_notes", "（无）")
        prompt = LLM_USER_TEMPLATE.format(
            question=case["user_question"], ref=ref, notes=notes,
            reply=case["auto_reply"],
            minimum="；".join(intent.get("min_required", ["（无）"])),
            mode="是" if intent.get("needs_info") else
                 "需要针对具体对象作答" if intent.get("needs_instance_answer") else "否",
            info_need="订单号/账号/具体商品信息" if intent.get("needs_info") else "无",
            needs_comfort="是" if (F.user_needs_comfort(case["user_question"])
                                 or intent.get("severity") == "high") else "否",
        )
        out = self._call(prompt)
        res, res_ev = self.judge_resolution(case, intent)
        comp, comp_ev = self.judge_completeness(case, intent)
        tone, tone_ev = self.judge_tone(case, intent)
        res_ev["llm_reason"] = out.get("resolution_reason", "")
        comp_ev["llm_reason"] = out.get("completeness_reason", "")
        tone_ev["llm_reason"] = out.get("tone_reason", "")
        return {
            "case_id": case["id"],
            "intent": intent.get("label", "未分类"),
            "facts": facts,
            "resolution": float(out.get("resolution", res)),
            "completeness": float(out.get("completeness", comp)),
            "tone": float(out.get("tone", tone)),
            "evidence": {
                "resolution": res_ev, "completeness": comp_ev, "tone": tone_ev,
                "rule_baseline": {"resolution": res, "completeness": comp, "tone": tone},
            },
            "judge": self.name,
        }


def build_judge(mode="mock", **kwargs):
    if mode == "llm":
        return LLMJudge(**kwargs)
    return MockJudge()
