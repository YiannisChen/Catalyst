from __future__ import annotations

from pathlib import Path

import pytest
from catalyst_agents.attribution.policy import resolve_attribution_policy
from catalyst_agents.engine.rules import V2Rules
from catalyst_app.deployment_configuration import (
    PUBLIC_DEPLOYMENT_COMMAND_LINE_ONLY,
    PUBLIC_DEPLOYMENT_PINNED_ENV,
    validate_public_deployment_environment,
)


def _unit_environment(environment_file: dict[str, str] | None = None) -> tuple[dict[str, str], dict[str, str]]:
    unit = next(
        parent / "deploy" / "api" / "catalyst-api.service"
        for parent in Path(__file__).resolve().parents
        if (parent / "deploy" / "api" / "catalyst-api.service").is_file()
    )
    unit_environment: dict[str, str] = {}
    file_environment: dict[str, str] = {}
    exec_prefix: dict[str, str] = {}
    for line_number, raw in enumerate(unit.read_text(encoding="utf-8").splitlines(), 1):
        if raw.startswith("EnvironmentFile="):
            file_environment.update(environment_file or {})
            continue
        if not raw.startswith("Environment="):
            if raw.startswith("ExecStart="):
                command = raw[len("ExecStart=") :].split()
                assert command[0] == "/usr/bin/env"
                for item in command[1:]:
                    if "=" not in item:
                        break
                    key, value = item.split("=", 1)
                    exec_prefix[key] = value
            continue
        payload = raw[len("Environment=") :]
        key, separator, value = payload.partition("=")
        assert separator, f"malformed Environment= line {line_number}: {raw!r}"
        assert key, f"missing Environment= key on line {line_number}"
        unit_environment[key] = value
    # systemd.exec precedence: Environment= < EnvironmentFile=; command-line
    # assignments from /usr/bin/env then override the environment passed in.
    environment = {**unit_environment, **file_environment, **exec_prefix}
    return environment, exec_prefix


def test_api_unit_pins_v21_policy_and_public_tavily_off() -> None:
    environment, forced = _unit_environment()

    expected_v2_rules = PUBLIC_DEPLOYMENT_PINNED_ENV["CATALYST_V2_RULES"]
    expected_window_policy = PUBLIC_DEPLOYMENT_PINNED_ENV["CATALYST_EVIDENCE_WINDOW_POLICY"]
    expected_web_evidence = PUBLIC_DEPLOYMENT_PINNED_ENV["CATALYST_WEB_EVIDENCE"]

    assert forced["CATALYST_V2_RULES"] == expected_v2_rules
    assert forced["CATALYST_EVIDENCE_WINDOW_POLICY"] == expected_window_policy
    assert forced["CATALYST_WEB_EVIDENCE"] == expected_web_evidence

    policy = resolve_attribution_policy(env=environment)

    parsed_rules = V2Rules.parse(policy.v2_rules)
    assert parsed_rules == V2Rules.parse(expected_v2_rules)
    assert all(parsed_rules.to_dict().values())
    assert policy.evidence_window_policy == expected_window_policy
    assert policy.web_evidence == expected_web_evidence
    assert policy.web_on_demand is False

    assert forced == {**PUBLIC_DEPLOYMENT_PINNED_ENV, **PUBLIC_DEPLOYMENT_COMMAND_LINE_ONLY}


def test_api_unit_uses_one_worker() -> None:
    unit = next(
        parent / "deploy" / "api" / "catalyst-api.service"
        for parent in Path(__file__).resolve().parents
        if (parent / "deploy" / "api" / "catalyst-api.service").is_file()
    )
    exec_start = next(
        line for line in unit.read_text(encoding="utf-8").splitlines()
        if line.startswith("ExecStart=")
    )
    unit_text = unit.read_text(encoding="utf-8")
    assert "After=network-online.target\n" in unit_text
    assert "After=network-online.target catalyst-daily.service" not in unit_text
    assert exec_start.split()[-2:] == ["--workers", "1"]
    assert "CATALYST_ANONYMOUS_DAILY_RUN_LIMIT=0" in exec_start
    environment, forced = _unit_environment()
    assert forced["CATALYST_DB_PATH"] == "/opt/catalyst/repo/data/m9/catalyst_corpus.sqlite3"
    assert forced["CATALYST_RUNTIME_DB_PATH"] == "/var/lib/catalyst-api/live_runtime.sqlite3"
    assert environment["CATALYST_DB_PATH"].endswith("catalyst_corpus.sqlite3")
    assert environment["CATALYST_RUNTIME_DB_PATH"].startswith("/var/lib/catalyst-api/")


def test_health_unit_reads_optional_environment_file() -> None:
    unit = next(
        parent / "deploy" / "ops" / "catalyst-health.service"
        for parent in Path(__file__).resolve().parents
        if (parent / "deploy" / "ops" / "catalyst-health.service").is_file()
    )

    assert "EnvironmentFile=-/opt/catalyst/.env" in unit.read_text(encoding="utf-8")


def test_api_unit_pins_every_public_value_after_hostile_environment_file() -> None:
    hostile = {
        key: f"hostile-{value or 'empty'}"
        for key, value in PUBLIC_DEPLOYMENT_PINNED_ENV.items()
    }
    hostile["CATALYST_FILING_UNITS"] = "off"
    environment, forced = _unit_environment(hostile)

    assert {
        key: environment[key] for key in PUBLIC_DEPLOYMENT_PINNED_ENV
    } == PUBLIC_DEPLOYMENT_PINNED_ENV
    assert forced == {**PUBLIC_DEPLOYMENT_PINNED_ENV, **PUBLIC_DEPLOYMENT_COMMAND_LINE_ONLY}
    assert environment["CATALYST_FILING_UNITS"] == ""
    validate_public_deployment_environment(environment)


def test_public_startup_check_rejects_unpinned_or_disabled_environment() -> None:
    valid = dict(PUBLIC_DEPLOYMENT_PINNED_ENV)
    validate_public_deployment_environment(valid)

    invalid_policy = dict(valid, CATALYST_V2_RULES="filing")
    with pytest.raises(RuntimeError, match="deployment_configuration"):
        validate_public_deployment_environment(invalid_policy)


def test_headline_archive_timer_precedes_daily_and_covers_yesterday() -> None:
    repo = next(parent for parent in Path(__file__).resolve().parents if (parent / "deploy").is_dir())
    service = (repo / "deploy/api/catalyst-headline-archive.service").read_text(encoding="utf-8")
    timer = (repo / "deploy/api/catalyst-headline-archive.timer").read_text(encoding="utf-8")
    wrapper = (repo / "deploy/api/run-headline-archive.sh").read_text(encoding="utf-8")
    daily_timer = (repo / "deploy/api/catalyst-daily.timer").read_text(encoding="utf-8")

    assert "Before=catalyst-daily.service" in service
    archive_time = next(line.split()[1] for line in timer.splitlines() if line.startswith("OnCalendar="))
    daily_time = next(line.split()[2] for line in daily_timer.splitlines() if line.startswith("OnCalendar="))
    assert archive_time == "10:30:00"
    assert daily_time == "11:00:00"
    assert "Persistent=true" in timer
    assert "END=$(date -u -d yesterday +%F)" in wrapper
    assert 'START=$(date -u -d "$END - 7 days" +%F)' in wrapper
    assert '--start "$START" --end "$END"' in wrapper



def test_api_unit_points_featured_cases_at_the_tracked_manifest() -> None:
    import json
    import re

    environment, _ = _unit_environment()
    repo = next(parent for parent in Path(__file__).resolve().parents if (parent / "deploy").is_dir())
    bundle = environment["CATALYST_FEATURED_BUNDLE"]
    manifest_path = environment["CATALYST_FEATURED_MANIFEST"]
    assert manifest_path.startswith("/opt/catalyst/repo/")
    tracked = repo / manifest_path[len("/opt/catalyst/repo/"):]
    manifest = json.loads(tracked.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "featured_manifest_v1"
    assert len(manifest["cases"]) == 8
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["bundle_content_sha256"])
    pinned = (tracked.parent / (Path(bundle).name + ".sha256")).read_text(encoding="utf-8").split()
    assert re.fullmatch(r"[0-9a-f]{64}", pinned[0]) and pinned[1] == Path(bundle).name
