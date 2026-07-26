"""Typed lineage validation for benchmark annotation actions."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


@dataclass(frozen=True)
class LineageError:
    code: str
    field: str
    message: str

    def __getitem__(self, key: str) -> str:
        return getattr(self, key)


class ActionTimestamps(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    annotation_started: datetime.datetime
    annotation_completed: datetime.datetime
    second_pass_started: datetime.datetime | None = None
    second_pass_completed: datetime.datetime | None = None

    @field_validator("annotation_started", "annotation_completed", "second_pass_started", "second_pass_completed")
    @classmethod
    def _strict_utc(cls, value: datetime.datetime | None) -> datetime.datetime | None:
        if value is not None and (
            value.tzinfo is None or value.utcoffset() != datetime.timedelta(0)
        ):
            raise ValueError("timestamps must use UTC offset +00:00")
        return value


class LineageRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_generation_source: Literal["manual", "retrieval_arm_union"]
    model_assisted_fields: tuple[str, ...] = ()
    human_confirmed_fields: tuple[str, ...] = ()
    timestamps: ActionTimestamps
    adjudication_status: Literal["pending", "in_review", "resolved"]

    @model_validator(mode="after")
    def _canonical_fields(self) -> "LineageRecord":
        for name in ("model_assisted_fields", "human_confirmed_fields"):
            values = getattr(self, name)
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{name} must be sorted and unique")
        return self


def validate_lineage(
    record: LineageRecord,
    session_date: datetime.date | str | None = None,
) -> tuple[LineageError, ...]:
    errors: list[LineageError] = []
    ts = record.timestamps
    values = {
        "annotation_started": ts.annotation_started,
        "annotation_completed": ts.annotation_completed,
        "second_pass_started": ts.second_pass_started,
        "second_pass_completed": ts.second_pass_completed,
    }
    for name in ("annotation_started", "annotation_completed"):
        if values[name] is None:
            errors.append(LineageError(
                "missing_action_timestamp", name, f"{name} is required",
            ))
    for name, value in values.items():
        if value is not None and (
            value.tzinfo is None or value.utcoffset() != datetime.timedelta(0)
        ):
            errors.append(LineageError(
                "non_utc_timestamp", name, f"{name} must use UTC offset +00:00",
            ))

    valid_utc = {
        name: value
        for name, value in values.items()
        if value is not None
        and value.tzinfo is not None
        and value.utcoffset() == datetime.timedelta(0)
    }

    if session_date is not None and "annotation_started" in valid_utc:
        day = datetime.date.fromisoformat(session_date) if isinstance(session_date, str) else session_date
        session_start = datetime.datetime.combine(day, datetime.time(), tzinfo=datetime.timezone.utc)
        if valid_utc["annotation_started"] < session_start:
            errors.append(LineageError(
                "annotation_predates_session", "annotation_started",
                "annotation_started must be on or after the session date",
            ))

    pair_names = [("annotation_started", "annotation_completed")]
    if ts.second_pass_started is not None:
        pair_names.append(("annotation_completed", "second_pass_started"))
    if ts.second_pass_started is not None and ts.second_pass_completed is not None:
        pair_names.append(("second_pass_started", "second_pass_completed"))
    pairs = [
        (left_name, valid_utc[left_name], right_name, valid_utc[right_name])
        for left_name, right_name in pair_names
        if left_name in valid_utc and right_name in valid_utc
    ]
    for left_name, left, right_name, right in pairs:
        if left == right:
            errors.append(LineageError("batch_timestamp", left_name, f"{left_name} equals {right_name}"))
        elif left > right:
            errors.append(LineageError("invalid_action_order", right_name, f"{left_name} must precede {right_name}"))

    if record.adjudication_status == "pending":
        if ts.second_pass_started is not None or ts.second_pass_completed is not None:
            errors.append(LineageError("invalid_adjudication_state", "adjudication_status", "pending forbids second-pass timestamps"))
    elif record.adjudication_status == "in_review":
        if ts.second_pass_started is None:
            errors.append(LineageError("missing_action_timestamp", "second_pass_started", "in_review requires second_pass_started"))
        if ts.second_pass_completed is not None:
            errors.append(LineageError("invalid_adjudication_state", "second_pass_completed", "in_review forbids second_pass_completed"))
    else:
        for name in ("second_pass_started", "second_pass_completed"):
            if getattr(ts, name) is None:
                errors.append(LineageError("missing_action_timestamp", name, f"resolved requires {name}"))

    return tuple(sorted(errors, key=lambda error: (error.code, error.field)))


__all__ = ["ActionTimestamps", "LineageError", "LineageRecord", "validate_lineage"]
