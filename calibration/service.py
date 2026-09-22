from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from db.models import CalibrationModel, MarketOutcome, SignalCalibration, SignalEvaluation, SignalRun


CALIBRATION_METHOD = "quantile_beta_pava_v1"
TARGETS = ("kospi_up", "semiconductor_up", "gap_up", "up_close")


@dataclass(frozen=True)
class TrainingPoint:
    session_date: date
    score: float
    actual: bool


_TARGET_ACCESSORS: dict[str, tuple[Callable[[SignalRun], float], Callable[[SignalEvaluation], bool | None]]] = {
    "kospi_up": (lambda row: float(row.kospi_score), lambda row: row.kospi_up_actual),
    "semiconductor_up": (
        lambda row: float(row.semiconductor_score),
        lambda row: row.semiconductor_up_actual,
    ),
    "gap_up": (lambda row: float(row.gap_up_probability), lambda row: row.gap_up_actual),
    "up_close": (
        lambda row: float(row.up_close_probability),
        lambda row: row.up_close_actual,
    ),
}

_PROBABILITY_FIELDS = {
    "kospi_up": "kospi_up_probability",
    "semiconductor_up": "semiconductor_up_probability",
    "gap_up": "gap_up_probability",
    "up_close": "up_close_probability",
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _json_dict(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _training_points(session: Session, *, target: str, engine_version: str) -> list[TrainingPoint]:
    if target not in _TARGET_ACCESSORS:
        raise ValueError(f"unknown calibration target: {target}")
    score_getter, actual_getter = _TARGET_ACCESSORS[target]
    rows = session.execute(
        select(SignalEvaluation, SignalRun)
        .join(SignalRun, SignalRun.id == SignalEvaluation.signal_run_id)
        .where(SignalRun.engine_version == engine_version)
        .order_by(SignalEvaluation.session_date.asc(), SignalEvaluation.id.asc())
    ).all()
    points: list[TrainingPoint] = []
    for evaluation, signal in rows:
        actual = actual_getter(evaluation)
        if actual is None:
            continue
        signal_details = _json_dict(signal.details_json)
        eligible_raw = signal_details.get("calibration_eligible_targets")
        if isinstance(eligible_raw, list):
            eligible_targets = {str(item) for item in eligible_raw}
            if target not in eligible_targets:
                continue
        points.append(
            TrainingPoint(
                session_date=evaluation.session_date,
                score=_clamp(score_getter(signal), 0.0, 100.0),
                actual=bool(actual),
            )
        )
    return points


def _quantile_bins(points: list[TrainingPoint], bin_count: int) -> list[dict[str, object]]:
    # Equal raw scores must never be split across calibration bins. Otherwise the
    # calibrated probability for the same score can depend on row/date ordering.
    score_groups: list[list[TrainingPoint]] = []
    for item in sorted(points, key=lambda point: (round(point.score, 8), point.session_date)):
        score_key = round(item.score, 8)
        if not score_groups or round(score_groups[-1][0].score, 8) != score_key:
            score_groups.append([])
        score_groups[-1].append(item)

    requested = max(1, min(int(bin_count), len(score_groups)))
    if not score_groups:
        return []

    # Choose boundaries near observation-count quantiles, but only between whole
    # score groups. Constraints guarantee every remaining bin receives >=1 group.
    cumulative_counts: list[int] = [0]
    for group in score_groups:
        cumulative_counts.append(cumulative_counts[-1] + len(group))

    boundaries: list[int] = []
    previous = 0
    total = cumulative_counts[-1]
    for bin_index in range(1, requested):
        target = total * bin_index / requested
        remaining_bins = requested - bin_index
        first_boundary = previous + 1
        last_boundary = len(score_groups) - remaining_bins
        boundary = min(
            range(first_boundary, last_boundary + 1),
            key=lambda candidate: (
                abs(cumulative_counts[candidate] - target),
                candidate,
            ),
        )
        boundaries.append(boundary)
        previous = boundary
    boundaries.append(len(score_groups))

    bins: list[dict[str, object]] = []
    start_group = 0
    for end_group in boundaries:
        chunk = [item for group in score_groups[start_group:end_group] for item in group]
        start_group = end_group
        positives = sum(1 for item in chunk if item.actual)
        bins.append(
            {
                "min_score": min(item.score for item in chunk),
                "max_score": max(item.score for item in chunk),
                "mean_score": sum(item.score for item in chunk) / len(chunk),
                "n": len(chunk),
                "positive_count": positives,
                "raw_rate": positives / len(chunk),
            }
        )
    return bins


def _pava(values: list[float], weights: list[float]) -> list[float]:
    blocks: list[dict[str, object]] = []
    for index, (value, weight) in enumerate(zip(values, weights, strict=True)):
        blocks.append({"start": index, "end": index, "weight": weight, "value": value})
        while len(blocks) >= 2 and float(blocks[-2]["value"]) > float(blocks[-1]["value"]):
            right = blocks.pop()
            left = blocks.pop()
            total_weight = float(left["weight"]) + float(right["weight"])
            merged_value = (
                float(left["value"]) * float(left["weight"])
                + float(right["value"]) * float(right["weight"])
            ) / total_weight
            blocks.append(
                {
                    "start": left["start"],
                    "end": right["end"],
                    "weight": total_weight,
                    "value": merged_value,
                }
            )
    result = [0.0] * len(values)
    for block in blocks:
        for index in range(int(block["start"]), int(block["end"]) + 1):
            result[index] = float(block["value"])
    return result


def _apply_nodes(nodes: list[dict[str, object]], score: float) -> float:
    if not nodes:
        raise ValueError("calibration model has no nodes")
    ordered = sorted(nodes, key=lambda item: float(item["mean_score"]))
    x = _clamp(float(score), 0.0, 100.0)
    first_x = float(ordered[0]["mean_score"])
    first_y = float(ordered[0]["probability"])
    if x <= first_x:
        return first_y
    last_x = float(ordered[-1]["mean_score"])
    last_y = float(ordered[-1]["probability"])
    if x >= last_x:
        return last_y
    for left, right in zip(ordered, ordered[1:]):
        x0 = float(left["mean_score"])
        x1 = float(right["mean_score"])
        if x > x1:
            continue
        y0 = float(left["probability"])
        y1 = float(right["probability"])
        if x1 <= x0:
            return (y0 + y1) / 2.0
        ratio = (x - x0) / (x1 - x0)
        return y0 + ratio * (y1 - y0)
    return last_y


def _brier(probabilities: list[float], actuals: list[bool]) -> float:
    if not probabilities:
        return 0.0
    return sum((prob - (1.0 if actual else 0.0)) ** 2 for prob, actual in zip(probabilities, actuals, strict=True)) / len(probabilities)


def _build_model_payload(
    points: list[TrainingPoint],
    *,
    bin_count: int,
    prior_strength: float,
) -> dict[str, object]:
    positives = sum(1 for item in points if item.actual)
    n = len(points)
    # Beta(1,1) smoothing prevents a zero/one prior center in one-sided samples.
    prior_center = (positives + 1.0) / (n + 2.0)
    bins = _quantile_bins(points, bin_count)
    shrunk_rates: list[float] = []
    weights: list[float] = []
    for item in bins:
        local_n = int(item["n"])
        local_positive = int(item["positive_count"])
        shrunk = (local_positive + prior_strength * prior_center) / (local_n + prior_strength)
        item["shrunk_rate"] = shrunk
        shrunk_rates.append(shrunk)
        weights.append(float(local_n))

    monotonic = _pava(shrunk_rates, weights)
    nodes: list[dict[str, object]] = []
    for item, probability in zip(bins, monotonic, strict=True):
        nodes.append(
            {
                "min_score": round(float(item["min_score"]), 6),
                "max_score": round(float(item["max_score"]), 6),
                "mean_score": round(float(item["mean_score"]), 6),
                "n": int(item["n"]),
                "positive_count": int(item["positive_count"]),
                "raw_rate": round(float(item["raw_rate"]), 8),
                "shrunk_rate": round(float(item["shrunk_rate"]), 8),
                "probability": round(_clamp(float(probability), 0.001, 0.999), 8),
            }
        )

    raw_probabilities = [_clamp(item.score / 100.0, 0.001, 0.999) for item in points]
    calibrated_probabilities = [_apply_nodes(nodes, item.score) for item in points]
    actuals = [item.actual for item in points]
    ece = sum(
        (int(node["n"]) / n) * abs(float(node["probability"]) - float(node["raw_rate"]))
        for node in nodes
    )
    return {
        "nodes": nodes,
        "base_rate": positives / n,
        "prior_center": prior_center,
        "brier_raw": _brier(raw_probabilities, actuals),
        "brier_calibrated": _brier(calibrated_probabilities, actuals),
        "expected_calibration_error": ece,
    }


def train_target(
    session: Session,
    *,
    target: str,
    engine_version: str,
    min_samples: int,
    min_class_count: int,
    bin_count: int,
    prior_strength: float,
) -> dict[str, object]:
    points = _training_points(session, target=target, engine_version=engine_version)
    positives = sum(1 for item in points if item.actual)
    negatives = len(points) - positives
    distinct_scores = len({round(item.score, 8) for item in points})
    if len(points) < min_samples:
        return {
            "target": target,
            "trained": False,
            "sample_count": len(points),
            "reason": f"need at least {min_samples} samples",
        }
    if positives < min_class_count or negatives < min_class_count:
        return {
            "target": target,
            "trained": False,
            "sample_count": len(points),
            "positive_count": positives,
            "negative_count": negatives,
            "distinct_score_count": distinct_scores,
            "reason": f"need at least {min_class_count} positive and negative samples",
        }
    if distinct_scores < 3:
        return {
            "target": target,
            "trained": False,
            "sample_count": len(points),
            "positive_count": positives,
            "negative_count": negatives,
            "distinct_score_count": distinct_scores,
            "reason": "need at least 3 distinct score values",
        }

    payload = _build_model_payload(
        points,
        bin_count=bin_count,
        prior_strength=prior_strength,
    )
    session.execute(
        update(CalibrationModel)
        .where(CalibrationModel.target == target)
        .where(CalibrationModel.engine_version == engine_version)
        .where(CalibrationModel.active.is_(True))
        .values(active=False)
    )
    row = CalibrationModel(
        target=target,
        engine_version=engine_version,
        method=CALIBRATION_METHOD,
        sample_count=len(points),
        positive_count=positives,
        base_rate=round(float(payload["base_rate"]), 8),
        bin_count=len(payload["nodes"]),
        prior_strength=float(prior_strength),
        trained_from_date=points[0].session_date,
        trained_through_date=points[-1].session_date,
        brier_raw=round(float(payload["brier_raw"]), 8),
        brier_calibrated=round(float(payload["brier_calibrated"]), 8),
        expected_calibration_error=round(float(payload["expected_calibration_error"]), 8),
        active=True,
        model_json=json.dumps(
            {
                "method": CALIBRATION_METHOD,
                "target": target,
                "engine_version": engine_version,
                "nodes": payload["nodes"],
                "prior_center": round(float(payload["prior_center"]), 8),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    session.add(row)
    session.flush()
    return {
        "target": target,
        "trained": True,
        "model_id": row.id,
        "sample_count": row.sample_count,
        "positive_count": positives,
        "negative_count": negatives,
        "distinct_score_count": distinct_scores,
        "trained_from_date": row.trained_from_date.isoformat(),
        "trained_through_date": row.trained_through_date.isoformat(),
        "brier_raw": row.brier_raw,
        "brier_calibrated": row.brier_calibrated,
        "expected_calibration_error": row.expected_calibration_error,
        "diagnostic_scope": "in_sample_descriptive_only",
    }


def calibration_readiness(
    session: Session,
    *,
    engine_version: str,
    min_samples: int,
    min_class_count: int,
) -> dict[str, object]:
    items: dict[str, object] = {}
    for target in TARGETS:
        points = _training_points(session, target=target, engine_version=engine_version)
        positives = sum(1 for item in points if item.actual)
        negatives = len(points) - positives
        distinct_scores = len({round(item.score, 8) for item in points})
        reasons: list[str] = []
        if len(points) < min_samples:
            reasons.append(f"samples {len(points)}/{min_samples}")
        if positives < min_class_count:
            reasons.append(f"positive {positives}/{min_class_count}")
        if negatives < min_class_count:
            reasons.append(f"negative {negatives}/{min_class_count}")
        if distinct_scores < 3:
            reasons.append(f"distinct_scores {distinct_scores}/3")
        items[target] = {
            "ready": not reasons,
            "sample_count": len(points),
            "positive_count": positives,
            "negative_count": negatives,
            "distinct_score_count": distinct_scores,
            "reasons": reasons,
        }
    return items


def train_all_targets(
    session: Session,
    *,
    engine_version: str,
    min_samples: int,
    min_class_count: int,
    bin_count: int,
    prior_strength: float,
) -> dict[str, object]:
    results = [
        train_target(
            session,
            target=target,
            engine_version=engine_version,
            min_samples=min_samples,
            min_class_count=min_class_count,
            bin_count=bin_count,
            prior_strength=prior_strength,
        )
        for target in TARGETS
    ]
    session.commit()
    return {
        "engine_version": engine_version,
        "method": CALIBRATION_METHOD,
        "trained_targets": sum(1 for item in results if item["trained"]),
        "targets": results,
        "note": (
            "Training metrics are in-sample descriptive diagnostics. Persisted future "
            "signal_calibrations are the no-lookahead records used for live/out-of-sample evaluation."
        ),
    }


def get_active_models(session: Session, *, engine_version: str) -> dict[str, CalibrationModel]:
    rows = list(
        session.scalars(
            select(CalibrationModel)
            .where(CalibrationModel.engine_version == engine_version)
            .where(CalibrationModel.active.is_(True))
            .order_by(CalibrationModel.target.asc(), CalibrationModel.created_at.desc())
        ).all()
    )
    result: dict[str, CalibrationModel] = {}
    for row in rows:
        result.setdefault(row.target, row)
    return result


def apply_model(row: CalibrationModel, score: float) -> float:
    payload = _json_dict(row.model_json)
    nodes = payload.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError(f"calibration model {row.id} has invalid nodes")
    return round(_clamp(_apply_nodes(nodes, score), 0.001, 0.999), 8)


def get_signal_calibration(session: Session, signal_run_id: int) -> SignalCalibration | None:
    return session.scalar(
        select(SignalCalibration).where(SignalCalibration.signal_run_id == signal_run_id).limit(1)
    )


def get_signal_calibration_map(
    session: Session,
    signal_run_ids: list[int],
) -> dict[int, SignalCalibration]:
    if not signal_run_ids:
        return {}
    rows = list(
        session.scalars(
            select(SignalCalibration).where(SignalCalibration.signal_run_id.in_(signal_run_ids))
        ).all()
    )
    return {row.signal_run_id: row for row in rows}


def apply_active_models_to_signal(session: Session, signal: SignalRun) -> SignalCalibration | None:
    existing = get_signal_calibration(session, signal.id)
    if existing is not None:
        return existing
    models = get_active_models(session, engine_version=signal.engine_version)
    if not models:
        return None

    scores = {
        "kospi_up": float(signal.kospi_score),
        "semiconductor_up": float(signal.semiconductor_score),
        "gap_up": float(signal.gap_up_probability),
        "up_close": float(signal.up_close_probability),
    }
    signal_details = _json_dict(signal.details_json)
    eligible_raw = signal_details.get("calibration_eligible_targets")
    eligible_targets = (
        {str(item) for item in eligible_raw}
        if isinstance(eligible_raw, list)
        else set(scores)
    )
    probabilities: dict[str, float] = {}
    model_ids: dict[str, int] = {}
    model_meta: dict[str, object] = {}
    for target, score in scores.items():
        if target not in eligible_targets:
            continue
        model = models.get(target)
        if model is None:
            continue
        probabilities[target] = apply_model(model, score)
        model_ids[target] = model.id
        model_meta[target] = {
            "model_id": model.id,
            "sample_count": model.sample_count,
            "trained_through_date": model.trained_through_date.isoformat(),
            "method": model.method,
        }

    if not probabilities:
        return None
    row = SignalCalibration(
        signal_run_id=signal.id,
        engine_version=signal.engine_version,
        kospi_up_probability=probabilities.get("kospi_up"),
        semiconductor_up_probability=probabilities.get("semiconductor_up"),
        gap_up_probability=probabilities.get("gap_up"),
        up_close_probability=probabilities.get("up_close"),
        model_ids_json=json.dumps(model_ids, ensure_ascii=False, separators=(",", ":")),
        details_json=json.dumps(
            {
                "available_targets": sorted(probabilities),
                "models": model_meta,
                "raw_scores": scores,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    session.add(row)
    session.flush()
    return row


def serialize_signal_calibration(row: SignalCalibration | None) -> dict[str, object]:
    if row is None:
        return {
            "available": False,
            "available_targets": [],
            "probabilities": {
                "kospi_up": None,
                "semiconductor_up": None,
                "gap_up": None,
                "up_close": None,
            },
        }
    details = _json_dict(row.details_json)
    probabilities = {
        "kospi_up": row.kospi_up_probability,
        "semiconductor_up": row.semiconductor_up_probability,
        "gap_up": row.gap_up_probability,
        "up_close": row.up_close_probability,
    }
    available_targets = [key for key, value in probabilities.items() if value is not None]
    return {
        "available": bool(available_targets),
        "available_targets": available_targets,
        "probabilities": probabilities,
        "created_at": row.created_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if row.created_at.tzinfo
        else row.created_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
        "model_ids": _json_dict(row.model_ids_json),
        "models": details.get("models", {}),
    }


def serialize_model(row: CalibrationModel, *, include_nodes: bool = False) -> dict[str, object]:
    payload = _json_dict(row.model_json)
    item = {
        "id": row.id,
        "created_at": row.created_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if row.created_at.tzinfo
        else row.created_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
        "target": row.target,
        "engine_version": row.engine_version,
        "method": row.method,
        "active": row.active,
        "sample_count": row.sample_count,
        "positive_count": row.positive_count,
        "base_rate": row.base_rate,
        "bin_count": row.bin_count,
        "prior_strength": row.prior_strength,
        "trained_from_date": row.trained_from_date.isoformat(),
        "trained_through_date": row.trained_through_date.isoformat(),
        "diagnostics": {
            "scope": "in_sample_descriptive_only",
            "brier_raw": row.brier_raw,
            "brier_calibrated": row.brier_calibrated,
            "expected_calibration_error": row.expected_calibration_error,
        },
    }
    if include_nodes:
        item["nodes"] = payload.get("nodes", [])
    return item


def list_calibration_models(
    session: Session,
    *,
    engine_version: str | None,
    active_only: bool,
    limit: int,
) -> list[CalibrationModel]:
    stmt = select(CalibrationModel)
    if engine_version:
        stmt = stmt.where(CalibrationModel.engine_version == engine_version)
    if active_only:
        stmt = stmt.where(CalibrationModel.active.is_(True))
    return list(
        session.scalars(
            stmt.order_by(CalibrationModel.created_at.desc(), CalibrationModel.id.desc()).limit(limit)
        ).all()
    )


def calibration_performance(session: Session, *, limit: int = 5000) -> dict[str, object]:
    rows = session.execute(
        select(SignalEvaluation, SignalRun, SignalCalibration)
        .join(SignalRun, SignalRun.id == SignalEvaluation.signal_run_id)
        .join(SignalCalibration, SignalCalibration.signal_run_id == SignalRun.id)
        .order_by(SignalEvaluation.session_date.desc(), SignalEvaluation.id.desc())
        .limit(limit)
    ).all()

    actual_getters = {key: pair[1] for key, pair in _TARGET_ACCESSORS.items()}
    score_getters = {key: pair[0] for key, pair in _TARGET_ACCESSORS.items()}
    result: dict[str, object] = {}
    for target in TARGETS:
        field = _PROBABILITY_FIELDS[target]
        calibrated_probs: list[float] = []
        raw_probs: list[float] = []
        actuals: list[bool] = []
        for evaluation, signal, calibration in rows:
            probability = getattr(calibration, field)
            actual = actual_getters[target](evaluation)
            if probability is None or actual is None:
                continue
            calibrated_probs.append(float(probability))
            raw_probs.append(_clamp(score_getters[target](signal) / 100.0, 0.001, 0.999))
            actuals.append(bool(actual))
        result[target] = {
            "n": len(actuals),
            "brier_raw": None if not actuals else round(_brier(raw_probs, actuals), 8),
            "brier_calibrated": None
            if not actuals
            else round(_brier(calibrated_probs, actuals), 8),
            "scope": "persisted_at_signal_time_only",
        }
    return {
        "evaluation_rows_with_calibration": len(rows),
        "targets": result,
        "note": (
            "Only calibrations persisted when each signal was created are evaluated here, "
            "so later retraining is not retroactively applied to old forecasts."
        ),
    }
