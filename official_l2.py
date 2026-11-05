"""Official historical L2 snapshots as an independent, read-only CSV2 source.

The adapter never reads fills, ABCI states, or book diffs. An injected source
owns authenticated transport. Without an explicit certified timestamp frontier
the runner scans through the requested end, because capture-hour filenames do
not bound the exchange clock (real records can spill into the next hour).
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
from typing import Iterable

ALGORITHM_VERSION = "official-l2-v2"
VERSION = ALGORITHM_VERSION
HOUR_NS = 3_600_000_000_000
DEFAULT_START = "2025-03-22T00:00:00Z"
DEFAULT_END = "2025-10-22T00:00:00Z"
COMMON = ["schema_version", "run_id", "record_type", "episode_id", "status", "reason_code",
          "reason_message", "error_file", "error_member", "error_line", "error_block", "issues_json"]
RANGE = ["data_start", "data_end_exclusive", "formal_start_inclusive", "formal_end_inclusive"]
FILL = ["event_id", "event_time", "block_number", "coin", "direction", "price", "qty",
        "source_file", "source_member", "source_line", "source_event_index"]
SUMMARY = ["coin", "direction", "segment_id", "episode_start", "episode_end", "start_block",
           "end_block", "event_count", "formal_event_count", "buffer_event_count", "duration_ms",
           "total_qty", "entry_notional", "entry_vwap", "partial_episode", "partial_reason",
           "split_before", "split_after"]
CSV1_FIELDS = COMMON + FILL + RANGE + ["is_formal_event"] + [k for k in SUMMARY if k not in FILL] + ["episode_input_hash"]
CSV2_FIELDS = COMMON + ["episode_input_hash", "coin", "direction", "total_qty", "entry_notional",
    "entry_vwap", "partial_episode", "partial_reason", "split_before", "split_after", "tn",
    "target_block", "book_state_point", "book_side", "snapshot_file", "snapshot_member",
    "snapshot_block", "snapshot_time", "snapshot_source_id", "snapshot_selection_status",
    "replay_from_block_exclusive", "replay_to_block_inclusive", "last_applied_block",
    "replay_coverage_mode", "diff_sources_json", "order_count", "price_level_count", "total_depth",
    "side_notional", "best_price", "worst_price", "book_is_empty", "orders_json", "book_hash",
    "snapshot_lag_ms"]
CSV3_FIELDS = COMMON + ["episode_input_hash", "csv2_row_hash", "book_hash", "coin", "direction",
    "episode_start", "tn", "target_block", "event_count", "total_qty", "entry_notional", "entry_vwap",
    "partial_episode", "partial_reason", "split_before", "split_after", "book_side", "order_count",
    "price_level_count", "total_depth", "best_price", "worst_price", "filled_qty", "unfilled_qty",
    "fill_ratio", "close_notional", "exit_vwap", "entry_cost_of_filled_qty", "executed_order_count",
    "executed_price_level_count", "last_execution_price", "la", "depth_insufficient", "fill_trace_json"]
CHINESE_CSV1 = ["模式版本", "运行编号", "记录类型", "接管组编号", "处理状态", "原因代码",
 "原因说明", "错误文件", "错误文件内路径", "错误行号", "错误区块号", "问题明细JSON",
 "接管事件编号", "接管时间UTC", "区块号", "币种", "接管方向", "接管价格", "接管数量",
 "来源文件", "来源文件内路径", "来源行号", "来源事件序号", "数据开始时间UTC", "数据结束时间UTC（不含）",
 "正式开始时间UTC（含）", "正式结束时间UTC（含）", "是否正式范围接管", "连续数据段编号",
 "接管组开始时间UTC", "接管组结束时间UTC", "接管组首区块号", "接管组末区块号",
 "接管笔数", "正式范围接管笔数", "缓冲区接管笔数", "持续时间毫秒", "接管总数量",
 "接管总金额", "加权接管均价", "是否部分接管组", "部分接管组原因", "前侧分段依据", "后侧分段依据", "接管组输入哈希"]
assert len(CSV1_FIELDS) == len(CHINESE_CSV1) == 45
assert len(CSV2_FIELDS) == 47
csv.field_size_limit(2**31 - 1)
_ACTIVE_RUNS = set()
_ACTIVE_RUN_GUARD = threading.Lock()


class InvalidInput(ValueError):
    pass


class SelectionError(ValueError):
    def __init__(self, code, message, key="", line=""):
        super().__init__(message)
        self.code, self.key, self.line = code, key, line


class ObjectNotFound(FileNotFoundError):
    """Only a verified absent object, never a permission failure."""


class SourceAccessError(RuntimeError):
    """A source permission/configuration failure. The runner must stop."""


def text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(text(value).encode("utf-8")).hexdigest()


def dec_text(value):
    value = Decimal(value)
    if not value.is_finite():
        raise InvalidInput("Nonfinite decimal")
    if value == 0:
        return "0"
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def parse_time_ns(value):
    """Integers are exchange Unix milliseconds. ISO strings retain all nine digits."""
    if isinstance(value, bool):
        raise InvalidInput("Boolean timestamp")
    if isinstance(value, int):
        return value * 1_000_000
    if not isinstance(value, str):
        raise InvalidInput("Timestamp must be an ISO string or integer milliseconds")
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})?", value)
    if not match:
        raise InvalidInput("Invalid timestamp")
    fraction = int((match[2] or "").ljust(9, "0"))
    date = datetime.fromisoformat(match[1] + ("+00:00" if match[3] in (None, "Z") else match[3]))
    delta = date.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + fraction


def time_text_ns(value):
    seconds, fraction = divmod(value, 1_000_000_000)
    date = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    digits = f"{fraction:09d}".rstrip("0")
    return date.strftime("%Y-%m-%dT%H:%M:%S") + ("." + digits if digits else ".000") + "Z"


def episode_hash(rows):
    return digest({"fills": [{k: row.get(k, "") for k in FILL} for row in rows],
                   "summary": {k: rows[0].get(k, "") for k in SUMMARY}})


@dataclass
class InputData:
    episodes: list
    issues: list
    sha256: str
    english_rows: list


def load_csv1(path, start_ns=None, end_ns=None):
    start_ns = parse_time_ns(DEFAULT_START) if start_ns is None else start_ns
    end_ns = parse_time_ns(DEFAULT_END) if end_ns is None else end_ns
    if start_ns >= end_ns:
        raise InvalidInput("Empty CSV2 range")
    path = Path(path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    groups, issues, rows, seen = {}, [], [], set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header not in (CSV1_FIELDS, CHINESE_CSV1):
            raise InvalidInput("CSV1 must have the exact English or Chinese 45-column header")
        for number, values in enumerate(reader, 2):
            if len(values) != len(CSV1_FIELDS):
                raise InvalidInput(f"CSV1 column count differs at line {number}")
            row = dict(zip(CSV1_FIELDS, values))
            rows.append(row)
            if row["schema_version"] != "hlp-csv-v3":
                raise InvalidInput("Unsupported CSV1 schema")
            if row["record_type"] == "issue":
                if row["status"] != "input_issue" or row["episode_id"]:
                    raise InvalidInput("Malformed CSV1 diagnostic")
                issues.append(row)
                continue
            if row["record_type"] != "fill" or row["status"] != "grouped" or not row["episode_id"] or not row["event_id"]:
                raise InvalidInput("Malformed CSV1 business row")
            if row["event_id"] in seen:
                raise InvalidInput("Duplicate CSV1 event ID")
            seen.add(row["event_id"])
            if row["direction"] not in ("long", "short") or not row["coin"] or any(c in row["coin"] for c in ("/", "\\", "\x00")):
                raise InvalidInput("Invalid CSV1 coin/direction")
            if row["partial_episode"] not in ("true", "false") or (row["block_number"] and not row["block_number"].isdigit()):
                raise InvalidInput("Invalid CSV1 partial flag or known block")
            for field in ("price", "qty"):
                value = Decimal(row[field])
                if not value.is_finite() or value <= 0:
                    raise InvalidInput("Nonpositive CSV1 " + field)
            t = parse_time_ns(row["event_time"])
            if not parse_time_ns(row["data_start"]) <= t < parse_time_ns(row["data_end_exclusive"]):
                raise InvalidInput("Fill outside CSV1 prepared dates")
            formal = parse_time_ns(row["formal_start_inclusive"]) <= t <= parse_time_ns(row["formal_end_inclusive"])
            if row["is_formal_event"] != str(formal).lower():
                raise InvalidInput("CSV1 formal flag mismatch")
            groups.setdefault(row["episode_id"], []).append(row)
    episodes = []
    for name, fills in groups.items():
        try:
            episode = _validate_episode(name, fills, start_ns, end_ns)
        except (InvalidInput, InvalidOperation, ValueError) as exc:
            first = fills[0]
            tn = first["episode_end"]
            try:
                selected = start_ns <= parse_time_ns(tn) < end_ns
            except InvalidInput:
                selected = any(start_ns <= parse_time_ns(row["event_time"]) < end_ns for row in fills)
            episode = None
            if selected:
                episode = {**{k: first[k] for k in SUMMARY}, "episode_id": name,
                    "episode_input_hash": first["episode_input_hash"], "tn": tn,
                    "tn_ns": parse_time_ns(fills[-1]["event_time"]), "target_block": first["end_block"],
                    "_input_problem": dict(code="INCONSISTENT_CSV1_EPISODE", message=str(exc), source_file=str(path))}
        if episode is not None:
            episodes.append(episode)
    return InputData(sorted(episodes, key=lambda ep: (ep["coin"], ep["tn_ns"], ep["episode_id"])), issues, sha, rows)


def _validate_episode(name, fills, start_ns, end_ns):
    first = fills[0]
    if any(any(row[k] != first[k] for k in SUMMARY + RANGE + ["episode_input_hash", "run_id"]) for row in fills):
        raise InvalidInput("Conflicting CSV1 episode summary " + name)
    if episode_hash(fills) != first["episode_input_hash"]:
        raise InvalidInput("CSV1 episode hash mismatch " + name)
    times = [parse_time_ns(row["event_time"]) for row in fills]
    if any(b < a or b - a > HOUR_NS // 2 for a, b in zip(times, times[1:])):
        raise InvalidInput("CSV1 chronology or episode-chain mismatch " + name)
    known = [int(row["block_number"]) for row in fills if row["block_number"]]
    if known != sorted(known):
        raise InvalidInput("CSV1 known block ordering mismatch " + name)
    formal_count = sum(row["is_formal_event"] == "true" for row in fills)
    if (len(fills) != int(first["event_count"]) or formal_count != int(first["formal_event_count"])
            or len(fills) - formal_count != int(first["buffer_event_count"])):
        raise InvalidInput("CSV1 event counts mismatch " + name)
    if (first["episode_start"] != fills[0]["event_time"] or first["episode_end"] != fills[-1]["event_time"]
            or first["start_block"] != fills[0]["block_number"] or first["end_block"] != fills[-1]["block_number"]
            or Decimal(first["duration_ms"]) != Decimal(times[-1] - times[0]) / 1_000_000):
        raise InvalidInput("CSV1 episode endpoints mismatch " + name)
    with localcontext() as ctx:
        ctx.prec = 60
        quantity = sum((Decimal(row["qty"]) for row in fills), Decimal(0))
        notional = sum((Decimal(row["price"]) * Decimal(row["qty"]) for row in fills), Decimal(0))
        if quantity != Decimal(first["total_qty"]) or notional != Decimal(first["entry_notional"]) or notional / quantity != Decimal(first["entry_vwap"]):
            raise InvalidInput("CSV1 episode arithmetic mismatch " + name)
    if start_ns <= times[-1] < end_ns:
        return {**{k: first[k] for k in SUMMARY}, "episode_id": name,
            "episode_input_hash": first["episode_input_hash"], "tn": fills[-1]["event_time"],
            "tn_ns": times[-1], "target_block": fills[-1]["block_number"]}
    return None


@dataclass
class Snapshot:
    time_ns: int
    coin: str
    levels: tuple
    block: str = ""
    capture_time_ns: int | None = None

    @property
    def signature(self):
        return digest({"coin": self.coin, "time_ns": self.time_ns, "levels": self.levels, "block": self.block})

    def to_dict(self):
        return dict(time_ns=self.time_ns, coin=self.coin, levels=self.levels, block=self.block, capture_time_ns=self.capture_time_ns)

    @classmethod
    def from_dict(cls, value):
        return cls(**value)


def _parse_snapshot(doc):
    if not isinstance(doc, dict):
        raise SelectionError("INVALID_L2_RECORD", "L2 record must be an object")
    capture = parse_time_ns(doc["time"]) if "raw" in doc and "time" in doc else None
    payload = doc.get("raw", doc)
    if not isinstance(payload, dict):
        raise SelectionError("INVALID_L2_RECORD", "L2 wrapper is not an object")
    if "channel" in payload and payload["channel"] != "l2Book":
        raise SelectionError("INVALID_L2_RECORD", "Unexpected historical channel")
    payload = payload.get("data", payload)
    if not isinstance(payload, dict) or not isinstance(payload.get("coin"), str):
        raise SelectionError("INVALID_L2_RECORD", "Missing L2 coin")
    if "time" not in payload:
        raise SelectionError("INVALID_L2_RECORD", "Missing exchange timestamp")
    timestamp = parse_time_ns(payload["time"])
    levels = payload.get("levels")
    if not isinstance(levels, list) or len(levels) != 2:
        raise SelectionError("INVALID_L2_RECORD", "Expected complete bid/ask arrays")
    normalized = []
    for side, entries in enumerate(levels):
        if not isinstance(entries, list):
            raise SelectionError("INVALID_L2_RECORD", "L2 side must be an array")
        result, prices = [], set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise SelectionError("INVALID_L2_LEVEL", "L2 level must be an object")
            price, qty = Decimal(entry["px"]), Decimal(entry["sz"])
            if not price.is_finite() or price <= 0 or not qty.is_finite() or qty < 0:
                raise SelectionError("INVALID_L2_LEVEL", "Invalid L2 price/size")
            if price in prices:
                raise SelectionError("INVALID_L2_LEVEL", "Duplicate price in one published side")
            prices.add(price)
            row = dict(price=dec_text(price), qty=dec_text(qty))
            count = entry.get("n")
            if count is not None:
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise SelectionError("INVALID_L2_LEVEL", "n must be a nonnegative integer")
                row["n"] = count
            result.append(row)
        normalized.append(sorted(result, key=lambda row: Decimal(row["price"]), reverse=side == 0))
    block = payload.get("block_number", doc.get("block_number", ""))
    if block != "":
        if isinstance(block, bool) or not isinstance(block, int) or block < 0:
            raise SelectionError("INVALID_L2_RECORD", "Invalid source block number")
        block = str(block)
    return Snapshot(timestamp, payload["coin"], tuple(normalized), block, capture)


def parse_snapshot(doc):
    try:
        return _parse_snapshot(doc)
    except (InvalidInput, KeyError, TypeError, InvalidOperation) as exc:
        raise SelectionError("INVALID_L2_RECORD", str(exc)) from exc


def book_metrics(snapshot, direction):
    if direction not in ("long", "short"):
        raise InvalidInput("Invalid HLP direction")
    levels = snapshot.levels[0 if direction == "long" else 1]
    with localcontext() as ctx:
        ctx.prec = 60
        depth = sum((Decimal(row["qty"]) for row in levels), Decimal(0))
        amount = sum((Decimal(row["price"]) * Decimal(row["qty"]) for row in levels), Decimal(0))
    reliable_counts = all("n" in row for row in levels)
    return dict(order_count=sum(row["n"] for row in levels) if reliable_counts else "",
        price_level_count=len(levels), total_depth=dec_text(depth), side_notional=dec_text(amount),
        best_price=levels[0]["price"] if levels else "", worst_price=levels[-1]["price"] if levels else "",
        book_is_empty=not levels, orders_json=text(levels), book_hash=digest(levels))


@dataclass
class Candidate:
    snapshot: Snapshot
    key: str = ""
    line: int = 0
    source_identity: dict | None = None
    conflict: bool = False

    def to_dict(self):
        return dict(snapshot=self.snapshot.to_dict(), key=self.key, line=self.line,
                    source_identity=self.source_identity, conflict=self.conflict)

    @classmethod
    def from_dict(cls, value):
        return cls(snapshot=Snapshot.from_dict(value["snapshot"]), **{k: v for k, v in value.items() if k != "snapshot"})


def update_candidate(previous, snapshot, key="", line=0, identity=None):
    if previous is None or snapshot.time_ns < previous.snapshot.time_ns:
        return Candidate(snapshot, key, line, identity or {})
    if snapshot.time_ns == previous.snapshot.time_ns and snapshot.signature != previous.snapshot.signature:
        previous.conflict = True
    return previous


def select_snapshots(records, coin, targets, start_ns, end_ns):
    candidates = {}
    for line, record in enumerate(records, 1):
        snapshot = record if isinstance(record, Snapshot) else parse_snapshot(record)
        if snapshot.coin != coin:
            raise SelectionError("L2_COIN_MISMATCH", "Object contains an unexpected coin", line=line)
        if not start_ns <= snapshot.time_ns < end_ns:
            continue
        for episode in targets:
            if snapshot.time_ns >= episode["tn_ns"]:
                eid = episode["episode_id"]
                candidates[eid] = update_candidate(candidates.get(eid), snapshot, line=line)
    return candidates


def iter_json_lines(chunks, max_line_bytes=8 * 1024 * 1024):
    """Consume LZ4 frames incrementally and reject incomplete frames or JSON."""
    import lz4.frame
    decoder = lz4.frame.LZ4FrameDecompressor()
    pending = b""
    count = 0
    any_frame = False
    for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise SelectionError("CORRUPT_L2_OBJECT", "Transport chunks must be bytes")
        remaining = chunk
        while remaining:
            if decoder.eof:
                decoder = lz4.frame.LZ4FrameDecompressor()
            try:
                decoded = decoder.decompress(remaining, max_length=max_line_bytes + 1)
                while True:
                    pending += decoded
                    while b"\n" in pending:
                        raw, pending = pending.split(b"\n", 1)
                        count += 1
                        if len(raw) > max_line_bytes:
                            raise SelectionError("L2_LINE_TOO_LARGE", "L2 line exceeds configured bound", line=count)
                        if raw.strip():
                            try:
                                yield count, json.loads(raw, parse_float=Decimal)
                            except (ValueError, UnicodeError) as exc:
                                raise SelectionError("CORRUPT_L2_OBJECT", "Invalid L2 JSON", line=count) from exc
                    if len(pending) > max_line_bytes:
                        raise SelectionError("L2_LINE_TOO_LARGE", "L2 line exceeds configured bound", line=count + 1)
                    if decoder.eof or decoder.needs_input:
                        break
                    decoded = decoder.decompress(b"", max_length=max_line_bytes + 1)
                any_frame = any_frame or decoder.eof
                remaining = decoder.unused_data or b"" if decoder.eof else b""
            except RuntimeError as exc:
                raise SelectionError("CORRUPT_L2_OBJECT", "LZ4 decompression failed", line=count + 1) from exc
    if not any_frame or not decoder.eof:
        raise SelectionError("CORRUPT_L2_OBJECT", "Truncated LZ4 frame", line=count + 1)
    if pending.strip():
        try:
            yield count + 1, json.loads(pending, parse_float=Decimal)
        except (ValueError, UnicodeError) as exc:
            raise SelectionError("CORRUPT_L2_OBJECT", "Invalid final L2 JSON", line=count + 1) from exc


@dataclass
class ObjectScan:
    candidates: dict
    problems: list
    stats: dict


def scan_object(chunks, coin, targets, key, identity, start_ns=None, end_ns=None):
    start_ns = parse_time_ns(DEFAULT_START) if start_ns is None else start_ns
    end_ns = parse_time_ns(DEFAULT_END) if end_ns is None else end_ns
    candidates, problems = {}, []
    stats = dict(records=0, bytes_read=0, time_ordered=True, first_time_ns=None, last_time_ns=None,
                 min_time_ns=None, max_time_ns=None, capture_min_ns=None, capture_max_ns=None, complete=False)

    def counted():
        for chunk in chunks:
            stats["bytes_read"] += len(chunk)
            yield chunk

    try:
        for line, record in iter_json_lines(counted()):
            snapshot = parse_snapshot(record)
            if snapshot.coin != coin:
                raise SelectionError("L2_COIN_MISMATCH", "Object contains an unexpected coin", key, line)
            stats["records"] += 1
            previous = stats["last_time_ns"]
            stats["time_ordered"] = stats["time_ordered"] and (previous is None or previous <= snapshot.time_ns)
            stats["first_time_ns"] = snapshot.time_ns if previous is None else stats["first_time_ns"]
            stats["last_time_ns"] = snapshot.time_ns
            for field, value in (("time", snapshot.time_ns), ("capture", snapshot.capture_time_ns)):
                if value is None:
                    continue
                a, b = field + "_min_ns" if field == "capture" else "min_time_ns", field + "_max_ns" if field == "capture" else "max_time_ns"
                stats[a] = value if stats[a] is None else min(stats[a], value)
                stats[b] = value if stats[b] is None else max(stats[b], value)
            if not start_ns <= snapshot.time_ns < end_ns:
                continue
            for episode in targets:
                if snapshot.time_ns >= episode["tn_ns"]:
                    eid = episode["episode_id"]
                    candidates[eid] = update_candidate(candidates.get(eid), snapshot, key, line, identity)
        stats["complete"] = True
    except (SelectionError, InvalidInput, KeyError, TypeError, InvalidOperation, UnicodeError) as exc:
        problems.append(dict(code=getattr(exc, "code", "CORRUPT_L2_OBJECT"), message=str(exc),
                             source_file=key, source_line=getattr(exc, "line", "")))
        candidates = {}
    return ObjectScan(candidates, problems, stats)


def result_row(episode, candidate=None, run_id="official_l2", problem=None, issues=None):
    row = dict.fromkeys(CSV2_FIELDS, "")
    row.update(schema_version="hlp-csv-v3", run_id=run_id, record_type="episode",
               episode_id=episode["episode_id"], status="book_ready", issues_json=text(issues or []),
               book_state_point="post_takeover_l2_snapshot", book_side="bid" if episode["direction"] == "long" else "ask")
    for field in ("episode_input_hash", "coin", "direction", "total_qty", "entry_notional", "entry_vwap",
                  "partial_episode", "partial_reason", "split_before", "split_after", "tn", "target_block"):
        row[field] = episode[field]
    if candidate is not None and candidate.conflict:
        problem = dict(code="CONFLICTING_L2_AT_SAME_TIME", message="Selected exchange timestamp has conflicting published snapshots",
                       source_file=candidate.key, source_line=candidate.line)
    if problem is not None or candidate is None:
        problem = problem or dict(code="NO_L2_AT_OR_AFTER_TN_IN_RANGE", message="No available snapshot at or after Tn within the allowed range")
        row.update(status="skipped", reason_code=problem["code"], reason_message=problem["message"],
                   error_file=problem.get("source_file", ""), error_line=problem.get("source_line", ""),
                   issues_json=text([*(issues or []), problem]))
        return row
    snapshot = candidate.snapshot
    if snapshot.time_ns < episode["tn_ns"]:
        raise InvalidInput("Selected snapshot predates Tn")
    row.update(snapshot_file=candidate.key if candidate.key.startswith("s3://") else "s3://hyperliquid-archive/" + candidate.key, snapshot_block=snapshot.block,
        snapshot_time=time_text_ns(snapshot.time_ns), snapshot_selection_status="nearest_at_or_after_tn",
        snapshot_source_id=digest(dict(object=candidate.source_identity, key=candidate.key, line=candidate.line, snapshot=snapshot.signature)),
        snapshot_lag_ms=dec_text(Decimal(snapshot.time_ns - episode["tn_ns"]) / 1_000_000))
    row.update(book_metrics(snapshot, episode["direction"]))
    return row


class LocalObject:
    def __init__(self, path, key):
        self.path, self.key = Path(path), key
        stat = self.path.stat()
        self.identity = dict(uri="s3://hyperliquid-archive/" + key, local_path=str(self.path.resolve()),
                             size=stat.st_size, mtime_ns=stat.st_mtime_ns)
        self.handle = None

    def __enter__(self):
        self.handle = self.path.open("rb")
        return self

    def iter_chunks(self):
        while chunk := self.handle.read(256 * 1024):
            yield chunk

    def __exit__(self, *_args):
        self.handle.close()
        stat = self.path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (self.identity["size"], self.identity["mtime_ns"]):
            raise SourceAccessError("Local cached source changed while reading")


class LocalSource:
    """Read an existing archive layout. Never makes a network request."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.fingerprint = "local:" + str(self.root)

    def open(self, key):
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise SourceAccessError("Archive key escapes local source")
        if not path.is_file():
            raise ObjectNotFound(key)
        return LocalObject(path, key)


def _csv_bytes(fields, row=None):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n", extrasaction="raise")
    if row is None:
        writer.writeheader()
    else:
        writer.writerow({k: str(v).lower() if isinstance(v, bool) else "" if v is None else v for k, v in row.items()})
    return stream.getvalue().encode("utf-8")


def _fault(point, **context):
    """Offline tests may replace this seam to verify interrupted-write recovery."""


def _log(path, event, **details):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text(dict(at=datetime.now(timezone.utc).isoformat(), event=event, **details)) + "\n")


def _connect(path):
    db = sqlite3.connect(path, timeout=30)
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    db.commit()
    return db


def _get(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _put(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, text(value)))


@contextmanager
def _run_lock(path):
    run_key = str(path.resolve())
    with _ACTIVE_RUN_GUARD:
        if run_key in _ACTIVE_RUNS:
            raise InvalidInput("Another writer is using this run")
        _ACTIVE_RUNS.add(run_key)
    handle = None
    offset = 1 << 44
    try:
        handle = path.open("a+b")
        if os.name == "nt":
            import msvcrt
            handle.seek(offset)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, offset)
        yield
    finally:
        if handle is not None:
            handle.close()
        with _ACTIVE_RUN_GUARD:
            _ACTIVE_RUNS.discard(run_key)


def _hour_key(coin, hour):
    dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=hour // 1_000_000_000)
    return f"market_data/{dt:%Y%m%d}/{dt.hour}/l2Book/{coin}.lz4"


def _repair_output(path, db):
    expected = _get(db, "csv2_offset")
    with path.open("r+b") as handle:
        header = _csv_bytes(CSV2_FIELDS)
        if handle.read(len(header)) != header:
            raise InvalidInput("CSV2 header changed")
        for position, end, sha in db.execute("SELECT position,end,sha FROM committed_rows ORDER BY position"):
            handle.seek(position)
            if hashlib.sha256(handle.read(end - position)).hexdigest() != sha:
                raise InvalidInput("Committed CSV2 bytes changed or are missing")
        handle.seek(0, os.SEEK_END)
        if handle.tell() < expected:
            raise InvalidInput("CSV2 is shorter than committed progress")
        handle.truncate(expected)
        handle.flush()
        os.fsync(handle.fileno())


def _commit_row(directory, db, row):
    payload = _csv_bytes(CSV2_FIELDS, row)
    position = _get(db, "csv2_offset")
    with (directory / "csv2.csv").open("r+b") as handle:
        handle.seek(position)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    _fault("csv2_fsynced_before_progress", row=row, position=position)
    with db:
        db.execute("INSERT INTO committed_rows VALUES (?,?,?,?,?)",
                   (row["episode_id"], position, position + len(payload), hashlib.sha256(payload).hexdigest(), row["status"]))
        _put(db, "csv2_offset", position + len(payload))
    _fault("csv2_progress_committed", row=row)


def _episode_issues(issues, episode):
    hour = episode["tn_ns"] // HOUR_NS * HOUR_NS
    return [{k: v for k, v in issue.items() if k != "hour_ns"}
            for issue in issues if issue.get("hour_ns", hour) >= hour]


def run_csv2(csv1_path, run_dir, source, start=DEFAULT_START, end=DEFAULT_END, resume=True,
             episode_ids=None, policy="strict", max_cache_bytes=128 * 1024 * 1024):
    """Stream the required coin-hours and durably emit one CSV2 row per episode.

    ``validated_next_hour`` is an explicit user-approved archive ordering
    assumption. It completes an episode only after a fully read later hour has
    min(exchange time) strictly above the current candidate. Every earlier or
    conflicting candidate in the complete object is considered first. A time
    regression that reaches the candidate extends that episode's search until
    a later complete object establishes a frontier. Capture/key-hour mismatch
    prevents that object from serving as a frontier. ``strict`` always scans
    the coin through the query end. No maximum snapshot lag is imposed.
    """
    if policy not in ("strict", "validated_next_hour"):
        raise InvalidInput("Unknown L2 selection policy")
    start_ns, end_ns = parse_time_ns(start), parse_time_ns(end)
    if start_ns >= end_ns or start_ns % HOUR_NS or end_ns % HOUR_NS:
        raise InvalidInput("Run bounds must be increasing whole UTC hours")
    directory = Path(run_dir)
    directory.mkdir(parents=True, exist_ok=True)
    config = dict(version=VERSION, start=start, end=end, policy=policy,
                  episode_ids=sorted(episode_ids) if episode_ids is not None else None,
                  source_fingerprint=str(source.fingerprint), csv2_fields=CSV2_FIELDS)
    with _run_lock(directory / "progress.sqlite"):
        db, cache = _connect(directory / "progress.sqlite"), _connect(directory / "cache.sqlite")
        try:
            db.execute("CREATE TABLE IF NOT EXISTS committed_rows(episode TEXT PRIMARY KEY,position INTEGER,end INTEGER,sha TEXT,status TEXT)")
            cache.execute("CREATE TABLE IF NOT EXISTS objects(key TEXT PRIMARY KEY,body TEXT NOT NULL)")
            cache.commit()
            stored = _get(db, "config")
            if stored is None:
                if any((directory / name).exists() for name in ("csv1.csv", "csv2.csv", "csv3.csv")):
                    raise InvalidInput("Refuse to initialize over existing run CSV files")
                imported = load_csv1(csv1_path, start_ns, end_ns)
                episodes = imported.episodes
                if episode_ids is not None:
                    chosen = set(episode_ids)
                    episodes = [ep for ep in episodes if ep["episode_id"] in chosen]
                    if {ep["episode_id"] for ep in episodes} != chosen:
                        raise InvalidInput("Sample list contains unknown or out-of-range episodes")
                shutil.copyfile(csv1_path, directory / "csv1.csv")
                for name, fields in (("csv2.csv", CSV2_FIELDS), ("csv3.csv", CSV3_FIELDS)):
                    with (directory / name).open("xb") as handle:
                        handle.write(_csv_bytes(fields))
                        handle.flush()
                        os.fsync(handle.fileno())
                with db:
                    _put(db, "config", config)
                    _put(db, "input_sha256", imported.sha256)
                    _put(db, "input_issues", imported.issues)
                    _put(db, "episodes", episodes)
                    _put(db, "csv2_offset", len(_csv_bytes(CSV2_FIELDS)))
                _log(directory / "run.log", "L2_RUN_INITIALIZED", config=config, episodes=len(episodes), csv1_sha256=imported.sha256)
            else:
                if not resume or stored != config:
                    raise InvalidInput("Run configuration changed or resume disabled. Use a new run directory")
                if hashlib.sha256((directory / "csv1.csv").read_bytes()).hexdigest() != _get(db, "input_sha256"):
                    raise InvalidInput("Imported CSV1 changed")
                episodes = _get(db, "episodes")
            _repair_output(directory / "csv2.csv", db)
            completed = {row[0] for row in db.execute("SELECT episode FROM committed_rows")}
            by_coin = defaultdict(list)
            for episode in episodes:
                if episode["episode_id"] not in completed:
                    if "_input_problem" in episode:
                        _commit_row(directory, db, result_row(episode, run_id=directory.name, problem=episode["_input_problem"]))
                    else:
                        by_coin[episode["coin"]].append(episode)
            run_id = directory.name
            for coin, coin_episodes in sorted(by_coin.items()):
                state = _get(cache, "coin:" + coin)
                if state is None:
                    state = dict(hour=min(ep["tn_ns"] for ep in coin_episodes) // HOUR_NS * HOUR_NS,
                                 candidates={}, issues=[], strict=policy == "strict", previous_max_ns=None)
                candidates = {eid: Candidate.from_dict(value) for eid, value in state["candidates"].items()}
                pending = {ep["episode_id"]: ep for ep in coin_episodes}
                hour = state["hour"]
                while hour < end_ns and pending:
                    next_demand_hour = min(ep["tn_ns"] for ep in pending.values()) // HOUR_NS * HOUR_NS
                    if hour < next_demand_hour:
                        hour = next_demand_hour
                        state["previous_max_ns"] = None
                    key = _hour_key(coin, hour)
                    targets = [ep for ep in pending.values() if ep["tn_ns"] < hour + HOUR_NS]
                    previous_candidates = {eid: c.snapshot.time_ns for eid, c in candidates.items()}
                    cached = cache.execute("SELECT body FROM objects WHERE key=?", (key,)).fetchone()
                    if cached is not None:
                        saved = json.loads(cached[0])
                        scan = ObjectScan({eid: Candidate.from_dict(value) for eid, value in saved["candidates"].items()}, saved["problems"], saved["stats"])
                    else:
                        try:
                            with source.open(key) as opened:
                                scan = scan_object(opened.iter_chunks(), coin, targets, key, opened.identity, start_ns, end_ns)
                        except ObjectNotFound:
                            scan = ObjectScan({}, [], dict(missing=True, complete=True, records=0, bytes_read=0))
                        except SelectionError as exc:
                            if exc.code != "SAMPLE_SEARCH_SCOPE_EXHAUSTED":
                                raise
                            for episode in pending.values():
                                _commit_row(directory, db, result_row(episode, run_id=run_id,
                                    problem=dict(code=exc.code, message=str(exc), source_file=key),
                                    issues=_episode_issues(state["issues"], episode)))
                            pending.clear()
                            _log(directory / "run.log", "SAMPLE_SEARCH_SCOPE_EXHAUSTED", coin=coin, key=key)
                            break
                        # AccessDenied and network failures deliberately propagate.
                        saved = dict(candidates={eid: value.to_dict() for eid, value in scan.candidates.items()}, problems=scan.problems, stats=scan.stats)
                        with cache:
                            cache.execute("INSERT INTO objects VALUES (?,?)", (key, text(saved)))
                        _log(directory / "run.log", "L2_OBJECT_SCANNED", key=key, stats=scan.stats, problems=scan.problems)
                    if scan.stats.get("missing"):
                        state["issues"].append(dict(code="MISSING_L2_OBJECT", source_file=key,
                                                   hour_ns=hour, message="Object is confirmed absent; continue to later archive snapshots"))
                    for eid, incoming in scan.candidates.items():
                        if eid in pending:
                            current = candidates.get(eid)
                            if current is None or incoming.snapshot.time_ns < current.snapshot.time_ns:
                                candidates[eid] = incoming
                            else:
                                merged = update_candidate(current, incoming.snapshot, incoming.key, incoming.line, incoming.source_identity)
                                if incoming.snapshot.time_ns == merged.snapshot.time_ns:
                                    merged.conflict = merged.conflict or incoming.conflict
                    if scan.problems:
                        for episode in targets:
                            _commit_row(directory, db, result_row(episode, run_id=run_id,
                                        problem=dict(code="L2_SELECTION_UNCONFIRMED", message="Unreadable object prevents proving the nearest snapshot", source_file=key),
                                        issues=[*_episode_issues(state["issues"], episode), *scan.problems]))
                            pending.pop(episode["episode_id"])
                            candidates.pop(episode["episode_id"], None)
                    minimum = scan.stats.get("min_time_ns")
                    maximum = scan.stats.get("max_time_ns")
                    capture_partition_ok = True
                    if scan.stats.get("records"):
                        capture_min, capture_max = scan.stats.get("capture_min_ns"), scan.stats.get("capture_max_ns")
                        capture_partition_ok = capture_min is None or hour <= capture_min <= capture_max < hour + HOUR_NS
                        observations = []
                        if not scan.stats["time_ordered"]:
                            observations.append("L2_OBJECT_TIME_DISORDER")
                        if state["previous_max_ns"] is not None and minimum < state["previous_max_ns"]:
                            observations.append("L2_BOUNDARY_TIME_OVERLAP")
                        if not capture_partition_ok:
                            observations.append("L2_CAPTURE_HOUR_MISMATCH")
                        for code in observations:
                            state["issues"].append(dict(code=code, source_file=key, hour_ns=hour,
                                message="Complete-object exchange-time minima determine each pending episode's frontier",
                                min_time_ns=minimum, max_time_ns=maximum,
                                previous_max_time_ns=state["previous_max_ns"]))
                        if observations:
                            affected = [eid for eid, candidate in candidates.items()
                                        if eid in pending and minimum <= candidate.snapshot.time_ns]
                            _log(directory / "run.log", "L2_ORDERING_OBSERVATION", key=key,
                                 observations=observations, candidate_search_extended=affected,
                                 capture_partition_ok=capture_partition_ok)
                        state["previous_max_ns"] = maximum if state["previous_max_ns"] is None else max(maximum, state["previous_max_ns"])
                    finished = []
                    if policy == "validated_next_hour" and minimum is not None and capture_partition_ok:
                        for eid, episode in pending.items():
                            candidate = candidates.get(eid)
                            if (candidate is not None and eid in previous_candidates
                                    and hour > episode["tn_ns"] // HOUR_NS * HOUR_NS
                                    and minimum > candidate.snapshot.time_ns):
                                _commit_row(directory, db, result_row(episode, candidate, run_id, issues=_episode_issues(state["issues"], episode)))
                                finished.append(eid)
                    for eid in finished:
                        pending.pop(eid)
                        candidates.pop(eid, None)
                    hour += HOUR_NS
                    state.update(hour=hour, candidates={eid: value.to_dict() for eid, value in candidates.items() if eid in pending})
                    with cache:
                        _put(cache, "coin:" + coin, state)
                    if cache.execute("PRAGMA page_count").fetchone()[0] * cache.execute("PRAGMA page_size").fetchone()[0] > max_cache_bytes:
                        raise SourceAccessError("L2 cache capacity reached; preserve progress and review the configured cap")
                for eid, episode in pending.items():
                    _commit_row(directory, db, result_row(episode, candidates.get(eid), run_id, issues=_episode_issues(state["issues"], episode)))
                _log(directory / "run.log", "L2_COIN_COMPLETE", coin=coin)
            with db:
                _put(db, "completed", True)
            results = dict(db.execute("SELECT status,count(*) FROM committed_rows GROUP BY status"))
            _log(directory / "run.log", "CSV2_COMPLETE", episodes=len(episodes), results=results)
            return dict(episodes=len(episodes), results=results, csv2=str(directory / "csv2.csv"), policy=policy)
        finally:
            cache.close()
            db.close()
