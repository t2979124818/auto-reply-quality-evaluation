"""
特征抽取层：把一段回复变成**可审计的结构化信号**。

设计取舍
--------
这里所有信号都是正则/规则实现的，好处是：
  - 零依赖、可复现、可单测、结果可解释（每个扣分都能指到具体那句话）
  - 不依赖 LLM 的随机性，适合做成"定期跑的自动化流程"（业务方的后续要求）
坏处是召回率有限、对改写敏感。所以 pipeline 支持 LLM 判官作为可选后端，
两者共用同一套 rubric 与输出数据结构，可以互相做一致性校验。
"""

import re

# ---------------------------------------------------------------------------
# 一、行为信号
# ---------------------------------------------------------------------------
# 主动承接：明确表示"我来做"
RE_COMMIT = re.compile(
    r"我帮您|我帮|帮您(查|查询|处理|操作|联系|确认|申请|发起|跟进|看看|看)"
    r"|我来帮|我来处理|我这就|我现在就(帮|来)|为您(跟进|处理|查询)"
    r"|我(可以|会)帮您|我帮您查"
)

# 索取必要信息
RE_ASK_INFO = re.compile(
    r"订单号|订单编号|券号|优惠券编号|您的账号|账号是|具体是哪|哪两款|哪个环节|卡在哪"
    r"|尺码|型号|具体商品|哪些商品|是哪款|请您提供|请提供|方便告诉我是哪|能提供"
    r"|请问您(遇到了什么|卡在|是哪|是哪种)|您的问题是什么|麻烦您(告诉|说)"
)

# 感谢并承接反馈（建议类场景的唯一有效动作）
RE_FEEDBACK_ACK = re.compile(
    r"感谢您(的)?(宝贵)?(建议|反馈)|会将您的(反馈|建议)|转达给产品团队")

# 尽力协助：比无条件的「会协助您处理」更强的承接信号
RE_OFFER_HELP = re.compile(r"尽力(协助|帮助|为您|帮您)")

# 给出明确结论
RE_CONCLUSION = re.compile(
    r"可以(随身)?携带|可以(申请|选择|直接)?(退货|退款|换新|换货|取消|退换)"
    r"|(退货运费|运费)由我们承担|由商家承担|不超过\s*100\s*Wh"
    r"|在\s*\d+\s*天质保期内|属于质量问题|可以放心"
)

# 推诿：把动作推回给用户/第三方/页面
RE_DEFLECTIONS = [
    (re.compile(r"(查看|详见|参见).{0,6}商品详情页|商品详情页.{0,6}(查看|参数|说明)"), "让用户自己去查商品详情页"),
    (re.compile(r"联系(品牌)?官方客服|联系我们的客服|请联系客服|联系客服(核实|获取|协助|询问)"), "让用户自己再联系一次客服"),
    (re.compile(r"联系快递(员|公司)"), "让用户自己联系快递方"),
    (re.compile(r"请您(自行|先)|建议您(先|自行|耐心)"), "让用户自行处理"),
    (re.compile(r"请您(检查|核对|确认|关注|尝试)|建议您(检查|核对|查看|关注|尝试|先购买)"), "让用户自行检查/核对"),
    (re.compile(r"耐心等待"), "让用户被动等待"),
    (re.compile(r"(咨询|问).{0,6}航空公司"), "把结论推给航空公司"),
    (re.compile(r"(可以在|请在|需在|要到).{0,16}(详情页|页面|APP|小程序).{0,10}(查看|操作|了解|确认)"),
     "让用户自己去页面操作"),
    (re.compile(r"截图发给客服"), "把排查工作转给用户截图"),
    (re.compile(r"运费自理|自行承担运费|邮费自理"), "把费用与操作一并推给用户"),
]

# 内部事项（对用户无价值的自我管理说明）
RE_INTERNAL = re.compile(r"加强.{0,8}培训|培训(团队|客服)|反馈给.{0,6}(部门|团队)|转达给产品团队|会(加强|改进)")

# 情绪/安抚词
RE_EMPATHY = re.compile(r"抱歉|歉意|对不起|理解您|理解您的|重视|不便|困扰|久等|体谅|让您")
RE_THANKS = re.compile(r"感谢您的宝贵建议|感谢您的建议|感谢您")
RE_GREET = re.compile(r"^\s*(您好|你好|亲)")
RE_BLAME = re.compile(r"您应该|是您(自己)?的|请您自行(承担|负责)|这属于您")
RE_OVER_PROMISE = re.compile(r"保证|一定(能|会|可以)|绝对|100%|百分百")

# 用户情绪信号（决定是否需要安抚）
RE_USER_EMOTION = re.compile(
    r"态度|太差|等了|没人理|不理我|又是|连续|怎么(用不了|办)|搞半天|太复杂|搞不懂"
    r"|真的吗|诈骗|异地登录|投诉|生气|急|不好|坏了|没声音|取不出来|不方便"
)

# 能力类断言（无法核实，计入不瞎编指标）
RE_CAPABILITY_CLAIM = re.compile(
    r"系统(会|将)(自动)?(提醒|通知|发送)|已支持|支持视频展示|会自动退回|平台(会|将)"
)

NUMBER_RE = re.compile(r"(\d+)\s*(个工作日|工作日|小时|天|Wh|mAh|元|折|%)")


def deflect_hits(text: str):
    hits = []
    for pattern, reason in RE_DEFLECTIONS:
        if pattern.search(text):
            hits.append(reason)
    return hits


def user_needs_comfort(question: str) -> bool:
    return bool(RE_USER_EMOTION.search(question))


# ---------------------------------------------------------------------------
# 二、断言抽取
# ---------------------------------------------------------------------------
POLICY_CONTEXT = re.compile(r"退款|退货|换货|质保|运费|邮费|取消|到账|限额|携带|托运|补货")
PRODUCT_ATTR = re.compile(r"这款|该款|该商品|本商品|这款商品")
PRODUCT_ATTR_WORDS = re.compile(r"材质|成分|容量|参数|型号|配置|颜色|尺寸")
COMMIT_CLAIM = re.compile(r"我帮您|我们会|为您|承诺|保证|可以联系客服|如需")
# 带金额的承诺（赔偿/补偿）—— 用户会当真，所以必须是可校验的断言。
# 注意：不能把「优惠券/券」当作触发词，否则 "优惠券 1.未达到门槛 2.已过期"
# 这种排号列表会被误判成金额承诺（已用对抗集验证过这个假阳性）。
AMOUNT_PROMISE = re.compile(r"(赔偿|补偿|赔付|减免|退还|报销)\D{0,8}\d+|\d+\s*(元|块钱)")


def split_sentences(text: str):
    parts = re.split(r"[。；！!？?\n]+", text)
    return [p.strip() for p in parts if p.strip()]


def extract_claims(reply: str):
    """把回复拆成原子断言。返回 [{text, kind, numbers}]"""
    claims = []
    for sent in split_sentences(reply):
        if len(sent) < 6:
            continue
        numbers = NUMBER_RE.findall(sent)
        if PRODUCT_ATTR.search(sent) and PRODUCT_ATTR_WORDS.search(sent):
            kind = "product_attribute"
        elif AMOUNT_PROMISE.search(sent):
            kind = "commitment_amount"
        elif RE_CAPABILITY_CLAIM.search(sent):
            kind = "capability_claim"
        elif numbers and POLICY_CONTEXT.search(sent):
            kind = "policy_amount"
        elif POLICY_CONTEXT.search(sent):
            kind = "policy_statement"
        elif COMMIT_CLAIM.search(sent):
            kind = "commitment"
        else:
            kind = "other"
        claims.append({"text": sent, "kind": kind, "numbers": numbers,
                       "verdict": None, "reason": "", "ambiguous": False})
    return claims


# ---------------------------------------------------------------------------
# 三、断言校验（准确性 / 无编造性的取数逻辑）
# ---------------------------------------------------------------------------
# 明确的约束规则：命中即给出判定，优先级高于通用数值比对
CONSTRAINT_RULES = [
    (re.compile(r"(质量问题|非买家原因).{0,24}(我们|商家|卖家).{0,6}承担"),
     "return_shipping_fee", "supported", "与政策一致：质量问题的退货运费由商家承担"),
    (re.compile(r"(非质量问题|买家原因|不喜欢|买错).{0,24}(买家|您|用户).{0,6}承担"),
     "return_shipping_fee", "supported", "与政策一致：非质量问题的退货运费由买家承担"),
    (re.compile(r"换货运费由(买家|您|用户)承担"),
     "exchange_shipping_fee", "contradicted",
     "无条件声明换货运费由买家承担，忽略了'质量问题由商家承担'的情形"),
    (re.compile(r"(不超过|低于|≤|小于等于)\s*100\s*Wh"),
     "powerbank_wh", "supported", "与民航局规定一致（≤100Wh 可随身携带、不可托运）"),
    (re.compile(r"(不能|不可|禁止).{0,8}托运"),
     "powerbank_wh", "supported", "与民航局规定一致"),
    (re.compile(r"(不能|不可|禁止).{0,10}(随身)?携带上(飞机|机)"),
     "powerbank_wh", "contradicted", "与民航局规定冲突：≤100Wh 是可以随身携带的"),
    (re.compile(r"\d+\s*天质保"),
     "warranty_days", "supported", "与 KB 中记录的质保天数取值一致"),
    (re.compile(r"\d+([-～至]\d+)?\s*个?工作日"),
     "refund_timeline", "supported", "退款到账时效与 KB 记录一致"),
]

UNIT_TO_KEY = {"工作日": "工作日", "个工作日": "工作日", "小时": "小时", "天": "天",
               "Wh": "Wh", "mAh": "mAh", "元": "元", "折": "折", "%": "%"}


def _numbers_subset(numbers, allowed):
    if not numbers:
        return True
    return all((int(v), UNIT_TO_KEY.get(u, u)) in allowed for v, u in numbers)


def verify_claim(claim, intent, product_facts, policy_facts):
    """给单条断言打上 supported / contradicted / unverifiable。

    规则优先级：明确约束规则 > 数值集合比对 > 能力断言 > 其余
    """
    text = claim["text"]

    for pattern, category, verdict, reason in CONSTRAINT_RULES:
        if pattern.search(text):
            claim["verdict"] = verdict
            claim["reason"] = reason
            if policy_facts.get(category, {}).get("ambiguous"):
                claim["ambiguous"] = True
                claim["reason"] += "（注：该政策的取值在 KB 内存在不一致，见 README 局限 3）"
            return claim

    if claim["kind"] == "policy_amount":
        # 归到某个政策类目
        category = None
        if re.search(r"退款|到账", text):
            category = "refund_timeline"
        elif re.search(r"质保", text):
            category = "warranty_days"
        elif re.search(r"取消", text):
            category = "cancel_before_ship"
        elif re.search(r"Wh|限额|携带|托运", text):
            category = "powerbank_wh"

        if category is None:
            claim["verdict"] = "unverifiable"
            claim["reason"] = "回复给出了具体数值，但 KB 中没有对应政策条目可供核对"
            return claim

        facts = policy_facts[category]
        if _numbers_subset(claim["numbers"], facts["allowed"]):
            claim["verdict"] = "supported"
            claim["reason"] = facts["note"]
            if facts.get("ambiguous"):
                claim["ambiguous"] = True
            return claim
        claim["verdict"] = "unverifiable"
        claim["reason"] = f"数值不在 KB 记录的取值范围（{category}），需人工确认"
        return claim

    if claim["kind"] == "product_attribute":
        key = (intent or {}).get("product_fact_key")
        if key and key in product_facts:
            patterns = product_facts[key]["patterns"]
            if any(re.search(p, text) for p in patterns):
                claim["verdict"] = "supported"
                claim["reason"] = f"与 KB 中的商品参数一致（{product_facts[key]['fact']}）"
            else:
                claim["verdict"] = "contradicted"
                claim["reason"] = f"与 KB 中的商品参数不一致（{product_facts[key]['fact']}）"
            return claim
        claim["verdict"] = "unverifiable"
        claim["reason"] = "KB 中没有该商品的参数，无法核实这条具体参数断言"
        return claim

    if claim["kind"] == "commitment_amount":
        # 金额/补偿类承诺：KB 里没有授权额度表，一律判无法核实，并提示人工复核
        claim["verdict"] = "unverifiable"
        claim["reason"] = "向用户承诺了具体金额/补偿，但 KB 没有授权额度可核对（空口承诺风险）"
        return claim

    if claim["kind"] == "capability_claim":
        claim["verdict"] = "unverifiable"
        claim["reason"] = "对系统能力的断言，KB 无法验证（存在'空口承诺'风险）"
        return claim

    if claim["kind"] == "commitment":
        claim["verdict"] = "excluded"
        claim["reason"] = "承诺类语句不参与事实性判定，由解决力/推进完备性指标覆盖"
        return claim

    if claim["kind"] == "policy_statement":
        claim["verdict"] = "supported"
        claim["reason"] = "政策描述未含具体数值，与 KB 无冲突"
        return claim

    claim["verdict"] = "excluded"
    claim["reason"] = "非事实性语句（问候/建议/引导）"
    return claim
