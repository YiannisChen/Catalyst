"""Checked-in model prices and per-role token bounds for public runs.

The ProviderBudgetGuard can only bound a provider call when it has both an
identity-bound price and a token envelope for the role making the call. Public
production runs therefore read one explicit, checked-in pricing file. A
missing file, malformed file, unknown model, or missing production role fails
closed; there is no implicit price and no code-level fallback.

The file schema is the same one the evaluation harness accepts:

    {
      "models": {
        "<provider>/<model_id>": {
          "input_usd_per_million_tokens": <number>,
          "output_usd_per_million_tokens": <number>
        }
      },
      "role_token_limits": {
        "<role>": {"max_input_tokens": <int>, "max_output_tokens": <int>}
      }
    }
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from catalyst_agents.runtime.provider_budget import ModelPrice, RoleTokenLimit

PRICING_FILE_ENV = "CATALYST_PRICING_FILE"
DEFAULT_PRICING_PATH = Path(__file__).resolve().with_name("pricing.json")


class PricingUnavailable(RuntimeError):
    """The checked-in pricing configuration cannot price a run."""


def required_role_names() -> frozenset[str]:
    """Production roles whose token bounds must be configured.

    These are the role constants used by the production graph. Importing them
    lazily keeps this small module independent of graph construction order.
    """
    from catalyst_agents.attribution.reported_reason import REPORTED_REASON_ROLE
    from catalyst_agents.attribution.verifier import VERIFIER_ROLE
    from catalyst_agents.engine.event_date import ROLE as EVENT_DATER_ROLE
    from catalyst_agents.nodes.desk_decision import DESK_EXTRACTOR_ROLE
    from catalyst_agents.nodes.evidence_analyst import ANALYST_ROLE
    from catalyst_agents.nodes.writer import WRITER_ROLE

    return frozenset(
        {
            ANALYST_ROLE,
            WRITER_ROLE,
            VERIFIER_ROLE,
            REPORTED_REASON_ROLE,
            DESK_EXTRACTOR_ROLE,
            EVENT_DATER_ROLE,
        }
    )


@dataclass(frozen=True)
class PricingConfig:
    """Validated prices and role token envelopes loaded from one file."""

    path: Path
    prices: dict[tuple[str, str], ModelPrice]
    role_token_limits: dict[str, RoleTokenLimit]

    def price_for(self, provider: str, model_id: str) -> ModelPrice:
        identity = (str(provider), str(model_id))
        price = self.prices.get(identity)
        if price is None:
            raise PricingUnavailable(
                f"no identity-bound price for {identity[0]}/{identity[1]}"
            )
        return price


def pricing_file_path(path: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the configured pricing file, defaulting to the checked-in file."""
    if path is not None:
        return Path(path).expanduser()
    raw = os.environ.get(PRICING_FILE_ENV)
    if raw:
        return Path(raw).expanduser()
    return DEFAULT_PRICING_PATH


def load_pricing_config(
    path: str | os.PathLike[str] | None = None,
) -> PricingConfig:
    """Load and strictly validate one non-secret pricing file.

    Every failure raises PricingUnavailable. Callers that admit or dispatch a
    public run must fail closed rather than substitute a default price.
    """
    source = pricing_file_path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise PricingUnavailable(f"pricing file is unreadable: {source}") from exc
    try:
        payload = json.loads(text, object_pairs_hook=_object_without_duplicates)
    except (json.JSONDecodeError, _DuplicateKeyError, TypeError, ValueError) as exc:
        raise PricingUnavailable(f"pricing file is malformed: {source}") from exc

    models = _require_mapping(payload.get("models"), "models")
    limits = _require_mapping(payload.get("role_token_limits"), "role_token_limits")
    if not models:
        raise PricingUnavailable("pricing file has no models")
    if not limits:
        raise PricingUnavailable("pricing file has no role token limits")

    prices: dict[tuple[str, str], ModelPrice] = {}
    for key, value in models.items():
        if not isinstance(key, str):
            raise PricingUnavailable("model price keys must be strings")
        provider, separator, model_id = key.partition("/")
        if not separator or not provider or not model_id:
            raise PricingUnavailable(
                f"price key {key!r} must be '<provider>/<model_id>'"
            )
        record = _require_mapping(value, f"models[{key!r}]")
        input_price = _finite_non_negative(
            record.get("input_usd_per_million_tokens"), f"models[{key!r}].input"
        )
        output_price = _finite_non_negative(
            record.get("output_usd_per_million_tokens"), f"models[{key!r}].output"
        )
        identity = (provider, model_id)
        if identity in prices:
            raise PricingUnavailable(f"duplicate model price identity: {key}")
        prices[identity] = ModelPrice(
            provider=provider,
            model_id=model_id,
            input_usd_per_million_tokens=input_price,
            output_usd_per_million_tokens=output_price,
        )

    role_limits: dict[str, RoleTokenLimit] = {}
    for role, value in limits.items():
        if not isinstance(role, str) or not role:
            raise PricingUnavailable("role names must be non-empty strings")
        record = _require_mapping(value, f"role_token_limits[{role!r}]")
        role_limits[role] = RoleTokenLimit(
            max_input_tokens=_non_negative_int(
                record.get("max_input_tokens"), f"role_token_limits[{role!r}].max_input_tokens"
            ),
            max_output_tokens=_non_negative_int(
                record.get("max_output_tokens"), f"role_token_limits[{role!r}].max_output_tokens"
            ),
        )

    missing_roles = required_role_names() - set(role_limits)
    if missing_roles:
        raise PricingUnavailable(
            "pricing file is missing production role bounds: "
            + ", ".join(sorted(missing_roles))
        )
    return PricingConfig(path=source, prices=prices, role_token_limits=role_limits)


def priced_run_max_cost_usd(
    config: PricingConfig,
    *,
    provider: str,
    model_id: str,
    ceiling_usd: float,
) -> float:
    """The admission reservation ceiling derived from the configured envelope.

    The one-attempt-per-role envelope is an upper bound on the configured role
    token allowances; the run reservation is that envelope capped by the
    service/request ceiling. The ProviderBudgetGuard also receives this capped
    value, so actual settled cost can never exceed it.
    """
    if not math.isfinite(float(ceiling_usd)) or float(ceiling_usd) <= 0:
        raise PricingUnavailable("run cost ceiling must be finite and positive")
    price = config.price_for(provider, model_id)
    try:
        envelope = sum(
            price.bound_usd(
                max_input_tokens=limit.max_input_tokens,
                max_output_tokens=limit.max_output_tokens,
            )
            for limit in config.role_token_limits.values()
        )
    except (TypeError, ValueError) as exc:
        raise PricingUnavailable("configured role token envelope is invalid") from exc
    if not math.isfinite(envelope) or envelope <= 0:
        raise PricingUnavailable("configured role token envelope has no cost bound")
    return min(float(ceiling_usd), envelope)


class _DuplicateKeyError(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PricingUnavailable(f"{name} must be an object")
    return value


def _finite_non_negative(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PricingUnavailable(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise PricingUnavailable(f"{name} must be finite and non-negative")
    return number


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PricingUnavailable(f"{name} must be a non-negative integer")
    return value


__all__ = [
    "DEFAULT_PRICING_PATH",
    "PRICING_FILE_ENV",
    "PricingConfig",
    "PricingUnavailable",
    "load_pricing_config",
    "priced_run_max_cost_usd",
    "pricing_file_path",
    "required_role_names",
]
