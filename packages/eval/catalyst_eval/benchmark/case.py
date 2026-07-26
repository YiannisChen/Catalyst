"""Strict benchmark case contracts."""

from __future__ import annotations

import datetime
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .judgment import EvidenceJudgment
from .lineage import LineageRecord
from .pool_manifest import PoolManifest

SEMVER = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


class CauseLabel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    descriptor: str
    direction: Literal["positive", "negative", "neutral", "mixed"]

    @field_validator("descriptor")
    @classmethod
    def _descriptor(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value.split()) <= 20:
            raise ValueError("descriptor must contain 1-20 words")
        return value


class BenchmarkCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    schema_version: str
    dataset_version: str
    split: Literal["core_answerable", "core_abstain", "retrieval_only", "adversarial"]
    parent_case_id: str | None = None
    ticker: str
    session_date: datetime.date
    cutoff_ts: datetime.datetime
    observable_facts: dict[str, bool | int | float | str | None]
    answerability: Literal["answerable", "abstain", "retrieval_only"]
    expected_abstention_reason_class: str | None = None
    acceptable_cause_labels: tuple[CauseLabel, ...] = ()
    evidence_judgments_by_chunk_id: dict[str, EvidenceJudgment] = Field(default_factory=dict)
    pool_manifest: PoolManifest | None = None
    unjudged_handling: Literal["chunks_outside_pool_explicitly_unjudged"] = "chunks_outside_pool_explicitly_unjudged"
    lineage: LineageRecord | None = None
    annotator_notes: str = ""

    @field_validator("case_id")
    @classmethod
    def _case_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("case_id must be non-empty")
        return value

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, value: str) -> str:
        if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", value) is None:
            raise ValueError("invalid ticker")
        return value

    @field_validator("schema_version", "dataset_version")
    @classmethod
    def _semver(cls, value: str) -> str:
        if SEMVER.fullmatch(value) is None:
            raise ValueError("not a semantic version")
        return value

    @field_validator("cutoff_ts")
    @classmethod
    def _cutoff_utc(cls, value: datetime.datetime) -> datetime.datetime:
        if value.tzinfo is None or value.utcoffset() != datetime.timedelta(0):
            raise ValueError("cutoff_ts must use UTC offset +00:00")
        return value

    @model_validator(mode="after")
    def _cross_field_contract(self) -> "BenchmarkCase":
        if (self.split == "adversarial") != (self.parent_case_id is not None):
            raise ValueError("parent_case_id is required only for adversarial cases")
        expected = {
            "core_answerable": "answerable",
            "core_abstain": "abstain",
            "retrieval_only": "retrieval_only",
        }.get(self.split)
        if expected is not None and self.answerability != expected:
            raise ValueError(f"answerability must be {expected} for split {self.split}")
        has_reason = bool(self.expected_abstention_reason_class and self.expected_abstention_reason_class.strip())
        if (self.answerability == "abstain") != has_reason:
            raise ValueError("expected_abstention_reason_class is required iff answerability is abstain")

        label_keys = tuple((label.descriptor, label.direction) for label in self.acceptable_cause_labels)
        if label_keys != tuple(sorted(set(label_keys))):
            raise ValueError("acceptable_cause_labels must be sorted and unique")
        if self.evidence_judgments_by_chunk_id and self.pool_manifest is None:
            raise ValueError("judgments require pool_manifest")
        inventory = set(self.pool_manifest.chunk_inventory) if self.pool_manifest else set()
        for chunk_id, judgment in self.evidence_judgments_by_chunk_id.items():
            if chunk_id != judgment.chunk_id:
                raise ValueError("judgment mapping key must equal judgment chunk_id")
            if chunk_id not in inventory:
                raise ValueError("judged chunk must appear in pool_manifest inventory")
        return self


__all__ = ["BenchmarkCase", "CauseLabel"]
