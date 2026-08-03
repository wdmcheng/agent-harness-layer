"""结构化validity指标从公开bound seam取证的合同。"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.contracts.test_provider_neutral_structured_failure_contracts import (
    _bound_fake,  # pyright: ignore[reportPrivateUsage]
    _request,  # pyright: ignore[reportPrivateUsage]
)
from tests.contracts.test_provider_neutral_structured_transport_contracts import (
    _bound,  # pyright: ignore[reportPrivateUsage]
    _ControlledProvider,  # pyright: ignore[reportPrivateUsage]
    _schema,  # pyright: ignore[reportPrivateUsage]
)
from tests.contracts.test_provider_neutral_structured_transport_contracts import (
    _request as _transport_request,  # pyright: ignore[reportPrivateUsage]
)

from agent_harness.evals import score_structured_output
from agent_harness.models import ModelProviderInvocationError


@pytest.mark.asyncio
async def test_structured_eval_scores_only_valid_public_seam_result(tmp_path: Path) -> None:
    """Valid 得一分；invalid 与 needs-review 均从同一公开 seam 得零分。"""

    valid_dir = tmp_path / "valid"
    invalid_dir = tmp_path / "invalid"
    review_dir = tmp_path / "needs-review"
    for directory in (valid_dir, invalid_dir, review_dir):
        directory.mkdir()

    valid_service, valid_storage, _valid_provider, valid_bound, _run_id = await _bound_fake(
        valid_dir,
        candidates=({"answer": "ok"},),
    )
    try:
        response = await valid_bound.complete_structured(
            _request(),
            operation_key="eval-valid",
        )
        valid_score = score_structured_output(
            response=response,
            terminal_status="valid",
            error_code=None,
        )
        assert valid_score.value == 1.0
        assert valid_score.passed is True
    finally:
        await valid_service.aclose()
        await valid_storage.dispose()

    invalid_service, invalid_storage, _invalid_provider, invalid_bound, _run_id = await _bound_fake(
        invalid_dir,
        candidates=({"wrong": 1},),
    )
    try:
        with pytest.raises(ModelProviderInvocationError) as invalid:
            await invalid_bound.complete_structured(
                _request(),
                operation_key="eval-invalid",
            )
        invalid_score = score_structured_output(
            response=None,
            terminal_status="invalid",
            error_code=invalid.value.code,
        )
        assert invalid_score.value == 0.0
        assert invalid_score.passed is False
    finally:
        await invalid_service.aclose()
        await invalid_storage.dispose()

    schema = _schema()
    review_provider = _ControlledProvider(schema, close_error=True)
    review_service, review_storage, review_bound, _run_id = await _bound(
        review_dir,
        provider=review_provider,
        schema=schema,
    )
    try:
        with pytest.raises(ModelProviderInvocationError) as needs_review:
            await review_bound.complete_structured(
                _transport_request(),
                operation_key="eval-needs-review",
            )
        review_score = score_structured_output(
            response=None,
            terminal_status="needs_review",
            error_code=needs_review.value.code,
        )
        assert review_score.value == 0.0
        assert review_score.passed is False
    finally:
        await review_service.aclose()
        await review_storage.dispose()
