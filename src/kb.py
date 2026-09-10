"""
知识库（KB）：评估的"事实基准"。

为什么需要 KB
------------
"准确""不瞎编"这两个指标必须有一个可追溯的基准，否则只能靠模型自己背书，
而模型自评事实性已被证明不可靠。这里用三层基准：

1. POLICY_FACTS  —— 平台政策数值（退款时效、质保天数、运费归属、民航限额）
2. PRODUCT_FACTS —— 具体商品参数（由 human_ref 的参考答案反推的少量已知事实）
3. INTENTS       —— 每个 case 的"这个问题要解决好，最少必须做到什么"
                     （min_required / forbidden，来自人工标注笔记的结构化沉淀）

这层 KB 是**手写的**，这也是本方案最主要的局限之一（见 README）。

关于 benchmark 的诚实说明
------------------------
case_human_ref.json 的人工参考答案同时也被我们当作"gold"使用。
这意味着评估结果对人工标注者的一致性敏感，并且**不能**证明自动回复在
人工标注者没意识到的维度上也是错的。README 的"局限"一节会明确讨论。
"""

# ---------------------------------------------------------------------------
# 1. 政策事实基准
# ---------------------------------------------------------------------------
# allowed: 允许出现的数值集合（来自平台公开政策 / 人工参考答案）
# note   : 给报告用的解释
POLICY_FACTS = {
    "refund_timeline": {
        "allowed": {(1, "工作日"), (3, "工作日"), (5, "工作日"), (7, "工作日"), (15, "工作日")},
        "note": "原路退回 1-3 工作日、银行卡 3-7 工作日、信用卡 5-15 工作日（human_ref case_03）",
    },
    "warranty_days": {
        "allowed": {(7, "天"), (30, "天")},
        "note": (
            "⚠ 冲突：case_04 说'7天质保期'（人工认可），case_18 说'30天质保期'（人工认可）。"
            "两条回复给出了不同的质保天数，KB 无法判定哪个正确 —— 这是 KB 缺失导致的"
            "**无法消解的不确定性**，已作为评估局限记录。"
        ),
        "ambiguous": True,
    },
    "powerbank_wh": {
        "allowed": {(100, "Wh")},
        "note": "民航局：额定能量 ≤100Wh 可随身携带、不可托运（human_ref case_02）",
    },
    "return_shipping_fee": {
        "allowed": set(),
        "note": "质量问题→商家承担；非质量问题→买家承担（human_ref case_09）",
    },
    "exchange_shipping_fee": {
        "allowed": set(),
        "note": "换货运费归属需区分原因，不能无条件声明（human_ref case_09 的运费规则同样适用）",
    },
    "cancel_before_ship": {
        "allowed": {(1, "工作日"), (3, "工作日")},
        "note": "未发货可取消，退款 1-3 个工作日（human_ref case_10）",
    },
}

# 2. 具体商品参数（仅收录人工参考答案中已确认的）
PRODUCT_FACTS = {
    "phone_case": {"fact": "TPU 软胶材质", "patterns": [r"TPU", r"软胶"]},
}

# ---------------------------------------------------------------------------
# 3. case 意图库
# ---------------------------------------------------------------------------
# needs_info          : 必须补信息（订单号/账号）才能推进 → 影响 M4
# needs_instance_answer: 问的是"这个/我的"具体对象 → 影响 M4
# min_required        : 人工标注沉淀的"至少要做到"清单（用于人工对齐，M3 信号参考）
# forbidden_actions   : 明确的推诿/减分行为
# severity            : high 表示情绪/风险场景，语气指标会启用"是否安抚"的强要求
# risk                : 出错后的业务风险等级（用于报告排序，不计入分数）
INTENTS = [
    {
        "key": "logistics_wrong_locker",
        "label": "快递放错柜取不出",
        "pattern": r"快递.*(放错|错)|取不出来|柜",
        "needs_info": True,
        "needs_instance_answer": False,
        "severity": "normal",
        "risk": "L2",
        "min_required": ["由商家/客服联系快递公司重新派送或送上门", "向用户索取订单号以便代为处理"],
        "forbidden_actions": ["让用户自己去放错的柜子取", "让用户自己联系快递公司"],
    },
    {
        "key": "powerbank_airline",
        "label": "充电宝能否上飞机",
        "pattern": r"充电宝.*(飞机|上机)|飞机.*充电宝",
        "needs_info": False,
        "needs_instance_answer": True,
        "severity": "normal",
        "risk": "L2",
        "min_required": ["给出该商品的实际额定容量并给出明确结论"],
        "forbidden_actions": ["只给通用规定让用户自己去核对"],
        "product_fact_key": None,  # KB 里没有这件商品的实际容量 → 无法实例化
    },
    {
        "key": "refund_timeline",
        "label": "退款到账时间",
        "pattern": r"退款.*(到账|多久|时间)|什么时候.*退",
        "needs_info": False,
        "needs_instance_answer": False,
        "severity": "normal",
        "risk": "L1",
        "min_required": ["给出各退款渠道的到账时效"],
        "forbidden_actions": [],
    },
    {
        "key": "earphone_defect_3days",
        "label": "耳机三天就坏（质保期内）",
        "pattern": r"耳机.*(没声|坏|故障)|左耳",
        "needs_info": False,
        "needs_instance_answer": False,
        "severity": "normal",
        "risk": "L2",
        "min_required": ["直接给出退换方案（用户已说明在质保期内）"],
        "forbidden_actions": ["先让用户做一轮自助排查再给方案"],
        "delayed_remedy": True,
    },
    {
        "key": "cs_wait_complaint",
        "label": "客服等待久投诉",
        "pattern": r"客服.*(态度|没|不理|等)|等了.*分钟|没人理",
        "needs_info": False,
        "needs_instance_answer": False,
        "severity": "high",
        "risk": "L2",
        "min_required": ["立即承接用户原本的问题"],
        "forbidden_actions": ["把内部管理事项（培训/考核）作为对用户的答复"],
        "internal_focus_penalty": True,
    },
    {
        "key": "coupon_unusable",
        "label": "优惠券用不了",
        "pattern": r"优惠券|券.*(用不了|不能用|无法使用)",
        "needs_info": True,
        "needs_instance_answer": False,
        "severity": "normal",
        "risk": "L2",
        "min_required": ["索取券号/订单号，代为排查具体原因"],
        "forbidden_actions": ["只罗列可能原因让用户自查"],
    },
    {
        "key": "account_risky_login",
        "label": "异地登录疑似诈骗短信",
        "pattern": r"异地登录|账号.*(异地|被盗|异常)|诈骗",
        "needs_info": True,
        "needs_instance_answer": False,
        "severity": "high",
        "risk": "L3",
        "min_required": ["安抚情绪", "索取账号由客服代查登录记录", "提示不要点击短信链接"],
        "forbidden_actions": ["把'是不是真的'的判断责任推给用户"],
    },
    {
        "key": "phone_case_material",
        "label": "手机壳材质",
        "pattern": r"手机壳|壳.*(材质|硅胶|塑料)",
        "needs_info": False,
        "needs_instance_answer": True,
        "severity": "normal",
        "risk": "L1",
        "min_required": ["直接给出该商品的材质"],
        "forbidden_actions": ["让用户去详情页自己看"],
        "product_fact_key": "phone_case",
    },
    {
        "key": "return_shipping_fee",
        "label": "退货运费谁出",
        "pattern": r"退货.*(邮费|运费)|邮费谁出",
        "needs_info": False,
        "needs_instance_answer": False,
        "severity": "normal",
        "risk": "L1",
        "min_required": ["说明两种情形的运费归属", "追问用户属于哪种情形"],
        "forbidden_actions": [],
    },
    {
        "key": "cancel_before_ship",
        "label": "未发货能否取消",
        "pattern": r"取消|未发货|还没发货",
        "needs_info": False,
        "needs_instance_answer": False,
        "severity": "normal",
        "risk": "L1",
        "min_required": ["给出明确结论与操作路径"],
        "forbidden_actions": [],
    },
    {
        "key": "exchange_two_items",
        "label": "两件衣服换尺码",
        "pattern": r"换(尺码|货)|两件衣服",
        "needs_info": True,
        "needs_instance_answer": False,
        "severity": "normal",
        "risk": "L2",
        "min_required": ["追问具体商品与目标尺码，分别受理"],
        "forbidden_actions": ["在未确认原因的情况下声明换货运费由买家承担"],
        "policy_contradiction": (r"换货运费由买家承担", "exchange_shipping_fee",
                                 "无条件声明换货运费由买家承担，与'按原因归属'的政策不符"),
    },
    {
        "key": "logistics_stalled",
        "label": "物流两天没更新",
        "pattern": r"物流|快递.*(没更新|不动|停)|两天没",
        "needs_info": True,
        "needs_instance_answer": False,
        "severity": "normal",
        "risk": "L2",
        "min_required": ["索取订单号，代查实际物流状态"],
        "forbidden_actions": ["只罗列可能原因并让用户耐心等待"],
    },
    {
        "key": "mask_ingredients",
        "label": "面膜成分（敏感肌）",
        "pattern": r"面膜|成分",
        "needs_info": False,
        "needs_instance_answer": True,
        "severity": "high",
        "risk": "L3",
        "min_required": ["给出该商品成分信息", "对敏感肌给出谨慎建议，不做安全保证"],
        "forbidden_actions": ["让用户自己去详情页核对成分"],
    },
    {
        "key": "feature_request_video",
        "label": "建议增加实物视频",
        "pattern": r"建议|能不能加.*功能|反馈",
        "needs_info": False,
        "needs_instance_answer": False,
        "severity": "normal",
        "risk": "L1",
        "min_required": ["感谢并承诺转达", "主动承接用户的具体需求"],
        "forbidden_actions": [],
    },
    {
        "key": "repeated_defect",
        "label": "连续两次收到坏商品",
        "pattern": r"又是坏的|连续.*坏|上次.*坏",
        "needs_info": False,
        "needs_instance_answer": False,
        "severity": "high",
        "risk": "L3",
        "min_required": ["承认问题的严重性", "给出具体（有额度/有方案）的补偿"],
        "forbidden_actions": ["只说'可以申请额外补偿优惠券'这类模糊承诺"],
        "vague_compensation": True,
    },
    {
        "key": "compare_two_phones",
        "label": "两款手机哪个好",
        "pattern": r"两款|哪个好|对比|推荐.*手机",
        "needs_info": True,
        "needs_instance_answer": False,
        "severity": "normal",
        "risk": "L1",
        "min_required": ["追问使用场景与预算（已做到）", "主动代做对比，而非让用户自己看参数"],
        "forbidden_actions": ["把用户推去自己看评价/参数"],
    },
    {
        "key": "two_questions_mixed",
        "label": "一条消息问两件事",
        "pattern": r"顺便|两个问题|退货的事",
        "needs_info": True,
        "needs_instance_answer": False,
        "severity": "normal",
        "risk": "L2",
        "min_required": ["同时受理两个问题", "索取两个订单号"],
        "forbidden_actions": ["让用户自己分别去两个页面查"],
    },
    {
        "key": "vacuum_2weeks",
        "label": "扫地机器人两周就坏",
        "pattern": r"扫地机器人|机器人.*(不工作|坏)",
        "needs_info": False,
        "needs_instance_answer": False,
        "severity": "normal",
        "risk": "L2",
        "min_required": ["确认在质保期内并给出方案", "追问用户倾向退款还是换新"],
        "forbidden_actions": [],
    },
    {
        "key": "bag_restock",
        "label": "包什么时候补货",
        "pattern": r"补货|到货|有货吗",
        "needs_info": False,
        "needs_instance_answer": True,
        "severity": "normal",
        "risk": "L1",
        "min_required": ["给出该商品的补货计划"],
        "forbidden_actions": ["只给'关注页面/加购物车'的通用建议"],
    },
    {
        "key": "return_flow_confusing",
        "label": "退货流程看不懂",
        "pattern": r"退货流程|怎么操作|搞半天|太复杂|搞不懂",
        "needs_info": True,
        "needs_instance_answer": False,
        "severity": "high",
        "risk": "L2",
        "min_required": ["追问用户卡在哪一步，逐步指导"],
        "forbidden_actions": ["再完整复述一遍流程"],
        "misdirected_trigger": True,
    },
]

# 4. 全局合规红线（与 case 无关的通用禁止项）
GLOBAL_RED_LINES = [
    (r"点击短信(中的)?链接", "引导用户点击短信链接（钓鱼风险）"),
    (r"(保证|承诺|绝对|100%)\s*(不过敏|无风险|一定能)", "对健康/结果做绝对保证"),
]

# 5. 每个 case 的人工裁定（由 annotator_notes 归纳，用于 backtest）
#    这不是"标准答案"，而是用来验证自动评分与人工判断是否同向。
HUMAN_VERDICT = {
    "case_01": ("weak", "把责任推给了用户，没有体现主动服务意识"),
    "case_02": ("weak", "没有查具体商品，只泛泛讲了规定，需要用户自己去确认"),
    "case_03": ("acceptable", "通用信息本身是准确的，但没有帮用户查实际状态"),
    "case_04": ("weak", "先给了一堆排查步骤，没有直接给出解决方案，增加用户操作负担"),
    "case_05": ("weak", "没有立刻帮用户解决原始问题，而是说了内部事项（加强培训）"),
    "case_06": ("weak", "罗列了一堆可能原因但没有帮用户实际排查"),
    "case_07": ("weak", "把判断责任推给用户，且情绪安抚不够"),
    "case_08": ("weak", "正确但没用：用户就是因为不想翻详情页才来问的"),
    "case_09": ("acceptable", "规则说明正确，但没追问具体情况；作为自动回复给出规则本身也有价值"),
    "case_10": ("acceptable", "基本正确，给出操作路径和退款时间；作为自动回复可接受"),
    "case_11": ("weak", "直接给通用流程，没有追问具体商品信息来帮用户处理"),
    "case_12": ("weak", "罗列可能原因但没有帮用户查实际状态"),
    "case_13": ("weak", "用户明确说皮肤敏感，回复让用户自己看详情页，没有个性化关注"),
    "case_14": ("good", "建议类反馈处理得不错，表达了感谢并承诺反馈"),
    "case_15": ("acceptable", "提到了补偿，但语气和力度不够，补偿不够具体"),
    "case_16": ("acceptable", "追问使用场景和预算基本合理，但把用户推去自己看评价"),
    "case_17": ("weak", "没有主动帮用户查，而是让用户自己去查"),
    "case_18": ("acceptable", "基本正确，给出质保期和方案；但没确认用户倾向哪种方案"),
    "case_19": ("weak", "没有查具体商品，给了通用建议"),
    "case_20": ("weak", "答非所问：又重复了一遍用户说搞不懂的退货流程"),
}

VERDICT_ORDER = {"weak": 0, "acceptable": 1, "good": 2}


def match_intent(question: str):
    """按 INTENTS 顺序做正则匹配，返回第一个命中的意图。"""
    import re

    for intent in INTENTS:
        if re.search(intent["pattern"], question):
            return intent
    return None
