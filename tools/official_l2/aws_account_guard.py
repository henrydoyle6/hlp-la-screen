"""Restrict local AWS download sessions to the user-approved AWS account."""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config


EXPECTED_ACCOUNT_ID = "555066174629"
FORMER_ACCOUNT_ID = "881004720361"
PAUSE_FILE = Path(__file__).resolve().parent.parent / "config" / "aws_activity_pause.json"
L2_PERMIT_FILE = PAUSE_FILE.with_name("aws_l2_verification_permit.json")
NODE_TRADES_PERMIT_FILE = PAUSE_FILE.with_name("aws_node_trades_sample_permit.json")


class AWSIdentityVerificationError(RuntimeError):
    """Safe diagnostic fields for a failed STS identity check; never stores secrets."""

    def __init__(self, cause_type, aws_error_code=None, http_status=None):
        super().__init__("Cannot verify AWS account identity; data requests are blocked.")
        self.cause_type = cause_type
        self.aws_error_code = aws_error_code
        self.http_status = http_status


def validate_l2_permit(permit_path):
    if Path(permit_path).resolve() != L2_PERMIT_FILE.resolve():
        raise PermissionError("Unrecognized scoped AWS permit")
    permit = json.loads(L2_PERMIT_FILE.read_text("utf-8"))
    if (permit.get("enabled") is not True or permit.get("approved_by_user") is not True
            or permit.get("activity") != "l2_history_verification_20261030"
            or permit.get("account_id") != EXPECTED_ACCOUNT_ID
            or datetime.now(timezone.utc) >= datetime.fromisoformat(permit["expires_at_utc"])):
        raise PermissionError("Scoped L2 verification permit is inactive or expired")
    limits = permit["limits"]
    if not (0 < limits["max_metadata_requests"] <= 20 and 0 < limits["max_get_requests"] <= 3
            and 0 < limits["max_body_bytes"] <= 6 * 1024 * 1024
            and 0 < limits["max_cost_usd"] <= 0.10):
        raise PermissionError("Scoped L2 verification limits exceed user authorization")
    return permit


def validate_l2_request(permit_path, model, params, **kwargs):
    permit = validate_l2_permit(permit_path)
    if model.name == "GetCallerIdentity" and model.service_model.service_name == "sts":
        return
    if model.service_model.service_name != "s3" or params.get("Bucket") != "hyperliquid-archive":
        raise PermissionError("Scoped L2 permit forbids this AWS service or bucket")
    if params.get("RequestPayer") != "requester":
        raise PermissionError("Scoped L2 request requires explicit RequestPayer")
    if model.name == "ListObjectsV2":
        if not params.get("Prefix", "").startswith("market_data/") or params.get("MaxKeys", 1000) > 1000:
            raise PermissionError("Scoped L2 permit forbids this directory query")
    elif model.name in ("HeadObject", "GetObject"):
        key = params.get("Key", "")
        if key not in permit.get("selected_keys", []) or not re.fullmatch(r"market_data/\d{8}/\d{1,2}/l2Book/BTC\.lz4", key):
            raise PermissionError("Object is outside the frozen BTC sample list")
        if model.name == "GetObject":
            match = re.fullmatch(r"bytes=0-(\d+)", params.get("Range", ""))
            if not match or int(match[1]) >= 2 * 1024 * 1024 or not params.get("IfMatch"):
                raise PermissionError("Only bounded conditional prefix Range GET is authorized")
    else:
        raise PermissionError("Scoped L2 permit forbids this AWS operation")


def validate_node_trades_permit(permit_path):
    """Validate the one-time, exact-object node_trades sample exception."""
    if Path(permit_path).resolve() != NODE_TRADES_PERMIT_FILE.resolve():
        raise PermissionError("Unrecognized scoped AWS permit")
    permit = json.loads(NODE_TRADES_PERMIT_FILE.read_text("utf-8"))
    if (permit.get("enabled") is not True or permit.get("approved_by_user") is not True
            or permit.get("activity") != "single_node_trades_sample_20261102"
            or permit.get("account_id") != EXPECTED_ACCOUNT_ID
            or datetime.now(timezone.utc) >= datetime.fromisoformat(permit["expires_at_utc"])):
        raise PermissionError("Scoped node_trades permit is inactive or expired")
    if permit.get("object") != {
        "bucket": "hl-mainnet-node-data",
        "key": "node_trades/hourly/20250322/10.lz4",
        "region": "ap-northeast-1",
        "size_bytes": 1064509,
        "etag": '"b1efa2161b04e84d19defd4c0c9617bb"',
    }:
        raise PermissionError("Scoped node_trades permit does not match the approved object")
    limits = permit.get("limits", {})
    if not (limits.get("max_sts_requests") == 1
            and limits.get("max_get_requests") == 1
            and limits.get("max_body_bytes") == 1064509
            and 0 < limits.get("max_cost_usd", 0) <= 0.10):
        raise PermissionError("Scoped node_trades limits exceed user authorization")
    return permit


def validate_node_trades_request(permit_path, model, params, usage):
    permit = validate_node_trades_permit(permit_path)
    service = model.service_model.service_name
    if service == "sts" and model.name == "GetCallerIdentity":
        usage["sts"] += 1
        if usage["sts"] > permit["limits"]["max_sts_requests"]:
            raise PermissionError("Scoped node_trades permit allows one STS identity request")
        return
    obj = permit["object"]
    if service != "s3" or model.name != "GetObject":
        raise PermissionError("Scoped node_trades permit allows only one exact-object GetObject")
    if (params.get("Bucket") != obj["bucket"]
            or params.get("Key") != obj["key"]
            or params.get("RequestPayer") != "requester"
            or params.get("IfMatch") != obj["etag"]
            or params.get("Range") != f"bytes=0-{obj['size_bytes'] - 1}"):
        raise PermissionError("S3 request does not match the approved node_trades sample")
    usage["get"] += 1
    if usage["get"] > permit["limits"]["max_get_requests"]:
        raise PermissionError("Scoped node_trades permit allows one GetObject request")


def assert_aws_activity_allowed(**_event_kwargs):
    """Honor the user pause before credential loading and every SDK request."""
    if not PAUSE_FILE.exists():
        return
    try:
        state = json.loads(PAUSE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PermissionError("Cannot read AWS pause status; all AWS requests are blocked.") from exc
    if not isinstance(state, dict) or state.get("paused", True) is not False:
        raise PermissionError(
            "AWS activity is paused by the user. No identity/data request or credential "
            "fallback is allowed. Explicit user instruction is required to resume."
        )


def verified_session_from_csv(
    path: Path | None,
    *,
    l2_permit: Path | None = None,
    node_trades_permit: Path | None = None,
):
    """Fail closed before any data request; never use the SDK default chain."""
    if l2_permit is not None and node_trades_permit is not None:
        raise ValueError("Only one scoped AWS permit may be used per session.")
    if l2_permit is None and node_trades_permit is None:
        assert_aws_activity_allowed()
    elif l2_permit is not None:
        validate_l2_permit(l2_permit)
    else:
        validate_node_trades_permit(node_trades_permit)
    if path is None:
        raise ValueError(
            "Specify a credential CSV explicitly. AWS account "
            f"{EXPECTED_ACCOUNT_ID} is required; automatic credential fallback is disabled."
        )
    path = Path(path).resolve()
    if "disabled_aws_credentials" in path.parts:
        raise ValueError("Quarantined former-account credentials cannot be used.")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != 1:
        raise ValueError("Credential CSV must contain exactly one row.")
    row = {str(k).strip().lower(): (v or "").strip() for k, v in rows[0].items()}
    access_key = row.get("access key id") or row.get("aws_access_key_id")
    secret_key = row.get("secret access key") or row.get("aws_secret_access_key")
    session_token = row.get("session token") or row.get("aws_session_token") or None
    if not access_key or not secret_key:
        raise ValueError("Credential CSV lacks Access key ID or Secret access key.")
    session = boto3.Session(
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=session_token,
        region_name="us-east-1",
    )
    if l2_permit is None and node_trades_permit is None:
        session.events.register("before-send.*", assert_aws_activity_allowed)
    elif l2_permit is not None:
        def check_scoped_pause(**kwargs):
            validate_l2_permit(l2_permit)
            # before-send handlers must return None or the SDK treats the value
            # as a synthetic HTTP response.
            return None
        session.events.register("before-send.*", check_scoped_pause)
        session.events.register("before-parameter-build.*", lambda **kwargs: validate_l2_request(l2_permit, **kwargs))
    else:
        usage = {"sts": 0, "get": 0}

        def check_node_trades_parameters(model, params, **_event_kwargs):
            # Botocore also supplies context/event_name to parameter-build hooks.
            # Keep the request guard focused on the modeled operation parameters.
            validate_node_trades_request(
                node_trades_permit,
                model=model,
                params=params,
                usage=usage,
            )

        def check_scoped_trade_pause(**kwargs):
            validate_node_trades_permit(node_trades_permit)
            # before-send handlers must return None or the SDK treats the value
            # as a synthetic HTTP response.
            return None

        session.events.register("before-send.*", check_scoped_trade_pause)
        session.events.register(
            "before-parameter-build.*",
            check_node_trades_parameters,
        )
    sts = session.client(
        "sts",
        config=Config(
            connect_timeout=10,
            read_timeout=15,
            retries={"max_attempts": 0},
        ),
    )
    try:
        identity = sts.get_caller_identity()
    except Exception as exc:
        response = getattr(exc, "response", None)
        error = response.get("Error", {}) if isinstance(response, dict) else {}
        metadata = response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
        code = error.get("Code")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", code):
            code = None
        status = metadata.get("HTTPStatusCode")
        if not isinstance(status, int) or not 100 <= status <= 599:
            status = None
        raise AWSIdentityVerificationError(type(exc).__name__, code, status) from None
    finally:
        sts.close()
    account = identity.get("Account")
    if account != EXPECTED_ACCOUNT_ID:
        raise PermissionError(
            f"AWS account {account or 'unknown'} is blocked. "
            f"Only {EXPECTED_ACCOUNT_ID} is approved for crawling."
        )
    return session
