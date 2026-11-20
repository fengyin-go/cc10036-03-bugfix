"""分群圈选领域服务：条件解析、人群计算、结果生成。

圈选结果按版本提交：条件变化时生效新版本，条件不变时刷新当前版本快照，
历次实际圈选版本均可回查（供同期群分析报告对照）。计算成功后才一次性
提交分群规模与版本记录，失败恢复时不会残留另一批的注册总数。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store
from app.analytics_engine import parse_segment_conditions, segment_population

VERSION_TABLE = "segment_version"


class SegmentRuleService(BaseModuleService):
    MODULE = "segment_rule"
    ENTITY = "分群规则"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["分群名称", "人群类型", "筛选条件", "排除条件", "交集策略", "刷新周期", "分群规模"]
    REQUIRED_FIELDS = ["分群名称", "人群类型", "筛选条件"]
    STATUS_ORDER = ["草稿", "计算中", "已生效", "已过期"]
    ACTION_RULES = {"开始计算": "计算中", "生效分群": "已生效", "标记过期": "已过期"}
    NEGATIVE_ACTIONS = ["标记过期"]

    # ---- 业务方法 ----

    def compute_segment(
        self, rule_id: int, population: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """执行分群计算：解析条件并从人群中圈选出目标群体。

        分群列表与同期群分析共用本方法产出的同一份有效圈选：
        全量命中行 matched、命中成员 member_ids，sample 仅供预览。
        """
        rule = store.find(self.MODULE, rule_id)
        if rule is None:
            return {"ok": False, "message": f"分群规则 {rule_id} 不存在"}

        # 提交前快照已生效的注册总数；一旦本批计算失败，按快照恢复，
        # 避免残留另一批（未提交）的注册总数。
        committed_size = rule.get("分群规模", "0")
        try:
            include_expr = str(rule.get("筛选条件", ""))
            exclude_expr = str(rule.get("排除条件", ""))
            intersect_strategy = str(rule.get("交集策略", "all"))

            include_conditions = parse_segment_conditions(include_expr)
            exclude_conditions = parse_segment_conditions(exclude_expr)

            matched, count = segment_population(
                population, include_conditions, exclude_conditions, intersect_strategy,
            )
            member_ids = _member_ids_of(matched)

            # 全部计算成功后才开始提交：版本记录与分群规模一起生效。
            version = self._commit_selection_version(
                rule_id, include_expr, exclude_expr, intersect_strategy,
                count, member_ids,
            )
            rule["分群规模"] = str(count)
        except Exception as exc:  # noqa: BLE001 - 失败恢复必须兜底所有异常
            rule["分群规模"] = committed_size
            return {
                "ok": False,
                "message": f"分群计算失败，已恢复上一版有效圈选（分群规模={committed_size}）：{exc}",
            }

        self.run_action(rule_id, "生效分群")

        return {
            "ok": True,
            "rule_name": str(rule.get("分群名称", "")),
            "population_total": len(population),
            "matched_count": count,
            "match_rate": round(count / len(population) * 100, 2) if population else 0,
            "include_conditions": [vars(c) for c in include_conditions],
            "exclude_conditions": [vars(c) for c in exclude_conditions],
            "matched": matched,
            "member_ids": member_ids,
            "sample": matched[:5],
            "version": version["version"],
        }

    def _commit_selection_version(
        self, rule_id: int, include_expr: str, exclude_expr: str,
        intersect_strategy: str, count: int, member_ids: list[str],
    ) -> dict[str, Any]:
        """提交一次有效圈选版本。

        条件签名（筛选/排除/交集策略）不变时刷新当前版本快照；
        条件变化时生效新版本，旧版本保留供报告回查。
        """
        versions = store.rows(VERSION_TABLE)
        own = [v for v in versions if int(v.get("rule_id", 0)) == rule_id]
        latest = own[-1] if own else None
        same_conditions = (
            latest is not None
            and str(latest.get("筛选条件", "")) == include_expr
            and str(latest.get("排除条件", "")) == exclude_expr
            and str(latest.get("交集策略", "")) == intersect_strategy
        )
        if same_conditions:
            latest["分群规模"] = count
            latest["member_ids"] = list(member_ids)
            latest["computed_at"] = time.time()
            return latest
        record = {
            "id": max((int(v.get("id", 0)) for v in versions), default=0) + 1,
            "rule_id": rule_id,
            "version": (int(latest.get("version", 0)) if latest else 0) + 1,
            "筛选条件": include_expr,
            "排除条件": exclude_expr,
            "交集策略": intersect_strategy,
            "分群规模": count,
            "member_ids": list(member_ids),
            "computed_at": time.time(),
        }
        versions.append(record)
        return record

    def list_selection_versions(self, rule_id: int) -> list[dict[str, Any]]:
        """回查该分群历次实际生效的圈选版本。"""
        return [
            v for v in store.rows(VERSION_TABLE)
            if int(v.get("rule_id", 0)) == rule_id
        ]

    def get_active_segments(self) -> list[dict[str, Any]]:
        """获取所有已生效的分群（供同期群分析和归因模型消费）。"""
        return [
            row for row in store.rows(self.MODULE)
            if row.get("status") == "已生效"
        ]

    def estimate_segment_size(self, rule_id: int, population_size: int) -> dict[str, Any]:
        """估算分群规模（抽样方法）。"""
        rule = store.find(self.MODULE, rule_id)
        if rule is None:
            return {"ok": False, "message": f"分群规则 {rule_id} 不存在"}
        # 简化估计：按条件复杂度估算
        include_conds = len(str(rule.get("筛选条件", "")).split(";"))
        exclude_conds = len(str(rule.get("排除条件", "")).split(";"))
        total_conds = include_conds + exclude_conds
        # 经验公式：每个条件约减半
        estimated = population_size * (0.6 ** max(total_conds, 1))
        return {
            "ok": True,
            "population_size": population_size,
            "condition_count": total_conds,
            "estimated_size": max(1, int(estimated)),
            "refresh_cycle": rule.get("刷新周期"),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        segments = store.rows(self.MODULE)
        base["artifact"] = "分群规则数据集"
        base["active_segments"] = sum(1 for row in segments if row.get("status") == "已生效")
        base["total_population_covered"] = sum(
            _parse_int(row.get("分群规模"), 0) for row in segments
        )
        return base


def _member_ids_of(matched: list[dict[str, Any]]) -> list[str]:
    """从命中行提取去重后的成员标识（保持命中顺序）。"""
    member_ids: list[str] = []
    seen: set[str] = set()
    for row in matched:
        uid = str(row.get("user_id", "") or "")
        if uid and uid not in seen:
            seen.add(uid)
            member_ids.append(uid)
    return member_ids


def _parse_int(val: Any, default: int) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default
