"""timefields: external times under ingestion/ use one declared conversion module."""
import ast
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from catalyst_data.ingestion.time import timefields as tf

INGESTION = Path(__file__).resolve().parents[1] / "catalyst_data" / "ingestion"
TIMEFIELDS_IMPL = Path(tf.__file__).resolve()
FORBIDDEN_CALLS = {"parsedate_to_datetime", "strptime", "fromtimestamp", "utcfromtimestamp", "ZoneInfo"}


def _ingestion_python_files():
    return sorted(INGESTION.rglob("*.py"))


def _calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
            yield node, f, name


def test_no_ingestion_module_parses_a_time_by_itself():
    found = []
    for path in _ingestion_python_files():
        if path.resolve() == TIMEFIELDS_IMPL:
            continue
        for node, f, name in _calls(ast.parse(path.read_text())):
            where = f"{path.name}:{node.lineno}"
            if name in FORBIDDEN_CALLS:
                found.append(f"{where} {name}()")
            elif name == "fromisoformat" and isinstance(f, ast.Attribute) and getattr(f.value, "id", None) == "datetime":
                found.append(f"{where} datetime.fromisoformat()  (a date: date.fromisoformat; a time: timefields)")
            elif name == "replace" and any(k.arg == "tzinfo" for k in node.keywords):
                found.append(f"{where} .replace(tzinfo=...)  (a zone is never assumed outside timefields)")
    assert found == []


def test_every_external_call_names_a_declared_field():
    for path in _ingestion_python_files():
        if path.resolve() == TIMEFIELDS_IMPL:
            continue
        for node, f, name in _calls(ast.parse(path.read_text())):
            if name in ("external", "external_stamp", "external_date") and isinstance(f, ast.Attribute):
                kw = {k.arg: k.value.value for k in node.keywords
                      if k.arg in ("source", "field") and isinstance(k.value, ast.Constant)}
                if len(kw) < 2:
                    continue  # a computed source (press_rss: per feed) is checked against FIELDS by its caller
                assert (kw["source"], kw["field"]) in tf.FIELDS, f"{path.name}:{node.lineno} {kw}"


def test_canonical_timefields_import_points_at_the_real_implementation():
    from catalyst_data.ingestion.time import timefields as implementation

    assert TIMEFIELDS_IMPL == (INGESTION / "time" / "timefields.py").resolve()
    assert implementation is tf


def test_recursive_audit_includes_a_real_collector():
    assert INGESTION / "collectors" / "sec.py" in _ingestion_python_files()


def test_finnhub_epoch_and_the_known_skew_flag():
    tv = tf.external(1761163851, source="finnhub", field="datetime", publisher="CNBC")
    assert tv.stamp == "2025-10-22T20:10:51Z" and tv.precision == "second" and tv.flags == ("known_skew",)
    assert tf.external(1761163851, source="finnhub", field="datetime", publisher="Yahoo").flags == ()
    assert tf.external(0, source="finnhub", field="datetime").stamp is None  # no time, not 1970


def test_sec_json_is_unreliable_and_the_index_page_is_new_york_time():
    assert tf.external("2026-04-09T00:10:51.000Z", source="sec", field="acceptanceDateTime").flags == ("unreliable",)
    assert tf.external_stamp("2026-04-08 16:10:51", source="sec", field="index_accepted") == "2026-04-08T20:10:51Z"
    assert tf.external_stamp("2026-01-08 16:10:51", source="sec", field="index_accepted") == "2026-01-08T21:10:51Z"


def test_a_time_without_a_zone_is_missing_never_assumed():
    for raw in ("2026-10-22T16:05:00", "2026-10-22 16:05:00"):
        assert tf.external(raw, source="page", field="declared").stamp is None
    assert tf.external("Thu, 22 Oct 2026 16:05:00 -0000", source="rss", field="pubDate").stamp is None
    assert tf.external("Thu, 22 Oct 2026 16:05:00", source="rss", field="pubDate").stamp is None
    assert tf.external_stamp("Thu, 22 Oct 2026 16:05:00 -0400", source="rss", field="pubDate") == "2026-10-22T20:05:00Z"
    assert tf.external_stamp("2026-10-22T16:05:00-04:00", source="page", field="declared") == "2026-10-22T20:05:00Z"
    assert tf.external_stamp("garbage", source="page", field="declared") is None


def test_other_readings():
    assert tf.external_stamp("20261022T201500Z", source="gdelt", field="seendate") == "2026-10-22T20:15:00Z"
    assert tf.external_stamp(1761163851000, source="polygon", field="bar_t") == "2025-10-22T20:10:51Z"
    tv = tf.external("2026-10-22", source="finra", field="date")
    assert tv.utc is None and tv.day == date(2026, 10, 22) and tv.precision == "day"
    assert tf.external_stamp("Thu, 22 Oct 2026 16:05:00 GMT", source="tavily", field="published_date") == "2026-10-22T16:05:00Z"
    assert tf.external_date("2026-10-22T23:30:00-04:00", source="tavily", field="published_date") == date(2026, 10, 22)
    assert tf.dateline("October 22, 2026") == date(2026, 10, 22) and tf.dateline("10/22/2026") == date(2026, 10, 22)
    with pytest.raises(KeyError):
        tf.external("x", source="nobody", field="nothing")


def test_stored_values_round_trip_and_naive_is_refused():
    t = datetime(2026, 10, 22, 20, 10, 51, tzinfo=timezone.utc)
    assert tf.stored(tf.stamp(t)) == t
    with pytest.raises(ValueError):
        tf.stamp(datetime(2026, 10, 22, 20, 10, 51))
    with pytest.raises(ValueError):
        tf.stored("2026-10-22T20:10:51")
    assert tf.stored_or_none("2026-10-22T20:10:51") is None
    assert tf.ny_wall("2026-10-22", 16).astimezone(timezone.utc).hour == 20
    assert tf.ny_wall("2026-11-02", 16).astimezone(timezone.utc).hour == 21  # after the DST change


def test_nextera_ir_feed_fixed_est_clock():
    # 14:51 EDT (PR Newswire 18:51Z) written as 19:51 GMT; in EST the same rule changes nothing
    assert tf.external_stamp("Thu, 22 Oct 2026 19:51:00 GMT", source="rss:ir:NEE", field="pubDate") == "2026-10-22T18:51:00Z"
    assert tf.external_stamp("Thu, 03 Dec 2026 13:30:00 GMT", source="rss:ir:NEE", field="pubDate") == "2026-12-03T13:30:00Z"


def test_alpaca_fractional_update_time_rounds_up_for_safe_second_storage():
    assert tf.external_stamp(
        "2026-01-02T21:00:00.100Z", source="alpaca", field="updated_at"
    ) == "2026-01-02T21:00:01Z"
    assert tf.external_stamp(
        "2026-01-02T20:59:59.100Z", source="alpaca", field="updated_at"
    ) == "2026-01-02T21:00:00Z"


def test_alpaca_whole_second_update_time_is_unchanged():
    assert tf.external_stamp(
        "2026-01-02T21:00:00Z", source="alpaca", field="updated_at"
    ) == "2026-01-02T21:00:00Z"


def test_alpaca_fractional_created_time_rounds_up_too():
    assert tf.external_stamp(
        "2026-01-02T21:00:00.100Z", source="alpaca", field="created_at"
    ) == "2026-01-02T21:00:01Z"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-01-02T21:00:00.000001Z", "2026-01-02T21:00:01Z"),
        ("2026-01-02T21:00:00.999999Z", "2026-01-02T21:00:01Z"),
        ("2026-01-02T23:59:59.000001Z", "2026-01-03T00:00:00Z"),
    ],
)
def test_alpaca_fractional_update_time_rounds_up_across_boundaries(raw, expected):
    assert tf.external_stamp(raw, source="alpaca", field="updated_at") == expected
