"""同期群分析领域服务：保留率计算、群组追踪、跨模块数据消费。

分群留存与分群列表共用同一份有效圈选：按全部命中成员（而非预览样本）
取回其全部有效注册与回访记录计算保留率；每次计算留存报告快照，
记录实际圈选版本与分子分母，供事后复核旧数与迟到事件影响。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store
from app.analytics_engine import compute_cohort_retention

REPORT_TABLE = "cohort_report"


class CohortAnalysisService(BaseModuleService):
    MODULE = "cohort_analysis"
    ENTITY = "群组配置"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["群组名称", "初始事件", "回访事件", "分析周期", "群组大小", "保留计算", "显示格式"]
    REQUIRED_FIELDS = ["群组名称", "初始事件", "回访事件"]
    STATUS_ORDER = ["草稿", "分析中", "已完成", "已归档"]
    ACTION_RULES = {"开始分析": "分析中", "完成分析": "已完成", "归档群组": "已归档"}
    NEGATIVE_ACTIONS = []

    # ---- 业务方法 ----

    def run_cohort_analysis(
        self, config_id: int, events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """执行同期群分析：基于事件数据计算保留率矩阵。"""
        cohort_config = store.find(self.MODULE, config_id)
        if cohort_config is None:
            return {"ok": False, "message": f"同期群配置 {config_id} 不存在"}

        init_event = str(cohort_config.get("初始事件", ""))
        return_event = str(cohort_config.get("回访事件", ""))
        period_str = str(cohort_config.get("分析周期", "7"))
        try:
            period_days = int(period_str)
        except ValueError:
            period_days = 7

        max_periods = 8
        retention_method = str(cohort_config.get("保留计算", "standard")).lower()

        if retention_method == "bounded":
            max_periods = 12  # 有界保留

        matrix = compute_cohort_retention(
            events, init_event, return_event, period_days, max_periods,
        )

        self.run_action(config_id, "完成分析")

        return {
            "ok": True,
            "config_name": str(cohort_config.get("群组名称", "")),
            "init_event": init_event,
            "return_event": return_event,
            "period_days": period_days,
            "max_periods": max_periods,
            "cohort_count": len(matrix.cohort_ids),
            "cohort_ids": matrix.cohort_ids,
            "period_labels": matrix.period_labels,
            "retention_matrix": matrix.retention_matrix,
            # 分子/分母明细，供核对实际留存人数与群组人数
            "cohort_sizes": matrix.cohort_sizes,
            "retained_counts": matrix.retained_counts,
            "avg_retention_week1": round(
                sum(row[0] for row in matrix.retention_matrix if len(row) > 0)
                / max(len(matrix.retention_matrix), 1), 2,
            ),
        }

    def compare_cohort_retention(
        self, config_id_1: int, config_id_2: int,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """比较两个群组的保留率。"""
        result_1 = self.run_cohort_analysis(config_id_1, events)
        result_2 = self.run_cohort_analysis(config_id_2, events)
        if not result_1.get("ok") or not result_2.get("ok"):
            return {"ok": False, "message": "分析运行失败"}
        r1_avg = result_1.get("avg_retention_week1", 0)
        r2_avg = result_2.get("avg_retention_week1", 0)
        return {
            "ok": True,
            "config_1": result_1.get("config_name"),
            "config_2": result_2.get("config_name"),
            "retention_1_week1": r1_avg,
            "retention_2_week1": r2_avg,
            "difference": round(r2_avg - r1_avg, 2),
        }

    def get_retention_for_segment(
        self, segment_id: str, events: list[dict[str, Any]], config_id: int = 1,
    ) -> dict[str, Any]:
        """按分群的有效圈选运行同期群分析（分群分析→同期群分析的跨模块消费）。

        与分群列表共用同一份有效圈选：取全部命中成员，再取回这些成员的
        全部有效注册与回访记录计算保留率，不再使用预览样本。
        """
        from app.services import service_for
        segment_svc = service_for("segment_rule")
        if segment_svc is None:
            return {"ok": False, "message": "分群服务不可用"}
        segment = store.find("segment_rule", int(segment_id))
        if segment is None:
            return {"ok": False, "message": f"分群 {segment_id} 不存在"}
        seg_results = segment_svc.compute_segment(int(segment_id), events)
        if not seg_results.get("ok"):
            return {"ok": False, "message": f"分群圈选失败：{seg_results.get('message', '')}"}

        member_ids = {str(uid) for uid in seg_results.get("member_ids", [])}
        if member_ids:
            # 取全有效回访记录：命中成员的全部事件都进入保留率计算
            scoped_events = [
                e for e in events if str(e.get("user_id", "")) in member_ids
            ]
        else:
            scoped_events = list(seg_results.get("matched") or [])

        result = self.run_cohort_analysis(config_id, scoped_events)
        if not result.get("ok"):
            return result

        report = self._store_report(
            segment_id=int(segment_id),
            segment=segment,
            seg_results=seg_results,
            analysis=result,
        )
        result.update({
            "segment_id": int(segment_id),
            "segment_name": str(segment.get("分群名称", "")),
            "segment_version": seg_results.get("version"),
            "segment_matched_count": seg_results.get("matched_count"),
            "report_id": report["id"],
        })
        return result

    def _store_report(
        self, *, segment_id: int, segment: dict[str, Any],
        seg_results: dict[str, Any], analysis: dict[str, Any],
    ) -> dict[str, Any]:
        """留存报告快照：记录实际圈选版本与分子分母，供复核旧数。"""
        reports = store.rows(REPORT_TABLE)
        record = {
            "id": max((int(r.get("id", 0)) for r in reports), default=0) + 1,
            "segment_id": segment_id,
            "segment_name": str(segment.get("分群名称", "")),
            "segment_version": seg_results.get("version"),
            "member_count": seg_results.get("matched_count"),
            "cohort_ids": analysis.get("cohort_ids", []),
            "cohort_sizes": analysis.get("cohort_sizes", []),
            "retained_counts": analysis.get("retained_counts", []),
            "retention_matrix": analysis.get("retention_matrix", []),
            "created_at": time.time(),
        }
        reports.append(record)
        return record

    def list_segment_reports(self, segment_id: int) -> list[dict[str, Any]]:
        """回查该分群历次留存报告及其对应的实际圈选版本。"""
        return [
            r for r in store.rows(REPORT_TABLE)
            if int(r.get("segment_id", 0)) == segment_id
        ]

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["artifact"] = "同期群分析数据集"
        rows = store.rows(self.MODULE)
        base["completed_analyses"] = sum(1 for row in rows if row.get("status") == "已完成")
        return base
