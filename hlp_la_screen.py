"""Shared exact order-book and LA engine. Production entry: python run_hlp.py.

Framework: fills -> completed CSV1 -> single-side CSV2 -> CSV-only CSV3.
Formal UTC boundaries are inclusive, raw prepared-day end is exclusive.
Six durable outputs and resumable SQLite state are specified in docs/vnext_spec.md.
"""
from __future__ import annotations
import bz2
import gzip
import json
import lzma
import os
import contextlib
import zipfile
import io
import logging
import sys
import threading
import time as _clock
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping
VERSION = '0.2.0'
GAP_MS = 30 * 60 * 1000
BUFFER_MS = 60 * 60 * 1000
CALC_PRECISION = 60
FRAMEWORK = "See docs/vnext_spec.md: inclusive formal range, staged CSVs, local issues, six SQLite/CSV/log files."
INPUT_CONTRACT = "Node by-block fills/diffs; native RMP via shared extractor; Decimal prices and remaining quantities."

class BookError(RuntimeError):
    """Local reconstruction failure; callers can skip just affected episodes."""

    def __init__(self, code: str, message: str, source: str = "") -> None:
        self.code = str(code)
        self.message = str(message)
        self.source = str(source)
        label = f"{self.code} [{self.source}]" if self.source else self.code
        super().__init__(f"{label}: {self.message}")


@dataclass(frozen=True)
class SnapshotRef:
    """Declared metadata; no filesystem access or file preflight is performed."""

    path: str
    block_number: int
    time_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.path, (str, Path)) or not str(self.path):
            raise BookError("INVALID_SNAPSHOT_REF", "Snapshot path must be non-empty")
        object.__setattr__(self, "path", str(self.path))
        object.__setattr__(self, "block_number", _book_int(self.block_number, "snapshot block_number"))
        object.__setattr__(self, "time_ms", _book_int(self.time_ms, "snapshot time_ms"))


def book_decimal_text(value: Decimal) -> str:
    """Stable decimal text without float conversion or context rounding."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise BookError("INVALID_NUMBER", "Expected a finite Decimal")
    if value == 0:
        return "0"
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result


def _book_decimal(value: Any, field: str, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise BookError(
            "INVALID_NUMBER",
            f"{field} must be a decimal string, integer, or Decimal; not bool/float",
        )
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise BookError("INVALID_NUMBER", f"Invalid {field}: {value!r}") from exc
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        comparison = "non-negative" if allow_zero else "positive"
        raise BookError("INVALID_NUMBER", f"{field} must be finite and {comparison}")
    return result


def _book_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise BookError("INVALID_INTEGER", f"{field} cannot be a boolean")
    try:
        decimal_value = Decimal(str(value))
        if not decimal_value.is_finite() or decimal_value != decimal_value.to_integral_value():
            raise ValueError("not an integer")
        result = int(decimal_value)
    except (InvalidOperation, TypeError, ValueError, OverflowError) as exc:
        raise BookError("INVALID_INTEGER", f"Invalid {field}: {value!r}") from exc
    if result < 0:
        raise BookError("INVALID_INTEGER", f"{field} must be non-negative")
    return result


def _book_oid(value: Any) -> str:
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list, tuple, float)):
        raise BookError("INVALID_ORDER_ID", f"Invalid order id: {value!r}")
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise BookError("INVALID_ORDER_ID", "A numeric order id must be a finite integer")
        value = int(value)
    result = str(value).strip()
    if not result:
        raise BookError("INVALID_ORDER_ID", "An order id cannot be empty")
    return result


def _book_side(value: Any) -> str:
    mapping = {"b": "bid", "bid": "bid", "buy": "bid", "a": "ask", "ask": "ask", "sell": "ask"}
    side = mapping.get(str(value).lower())
    if side is None:
        raise BookError("INVALID_SIDE", f"Unknown order-book side: {value!r}")
    return side


def _book_field(data: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in data:
            return data[name]
    raise BookError("MISSING_FIELD", f"Missing field: {' / '.join(names)}")


def _book_order_record(raw: Any) -> tuple[str, Mapping[str, Any]]:
    """Native includeUsers snapshots store each order as [address, order]."""
    if isinstance(raw, Mapping):
        user = raw.get("user", "")
        if user is None:
            user = ""
        if not isinstance(user, str):
            raise BookError("SNAPSHOT_SCHEMA", "Order user must be a string")
        return user, raw
    if isinstance(raw, (list, tuple)) and len(raw) == 2 and isinstance(raw[0], str) and isinstance(raw[1], Mapping):
        return raw[0], raw[1]
    raise BookError("SNAPSHOT_SCHEMA", "Expected an order object or [user, order] pair")


def _book_is_trigger(order: Mapping[str, Any]) -> bool:
    value = order.get("isTrigger", False)
    if isinstance(value, bool):
        return value
    # Explicit normalized JSON adapters may serialize booleans as these strings.
    if value == "true":
        return True
    if value == "false":
        return False
    raise BookError("SNAPSHOT_SCHEMA", "isTrigger must be a boolean")


@dataclass(frozen=True)
class Order:
    oid: str
    side: str
    price: Decimal
    qty: Decimal
    user: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "oid", _book_oid(self.oid))
        object.__setattr__(self, "side", _book_side(self.side))
        object.__setattr__(self, "price", _book_decimal(self.price, "price"))
        object.__setattr__(self, "qty", _book_decimal(self.qty, "qty", allow_zero=True))
        if not isinstance(self.user, str):
            raise BookError("INVALID_USER", "Order user must be a string")


class OrderBook:
    """Mutable single-coin book.  Same-price orders remain independent.

    An update to zero size is retained internally until its remove event arrives.
    This is important for native sequences `update(newSz=0)` then `remove`.
    Zero-size entries are excluded from executable side_orders(), while
    normalized() retains them for a faithful state-projection comparison.

    Native replay uses update.newSz as the new absolute remaining quantity.
    origSz is not required to match the old quantity unless strict_orig_sz is
    explicitly enabled for an archive whose origSz semantics are confirmed.
    """

    def __init__(self, coin: str, strict_orig_sz: bool = False) -> None:
        if not isinstance(coin, str) or not coin:
            raise BookError("INVALID_COIN", "coin must be a non-empty string")
        if not isinstance(strict_orig_sz, bool):
            raise BookError("INVALID_OPTION", "strict_orig_sz must be a boolean")
        self.coin = coin
        self.strict_orig_sz = strict_orig_sz
        self.orders: dict[str, Order] = {}

    def copy(self) -> OrderBook:
        other = OrderBook(self.coin, strict_orig_sz=self.strict_orig_sz)
        other.orders = dict(self.orders)  # Order values are immutable.
        return other

    def _insert_snapshot_order(self, raw: Any, inferred_side: str | None = None) -> None:
        user, record = _book_order_record(raw)
        if "coin" in record and record["coin"] != self.coin:
            raise BookError("SNAPSHOT_COIN_MISMATCH", f"Order coin {record['coin']!r} != {self.coin!r}")
        if _book_is_trigger(record):
            # Untriggered conditional orders are not executable resting liquidity.
            return
        oid = _book_oid(_book_field(record, "oid", "order_id"))
        raw_side = record.get("side", inferred_side)
        if raw_side is None:
            raise BookError("MISSING_SIDE", "Snapshot order has no side")
        side = _book_side(raw_side)
        if inferred_side is not None and side != inferred_side:
            raise BookError("SNAPSHOT_SIDE_MISMATCH", f"Order {oid} is in the wrong side array")
        price = _book_decimal(_book_field(record, "limitPx", "px", "price"), "price")
        qty = _book_decimal(_book_field(record, "sz", "qty"), "qty", allow_zero=True)
        if oid in self.orders:
            raise BookError("DUPLICATE_ORDER_ID", f"Snapshot repeats order {oid}")
        self.orders[oid] = Order(oid, side, price, qty, user)

    def apply_diff(self, event: Mapping[str, Any]) -> bool:
        """Apply one native raw-book diff; return False for another coin.

        No state is mutated until this individual event has been validated.
        `update.newSz` is the absolute remaining quantity, never a delta.
        `new.insertBefore`, if present, does not change the reconstructed
        price/size/id projection.  Exact same-price queue priority is not claimed.
        """
        if not isinstance(event, Mapping):
            raise BookError("DIFF_SCHEMA", "A raw book diff must be an object")
        if "coin" not in event:
            raise BookError("DIFF_COIN_MISSING", "Raw book diff has no coin")
        if event["coin"] != self.coin:
            return False
        oid = _book_oid(_book_field(event, "oid"))
        raw = _book_field(event, "raw_book_diff")
        if raw == "remove":
            operation, detail = "remove", {}
        elif isinstance(raw, Mapping) and len(raw) == 1:
            operation, detail = next(iter(raw.items()))
            if operation not in {"new", "update"} or not isinstance(detail, Mapping):
                raise BookError("DIFF_SCHEMA", f"Unsupported raw_book_diff: {raw!r}")
        else:
            raise BookError("DIFF_SCHEMA", f"Unsupported raw_book_diff: {raw!r}")

        if operation == "new":
            if oid in self.orders:
                raise BookError("DUPLICATE_ORDER_ID", f"New diff repeats existing order {oid}")
            if "side" not in event:
                raise BookError("MISSING_SIDE", f"New order {oid} has no side; side cannot be inferred safely")
            side = _book_side(event["side"])
            price = _book_decimal(_book_field(event, "px"), "price")
            qty = _book_decimal(_book_field(detail, "sz"), "qty", allow_zero=True)
            user = event.get("user", "") or ""
            if not isinstance(user, str):
                raise BookError("DIFF_SCHEMA", "Diff user must be a string")
            self.orders[oid] = Order(oid, side, price, qty, user)
            return True

        if oid not in self.orders:
            raise BookError("UNKNOWN_ORDER_ID", f"Cannot {operation} absent order {oid}")
        old = self.orders[oid]
        if "side" in event and _book_side(event["side"]) != old.side:
            raise BookError("DIFF_SIDE_MISMATCH", f"Order {oid} side differs from the reconstructed book")
        if "px" in event and _book_decimal(event["px"], "price") != old.price:
            raise BookError("DIFF_PRICE_MISMATCH", f"Order {oid} price differs from the reconstructed book")
        if operation == "remove":
            del self.orders[oid]
            return True

        if self.strict_orig_sz and "origSz" in detail:
            original = _book_decimal(detail["origSz"], "origSz", allow_zero=True)
            if original != old.qty:
                raise BookError(
                    "DIFF_SIZE_MISMATCH",
                    f"Order {oid}: origSz={book_decimal_text(original)} but replay has {book_decimal_text(old.qty)}",
                )
        qty = _book_decimal(_book_field(detail, "newSz"), "newSz", allow_zero=True)
        self.orders[oid] = Order(old.oid, old.side, old.price, qty, old.user)
        return True

    def side_orders(self, side: str) -> list[Order]:
        normalized_side = _book_side(side)
        values = sorted(
            (order for order in self.orders.values() if order.side == normalized_side and order.qty > 0),
            key=lambda order: order.oid,
        )
        # Stable second sort keeps deterministic order-id order for equal prices.
        # Do not negate Decimal prices: unary arithmetic can round under a context.
        return sorted(values, key=lambda order: order.price, reverse=(normalized_side == "bid"))

    def normalized(self) -> dict[str, dict[str, str]]:
        return {
            oid: {
                "side": self.orders[oid].side,
                "price": book_decimal_text(self.orders[oid].price),
                "qty": book_decimal_text(self.orders[oid].qty),
            }
            for oid in sorted(self.orders)
        }


def select_snapshot(refs: Iterable[SnapshotRef], tn_ms: int, target_block: int) -> SnapshotRef:
    """Choose the nearest declared time strictly before Tn, also before B.

    No file is opened here.  A missing chosen snapshot fails locally at load time;
    it is never replaced with a future/expected snapshot or used to bridge a
    missing diff interval.  Equal-time candidates use higher height as a tie-break.
    """
    eligible: list[SnapshotRef] = []
    for ref in refs:
        if ref.time_ms < tn_ms and ref.block_number < target_block:
            eligible.append(ref)
    if not eligible:
        raise BookError(
            "NO_PRIOR_SNAPSHOT",
            f"No snapshot has time < {tn_ms} and completed block < {target_block}",
        )
    return max(eligible, key=lambda ref: (ref.time_ms, ref.block_number))


def _book_json_constant(value: str) -> None:
    raise BookError("SNAPSHOT_JSON", f"Non-standard JSON number {value!r}")


def _book_read_json(path: Path) -> Any:
    suffix = path.suffix.lower()
    opener = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open, ".lzma": lzma.open}.get(suffix, open)
    try:
        with opener(path, "rt", encoding="utf-8-sig") as handle:
            return json.load(handle, parse_float=Decimal, parse_constant=_book_json_constant)
    except FileNotFoundError as exc:
        raise BookError("SNAPSHOT_MISSING", "Snapshot file is missing", str(path)) from exc
    except BookError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise BookError("SNAPSHOT_READ_ERROR", str(exc), str(path)) from exc


def _book_payload(data: Any, ref: SnapshotRef) -> Any:
    """Unwrap documented native [height, books] and explicit normalized wrappers."""
    if isinstance(data, Mapping):
        for name in ("block_number", "height", "block"):
            if name in data:
                declared = _book_int(data[name], name)
                if declared != ref.block_number:
                    raise BookError(
                        "SNAPSHOT_HEIGHT_MISMATCH",
                        f"Embedded height {declared} != declared completed height {ref.block_number}",
                        ref.path,
                    )
        if "state_point" in data and data["state_point"] != "block_end":
            raise BookError("SNAPSHOT_STATE_POINT", "Snapshot must represent a completed block", ref.path)
        if "books" in data:
            return data["books"]
        if "orders" in data:
            return data  # Explicit flat adapter, parsed below.
        raise BookError("SNAPSHOT_SCHEMA", "Unknown snapshot object: expected books or orders", ref.path)
    if (
        isinstance(data, list)
        and len(data) == 2
        and isinstance(data[0], (int, Decimal))
        and not isinstance(data[0], bool)
        and isinstance(data[1], list)
    ):
        height = _book_int(data[0], "height")
        if height != ref.block_number:
            raise BookError(
                "SNAPSHOT_HEIGHT_MISMATCH",
                f"Embedded height {height} != declared completed height {ref.block_number}",
                ref.path,
            )
        return data[1]
    return data


def _book_from_payload(
    data: Any, ref: SnapshotRef, coin: str, strict_orig_sz: bool = False
) -> OrderBook:
    payload = _book_payload(data, ref)
    book = OrderBook(coin, strict_orig_sz=strict_orig_sz)
    if isinstance(payload, Mapping) and "orders" in payload:
        orders = payload["orders"]
        if not isinstance(orders, list):
            raise BookError("SNAPSHOT_SCHEMA", "orders must be an array", ref.path)
        declared_coin = payload.get("coin")
        if declared_coin is not None and not isinstance(declared_coin, str):
            raise BookError("SNAPSHOT_SCHEMA", "Snapshot coin must be a string", ref.path)
        if declared_coin is not None and declared_coin != coin:
            # A single-coin adapter is not evidence about any different coin.
            raise BookError("SNAPSHOT_COIN_MISSING", f"Single-coin snapshot covers {declared_coin!r}, not {coin!r}", ref.path)
        for raw in orders:
            _, record = _book_order_record(raw)
            record_coin = record.get("coin", declared_coin)
            if record_coin is None:
                raise BookError("SNAPSHOT_SCHEMA", "Flat snapshot order must identify its coin", ref.path)
            if record_coin != coin:
                continue
            book._insert_snapshot_order(raw)
        return book
    if not isinstance(payload, list):
        raise BookError("SNAPSHOT_SCHEMA", "Native snapshot books must be an array", ref.path)
    found = False
    for entry in payload:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2 or not isinstance(entry[0], str):
            raise BookError("SNAPSHOT_SCHEMA", "Expected [coin, [bids, asks]]", ref.path)
        if entry[0] != coin:
            continue
        if found:
            raise BookError("SNAPSHOT_DUPLICATE_COIN", f"Snapshot repeats {coin}", ref.path)
        found = True
        sides = entry[1]
        if not isinstance(sides, (list, tuple)) or len(sides) != 2:
            raise BookError("SNAPSHOT_SCHEMA", "Coin book must contain bids and asks arrays", ref.path)
        for side, values in zip(("bid", "ask"), sides):
            if not isinstance(values, list):
                raise BookError("SNAPSHOT_SCHEMA", "Each side must be an order array", ref.path)
            for raw in values:
                book._insert_snapshot_order(raw, side)
    # The native format is a complete books export.  An omitted coin has no
    # resting orders in this projection; a flat single-coin adapter differs.
    return book


def load_snapshot(ref: SnapshotRef, coin: str, strict_orig_sz: bool = False, *, cache=None, log=None) -> OrderBook:
    """Shared native RMP or exact JSON L4 adapter, used by all execution paths."""
    from rmp_l4 import is_rmp, load_book
    try:
        if is_rmp(ref.path):
            book = load_book(ref, coin, cache, log)
            book.strict_orig_sz = strict_orig_sz
            return book
        return _book_from_payload(_book_read_json(Path(ref.path)), ref, coin, strict_orig_sz)
    except BookError:
        raise
    except (OSError, ValueError, KeyError, EOFError) as exc:
        raise BookError('SNAPSHOT_READ_ERROR', str(exc), ref.path) from exc


def _book_diff_source(block: Mapping[str, Any]) -> str:
    """Preserve file/line metadata supplied by either reader or test harness."""
    source = ""
    for name in ("source_file", "_source_file", "source", "__source__"):
        value = block.get(name)
        if value is not None and str(value):
            source = str(value)
            break
    for name in ("source_line", "_source_line", "line_number", "_line_number", "line", "__line__"):
        value = block.get(name)
        if value is not None and str(value):
            suffix = f"line={value}"
            return f"{source}:{suffix}" if source else suffix
    return source

def replay_blocks(
    book: OrderBook,
    blocks: Iterable[Mapping[str, Any]],
    snapshot_block: int,
    target_block: int,
    dense: bool = True,
    progress: Callable[[int, int], None] | None = None,
) -> OrderBook:
    """Forward-replay complete block events up to and including target_block.

    dense=True requires every height, including explicit empty events blocks.
    dense=False permits omitted unchanged blocks ONLY when the caller has already
    established that all required source files/coverage intervals were consumed.
    Sparse row absence alone never proves whether data is complete.

    The caller must discard a book after a replay error.  Keeping a partial
    instance after a missing/invalid diff would silently corrupt later results.
    progress, when supplied, is called as progress(block_number, raw_event_count).
    """
    initial = _book_int(snapshot_block, "snapshot_block")
    target = _book_int(target_block, "target_block")
    if target < initial:
        raise BookError("SNAPSHOT_AFTER_TARGET", "Forward replay cannot start after its target")
    if target == initial:
        return book
    last = initial
    last_source = ""
    for block in blocks:
        if not isinstance(block, Mapping):
            raise BookError("DIFF_BLOCK_SCHEMA", "A diff block must be an object", last_source)
        source = _book_diff_source(block)
        if source:
            last_source = source
        try:
            number = _book_int(_book_field(block, "block_number"), "block_number")
        except BookError as exc:
            raise BookError(exc.code, exc.message, source) from exc
        if number <= initial:
            continue  # Snapshot already contains these changes.
        if number > target:
            break  # No future block is applied, even if provided in the iterator.
        if number <= last:
            raise BookError("DIFF_BLOCK_ORDER", f"Block {number} follows already applied block {last}", source)
        if dense and number != last + 1:
            raise BookError("MISSING_DIFF_BLOCK", f"Expected block {last + 1}, encountered {number}", source)
        events = block.get("events")
        if not isinstance(events, list):
            raise BookError("DIFF_BLOCK_SCHEMA", f"Block {number} events must be an array", source)
        for index, event in enumerate(events):
            try:
                book.apply_diff(event)
            except BookError as exc:
                context = f"{source}:block={number}:event={index}" if source else f"block={number}:event={index}"
                raise BookError(exc.code, exc.message, context) from exc
        last = number
        if progress is not None:
            progress(number, len(events))
        if number == target:
            return book
    if dense and last != target:
        raise BookError(
            "MISSING_DIFF_BLOCK",
            f"Replay ended at {last}; required complete target is {target}",
            last_source,
        )
    return book

def parse_time(value: Any) -> int:
    """Integer numbers are epoch milliseconds; ISO strings require an explicit zone.

    Native block_time strings without a zone are documented as UTC by this adapter.
    Decimal fractions beyond milliseconds are truncated consistently.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError("time must be ISO8601 or epoch milliseconds")
    if isinstance(value, (int, Decimal)):
        return int(value)
    if isinstance(value, float):
        raise ValueError("use integer milliseconds, not binary floating timestamps")
    s = str(value).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    dt = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = dt.astimezone(timezone.utc) - epoch
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def time_text(ms: int) -> str:
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=ms)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def dec_text(n: Any) -> str:
    d = n if isinstance(n, Decimal) else Decimal(str(n))
    if not d.is_finite():
        raise ValueError("non-finite decimal")
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def positive_decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise ValueError(f"{name} must be a decimal string/integer")
    d = Decimal(str(value))
    if not d.is_finite() or d <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return d


def json_text(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=lambda o: dec_text(o) if isinstance(o, Decimal) else str(o))


class InputProblem(Exception):
    def __init__(self, code: str, message: str, source: str = ""):
        self.code, self.source = code, source
        super().__init__(message)


@contextlib.contextmanager
def _open_zip_json(path):
    try:
        with zipfile.ZipFile(path) as z:
            members=[m for m in z.infolist() if not m.is_dir()]
            if len(members)!=1:raise OSError("JSONL ZIP must contain exactly one member")
            with z.open(members[0]) as binary:
                with io.TextIOWrapper(binary,encoding="utf-8-sig") as text:
                    yield text
    except zipfile.BadZipFile as exc:raise OSError(str(exc)) from exc


def open_json_text(path: str):
    """Open one requested shard now; never probe files before processing them."""
    p = str(path)
    if p.lower().endswith(".zip"):
        return _open_zip_json(p)
    if p.endswith(".gz"):
        return gzip.open(p, "rt", encoding="utf-8-sig")
    if p.endswith(".bz2"):
        return bz2.open(p, "rt", encoding="utf-8-sig")
    if p.endswith((".xz", ".lzma")):
        return lzma.open(p, "rt", encoding="utf-8-sig")
    if p.endswith(".lz4"):
        try:
            import lz4.frame
        except ImportError as e:
            raise InputProblem("MISSING_CODEC", "Install lz4 to read .lz4 shards", p) from e
        return lz4.frame.open(p, "rt", encoding="utf-8-sig")
    if p.endswith((".zst", ".zstd")):
        try:
            import zstandard
        except ImportError as e:
            raise InputProblem("MISSING_CODEC", "Install zstandard to read .zst shards", p) from e
        return io.TextIOWrapper(zstandard.open(p, "rb"), encoding="utf-8-sig")
    return open(p, "rt", encoding="utf-8-sig")


class RunLog:
    def __init__(self, path: Path, console: bool = True, append: bool = False):
        self.logger = logging.Logger(f"hlp-{id(self)}", logging.INFO)
        fmt = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S")
        fmt.converter = _clock.gmtime
        fh = logging.FileHandler(path, encoding="utf-8", mode="a" if append else "w")
        fh.setFormatter(fmt)
        self.logger.addHandler(fh)
        if console:
            sh = logging.StreamHandler(sys.stderr)
            sh.setFormatter(fmt)
            self.logger.addHandler(sh)
        self.started = _clock.monotonic()
        self.last_heartbeat = self.started
        self.lines = 0
        self.last_event = "initializing"
        self.last_event_time = self.started
        self.stop_heartbeat = threading.Event()
        self.heartbeat_thread = threading.Thread(target=self._idle_heartbeat, daemon=True)
        self.heartbeat_thread.start()

    def _idle_heartbeat(self):
        # Converting a large .rmp can take minutes without producing input rows.
        # Report that the current stage is still running, without guessing percent.
        while not self.stop_heartbeat.wait(10):
            now = _clock.monotonic()
            if now - self.last_event_time >= 10:
                self.logger.info("HEARTBEAT %s", json_text({"last_event":self.last_event,"elapsed_seconds":round(now-self.started,2)}))

    def info(self, event: str, **data):
        self.last_event, self.last_event_time = event, _clock.monotonic()
        self.logger.info("%s %s", event, json_text(data))

    def warning(self, event: str, **data):
        self.last_event, self.last_event_time = event, _clock.monotonic()
        self.logger.warning("%s %s", event, json_text(data))

    def heartbeat(self, phase: str, source: str, count: int):
        now = _clock.monotonic()
        if now - self.last_heartbeat >= 2:
            self.info("PROGRESS", phase=phase, source=source, count=count, elapsed_seconds=round(now-self.started, 2))
            self.last_heartbeat = now

    def close(self):
        self.stop_heartbeat.set()
        self.heartbeat_thread.join(timeout=1)
        for h in list(self.logger.handlers):
            h.flush()
            h.close()
            self.logger.removeHandler(h)


@dataclass
class FillRecord:
    event_id: str
    coin: str
    direction: str
    price: Decimal
    qty: Decimal
    time_ms: int
    block_number: int
    source_file: str
    source_line: int


@dataclass
class FillGap:
    source_file: str
    reason: str
    start_ms: int | None = None
    end_ms: int | None = None


@dataclass
class Episode:
    coin: str
    direction: str
    fills: list[FillRecord] = field(default_factory=list)
    episode_id: str = ""
    partial_reasons: list[str] = field(default_factory=list)
    split_before: str = ""
    split_after: str = ""
    status: str = "pending"
    skip_reason: str = ""
    error_file: str = ""
    snapshot: Any = None
    book_result: Any = None
    la_result: Any = None

    @property
    def first(self):
        return self.fills[0]

    @property
    def last(self):
        return self.fills[-1]

    @property
    def total_qty(self):
        with localcontext() as ctx:
            ctx.prec = CALC_PRECISION
            return sum((f.qty for f in self.fills), Decimal(0))

    @property
    def entry_vwap(self):
        with localcontext() as ctx:
            ctx.prec = CALC_PRECISION
            return sum((f.price*f.qty for f in self.fills), Decimal(0))/self.total_qty


def _parse_fill(event: Any, header: dict, fmt: str, owners: set[str], path: str, line: int, index: int, start_ms: int | None = None, end_ms: int | None = None) -> FillRecord | None:
    if fmt == "node":
        if not isinstance(event, list) or len(event) != 2 or not isinstance(event[1], dict):
            raise ValueError("node event must be [owner_address, fill_object]")
        owner, f = str(event[0]).lower(), event[1]
        if owner not in owners:
            return None
        liq = f.get("liquidation")
        if not isinstance(liq, dict) or liq.get("method") != "backstop":
            return None
        if str(liq.get("liquidatedUser", "")).lower() == owner:
            return None
        tm = parse_time(f.get("time", header.get("block_time")))
        if (start_ms is not None and tm < start_ms) or (end_ms is not None and tm > end_ms):
            return None
        side = f.get("side")
        if side not in ("A", "B"):
            raise ValueError("HLP fill.side must be B or A")
        direction = "long" if side == "B" else "short"
        price, qty = positive_decimal(f.get("px"), "px"), positive_decimal(f.get("sz"), "sz")
        coin = str(f.get("coin", ""))
        tid = str(f.get("tid", f.get("hash", "")))
        eid = f"{owner}:{tid}:{Path(path).name}:{line}:{index}"
    else:
        if not isinstance(event, dict):
            raise ValueError("normalized event must be an object")
        f = event
        tm = parse_time(f.get("time", header.get("block_time")))
        if (start_ms is not None and tm < start_ms) or (end_ms is not None and tm > end_ms):
            return None
        direction = f.get("direction")
        if direction not in ("long", "short"):
            raise ValueError("normalized direction must be long or short")
        price, qty = positive_decimal(f.get("price"), "price"), positive_decimal(f.get("qty"), "qty")
        coin = str(f.get("coin", ""))
        eid = str(f.get("event_id", f"{Path(path).name}:{line}:{index}"))
    if not coin:
        raise ValueError("coin is required")
    b = f.get("block_number", header.get("block_number"))
    if isinstance(b, bool) or not isinstance(b, int) or b < 0:
        raise ValueError("integer block_number is required")
    if "block_number" in header and b != header["block_number"]:
        raise ValueError("event block_number disagrees with batch")
    return FillRecord(eid, coin, direction, price, qty, tm, b, path, line)


def build_episodes(records: Iterable[FillRecord | FillGap], d1: int, dn: int) -> list[Episode]:
    active: dict[tuple[str,str],Episode] = {}
    all_eps: list[Episode] = []
    first_segment = True
    segment_reason = ""
    seen_in_segment: set[tuple[str,str]] = set()
    for item in records:
        if isinstance(item, FillGap):
            why = f"{item.reason}: {item.source_file}"
            for ep in active.values():
                ep.split_after = why
                all_eps.append(ep)
            active.clear()
            first_segment = False
            segment_reason = why
            seen_in_segment.clear()
            continue
        f = item
        key = (f.coin, f.direction)
        ep = active.get(key)
        if ep is not None and f.time_ms - ep.last.time_ms > GAP_MS:
            all_eps.append(ep)
            del active[key]
            ep = None
        if ep is None:
            ep = Episode(f.coin, f.direction)
            if first_segment and f.time_ms - d1 < GAP_MS:
                ep.partial_reasons.append("data_left_boundary")
            if key not in seen_in_segment and segment_reason:
                ep.split_before = segment_reason
            seen_in_segment.add(key)
            active[key] = ep
        ep.fills.append(f)
    for ep in active.values():
        if dn - ep.last.time_ms <= GAP_MS:
            ep.partial_reasons.append("data_right_boundary")
        all_eps.append(ep)
    core_start, core_end = d1+BUFFER_MS, dn-BUFFER_MS
    kept = [e for e in all_eps if any(core_start <= f.time_ms <= core_end for f in e.fills)]
    kept.sort(key=lambda e:(e.first.time_ms,e.first.block_number,e.coin,e.direction))
    for i, ep in enumerate(kept, 1):
        ep.episode_id = f"EP{i:06d}"
    return kept


def simulate_close(book, direction: str, qty: Decimal, entry_vwap: Decimal) -> dict:
    if direction not in ("long", "short") or qty <= 0:
        raise ValueError("invalid simulation direction/quantity")
    side = "bid" if direction == "long" else "ask"
    remaining, amount, filled = qty, Decimal(0), Decimal(0)
    trace = []
    with localcontext() as ctx:
        ctx.prec = CALC_PRECISION
        for order in book.side_orders(side):
            if remaining <= 0:
                break
            take = min(remaining, order.qty)
            if take <= 0:
                continue
            trace.append({"oid":order.oid,"price":dec_text(order.price),"available_qty":dec_text(order.qty),"filled_qty":dec_text(take)})
            amount += order.price*take
            filled += take
            remaining -= take
        exit_vwap = amount/filled if filled else None
        la = (amount-entry_vwap*filled) if direction == "long" else (entry_vwap*filled-amount)
    return {"filled_qty":filled,"unfilled_qty":remaining,"exit_vwap":exit_vwap,"la":la,"depth_insufficient":remaining>0,"fill_trace":trace}

if __name__ == '__main__':
    print('Use python run_hlp.py; this file is a shared engine, not a screening CLI.', file=sys.stderr)
    raise SystemExit(2)
