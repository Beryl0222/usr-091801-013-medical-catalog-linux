# 医保项目目录衔接 · 目录治理后端

首批国家医保医疗服务项目目录（**十三类服务**）与各省现行项目目录之间的
衔接治理后端。保存国家项目、省级项目以及一对一 / 一对多 / 多对一映射的
**生效区间版本**，支撑跨省异地就医结算先行、再逐步与各省目录衔接。

仅依赖 Python 3 标准库；`npm test` 运行全部契约与领域测试。

## 核心规则

1. **时间点解释，不重写历史。** 所有目录项目和映射版本均为左闭右开
   区间 `[effective_from, effective_until)`；结算严格按“就医发生时点 +
   参保地 + 就医地”取唯一适用版本。目录项目新版本只能从未来日期生效，
   追溯插入会被拒绝，历史费用始终保留当时的解释。
2. **映射建议必须带依据和冲突项。** 专家提交时系统自动对照现行与待生效
   版本检出 `DUPLICATE_MAPPING` / `OVERLAPPING_MEMBER` 冲突，专家必须对
   每一项逐条确认（`conflict_acknowledgements`），建议永久保留冲突清单。
3. **业务、财务双审后才能发布。** 顺序为 业务审核 → 财务审核；任一驳回
   即终结。发布只产生**待生效版本（PENDING）**，由跨日跑批统一激活。
4. **待生效版本可撤回，已生效版本不可改。** 撤回 PENDING 版本后其接续的
   旧版本恢复 ACTIVE 且重新开放区间。跨日跑批一次性激活全部到期版本，
   任一编码互斥冲突则**整批中止、状态不变**。
5. **未决映射进入人工队列，绝不猜测。** 无生效映射、项目时点版本缺失、
   多映射歧义都使整单标记 `MANUAL_REVIEW`；待生效版本只作为队列提示，
   不参与结算。
6. **正式生效后只能冲正。** 结算记录不可变；错误通过追加冲正记录修复，
   冲正按原就医时点重算并给出各项差额，原记录与冲正挂接可查。
7. **结果必须可追溯。** 每笔结算返回国家 / 就医地 / 参保地三个目录版本、
   引擎版本，以及每行的国家项目版本、映射分组与映射版本，不只有金额。
8. **影子模式不落库。** 回放仓库内置的历史脱敏账单，报告行/单覆盖率与
   支付差异，不写正式结算、不产生人工队列。

## 文件结构

| 文件 | 职责 |
|---|---|
| `catalog.py` | 领域核心：目录与映射时间版本、状态机、建议/双审/发布/撤回、跨日激活、一致性检查、原子持久化与只追加审计 |
| `settlement.py` | 结算引擎：时点版本解析、1:N / N:1 分摊、跨省支付限额、人工队列、冲正 |
| `shadow.py` | 影子回放与覆盖率/支付差异报告（JSON + 文本） |
| `seed_data.py` | 虚构脱敏种子：国家 26 项（十三类各 2 项）、沪/浙两省目录与粒度差异映射 |
| `data/historical_bills.json` | 8 张历史脱敏账单（含跨省、未决项目、基线前账单） |
| `service.py` | HTTP 入口与 CLI（`--check/--seed/--shadow`） |
| `service_contract.py` / `test_domain.py` | 健康契约测试与 25 项领域/接口/并发测试 |

种子刻意制造粒度差异：上海将“白内障+人工晶体”“全麻+插管”“血尿常规”
捆绑收费（N:1）；浙江将“一级护理”“全身麻醉”拆成子项收费（1:N）；
两省各保留一个尚无国家衔接的地方项目（`SH-TZ001`、`ZJ-HL003`）。

## 快速开始

```bash
python3 service.py --check              # 基础自检（十三类已登记）
python3 service.py --seed --reset       # 初始化种子（--reset 重建数据文件）
python3 service.py --shadow             # 影子回放并打印文本报告
python3 service.py --port 8000          # 启动服务（空数据自动播种）
npm test                                # 全部测试
```

当前种子影子回放结果：8 张账单整单覆盖 5 张、费用行覆盖率 83.3%，
申报与国家参照价差异 +1722.00 元；3 张未决账单（地方特色项目、未衔接
护理子项、2025 年基线生效前账单）全部按未决处理而非猜测。

## HTTP 接口

写操作的操作人取 `X-Actor` 请求头；业务错误返回
`{error, message, details}`，状态码 400 / 404 / 409。

```
GET  /health
GET  /catalogs[?scope=NATIONAL|PROVINCE&province=SH]
GET  /catalogs/{id}/items?date=YYYY-MM-DD
GET  /catalogs/{id}/items/{code}/versions
GET  /mappings?province=ZJ&date=2026-04-18
GET  /mappings/{group_id}

POST /proposals                          # {province,direction,members,weights,evidence,conflict_acknowledgements}
GET  /proposals[?status=&province=]
GET  /proposals/{id}
POST /proposals/{id}/reviews             # {kind: BUSINESS|FINANCE, decision: APPROVE|REJECT, comment}
POST /proposals/{id}/publish             # {effective_from, effective_until?}
POST /mappings/{id}/versions/{v}/withdraw# {reason}
POST /admin/activate                     # {"as_of": "YYYY-MM-DD"} 跨日跑批
GET  /admin/consistency?as_of=YYYY-MM-DD

POST /settlements                        # {bill_id,service_date,provider_province,insured_province,lines[]}
GET  /settlements[?bill_id=&status=]
GET  /settlements/{id}                   # 含全部目录/映射版本与冲正列表
POST /settlements/{id}/reversals         # {reason, corrected_lines[]}
GET  /reversals/{id}
GET  /queue[?status=OPEN]
POST /queue/{id}/annotations             # {note, decision: ANNOTATED|ESCALATED|RESOLVED_EXTERNALLY}

GET|POST /shadow/replay                  # GET 回放内置账单；POST 可传 {"bills": [...]}
GET  /audit?limit=100
```

### 典型流程

```bash
# 1) 专家提交（首次返回 409 与冲突清单，逐条确认后重提）
curl -X POST localhost:8000/proposals -H 'Content-Type: application/json' -d '{
  "province":"SH","direction":"1:1",
  "members":[{"side":"NATIONAL","code":"N-HL001"},
             {"side":"PROVINCE","code":"SH-HL002"}],
  "evidence":[{"type":"DOCUMENT","reference":"沪医保[2026]12号","summary":"一级护理改挂论证"}],
  "conflict_acknowledgements":[{"key":"<首次响应给出>","note":"确认接续旧版本"}]}'

# 2) 业务、财务依次审核 → 发布待生效 → 跨日跑批
curl -X POST localhost:8000/proposals/{id}/reviews    -d '{"kind":"BUSINESS","decision":"APPROVE"}'
curl -X POST localhost:8000/proposals/{id}/reviews    -d '{"kind":"FINANCE","decision":"APPROVE"}'
curl -X POST localhost:8000/proposals/{id}/publish    -d '{"effective_from":"2026-10-01"}'
curl -X POST localhost:8000/admin/activate            -d '{"as_of":"2026-10-01"}'
```

## 金额与分摊口径

- 所有金额以字符串形式的两位小数传输，内部使用 `Decimal`；
- **1:N**（国家父项目拆成多条省子项）：国家参照价按权重分摊到子项，
  如全身麻醉 800 元 × 0.4 / 0.6 = 320 / 480；
- **N:1**（省级捆绑行对应多个国家项目）：账单行金额按权重拆分展示，
  国家参照额为各国家项目之和；跨省结算时参保地支付限额沿**参保地自己的
  映射与权重**独立计算（两套权重不连乘）；
- 支付参照额 = min(国家参照额, 参保地支付限额)，无参保地限额时取国家参照。
