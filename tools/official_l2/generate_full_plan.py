"""Build the exact offline full-run scope. Approval must be recorded explicitly."""

import argparse
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

ACCOUNT = "555066174629"
START = datetime(2025, 3, 22, tzinfo=timezone.utc)
END = datetime(2025, 10, 22, tzinfo=timezone.utc)
EXPECTED_CSV1_SHA256 = "89bbee26d9ebad3f43d759d8406785bd049062d460365f6aab814e158f4d0022"


def make_plan(csv1, approved=False, expiry=None):
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
    approved_at = None
    if approved:
        if not expiry:
            raise ValueError("Approved plan requires an explicit source-access expiry")
        expires = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        if expires.utcoffset() != timedelta(0):
            raise ValueError("Expiry must use UTC")
        approved_at = datetime.now(timezone.utc).replace(microsecond=0)
        remaining = (expires - approved_at).total_seconds()
        if not 900 <= remaining <= 21600:
            raise ValueError("Approval must expire 15 minutes to 6 hours from now")
        expiry = expires.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        expiry = "SET_AFTER_USER_APPROVAL"
    return {
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
            "source_bytes": 8 * 1024**3,
            "object_bytes": 64 * 1024**2,
            "source_requests": 312063,
            "output_bytes": 252 * 1024**2,
            "cache_bytes": 128 * 1024**2,
            "data_runtime_seconds": 18900,
            "max_instance_minutes": 360,
            "instance_shutdown_minutes": 330,
            "bundle_expanded_bytes": 32 * 1024**2,
            "max_cost_reserve_usd": 3.0,
        },
        "method_assumption": "User-approved sampled exchange-time ordering with complete successor-hour inspection; extend on anomalies.",
        "scope_semantics": "Allowed keys are an upper search scope. Read only unresolved coin/hour objects and stop once selection is confirmed. Budget exhaustion leaves incomplete work pending.",
        "credentials": "Explicit IMDSv2 EC2 role. Verify account and same Region before every source request; IAM requires the named S3GatewayEndpoint.",
        "instance_rule": "One approved full-run worker. No automatic replacement or second paid attempt. The six-hour total instance allowance includes any explicitly approved replacement.",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv1", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--expires-at-utc")
    args = parser.parse_args()
    plan = make_plan(args.csv1, args.approve, args.expires_at_utc)
    with args.output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(plan, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"path": str(args.output), "episodes": plan["expected_episode_count"],
                      "keys": len(plan["allowed_keys"]), "approved": plan["approval_recorded"],
                      "plan_bytes": args.output.stat().st_size}))


if __name__ == "__main__":
    main()
