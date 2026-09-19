"""目录治理核心领域模型。

管理国家项目目录、省级项目目录，以及两者之间一对多 / 多对一 / 一对一
映射的生效区间版本。所有写操作都在同一把锁内完成并原子落盘，审计流水
只追加、不可修改。

关键规则：
- 生效区间为左闭右开 [effective_from, effective_until)；
- 历史费用永远按“就医发生时点”有效的版本解释；
- 映射建议必须附依据，并对系统检出的每一项冲突逐条确认；
- 业务审核、财务审核均通过后才能发布为待生效版本；
- 跨日跑批一次性激活全部到期版本，任一冲突则整批中止。
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

NATIONAL_SCOPE = "NATIONAL"
PROVINCE_SCOPE = "PROVINCE"

# 首批国家医保医疗服务项目目录的十三类服务
CATALOG_CATEGORIES = [
    "内科",
    "外科",
    "妇产科",
    "儿科",
    "眼科",
    "口腔",
    "麻醉",
    "护理",
    "康复",
    "中医",
    "病理",
    "检验",
    "影像",
]

# 版本状态
PENDING = "PENDING"        # 待生效：已发布，等待跨日跑批激活
ACTIVE = "ACTIVE"          # 已生效
SUPERSEDED = "SUPERSEDED"  # 被新版本接续
WITHDRAWN = "WITHDRAWN"    # 待生效版本被撤回

# 映射方向
ONE_TO_ONE = "1:1"
ONE_TO_MANY = "1:N"
MANY_TO_ONE = "N:1"

# 建议 / 审核状态
DRAFT = "DRAFT"
BUSINESS_APPROVED = "BUSINESS_APPROVED"
FINANCE_APPROVED = "FINANCE_APPROVED"
REJECTED = "REJECTED"
PUBLISHED = "PUBLISHED"

BUSINESS_REVIEW = "BUSINESS"
FINANCE_REVIEW = "FINANCE"
REVIEW_APPROVE = "APPROVE"
REVIEW_REJECT = "REJECT"

MONEY_QUANT = Decimal("0.01")


class DomainError(Exception):
    """所有可预期的业务错误，HTTP 层映射为 4xx。"""

    def __init__(self, message, code="DOMAIN_ERROR", details=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}


class NotFound(DomainError):
    def __init__(self, message):
        super().__init__(message, "NOT_FOUND")


class ValidationError(DomainError):
    def __init__(self, message, details=None):
        super().__init__(message, "VALIDATION_ERROR", details)


class ConflictError(DomainError):
    def __init__(self, message, details=None):
        super().__init__(message, "CONFLICT", details)


# ---------- 基础工具 ----------

def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def d(value):
    """安全转 Decimal。"""
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def money(value):
    if value is None:
        return None
    q = d(value).quantize(MONEY_QUANT)
    return str(q)


def parse_date(value):
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return datetime.strptime(value, "%Y-%m-%d").date()


def iso_day(value):
    return value.isoformat() if value else None


def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def interval_contains(version, on_day):
    start = parse_date(version["effective_from"])
    if on_day < start:
        return False
    end = version.get("effective_until")
    if end is not None and on_day >= parse_date(end):
        return False
    return True


# ---------- 注册中心 ----------

class Registry:
    """目录、映射、建议、人工队列、结算、审计的事务性注册中心。"""

    def __init__(self, path=None, clock=date.today):
        self.path = path
        self.clock = clock
        self.lock = threading.RLock()
        self._state = self._empty_state()
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                self._state = json.load(handle)

    @staticmethod
    def _empty_state():
        return {
            "catalogs": {},
            "mappings": {},
            "proposals": {},
            "queue": {},
            "settlements": {},
            "reversals": [],
            "audit": [],
        }

    # ===== 持久化与审计 =====

    def _persist_locked(self):
        if not self.path:
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._state, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _audit_locked(self, actor, action, detail):
        record = {
            "id": new_id("aud"),
            "timestamp": utcnow_iso(),
            "actor": actor,
            "action": action,
            "detail": detail,
        }
        self._state["audit"].append(record)
        return record

    def audit_tail(self, limit=100):
        with self.lock:
            return list(self._state["audit"][-limit:])

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self._state)

    def restore(self, state):
        with self.lock:
            self._state = copy.deepcopy(state)

    # ===== 目录 =====

    def create_catalog(self, scope, name, province=None, actor="system"):
        if scope == PROVINCE_SCOPE and not province:
            raise ValidationError("省级目录必须提供省份编码")
        with self.lock:
            for existing in self._state["catalogs"].values():
                if existing["scope"] == scope and existing.get("province") == province:
                    raise ConflictError("目录已存在", {"catalog_id": existing["id"]})
            catalog_id = "NATIONAL" if scope == NATIONAL_SCOPE else f"PROV-{province}"
            record = {
                "id": catalog_id,
                "scope": scope,
                "province": province,
                "name": name,
                "current_version": 1,
                "created_at": iso_day(self.clock()),
                "items": {},
            }
            self._state["catalogs"][catalog_id] = record
            self._audit_locked(actor, "CATALOG_CREATED", {"catalog_id": catalog_id})
            self._persist_locked()
            return copy.deepcopy(record)

    def get_catalog(self, catalog_id):
        with self.lock:
            catalog = self._state["catalogs"].get(catalog_id)
            if not catalog:
                raise NotFound(f"目录不存在: {catalog_id}")
            return copy.deepcopy(catalog)

    def list_catalogs(self, scope=None, province=None):
        with self.lock:
            result = []
            for catalog in self._state["catalogs"].values():
                if scope and catalog["scope"] != scope:
                    continue
                if province is not None and catalog.get("province") != province:
                    continue
                result.append({k: v for k, v in catalog.items() if k != "items"})
            return result

    def add_item(self, catalog_id, code, name, category, unit=None,
                 price=None, payment_limit=None, effective_from=None,
                 actor="system"):
        """新增项目，或为既有项目追加一个新版本。"""
        if category not in CATALOG_CATEGORIES:
            raise ValidationError(f"未知服务类别: {category}",
                                  {"allowed": CATALOG_CATEGORIES})
        start = parse_date(effective_from) or self.clock()
        with self.lock:
            catalog = self._state["catalogs"].get(catalog_id)
            if not catalog:
                raise NotFound(f"目录不存在: {catalog_id}")
            versions = catalog["items"].setdefault(code, [])
            if versions:
                # 新版本只能面向未来接续：追溯插入会改变历史费用的解释。
                # 种子基线通过空历史写入，不受此限。
                last = versions[-1]
                if start <= self.clock():
                    raise ConflictError(
                        f"项目 {code} 的新版本只能从未来日期生效，"
                        "不得追溯改变历史费用的解释",
                        {"today": iso_day(self.clock())},
                    )
                if start <= parse_date(last["effective_from"]):
                    raise ConflictError(
                        f"项目 {code} 的新版本起点必须晚于现版本起点",
                        {"current_effective_from": last["effective_from"]},
                    )
                if last.get("effective_until") and \
                        start < parse_date(last["effective_until"]):
                    raise ConflictError(
                        f"项目 {code} 在 {start} 已有生效版本",
                        {"current_effective_until": last["effective_until"]},
                    )
            version_no = len(versions) + 1
            record = {
                "code": code,
                "name": name,
                "category": category,
                "unit": unit,
                "price": money(price),
                "payment_limit": money(payment_limit),
                "version": version_no,
                "catalog_version": catalog["current_version"] + 1,
                "effective_from": iso_day(start),
                "effective_until": None,
            }
            if versions:
                versions[-1]["effective_until"] = iso_day(start)
            versions.append(record)
            catalog["current_version"] += 1
            self._audit_locked(actor, "ITEM_VERSION_ADDED", {
                "catalog_id": catalog_id, "code": code, "version": version_no,
                "effective_from": record["effective_from"],
            })
            self._persist_locked()
            return copy.deepcopy(record)

    def item_versions(self, catalog_id, code):
        with self.lock:
            catalog = self._state["catalogs"].get(catalog_id)
            if not catalog:
                raise NotFound(f"目录不存在: {catalog_id}")
            return copy.deepcopy(catalog["items"].get(code, []))

    def effective_item(self, catalog_id, code, on_day):
        """取某目录项目在指定时点的唯一有效版本；不存在返回 None。"""
        on_day = parse_date(on_day)
        with self.lock:
            catalog = self._state["catalogs"].get(catalog_id)
            if not catalog:
                raise NotFound(f"目录不存在: {catalog_id}")
            hit = None
            for version in catalog["items"].get(code, []):
                if interval_contains(version, on_day):
                    if hit:  # 理论上不会发生：入库时已串行化区间
                        raise ConflictError(
                            f"项目 {code} 在 {on_day} 存在多个生效版本",
                            {"catalog_id": catalog_id},
                        )
                    hit = version
            return copy.deepcopy(hit) if hit else None

    def effective_items(self, catalog_id, on_day):
        with self.lock:
            catalog = self._state["catalogs"].get(catalog_id)
            if not catalog:
                raise NotFound(f"目录不存在: {catalog_id}")
            on_day = parse_date(on_day)
            items = {}
            for code, versions in catalog["items"].items():
                for version in versions:
                    if interval_contains(version, on_day):
                        items[code] = copy.deepcopy(version)
            return list(items.values())

    def national_catalog_id(self):
        for catalog in self._state["catalogs"].values():
            if catalog["scope"] == NATIONAL_SCOPE:
                return catalog["id"]
        raise NotFound("国家目录尚未建立")

    def _national_id_locked(self):
        for catalog in self._state["catalogs"].values():
            if catalog["scope"] == NATIONAL_SCOPE:
                return catalog["id"]
        raise NotFound("国家目录尚未建立")

    # ===== 映射版本的只读查询 =====

    def _effective_group_versions_locked(self, province, on_day,
                                         include_pending=False):
        """返回该省在指定时点处于生效区间内的分组版本列表。

        待生效版本被撤回后，其接续的旧版本恢复 ACTIVE，因此必须扫描全量
        版本历史，而不能只看每组的最新版本。
        """
        on_day = parse_date(on_day)
        hits = []
        for group in self._state["mappings"].values():
            if group["province"] != province:
                continue
            for version in group["versions"]:
                if version["status"] not in (ACTIVE, PENDING):
                    continue
                if version["status"] == PENDING and not include_pending:
                    continue
                if interval_contains(version, on_day):
                    hits.append((group, version))
        return hits

    def effective_mappings(self, province, on_day=None):
        on_day = parse_date(on_day) or self.clock()
        with self.lock:
            out = []
            for group, version in self._effective_group_versions_locked(province, on_day):
                out.append({
                    "group_id": group["id"],
                    "province": province,
                    "direction": group["direction"],
                    "version": version["version"],
                    "status": version["status"],
                    "effective_from": version["effective_from"],
                    "effective_until": version["effective_until"],
                    "members": copy.deepcopy(version["members"]),
                    "weights": copy.deepcopy(version.get("weights")),
                })
            return out

    def get_group(self, group_id):
        with self.lock:
            group = self._state["mappings"].get(group_id)
            if not group:
                raise NotFound(f"映射分组不存在: {group_id}")
            return copy.deepcopy(group)

    def list_groups(self, province=None):
        with self.lock:
            result = []
            for group in self._state["mappings"].values():
                if province and group["province"] != province:
                    continue
                latest = group["versions"][-1]
                result.append({
                    "group_id": group["id"],
                    "province": group["province"],
                    "direction": group["direction"],
                    "latest_version": latest["version"],
                    "status": latest["status"],
                    "effective_from": latest["effective_from"],
                    "effective_until": latest["effective_until"],
                })
            return result

    # ===== 初始化导入（仅用于建立各省现行基线）=====

    def bootstrap_active_mapping(self, province, direction, members,
                                 effective_from, weights=None, actor="system"):
        """直接登记一条现行生效映射。仅用于系统初始化 / 种子导入。"""
        members = copy.deepcopy(members)
        start = parse_date(effective_from)
        with self.lock:
            nat_codes, prov_codes, weights = self._validate_membership_locked(
                province, direction, members, weights)
            conflicts = self._detect_conflicts_locked(
                province, direction, nat_codes, prov_codes)
            if conflicts:
                raise ConflictError("现行基线之间存在冲突，无法导入",
                                    {"conflicts": conflicts})
            group = {
                "id": new_id("map"),
                "province": province,
                "direction": direction,
                "versions": [{
                    "version": 1,
                    "status": ACTIVE,
                    "direction": direction,
                    "members": members,
                    "weights": weights,
                    "effective_from": iso_day(start),
                    "effective_until": None,
                    "proposal_id": None,
                    "supersedes_version": None,
                    "published_by": actor,
                    "published_at": None,
                    "activated_at": iso_day(start),
                }],
            }
            self._state["mappings"][group["id"]] = group
            self._audit_locked(actor, "MAPPING_BOOTSTRAPPED", {
                "group_id": group["id"], "province": province,
                "direction": direction, "effective_from": iso_day(start),
            })
            self._persist_locked()
            return {"group_id": group["id"], "version": 1, "status": ACTIVE}

    # ===== 建议：冲突检测 =====

    def _classify_members(self, members):
        national = sorted((m for m in members if m["side"] == "NATIONAL"),
                          key=lambda m: m["code"])
        provinces = sorted((m for m in members if m["side"] == "PROVINCE"),
                           key=lambda m: m["code"])
        return national, provinces

    def _validate_membership_locked(self, province, direction, members, weights):
        national, provinces = self._classify_members(members)
        if not national or not provinces:
            raise ValidationError("映射必须同时包含国家侧与省级侧编码")
        nat_codes = [m["code"] for m in national]
        prov_codes = [m["code"] for m in provinces]
        if len(set(nat_codes)) != len(nat_codes) or len(set(prov_codes)) != len(prov_codes):
            raise ValidationError("同一侧编码不得重复")
        # 元组含义：(国家侧数量要求, 省级侧数量要求)，1 表示恰好一个，2 表示至少两个
        expected = {ONE_TO_ONE: (1, 1), ONE_TO_MANY: (1, 2), MANY_TO_ONE: (2, 1)}
        need_nat, need_prov = expected[direction]
        if (need_nat == 1 and len(nat_codes) != 1) or \
           (need_nat == 2 and len(nat_codes) < 2) or \
           (need_prov == 1 and len(prov_codes) != 1) or \
           (need_prov == 2 and len(prov_codes) < 2):
            raise ValidationError(
                f"方向 {direction} 与成员数量不符",
                {"national": nat_codes, "province": prov_codes},
            )
        nat_id = self._national_id_locked()
        nat_catalog = self._state["catalogs"][nat_id]
        prov_catalog = self._state["catalogs"].get(f"PROV-{province}")
        if not prov_catalog:
            raise NotFound(f"省级目录不存在: PROV-{province}")
        categories = set()
        for code in nat_codes:
            if code not in nat_catalog["items"]:
                raise ValidationError(f"国家项目不存在: {code}")
            categories.add(nat_catalog["items"][code][-1]["category"])
        for code in prov_codes:
            if code not in prov_catalog["items"]:
                raise ValidationError(f"省级项目不存在: {code}")
            categories.add(prov_catalog["items"][code][-1]["category"])
        if len(categories) > 1:
            raise ValidationError(
                "映射成员必须属于同一服务类别",
                {"categories": sorted(categories)},
            )
        child_count = (len(prov_codes) if direction == ONE_TO_MANY
                       else len(nat_codes) if direction == MANY_TO_ONE
                       else 0)
        if child_count:
            if weights is None:
                weights = [f"{Decimal(1) / child_count:.6f}".rstrip("0").rstrip(".")
                           for _ in range(child_count)]
            if len(weights) != child_count:
                raise ValidationError("分摊权重数量与子项数量不一致")
            total = sum(d(w) for w in weights)
            if total <= 0:
                raise ValidationError("分摊权重之和必须为正")
        return nat_codes, prov_codes, weights

    def _detect_conflicts_locked(self, province, direction, nat_codes, prov_codes):
        """对照当前生效版本与待生效版本检出冲突。"""
        conflicts = []
        today = self.clock()

        def scan(version, group, status_label):
            v_nat = {m["code"] for m in version["members"] if m["side"] == "NATIONAL"}
            v_prov = {m["code"] for m in version["members"] if m["side"] == "PROVINCE"}
            same_group = v_nat == set(nat_codes) and v_prov == set(prov_codes)
            if same_group:
                conflicts.append({
                    "key": f"DUP:{group['id']}:v{version['version']}",
                    "type": "DUPLICATE_MAPPING",
                    "group_id": group["id"],
                    "version": version["version"],
                    "status": status_label,
                    "message": "与已有映射完全相同",
                })
                return
            nat_overlap = sorted(v_nat & set(nat_codes))
            prov_overlap = sorted(v_prov & set(prov_codes))
            if nat_overlap or prov_overlap:
                conflicts.append({
                    "key": f"OVERLAP:{group['id']}:v{version['version']}",
                    "type": "OVERLAPPING_MEMBER",
                    "group_id": group["id"],
                    "version": version["version"],
                    "status": status_label,
                    "national_overlap": nat_overlap,
                    "province_overlap": prov_overlap,
                    "message": "与已有映射存在编码交叉，方向或粒度可能不一致",
                })

        for group in self._state["mappings"].values():
            if group["province"] != province:
                continue
            for version in group["versions"]:
                if version["status"] == WITHDRAWN:
                    continue
                if version["status"] == ACTIVE and interval_contains(version, today):
                    scan(version, group, ACTIVE)
                elif version["status"] == PENDING:
                    scan(version, group, PENDING)
                elif version["status"] == SUPERSEDED:
                    continue
        return conflicts

    def submit_proposal(self, province, direction, members, evidence,
                        conflict_acknowledgements=None, weights=None,
                        proposal_id=None, actor="expert", note=""):
        """专家提交映射建议。evidence 为依据列表；检出的冲突必须逐条确认。"""
        if not evidence or not isinstance(evidence, list):
            raise ValidationError("必须提供映射依据(evidence)列表")
        for item in evidence:
            if not item.get("type") or not item.get("summary"):
                raise ValidationError("每条依据至少包含 type 与 summary")
            if item["type"] == "DOCUMENT" and not item.get("reference"):
                raise ValidationError("文件类依据必须提供 reference")
        members = copy.deepcopy(members)
        with self.lock:
            nat_codes, prov_codes, weights = self._validate_membership_locked(
                province, direction, members, weights)
            conflicts = self._detect_conflicts_locked(
                province, direction, nat_codes, prov_codes)
            acked = {a.get("key") for a in (conflict_acknowledgements or [])}
            missing = [c for c in conflicts if c["key"] not in acked]
            if missing:
                raise ConflictError(
                    "存在未确认的冲突项，专家须逐条列出并确认后再提交",
                    {"conflicts": conflicts, "missing_acknowledgements":
                     [c["key"] for c in missing]},
                )
            pid = proposal_id or new_id("prop")
            record = {
                "id": pid,
                "province": province,
                "direction": direction,
                "members": members,
                "weights": weights,
                "evidence": evidence,
                "conflicts": conflicts,
                "conflict_acknowledgements": conflict_acknowledgements or [],
                "note": note,
                "status": DRAFT,
                "submitted_by": actor,
                "submitted_at": utcnow_iso(),
                "reviews": [],
                "published_group": None,
            }
            self._state["proposals"][pid] = record
            self._audit_locked(actor, "PROPOSAL_SUBMITTED", {
                "proposal_id": pid, "province": province, "direction": direction,
                "conflict_count": len(conflicts),
            })
            self._persist_locked()
            return copy.deepcopy(record)

    def get_proposal(self, proposal_id):
        with self.lock:
            record = self._state["proposals"].get(proposal_id)
            if not record:
                raise NotFound(f"建议不存在: {proposal_id}")
            return copy.deepcopy(record)

    def list_proposals(self, status=None, province=None):
        with self.lock:
            out = []
            for record in self._state["proposals"].values():
                if status and record["status"] != status:
                    continue
                if province and record["province"] != province:
                    continue
                out.append(copy.deepcopy(record))
            return out

    # ===== 双审 =====

    def review_proposal(self, proposal_id, kind, decision, actor, comment=""):
        if kind not in (BUSINESS_REVIEW, FINANCE_REVIEW):
            raise ValidationError("审核类型必须为 BUSINESS 或 FINANCE")
        if decision not in (REVIEW_APPROVE, REVIEW_REJECT):
            raise ValidationError("审核结论必须为 APPROVE 或 REJECT")
        with self.lock:
            record = self._state["proposals"].get(proposal_id)
            if not record:
                raise NotFound(f"建议不存在: {proposal_id}")
            status = record["status"]
            if status in (REJECTED, PUBLISHED):
                raise ConflictError(f"建议已终结，状态为 {status}")
            if kind == BUSINESS_REVIEW:
                if status != DRAFT:
                    raise ConflictError("业务审核只能在待审(DRAFT)阶段进行",
                                        {"status": status})
            else:
                if status != BUSINESS_APPROVED:
                    raise ConflictError("必须先通过业务审核才能进行财务审核",
                                        {"status": status})
            if any(r["kind"] == kind for r in record["reviews"]):
                raise ConflictError(f"{kind} 审核已存在，不可重复审核")
            review = {
                "kind": kind,
                "decision": decision,
                "reviewer": actor,
                "comment": comment,
                "reviewed_at": utcnow_iso(),
            }
            record["reviews"].append(review)
            if decision == REVIEW_REJECT:
                record["status"] = REJECTED
                action = "PROPOSAL_REJECTED"
            elif kind == BUSINESS_REVIEW:
                record["status"] = BUSINESS_APPROVED
                action = "PROPOSAL_BUSINESS_APPROVED"
            else:
                record["status"] = FINANCE_APPROVED
                action = "PROPOSAL_FINANCE_APPROVED"
            self._audit_locked(actor, action, {
                "proposal_id": proposal_id, "decision": decision,
            })
            self._persist_locked()
            return copy.deepcopy(record)

    # ===== 发布 / 撤回 =====

    def _find_target_group_locked(self, province, nat_codes, prov_codes):
        """根据成员编码找到要接续的既有分组。"""
        nat_set, prov_set = set(nat_codes), set(prov_codes)
        matches = set()
        for group in self._state["mappings"].values():
            if group["province"] != province:
                continue
            for version in group["versions"]:
                if version["status"] == WITHDRAWN:
                    continue
                v_nat = {m["code"] for m in version["members"] if m["side"] == "NATIONAL"}
                v_prov = {m["code"] for m in version["members"] if m["side"] == "PROVINCE"}
                if v_nat & nat_set or v_prov & prov_set:
                    matches.add(group["id"])
                    break
        if len(matches) > 1:
            raise ConflictError(
                "新映射与多个现存分组交叉，无法隐式接续；请先撤回/调整相关待生效版本",
                {"group_ids": sorted(matches)},
            )
        return next(iter(matches)) if matches else None

    def publish_proposal(self, proposal_id, effective_from, actor="publisher",
                         effective_until=None):
        """双审通过后发布为待生效版本（跨日跑批后生效）。"""
        start = parse_date(effective_from)
        end = parse_date(effective_until)
        if start < self.clock():
            raise ValidationError("生效起点不得早于当前日期")
        if end and end <= start:
            raise ValidationError("生效止点必须晚于起点")
        with self.lock:
            record = self._state["proposals"].get(proposal_id)
            if not record:
                raise NotFound(f"建议不存在: {proposal_id}")
            if record["status"] != FINANCE_APPROVED:
                raise ConflictError("只有业务与财务双审通过的建议才能发布",
                                    {"status": record["status"]})
            nat, prov = self._classify_members(record["members"])
            nat_codes = [m["code"] for m in nat]
            prov_codes = [m["code"] for m in prov]
            target_id = self._find_target_group_locked(
                record["province"], nat_codes, prov_codes)
            target = self._state["mappings"].get(target_id) if target_id else None
            if target:
                if any(v["status"] == PENDING for v in target["versions"]):
                    raise ConflictError(
                        "该分组存在待生效版本，请先撤回后再发布新版本",
                        {"group_id": target["id"]},
                    )
                active_predecessors = [
                    i for i, v in enumerate(target["versions"])
                    if v["status"] == ACTIVE]
                pred_idx = active_predecessors[-1] if active_predecessors else None
                if pred_idx is not None:
                    predecessor = target["versions"][pred_idx]
                    if start <= parse_date(predecessor["effective_from"]):
                        raise ValidationError(
                            "新版本生效起点必须晚于现版本生效起点",
                            {"current_from": predecessor["effective_from"]},
                        )
            # 同一生效日的全局互斥最终校验（防止并发发布造成编码重叠）；
            # 目标分组的旧版本将被本次候选接续，不参与互斥
            self._assert_activation_set_valid_locked(
                record["province"], start,
                extra=[(record["direction"], record["members"])],
                ignore_group_id=target_id,
            )

            if target is None:
                group_id = new_id("map")
                target = {
                    "id": group_id,
                    "province": record["province"],
                    "direction": record["direction"],
                    "versions": [],
                }
                self._state["mappings"][group_id] = target
                supersedes = None
            else:
                supersedes = (pred_idx + 1) if pred_idx is not None else None

            version_no = len(target["versions"]) + 1
            version = {
                "version": version_no,
                "status": PENDING,
                "direction": record["direction"],
                "members": copy.deepcopy(record["members"]),
                "weights": copy.deepcopy(record.get("weights")),
                "effective_from": iso_day(start),
                "effective_until": iso_day(end),
                "proposal_id": proposal_id,
                "supersedes_version": supersedes,
                "published_by": actor,
                "published_at": utcnow_iso(),
                "activated_at": None,
            }
            target["versions"].append(version)
            if supersedes:
                target["versions"][supersedes - 1]["effective_until"] = iso_day(start)
            record["status"] = PUBLISHED
            record["published_group"] = {
                "group_id": target["id"], "version": version_no,
                "effective_from": iso_day(start),
            }
            self._audit_locked(actor, "MAPPING_PUBLISHED", {
                "proposal_id": proposal_id,
                "group_id": target["id"], "version": version_no,
                "effective_from": iso_day(start),
            })
            self._persist_locked()
            return {"group_id": target["id"], "version": version_no,
                    "status": PENDING, "effective_from": iso_day(start)}

    def withdraw_version(self, group_id, version, actor, reason):
        """撤回待生效版本；若接续了旧版本，旧版本重新开放区间。"""
        if not reason or not reason.strip():
            raise ValidationError("撤回必须填写原因")
        with self.lock:
            group = self._state["mappings"].get(group_id)
            if not group:
                raise NotFound(f"映射分组不存在: {group_id}")
            if version < 1 or version > len(group["versions"]):
                raise NotFound(f"版本不存在: {group_id} v{version}")
            target = group["versions"][version - 1]
            if target["status"] != PENDING:
                raise ConflictError("只能撤回待生效(PENDING)版本",
                                    {"status": target["status"]})
            target["status"] = WITHDRAWN
            target["withdrawn_by"] = actor
            target["withdrawn_reason"] = reason
            target["withdrawn_at"] = utcnow_iso()
            previous_no = target.get("supersedes_version")
            reopened = None
            if previous_no:
                previous = group["versions"][previous_no - 1]
                previous["status"] = ACTIVE
                previous["effective_until"] = None
                reopened = previous_no
            self._audit_locked(actor, "MAPPING_WITHDRAWN", {
                "group_id": group_id, "version": version,
                "reopened_previous_version": reopened, "reason": reason,
            })
            self._persist_locked()
            return {"group_id": group_id, "version": version,
                    "status": WITHDRAWN, "reopened_previous_version": reopened}

    # ===== 跨日激活跑批 =====

    def _activation_plan_locked(self, as_of):
        """返回 (到期待激活列表, 激活后在 as_of 仍然生效的全部版本描述)。"""
        due = []
        for group in self._state["mappings"].values():
            latest = group["versions"][-1]
            if latest["status"] != PENDING:
                continue
            start = parse_date(latest["effective_from"])
            if start > as_of:
                continue
            end = parse_date(latest.get("effective_until"))
            if end and as_of >= end:
                raise ConflictError(
                    f"分组 {group['id']} v{latest['version']} 尚未激活即已过期",
                    {"group_id": group["id"], "version": latest["version"],
                     "effective_from": latest["effective_from"],
                     "effective_until": latest["effective_until"]},
                )
            due.append((group, latest))
        effective = []
        for group in self._state["mappings"].values():
            for version in group["versions"]:
                if version["status"] == ACTIVE and interval_contains(version, as_of):
                    effective.append((group, version))
        return due, effective

    def _assert_activation_set_valid_locked(self, province, on_day, extra=None,
                                            ignore_group_id=None,
                                            ignore_superseded=None):
        """校验某省某天所有生效映射的编码互斥。

        extra 为待加入的候选；ignore_group_id 分组的版本将被候选接续，
        不参与互斥；ignore_superseded 中的 (group_id, version) 同理，
        用于跨日跑批时排除将被接续的旧版本。
        """
        ignore_superseded = ignore_superseded or set()
        owner = {}
        def claim(codes, group_id, version_no, status):
            for code in codes:
                if code in owner and owner[code] != (group_id, version_no):
                    raise ConflictError(
                        f"编码 {code} 在 {on_day} 同时归属两个映射版本",
                        {"code": code,
                         "owners": [
                            {"group_id": owner[code][0], "version": owner[code][1]},
                            {"group_id": group_id, "version": version_no,
                             "status": status}]},
                    )
                owner[code] = (group_id, version_no)
        for group, version in self._effective_group_versions_locked(province, on_day):
            if group["id"] == ignore_group_id:
                continue
            if (group["id"], version["version"]) in ignore_superseded:
                continue
            nat = [m["code"] for m in version["members"] if m["side"] == "NATIONAL"]
            prov = [m["code"] for m in version["members"] if m["side"] == "PROVINCE"]
            claim(nat + prov, group["id"], version["version"], version["status"])
        for index, (direction, members) in enumerate(extra or []):
            nat = [m["code"] for m in members if m["side"] == "NATIONAL"]
            prov = [m["code"] for m in members if m["side"] == "PROVINCE"]
            claim(nat + prov, f"<new-{index}>", index, PENDING)

    def activate_due(self, as_of=None, actor="batch"):
        """跨日跑批：原子激活所有到期版本；任一冲突整批中止、状态不变。"""
        as_of = parse_date(as_of) or self.clock()
        with self.lock:
            due, _ = self._activation_plan_locked(as_of)
            if not due:
                return {"as_of": iso_day(as_of), "activated": [], "aborted": False}
            # 整批预校验：逐省构造“激活后”的互斥集合，不修改任何状态
            provinces = {group["province"] for group, _ in due}
            for province in provinces:
                due_versions = [v for g, v in due if g["province"] == province]
                # 激活后仍生效的旧版本 = 现行版本中未被任一到期版本接续者
                superseded_marks = set()
                extras = []
                for version in due_versions:
                    group = self._group_of_version_locked(version)
                    prev_no = version.get("supersedes_version")
                    if prev_no:
                        superseded_marks.add((group["id"], prev_no))
                    extras.append((version["direction"], version["members"]))
                self._assert_activation_set_valid_locked(
                    province, as_of, extra=extras,
                    ignore_superseded=superseded_marks)

            # 预校验通过，提交整批状态变更
            activated = []
            for group, version in due:
                prev_no = version.get("supersedes_version")
                if prev_no:
                    previous = group["versions"][prev_no - 1]
                    previous["status"] = SUPERSEDED
                    previous["effective_until"] = version["effective_from"]
                version["status"] = ACTIVE
                version["activated_at"] = iso_day(as_of)
                activated.append({"group_id": group["id"], "version": version["version"],
                                  "province": group["province"],
                                  "effective_from": version["effective_from"]})
            self._audit_locked(actor, "MAPPINGS_ACTIVATED", {
                "as_of": iso_day(as_of), "count": len(activated),
                "groups": activated,
            })
            self._persist_locked()
            return {"as_of": iso_day(as_of), "activated": activated, "aborted": False}

    def _group_of_version_locked(self, version_obj):
        for group in self._state["mappings"].values():
            for candidate in group["versions"]:
                if candidate is version_obj:
                    return group
        raise ConflictError("内部错误：找不到版本所属分组")

    # ===== 一致性检查（跨日跑批前后均可调用）=====

    def consistency_check(self, as_of=None):
        as_of = parse_date(as_of) or self.clock()
        report = {"as_of": iso_day(as_of), "ok": True, "issues": [],
                  "stats": {"catalogs": 0, "active_groups": 0, "pending_versions": 0}}
        with self.lock:
            report["stats"]["catalogs"] = len(self._state["catalogs"])
            for group in self._state["mappings"].values():
                for version in group["versions"]:
                    if version["status"] == PENDING \
                            and parse_date(version["effective_from"]) <= as_of:
                        report["issues"].append({
                            "level": "WARNING",
                            "type": "DUE_NOT_ACTIVATED",
                            "group_id": group["id"], "version": version["version"],
                            "message": "版本已到期但尚未跑批激活，结算将进入人工队列",
                        })
                    if version["status"] == ACTIVE \
                            and version.get("effective_until") \
                            and parse_date(version["effective_until"]) <= as_of:
                        report["issues"].append({
                            "level": "WARNING",
                            "type": "EXPIRED_WITHOUT_SUCCESSOR",
                            "group_id": group["id"], "version": version["version"],
                            "message": "生效区间已结束且无接续版本，相关编码将无法结算",
                        })
            for province in {g["province"] for g in self._state["mappings"].values()}:
                try:
                    self._assert_activation_set_valid_locked(province, as_of)
                except ConflictError as exc:
                    report["ok"] = False
                    report["issues"].append({
                        "level": "ERROR", "type": "OVERLAPPING_ACTIVE_MAPPINGS",
                        "province": province, **exc.details,
                        "message": exc.message,
                    })
                # 干跑跨日激活：若今天直接跑批会整批中止，必须提前报 ERROR
                try:
                    due, _ = self._activation_plan_locked(as_of)
                    due_here = [v for g, v in due if g["province"] == province]
                    if due_here:
                        marks, extras = set(), []
                        for version in due_here:
                            group = self._group_of_version_locked(version)
                            if version.get("supersedes_version"):
                                marks.add((group["id"],
                                           version["supersedes_version"]))
                            extras.append((version["direction"],
                                           version["members"]))
                        self._assert_activation_set_valid_locked(
                            province, as_of, extra=extras,
                            ignore_superseded=marks)
                except ConflictError as exc:
                    report["ok"] = False
                    report["issues"].append({
                        "level": "ERROR",
                        "type": "BATCH_ACTIVATION_WOULD_ABORT",
                        "province": province, **exc.details,
                        "message": exc.message,
                    })
            report["stats"]["active_groups"] = sum(
                1 for g in self._state["mappings"].values()
                if g["versions"][-1]["status"] == ACTIVE)
            report["stats"]["pending_versions"] = sum(
                1 for g in self._state["mappings"].values()
                if g["versions"][-1]["status"] == PENDING)
            if any(i["level"] == "ERROR" for i in report["issues"]):
                report["ok"] = False
            return report
