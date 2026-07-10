"""确定性多标签 optimization/holdout 分配算法。"""

from __future__ import annotations

import hashlib
import json

from agent_harness.evals.dataset_models import BehaviorTag, DatasetSplitRequest
from agent_harness.evals.errors import DatasetSplitError


def select_holdout(
    *,
    request: DatasetSplitRequest,
    case_ids: list[str],
    case_tags: dict[str, list[BehaviorTag]],
    target_size: int,
) -> set[str]:
    """搜索满足双侧标签覆盖的稳定 membership，避免贪心局部最优。"""

    tag_counts = {
        tag: sum(tag in case_tags[case_id] for case_id in case_ids) for tag in request.tags
    }
    constrained_tags = [tag for tag in request.tags if tag_counts[tag] >= 2]
    tag_bits = {tag: 1 << index for index, tag in enumerate(constrained_tags)}
    required_mask = (1 << len(constrained_tags)) - 1
    ordered_ids = sorted(
        case_ids,
        key=lambda case_id: (
            -sum(tag in tag_bits for tag in case_tags[case_id]),
            _stable_case_hash(request, case_id),
        ),
    )
    case_masks = {
        case_id: sum(tag_bits.get(tag, 0) for tag in case_tags[case_id])
        for case_id in ordered_ids
    }
    selected = _find_feasible_membership(
        ordered_ids=ordered_ids,
        case_masks=case_masks,
        target_size=target_size,
        required_mask=required_mask,
    )
    if selected is None:
        raise DatasetSplitError(
            "eval.split.unrepresentable_tags",
            "requested holdout size cannot preserve tag coverage on both subsets",
            status_code=422,
            field_path="tags",
        )

    distribution = tag_distribution(
        request.tags,
        case_tags,
        optimization=sorted(set(case_ids) - selected),
        holdout=sorted(selected),
        regression=[],
    )
    missing = [
        tag.value
        for tag, count in tag_counts.items()
        if count >= 2
        and (
            distribution[tag.value]["optimization"] == 0
            or distribution[tag.value]["holdout"] == 0
        )
    ]
    if missing:
        raise DatasetSplitError(
            "eval.split.unrepresentable_tags",
            "split cannot preserve requested tag coverage",
            status_code=422,
            field_path="tags",
            hint=", ".join(sorted(missing)),
        )
    return selected


def _find_feasible_membership(
    *,
    ordered_ids: list[str],
    case_masks: dict[str, int],
    target_size: int,
    required_mask: int,
) -> set[str] | None:
    """有限状态搜索；五个闭集标签使状态空间保持可控且结果确定。"""

    # 状态只记录 holdout/optimization 是否已覆盖每个标签，不记录具体计数。
    # 最多五个标签，因此每层状态数受 2^(5*2) * target_size 约束。
    states: dict[tuple[int, int, int], int] = {(0, 0, 0): 0}
    for index, case_id in enumerate(ordered_ids):
        mask = case_masks[case_id]
        next_states: dict[tuple[int, int, int], int] = {}
        remaining_after = len(ordered_ids) - index - 1
        for (chosen_count, holdout_mask, optimization_mask), chosen_bits in states.items():
            # include-first 加上稳定 case order，固定多解时的选择；后续状态只保留首解。
            if chosen_count < target_size:
                included_key = (chosen_count + 1, holdout_mask | mask, optimization_mask)
                next_states.setdefault(included_key, chosen_bits | (1 << index))
            if chosen_count + remaining_after >= target_size:
                excluded_key = (chosen_count, holdout_mask, optimization_mask | mask)
                next_states.setdefault(excluded_key, chosen_bits)
        states = next_states

    for (chosen_count, holdout_mask, optimization_mask), chosen_bits in states.items():
        if (
            chosen_count == target_size
            and holdout_mask == required_mask
            and optimization_mask == required_mask
        ):
            return {
                case_id
                for index, case_id in enumerate(ordered_ids)
                if chosen_bits & (1 << index)
            }
    return None


def tag_distribution(
    tags: list[BehaviorTag],
    case_tags: dict[str, list[BehaviorTag]],
    *,
    optimization: list[str],
    holdout: list[str],
    regression: list[str],
) -> dict[str, dict[str, int]]:
    subsets = {
        "optimization": optimization,
        "holdout": holdout,
        "regression": regression,
    }
    return {
        tag.value: {
            subset: sum(tag in case_tags[case_id] for case_id in ids)
            for subset, ids in subsets.items()
        }
        for tag in tags
    }


def split_id(
    *,
    request: DatasetSplitRequest,
    optimization: list[str],
    holdout: list[str],
    regression: list[str],
) -> str:
    payload = {
        "tenant_id": request.tenant_id,
        "agent_id": request.agent_id,
        "dataset": request.dataset,
        "tags": request.tags,
        "split_strategy": request.split_strategy,
        "optimization_ratio": request.optimization_ratio,
        "holdout_ratio": request.holdout_ratio,
        "regression_policy": request.regression_policy.to_payload(),
        "optimization": optimization,
        "holdout": holdout,
        "regression": regression,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    return f"split_{hashlib.sha256(encoded).hexdigest()[:24]}"


def _stable_case_hash(request: DatasetSplitRequest, case_id: str) -> str:
    value = "\0".join([request.tenant_id, request.agent_id, request.dataset, case_id])
    return hashlib.sha256(value.encode()).hexdigest()
