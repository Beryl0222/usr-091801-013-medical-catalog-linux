"""演示与测试用种子数据。

* 国家目录两版：NAT-2025（2025 全年）与 NAT-2026（2026-01-01 起），
  2026 版包含改名、降价与编码拆分（呼吸类一拆二）。
* 省级目录：湖北 HB、北京 BJ，各有 2025/2026 两版；湖北另有一版未发布草稿。
* 映射：一对一、一对多（省粒度更细的心电）、多对一（省打包的肺功能），
  2026 年映射刻意不完整，用于演示覆盖率缺口与人工队列。
* 提案：已发布、业务已批财务未审、财务拒绝、待生效（2027 年）四种状态。
* 历史脱敏账单：跨 2025/2026、含跨省异地就医与各类未决情形。
"""

from __future__ import annotations

from catalog_domain import CatalogGovernanceService

# ---------------------------------------------------------------------------
# 国家目录
# ---------------------------------------------------------------------------

NATIONAL_2025 = [
    {"code": "N1001", "name": "普通门诊诊查费", "category": "综合医疗服务", "unit": "次", "price": "20.00"},
    {"code": "N2001", "name": "X线计算机体层成像(CT平扫)", "category": "诊断", "unit": "部位", "price": "180.00"},
    {"code": "N3001", "name": "静脉输液", "category": "治疗", "unit": "次", "price": "12.00",
     "payment_limit": "限住院或急诊留观"},
    {"code": "N4001", "name": "运动疗法", "category": "康复", "unit": "次", "price": "45.00"},
    {"code": "N5001", "name": "重症监护", "category": "护理", "unit": "小时", "price": "120.00"},
    {"code": "N6001", "name": "全身麻醉", "category": "麻醉", "unit": "次", "price": "600.00"},
    {"code": "N7001", "name": "常规心电图检查", "category": "心血管", "unit": "次", "price": "30.00"},
    {"code": "N8001", "name": "肺功能检查", "category": "呼吸", "unit": "次", "price": "80.00"},
    {"code": "N9001", "name": "眼压检查", "category": "眼科", "unit": "次", "price": "15.00"},
    {"code": "NA001", "name": "妇科检查", "category": "妇科", "unit": "次", "price": "25.00"},
    {"code": "NB001", "name": "普通针刺", "category": "中医", "unit": "次", "price": "30.00"},
    {"code": "NC001", "name": "术中冰冻切片检查", "category": "病理检查", "unit": "次", "price": "220.00"},
    {"code": "ND001", "name": "彩色多普勒超声", "category": "医技检查", "unit": "次", "price": "120.00"},
]

# 2026 版：心电改名+调价、CT/输液降价、眼科调价，呼吸类 N8001 拆为 N8011+N8012。
NATIONAL_2026 = [
    {"code": "N1001", "name": "普通门诊诊查费", "category": "综合医疗服务", "unit": "次", "price": "22.00"},
    {"code": "N2001", "name": "X线计算机体层成像(CT平扫)", "category": "诊断", "unit": "部位", "price": "160.00"},
    {"code": "N3001", "name": "静脉输液", "category": "治疗", "unit": "次", "price": "10.00",
     "payment_limit": "限住院或急诊留观"},
    {"code": "N4001", "name": "运动疗法", "category": "康复", "unit": "次", "price": "45.00"},
    {"code": "N5001", "name": "重症监护", "category": "护理", "unit": "小时", "price": "120.00"},
    {"code": "N6001", "name": "全身麻醉", "category": "麻醉", "unit": "次", "price": "600.00"},
    {"code": "N7001", "name": "常规十二导联心电图检查", "category": "心血管", "unit": "次", "price": "35.00"},
    {"code": "N8011", "name": "肺容量检查", "category": "呼吸", "unit": "次", "price": "45.00"},
    {"code": "N8012", "name": "肺通气功能检查", "category": "呼吸", "unit": "次", "price": "50.00"},
    {"code": "N9001", "name": "眼压检查", "category": "眼科", "unit": "次", "price": "18.00"},
    {"code": "NA001", "name": "妇科检查", "category": "妇科", "unit": "次", "price": "25.00"},
    {"code": "NB001", "name": "普通针刺", "category": "中医", "unit": "次", "price": "30.00"},
    {"code": "NC001", "name": "术中冰冻切片检查", "category": "病理检查", "unit": "次", "price": "220.00"},
    {"code": "ND001", "name": "彩色多普勒超声", "category": "医技检查", "unit": "次", "price": "120.00"},
]

# ---------------------------------------------------------------------------
# 省级目录
# ---------------------------------------------------------------------------

HB_2025 = [
    {"code": "HB0101", "name": "门诊挂号诊查费", "category": "综合医疗服务", "unit": "次", "price": "22.00"},
    {"code": "HB0201", "name": "多层螺旋CT平扫", "category": "诊断", "unit": "部位", "price": "175.00"},
    {"code": "HB0301", "name": "静脉输液(门诊)", "category": "治疗", "unit": "次", "price": "11.00"},
    {"code": "HB0401", "name": "运动康复训练", "category": "康复", "unit": "次", "price": "42.00"},
    {"code": "HB0501", "name": "ICU重症监护", "category": "护理", "unit": "小时", "price": "110.00"},
    {"code": "HB0601", "name": "全身麻醉", "category": "麻醉", "unit": "次", "price": "580.00"},
    {"code": "HB0701", "name": "常规心电图", "category": "心血管", "unit": "次", "price": "28.00"},
    {"code": "HB0702", "name": "床旁心电图加收", "category": "心血管", "unit": "次", "price": "8.00"},
    {"code": "HB0801", "name": "肺功能检查", "category": "呼吸", "unit": "次", "price": "75.00"},
    {"code": "HB0901", "name": "非接触眼压测定", "category": "眼科", "unit": "次", "price": "12.00"},
    {"code": "HBA001", "name": "妇科常规检查", "category": "妇科", "unit": "次", "price": "22.00"},
    {"code": "HBB001", "name": "针灸(体针)", "category": "中医", "unit": "次", "price": "26.00"},
    {"code": "HBC001", "name": "术中冰冻病理检查", "category": "病理检查", "unit": "次", "price": "200.00"},
    {"code": "HBD001", "name": "彩色多普勒超声常规", "category": "医技检查", "unit": "次", "price": "115.00"},
    # 省级增补项目，长期没有国家对应，用于制造未决队列。
    {"code": "HB0999", "name": "经皮穴位电刺激(省增补)", "category": "中医", "unit": "次", "price": "50.00",
     "payment_limit": "省限中医适宜技术"},
]

HB_2026 = [
    {"code": "HB0101", "name": "门诊挂号诊查费", "category": "综合医疗服务", "unit": "次", "price": "22.00"},
    {"code": "HB0201", "name": "多层螺旋CT平扫", "category": "诊断", "unit": "部位", "price": "160.00"},
    {"code": "HB0301", "name": "静脉输液(门诊)", "category": "治疗", "unit": "次", "price": "10.00"},
    {"code": "HB0401", "name": "运动康复训练", "category": "康复", "unit": "次", "price": "45.00"},
    {"code": "HB0501", "name": "ICU重症监护", "category": "护理", "unit": "小时", "price": "120.00"},
    {"code": "HB0601", "name": "全身麻醉", "category": "麻醉", "unit": "次", "price": "600.00"},
    {"code": "HB0701", "name": "常规十二导联心电图", "category": "心血管", "unit": "次", "price": "35.00"},
    {"code": "HB0702", "name": "床旁心电图加收", "category": "心血管", "unit": "次", "price": "10.00"},
    {"code": "HB0801", "name": "肺通气+容量联合检查", "category": "呼吸", "unit": "次", "price": "95.00"},
    {"code": "HB0901", "name": "非接触眼压测定", "category": "眼科", "unit": "次", "price": "18.00"},
    {"code": "HBA001", "name": "妇科常规检查", "category": "妇科", "unit": "次", "price": "25.00"},
    {"code": "HBB001", "name": "针灸(体针)", "category": "中医", "unit": "次", "price": "30.00"},
    {"code": "HBC001", "name": "术中冰冻病理检查", "category": "病理检查", "unit": "次", "price": "220.00"},
    {"code": "HBD001", "name": "彩色多普勒超声常规", "category": "医技检查", "unit": "次", "price": "120.00"},
    {"code": "HB0999", "name": "经皮穴位电刺激(省增补)", "category": "中医", "unit": "次", "price": "50.00",
     "payment_limit": "省限中医适宜技术"},
]

BJ_2025 = [
    {"code": "BJ0101", "name": "门诊诊查费", "category": "综合医疗服务", "unit": "次", "price": "25.00"},
    {"code": "BJ0201", "name": "CT平扫", "category": "诊断", "unit": "部位", "price": "185.00"},
    {"code": "BJ0701", "name": "心电图检查", "category": "心血管", "unit": "次", "price": "32.00"},
    {"code": "BJ0801", "name": "肺通气+容量联合检查", "category": "呼吸", "unit": "次", "price": "85.00"},
]

BJ_2026 = [
    {"code": "BJ0101", "name": "门诊诊查费", "category": "综合医疗服务", "unit": "次", "price": "25.00"},
    {"code": "BJ0201", "name": "CT平扫", "category": "诊断", "unit": "部位", "price": "160.00"},
    {"code": "BJ0701", "name": "十二导联心电图检查", "category": "心血管", "unit": "次", "price": "35.00"},
    {"code": "BJ0801", "name": "肺通气+容量联合检查", "category": "呼吸", "unit": "次", "price": "95.00"},
]


def _links(pairs: list[tuple[str, str]], note: str = "") -> dict:
    return {
        "links": [{"national_code": n, "provincial_code": p} for n, p in pairs],
        "note": note,
    }


# ---------------------------------------------------------------------------
# 历史脱敏账单（影子回放与跑批共用同一请求格式）
# ---------------------------------------------------------------------------

HISTORICAL_BILLS: list[dict] = [
    {  # 1:1 已覆盖，2025 国家价 30，医院实收 35
        "claim_id": "CLM-2025-0315-001", "insured_region": "HB", "care_region": "HB",
        "service_date": "2025-03-15",
        "lines": [{"line_id": "L1", "provincial_code": "HB0701", "charged_amount": "35.00"}],
    },
    {  # 一对多：床旁加收仍归入国家心电项目
        "claim_id": "CLM-2025-0420-002", "insured_region": "HB", "care_region": "HB",
        "service_date": "2025-04-20",
        "lines": [
            {"line_id": "L1", "provincial_code": "HB0701", "charged_amount": "28.00"},
            {"line_id": "L2", "provincial_code": "HB0702", "charged_amount": "8.00"},
        ],
    },
    {  # CT，2025 国家价 180
        "claim_id": "CLM-2025-0511-003", "insured_region": "HB", "care_region": "HB",
        "service_date": "2025-05-11",
        "lines": [{"line_id": "L1", "provincial_code": "HB0201", "charged_amount": "200.00"}],
    },
    {  # 省增补无国家对应 → 未映射，入人工队列
        "claim_id": "CLM-2025-0602-004", "insured_region": "HB", "care_region": "HB",
        "service_date": "2025-06-02",
        "lines": [{"line_id": "L1", "provincial_code": "HB0999", "charged_amount": "55.00"}],
    },
    {  # 一单两行：输液 + ICU 护理
        "claim_id": "CLM-2025-0718-005", "insured_region": "HB", "care_region": "HB",
        "service_date": "2025-07-18",
        "lines": [
            {"line_id": "L1", "provincial_code": "HB0301", "charged_amount": "15.00"},
            {"line_id": "L2", "provincial_code": "HB0501", "quantity": 2, "charged_amount": "300.00"},
        ],
    },
    {  # 跨省异地就医：湖北参保，北京就医
        "claim_id": "CLM-2025-0909-006", "insured_region": "HB", "care_region": "BJ",
        "service_date": "2025-09-09",
        "lines": [{"line_id": "L1", "provincial_code": "BJ0701", "charged_amount": "40.00"}],
    },
    {  # 2025 版肺功能还是 1:1
        "claim_id": "CLM-2025-1120-007", "insured_region": "HB", "care_region": "HB",
        "service_date": "2025-11-20",
        "lines": [{"line_id": "L1", "provincial_code": "HB0801", "charged_amount": "90.00"}],
    },
    {  # 北京 CT，年末
        "claim_id": "CLM-2025-1228-008", "insured_region": "BJ", "care_region": "BJ",
        "service_date": "2025-12-28",
        "lines": [{"line_id": "L1", "provincial_code": "BJ0201", "charged_amount": "190.00"}],
    },
    {  # 换版后 CT 降到 160
        "claim_id": "CLM-2026-0108-009", "insured_region": "HB", "care_region": "HB",
        "service_date": "2026-01-08",
        "lines": [{"line_id": "L1", "provincial_code": "HB0201", "charged_amount": "190.00"}],
    },
    {  # 多对一：省一项打包国家两项，基准 45+50=95
        "claim_id": "CLM-2026-0215-010", "insured_region": "HB", "care_region": "HB",
        "service_date": "2026-02-15",
        "lines": [{"line_id": "L1", "provincial_code": "HB0801", "charged_amount": "90.00"}],
    },
    {  # 北京 2026 映射尚未发布 → 映射版本缺口
        "claim_id": "CLM-2026-0330-011", "insured_region": "HB", "care_region": "BJ",
        "service_date": "2026-03-30",
        "lines": [{"line_id": "L1", "provincial_code": "BJ0701", "charged_amount": "40.00"}],
    },
    {  # 湖北 2026 映射未覆盖康复类 → 编码缺口
        "claim_id": "CLM-2026-0412-012", "insured_region": "HB", "care_region": "HB",
        "service_date": "2026-04-12",
        "lines": [{"line_id": "L1", "provincial_code": "HB0401", "charged_amount": "60.00"}],
    },
    {  # 心电新价 35，数量 2
        "claim_id": "CLM-2026-0707-013", "insured_region": "HB", "care_region": "HB",
        "service_date": "2026-07-07",
        "lines": [{"line_id": "L1", "provincial_code": "HB0701", "quantity": 2, "charged_amount": "80.00"}],
    },
    {  # 输液新价 10
        "claim_id": "CLM-2026-0819-014", "insured_region": "HB", "care_region": "HB",
        "service_date": "2026-08-19",
        "lines": [{"line_id": "L1", "provincial_code": "HB0301", "charged_amount": "12.00"}],
    },
]


# ---------------------------------------------------------------------------
# 装载
# ---------------------------------------------------------------------------


def build_seeded_service() -> CatalogGovernanceService:
    """构造带完整治理样例的服务。"""
    svc = CatalogGovernanceService()

    # --- 国家目录两版 --------------------------------------------------
    svc.create_catalog_version(
        level="national", region=None, version="NAT-2025",
        effective_from="2025-01-01", effective_to="2025-12-31",
        projects=NATIONAL_2025,
    )
    svc.publish_catalog_version("NATIONAL", "NAT-2025")
    svc.create_catalog_version(
        level="national", region=None, version="NAT-2026",
        effective_from="2026-01-01", effective_to=None,
        projects=NATIONAL_2026,
    )
    svc.publish_catalog_version("NATIONAL", "NAT-2026")

    # --- 省级目录 ------------------------------------------------------
    svc.create_catalog_version(
        level="provincial", region="HB", version="HB-2025",
        effective_from="2025-01-01", effective_to="2025-12-31",
        projects=HB_2025,
    )
    svc.publish_catalog_version("PROV-HB", "HB-2025")
    svc.create_catalog_version(
        level="provincial", region="HB", version="HB-2026",
        effective_from="2026-01-01", effective_to="2026-12-31",
        projects=HB_2026,
    )
    svc.publish_catalog_version("PROV-HB", "HB-2026")
    svc.create_catalog_version(
        level="provincial", region="BJ", version="BJ-2025",
        effective_from="2025-01-01", effective_to="2025-12-31",
        projects=BJ_2025,
    )
    svc.publish_catalog_version("PROV-BJ", "BJ-2025")
    svc.create_catalog_version(
        level="provincial", region="BJ", version="BJ-2026",
        effective_from="2026-01-01", effective_to=None,
        projects=BJ_2026,
    )
    svc.publish_catalog_version("PROV-BJ", "BJ-2026")
    # 未发布草稿：经办人员可继续编辑，不能参与结算。
    svc.create_catalog_version(
        level="provincial", region="HB", version="HB-2027-DRAFT",
        effective_from="2027-01-01", effective_to=None,
        projects=HB_2026,
    )

    # --- 湖北 2025 映射（1:1 为主，心电为 1:N，省增补不映射） ----------
    p_hb25 = svc.submit_proposal({
        "proposer": "湖北医保专家组",
        "title": "湖北省2025年国家项目衔接建议",
        "national_version": "NAT-2025", "region": "HB",
        "intended_from": "2025-01-01", "intended_to": "2025-12-31",
        "evidence": [
            "《国家医保信息业务编码标准》2024版",
            "湖北省医疗保障局鄂医保发〔2024〕18号价格项目对照表",
        ],
        "groups": [
            _links([("N1001", "HB0101")]),
            _links([("N2001", "HB0201")]),
            _links([("N3001", "HB0301")]),
            _links([("N4001", "HB0401")]),
            _links([("N5001", "HB0501")]),
            _links([("N6001", "HB0601")]),
            _links([("N7001", "HB0701"), ("N7001", "HB0702")],
                   "省心电拆为常规与床旁加收，内涵一致"),
            _links([("N8001", "HB0801")]),
            _links([("N9001", "HB0901")]),
            _links([("NA001", "HBA001")]),
            _links([("NB001", "HBB001")]),
            _links([("NC001", "HBC001")]),
            _links([("ND001", "HBD001")]),
        ],
    })
    svc.review_proposal(p_hb25["proposal_id"], kind="business", decision="approved",
                        reviewer="业务审核-周敏", comment="内涵与适用范围核对一致")
    svc.review_proposal(p_hb25["proposal_id"], kind="finance", decision="approved",
                        reviewer="财务审核--吴芳", comment="当量与支付比例确认")
    svc.publish_mapping(p_hb25["proposal_id"], version="2025-1",
                        effective_from="2025-01-01", effective_to="2025-12-31")

    # --- 湖北 2026 映射（呼吸变 N:1，刻意只覆盖部分类别） --------------
    p_hb26 = svc.submit_proposal({
        "proposer": "湖北医保专家组",
        "title": "湖北省2026年首批衔接建议(心血管/呼吸/诊断/治疗/眼科)",
        "national_version": "NAT-2026", "region": "HB",
        "intended_from": "2026-01-01", "intended_to": "2026-12-31",
        "evidence": [
            "国家医保局2026年版医疗服务项目目录通告",
            "湖北省2026年价格项目衔接听证会纪要",
        ],
        "groups": [
            _links([("N2001", "HB0201")]),
            _links([("N3001", "HB0301")]),
            _links([("N7001", "HB0701")]),
            _links([("N8011", "HB0801"), ("N8012", "HB0801")],
                   "省联合检查打包国家容量+通气两项，按各50%当量结算"),
            _links([("N9001", "HB0901")]),
        ],
    })
    svc.review_proposal(p_hb26["proposal_id"], kind="business", decision="approved",
                        reviewer="业务审核-周敏", comment="打包内涵已与临床学会确认")
    svc.review_proposal(p_hb26["proposal_id"], kind="finance", decision="approved",
                        reviewer="财务审核-吴芳", comment="打包当量按50%+50%确认")
    svc.publish_mapping(p_hb26["proposal_id"], version="2026-1",
                        effective_from="2026-01-01", effective_to="2026-12-31")

    # --- 北京 2025 映射；2026 暂不发布，制造跨省缺口 -------------------
    p_bj25 = svc.submit_proposal({
        "proposer": "北京市医保局价格组",
        "title": "北京市2025年跨省结算首批映射",
        "national_version": "NAT-2025", "region": "BJ",
        "intended_from": "2025-01-01", "intended_to": "2025-12-31",
        "evidence": ["京医保发〔2024〕27号附件2对照表"],
        "groups": [
            _links([("N2001", "BJ0201")]),
            _links([("N7001", "BJ0701")]),
            _links([("N8001", "BJ0801")]),
        ],
    })
    svc.review_proposal(p_bj25["proposal_id"], kind="business", decision="approved",
                        reviewer="业务审核-郑凯")
    svc.review_proposal(p_bj25["proposal_id"], kind="finance", decision="approved",
                        reviewer="财务审核-孙丽")
    svc.publish_mapping(p_bj25["proposal_id"], version="2025-1",
                        effective_from="2025-01-01", effective_to="2025-12-31")

    # --- 在审提案：业务已批，财务未审（康复/护理等 2026 补缺） ---------
    svc.submit_proposal({
        "proposer": "湖北医保专家组",
        "title": "湖北省2026年第二批衔接建议(康复/护理/麻醉等待财务审核)",
        "national_version": "NAT-2026", "region": "HB",
        "intended_from": "2026-01-01", "intended_to": "2026-12-31",
        "evidence": [
            "省康复医学会《运动疗法项目内涵论证意见》",
            "省内三家三甲医院2025年成本测算表",
        ],
        "groups": [
            _links([("N4001", "HB0401")]),
            _links([("N5001", "HB0501")]),
            _links([("N6001", "HB0601")]),
        ],
    })
    pending = svc.list_proposals(status="submitted")
    svc.review_proposal(pending[0]["proposal_id"], kind="business", decision="approved",
                        reviewer="业务审核-周敏", comment="内涵一致，同意")

    # --- 被拒提案：把省增补项目错误挂到重症监护 ------------------------
    p_bad = svc.submit_proposal({
        "proposer": "某地市经办建议",
        "title": "省增补经皮穴位电刺激挂接建议",
        "national_version": "NAT-2026", "region": "HB",
        "intended_from": "2026-01-01", "intended_to": "2026-12-31",
        "evidence": ["经办人电话记录(无正式文件)"],
        "groups": [_links([("N5001", "HB0999")], "试图挂到重症监护")],
    })
    svc.review_proposal(p_bad["proposal_id"], kind="business", decision="approved",
                        reviewer="业务审核-周敏", comment="先转财务评估")
    svc.review_proposal(p_bad["proposal_id"], kind="finance", decision="rejected",
                        reviewer="财务审核-吴芳",
                        comment="中医适宜技术与重症监护内涵完全不符，拒绝挂接")

    # --- 待生效映射版本（2027-01-01 起，已发布可撤回） -----------------
    p_future = svc.submit_proposal({
        "proposer": "湖北医保专家组",
        "title": "湖北省2027年预发布衔接建议",
        "national_version": "NAT-2026", "region": "HB",
        "intended_from": "2027-01-01", "intended_to": None,
        "evidence": ["2027年目录衔接预备会议纪要"],
        "groups": [
            _links([("N7001", "HB0701")]),
            _links([("N4001", "HB0401")]),
            _links([("N5001", "HB0501")]),
        ],
    })
    svc.review_proposal(p_future["proposal_id"], kind="business", decision="approved",
                        reviewer="业务审核-周敏")
    svc.review_proposal(p_future["proposal_id"], kind="finance", decision="approved",
                        reviewer="财务审核-吴芳")
    svc.publish_mapping(p_future["proposal_id"], version="2027-PENDING",
                        effective_from="2027-01-01", effective_to=None)

    return svc
