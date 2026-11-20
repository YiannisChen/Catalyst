#!/usr/bin/env python3
"""Catalyst monitor, run on the second host (Hong Kong). Standard library only.

It pulls the data server's health report over SSH with a key restricted to a forced command (monitor-gate: the
report and the backups, nothing else), judges it, and mails alerts and a daily summary. It also pulls the nightly
backups and keeps seven days. The rules are here, in one place.

    monitor.py check          # every 15 min: alert on new problems (each at most once per 6 h)
    monitor.py daily          # once a day: summary mail
    monitor.py pull-backup    # once a day: copy the latest backup, verify sha256, keep 7 days
    monitor.py restore-drill  # monthly: restore the latest backup to a scratch dir, integrity check, row counts
    monitor.py test-email     # send one test mail

The daily summary is the monitor's own heartbeat: no summary by 15:00 UTC means this host or its mail is down.

Configuration: /opt/catalyst-monitor/.env (ALERT_SMTP_USER, ALERT_SMTP_PASSWORD, ALERT_TO, CATALYST_US_HOST).
"""
from __future__ import annotations

import email.message
import hashlib
import json
import os
import re
import shutil
import smtplib
import ssl
import statistics
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path(os.environ.get("CATALYST_MONITOR_HOME", "/opt/catalyst-monitor"))
STATE = HOME / "state.json"
BACKUP = HOME / "backup"
KEY = HOME / "id_monitor"
KEEP_DAYS = 7

# Rules. Which sources exist, how often each is expected to complete and how loudly it alerts come from the source
# registry (data/manifests/sources_v1.json on the data server), which the health report carries; nothing about
# individual sources is configured here.
REPORT_MAX_AGE_MIN = 45            # the data server writes a report every 10 min
ERROR_RATE_MAX = 0.10              # per source over 24 h, with at least MIN_ATTEMPTS
MIN_ATTEMPTS = 10
ACTIVE = ("enabled", "trial")      # registry statuses that are run and checked
# "Silent": no completed request (OK or EMPTY; EMPTY is not a failure) for SILENT_FACTOR x the source's expected
# interval (registry expected_hours; weekday-only timers include the weekend gap), at least SILENT_MIN_HOURS.
SILENT_FACTOR = 3
SILENT_MIN_HOURS = 1
NEW_STORE_GRACE_H = 72             # a store not created yet is fine for this long after its sources' ``since``
# "Fewer OK than usual" (a parser that breaks without raising): the 24 h OK count is below half the median of the
# seven earlier days and below half of the same window a week ago (so a weekend compares with a weekend); or the
# EMPTY share is at least twice the usual and EMPTY_JUMP above it. Only for sources with a full week of history and
# a usual OK count of at least USUAL_OK_MIN.
USUAL_OK_MIN = 10
EMPTY_JUMP = 0.30
# Time stamps (2026-10-22 review): per publisher, the share of company bodies whose API time and page time differ by
# a whole 4 or 5 hours, over at least TIME_MIN_COMPARED bodies in 24 h.
WHOLE_HOUR_MAX = 0.01
TIME_MIN_COMPARED = 20
# Independent sources for one event (health cross_checks, e.g. an 8-K's exhibit against the Benzinga results wire):
# pairs over 7 days; unrelated pairs rarely land on a whole 4/5 h, so a higher share is a clock error.
CROSS_MIN_COMPARED = 10
# Fetching: logical task completion over 7 days; legacy calls stay separate.
FETCH_MIN_REQUESTS = 50
FETCH_EVENTUAL_MIN = 0.95
CROSS_WHOLE_HOUR_MAX = 0.05
BACKUP_DUE_UTC = 16                # the data server's backup starts 15:00 UTC and takes about a minute
BACKUP_MAX_AGE_H = 26
DAILY_DONE_BY_UTC = 14             # the daily job starts 11:00 UTC and has finished by 13:05 in practice
DISK_FREE_MIN_PCT = 15
REALERT_HOURS = 6
UNIT_STUCK_HOURS = 2               # a failed service still failed this long is mailed whatever its source's tier

# Tiers: 1 = mailed at once (data correctness, or attribution as a whole would fail); 2 = in the daily summary only;
# 3 = recorded only (state.json, and the daily summary's last section). A source's problems take the source's tier.
# Per kind: the failure in docs/data/operations.md, what it affects, what the system has done, what a person does.
KINDS = {
    "unreachable": (1, "D10", "看不到数据服务器的状态（采集本身不一定停了）", "每 15 分钟重试", "ssh 上数据服务器，看 uptime、systemctl --failed、journalctl -u catalyst-health"),
    "stale_report": (1, "D11", "监控读到的是旧报告，其他判断都不可信", "每 15 分钟重试", "systemctl status catalyst-health.timer；必要时 systemctl restart catalyst-health.timer"),
    "no_registry": (1, "D11", "健康报告里没有来源登记表，来源检查全部跳过", "无", "确认数据服务器上的 collectors 检出是最新的（deploy/deploy_collectors.sh）"),
    "daily_not_started": (1, "D20", "今天的会话没有新闻和行情，归因会全部停用或用旧数据", "定时任务 Persistent=true，重启后补跑", "systemctl status catalyst-daily；手动 systemctl start catalyst-daily"),
    "daily_not_done": (1, "D21", "今天的数据可能不完整", "无（任务本身有每项预算和重试）", "journalctl -u catalyst-daily 只看状态和错误字段；必要时再跑一次"),
    "daily_failed": (1, "D22", "该阶段的数据缺失，数据就绪会停用对应依据", "失败项下一次运行重试", "按 runbook 的日更故障一节处理"),
    "daily_postprocess_failed": (1, "D23", "ready_range 或正文 FTS 后处理没有成功，范围快照可能过期或新闻检索未更新", "下次日更会重试；正文 FTS 可切回前一 published build", "查看 catalyst-health 报告的 daily_pipeline 与 catalyst-daily journal；磁盘不足先释放空间"),
    "health_check_unavailable": (1, "D23", "健康检查读取失败，相关监控结论暂不可用", "已保留可读取的部分结果和此前告警状态", "检查报告中的数据库路径、schema 或 systemctl 错误；修复后确认健康报告恢复"),
    "store": (1, "D30", "该库的所有来源都读不到（库缺失、损坏或被锁）", "无", "sqlite3 <库> 'PRAGMA integrity_check'；损坏则从备份恢复（runbook 恢复演练一节）"),
    "sec_exhibit_units": (1, "D30", "正文库的 SEC 附件单元不可用（原因见标题），filing 规则与 8-K 读取会拿不到附件", "无", "按原因处理：缺表或空表时确认部署用的正文库来源并核对 sec_exhibit_units 行数；损坏、锁超时或库缺失时检查 news_bodies_v2.sqlite3"),
    "errors": (2, "D01", "该来源的数据缺一部分", "下一次定时运行重试；429 按令牌桶退避", "看 journalctl 的错误字段；持续多天则在登记表把它标为停用"),
    "degradation": (1, "D02", "该来源连续 3 天没有成功完成", "采集继续运行；只建议停用，不自动改来源登记表", "核对错误原因；确认停用后由人修改登记表并部署"),
    "degradation_unknown": (3, "D02", "缺少可信的首次观测时间，连续失败时长未知", "不推断失败天数", "确认健康报告是否包含可信的账本首次观测时间"),
    "silent": (2, "D02", "该来源没有新数据", "定时任务继续按时运行", "systemctl status <定时器>；接口改版、额度用完或 key 过期见 D02"),
    "no_key": (2, "D04", "该来源每次都因为没有 key 而跳过，完全没有数据", "无", "检查 /opt/catalyst/.env 里对应的 key 是否还在（不要打印值）；过期则续期后重新部署"),
    "never_ok": (2, "D03", "该来源（或其中一个订阅）试了多天，一次成功都没有", "无", "对照页面看返回的格式：不是 RSS 就加解析或停用，确实没有内容就在登记表说明"),
    "usual": (2, "D03", "该来源可能在静默地丢数据（解析失效）", "无", "抽几条最新记录对比页面，修解析器"),
    "never_ran": (2, "D02", "登记为启用的来源没有任何记录", "无", "确认定时器已安装并启用（sources timers 与 systemctl list-timers 对照）"),
    "unregistered": (3, "D60", "账本里有登记表不认识的来源", "不检查它", "在登记表补一行，或停掉它"),
    "time": (1, "D40", "该发布方的发布时间有整小时偏差，可能把事后的新闻当成事前", "按较晚的时间入库（times.reconcile）", "按时区审计流程核对，把发布方加入 times.SKEWED"),
    "backup_missing": (1, "D50", "今天没有备份，出事时最多丢一天", "无", "systemctl status catalyst-backup；手动 systemctl start catalyst-backup"),
    "backup_stale": (1, "D50", "超过一天没有新备份", "无", "同上"),
    "pull_failed": (1, "D51", "监控主机上没有今天的异地备份", "下一次拉取重试", "在监控主机上 monitor.py pull-backup"),
    "unit": (1, "D12", "该服务这次没有完成", "下一次定时运行重试", "journalctl -u <服务> 看状态和错误字段"),
    "fetch": (2, "D01", "该来源有一部分采集任务尚未完成或失败", "可恢复的失败按 resilient 规则重试；具体队列状态见健康报告", "看健康报告 fetching 里的错误类型：429 多则降频，403/404 多则查接口或停用"),
    "timer_missing": (2, "D61", "登记表里的定时器没有装在服务器上", "无", "deploy/deploy_collectors.sh 重新部署"),
    "timer_unregistered": (3, "D61", "服务器上有登记表没写的定时器（临时研究任务）", "无", "用完后停掉，或补进登记表"),
    "disk": (1, "D52", "磁盘满会让所有写库失败", "分钟线按 90 天滚动清理（有备份才删）", "du -sh /opt/catalyst/*；清理 snap/ 与 research/ 里不再需要的文件"),
    "monitor_disk": (1, "D53", "监控主机磁盘满会让备份拉取失败", "备份只保留 7 天", "清理 /opt/catalyst-monitor/backup"),
}


def problem(kind: str, text: str, tier: int | None = None) -> dict:
    default, ref, impact, auto, human = KINDS[kind]
    return {"tier": default if tier is None else tier, "kind": kind, "ref": ref, "text": text, "impact": impact,
            "auto": auto, "human": human}


def load_env(path: Path = HOME / ".env") -> dict:
    env = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse(ts: str | None) -> datetime | None:
    """ISO 8601 with Z or an offset, with or without fractions; naive means UTC."""
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def gate(command: str, *, binary: bool = False, timeout: int = 120):
    host = ENV.get("CATALYST_US_HOST") or os.environ["CATALYST_US_HOST"]
    out = subprocess.run(
        ["ssh", "-i", str(KEY), "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
         "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={HOME / 'known_hosts'}",
         host, command], capture_output=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(f"gate {command.split()[0]} failed ({out.returncode}): {out.stderr.decode()[-300:]}")
    return out.stdout if binary else out.stdout.decode()


def send(subject: str, body: str) -> None:
    user, password = ENV["ALERT_SMTP_USER"], ENV["ALERT_SMTP_PASSWORD"]
    msg = email.message.EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, ENV.get("ALERT_TO", user), subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=60) as s:
        s.login(user, password)
        s.send_message(msg)


# ------------------------------------------------------------------------------------------------- judging

def _match(key: str, pattern: str) -> bool:
    from fnmatch import fnmatchcase
    return key == pattern or fnmatchcase(key, pattern)


def entry_for(registry: dict, store: str, key: str) -> dict | None:
    entries = [e for e in registry.get("sources", ()) if e["store"] == store]
    return next((e for e in entries if e["key"] == key), None) or next(
        (e for e in entries if _match(key, e["key"])), None)


def silent_limit_hours(entry: dict) -> float | None:
    hours = entry.get("expected_hours")
    return None if hours is None else max(SILENT_FACTOR * hours, SILENT_MIN_HOURS)


def _young(entry: dict, at: datetime, hours: float) -> bool:
    since = parse(entry.get("since"))
    return since is not None and at - since < timedelta(hours=hours)


def _current_report(report: dict | None, at: datetime) -> bool:
    """Only fresh reports whose clock does not appear to be ahead of the monitor can advance failure time."""
    if report is None:
        return False
    generated = parse(report.get("generated_at"))
    return generated is not None and timedelta(0) <= at - generated <= timedelta(minutes=REPORT_MAX_AGE_MIN)


def _store_health_cleared(report: dict | None, at: datetime, field: str) -> bool:
    """A prior hard-store alert recovers only on a fresh report with explicit non-error status."""
    if not _current_report(report, at):
        return False
    result = report.get(field)
    return isinstance(result, dict) and "status" in result and result["status"] != "error"


def _health_items(result: object) -> dict:
    """Payload items for a status-wrapped health check; errors and malformed rows are not findings."""
    if not isinstance(result, dict) or result.get("status") == "error":
        return {}
    return {key: value for key, value in result.items() if key not in {"status", "reason"}
            and isinstance(value, dict) and value.get("status") != "error"}


def _degradation_status(source: dict, generated: datetime, at: datetime) -> tuple[str, float | None]:
    """Return (status, elapsed hours) using only timestamps at or before the trusted health report time."""
    last_done_value = source.get("last_done_at")
    first_observed_value = source.get("first_observed_at")
    last_done = parse(last_done_value)
    first_observed = parse(first_observed_value)
    if last_done_value is not None and last_done is None:
        return "unknown", None
    if first_observed_value is not None and first_observed is None:
        return "unknown", None
    if (last_done is not None and (last_done > generated or last_done > at)) or \
            (first_observed is not None and (first_observed > generated or first_observed > at)):
        return "unknown", None
    if last_done is not None and first_observed is not None and first_observed > last_done:
        return "unknown", None
    anchor = last_done or first_observed
    if anchor is None:
        return "unknown", None
    return "known", max(0.0, (at - anchor).total_seconds() / 3600)


def _degradation_problem(key: str, source: dict | None, generated: datetime, at: datetime) -> dict:
    if source is None:
        return problem("degradation_unknown", f"{key} 连续失败时长未知（没有首次有效观测时间）")
    status, elapsed_hours = _degradation_status(source, generated, at)
    if status == "unknown":
        return problem("degradation_unknown", f"{key} 连续失败时长未知（缺少可信的首次观测或时间戳）")
    if elapsed_hours is None or elapsed_hours < 72:
        return {}
    elapsed_days = int(elapsed_hours // 24)
    result = problem("degradation", f"{key} 已连续 {elapsed_days} 天没有成功完成，建议停用；登记表未自动修改")
    result["failure_days"] = elapsed_days
    return result


def problems(report: dict | None, error: str | None, at: datetime) -> dict[str, dict]:
    """{problem key: problem(kind, text, tier)} for everything wrong now."""
    out: dict[str, dict] = {}
    if report is None:
        out["unreachable"] = problem("unreachable", f"无法取得数据服务器的健康报告：{error}")
        return out
    gen = parse(report.get("generated_at"))
    if gen is None or at - gen > timedelta(minutes=REPORT_MAX_AGE_MIN):
        out["stale_report"] = problem("stale_report", f"健康报告过期：生成于 {report.get('generated_at')}")
    report_current = _current_report(report, at)
    job = report.get("daily_job") or {}
    if at.weekday() in (1, 2, 3, 4, 5) and at.hour >= DAILY_DONE_BY_UTC:  # Tue..Sat, after the usual finish
        created = parse(job.get("created_at"))
        if created is None or created.date() != at.date():
            out["daily_not_started"] = problem("daily_not_started",
                                               f"今天的日更任务没有启动（最近一次：{job.get('job_id')}）")
        elif job.get("status") in ("PENDING", "RUNNING"):
            out["daily_not_done"] = problem("daily_not_done", f"日更任务 {job.get('job_id')} 到 {DAILY_DONE_BY_UTC}:00 UTC"
                                            f" 仍未完成（{job.get('status')}）")
    for stage, counts in (job.get("stages") or {}).items():
        if stage != "deferred" and counts.get("FAILED"):  # deferred: hosts briefly down, retried later
            out[f"daily_failed:{stage}"] = problem("daily_failed", f"日更任务 {job.get('job_id')} 的 {stage} 阶段有 "
                                                   f"{counts['FAILED']} 项失败")
    pipeline = report.get("daily_pipeline") or {}
    for step, label in (("ready_snapshot", "ready_range 快照"), ("corpus_fts", "正文 FTS 重建")):
        result = pipeline.get(step) or {}
        if result.get("status") == "failed":
            reason = result.get("reason") or result.get("reason_code") or "unknown error"
            out[f"daily_postprocess:{step}"] = problem(
                "daily_postprocess_failed", f"日更后的{label}失败：{reason}"
            )
    failed_stores = {row.get("store") for row in report.get("sources", ())
                     if row.get("missing_store") or row.get("error")}
    dependencies = {"dead_links": {"daily"}, "cross_checks": {"daily", "alpaca_news"}}
    health_checks = ("page_versions", "dead_links", "time_checks", "cross_checks")
    for check in health_checks:
        result = report.get(check)
        if isinstance(result, dict) and result.get("status") == "error":
            if dependencies.get(check, set()) & failed_stores:
                continue  # the same unreadable backing store already has a D30 alert
            reason = result.get("reason") or "unknown error"
            out[f"health_check_unavailable:{check}"] = problem(
                "health_check_unavailable", f"健康检查 {check} 检查不可用：{reason}"
            )
    never_result = report.get("never_ok")
    if isinstance(never_result, dict) and never_result.get("errors"):
        for store, reason in never_result["errors"].items():
            if store in failed_stores:
                continue  # the same unreadable store already has its D30 alert
            out[f"health_check_unavailable:never_ok:{store}"] = problem(
                "health_check_unavailable", f"never_ok 检查 {store} 不可用：{reason}"
            )
    elif isinstance(never_result, dict) and never_result.get("status") == "error":
        out["health_check_unavailable:never_ok"] = problem(
            "health_check_unavailable", f"never_ok 检查不可用：{never_result.get('reason') or 'unknown error'}"
        )
    unit_result = report.get("units")
    if isinstance(unit_result, dict) and unit_result.get("errors"):
        for command, reason in unit_result["errors"].items():
            out[f"health_check_unavailable:units:{command}"] = problem(
                "health_check_unavailable", f"units 检查不可用（{command}）：{reason}"
            )
    elif isinstance(unit_result, dict) and unit_result.get("status") == "error":
        out["health_check_unavailable:units"] = problem(
            "health_check_unavailable", f"units 检查不可用：{unit_result.get('reason') or 'unknown error'}"
        )
    registry = report.get("registry")
    if not registry:
        out["no_registry"] = problem("no_registry", "健康报告里没有来源登记表")
        registry = {"sources": []}
    entries = [e for e in registry["sources"] if e["status"] in ACTIVE]
    seen: set[str] = set()
    for s in report.get("sources", ()):
        name = f"{s['store']}:{s['source']}"
        if s.get("missing_store") or s.get("error"):
            store_entries = [e for e in entries if e["store"] == s["store"]]
            seen.update(e["id"] for e in store_entries)
            waiting = all(_young(e, at, max(silent_limit_hours(e) or 0, NEW_STORE_GRACE_H)) for e in store_entries)
            if store_entries and not (s.get("missing_store") and waiting):
                out[f"store:{s['store']}"] = problem("store", f"{s['store']} 读不到：{s.get('missing_store') or s.get('error')}")
            continue
        entry = entry_for(registry, s["store"], s["source"])
        if entry is None:
            out[f"unregistered:{name}"] = problem("unregistered", f"{name} 不在来源登记表里")
            continue
        seen.add(entry["id"])
        if entry["status"] not in ACTIVE:
            continue  # disabled or planned: not checked
        tier = entry["tier"]
        if (report_current and "first_observed_at" in s
                and not (s.get("DISABLED", 0) and not s.get("first_observed_at")
                         and not s.get("last_done_at"))):
            degradation = _degradation_problem(name, s, gen, at)
            if degradation:
                out[f"degradation:{name}"] = degradation
        attempts = s.get("attempts", 0)
        if attempts >= MIN_ATTEMPTS and s["ERROR"] / attempts > ERROR_RATE_MAX:
            out[f"errors:{name}"] = problem("errors", f"{name} 过去 24 小时失败率 {s['ERROR']}/{attempts}", tier)
        if s.get("DISABLED", 0):
            if not s.get("OK") and not s.get("EMPTY"):
                out[f"no_key:{name}"] = problem("no_key", f"{name} 过去 24 小时 {s['DISABLED']} 次因为没有 key 跳过，"
                                                f"没有一次成功", tier)
            continue  # the silence rule does not apply to a source the collector skipped
        limit = silent_limit_hours(entry)
        last_done = parse(s.get("last_done_at"))
        if limit is not None and (last_done is None or at - last_done > timedelta(hours=limit)) \
                and not _young(entry, at, limit):
            out[f"silent:{name}"] = problem("silent", f"{name} 超过 {limit:g} 小时（预期间隔的 {SILENT_FACTOR} 倍）没有"
                                            f"一次成功（最后成功：{s.get('last_done_at')}）", tier)
        drop = fewer_ok_than_usual(s)
        if drop:
            out[f"usual:{name}"] = problem("usual", f"{name} {drop}", tier)
    never_ok = report.get("never_ok") or ()
    if isinstance(never_ok, dict):
        never_ok = never_ok.get("items", ())
    for row in never_ok:
        out[f"never_ok:{row['store']}:{row['unit']}"] = problem(
            "never_ok", f"{row['store']}:{row['unit']} 自 {row['first_attempt_at']} 起试了 {row['attempts']} 次，"
                        f"一次成功都没有")
    for e in entries:
        limit = silent_limit_hours(e)
        if e["id"] not in seen and limit is not None and not _young(e, at, limit):
            out[f"never_ran:{e['id']}"] = problem("never_ran", f"{e['id']} 登记为{e['status']}，但账本里没有任何记录",
                                                  e["tier"])
    for pub, t in _health_items(report.get("time_checks")).items():
        bad = t.get("whole_hour", 0) + t.get("disagree", 0)  # known_offset: SEC's JSON, corrected from the index
        if t.get("compared", 0) >= TIME_MIN_COMPARED and bad / t["compared"] > WHOLE_HOUR_MAX:
            out[f"time:{pub}"] = problem("time", f"{pub} 的两个时间不一致 {bad}/{t['compared']}（整小时 "
                                         f"{t.get('whole_hour', 0)}，其他 {t.get('disagree', 0)}；过去 24 小时）")
    for name, t in _health_items(report.get("cross_checks")).items():
        if t.get("compared", 0) >= CROSS_MIN_COMPARED and t.get("whole_hour", 0) / t["compared"] > CROSS_WHOLE_HOUR_MAX:
            out[f"cross:{name}"] = problem("time", f"{name}：同一事件在两个独立来源的时间整小时偏差 "
                                           f"{t['whole_hour']}/{t['compared']}（过去 7 天）")
    b = report.get("backup") or {}
    made = parse(b.get("manifest_at"))
    if at.hour >= BACKUP_DUE_UTC and b.get("latest") != at.date().isoformat():
        out["backup_missing"] = problem("backup_missing", f"数据服务器今天的备份没有产生（最新：{b.get('latest')}）")
    if made is None or at - made > timedelta(hours=BACKUP_MAX_AGE_H):
        out["backup_stale"] = problem("backup_stale", f"数据服务器超过 {BACKUP_MAX_AGE_H} 小时没有新备份（最新："
                                      f"{b.get('latest')}，{b.get('manifest_at')}）")
    fetching = report.get("fetching") or {}
    if fetching.get("schema_version") == 2:
        for source, item in fetching.get("sources", {}).items():
            task = item.get("tasks", {})
            rate = task.get("rate")
            if source != "news_archive:gdelt" and task.get("registered", 0) >= FETCH_MIN_REQUESTS \
                    and rate is not None and rate < FETCH_EVENTUAL_MIN:
                out[f"fetch:{source}"] = problem("fetch", f"{source} 过去 7 天任务完成率 {rate:.1%}"
                    f"（成功 {task.get('succeeded', 0)}/{task['registered']}；"
                    f"等待 {task.get('pending', 0)}，重试中 {task.get('retrying', 0)}）")
    else:
        for provider, v in fetching.items():
            rate = v.get("ok_after_retries") if isinstance(v, dict) else None
            if provider != "gdelt" and isinstance(v, dict) and v.get("requests", 0) >= FETCH_MIN_REQUESTS \
                    and rate is not None and rate < FETCH_EVENTUAL_MIN:
                out[f"fetch:{provider}"] = problem("fetch", f"{provider} 过去 7 天旧口径调用完成率 {rate:.1%}"
                    f"（{v['requests']} 次调用；与三层统计不可比）")
    units = report.get("units") or {}
    infra = set(registry.get("infrastructure_timers", {}))
    for unit in units.get("failed", ()):
        base = unit.rsplit(".", 1)[0]
        tiers = [e["tier"] for e in entries if base in e["timers"]]
        tier = 1 if base in infra or not tiers else min(tiers)
        out[f"unit:{unit}"] = problem("unit", f"服务失败：{unit}", tier)
    for t in units.get("not_installed", ()):
        out[f"timer_missing:{t}"] = problem("timer_missing", f"定时器 {t} 在登记表里，但服务器上没有")
    for t in units.get("unregistered", ()):
        out[f"timer_unregistered:{t}"] = problem("timer_unregistered", f"服务器上的定时器 {t} 不在登记表里")
    exhibit_health = report.get("sec_exhibit_units") or {}
    if exhibit_health.get("status") == "error":
        reason = exhibit_health.get("reason", "unknown")
        out["sec_exhibit_units"] = problem(
            "sec_exhibit_units", f"正文库 SEC 附件单元检查失败（{reason}），filing 规则与 8-K 读取会拿不到附件"
        )
    d = report.get("disk") or {}
    if d.get("free_pct") is not None and d["free_pct"] < DISK_FREE_MIN_PCT:
        out["disk"] = problem("disk", f"数据服务器磁盘剩余 {d['free_pct']}%（{d.get('free_gb')} GB）")
    local = shutil.disk_usage("/")
    if 100 * local.free / local.total < DISK_FREE_MIN_PCT:
        out["monitor_disk"] = problem("monitor_disk", f"监控主机磁盘剩余 {100 * local.free / local.total:.1f}%")
    return out


def _held_problem_keys(report: dict | None, prior_keys) -> set[str]:
    """Prior alerts whose underlying check failed now cannot be declared recovered."""
    if not report:
        return set()
    held = set()
    for field, prefix in (("time_checks", "time:"), ("cross_checks", "cross:")):
        result = report.get(field)
        if isinstance(result, dict) and result.get("status") == "error":
            held.update(key for key in prior_keys if key.startswith(prefix))
    never = report.get("never_ok")
    if isinstance(never, dict):
        errors = never.get("errors") or {}
        for store in errors:
            prefix = f"never_ok:{store}:"
            held.update(key for key in prior_keys if key.startswith(prefix))
        if never.get("status") == "error" and not errors:
            held.update(key for key in prior_keys if key.startswith("never_ok:"))
    units = report.get("units")
    if isinstance(units, dict):
        errors = units.get("errors") or {}
        if {"failed_units", "activating_units"} & errors.keys():
            held.update(key for key in prior_keys if key.startswith("unit:"))
        if "timers" in errors:
            held.update(key for key in prior_keys if key.startswith(("timer_missing:", "timer_unregistered:")))
    return held


def describe(p: dict) -> str:
    """One problem as it appears in a mail: what happened, what it affects, what was done, what to do, where."""
    return (f"- {p['text']}\n  影响：{p['impact']}\n  系统已做：{p['auto']}\n  需要人做：{p['human']}\n"
            f"  见 docs/data/operations.md {p['ref']}")


def fewer_ok_than_usual(s: dict) -> str | None:
    hist = s.get("history") or []
    if len(hist) < 7 or any(sum(h.values()) == 0 for h in hist):
        return None  # not a full week of history yet
    usual = statistics.median(h["OK"] for h in hist)
    if usual < USUAL_OK_MIN:
        return None
    week_ago = hist[6]["OK"]
    if s["OK"] < usual / 2 and s["OK"] < week_ago / 2:
        return f"过去 24 小时成功 {s['OK']} 次，平时中位 {usual:g} 次、上周同期 {week_ago} 次（可能是解析出了问题）"
    attempts = s["OK"] + s["EMPTY"] + s["ERROR"]
    usual_share = statistics.median(h["EMPTY"] / sum(h.values()) for h in hist)
    if attempts >= MIN_ATTEMPTS:
        share = s["EMPTY"] / attempts
        if share >= 2 * usual_share and share >= usual_share + EMPTY_JUMP:
            return f"过去 24 小时为空的比例 {share:.0%}，平时 {usual_share:.0%}（可能是解析出了问题）"
    return None


def fetch_report() -> tuple[dict | None, str | None]:
    try:
        return json.loads(gate("health")), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {str(exc)[:300]}"


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def escalate_stuck_units(found: dict[str, dict], state: dict, at: datetime, running: frozenset[str] = frozenset()) -> None:
    """A tier-2 source's failed service is only in the daily summary, and the summary resets every 24 h, so an hourly
    collector that failed on every run for a day (alpaca news, 10-29) was never mailed. Remember since when each
    service has been continuously failed and raise it to tier 1 once that is UNIT_STUCK_HOURS."""
    previous = state.get("unit_failed_since", {})
    if not isinstance(previous, dict):
        previous = {}
    since = {k: previous.get(k, at.isoformat()) for k, p in found.items() if p.get("kind") == "unit"}
    for k, first in since.items():
        try:
            stuck = at - parse(first) >= timedelta(hours=UNIT_STUCK_HOURS)
        except (TypeError, ValueError):  # a damaged state file must not stop the check that mails
            since[k], stuck = at.isoformat(), False
        if found[k]["tier"] > 1 and stuck:
            found[k] = {**found[k], "tier": 1, "text": f"{found[k]['text']}（已连续失败 {UNIT_STUCK_HOURS} 小时以上）"}
    # a failed service that is running again is not recovered until it finishes: keep its clock meanwhile
    since.update({k: v for k, v in previous.items() if k in running and k not in since})
    state["unit_failed_since"] = since


def check() -> int:
    """Mail tier-1 problems at once (each at most once per REALERT_HOURS) and their recovery; keep tier 2 and 3 for
    the daily summary."""
    at = now()
    report, error = fetch_report()
    found = problems(report, error, at)
    state = load_state()
    pull = state.get("pull") or {}
    if pull and not pull.get("ok"):
        found["pull_failed"] = problem("pull_failed", f"监控主机拉取备份失败（{pull.get('at')}）：{pull.get('error')}")
    previous_unit_failures = dict(state.get("unit_failed_since") or {})
    escalate_stuck_units(found, state, at, frozenset(f"unit:{u}" for u in (report or {}).get("units", {}).get("activating", ())))
    unit_errors = (report or {}).get("units", {}).get("errors", {})
    if {"failed_units", "activating_units"} & unit_errors.keys():
        state["unit_failed_since"].update(previous_unit_failures)
    urgent = {k: p for k, p in found.items() if p["tier"] == 1}
    low = state.get("low", {})
    for k, p in found.items():
        if p["tier"] > 1:
            low[k] = {**p, "first": low.get(k, {}).get("first", at.isoformat()), "last": at.isoformat()}
    sent = state.get("alerted", {})
    fresh = {k: p for k, p in urgent.items()
             if k not in sent or (p.get("kind") != "degradation"
                                  and at - parse(sent[k]) > timedelta(hours=REALERT_HOURS))}
    report_current = _current_report(report, at)
    held = _held_problem_keys(report, sent)
    store_health_fields = {"sec_exhibit_units"}
    resolved = [
        k for k in sent
        if k not in urgent and k not in held
        and not (k.startswith("degradation:") and (not report_current or
                 (k in found and found[k].get("kind") == "degradation_unknown")))
        and not (k in store_health_fields and not _store_health_cleared(report, at, k))
    ]
    if fresh:
        body = "\n\n".join(describe(p) for p in fresh.values())
        others = [p["text"] for k, p in urgent.items() if k not in fresh]
        if others:
            body += "\n\n仍未解决（之前已通知）：\n" + "\n".join(f"- {t}" for t in others)
        send(f"[Catalyst] 告警：{len(fresh)} 项", body + f"\n\n检查时间：{at:%Y-%m-%d %H:%M} UTC")
        for k in fresh:
            sent[k] = at.isoformat()
    if resolved:
        send("[Catalyst] 已恢复", "\n".join(f"- {k}" for k in resolved) + f"\n\n检查时间：{at:%Y-%m-%d %H:%M} UTC")
    for k in resolved:
        sent.pop(k, None)
    reminder_days = state.get("degradation_reminder_days", {})
    if not isinstance(reminder_days, dict):
        reminder_days = {}
    if report_current:
        active_degradations = {k for k, p in found.items()
                               if p.get("kind") in ("degradation", "degradation_unknown")}
        reminder_days = {k: day for k, day in reminder_days.items() if k in active_degradations}
    state.update({"alerted": sent, "low": low, "degradation_reminder_days": reminder_days,
                  "last_check": at.isoformat(), "last_found": sorted(found)})
    STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    print(json.dumps({"found": sorted(found), "mailed": sorted(fresh), "resolved": resolved}))
    return 0


def daily() -> int:
    at = now()
    report, error = fetch_report()
    found = problems(report, error, at)
    state = load_state()
    low = state.get("low", {})
    for k, p in found.items():  # the current ones too, in case no check ran since
        if p["tier"] > 1:
            low.setdefault(k, {**p, "first": at.isoformat(), "last": at.isoformat()})
    urgent = [p for p in found.values() if p["tier"] == 1]
    lines = [f"Catalyst 日报 {at:%Y-%m-%d}（UTC）", ""]
    lines.append("一级问题（已即时告警）：" + ("无" if not urgent else ""))
    lines += [f"- {p['text']}" for p in urgent]
    tier2 = [(k, p) for k, p in sorted(low.items()) if p["tier"] == 2]
    held = _held_problem_keys(report, set(low) | set(state.get("alerted", {})))
    lines += ["", "二级问题（过去 24 小时出现过，只在日报里）：" + ("无" if not tier2 else "")]
    lines += [describe(p) + ("" if k in found or k in held else f"\n  （已恢复，最后一次 {p['last'][:16]}）")
              for k, p in tier2]
    reminder_days = state.get("degradation_reminder_days", {})
    if not isinstance(reminder_days, dict):
        reminder_days = {}
    report_current = _current_report(report, at)
    if report_current:
        active_degradations = {k for k, p in found.items()
                               if p.get("kind") in ("degradation", "degradation_unknown")}
        reminder_days = {k: day for k, day in reminder_days.items() if k in active_degradations}
        today = at.date().isoformat()
        due = [(k, p) for k, p in sorted(found.items())
               if p.get("kind") == "degradation" and p.get("failure_days", 0) >= 7
               and reminder_days.get(k) != today]
        if due:
            lines += ["", "持续失败满 7 天（每天提醒一次）："]
            lines += [f"  {p['text']}" for _, p in due]
            for key, _ in due:
                reminder_days[key] = today
    if report:
        job = report.get("daily_job") or {}
        lines += ["", f"日更任务：{job.get('job_id')}  状态 {job.get('status')}  完成于 {job.get('finished_at')}"]
        for stage, counts in sorted((job.get("stages") or {}).items()):
            lines.append(f"  {stage}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        p = report.get("prices") or {}
        lines += ["", f"行情：最近交易日 {p.get('last_spy_session')}，来源 {p.get('bars_by_source')}"]
        lines += ["", "各数据源（过去 24 小时：成功 / 为空 / 失败 / 未启用；最后成功时间；登记状态）："]
        registry = report.get("registry") or {"sources": []}
        for check in ("page_versions", "dead_links", "time_checks", "cross_checks"):
            result = report.get(check)
            if isinstance(result, dict) and result.get("status") == "error":
                lines.append(f"  {check} 检查不可用：{result.get('reason') or 'unknown error'}")
        for check in ("never_ok", "units"):
            result = report.get(check)
            if isinstance(result, dict):
                for name, reason in (result.get("errors") or {}).items():
                    lines.append(f"  {check} 检查不可用（{name}）：{reason}")
                if result.get("status") == "error" and not result.get("errors"):
                    lines.append(f"  {check} 检查不可用：{result.get('reason') or 'unknown error'}")
        for s in report.get("sources", ()):
            if s.get("missing_store") or s.get("error"):
                lines.append(f"  {s['store']}:{s['source']}  读不到")
                continue
            e = entry_for(registry, s["store"], s["source"])
            if e is not None and e["status"] not in ACTIVE:
                continue  # disabled sources are listed once below
            lines.append(f"  {s['store']}:{s['source']:<22} {s['OK']:>5} / {s['EMPTY']:>5} / {s['ERROR']:>4} / "
                         f"{s['DISABLED']:>3}   {s.get('last_ok_at') or '-'}   {e['status'] if e else '未登记'}")
        off = [e["id"] for e in registry["sources"] if e["status"] not in ACTIVE]
        lines += ["", f"停用或计划中（不检查）：{', '.join(off) or '无'}"]
        dead = _health_items(report.get("dead_links"))
        if dead:
            lines += ["", "永久失效的链接（站点连续多天无响应，已停止重试，不计入日更状态）："]
            lines += [f"  {host}: {v['links']} 条（{v['since']} 起）" for host, v in sorted(dead.items())
                      ]
        pv = _health_items(report.get("page_versions"))
        if pv:
            lines += ["", "页面修改时间记录（RG-17，过去 24 小时）："]
            lines += [f"  {k}: 页面 {v['pages']}，修改晚于截止点 {v['modified_after_cutoff']}，未声明修改时间 "
                      f"{v['no_declared_modification']}" for k, v in sorted(pv.items())
                      if isinstance(v, dict) and v.get("status") != "error"]
        fetching = report.get("fetching") or {}
        if fetching.get("schema_version") == 2:
            lines += ["", "抓取稳定性（三层独立分母；旧七天基线不可比）："]
            for source, item in sorted(fetching.get("sources", {}).items()):
                parts = []
                for label, name, good, total in (("请求", "requests", "successes", "attempts"),
                        ("任务", "tasks", "succeeded", "registered"), ("正文目标", "targets", "acquired", "registered")):
                    value = item.get(name, {})
                    rate = value.get("rate")
                    text = "无目标或未覆盖" if rate is None else f"{rate:.1%}"
                    parts.append(f"{label} {value.get(good, 0)}/{value.get(total, 0)}（{text}）"
                        f" 等待 {value.get('pending', 0)} 重试中 {value.get('retrying', 0)}")
                lines.append(f"  {source}: " + "；".join(parts)
                    + f"；正文发现不完整任务 {item.get('targets', {}).get('discovery_incomplete_tasks', 0)}")
        else:
            fx = {k: v for k, v in fetching.items() if isinstance(v, dict) and v.get("requests")}
            if fx:
                lines += ["", "旧口径调用统计（与三层统计不可比）："]
                lines += [f"  {k}: {v['requests']} 次调用；重试后成功 {v['ok_after_retries']:.1%}"
                          for k, v in sorted(fx.items())]
        tc = _health_items(report.get("time_checks"))
        cc = _health_items(report.get("cross_checks"))
        if tc or cc:
            lines += ["", "时间一致性（两个时间都有的条目，过去 24 小时；独立来源对照，过去 7 天）："]
            lines += [f"  {k}: 比较 {v.get('compared', 0)}，整小时 {v.get('whole_hour', 0)}，其他不一致 "
                      f"{v.get('disagree', 0)}，已知偏差已更正 {v.get('known_offset', 0)}" for k, v in sorted(tc.items())
                      if isinstance(v, dict) and v.get("status") != "error"]
            lines += [f"  {k}: 配对 {v.get('compared', 0)}，整小时 {v.get('whole_hour', 0)}"
                      for k, v in sorted(cc.items()) if isinstance(v, dict) and v.get("status") != "error"]
        d = report.get("disk") or {}
        lines += ["", f"数据服务器磁盘：剩余 {d.get('free_gb')} GB（{d.get('free_pct')}%）"]
    backups = sorted(p.name for p in BACKUP.glob("20??-??-??")) if BACKUP.exists() else []
    lines += [f"监控主机上的备份：{', '.join(backups) or '无'}"]
    tier3 = [p["text"] for _, p in sorted(low.items()) if p["tier"] == 3]
    if tier3:
        lines += ["", "三级（只记录）："] + [f"  {t}" for t in tier3]
    n = len(urgent) + len(tier2)
    send(f"[Catalyst] 日报 {at:%Y-%m-%d}" + (f"（{n} 个问题）" if n else ""), "\n".join(lines))
    state["low"] = {k: value for k, value in low.items() if k in held}
    state["degradation_reminder_days"] = reminder_days
    STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    print("\n".join(lines))
    return 0


def pull_backup() -> int:
    state = load_state()
    try:
        day, n = _pull()
        state["pull"] = {"at": now().isoformat(), "ok": True, "day": day, "files": n}
    except Exception as exc:  # noqa: BLE001  (recorded, so check() alerts on it until a pull succeeds)
        state["pull"] = {"at": now().isoformat(), "ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        STATE.write_text(json.dumps(state, indent=1))
        raise
    STATE.write_text(json.dumps(state, indent=1))
    return 0


def _pull() -> tuple[str, int]:
    listing = [line.split() for line in gate("backup-list").splitlines() if line.strip()]
    _pull_minute(listing)
    days = sorted({path.split("/")[0] for path, _ in listing if re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.split("/")[0])})
    if not days:
        raise RuntimeError("no backup on the data server")
    day = days[-1]
    target = BACKUP / day
    manifest = gate(f"backup-get {day}/MANIFEST")
    expected = {name: digest for digest, name in (line.split() for line in manifest.splitlines() if line.strip())}
    # a backup re-made the same day replaces that day's directory there, so compare the manifest, not the name
    if (target / "MANIFEST").is_file() and (target / "MANIFEST").read_text() == manifest:
        print(f"{day} already here")
    else:
        tmp = BACKUP / f".{day}.partial"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        expected = {name: digest for digest, name in (line.split() for line in manifest.splitlines() if line.strip())}
        for name, digest in expected.items():
            data = gate(f"backup-get {day}/{name}", binary=True, timeout=3600)
            if hashlib.sha256(data).hexdigest() != digest:
                raise RuntimeError(f"sha256 mismatch for {day}/{name}")
            (tmp / name).write_bytes(data)
        (tmp / "MANIFEST").write_text(manifest)
        if target.exists():
            shutil.rmtree(target)
        tmp.rename(target)
        print(f"{day}: {len(expected)} files verified")
    for old in sorted(p for p in BACKUP.glob("20??-??-??"))[:-KEEP_DAYS]:
        shutil.rmtree(old)
    return day, len(expected)


def _pull_minute(listing) -> None:
    """Closed months of minute bars are backed up once on the data server; copy the ones not here yet."""
    names = [path for path, _ in listing if path.startswith("minute/") and path.endswith(".zst")]
    if not names:
        return
    manifest = gate("backup-get minute/MANIFEST")
    expected = {name: digest for digest, name in (line.split() for line in manifest.splitlines() if line.strip())}
    target = BACKUP / "minute"
    target.mkdir(parents=True, exist_ok=True)
    for path in names:
        name = path.split("/", 1)[1]
        if (target / name).is_file() or name not in expected:
            continue
        data = gate(f"backup-get minute/{name}", binary=True, timeout=3600)
        if hashlib.sha256(data).hexdigest() != expected[name]:
            raise RuntimeError(f"sha256 mismatch for minute/{name}")
        (target / f".{name}.partial").write_bytes(data)
        (target / f".{name}.partial").rename(target / name)
        print(f"minute/{name} verified")


def restore_drill() -> int:
    """Restore the newest local backup into a scratch directory, run PRAGMA integrity_check on every store and
    compare each table's row count with the ROWCOUNTS written when the backup was taken. Mails the result."""
    import sqlite3
    import tempfile
    days = sorted(p for p in BACKUP.glob("20??-??-??") if (p / "MANIFEST").is_file())
    if not days:
        raise RuntimeError("no local backup to restore")
    src = days[-1]
    expected: dict[tuple[str, str], int] = {}
    if (src / "ROWCOUNTS").is_file():
        for line in (src / "ROWCOUNTS").read_text().splitlines():
            db, table, n = line.rsplit(" ", 2)
            expected[(db, table)] = int(n)
    lines, bad = [f"恢复演练 {now():%Y-%m-%d %H:%M} UTC，备份 {src.name}", ""], 0
    with tempfile.TemporaryDirectory(prefix="catalyst-restore-", dir=str(HOME)) as tmp:
        for z in sorted(src.glob("*.sqlite3.zst")):
            db = z.name[: -len(".zst")]
            out = Path(tmp) / db
            subprocess.run(["zstd", "-q", "-d", str(z), "-o", str(out)], check=True)
            conn = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
            try:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                tables = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
                mismatch = []
                for t in tables:
                    n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                    if (db, t) in expected and expected[(db, t)] != n:
                        mismatch.append(f"{t} {n}≠{expected[(db, t)]}")
                checked = sum(1 for t in tables if (db, t) in expected)
            finally:
                conn.close()
            ok = integrity == "ok" and not mismatch
            bad += not ok
            lines.append(f"{'正常' if ok else '异常'}  {db}: integrity {integrity}；{len(tables)} 张表，"
                         f"{checked} 张与备份时的行数核对" + (f"，不一致：{', '.join(mismatch)}" if mismatch else "一致")
                         + f"；{out.stat().st_size / 1e6:.0f} MB")
            out.unlink()
    if not expected:
        lines.append("（这份备份没有 ROWCOUNTS，只做了完整性检查）")
    body = "\n".join(lines)
    (HOME / "restore_drill.log").open("a").write(body + "\n\n")
    send(f"[Catalyst] 恢复演练{'：' + str(bad) + ' 个库异常' if bad else '：全部正常'}", body)
    print(body)
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "check"
    if cmd == "test-email":
        send("[Catalyst] 测试邮件", f"监控主机可以发邮件了。{now():%Y-%m-%d %H:%M} UTC")
        print("sent")
        return 0
    try:
        return {"check": check, "daily": daily, "pull-backup": pull_backup, "restore-drill": restore_drill}[cmd]()
    except Exception as exc:  # noqa: BLE001  (a broken monitor must still say so)
        if cmd != "check":
            try:
                send(f"[Catalyst] 监控任务 {cmd} 失败", f"{type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001
                pass
        raise


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
