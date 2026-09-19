"""种子数据：首批国家目录、沪浙两省目录与现行映射基线。

数据均为虚构的脱敏样例，用于影子回放与测试。编码粒度刻意制造差异：
- 上海把“白内障摘除+人工晶体”“全麻+插管”“血/尿常规”按项目捆绑收费（N:1）；
- 浙江把国家“一级护理”“全身麻醉”拆成更细的子项目收费（1:N）；
- 两省各保留一个尚未与国家目录衔接的地方项目，用于覆盖率与人工队列验证。
"""

from __future__ import annotations

from catalog import (
    MANY_TO_ONE,
    NATIONAL_SCOPE,
    ONE_TO_MANY,
    ONE_TO_ONE,
    PROVINCE_SCOPE,
)

BASELINE_FROM = "2026-01-01"

# 国家目录：十三类，每类两项，含国家参照价
NATIONAL_ITEMS = [
    ("N-NK001", "普通门诊诊查费", "内科", "次", "25.00"),
    ("N-NK002", "住院诊查费", "内科", "次", "40.00"),
    ("N-WK001", "浅表肿物切除术", "外科", "次", "350.00"),
    ("N-WK002", "阑尾切除术", "外科", "次", "1200.00"),
    ("N-FC001", "顺产接生", "妇产科", "次", "1800.00"),
    ("N-FC002", "剖宫产术", "妇产科", "次", "3200.00"),
    ("N-EK001", "儿科住院诊查", "儿科", "日", "30.00"),
    ("N-EK002", "新生儿复苏", "儿科", "次", "500.00"),
    ("N-YK001", "白内障超声乳化摘除术", "眼科", "次", "2500.00"),
    ("N-YK002", "人工晶体置入术", "眼科", "次", "1200.00"),
    ("N-KQ001", "龋齿充填术", "口腔", "牙", "120.00"),
    ("N-KQ002", "复杂牙拔除术", "口腔", "牙", "90.00"),
    ("N-MZ001", "全身麻醉", "麻醉", "次", "800.00"),
    ("N-MZ002", "气管插管术", "麻醉", "次", "200.00"),
    ("N-HL001", "一级护理", "护理", "日", "35.00"),
    ("N-HL002", "重症监护", "护理", "小时", "15.00"),
    ("N-KF001", "运动疗法", "康复", "次", "60.00"),
    ("N-KF002", "作业疗法", "康复", "次", "50.00"),
    ("N-ZY001", "针灸治疗", "中医", "次", "40.00"),
    ("N-ZY002", "推拿治疗", "中医", "次", "55.00"),
    ("N-BL001", "常规病理检查", "病理", "例", "150.00"),
    ("N-BL002", "冰冻切片检查", "病理", "例", "300.00"),
    ("N-JY001", "血常规检查", "检验", "次", "20.00"),
    ("N-JY002", "尿常规检查", "检验", "次", "12.00"),
    ("N-YX001", "CT平扫", "影像", "部位", "220.00"),
    ("N-YX002", "磁共振平扫", "影像", "部位", "450.00"),
]

# 省级项目：(code, name, category, unit, local_price, payment_limit)
SHANGHAI_ITEMS = [
    ("SH-NK001", "普通门诊诊查费", "内科", "次", "24.00", "24.00"),
    ("SH-WK001", "浅表肿物切除手术费", "外科", "次", "360.00", "340.00"),
    ("SH-WK002", "阑尾切除术", "外科", "次", "1180.00", "1150.00"),
    ("SH-FC001", "阴道分娩接生", "妇产科", "次", "1750.00", "1700.00"),
    ("SH-EK001", "儿童住院诊查", "儿科", "日", "28.00", "28.00"),
    ("SH-YK001", "白内障摘除联合人工晶体置入术", "眼科", "次", "3400.00", "3300.00"),
    ("SH-KQ001", "龋齿充填", "口腔", "牙", "115.00", "110.00"),
    ("SH-MZ001", "全身麻醉（含气管插管）", "麻醉", "次", "950.00", "900.00"),
    ("SH-HL002", "重症监护费", "护理", "小时", "14.00", "14.00"),
    ("SH-KF001", "运动康复训练", "康复", "次", "58.00", "55.00"),
    ("SH-ZY001", "针灸", "中医", "次", "42.00", "40.00"),
    ("SH-BL001", "常规病理检验", "病理", "例", "160.00", "150.00"),
    ("SH-JY001", "血尿常规组合检查", "检验", "次", "30.00", "28.00"),
    ("SH-YX002", "磁共振成像平扫", "影像", "部位", "430.00", "430.00"),
    ("SH-TZ001", "沪上特色中医定向透药", "中医", "次", "66.00", "60.00"),
]

ZHEJIANG_ITEMS = [
    ("ZJ-NK001", "门诊一般诊疗费", "内科", "次", "26.00", "25.00"),
    ("ZJ-WK002", "阑尾切除手术", "外科", "次", "1220.00", "1180.00"),
    ("ZJ-FC001", "顺产接产", "妇产科", "次", "1780.00", "1760.00"),
    ("ZJ-FC002", "子宫下段剖宫产", "妇产科", "次", "3100.00", "3000.00"),
    ("ZJ-EK002", "新生儿窒息复苏", "儿科", "次", "480.00", "460.00"),
    ("ZJ-YK001", "白内障超声乳化术", "眼科", "次", "2300.00", "2300.00"),
    ("ZJ-YK002", "人工晶体置入", "眼科", "次", "1150.00", "1100.00"),
    ("ZJ-KQ002", "阻生牙拔除", "口腔", "牙", "88.00", "85.00"),
    ("ZJ-MZ001", "全身麻醉诱导", "麻醉", "次", "300.00", "290.00"),
    ("ZJ-MZ002", "全身麻醉维持", "麻醉", "次", "560.00", "520.00"),
    ("ZJ-MZ003", "气管内插管术", "麻醉", "次", "190.00", "180.00"),
    ("ZJ-HL001", "晨间分级护理", "护理", "日", "18.00", "18.00"),
    ("ZJ-HL002", "晚间分级护理", "护理", "日", "20.00", "19.00"),
    ("ZJ-HL003", "造口护理", "护理", "次", "45.00", "42.00"),
    ("ZJ-KF002", "作业治疗", "康复", "次", "48.00", "46.00"),
    ("ZJ-ZY002", "中医推拿", "中医", "次", "52.00", "50.00"),
    ("ZJ-BL002", "术中冰冻切片", "病理", "例", "290.00", "280.00"),
    ("ZJ-JY001", "全血细胞分析", "检验", "次", "19.00", "18.00"),
    ("ZJ-YX001", "X线计算机体层平扫", "影像", "部位", "210.00", "205.00"),
    ("ZJ-YX002", "MRI平扫", "影像", "部位", "440.00", "430.00"),
]

# 现行映射基线：(direction, members[(side,code)], weights)
SHANGHAI_MAPPINGS = [
    (ONE_TO_ONE, [("NATIONAL", "N-NK001"), ("PROVINCE", "SH-NK001")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-WK002"), ("PROVINCE", "SH-WK002")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-FC001"), ("PROVINCE", "SH-FC001")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-EK001"), ("PROVINCE", "SH-EK001")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-KQ001"), ("PROVINCE", "SH-KQ001")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-HL002"), ("PROVINCE", "SH-HL002")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-KF001"), ("PROVINCE", "SH-KF001")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-ZY001"), ("PROVINCE", "SH-ZY001")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-BL001"), ("PROVINCE", "SH-BL001")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-YX002"), ("PROVINCE", "SH-YX002")], None),
    # 上海捆绑收费：一条地方行对应多个国家项目
    (MANY_TO_ONE,
     [("NATIONAL", "N-YK001"), ("NATIONAL", "N-YK002"),
      ("PROVINCE", "SH-YK001")],
     ["0.70", "0.30"]),
    (MANY_TO_ONE,
     [("NATIONAL", "N-MZ001"), ("NATIONAL", "N-MZ002"),
      ("PROVINCE", "SH-MZ001")],
     ["0.80", "0.20"]),
    (MANY_TO_ONE,
     [("NATIONAL", "N-JY001"), ("NATIONAL", "N-JY002"),
      ("PROVINCE", "SH-JY001")],
     ["0.60", "0.40"]),
]

ZHEJIANG_MAPPINGS = [
    (ONE_TO_ONE, [("NATIONAL", "N-NK001"), ("PROVINCE", "ZJ-NK001")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-WK002"), ("PROVINCE", "ZJ-WK002")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-FC001"), ("PROVINCE", "ZJ-FC001")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-FC002"), ("PROVINCE", "ZJ-FC002")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-EK002"), ("PROVINCE", "ZJ-EK002")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-YK001"), ("PROVINCE", "ZJ-YK001")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-YK002"), ("PROVINCE", "ZJ-YK002")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-KQ002"), ("PROVINCE", "ZJ-KQ002")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-MZ002"), ("PROVINCE", "ZJ-MZ003")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-KF002"), ("PROVINCE", "ZJ-KF002")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-ZY002"), ("PROVINCE", "ZJ-ZY002")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-BL002"), ("PROVINCE", "ZJ-BL002")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-JY001"), ("PROVINCE", "ZJ-JY001")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-YX001"), ("PROVINCE", "ZJ-YX001")], None),
    (ONE_TO_ONE, [("NATIONAL", "N-YX002"), ("PROVINCE", "ZJ-YX002")], None),
    # 浙江拆细收费：一个国家项目对应多条地方子项
    (ONE_TO_MANY,
     [("NATIONAL", "N-HL001"),
      ("PROVINCE", "ZJ-HL001"), ("PROVINCE", "ZJ-HL002")],
     ["0.50", "0.50"]),
    (ONE_TO_MANY,
     [("NATIONAL", "N-MZ001"),
      ("PROVINCE", "ZJ-MZ001"), ("PROVINCE", "ZJ-MZ002")],
     ["0.40", "0.60"]),
]


def seed(registry, actor="system"):
    """向空注册中心写入国家目录、两省目录与现行映射基线。"""
    registry.create_catalog(NATIONAL_SCOPE, "首批国家医保医疗服务项目目录",
                            actor=actor)
    for code, name, category, unit, price in NATIONAL_ITEMS:
        registry.add_item("NATIONAL", code, name, category, unit=unit,
                          price=price, effective_from=BASELINE_FROM, actor=actor)

    registry.create_catalog(PROVINCE_SCOPE, "上海市医保医疗服务项目目录",
                            province="SH", actor=actor)
    for code, name, category, unit, price, limit in SHANGHAI_ITEMS:
        registry.add_item("PROV-SH", code, name, category, unit=unit,
                          price=price, payment_limit=limit,
                          effective_from=BASELINE_FROM, actor=actor)

    registry.create_catalog(PROVINCE_SCOPE, "浙江省医保医疗服务项目目录",
                            province="ZJ", actor=actor)
    for code, name, category, unit, price, limit in ZHEJIANG_ITEMS:
        registry.add_item("PROV-ZJ", code, name, category, unit=unit,
                          price=price, payment_limit=limit,
                          effective_from=BASELINE_FROM, actor=actor)

    for direction, members, weights in SHANGHAI_MAPPINGS:
        registry.bootstrap_active_mapping(
            "SH", direction, [{"side": s, "code": c} for s, c in members],
            BASELINE_FROM, weights=weights, actor=actor)
    for direction, members, weights in ZHEJIANG_MAPPINGS:
        registry.bootstrap_active_mapping(
            "ZJ", direction, [{"side": s, "code": c} for s, c in members],
            BASELINE_FROM, weights=weights, actor=actor)
    return registry
