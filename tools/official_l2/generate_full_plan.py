"""Build the exact offline scope and deduct verified use from the task budget."""

import argparse
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re

ACCOUNT = "555066174629"
START = datetime(2025, 3, 22, tzinfo=timezone.utc)
END = datetime(2025, 10, 22, tzinfo=timezone.utc)
EXPECTED_CSV1_SHA256 = "89bbee26d9ebad3f43d759d8406785bd049062d460365f6aab814e158f4d0022"
ORIGINAL_TASK_EXPIRY = "2026-11-06T04:55:00Z"
TOTAL_SOURCE_BYTES = 8 * 1024**3
TOTAL_SOURCE_REQUESTS = 312063
TOTAL_INSTANCE_SECONDS = 360 * 60
TOTAL_OUTPUT_BYTES = 256 * 1024**2
BOOTSTRAP_RESERVE_BYTES = 4 * 1024**2
EXPORT_NAMES = {"output/bootstrap.log"} | {
    "output/full/" + name for name in (
        "csv1.csv", "csv2.csv", "csv3.csv", "run.log", "progress.sqlite", "cache.sqlite")}


def _utc(value, name):
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value):
        raise ValueError(name + " must be an explicit UTC timestamp ending in Z")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _count(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(name + " must be a nonnegative integer")
    return value


def read_prior_usage(path, csv1_digest):
    """Read a separately verified termination/export summary; never access AWS."""
    raw = path.read_bytes()
    if len(raw) > 1024**2:
        raise ValueError("Prior-attempt summary exceeds 1 MiB")
    summary = json.loads(raw)
    if not isinstance(summary, dict) or not (
            summary.get("verification_status") == "verified_full_incomplete_attempts"
            and summary.get("account_id") == ACCOUNT
            and summary.get("region") == "us-east-1"
            and summary.get("csv1_sha256") == csv1_digest
            and summary.get("original_expires_at_utc") == ORIGINAL_TASK_EXPIRY):
        raise ValueError("Prior-attempt summary differs from the verified original task")
    attempts = summary.get("attempts")
    if not isinstance(attempts, list) or not attempts or len(attempts) > 32:
        raise ValueError("Prior summary must contain 1 to 32 verified attempts")
    usage = dict(source_bytes_charged=0, source_requests=0, instance_seconds=0, output_bytes=0)
    seen, intervals = set(), []
    now = datetime.now(timezone.utc)
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise ValueError("Prior attempt must be an object")
        instance = attempt.get("instance_id")
        if (not isinstance(instance, str) or not re.fullmatch(r"i-[0-9a-f]{8,17}", instance)
                or instance in seen or attempt.get("state") != "terminated"):
            raise ValueError("Each prior instance must be unique and verified terminated")
        seen.add(instance)
        launch = _utc(attempt.get("launch_time_utc"), "launch_time_utc")
        terminated = _utc(attempt.get("observed_terminated_utc"), "observed_terminated_utc")
        if not launch < terminated <= now:
            raise ValueError("Prior termination times must be after launch and already observed")
        intervals.append((launch, terminated))
        usage["instance_seconds"] += math.ceil((terminated - launch).total_seconds())
        stats = attempt.get("full_statistics")
        if not isinstance(stats, dict) or not (
                stats.get("event") == "full_incomplete" and stats.get("account") == ACCOUNT
                and stats.get("region") == "us-east-1" and stats.get("instance") == instance
                and stats.get("expected_episodes") == 354):
            raise ValueError("Prior statistics do not identify the scoped incomplete full run")
        received = _count(stats.get("source_bytes_received"), "source_bytes_received")
        reserved = _count(stats.get("source_bytes_reserved"), "source_bytes_reserved")
        usage["source_bytes_charged"] += max(received, reserved)
        usage["source_requests"] += _count(stats.get("source_requests"), "source_requests")
        files = attempt.get("export_files")
        if not isinstance(files, dict) or set(files) != EXPORT_NAMES:
            raise ValueError("Prior export sizes must identify all seven exact result objects")
        usage["output_bytes"] += sum(_count(size, "export size") for size in files.values())
    intervals.sort()
    if any(previous[1] > following[0] for previous, following in zip(intervals, intervals[1:])):
        raise ValueError("Prior instance intervals cannot overlap")
    for key, cap in (("source_bytes_charged", TOTAL_SOURCE_BYTES),
                     ("source_requests", TOTAL_SOURCE_REQUESTS),
                     ("instance_seconds", TOTAL_INSTANCE_SECONDS),
                     ("output_bytes", TOTAL_OUTPUT_BYTES)):
        if usage[key] > cap:
            raise ValueError("Verified prior use already exceeds the task allowance: " + key)
    return usage, {
        "prior_attempt_summary_sha256": hashlib.sha256(raw).hexdigest(),
        "prior_attempt_summary_name": path.name,
        "prior_attempt_count": len(attempts),
        "original_expires_at_utc": ORIGINAL_TASK_EXPIRY,
        "continuation_authorization": "Existing task-scoped full-run approval; manually enabled only after verified remaining budgets and predecessor cleanup. No automatic replacement or retry.",
    }


def make_plan(csv1, approved=False, expiry=None, prior_attempt_summary=None):
    digest = hashlib.sha256(csv1.read_bytes()).hexdigest()
    if digest != EXPECTED_CSV1_SHA256:
        raise ValueError("CSV1 differs from the requested authoritative input")
    with csv1.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if len(reader.fieldnames) != 45:
            raise ValueError("Expected 45 original Chinese CSV1 columns")
        episodes = {}
        for row in reader:
            if row["记录类型"] != "fill":
                continue
            t = datetime.fromisoformat(row["接管组结束时间UTC"].replace("Z", "+00:00"))
            if not START <= t < END:
                continue
            episode = (row["币种"], row["接管方向"], row["接管组结束时间UTC"], row["接管组输入哈希"])
            eid = row["接管组编号"]
            if eid in episodes and episodes[eid] != episode:
                raise ValueError("Inconsistent existing episode summary")
            episodes[eid] = episode
    if len(episodes) != 354:
        raise ValueError("Expected 354 existing selected episodes")
    earliest = {}
    for coin, direction, timestamp, episode_hash in episodes.values():
        if direction not in {"long", "short"}:
            raise ValueError("Unexpected direction")
        if not coin or "/" in coin or "\\" in coin:
            raise ValueError("Unsafe coin name")
        hour = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).replace(minute=0, second=0, microsecond=0)
        earliest[coin] = min(earliest.get(coin, hour), hour)
    keys = []
    for coin, first in sorted(earliest.items()):
        hour = first
        while hour < END:
            keys.append(f"market_data/{hour:%Y%m%d}/{hour.hour}/l2Book/{coin}.lz4")
            hour += timedelta(hours=1)
    if len(keys) != 156031 or len(keys) != len(set(keys)) or len(earliest) != 172:
        raise ValueError("Unexpected full scope")
    prior_usage = dict(source_bytes_charged=0, source_requests=0, instance_seconds=0, output_bytes=0)
    provenance = {}
    if prior_attempt_summary is not None:
        prior_usage, provenance = read_prior_usage(Path(prior_attempt_summary), digest)
    remaining_minutes = (TOTAL_INSTANCE_SECONDS - prior_usage["instance_seconds"]) // 60
    timer_minutes = min(330, remaining_minutes - 15)
    data_seconds = min(18900, (timer_minutes - 15) * 60)
    source_bytes = TOTAL_SOURCE_BYTES - prior_usage["source_bytes_charged"]
    source_requests = TOTAL_SOURCE_REQUESTS - prior_usage["source_requests"]
    output_bytes = min(252 * 1024**2, TOTAL_OUTPUT_BYTES - prior_usage["output_bytes"] - BOOTSTRAP_RESERVE_BYTES)
    if timer_minutes < 15 or min(data_seconds, source_bytes, source_requests, output_bytes) <= 0:
        raise ValueError("Remaining task allowance cannot safely support another manual attempt")
    approved_at = None
    if approved:
        if not expiry:
            raise ValueError("Approved plan requires an explicit source-access expiry")
        expires = _utc(expiry, "expires_at_utc")
        if provenance and expires > _utc(ORIGINAL_TASK_EXPIRY, "original expiry"):
            raise ValueError("Continuation cannot extend the original source-access expiry")
        approved_at = datetime.now(timezone.utc).replace(microsecond=0)
        remaining = (expires - approved_at).total_seconds()
        if not 900 <= remaining <= 21600:
            raise ValueError("Approval must expire 15 minutes to 6 hours from now")
        expiry = expires.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        expiry = "SET_AFTER_USER_APPROVAL"
    plan = {
        "account_id": ACCOUNT,
        "region": "us-east-1",
        "source_bucket": "hyperliquid-archive",
        "csv1_sha256": digest,
        "csv2_start_inclusive": "2025-03-22T00:00:00Z",
        "csv2_end_exclusive": "2025-10-22T00:00:00Z",
        "expected_episode_count": len(episodes),
        "episode_ids": sorted(episodes),
        "allowed_keys": sorted(keys),
        "selection_policy": "validated_next_hour",
        "approval_recorded": approved,
        "approved_at_utc": approved_at.strftime("%Y-%m-%dT%H:%M:%SZ") if approved_at else "SET_AFTER_USER_APPROVAL",
        "expires_at_utc": expiry,
        "limits": {
            "source_bytes": source_bytes,
            "object_bytes": 64 * 1024**2,
            "source_requests": source_requests,
            "output_bytes": output_bytes,
            "cache_bytes": 128 * 1024**2,
            "data_runtime_seconds": data_seconds,
            "max_instance_minutes": remaining_minutes,
            "instance_shutdown_minutes": timer_minutes,
            "bundle_expanded_bytes": 32 * 1024**2,
            "max_cost_reserve_usd": 3.0,
        },
        "method_assumption": "User-approved sampled exchange-time ordering with complete successor-hour inspection; extend on anomalies.",
        "scope_semantics": "Allowed keys are an upper search scope. Read only unresolved coin/hour objects and stop once selection is confirmed. Budget exhaustion leaves incomplete work pending.",
        "credentials": "Explicit IMDSv2 EC2 role. Verify account and same Region before every source request; IAM requires the named S3GatewayEndpoint.",
        "instance_rule": "One full-run worker at a time. No automatic replacement or retry. A manual continuation requires verified predecessor termination/cleanup and task-scoped approval within the original cumulative allowance.",
    }
    if provenance:
        plan.update(provenance)
        plan["prior_usage"] = prior_usage
        plan["remaining_instance_minutes"] = remaining_minutes
    return plan


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv1", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--expires-at-utc")
    parser.add_argument("--prior-attempt-summary", type=Path,
                        help="Separately verified terminated full_incomplete attempts; deduct their cumulative use")
    args = parser.parse_args()
    plan = make_plan(args.csv1, args.approve, args.expires_at_utc, args.prior_attempt_summary)
    with args.output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(plan, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"path": str(args.output), "episodes": plan["expected_episode_count"],
                      "keys": len(plan["allowed_keys"]), "approved": plan["approval_recorded"],
                      "plan_bytes": args.output.stat().st_size}))


if __name__ == "__main__":
    main()
