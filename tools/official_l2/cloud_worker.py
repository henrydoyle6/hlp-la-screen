"""Run the approved L2 sample on an explicitly selected EC2 instance role."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

import aws_account_guard as account_guard
from official_l2 import ObjectNotFound, SelectionError, SourceAccessError, run_csv2

ACCOUNT = "555066174629"
REGION = "us-east-1"
SOURCE_BUCKET = "hyperliquid-archive"
IMDS = "http://169.254.169.254/latest/"


def metadata_session():
    """Explicit IMDSv2 role selection. Never use an SDK credential fallback."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    token_request = urllib.request.Request(
        IMDS + "api/token", data=b"", method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "7200"})
    with opener.open(token_request, timeout=3) as response:
        token = response.read(1024).decode()

    def get(path):
        request = urllib.request.Request(IMDS + path, headers={"X-aws-ec2-metadata-token": token})
        with opener.open(request, timeout=3) as response:
            return response.read(65536).decode()

    document = json.loads(get("dynamic/instance-identity/document"))
    if document["accountId"] != ACCOUNT or document["region"] != REGION:
        raise PermissionError("EC2 account/region does not match approved deployment")
    role = get("meta-data/iam/security-credentials/").strip()
    if not role or "/" in role or "\n" in role:
        raise PermissionError("Expected exactly one explicitly selected EC2 role")
    credentials = json.loads(get("meta-data/iam/security-credentials/" + role))
    if credentials["Code"] != "Success":
        raise PermissionError("EC2 role credentials unavailable")
    session = boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["Token"], region_name=REGION)
    return session, document["instanceId"], role


class RemoteObject:
    def __init__(self, response, source, key):
        self.response, self.source, self.key = response, source, key
        self.identity = {
            "bucket": SOURCE_BUCKET, "key": key, "region": REGION,
            "etag": response["ETag"], "size": response["ContentLength"],
            "version_id": response.get("VersionId"),
        }
        self.actual = 0

    def iter_chunks(self):
        for chunk in self.response["Body"].iter_chunks(256 * 1024):
            self.actual += len(chunk)
            self.source.received += len(chunk)
            if self.actual > self.response["ContentLength"]:
                raise SourceAccessError("S3 response exceeded its declared length")
            if self.source.received > self.source.plan["limits"]["source_bytes"]:
                raise SourceAccessError("Approved source byte limit reached")
            self.source.deadline_check()
            yield chunk
        if self.actual != self.response["ContentLength"]:
            raise SourceAccessError("Truncated S3 response")


class ApprovedS3Source:
    """Account, region, gateway, object, runtime and request scope are independent gates."""
    def __init__(self, session, role, plan, output):
        self.plan, self.role = plan, role
        self.allowed = set(plan["allowed_keys"])
        self.fingerprint = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
        self.start = time.monotonic()
        self.requests = self.reserved = self.received = 0
        self.output = output
        self.database = sqlite3.connect(output / "cache.sqlite")
        self.database.execute("CREATE TABLE IF NOT EXISTS aws_usage "
                              "(sequence INTEGER PRIMARY KEY, kind TEXT, object_key TEXT, bytes_reserved INTEGER, at TEXT)")
        self.database.commit()
        if self.database.execute("SELECT count(*) FROM aws_usage").fetchone()[0]:
            raise PermissionError("This sample authorization permits one session")
        config = Config(connect_timeout=8, read_timeout=30, retries={"max_attempts": 0},
                        s3={"addressing_style": "virtual"})
        self.sts = session.client("sts", region_name=REGION, config=config)
        self.s3 = session.client("s3", region_name=REGION, config=config)
        self.s3.meta.events.register("before-send.s3.*", self.wire_request)
        self.fresh_identity = False
        self.current_key = None
        self.current_operation = None

    def deadline_check(self):
        if time.monotonic() - self.start > self.plan["limits"]["data_runtime_seconds"]:
            raise SourceAccessError("Approved sample runtime reached")
        if datetime.now(timezone.utc) >= datetime.fromisoformat(self.plan["expires_at_utc"]):
            raise SourceAccessError("Sample authorization expired")

    def verify_identity(self):
        self.deadline_check()
        identity = self.sts.get_caller_identity()
        if identity["Account"] != ACCOUNT or ACCOUNT != account_guard.EXPECTED_ACCOUNT_ID:
            raise SourceAccessError("STS account mismatch")
        if f":assumed-role/{self.role}/" not in identity["Arn"]:
            raise SourceAccessError("STS role differs from explicit IMDS role")
        self.fresh_identity = True

    def wire_request(self, request, **kwargs):
        self.deadline_check()
        if not self.fresh_identity:
            raise SourceAccessError("Fresh STS verification required before every AWS data request")
        self.fresh_identity = False
        parsed = urllib.parse.urlsplit(request.url)
        if parsed.scheme != "https" or parsed.hostname != f"{SOURCE_BUCKET}.s3.{REGION}.amazonaws.com":
            raise SourceAccessError("Unexpected S3 endpoint or cross-region redirection")
        self.requests += 1
        if self.requests > self.plan["limits"]["source_requests"]:
            raise SourceAccessError("Approved request limit reached")
        self.database.execute("INSERT INTO aws_usage VALUES (?,?,?,?,?)",
                              (self.requests, self.current_operation, self.current_key, 0,
                               datetime.now(timezone.utc).isoformat()))
        self.database.commit()

    def verify_bucket_region(self):
        self.current_operation = "GetBucketLocation"
        self.current_key = ""
        self.verify_identity()
        response = self.s3.get_bucket_location(Bucket=SOURCE_BUCKET)
        if response.get("LocationConstraint") not in (None, "", REGION):
            raise SourceAccessError("Official bucket is outside us-east-1")

    @contextmanager
    def open(self, key):
        if key not in self.allowed:
            raise SelectionError("SAMPLE_SEARCH_SCOPE_EXHAUSTED",
                                 "No match confirmed within approved sample object scope", key)
        self.current_key, self.current_operation = key, "GetObject"
        self.current_operation = "ListObjectsV2"
        self.verify_identity()
        try:
            listing = self.s3.list_objects_v2(Bucket=SOURCE_BUCKET, Prefix=key, MaxKeys=1,
                                               RequestPayer="requester")
            objects = listing.get("Contents", [])
            match = next((item for item in objects if item["Key"] == key), None)
            if match is None:
                raise ObjectNotFound(key)
            if match["Size"] > self.plan["limits"]["object_bytes"]:
                raise SourceAccessError("Object exceeds approved per-object limit")
            if self.reserved + match["Size"] > self.plan["limits"]["source_bytes"]:
                raise SourceAccessError("Object exceeds remaining approved byte limit")
            self.current_operation = "GetObject"
            self.verify_identity()
            response = self.s3.get_object(Bucket=SOURCE_BUCKET, Key=key, RequestPayer="requester",
                                          IfMatch=match["ETag"])
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            raise SourceAccessError(f"S3 {code} for {key}") from None
        obj = RemoteObject(response, self, key)
        try:
            length = response["ContentLength"]
            if response["ETag"] != match["ETag"] or length != match["Size"]:
                raise SourceAccessError("Object identity changed between listing and GET")
            if length > self.plan["limits"]["object_bytes"]:
                raise SourceAccessError("Object exceeds approved per-object limit")
            if self.reserved + length > self.plan["limits"]["source_bytes"]:
                raise SourceAccessError("Object exceeds remaining approved byte limit")
            self.reserved += length
            self.database.execute("UPDATE aws_usage SET bytes_reserved=? WHERE sequence=?",
                                  (length, self.requests))
            self.database.commit()
            yield obj
        finally:
            response["Body"].close()

    def close(self):
        self.database.close()
        self.s3.close()
        self.sts.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["sample"])
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", choices=[REGION], default=REGION)
    args = parser.parse_args()
    base = Path(__file__).resolve().parent
    plan = json.loads((base / "sample_plan.json").read_bytes())
    csv1 = base / "csv1.csv"
    if (hashlib.sha256(csv1.read_bytes()).hexdigest() != plan["csv1_sha256"]
            or plan["account_id"] != ACCOUNT or plan["region"] != REGION
            or plan["approval_recorded"] is not True):
        raise PermissionError("Input or scope differs from approved sample plan")
    output = Path("/opt/hl/run")
    output.mkdir(exist_ok=True)
    session, instance, role = metadata_session()
    source = ApprovedS3Source(session, role, plan, output)
    started = time.monotonic()
    error = None
    try:
        source.verify_bucket_region()
        run_csv2(csv1, output, source, resume=False, episode_ids=plan["episode_ids"],
                 policy=plan["selection_policy"])
    except Exception as exc:
        error = type(exc).__name__ + ": " + str(exc)
    finally:
        statistics = {
            "event": "sample_completed" if error is None else "sample_incomplete",
            "instance": instance, "account": ACCOUNT, "region": REGION,
            "elapsed_seconds": time.monotonic() - started, "source_requests": source.requests,
            "source_bytes_received": source.received, "source_bytes_reserved": source.reserved,
            "selection_policy": plan["selection_policy"], "error": error,
        }
        import resource
        statistics["peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        statistics["cpu_seconds"] = time.process_time()
        with (output / "run.log").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(statistics) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        source.close()
        config = Config(connect_timeout=8, read_timeout=30, retries={"max_attempts": 0},
                        s3={"addressing_style": "virtual"})
        sts = session.client("sts", region_name=REGION, config=config)
        s3 = session.client("s3", region_name=REGION, config=config)
        try:
            durable = ["csv1.csv", "csv2.csv", "csv3.csv", "run.log", "progress.sqlite", "cache.sqlite"]
            total = sum((output / name).stat().st_size for name in durable if (output / name).exists())
            if total > plan["limits"]["output_bytes"]:
                raise RuntimeError("Output exceeds authorized result size")
            for name in durable:
                path = output / name
                if not path.exists():
                    continue
                if sts.get_caller_identity()["Account"] != ACCOUNT:
                    raise PermissionError("STS account mismatch before output upload")
                with path.open("rb") as handle:
                    s3.put_object(Bucket=args.bucket, Key="output/sample/" + name, Body=handle,
                                  ExpectedBucketOwner=ACCOUNT, ServerSideEncryption="AES256")
            print(json.dumps(statistics), flush=True)
        finally:
            s3.close()
            sts.close()
    return 0 if error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
