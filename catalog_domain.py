"""医保国家/省级项目目录衔接的领域核心。

设计要点：
* 目录与映射均为"版本 + 生效区间（[effective_from, effective_to]）"模型，
  历史费用始终按就医发生时点找到的唯一有效版本解释。
* 映射提案必须附依据与冲突项，业务、财务两类审核均通过后才能发布为映射版本。
* 结算只依据已发布版本；未决映射进入人工队列，绝不猜测。
* 影子模式只回放出报告，不写正式结算；正式结算不可修改，出错只能冲正后重提。
* 所有变更在同一把锁内原子完成，跨日跑批与并发发布不会产生半成品状态。

金额使用 Decimal，对外序列化为字符串以避免浮点误差。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Callable, Iterable

# ---------------------------------------------------------------------------
# 常量与异常
# ---------------------------------------------------------------------------

NATIONAL_CATALOG = "NATIONAL"
NATIONAL_LEVEL = "national"
PROVINCIAL_LEVEL = "provincial"

# 首批十三类服务（演示用分类口径）。
SERVICE_CATEGORIES = [
    "综合医疗服务",
    "诊断",
    "治疗",
    "康复",
    "护理",
    "麻醉",
    "心血管",
    "呼吸",
    "眼科",
    "妇科",
    "中医",
    "病理检查",
    "医技检查",
]

ONE_TO_ONE = "one_to_one"
ONE_TO_MANY = "one_to_many"  # 一个国家项目对应多个省级项目（省粒度更细）
MANY_TO_ONE = "many_to_one"  # 多个国家项目打包为一个省级项目

RELATION_LABELS = {
    ONE_TO_ONE: "一对一",
    ONE_TO_MANY: "一对多",
    MANY_TO_ONE: "多对一",
}


class DomainError(Exception):
    """请求本身不合法（400）。"""


class NotFoundError(DomainError):
    """对象不存在（404）。"""


class StateConflictError(DomainError):
    """对象当前状态不允许该操作（409）。"""


def d(value) -> Decimal:
    """把字符串/数字安全转为两位小数 Decimal。"""
    if isinstance(value, Decimal):
        dec = value
    else:
        dec = Decimal(str(value))
    if dec < 0:
        raise DomainError("金额不能为负")
    return dec.quantize(Decimal("0.01"))


def money(value: Decimal) -> str:
    return str(d(value))


def signed_money(value) -> str:
    """差异金额允许为正（多收）或为负（低于国家基准）。"""
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    return str(dec.quantize(Decimal("0.01")))


def parse_date(value: str | date | None, *, name: str = "日期") -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DomainError(f"{name}格式应为 YYYY-MM-DD") from exc


def _far_future() -> date:
    return date(9999, 12, 31)


def interval_covers(start: date, end: date | None, day: date) -> bool:
    return start <= day and (end is None or day <= end)


def _overlaps(a_start: date, a_end: date | None, b_start: date, b_end: date | None) -> bool:
    """相邻版本（甲结束次日 = 乙开始）不算重叠。"""
    a_close = a_end or _far_future()
    b_close = b_end or _far_future()
    return a_start <= b_close and b_start <= a_close


# ---------------------------------------------------------------------------
# 领域对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Project:
    code: str
    name: str
    category: str
    unit: str
    price: Decimal
    payment_limit: str | None = None

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "category": self.category,
            "unit": self.unit,
            "price": money(self.price),
            "payment_limit": self.payment_limit,
        }


@dataclass
class CatalogVersion:
    catalog_id: str
    version: str
    level: str
    region: str | None
    effective_from: date
    effective_to: date | None
    projects: dict[str, Project] = field(default_factory=dict)
    published: bool = False
    withdrawn: bool = False
    created_at: datetime | None = None
    published_at: datetime | None = None

    @property
    def id(self) -> str:
        return f"{self.catalog_id}@{self.version}"

    def covers(self, day: date) -> bool:
        return self.published and not self.withdrawn and interval_covers(
            self.effective_from, self.effective_to, day
        )

    def to_dict(self, *, include_projects: bool = False) -> dict:
        data = {
            "catalog_id": self.catalog_id,
            "version": self.version,
            "id": self.id,
            "level": self.level,
            "region": self.region,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "status": (
                "withdrawn"
                if self.withdrawn
                else "published"
                if self.published
                else "draft"
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "project_count": len(self.projects),
        }
        if include_projects:
            data["projects"] = [p.to_dict() for p in self.projects.values()]
        return data


@dataclass(frozen=True)
class MappingLink:
    national_code: str
    provincial_code: str


@dataclass
class MappingGroup:
    """一组互相关联的国家/省项目；同组内构成 1:1、1:N 或 N:1。"""

    links: tuple[MappingLink, ...]
    note: str = ""

    @property
    def national_codes(self) -> tuple[str, ...]:
        return tuple(sorted({lk.national_code for lk in self.links}))

    @property
    def provincial_codes(self) -> tuple[str, ...]:
        return tuple(sorted({lk.provincial_code for lk in self.links}))

    @property
    def relation(self) -> str:
        n, p = len(self.national_codes), len(self.provincial_codes)
        if n == 1 and p == 1:
            return ONE_TO_ONE
        if n == 1 and p > 1:
            return ONE_TO_MANY
        if n > 1 and p == 1:
            return MANY_TO_ONE
        return "many_to_many"

    def to_dict(self) -> dict:
        return {
            "national_codes": list(self.national_codes),
            "provincial_codes": list(self.provincial_codes),
            "relation": self.relation,
            "relation_label": RELATION_LABELS.get(self.relation, self.relation),
            "links": [
                {"national_code": lk.national_code, "provincial_code": lk.provincial_code}
                for lk in self.links
            ],
            "note": self.note,
        }


@dataclass
class MappingVersion:
    mapping_version_id: str
    national_catalog_version: str  # 例如 NATIONAL@NAT-2026-1
    region: str
    version: str
    effective_from: date
    effective_to: date | None
    groups: list[MappingGroup] = field(default_factory=list)
    published: bool = False
    withdrawn: bool = False
    source_proposal_id: str | None = None
    published_at: datetime | None = None
    kind: str = "published"  # published / manual

    @property
    def id(self) -> str:
        return self.mapping_version_id

    def covers(self, day: date) -> bool:
        return self.published and not self.withdrawn and interval_covers(
            self.effective_from, self.effective_to, day
        )

    def group_for(self, provincial_code: str) -> MappingGroup | None:
        for group in self.groups:
            if provincial_code in group.provincial_codes:
                return group
        return None

    def to_dict(self, *, include_groups: bool = True) -> dict:
        data = {
            "mapping_version_id": self.mapping_version_id,
            "national_catalog_version": self.national_catalog_version,
            "region": self.region,
            "version": self.version,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "status": (
                "withdrawn"
                if self.withdrawn
                else "published"
                if self.published
                else "draft"
            ),
            "kind": self.kind,
            "source_proposal_id": self.source_proposal_id,
            "published_at": self.published_at.isoformat() if self.published_at else None,
        }
        if include_groups:
            data["groups"] = [g.to_dict() for g in self.groups]
        return data


@dataclass
class Review:
    kind: str  # business / finance
    decision: str  # approved / rejected
    reviewer: str
    comment: str
    reviewed_at: datetime

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "decision": self.decision,
            "reviewer": self.reviewer,
            "comment": self.comment,
            "reviewed_at": self.reviewed_at.isoformat(),
        }


@dataclass
class Proposal:
    proposal_id: str
    proposer: str
    national_catalog_version: str
    region: str
    title: str
    groups: list[MappingGroup]
    evidence: list[str]
    conflicts: list[str]
    intended_from: date
    intended_to: date | None
    submitted_at: datetime
    business_review: Review | None = None
    finance_review: Review | None = None
    status: str = "submitted"  # submitted/approved/rejected/published
    published_mapping_version_id: str | None = None

    def to_dict(self, *, include_groups: bool = True) -> dict:
        data = {
            "proposal_id": self.proposal_id,
            "proposer": self.proposer,
            "title": self.title,
            "national_catalog_version": self.national_catalog_version,
            "region": self.region,
            "status": self.status,
            "evidence": list(self.evidence),
            "conflicts": list(self.conflicts),
            "intended_from": self.intended_from.isoformat(),
            "intended_to": self.intended_to.isoformat() if self.intended_to else None,
            "submitted_at": self.submitted_at.isoformat(),
            "business_review": self.business_review.to_dict() if self.business_review else None,
            "finance_review": self.finance_review.to_dict() if self.finance_review else None,
            "published_mapping_version_id": self.published_mapping_version_id,
        }
        if include_groups:
            data["groups"] = [g.to_dict() for g in self.groups]
        return data


# 队列原因码
REASON_NO_NATIONAL_VERSION = "NO_NATIONAL_VERSION"
REASON_NO_PROVINCIAL_VERSION = "NO_PROVINCIAL_VERSION"
REASON_NO_MAPPING_VERSION = "MAPPING_VERSION_GAP"
REASON_UNMAPPED_CODE = "UNMAPPED_PROVINCIAL_CODE"

REASON_LABELS = {
    REASON_NO_NATIONAL_VERSION: "就医时点无有效国家目录版本",
    REASON_NO_PROVINCIAL_VERSION: "就医地在该时点无有效省级目录版本",
    REASON_NO_MAPPING_VERSION: "就医时点该省尚未发布有效映射版本",
    REASON_UNMAPPED_CODE: "省级项目在有效映射版本中没有对应关系",
}


@dataclass
class QueueItem:
    queue_id: str
    claim_id: str
    created_at: datetime
    care_region: str
    insured_region: str
    service_date: date
    provincial_code: str
    line_id: str
    reason: str
    detail: str
    national_catalog_version: str | None
    provincial_catalog_version: str | None
    mapping_version: str | None
    status: str = "open"  # open/closed
    resolution: dict | None = None

    def to_dict(self) -> dict:
        return {
            "queue_id": self.queue_id,
            "claim_id": self.claim_id,
            "created_at": self.created_at.isoformat(),
            "care_region": self.care_region,
            "insured_region": self.insured_region,
            "service_date": self.service_date.isoformat(),
            "provincial_code": self.provincial_code,
            "line_id": self.line_id,
            "reason": self.reason,
            "reason_label": REASON_LABELS.get(self.reason, self.reason),
            "detail": self.detail,
            "status": self.status,
            "resolution": self.resolution,
            "versions": {
                "national_catalog": self.national_catalog_version,
                "provincial_catalog": self.provincial_catalog_version,
                "mapping": self.mapping_version,
            },
        }


# ---------------------------------------------------------------------------
# 治理服务
# ---------------------------------------------------------------------------


class CatalogGovernanceService:
    """目录治理应用服务。线程安全：所有公共方法在锁内原子执行。"""

    def __init__(self, clock: Callable[[], datetime] | None = None):
        self._lock = threading.RLock()
        self._clock = clock or datetime.now
        self._catalogs: dict[str, CatalogVersion] = {}
        self._mappings: dict[str, MappingVersion] = {}
        self._proposals: dict[str, Proposal] = {}
        self._claims: dict[str, dict] = {}
        self._queue: dict[str, QueueItem] = {}
        self._corrections: dict[str, dict] = {}
        self._shadow_runs: dict[str, dict] = {}
        self._batches: dict[str, dict] = {}
        self._audit: list[dict] = []
        self._seq = 0

    # -- 基础工具 --------------------------------------------------------

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._clock():%Y%m%d}-{self._seq:04d}"

    def _audit_event(self, action: str, target: str, detail: dict) -> None:
        self._audit.append(
            {
                "at": self._clock().isoformat(),
                "action": action,
                "target": target,
                "detail": detail,
            }
        )

    def audit_log(self) -> list[dict]:
        with self._lock:
            return list(self._audit)

    @staticmethod
    def _build_groups(raw_groups: Iterable[dict]) -> list[MappingGroup]:
        groups: list[MappingGroup] = []
        for raw in raw_groups or []:
            links = raw.get("links")
            if not links:
                raise DomainError("映射组必须包含 links")
            link_tuple = tuple(
                MappingLink(
                    national_code=str(lk["national_code"]),
                    provincial_code=str(lk["provincial_code"]),
                )
                for lk in links
            )
            group = MappingGroup(links=link_tuple, note=str(raw.get("note", "")))
            if not group.national_codes or not group.provincial_codes:
                raise DomainError("映射组两端都必须有项目编码")
            if group.relation == "many_to_many":
                raise DomainError(
                    "不支持多对多映射，请拆分为多个一对多/多对一组并说明当量关系"
                )
            groups.append(group)
        if not groups:
            raise DomainError("提案至少要包含一个映射组")
        return groups

    # -- 目录版本 --------------------------------------------------------

    def create_catalog_version(
        self,
        *,
        level: str,
        region: str | None,
        version: str,
        effective_from: str | date,
        effective_to: str | date | None,
        projects: list[dict],
    ) -> dict:
        if level == NATIONAL_LEVEL:
            catalog_id = NATIONAL_CATALOG
            if region:
                raise DomainError("国家目录不归属任何省份")
        elif level == PROVINCIAL_LEVEL:
            if not region:
                raise DomainError("省级目录必须提供省份编码")
            catalog_id = f"PROV-{region}"
        else:
            raise DomainError("level 必须是 national 或 provincial")

        start = parse_date(effective_from, name="effective_from")
        end = parse_date(effective_to, name="effective_to")
        if start is None:
            raise DomainError("effective_from 必填")
        if end and end < start:
            raise DomainError("生效结束时间不能早于开始时间")

        built: dict[str, Project] = {}
        for item in projects or []:
            category = item["category"]
            if category not in SERVICE_CATEGORIES:
                raise DomainError(f"未知服务类别：{category}")
            project = Project(
                code=str(item["code"]),
                name=str(item["name"]),
                category=category,
                unit=str(item.get("unit", "次")),
                price=d(item.get("price", "0")),
                payment_limit=item.get("payment_limit"),
            )
            if project.code in built:
                raise DomainError(f"版本内项目编码重复：{project.code}")
            built[project.code] = project

        with self._lock:
            if catalog_id + "|" + version in {
                f"{c.catalog_id}|{c.version}" for c in self._catalogs.values()
            }:
                raise StateConflictError(f"目录版本已存在：{catalog_id}@{version}")
            cv = CatalogVersion(
                catalog_id=catalog_id,
                version=version,
                level=level,
                region=region,
                effective_from=start,
                effective_to=end,
                projects=built,
                created_at=self._clock(),
            )
            self._catalogs[cv.id] = cv
            self._audit_event(
                "catalog_version_created",
                cv.id,
                {"project_count": len(built), "effective_from": start.isoformat()},
            )
            return cv.to_dict()

    def publish_catalog_version(self, catalog_id: str, version: str) -> dict:
        with self._lock:
            cv = self._require_catalog(catalog_id, version)
            if cv.withdrawn:
                raise StateConflictError("已撤回版本不能发布")
            if cv.published:
                raise StateConflictError("版本已发布")
            for other in self._catalogs.values():
                if (
                    other is not cv
                    and other.catalog_id == cv.catalog_id
                    and other.published
                    and not other.withdrawn
                    and _overlaps(
                        cv.effective_from,
                        cv.effective_to,
                        other.effective_from,
                        other.effective_to,
                    )
                ):
                    raise StateConflictError(
                        f"生效区间与已发布版本 {other.id} 重叠："
                        f"{other.effective_from}~{other.effective_to or '长期'}"
                    )
            cv.published = True
            cv.published_at = self._clock()
            self._audit_event("catalog_version_published", cv.id, {})
            return cv.to_dict()

    def withdraw_catalog_version(self, catalog_id: str, version: str) -> dict:
        """只允许撤回尚未生效的待生效版本。"""
        with self._lock:
            cv = self._require_catalog(catalog_id, version)
            if not cv.published or cv.withdrawn:
                raise StateConflictError("只有已发布且未撤回的版本可以撤回")
            today = self._clock().date()
            if cv.effective_from <= today:
                raise StateConflictError(
                    "版本已经或正在生效，不能撤回；请发布新版本进行衔接"
                )
            cv.withdrawn = True
            self._audit_event("catalog_version_withdrawn", cv.id, {})
            return cv.to_dict()

    def _require_catalog(self, catalog_id: str, version: str) -> CatalogVersion:
        cv = self._catalogs.get(f"{catalog_id}@{version}")
        if cv is None:
            raise NotFoundError(f"目录版本不存在：{catalog_id}@{version}")
        return cv

    def list_catalog_versions(
        self, *, level: str | None = None, region: str | None = None
    ) -> list[dict]:
        with self._lock:
            result = []
            for cv in self._catalogs.values():
                if level and cv.level != level:
                    continue
                if region and cv.region != region:
                    continue
                result.append(cv.to_dict())
            return sorted(result, key=lambda x: (x["catalog_id"], x["effective_from"]))

    def get_catalog_version(self, catalog_id: str, version: str) -> dict:
        with self._lock:
            return self._require_catalog(catalog_id, version).to_dict(
                include_projects=True
            )

    def _effective_catalog(self, catalog_id: str, day: date) -> CatalogVersion | None:
        matches = [
            cv
            for cv in self._catalogs.values()
            if cv.catalog_id == catalog_id and cv.covers(day)
        ]
        if len(matches) > 1:
            raise StateConflictError(
                f"{catalog_id} 在 {day} 存在多个有效版本，目录治理数据损坏"
            )
        return matches[0] if matches else None

    # -- 映射提案与双审 --------------------------------------------------

    def _validate_groups_against_catalogs(
        self,
        national_version: CatalogVersion,
        region: str,
        groups: list[MappingGroup],
    ) -> None:
        """硬校验：编码必须存在，提案内部不得交叉，多对多拒绝。"""
        provincial_codes: set[str] = set()
        for cv in self._catalogs.values():
            if cv.catalog_id == f"PROV-{region}" and cv.published and not cv.withdrawn:
                provincial_codes.update(cv.projects)

        seen_national: set[str] = set()
        seen_provincial: set[str] = set()
        for index, group in enumerate(groups, start=1):
            for code in group.national_codes:
                if code not in national_version.projects:
                    raise DomainError(
                        f"第{index}组国家项目 {code} 不在 {national_version.id} 中"
                    )
                if code in seen_national:
                    raise DomainError(
                        f"国家项目 {code} 出现在多个映射组中，请合并或说明"
                    )
                seen_national.add(code)
            for code in group.provincial_codes:
                if not provincial_codes:
                    raise DomainError(f"省份 {region} 还没有已发布的省级目录")
                if code not in provincial_codes:
                    raise DomainError(
                        f"第{index}组省级项目 {code} 不在 {region} 已发布目录中"
                    )
                if code in seen_provincial:
                    raise DomainError(
                        f"省级项目 {code} 出现在多个映射组中，映射必须唯一"
                    )
                seen_provincial.add(code)

    def _detect_conflicts(
        self,
        national_version_id: str,
        region: str,
        groups: list[MappingGroup],
        intended_from: date,
        intended_to: date | None,
    ) -> list[str]:
        """软冲突：列出但不阻断提交，供业务与财务审核参考。

        1. 拟生效区间内同一省级项目已被其他映射版本覆盖；
        2. 一对多/多对一关系本身需要业务确认内涵、财务确认拆分/打包当量。
        """
        conflicts: list[str] = []
        interval = (intended_from, intended_to)
        for group in groups:
            if group.relation in (ONE_TO_MANY, MANY_TO_ONE):
                conflicts.append(
                    f"建议为{RELATION_LABELS[group.relation]}关系（{list(group.national_codes)}"
                    f"↔{list(group.provincial_codes)}），需业务确认内涵、财务确认当量"
                )
        for mv in self._mappings.values():
            if (
                mv.region != region
                or not mv.published
                or mv.withdrawn
                or not _overlaps(
                    intended_from, intended_to, mv.effective_from, mv.effective_to
                )
            ):
                continue
            existing_codes = {
                code for existing in mv.groups for code in existing.provincial_codes
            }
            for group in groups:
                dup = sorted(set(group.provincial_codes) & existing_codes)
                if dup:
                    conflicts.append(
                        f"省级项目 {dup} 在区间 "
                        f"{mv.effective_from}~{mv.effective_to or '长期'} 内已由 "
                        f"{mv.mapping_version_id}（对应 {mv.national_catalog_version}）映射，"
                        f"属跨国家目录版本衔接，请明确替换与历史解释口径"
                    )
        return conflicts

    def submit_proposal(self, payload: dict) -> dict:
        national_catalog_id = payload.get("national_catalog_id", NATIONAL_CATALOG)
        national_version_str = payload.get("national_version")
        region = payload.get("region")
        if not region:
            raise DomainError("提案必须指定省份 region")
        start = parse_date(payload.get("intended_from"), name="intended_from")
        end = parse_date(payload.get("intended_to"), name="intended_to")
        if start is None:
            raise DomainError("intended_from 必填")
        evidence = payload.get("evidence")
        if (
            not isinstance(evidence, list)
            or not evidence
            or not all(str(e).strip() for e in evidence)
        ):
            raise DomainError("提交映射建议必须列出依据（evidence 非空数组）")
        groups = self._build_groups(payload.get("groups"))

        with self._lock:
            national_version = self._require_catalog(
                national_catalog_id, national_version_str
            )
            self._validate_groups_against_catalogs(national_version, region, groups)
            conflicts = self._detect_conflicts(
                national_version.id, region, groups, start, end
            )
            proposal = Proposal(
                proposal_id=self._next_id("PRP"),
                proposer=str(payload.get("proposer", "匿名专家")),
                national_catalog_version=national_version.id,
                region=region,
                title=str(payload.get("title", "映射建议")),
                groups=groups,
                evidence=[str(e) for e in evidence],
                conflicts=conflicts,
                intended_from=start,
                intended_to=end,
                submitted_at=self._clock(),
            )
            self._proposals[proposal.proposal_id] = proposal
            self._audit_event(
                "proposal_submitted",
                proposal.proposal_id,
                {
                    "region": region,
                    "groups": len(groups),
                    "conflicts": len(conflicts),
                },
            )
            return proposal.to_dict()

    def review_proposal(
        self,
        proposal_id: str,
        *,
        kind: str,
        decision: str,
        reviewer: str,
        comment: str = "",
    ) -> dict:
        if kind not in ("business", "finance"):
            raise DomainError("审核类型必须是 business 或 finance")
        if decision not in ("approved", "rejected"):
            raise DomainError("审核结论必须是 approved 或 rejected")
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is None:
                raise NotFoundError(f"提案不存在：{proposal_id}")
            if proposal.status == "rejected":
                raise StateConflictError("提案已被拒绝，审核流程结束")
            if proposal.status == "published":
                raise StateConflictError("提案已发布为映射版本")
            review = Review(
                kind=kind,
                decision=decision,
                reviewer=reviewer,
                comment=comment,
                reviewed_at=self._clock(),
            )
            if kind == "business":
                if proposal.business_review:
                    raise StateConflictError("业务审核已完成")
                proposal.business_review = review
            else:
                if proposal.finance_review:
                    raise StateConflictError("财务审核已完成")
                proposal.finance_review = review

            if decision == "rejected":
                proposal.status = "rejected"
            elif proposal.business_review and proposal.finance_review:
                if (
                    proposal.business_review.decision == "approved"
                    and proposal.finance_review.decision == "approved"
                ):
                    proposal.status = "approved"
            self._audit_event(
                "proposal_reviewed",
                proposal_id,
                {"kind": kind, "decision": decision, "reviewer": reviewer},
            )
            return proposal.to_dict()

    def publish_mapping(
        self,
        proposal_id: str,
        *,
        version: str,
        effective_from: str | date,
        effective_to: str | date | None = None,
    ) -> dict:
        start = parse_date(effective_from, name="effective_from")
        end = parse_date(effective_to, name="effective_to")
        if start is None:
            raise DomainError("effective_from 必填")
        if end and end < start:
            raise DomainError("生效结束时间不能早于开始时间")
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is None:
                raise NotFoundError(f"提案不存在：{proposal_id}")
            if proposal.status != "approved":
                raise StateConflictError(
                    "只有业务与财务审核均通过的提案才能发布"
                )
            national_cv = self._catalogs.get(proposal.national_catalog_version)
            if national_cv is None or not national_cv.published:
                raise StateConflictError("对应的国家目录版本尚未发布")
            self._validate_groups_against_catalogs(
                national_cv, proposal.region, proposal.groups
            )

            mapping_id = f"MAP-{proposal.region}-{version}"
            if mapping_id in self._mappings:
                raise StateConflictError(f"映射版本号已存在：{mapping_id}")
            for mv in self._mappings.values():
                if (
                    mv.region != proposal.region
                    or mv.national_catalog_version != proposal.national_catalog_version
                    or not mv.published
                    or mv.withdrawn
                ):
                    continue
                # 同一国家目录版本下，区间必须首尾衔接，不能同时存在两套映射。
                # 跨国家目录版本（年度换版）允许重叠：结算时按就医时点有效的
                # 国家目录决定使用哪一套，不存在歧义。
                if _overlaps(start, end, mv.effective_from, mv.effective_to):
                    existing_codes = {
                        code for g in mv.groups for code in g.provincial_codes
                    }
                    for group in proposal.groups:
                        dup = set(group.provincial_codes) & existing_codes
                        if dup:
                            raise StateConflictError(
                                f"省级项目 {sorted(dup)} 在同一国家版本 "
                                f"{proposal.national_catalog_version} 下已由 "
                                f"{mv.mapping_version_id} 映射，区间不得重叠"
                            )

            mv = MappingVersion(
                mapping_version_id=mapping_id,
                national_catalog_version=proposal.national_catalog_version,
                region=proposal.region,
                version=version,
                effective_from=start,
                effective_to=end,
                groups=list(proposal.groups),
                published=True,
                source_proposal_id=proposal_id,
                published_at=self._clock(),
            )
            self._mappings[mapping_id] = mv
            proposal.status = "published"
            proposal.published_mapping_version_id = mapping_id
            self._audit_event(
                "mapping_published",
                mapping_id,
                {"proposal_id": proposal_id, "region": mv.region},
            )
            return mv.to_dict()

    def withdraw_mapping_version(self, mapping_version_id: str) -> dict:
        with self._lock:
            mv = self._mappings.get(mapping_version_id)
            if mv is None:
                raise NotFoundError(f"映射版本不存在：{mapping_version_id}")
            if not mv.published or mv.withdrawn:
                raise StateConflictError("只有已发布且未撤回的版本可以撤回")
            today = self._clock().date()
            if mv.effective_from <= today:
                raise StateConflictError("映射版本已生效，不能撤回；请发布新版本衔接")
            mv.withdrawn = True
            self._audit_event("mapping_withdrawn", mapping_version_id, {})
            return mv.to_dict()

    def list_mapping_versions(self, *, region: str | None = None) -> list[dict]:
        with self._lock:
            result = [
                mv.to_dict(include_groups=False)
                for mv in self._mappings.values()
                if not region or mv.region == region
            ]
            return sorted(result, key=lambda x: (x["region"], x["effective_from"]))

    def list_proposals(self, *, status: str | None = None) -> list[dict]:
        with self._lock:
            return [
                p.to_dict(include_groups=False)
                for p in self._proposals.values()
                if not status or p.status == status
            ]

    def get_proposal(self, proposal_id: str) -> dict:
        with self._lock:
            p = self._proposals.get(proposal_id)
            if p is None:
                raise NotFoundError(f"提案不存在：{proposal_id}")
            return p.to_dict()

    # -- 时点解析 --------------------------------------------------------

    def _effective_mapping(
        self, national_version_id: str, region: str, day: date
    ) -> MappingVersion | None:
        matches = [
            mv
            for mv in self._mappings.values()
            if mv.national_catalog_version == national_version_id
            and mv.region == region
            and mv.covers(day)
            and mv.kind == "published"
        ]
        if len(matches) > 1:
            raise StateConflictError(
                f"{region} 映射在 {day} 存在多个有效版本，数据损坏"
            )
        return matches[0] if matches else None

    def _manual_mapping(
        self,
        region: str,
        provincial_code: str,
        day: date,
        national_version_id: str,
    ) -> MappingVersion | None:
        for mv in self._mappings.values():
            if (
                mv.kind == "manual"
                and mv.region == region
                and mv.national_catalog_version == national_version_id
                and mv.covers(day)
                and mv.group_for(provincial_code)
            ):
                return mv
        return None

    def resolve_line(
        self, *, care_region: str, service_date: date, provincial_code: str
    ) -> dict:
        """纯解析：返回该省项目在就医时点命中的全部版本与国家项目（不入队、不写库）。"""
        with self._lock:
            national_cv = self._effective_catalog(NATIONAL_CATALOG, service_date)
            versions = {
                "national_catalog": national_cv.id if national_cv else None,
                "provincial_catalog": None,
                "mapping": None,
                "resolution_basis": None,
            }
            if national_cv is None:
                return {
                    "status": "unresolved",
                    "reason": REASON_NO_NATIONAL_VERSION,
                    "provincial_code": provincial_code,
                    "versions": versions,
                    "national_projects": [],
                }
            prov_cv = self._effective_catalog(f"PROV-{care_region}", service_date)
            versions["provincial_catalog"] = prov_cv.id if prov_cv else None
            if prov_cv is None:
                return {
                    "status": "unresolved",
                    "reason": REASON_NO_PROVINCIAL_VERSION,
                    "provincial_code": provincial_code,
                    "versions": versions,
                    "national_projects": [],
                }
            provincial_project = prov_cv.projects.get(provincial_code)
            mapping_mv = self._effective_mapping(national_cv.id, care_region, service_date)
            versions["mapping"] = mapping_mv.id if mapping_mv else None

            group = mapping_mv.group_for(provincial_code) if mapping_mv else None
            if group is None:
                # 正式映射没有，再看人工裁决是否已经给过口径。
                # 人工口径绑定登记时的国家目录版本，换版后自动失效，需要重新裁决。
                manual = self._manual_mapping(
                    care_region, provincial_code, service_date, national_cv.id
                )
                if manual is not None:
                    group = manual.group_for(provincial_code)
                    versions["mapping"] = manual.id
                    versions["resolution_basis"] = "manual_adjudication"
                else:
                    reason = (
                        REASON_NO_MAPPING_VERSION
                        if mapping_mv is None
                        else REASON_UNMAPPED_CODE
                    )
                    return {
                        "status": "unresolved",
                        "reason": reason,
                        "provincial_code": provincial_code,
                        "provincial_project": (
                            provincial_project.to_dict() if provincial_project else None
                        ),
                        "versions": versions,
                        "national_projects": [],
                    }
            else:
                versions["resolution_basis"] = "published_mapping"

            national_projects = [
                national_cv.projects[code].to_dict()
                for code in group.national_codes
                if code in national_cv.projects
            ]
            missing = [
                code for code in group.national_codes if code not in national_cv.projects
            ]
            if missing:
                return {
                    "status": "unresolved",
                    "reason": REASON_UNMAPPED_CODE,
                    "detail": f"映射引用的国家项目已不存在：{missing}",
                    "provincial_code": provincial_code,
                    "versions": versions,
                    "national_projects": [],
                }
            return {
                "status": "resolved",
                "provincial_code": provincial_code,
                "provincial_project": (
                    provincial_project.to_dict() if provincial_project else None
                ),
                "relation": group.relation,
                "relation_label": RELATION_LABELS[group.relation],
                "bundle": group.relation == MANY_TO_ONE,
                "note": group.note,
                "national_projects": national_projects,
                "versions": versions,
            }

    # -- 正式结算 --------------------------------------------------------

    def settle(self, payload: dict) -> dict:
        claim = self._build_claim(payload)
        with self._lock:
            return self._persist_claim(claim)

    def _build_claim(self, payload: dict) -> dict:
        care_region = payload.get("care_region")
        insured_region = payload.get("insured_region")
        if not care_region or not insured_region:
            raise DomainError("结算请求必须提供参保地与就医地")
        header_date = parse_date(payload.get("service_date"), name="service_date")
        if header_date is None:
            raise DomainError("结算请求必须提供 service_date")
        raw_lines = payload.get("lines")
        if not raw_lines:
            raise DomainError("结算请求至少包含一条费用明细")

        lines: list[dict] = []
        for index, raw in enumerate(raw_lines, start=1):
            line_date = parse_date(raw.get("service_date"), name="line service_date") or header_date
            qty = int(raw.get("quantity", 1))
            if qty <= 0:
                raise DomainError(f"第{index}行数量必须为正")
            charged = d(raw.get("charged_amount"))
            lines.append(
                {
                    "line_id": str(raw.get("line_id") or f"L{index}"),
                    "service_date": line_date,
                    "provincial_code": str(raw["provincial_code"]),
                    "quantity": qty,
                    "charged_amount": charged,
                }
            )
        return {
            "claim_id": payload.get("claim_id") or f"CLM-{uuid.uuid4().hex[:12]}",
            "insured_region": insured_region,
            "care_region": care_region,
            "submitted_at": self._clock().isoformat(),
            "lines": lines,
        }

    def _persist_claim(self, claim: dict) -> dict:
        record, items = self._plan_claim(claim)
        self._claims[record["claim_id"]] = record
        for item in items:
            self._queue[item.queue_id] = item
        self._audit_event(
            "claim_settled" if record["status"] == "settled" else "claim_manual_queued",
            record["claim_id"],
            {"status": record["status"], "lines": len(record["lines"])},
        )
        return record

    @staticmethod
    def _version_summary(lines: list[dict]) -> dict:
        national = sorted({l["versions"]["national_catalog"] for l in lines if l["versions"]["national_catalog"]})
        provincial = sorted({l["versions"]["provincial_catalog"] for l in lines if l["versions"]["provincial_catalog"]})
        mappings = sorted({l["versions"]["mapping"] for l in lines if l["versions"]["mapping"]})
        return {
            "national_catalog_versions": national,
            "provincial_catalog_versions": provincial,
            "mapping_versions": mappings,
        }

    def get_settlement(self, claim_id: str) -> dict:
        with self._lock:
            record = self._claims.get(claim_id)
            if record is None:
                raise NotFoundError(f"结算记录不存在：{claim_id}")
            result = dict(record)
            corrections = [
                c for c in self._corrections.values() if c["original_claim_id"] == claim_id
            ]
            result["corrections"] = corrections
            return result

    def reverse_settlement(
        self, claim_id: str, *, reason: str, operator: str
    ) -> dict:
        """冲正：原单不动，追加一条金额相反的冲正记录。"""
        if not reason or not str(reason).strip():
            raise DomainError("冲正必须填写原因")
        with self._lock:
            record = self._claims.get(claim_id)
            if record is None:
                raise NotFoundError(f"结算记录不存在：{claim_id}")
            if record["status"] != "settled":
                raise StateConflictError("只有已正式结算的单据可以冲正")
            if record.get("reversal_status") == "reversed":
                raise StateConflictError("该单据已冲正，不能重复冲正")
            correction_id = self._next_id("COR")
            correction = {
                "correction_id": correction_id,
                "original_claim_id": claim_id,
                "reason": reason,
                "operator": operator,
                "created_at": self._clock().isoformat(),
                "reversed_charged_amount": str(
                    -Decimal(record["total_charged_amount"]).quantize(Decimal("0.01"))
                ),
                "reversed_baseline_amount": str(
                    -Decimal(record["total_baseline_amount"]).quantize(Decimal("0.01"))
                ),
                "version_summary": record["version_summary"],
            }
            record["reversal_status"] = "reversed"
            self._corrections[correction_id] = correction
            self._audit_event("settlement_reversed", claim_id, {"correction_id": correction_id})
            return correction

    def list_settlements(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "claim_id": r["claim_id"],
                    "status": r["status"],
                    "care_region": r["care_region"],
                    "insured_region": r["insured_region"],
                    "submitted_at": r["submitted_at"],
                    "reversal_status": r.get("reversal_status"),
                    "version_summary": r["version_summary"],
                    "total_baseline_amount": r["total_baseline_amount"],
                }
                for r in self._claims.values()
            ]

    # -- 人工队列 --------------------------------------------------------

    def list_queue(self, *, status: str = "open") -> list[dict]:
        with self._lock:
            return [
                item.to_dict()
                for item in self._queue.values()
                if status == "all" or item.status == status
            ]

    def resolve_queue_item(
        self,
        queue_id: str,
        *,
        decision: str,
        operator: str,
        note: str,
        national_codes: list[str] | None = None,
        effective_from: str | date | None = None,
        effective_to: str | date | None = None,
    ) -> dict:
        """人工处理：map = 给出人工口径（登记为 MANUAL 映射版本，可审计、可重算）；
        reject = 该费用不予支付。处理后原单仍保留，需要作为新单据重新提交。"""
        if decision not in ("map", "reject"):
            raise DomainError("decision 必须是 map 或 reject")
        with self._lock:
            item = self._queue.get(queue_id)
            if item is None:
                raise NotFoundError(f"队列项不存在：{queue_id}")
            if item.status != "open":
                raise StateConflictError("队列项已处理")
            resolution = {
                "decision": decision,
                "operator": operator,
                "note": note,
                "resolved_at": self._clock().isoformat(),
            }
            if decision == "map":
                if not national_codes:
                    raise DomainError("人工映射必须提供 national_codes")
                start = parse_date(effective_from, name="effective_from") or item.service_date
                end = parse_date(effective_to, name="effective_to")
                national_version_id = item.national_catalog_version
                if national_version_id is None:
                    raise StateConflictError("缺少国家目录版本，无法登记人工口径")
                national_cv = self._catalogs.get(national_version_id)
                for code in national_codes:
                    if code not in national_cv.projects:
                        raise DomainError(f"国家项目 {code} 不在 {national_version_id}")
                manual_id = self._next_id("MANUAL")
                group = MappingGroup(
                    links=tuple(
                        MappingLink(national_code=code, provincial_code=item.provincial_code)
                        for code in national_codes
                    ),
                    note=f"人工裁决：{note}",
                )
                mv = MappingVersion(
                    mapping_version_id=manual_id,
                    national_catalog_version=national_version_id,
                    region=item.care_region,
                    version=manual_id,
                    effective_from=start,
                    effective_to=end,
                    groups=[group],
                    published=True,
                    published_at=self._clock(),
                    kind="manual",
                )
                self._mappings[manual_id] = mv
                resolution["manual_mapping_version_id"] = manual_id
                resolution["effective_from"] = start.isoformat()
                resolution["effective_to"] = end.isoformat() if end else None
                self._audit_event(
                    "manual_mapping_registered",
                    manual_id,
                    {"queue_id": queue_id, "operator": operator},
                )
            item.status = "closed"
            item.resolution = resolution
            self._audit_event("queue_closed", queue_id, {"decision": decision})
            return item.to_dict()

    # -- 影子回放 --------------------------------------------------------

    def shadow_replay(self, bills: list[dict], *, label: str = "") -> dict:
        """影子模式：回放历史账单，只出覆盖率/支付差异报告，不产生正式结算与队列。

        整个回放持锁，保证报告对应某一个治理快照，不会与并发发布互相撕裂。
        """
        with self._lock:
            return self._shadow_replay_impl(bills, label=label)

    def _shadow_replay_impl(self, bills: list[dict], *, label: str = "") -> dict:
        detail_rows: list[dict] = []
        covered = 0
        total_charged = Decimal("0.00")
        covered_charged = Decimal("0.00")
        total_baseline = Decimal("0.00")
        gap_charged = Decimal("0.00")
        category_stat: dict[str, dict] = {}
        reason_stat: dict[str, int] = {}

        for bill in bills or []:
            claim = self._build_claim(bill)
            for line in claim["lines"]:
                resolved = self.resolve_line(
                    care_region=claim["care_region"],
                    service_date=line["service_date"],
                    provincial_code=line["provincial_code"],
                )
                total_charged += line["charged_amount"]
                row = {
                    "claim_id": claim["claim_id"],
                    "line_id": line["line_id"],
                    "service_date": line["service_date"].isoformat(),
                    "care_region": claim["care_region"],
                    "provincial_code": line["provincial_code"],
                    "charged_amount": money(line["charged_amount"]),
                    "versions": resolved["versions"],
                }
                category = (resolved.get("provincial_project") or {}).get("category", "未知")
                stat = category_stat.setdefault(
                    category,
                    {"total": 0, "covered": 0, "charged": Decimal("0.00"),
                     "covered_charged": Decimal("0.00"),
                     "baseline": Decimal("0.00"), "gap_charged": Decimal("0.00")},
                )
                stat["total"] += 1
                stat["charged"] += line["charged_amount"]
                if resolved["status"] == "resolved":
                    covered += 1
                    covered_charged += line["charged_amount"]
                    stat["covered"] += 1
                    stat["covered_charged"] += line["charged_amount"]
                    unit = sum(
                        (Decimal(p["price"]) for p in resolved["national_projects"]),
                        Decimal("0.00"),
                    )
                    baseline = (unit * line["quantity"]).quantize(Decimal("0.01"))
                    total_baseline += baseline
                    stat["baseline"] += baseline
                    row.update(
                        {
                            "status": "covered",
                            "relation": resolved["relation"],
                            "national_projects": resolved["national_projects"],
                            "baseline_amount": money(baseline),
                            "difference_amount": signed_money(
                                line["charged_amount"] - baseline
                            ),
                        }
                    )
                else:
                    gap_charged += line["charged_amount"]
                    stat["gap_charged"] += line["charged_amount"]
                    reason_stat[resolved["reason"]] = (
                        reason_stat.get(resolved["reason"], 0) + 1
                    )
                    row.update(
                        {
                            "status": "gap",
                            "reason": resolved["reason"],
                            "reason_label": REASON_LABELS[resolved["reason"]],
                            "baseline_amount": None,
                            "difference_amount": None,
                        }
                    )
                detail_rows.append(row)

        total_lines = len(detail_rows)
        categories_report = []
        for cat, stat in sorted(category_stat.items()):
            categories_report.append(
                {
                    "category": cat,
                    "total_lines": stat["total"],
                    "covered_lines": stat["covered"],
                    "coverage_rate": (
                        round(stat["covered"] / stat["total"], 4) if stat["total"] else 0
                    ),
                    "charged_amount": money(stat["charged"]),
                    "baseline_amount": money(stat["baseline"]),
                    # 差异只在覆盖口径上计算；缺口行金额单列，不混入支付差异。
                    "difference_amount": signed_money(
                        stat["covered_charged"] - stat["baseline"]
                    ),
                    "gap_charged_amount": money(stat["gap_charged"]),
                }
            )
        run_id = self._next_id("SHD")
        run = {
            "run_id": run_id,
            "label": label,
            "ran_at": self._clock().isoformat(),
            "mode": "shadow",
            "total_lines": total_lines,
            "covered_lines": covered,
            "gap_lines": total_lines - covered,
            "coverage_rate": round(covered / total_lines, 4) if total_lines else 0,
            "total_charged_amount": money(total_charged),
            "covered_charged_amount": money(covered_charged),
            "total_baseline_amount": money(total_baseline),
            # 支付差异仅针对已覆盖行：实收合计 - 国家基准合计。
            "total_difference_amount": signed_money(covered_charged - total_baseline),
            "gap_charged_amount": money(gap_charged),
            "gap_reasons": [
                {"reason": r, "reason_label": REASON_LABELS[r], "count": c}
                for r, c in sorted(reason_stat.items())
            ],
            "by_category": categories_report,
            "lines": detail_rows,
        }
        self._shadow_runs[run_id] = run
        self._audit_event(
            "shadow_replay",
            run_id,
            {"label": label, "total": total_lines, "covered": covered},
        )
        return run

    def list_shadow_runs(self) -> list[dict]:
        with self._lock:
            return [
                {k: v for k, v in run.items() if k != "lines"}
                for run in self._shadow_runs.values()
            ]

    def get_shadow_run(self, run_id: str) -> dict:
        with self._lock:
            run = self._shadow_runs.get(run_id)
            if run is None:
                raise NotFoundError(f"影子运行不存在：{run_id}")
            return run

    # -- 跨日跑批 --------------------------------------------------------

    def run_batch(
        self,
        requests_: list[dict],
        *,
        label: str = "",
        fail_after: int | None = None,
    ) -> dict:
        """批量结算：先构造全部单据，再在同一把锁内规划并一次性落库。
        fail_after 用于演练失败回滚：在规划指定条数后抛出，整批不留任何记录。"""
        # 锁外只做输入解析，不触碰任何版本状态。
        claims = [self._build_claim(req) for req in requests_]
        with self._lock:
            batch_id = self._next_id("BCH")
            # 第一阶段：纯计算，任何一条报错都不动状态。
            planned: list[tuple[dict, list[QueueItem]]] = []
            for order, claim in enumerate(claims):
                record, items = self._plan_claim(claim)
                planned.append((record, items))
                if fail_after is not None and order + 1 == fail_after:
                    raise DomainError(
                        f"演练注入失败：第 {fail_after} 条之后中止，整批回滚（{batch_id}）"
                    )
            # 第二阶段：统一提交。
            for record, items in planned:
                self._claims[record["claim_id"]] = record
                for item in items:
                    self._queue[item.queue_id] = item
                self._audit_event(
                    "claim_settled" if record["status"] == "settled" else "claim_manual_queued",
                    record["claim_id"],
                    {"batch_id": batch_id},
                )
            settled = sum(1 for r, _ in planned if r["status"] == "settled")
            queued = len(planned) - settled
            batch = {
                "batch_id": batch_id,
                "label": label,
                "ran_at": self._clock().isoformat(),
                "total": len(planned),
                "settled": settled,
                "manual_review": queued,
                "claim_ids": [r["claim_id"] for r, _ in planned],
            }
            self._batches[batch_id] = batch
            self._audit_event("batch_completed", batch_id, {"total": len(planned)})
            batch["results"] = [r for r, _ in planned]
            return batch

    def _plan_claim(self, claim: dict) -> tuple[dict, list[QueueItem]]:
        """与 _persist_claim 等价但不直接落库，便于批量原子提交。"""
        result_lines: list[dict] = []
        queue_items: list[QueueItem] = []
        all_resolved = True
        total_charged = Decimal("0.00")
        total_baseline = Decimal("0.00")

        for line in claim["lines"]:
            resolved = self.resolve_line(
                care_region=claim["care_region"],
                service_date=line["service_date"],
                provincial_code=line["provincial_code"],
            )
            total_charged += line["charged_amount"]
            row = {
                "line_id": line["line_id"],
                "service_date": line["service_date"].isoformat(),
                "provincial_code": line["provincial_code"],
                "quantity": line["quantity"],
                "charged_amount": money(line["charged_amount"]),
                "versions": resolved["versions"],
            }
            if resolved["status"] != "resolved":
                all_resolved = False
                row["status"] = "unresolved"
                row["reason"] = resolved["reason"]
                row["reason_label"] = REASON_LABELS[resolved["reason"]]
                item = QueueItem(
                    queue_id=self._next_id("QUE"),
                    claim_id=claim["claim_id"],
                    created_at=self._clock(),
                    care_region=claim["care_region"],
                    insured_region=claim["insured_region"],
                    service_date=line["service_date"],
                    provincial_code=line["provincial_code"],
                    line_id=line["line_id"],
                    reason=resolved["reason"],
                    detail=resolved.get("detail", REASON_LABELS[resolved["reason"]]),
                    national_catalog_version=resolved["versions"]["national_catalog"],
                    provincial_catalog_version=resolved["versions"]["provincial_catalog"],
                    mapping_version=resolved["versions"]["mapping"],
                )
                queue_items.append(item)
                row["queue_id"] = item.queue_id
            else:
                unit_baseline = sum(
                    (Decimal(p["price"]) for p in resolved["national_projects"]),
                    Decimal("0.00"),
                )
                baseline = (unit_baseline * line["quantity"]).quantize(Decimal("0.01"))
                total_baseline += baseline
                row.update(
                    {
                        "status": "resolved",
                        "relation": resolved["relation"],
                        "relation_label": resolved["relation_label"],
                        "bundle": resolved["bundle"],
                        "provincial_project": resolved.get("provincial_project"),
                        "national_projects": resolved["national_projects"],
                        "baseline_amount": money(baseline),
                        "difference_amount": signed_money(line["charged_amount"] - baseline),
                        "resolution_basis": resolved["versions"]["resolution_basis"],
                    }
                )
            result_lines.append(row)

        status = "settled" if all_resolved else "manual_review"
        record = {
            "claim_id": claim["claim_id"],
            "insured_region": claim["insured_region"],
            "care_region": claim["care_region"],
            "submitted_at": claim["submitted_at"],
            "status": status,
            "lines": result_lines,
            "total_charged_amount": money(total_charged),
            "total_baseline_amount": money(total_baseline if all_resolved else Decimal("0.00")),
            "total_difference_amount": (
                signed_money(total_charged - total_baseline) if all_resolved else None
            ),
            "version_summary": self._version_summary(result_lines),
        }
        if status == "settled":
            record["reversal_status"] = "active"
        return record, queue_items

    def get_batch(self, batch_id: str) -> dict:
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                raise NotFoundError(f"批次不存在：{batch_id}")
            return dict(batch)

    # -- 种子装载（演示/测试用） ----------------------------------------

    def is_empty(self) -> bool:
        with self._lock:
            return not self._catalogs
