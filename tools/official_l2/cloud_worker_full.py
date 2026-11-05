"""Execute the separately approved full CSV2 run using frozen sample transport."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time

from cloud_worker import (ACCOUNT, REGION, SOURCE_BUCKET, ApprovedS3Source, Config,
                          metadata_session)
from official_l2 import HOUR_NS, load_csv1, parse_time_ns, run_csv2

START = "2025-03-22T00:00:00Z"
END = "2025-10-22T00:00:00Z"
EPISODE_COUNT = 354
ALLOWED_KEY_COUNT = 156031
RUN_DIR = Path("/opt/hl/run")
DURABLE = ("csv1.csv", "csv2.csv", "csv3.csv", "run.log", "progress.sqlite", "cache.sqlite")
BUNDLE_FILES = ("cloud_worker_full.py", "cloud_worker.py", "official_l2.py",
                "aws_account_guard.py", "csv1.csv", "full_plan.json")
LIMIT_CAPS = {
    "source_bytes": 8 * 1024**3,
    "object_bytes": 64 * 1024**2,
    "source_requests": 312063,
    "data_runtime_seconds": 315 * 60,
    "max_instance_minutes": 360,
    "output_bytes": 252 * 1024**2,
    "cache_bytes": 128 * 1024**2,
    "bundle_expanded_bytes": 32 * 1024**2,
}


def validate_inputs(base, plan, endpoint_id):
    """Finish local scope validation before accessing IMDS or any AWS service."""
    if not (plan.get("account_id") == ACCOUNT and plan.get("region") == REGION
            and plan.get("source_bucket") == SOURCE_BUCKET
            and plan.get("approval_recorded") is True
            and plan.get("selection_policy") == "validated_next_hour"
            and plan.get("csv2_start_inclusive") == START
            and plan.get("csv2_end_exclusive") == END
            and plan.get("expected_episode_count") == EPISODE_COUNT):
        raise PermissionError("Full input, selection policy, or approval differs from the reviewed scope")
    if not isinstance(endpoint_id, str) or not re.fullmatch(r"vpce-[0-9a-f]+", endpoint_id):
        raise PermissionError("An explicit S3 Gateway Endpoint identifier is required")
    expiry = datetime.fromisoformat(plan["expires_at_utc"])
    approved = datetime.fromisoformat(plan["approved_at_utc"])
    current = datetime.now(timezone.utc)
    if (expiry.tzinfo is None or approved.tzinfo is None or current >= expiry
            or approved > current or not approved < expiry <= approved + timedelta(minutes=360)):
        raise PermissionError("Full-run authorization expired")
    limits = plan["limits"]
    for key, cap in LIMIT_CAPS.items():
        value = limits.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= cap:
            raise PermissionError("Full-run limit differs from its approved bound: " + key)
    if limits["data_runtime_seconds"] > limits["max_instance_minutes"] * 60 - 15 * 60:
        raise PermissionError("Data runtime leaves insufficient time for result export and shutdown")
    if sum((base / name).stat().st_size for name in BUNDLE_FILES) > limits["bundle_expanded_bytes"]:
        raise PermissionError("Expanded full-run bundle exceeds the approved bound")
    csv1 = base / "csv1.csv"
    if hashlib.sha256(csv1.read_bytes()).hexdigest() != plan["csv1_sha256"]:
        raise PermissionError("CSV1 differs from the approved source")
    imported = load_csv1(csv1, parse_time_ns(START), parse_time_ns(END))
    expected_ids = {episode["episode_id"] for episode in imported.episodes}
    if (len(imported.episodes) != EPISODE_COUNT or len(expected_ids) != EPISODE_COUNT
            or set(plan.get("episode_ids", [])) != expected_ids
            or len(plan.get("episode_ids", [])) != EPISODE_COUNT
            or any(episode.get("_input_problem") for episode in imported.episodes)):
        raise PermissionError("Full-run episode coverage differs from the reviewed 354 episodes")
    keys = plan.get("allowed_keys")
    if not isinstance(keys, list) or len(keys) != ALLOWED_KEY_COUNT:
        raise PermissionError("Full-run object whitelist has an unexpected size")
    allowed = set(keys)
    if len(allowed) != ALLOWED_KEY_COUNT:
        raise PermissionError("Full-run object whitelist contains duplicate keys")
    first_by_coin = {}
    for episode in imported.episodes:
        coin, hour = episode["coin"], episode["tn_ns"] // HOUR_NS * HOUR_NS
        first_by_coin[coin] = min(first_by_coin.get(coin, hour), hour)
    expected = 0
    end = parse_time_ns(END)
    for coin, first in first_by_coin.items():
        for hour in range(first, end, HOUR_NS):
            date = datetime.fromtimestamp(hour // 1_000_000_000, timezone.utc)
            key = f"market_data/{date:%Y%m%d}/{date.hour}/l2Book/{coin}.lz4"
            if key not in allowed:
                raise PermissionError("Full-run whitelist omits an in-scope coin-hour")
            expected += 1
    if expected != len(allowed):
        raise PermissionError("Full-run whitelist contains an out-of-scope object")
    return imported


def append_statistics(directory, statistics):
    with (directory / "run.log").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(statistics) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def export_results(session, role, bucket, directory, plan):
    """Upload only the six durable result files to the owned, same-region bucket."""
    total = sum((directory / name).stat().st_size for name in DURABLE if (directory / name).exists())
    if total > plan["limits"]["output_bytes"]:
        raise RuntimeError("Full result export exceeds the authorized byte bound")
    config = Config(connect_timeout=8, read_timeout=30, retries={"max_attempts": 0},
                    s3={"addressing_style": "virtual", "us_east_1_regional_endpoint": "regional"})
    sts = session.client("sts", region_name=REGION, config=config)
    s3 = session.client("s3", region_name=REGION, config=config)
    try:
        for name in DURABLE:
            path = directory / name
            if not path.exists():
                continue
            identity = sts.get_caller_identity()
            if identity["Account"] != ACCOUNT or f":assumed-role/{role}/" not in identity["Arn"]:
                raise PermissionError("STS account or role mismatch before full result export")
            with path.open("rb") as handle:
                s3.put_object(Bucket=bucket, Key="output/full/" + name, Body=handle,
                              ExpectedBucketOwner=ACCOUNT, ServerSideEncryption="AES256")
    finally:
        s3.close()
        sts.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["full"])
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", choices=[REGION], default=REGION)
    args = parser.parse_args()
    base = Path(__file__).resolve().parent
    plan_path = base / "full_plan.json"
    if plan_path.stat().st_size > LIMIT_CAPS["bundle_expanded_bytes"]:
        raise PermissionError("Full plan exceeds the approved bundle bound")
    plan = json.loads(plan_path.read_bytes())
    endpoint_id = os.environ.get("HLP_EXPECTED_VPCE", "")
    imported = validate_inputs(base, plan, endpoint_id)
    output = RUN_DIR
    output.mkdir(exist_ok=True)
    session, instance, role = metadata_session()
    source = ApprovedS3Source(session, role, plan, output)
    started = time.monotonic()
    error = None
    result = None
    try:
        source.verify_bucket_region()
        result = run_csv2(base / "csv1.csv", output, source, start=START, end=END, resume=False,
                          episode_ids=plan["episode_ids"], policy=plan["selection_policy"],
                          max_cache_bytes=plan["limits"]["cache_bytes"])
        if result["episodes"] != len(imported.episodes):
            raise RuntimeError("Full CSV2 episode coverage differs from the validated input")
    except Exception as exc:
        error = type(exc).__name__ + ": " + str(exc)
    finally:
        import resource
        statistics = {
            "event": "full_completed" if error is None else "full_incomplete",
            "instance": instance, "account": ACCOUNT, "region": REGION,
            "expected_gateway_endpoint": endpoint_id,
            "gateway_enforcement": "IAM aws:SourceVpce condition on all source reads",
            "worker_elapsed_seconds": time.monotonic() - started, "source_requests": source.requests,
            "data_runtime_limit_seconds": plan["limits"]["data_runtime_seconds"],
            "instance_lifetime_budget_minutes": plan["limits"]["max_instance_minutes"],
            "source_bytes_received": source.received, "source_bytes_reserved": source.reserved,
            "selection_policy": plan["selection_policy"], "error": error,
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "cpu_seconds": time.process_time(), "expected_episodes": EPISODE_COUNT,
            "result": result,
        }
        append_statistics(output, statistics)
        source.close()
        export_results(session, role, args.bucket, output, plan)
        print(json.dumps(statistics), flush=True)
    return 0 if error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
