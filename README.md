# 医保项目目录衔接 · 目录治理后端

首批国家医保医疗服务项目目录（十三类服务）先用于跨省异地就医结算，再逐步与各省现行项目衔接。
本服务保存国家项目、省级项目及一对多 / 多对一映射的**生效区间**，管理映射建议的
**双轨审核发布**，并按**就医发生时点**为每笔费用找到唯一适用版本；未决映射进入人工队列，
绝不猜测。新目录先以**影子模式**回放历史脱敏账单，正式生效后出错只能**冲正**修复。

零第三方依赖：Python 3.10+ 标准库（HTTP 服务、线程锁、Decimal 金额）。

## 治理原则如何落地

| 业务要求 | 实现方式 |
| --- | --- |
| 编码粒度 / 名称 / 支付限制不同，不能直接替换 | 国家与省级目录各自版本化；映射版本独立于目录版本，允许年度换版时重新衔接 |
| 历史费用按当时版本解释 | 结算只接受 `service_date`，在已发布且区间覆盖该日的版本中解析；相邻年度版本首尾衔接 |
| 一对多 / 多对一 | 映射以"组"表达：1:N 标记 `one_to_many`，N:1 标记 `many_to_one` 且 `bundle=true`，基准价为组内国家项目之和；多对多必须拆组 |
| 专家建议必须列依据和冲突项 | `evidence` 必填非空；系统自动列出基数当量、区间重叠 / 跨版本衔接等 `conflicts` |
| 业务 + 财务双审 | 提案状态机 `submitted → approved → published`，任一审核拒绝即终态 `rejected`，双审通过才允许发布 |
| 发布 / 撤回待生效版本 | 已发布版本区间不得重叠（跨国家版本年度衔接除外）；仅未到生效日的版本可撤回 |
| 未决映射入人工队列 | 区分四种原因：无国家版本 / 无省版本 / 无映射版本 / 编码未映射；人工裁决登记为可审计的 `MANUAL-*` 映射版本，且**绑定国家目录版本，换版后自动失效** |
| 影子模式 | 回放只产出覆盖率与支付差异报告，不写结算、不写队列；差异只在覆盖行口径上计算，缺口金额单列 |
| 生效后只能冲正 | 原结算记录不可变；冲正追加金额相反的更正记录，原单标记 `reversed`，禁止重复冲正 |
| 跨日跑批事务一致性 | 批量先在同一把锁内完成全部规划再统一落库；任一条失败整批回滚；并发跑批不丢单、不重单 |
| 结果必须可溯源 | 每行返回 `national_catalog` / `provincial_catalog` / `mapping` 三个版本号、解析依据（正式映射或人工裁决）及命中的国家项目明细 |

## 文件结构

```
service.py            HTTP 入口（ThreadingHTTPServer），含 /health 与 --check
catalog_domain.py     领域核心：版本、映射组、提案审核、时点解析、队列、影子、冲正、跑批
seed_data.py          十三类种子数据：国家两版、湖北/北京两版、各类提案、14 张历史账单
service_contract.py   基础健康检查契约测试（原有）
test_domain.py        领域规则测试（28 例）
test_http_api.py      HTTP 接口端到端契约测试
test_concurrency.py   并发跑批 / 发布撤回一致性测试
test_service.js       npm test 入口，运行全部 unittest
```

## 快速开始

```bash
python3 service.py --check                      # 启动自检（种子装载 + 影子回放）
python3 service.py --port 8000                  # 启动（默认带演示种子数据）
python3 service.py --port 8000 --no-seed        # 空库启动
npm test                                        # 45 项测试
```

种子数据概况：国家目录 `NAT-2025` / `NAT-2026`（2026 版心电改名调价、呼吸一拆二）；
湖北 `HB-2025/2026`、北京 `BJ-2025/2026` 省级目录；已发布映射含 1:1、1:N、N:1；
另有业务已批待财务审核提案、财务拒绝提案、2027 待生效可撤回版本、14 张跨年度脱敏账单。

## API 一览

除 `/health` 外均为治理接口。错误返回 `400`（请求不合法）、`404`（不存在）、`409`（状态冲突）。

### 目录版本

```bash
# 查询（?level=national|provincial&region=HB）
GET  /catalogs/versions
GET  /catalogs/NATIONAL/versions/NAT-2025          # 含项目明细

# 新建为草稿（13 个类别之一，金额两位小数字符串）
POST /catalogs/versions
{"level":"provincial","region":"HB","version":"HB-2027",
 "effective_from":"2027-01-01","effective_to":null,
 "projects":[{"code":"HB0701","name":"心电图","category":"心血管",
              "unit":"次","price":"35.00","payment_limit":null}]}

POST /catalogs/PROV-HB/versions/HB-2027/publish    # 发布（区间重叠返回 409）
POST /catalogs/PROV-HB/versions/HB-2027/withdraw   # 仅未生效版本可撤回
```

### 映射提案与双审

```bash
POST /proposals                  # evidence 必填；多对多、缺编码、编码交叉返回 400
GET  /proposals?status=submitted
GET  /proposals/PRP-...

POST /proposals/PRP-.../reviews  # {"kind":"business|finance","decision":"approved|rejected",
                                 #  "reviewer":"...","comment":"..."}
POST /proposals/PRP-.../publish-mapping
# {"version":"2027-1","effective_from":"2027-01-01","effective_to":null}

GET  /mappings?region=HB
POST /mappings/MAP-HB-2027-1/withdraw
```

提案示例（1:N 与 N:1）：

```json
{
  "proposer": "湖北医保专家组",
  "national_version": "NAT-2026", "region": "HB",
  "intended_from": "2026-01-01", "intended_to": "2026-12-31",
  "evidence": ["国家医保局2026年版目录通告", "省听证会纪要"],
  "groups": [
    {"links": [{"national_code": "N7001", "provincial_code": "HB0701"},
               {"national_code": "N7001", "provincial_code": "HB0702"}],
     "note": "省心电拆为常规与床旁加收"},
    {"links": [{"national_code": "N8011", "provincial_code": "HB0801"},
               {"national_code": "N8012", "provincial_code": "HB0801"}],
     "note": "省联合检查打包国家容量+通气两项"}
  ]
}
```

### 结算（跨省异地就医）

```bash
POST /settlements
{
  "claim_id": "CLM-2026-0215-010",
  "insured_region": "HB", "care_region": "HB",
  "service_date": "2026-02-15",
  "lines": [
    {"line_id": "L1", "service_date": "2026-02-15",
     "provincial_code": "HB0801", "quantity": 1, "charged_amount": "90.00"}
  ]
}
```

响应每行都带版本溯源；N:1 打包行 `bundle=true`，基准 45+50=95：

```json
{
  "status": "settled",
  "lines": [{
    "versions": {
      "national_catalog": "NATIONAL@NAT-2026",
      "provincial_catalog": "PROV-HB@HB-2026",
      "mapping": "MAP-HB-2026-1",
      "resolution_basis": "published_mapping"
    },
    "relation": "many_to_one", "bundle": true,
    "national_projects": [{"code": "N8011", "price": "45.00"},
                          {"code": "N8012", "price": "50.00"}],
    "baseline_amount": "95.00",
    "difference_amount": "-5.00"
  }],
  "version_summary": {
    "national_catalog_versions": ["NATIONAL@NAT-2026"],
    "provincial_catalog_versions": ["PROV-HB@HB-2026"],
    "mapping_versions": ["MAP-HB-2026-1"]
  }
}
```

存在未决行时整单 `manual_review`，未决行进队列（其余行照常给出基准）：

```bash
GET  /settlements                         # 列表（含状态与版本摘要）
GET  /settlements/CLM-...                 # 明细 + 关联冲正记录
POST /settlements/CLM-.../reversal        # {"reason":"重复申报","operator":"赵"}
```

### 人工队列

```bash
GET  /queue?status=open                   # open / closed / all
POST /queue/QUE-.../resolve
# 给出人工口径（登记 MANUAL 映射版本，绑定当前国家目录版本）：
# {"decision":"map","operator":"会审组","note":"暂按普通针刺管理",
#  "national_codes":["NB001"],"effective_from":"2025-01-01"}
# 或不予支付：{"decision":"reject","operator":"...","note":"..."}
```

人工处理后原单保留，需以新单据重新提交；查询接口会标明
`resolution_basis: manual_adjudication` 及具体 `MANUAL-*` 版本号。

### 影子回放

```bash
POST /shadow/replay
  {"use_seed_bills": true, "label": "2026版影子回放"}   # 回放内置历史账单
# 或自带 bills（格式同 /settlements 请求数组）：
  {"bills": [ ... ], "label": "自定义回放"}
GET  /shadow/runs
GET  /shadow/runs/SHD-...
```

报告字段：`coverage_rate`、`covered_lines` / `gap_lines`、`gap_reasons`、
`total_charged_amount`、`covered_charged_amount`、`total_baseline_amount`、
`total_difference_amount`（仅覆盖行口径）、`gap_charged_amount`（缺口单列）、
`by_category` 分类统计，以及逐行版本明细。

种子账单基线：**16 行费用，13 行可解释，覆盖率 81.25%**；缺口分别来自
省增补项目无国家对应、湖北 2026 映射未覆盖康复类、北京 2026 映射版本尚未发布。

### 跨日跑批

```bash
POST /batches
{"label":"跨日跑批","requests":[ {结算请求}, ... ]}
GET  /batches/BCH-...
```

批量在锁内"先规划、后统一落库"：任一请求非法或注入失败（演练参数 `fail_after`），
整批回滚，不留下任何单据或队列项。

### 审计

```bash
GET /audit      # 全部治理动作（创建/发布/撤回/审核/队列处理/冲正/跑批/影子）
```

## 关键语义备忘

* 金额一律为两位小数字符串，`Decimal` 运算；差异可为负，冲正金额带负号。
* 生效区间为闭区间 `[effective_from, effective_to]`，`null` 表示长期；
  甲结束日次日 = 乙开始日视为正常衔接，不算重叠。
* 结算从不读草稿或已撤回版本；命中多个有效版本视为数据损坏并报错（409）。
* 人工裁决口径随国家目录版本失效——换版后未决需重新会审，防止旧口径跨版误用。
