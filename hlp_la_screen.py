#!/usr/bin/env python3
"""HLP episode screening and exact per-order block-end order-book reconstruction.

交付：筛选器、共享 rmp_l4.py、按需提取脚本及原/新增测试；Python 3.10+。
规则只采用用户2026-10-23本轮确认，完整规则和Codex提示词内嵌于下方。

使用：
  python hlp_la_screen.py --print-framework
  python hlp_la_screen.py --print-codex-prompt
  python hlp_la_screen.py --print-input-contract
  python hlp_la_screen.py --example-config > config.json
  # 编辑 config.json 的范围、已验证HLP地址、快照元数据及本地输入路径
  python hlp_la_screen.py --config config.json --output result
  python test_order_book_replay.py --verbose
  python test_order_book_replay.py --demo
  python test_order_book_replay.py --help

原生.rmp/.rmp.zip按需由rmp_l4.py一次扫描提取；需要msgpack。JSON L4不需要。
读取.gz/.bz2/.xz使用标准库；.lz4/.zst分别需lz4/zstandard。
任何数据均不会自动联网下载。输入是每行一个完整block的JSONL，
不能把stream-with-block-info逐事件行不经合批当作block-end输入。

快照高度由配置显式指定为已完成块末；时间为真实快照时点。不得凭文件名
猜高度，不自动尝试height±1。只有time<Tn且height<目标B的快照可作起点。
后快照只能在test脚本回放完成后读取作为答案。真实ABCI验收尚需用户数据。

一致性范围：指定币种活跃订单的oid/side/price/remaining_qty；同价逐单保留。
不重建raw diff没有的信息（整个账户状态、创建时间、条件单/同价队列元数据）。
LA是完整episode累计接管量的静态深度模拟，不是实际HLP账户余额与已实现收益。

数据流逐行读，不做输入文件预检；只在读到缺口时处理。输出冲突检查只保护
已有结果文件。核心输出为csv1.csv、csv2.csv、csv3.csv和实时刷新的run.log；提取缓存另存。
提取按源身份/币种/转换版本持久缓存；回放按币种/前快照缓存成功块末。

依据：
https://github.com/hyperliquid-dex/node
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/nodes/l1-data-schemas
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions
https://github.com/hyperliquid-dex/order_book_server
"""

from __future__ import annotations


import bz2
import gzip
import json
import lzma
import subprocess
import os
import contextlib
import zipfile
import hashlib
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


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


def load_snapshot(
    ref: SnapshotRef,
    coin: str,
    hl_node: str | None = None,
    chain: str = "Mainnet",
    strict_orig_sz: bool = False,
) -> OrderBook:
    """Load native/normalized L4 JSON, or convert an ABCI .rmp on demand.

    Missing/invalid data is detected when this function is used, never through
    an all-dataset preflight.  A .rmp requires a compatible user-provided hl-node.
    The converter is invoked without a shell; no binary is fetched or installed.
    strict_orig_sz is optional because historical origSz semantics are not
    assumed; update.newSz remains authoritative in the default mode.
    """
    path = Path(ref.path)
    try:
        if path.suffix.lower() != ".rmp":
            return _book_from_payload(_book_read_json(path), ref, coin, strict_orig_sz)
        if not hl_node:
            raise BookError(
                "ABCI_CONVERTER_REQUIRED",
                "A native .rmp requires hl_node for compute-l4-snapshots; supply an exported L4 JSON otherwise",
                ref.path,
            )
        # Opening on demand produces a precise missing/unreadable-source error
        # before invoking a converter; it is not a dataset preflight.
        with path.open("rb"):
            pass
        with tempfile.TemporaryDirectory(prefix="hlp_l4_snapshot_") as temp_dir:
            output = Path(temp_dir) / "snapshot.json"
            command = [str(hl_node), "--chain", str(chain), "compute-l4-snapshots", str(path), str(output)]
            try:
                result = subprocess.run(command, capture_output=True, text=True, errors="replace", check=False)
            except OSError as exc:
                raise BookError("SNAPSHOT_CONVERSION_FAILED", str(exc), ref.path) from exc
            if result.returncode != 0:
                details = (result.stderr or result.stdout or "converter returned no diagnostic").strip()[-4000:]
                raise BookError("SNAPSHOT_CONVERSION_FAILED", f"Exit {result.returncode}: {details}", ref.path)
            return _book_from_payload(_book_read_json(output), ref, coin, strict_orig_sz)
    except FileNotFoundError as exc:
        raise BookError("SNAPSHOT_MISSING", "Snapshot file is missing", ref.path) from exc
    except OSError as exc:
        raise BookError("SNAPSHOT_READ_ERROR", str(exc), ref.path) from exc
    except BookError as exc:
        if exc.source:
            raise
        raise BookError(exc.code, exc.message, ref.path) from exc


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


import argparse
import csv
import gzip
import bz2
import lzma
import io
import json
import logging
import sys
import threading
import time as _clock
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Iterable, Iterator

VERSION = "1.2.0-universal-ubuntu-20261024"
GAP_MS = 30 * 60 * 1000
BUFFER_MS = 60 * 60 * 1000
CALC_PRECISION = 60

FRAMEWORK = r'''
通用入口补充（最新用户要求，优先于旧条款的手动清单/不预探测约定）：
run_hlp.py 顶部集中配置 UTC 包含首尾的日期及 Ubuntu 数据/输出路径。
input_discovery.py 自动探测小时文件首块和受限 RMP context，先分组生成 csv1，
再发现实际 episode 所需快照和 diff；分片边界只用于路由，密集回放仍逐块证明完整。
选中快照损坏或候选歧义时明确失败，不静默用旧/未来快照。核心计算和比较保持原逻辑。
本轮优化补充（优先于下面旧框架中的转换/输出说明）：
先分组并写 csv1 初稿，按 episode 去重选最近前快照；按源快照汇总币种。
rmp_l4.py 一次扫描所需 RMP，按自身元数据定位 perp/HIP3/spot 的 asset 和精度。
提取缓存位于 rmp_cache_dir，键含源身份/币种/转换版本；成功标记最后原子提交。
单币种 JSON 只由所选原始快照和币种索引，不参与普通快照选择。
回放缓存按币种和源快照保存成功块末，目标升序处理；缺 diff 不推进缓存。
每个 episode 的 csv2/csv3 写出刷新后清空 book_result/la_result，保留 manager 缓存。
全部计算后将 csv1 初稿原子替换为最终状态；严格比较无容差，模拟不修改历史簿。
HLP 接管筛选执行框架（2026-10-23 本轮已确认口径）
================================================
1. 输出仅 csv1.csv、csv2.csv、csv3.csv、run.log。程序运行前不扫描、
   校验或探测输入文件。配置结构检查不读取数据；数据错误在实际读取时处理。
2. 以 HLP 接管后的仓位方向分组：每个连续 fills 数据段内，按 coin +
   direction 独立聚合。其他币种、相反方向插入不截断。相邻 <=30分钟
   继续同 episode，>30分钟新建。一个episode总时长可以超过30分钟。
3. D1/Dn 是明确给定的数据覆盖边界（UTC或带时区ISO8601）。本版采用
   闭区间 [D1,Dn]；正式区间为 [D1+1h,Dn-1h]，两端均包含。
   只要episode至少一笔落入正式区间，输出该episode的全部已读取接管，
   包括缓冲区内部分。只在缓冲区的episode不输出。
4. 外侧数据边界尚不足以确认episode完整时，用已读取部分继续计算，
   partial_episode=true，并标注仅基于部分episode。不猜测缺失数量。
   判定：数据开始处不足30分钟的同组首笔，或数据结束处不足30分钟的
   同组末笔，可能延伸到范围外。此标记不使episode失效。
5. fills文件缺失、声明覆盖段之间有空洞，或运行中某行无法读取时，
   在此处强制分段。关闭前段当前episode；恢复后从新episode开始。
   无论跨缺口两笔相隔多久，都不得跨缺口合并。不推断未知接管，不
   因缺口与附近记录的距离使两侧episode失效。分段原因与缺失文件
   写日志及备注。fills缺口不等于diff缺口。
6. 每个episode目标是最后接管Tn所在block B的块末订单簿，包含B全部
   相关diff。快照元数据必须声明其代表的已完成block与时间。
   从配置快照清单中选time<Tn且snapshot_block<B的最近前置快照；不可用之后的
   快照回填、反推或替代。最近前置快照读不到也不会静默改用另一个。
   重建路径缺diff只跳过依赖该路径的episode，不用B-1替代。
   对更晚的episode，若另一快照本身已早于其Tn，则可正常使用它。
7. 原始订单按oid保留。同价100x2、100x3是两条订单。csv2一个episode
   一行，orders_json保存整侧活跃订单。多头取bid降序；空头取ask升序。
   未触发条件单不算可成交深度。队列优先顺序不在本版一致性承诺内。
8. csv1每笔接管一行，同block多笔不合并，重复附带episode汇总指标。
   node模式只接受配置HLP账户拥有、liquidation.method=backstop的fills；
   排除owner==liquidatedUser的被接管方记录。方向用HLP这侧B/A映射。
   不硬编码HLP地址，不用dir显示文案识别，不对未知结构猜字段。
9. entry_vwap=sum(price*qty)/sum(qty)，qty均为正数。沿正确单侧逐价
   成交，最后一档允许部分成交。long LA=proceeds-entry_vwap*filled_qty；
   short LA=entry_vwap*filled_qty-cost。深度不足仅算已成交量；剩余量
   保留。不是HLP真实资金账户总盈亏，不含手续费/资金费或未成交估值。
10. 输出约定（第9问未逐项回复时采用的可见默认）：有效空簿filled=0、
    exit_vwap为空、LA=0、depth_insufficient=true；无法重建则LA为空、
    status=skipped。已识别episode在三个CSV都留状态行，不伪造订单。
11. diff覆盖：dense模式要求每个高度有块记录，空events也有效；file
    模式允许无变化高度不出现，但必须有明确、连续的分片覆盖范围。
    缺文件、坏行、显式gap均不能被file模式忽略。没有高度行不自动
    证明丢数据；没有覆盖声明也不自动证明数据完整。
12. 原生ABCI .rmp按本轮按需规则提取；同源本次扫描一次，仅缓存所需币种。
    不猜.rmp内部结构，不从文件名猜高度，不自动试S±1挑能通过的偏移。
    真实验收：只用前state和diff重建，之后才读取后state作为独立答案；
    精确比较指定coin双侧oid/side/price/remaining_qty，不声称重建了
    全部ABCI账户状态或raw diff未包含的订单元数据。
13. 订单原值用Decimal保存；数量汇总、VWAP和LA运算使用60位有效精度。
    update只把newSz赋为新的绝对剩余量。默认不假定历史origSz字段等于
    上一步剩余量；确认该数据契约后才可启用strict_orig_sz=true额外检查。
'''

INPUT_CONTRACT = r'''
输入契约与运行方式
================
1. 配置是UTF-8 JSON；相对数据路径按配置文件所在目录解析。所有价格和数量
   推荐用十进制字符串。数值时间用epoch毫秒，ISO8601无时区时按UTC处理。
   快照清单由用户或上游元数据提供：列出区间内全部可选快照及此前最近快照。
   time必须表示快照实际状态时点，不能用下载时间或文件修改时间替代。
2. 筛选器的原生.rmp经共享rmp_l4.py按需提取；也接受已导出的L4 JSON，例如：
   [100, [["BTC", [[{"oid":1,"side":"B","limitPx":"100","sz":"2"}],
                   [{"oid":2,"side":"A","limitPx":"101","sz":"3"}]]]]]
   同时支持裸books数组及includeUsers的[user,order]条目。
3. 原生fills按block合批，每行一个JSON：
   {"block_number":101,"block_time":"2026-10-01T01:00:00Z","events":[
     ["0x_hlp_owner",{"coin":"BTC","side":"B","px":"100","sz":"2",
      "time":1790816400000,"tid":123,
      "liquidation":{"method":"backstop","liquidatedUser":"0x_liquidated"}}]]}
   只识别配置hlp_addresses中的账户；地址需用户确认，示例占位符不能直接使用。
   标准化fills可用format="normalized"，每行一笔或同样的block批次：
   {"event_id":"e1","time":"2026-10-01T01:00:00Z","block_number":101,
    "coin":"BTC","direction":"long","price":"100","qty":"2"}
4. raw diff必须按block合批，同一block内保持原始顺序，每行完整一个block：
   {"block_number":101,"events":[
     {"coin":"BTC","oid":3,"side":"Bid","px":"99",
      "raw_book_diff":{"new":{"sz":"4"}}},
     {"coin":"BTC","oid":1,"raw_book_diff":{"update":{"newSz":"1"}}},
     {"coin":"BTC","oid":2,"raw_book_diff":"remove"}]}
   新增单必须有方向和价格；已有单update/remove可从当前状态查方向/价格。
   old raw diff若新增单无side，需要上游提供真实字段，程序不能猜测。
   插队字段不改变本任务的逐单价格/数量或LA；不承诺还原同价FIFO顺序。
5. fills使用明确的时间分片；diff使用时间分片或block分片，二者不可混用：
   "fills":{"format":"normalized","chunks":[
     {"path":"fills_00.jsonl","start":"2026-10-01T00:00:00Z",
      "end":"2026-10-02T00:00:00Z"}]}
   "diffs":{"coverage_mode":"dense","chunks":[
     {"path":"diffs_101_200.jsonl","start_block":101,"end_block":200}]}
   分片按时间/高度给出顺序；时间范围[start,end)，block范围两端包含。
   Dn恰为最后fills分片end时允许读入该时刻记录；正式区间两端均包含。
   或使用hourly path_template，支持{date}=YYYYMMDD、{iso_date}、
   {hour}=不补零小时、{hour02}=两位小时；程序生成预期路径后按需读取。
6. dense要求每个需要的高度有完整块记录（空events有效）。file模式依赖用户
   声明的连续完整分片，允许无变化block没有记录；不能从稀疏行独立证明完整性。
   显式diff缺口可写{"gap":{"start_block":102,"end_block":105}}。
   缺口与本次(S,B]无交集时忽略；有交集时失败。无范围缺口或无法解析范围的
   坏JSON不能被猜成无关内容。只处理目标B为止，不提前检查B之后的数据。
   显式fills缺口可写{"gap":{"start":"2026-10-01T01:10:00Z",
   "end":"2026-10-01T01:20:00Z"}}，时间区间[start,end)。
   现存fills分片必须覆盖其声明区间；空文件按该段没有成交处理。若完整JSON
   行被静默删除，或文件恰好在完整行处被截短，不能仅凭无接管判断缺失；
   已知缺口必须以缺失分片或显式gap提供。这里不增加运行前数据检查。
7. 每个episode独立模拟，不从后续episode的历史book扣除模拟成交。
   csv2的orders_json是完整相关单侧原始深度，csv3的fill_trace_json记录假想
   成交；同价按oid稳定排序只为结果可重复，不宣称是交易所队列优先级。
8. run.log持续flush：阶段起止、文件读取、fills分段、episode结果和失败来源。
   长转换阶段有运行心跳，不虚报百分比。已存在输出默认不覆盖；--overwrite
   显式覆盖四个文件。run结束summary中skipped>0表示部分episode未算LA。
9. 打印配置：python hlp_la_screen.py --example-config
   运行：python hlp_la_screen.py --config config.json --output result
   核对五个完整案例：python test_order_book_replay.py --demo
   验证真实数据的完整参数见：python test_order_book_replay.py --help
'''

CODEX_PROMPT = r'''
请维护并按本文件 FRAMEWORK 执行 HLP 筛选程序。以用户2026-10-23本轮
明确的规则为唯一依据，不恢复旧3.0.1筛选阈值，不按LA正负筛掉事件。
最终交付仅 hlp_la_screen.py 和 test_order_book_replay.py。

先阅读 FRAMEWORK、输入契约、已有unittest与demo。运行测试，人工核对
至少：交叉币向、30分钟等号、同block多笔、缓冲区纳入、边界partial继续、
fills短缺口强制分段且两边有效、多空LA、部分/零深度、同价不同oid、
new/update/remove、update0后remove、目标块内后续diff、未来快照禁用、
缺diff局部跳过、旧snapshot缺字段、错误预期快照必须失败。

原始输入适配只能由实际样本及官方字段确定。node fills外层账户才是
owner，liquidatedUser是被接管用户；HLP账户列表由用户配置。若历史新单
diff无side，仅snapshot+raw diff无法确定新增单方向，清楚报告而不猜测。
快照高度/时点一律显式；不改偏移以迎合预期答案；后快照只用于比较。
运行前不做文件存在性/数据扫描；运行中缺失按FRAMEWORK局部处理。
不得把fills断点扩散为附近episode失效，也不得静默跳过diff后继续拼簿。
使用Decimal和独立手算期望，避免浮点容差掩盖错单。先对抗审查、修复、
复测具体失败，再报告通过范围。没有真实ABCI输入时只能说模拟测试通过，
不得说真实快照一致性已通过。本优化版另附共享提取后端和测试，原比较逻辑保留。
'''


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


def _shards(section: dict, start_ms: int, end_ms: int) -> list[dict]:
    """Construct expected names from configuration only, without stat/glob/read."""
    if "chunks" in section:
        return [dict(c) for c in section["chunks"]]
    template = section.get("path_template")
    if not template:
        raise ValueError("input section needs chunks or an hourly path_template")
    result = []
    hour = start_ms - start_ms % 3600000
    while hour <= end_ms:
        dt = datetime.fromtimestamp(hour/1000, tz=timezone.utc)
        path = str(template).format(date=dt.strftime("%Y%m%d"), iso_date=dt.strftime("%Y-%m-%d"), hour=dt.hour, hour02=f"{dt.hour:02d}")
        result.append({"path":path,"start":time_text(hour),"end":time_text(hour+3600000)})
        hour += 3600000
    return result


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


def iter_fills(config: dict, log: RunLog) -> Iterator[FillRecord | FillGap]:
    sec = config["fills"]
    fmt = sec.get("format", "node")
    if fmt not in ("node", "normalized"):
        raise ValueError("fills.format must be node or normalized")
    owners = {str(x).lower() for x in sec.get("hlp_addresses", [])}
    if fmt == "node" and not owners:
        raise ValueError("node fills require explicitly configured hlp_addresses")
    start, end = parse_time(config["data_start"]), parse_time(config["data_end"])
    shards = _shards(sec, start, end)
    covered = start
    last_seen = None
    for c in shards:
        path = str(c["path"])
        a, z = parse_time(c["start"]), parse_time(c["end"])
        if z <= a:
            raise ValueError("fills chunk end must follow start")
        if z <= start or a > end:
            continue
        if a < covered and covered != start:
            raise ValueError("fills chunk ranges overlap or are out of order")
        if a > covered:
            gap = FillGap("<unlisted fills coverage>", "MISSING_FILLS_COVERAGE", covered, a)
            log.warning("FILLS_GAP", file=gap.source_file, reason=gap.reason, start=time_text(covered), end=time_text(a))
            yield gap
        covered = max(covered, z)
        log.info("READ_FILLS_FILE", file=path, start=time_text(a), end=time_text(z))
        try:
            with open_json_text(path) as fh:
                for line_num, raw in enumerate(fh, 1):
                    log.lines += 1
                    log.heartbeat("fills", path, log.lines)
                    if not raw.strip():
                        continue
                    try:
                        obj = json.loads(raw, parse_float=Decimal)
                        if isinstance(obj, dict) and "gap" in obj:
                            gap_a, gap_z = a, z
                            gap = obj["gap"]
                            if isinstance(gap, dict) and "start" in gap and "end" in gap:
                                gap_a, gap_z = parse_time(gap["start"]), parse_time(gap["end"])
                                if gap_z <= gap_a:
                                    raise ValueError("declared fills gap end must follow start")
                                if gap_z <= start or gap_a > end:
                                    continue
                            yield FillGap(path, "DECLARED_FILLS_GAP", gap_a, gap_z)
                            log.warning("FILLS_GAP", file=path, line=line_num, reason="DECLARED_FILLS_GAP",start=time_text(gap_a),end=time_text(gap_z))
                            continue
                        if isinstance(obj, dict) and "events" in obj:
                            if not isinstance(obj["events"], list):
                                raise ValueError("events must be a list")
                            header, events = obj, obj["events"]
                        else:
                            header, events = {}, [obj]
                        for idx, event in enumerate(events):
                            try:
                                f = _parse_fill(event, header, fmt, owners, path, line_num, idx, start, end)
                                if f is None or f.time_ms < start or f.time_ms > end:
                                    continue
                                if not (a <= f.time_ms < z or f.time_ms == end == z):
                                    raise ValueError("fill time is outside its declared shard coverage")
                                key = (f.time_ms, f.block_number)
                                if last_seen is not None and key < last_seen:
                                    raise ValueError("fills are not in chronological block order")
                                last_seen = key
                                if config.get("detailed_log",False):
                                    log.info("TAKEOVER_READ",event_id=f.event_id,coin=f.coin,direction=f.direction,
                                             time=time_text(f.time_ms),block=f.block_number,price=dec_text(f.price),qty=dec_text(f.qty),
                                             source_file=f.source_file,source_line=f.source_line,event_index=idx)
                                yield f
                            except (ValueError, TypeError, KeyError, ArithmeticError) as e:
                                log.warning("FILLS_GAP", file=path, line=line_num, event_index=idx, reason="UNREADABLE_FILL", detail=str(e))
                                yield FillGap(path, f"UNREADABLE_FILL line {line_num} event {idx}")
                    except (ValueError, TypeError, KeyError, ArithmeticError) as e:
                        log.warning("FILLS_GAP", file=path, line=line_num, reason="UNREADABLE_FILLS_LINE", detail=str(e))
                        yield FillGap(path, f"UNREADABLE_FILLS_LINE {line_num}")
        except (OSError, EOFError, UnicodeError, InputProblem) as e:
            reason = "MISSING_FILLS_FILE" if isinstance(e, FileNotFoundError) else "UNREADABLE_FILLS_FILE"
            log.warning("FILLS_GAP", file=path, reason=reason, detail=str(e), start=time_text(a), end=time_text(z))
            yield FillGap(path, reason, a, z)
    if covered < end:
        log.warning("FILLS_GAP", reason="MISSING_FILLS_COVERAGE", start=time_text(covered), end=time_text(end))
        yield FillGap("<unlisted fills coverage>", "MISSING_FILLS_COVERAGE", covered, end)


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
        if dn - ep.last.time_ms < GAP_MS:
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


def _diff_chunks(sec: dict, start_block: int, target_block: int, start_ms: int, target_ms: int) -> list[dict]:
    chunks = _shards(sec, start_ms, target_ms)
    chosen = []
    use_blocks = None
    covered_b, covered_t = start_block, start_ms
    for c in chunks:
        by_block = "start_block" in c and "end_block" in c
        if use_blocks is None:
            use_blocks = by_block
        elif by_block != use_blocks:
            raise InputProblem("DIFF_COVERAGE_SCHEMA", "Do not mix block and time coverage in one diff section", str(c.get("path", "")))
        if by_block:
            a, z = int(c["start_block"]), int(c["end_block"])
            if z < a:
                raise InputProblem("DIFF_COVERAGE_SCHEMA", "end_block precedes start_block", str(c["path"]))
            if z < start_block or a > target_block:
                continue
            if a > covered_b:
                raise InputProblem("MISSING_DIFF_COVERAGE", f"missing diff coverage blocks {covered_b}..{a-1}", str(c["path"]))
            covered_b = max(covered_b, z+1)
        else:
            if "start" not in c or "end" not in c:
                raise InputProblem("DIFF_COVERAGE_SCHEMA", "diff shards need explicit block or time coverage", str(c.get("path", "")))
            a, z = parse_time(c["start"]), parse_time(c["end"])
            if z <= a:
                raise InputProblem("DIFF_COVERAGE_SCHEMA", "invalid diff time coverage", str(c["path"]))
            if z <= start_ms or a > target_ms:
                continue
            if a > covered_t:
                raise InputProblem("MISSING_DIFF_COVERAGE", f"missing diff coverage {time_text(covered_t)}..{time_text(a)}", str(c["path"]))
            covered_t = max(covered_t, z)
        chosen.append(c)
    if start_block <= target_block:
        enough = covered_b > target_block if use_blocks else covered_t > target_ms
        if not chosen or not enough:
            raise InputProblem("MISSING_DIFF_COVERAGE", f"no complete declared coverage through block {target_block}", "<diff coverage>")
    return chosen


def iter_diff_blocks(section: dict, start_block: int, target_block: int, start_ms: int, target_ms: int, log: RunLog | None = None):
    """Read only the declared needed shards. File mode relies on their coverage contract.

    Block ranges are inclusive; time shard ranges are half-open. Missing files and
    malformed lines are errors even in sparse/file mode. Input must be batched by
    block (one complete block per JSON line); stream-with-block-info chunks cannot
    masquerade as complete block batches.
    """
    chunks = _diff_chunks(section, start_block, target_block, start_ms, target_ms)
    last_seen = start_block-1
    last_identity = None
    for c in chunks:
        path = str(c["path"])
        if log:
            log.info("READ_DIFF_FILE", file=path, first_needed_block=start_block, target_block=target_block)
        try:
            with open_json_text(path) as fh:
                for line_num, raw in enumerate(fh, 1):
                    if log:
                        log.heartbeat("diffs", path, line_num)
                    if not raw.strip():
                        continue
                    try:
                        obj = json.loads(raw, parse_float=Decimal)
                    except (ValueError, ArithmeticError) as e:
                        raise InputProblem("INVALID_DIFF_JSON", f"line {line_num}: {e}", path) from e
                    if not isinstance(obj, dict):
                        raise InputProblem("INVALID_DIFF_SCHEMA", f"line {line_num}: expected block object", path)
                    if "gap" in obj:
                        gap = obj["gap"]
                        if isinstance(gap, dict):
                            ga, gz = gap.get("start_block"), gap.get("end_block")
                            known_range = all(isinstance(x, int) and not isinstance(x, bool) for x in (ga, gz)) and ga <= gz
                            if known_range and (gz < start_block or ga > target_block):
                                continue
                        raise InputProblem("DECLARED_DIFF_GAP", f"line {line_num}: {gap}", path)
                    b = obj.get("block_number")
                    if isinstance(b, bool) or not isinstance(b, int):
                        raise InputProblem("INVALID_DIFF_SCHEMA", f"line {line_num}: integer block_number required", path)
                    if b < start_block:
                        continue
                    if b > target_block:
                        break
                    if not isinstance(obj.get("events"), list):
                        raise InputProblem("INVALID_DIFF_SCHEMA", f"line {line_num}: events array required", path)
                    if "start_block" in c and not (int(c["start_block"]) <= b <= int(c["end_block"])):
                        raise InputProblem("DIFF_OUTSIDE_COVERAGE", f"block {b} outside declared shard range", path)
                    identity = (b, json_text(obj["events"]))
                    if b == last_seen and identity == last_identity:
                        if log:
                            log.warning("DUPLICATE_DIFF_BLOCK_IGNORED", file=path, block=b)
                        continue
                    if b <= last_seen:
                        raise InputProblem("DIFF_BLOCK_ORDER", f"conflicting/unsorted block {b} after {last_seen}", path)
                    last_seen, last_identity = b, identity
                    obj["_source_file"] = path
                    obj["_source_line"] = line_num
                    yield obj
        except InputProblem:
            raise
        except (OSError, EOFError, UnicodeError) as e:
            code = "MISSING_DIFF_FILE" if isinstance(e, FileNotFoundError) else "UNREADABLE_DIFF_FILE"
            raise InputProblem(code, str(e), path) from e


from rmp_l4 import ExtractionCache, ExtractionError, is_rmp


class ReplayManager:
    def __init__(self, config: dict, log: RunLog):
        self.config, self.log = config, log
        # Only original whole-snapshot references participate in selection.
        self.refs = []
        declared = {}
        for s in config["snapshots"]:
            path = str(s["path"])
            if is_rmp(path): path=os.path.normcase(os.path.realpath(path))
            ref=SnapshotRef(path,int(s["block_number"]),parse_time(s["time"]))
            if path in declared and declared[path]!=(ref.block_number,ref.time_ms):
                raise ValueError("Conflicting metadata for the same source snapshot")
            declared[path]=(ref.block_number,ref.time_ms)
            if ref not in self.refs:self.refs.append(ref)
        self.cache: dict[tuple[str,SnapshotRef],tuple[Any,int,int,Any]] = {}
        self.l4_paths = {}
        self.extraction_errors = {}
        self.planning_errors = {}
        self.remaining = {}
        self.planned_tokens = set()
        self.finished_tokens = set()
        self.extraction = ExtractionCache(config.get("rmp_cache_dir", ".rmp_l4_cache"), log)
        self.stats = {"replay_hits":0,"replay_misses":0,"replay_commits":0}

    def prepare(self, episodes):
        """Complete the union of demanded coins BEFORE opening any RMP source."""
        groups = {}
        for ep in episodes:
            try:
                if getattr(ep, "discovery_error", None):
                    raise ep.discovery_error
                ref = select_snapshot(self.refs, ep.last.time_ms, ep.last.block_number)
                ep.snapshot = ref
                if id(ep) not in self.planned_tokens:
                    self.planned_tokens.add(id(ep))
                    key=(ep.coin,ref)
                    self.remaining[key]=self.remaining.get(key,0)+1
                if is_rmp(ref.path): groups.setdefault(ref,set()).add(ep.coin)
                self.log.info("EPISODE_BOOK_PLAN",episode=ep.episode_id,coin=ep.coin,
                              tn=time_text(ep.last.time_ms),target_block=ep.last.block_number,
                              snapshot=ref.path,snapshot_block=ref.block_number)
            except BookError as exc:self.planning_errors[ep.episode_id]=exc
        for ref, coins in groups.items():
            def validate(path, coin):
                load_snapshot(SnapshotRef(str(path),ref.block_number,ref.time_ms),coin,
                              strict_orig_sz=self.config.get("strict_orig_sz",False))
            try:
                results=self.extraction.ensure(ref,coins,validate)
            except ExtractionError as exc:results={coin:exc for coin in coins}
            for coin,result in results.items():
                key=(ref,coin)
                if isinstance(result,ExtractionError):self.extraction_errors[key]=result
                else:self.l4_paths[key]=result

    def finish_episode(self, ep):
        """Keep a successful book until its final planned consumer is flushed."""
        token=id(ep)
        if token not in self.planned_tokens or token in self.finished_tokens:return
        self.finished_tokens.add(token)
        key=(ep.coin,ep.snapshot)
        self.remaining[key]-=1
        if self.remaining[key]==0:
            self.cache.pop(key,None)
            self.log.info("REPLAY_CACHE_RELEASE",coin=ep.coin,snapshot=ep.snapshot.path,
                          reason="last_planned_episode_flushed")

    def get(self, ep: Episode):
        if ep.episode_id in self.planning_errors: raise self.planning_errors[ep.episode_id]
        ref = select_snapshot(self.refs, ep.last.time_ms, ep.last.block_number)
        ep.snapshot = ref
        selected = ref
        if is_rmp(ref.path):
            if (ref,ep.coin) not in self.l4_paths and (ref,ep.coin) not in self.extraction_errors:
                self.prepare([ep])
            error=self.extraction_errors.get((ref,ep.coin))
            if error:raise BookError(error.code,error.message,error.source)
            try:self.extraction.check_identity(ref)
            except ExtractionError as exc:raise BookError(exc.code,exc.message,exc.source) from exc
            selected=SnapshotRef(str(self.l4_paths[(ref,ep.coin)]),ref.block_number,ref.time_ms)
        target = ep.last.block_number
        cache_key = (ep.coin,ref)
        old = self.cache.get(cache_key)
        if old and old[1] <= target:
            _, previous_b, previous_t, cached = old
            book = cached.copy()
            start_b, start_t = previous_b, previous_t
            self.stats["replay_hits"]+=1
            self.log.info("BOOK_CACHE", episode=ep.episode_id,coin=ep.coin,snapshot=ref.path,from_block=start_b, target_block=target)
        else:
            self.stats["replay_misses"]+=1
            self.log.info("LOAD_SNAPSHOT", episode=ep.episode_id, file=ref.path,l4_file=selected.path, snapshot_block=ref.block_number, target_block=target)
            book = load_snapshot(selected, ep.coin, hl_node=self.config.get("hl_node"), chain=self.config.get("chain", "Mainnet"), strict_orig_sz=self.config.get("strict_orig_sz", False))
            start_b, start_t = ref.block_number, ref.time_ms
        if start_b < target:
            section = self.config.get("_prepared_coin_diffs",{}).get(ep.coin,self.config["diffs"])
            mode = section.get("coverage_mode", "dense")
            if mode not in ("dense", "file"):
                raise InputProblem("DIFF_COVERAGE_MODE", "coverage_mode must be dense or file")
            source = iter_diff_blocks(section, start_b+1, target, start_t, ep.last.time_ms, self.log)
            replay_started=_clock.monotonic()
            applied_events=0
            def progress(block, event_count):
                nonlocal applied_events
                applied_events+=event_count
                self.log.heartbeat("replay", ep.coin, block)
                if self.config.get("detailed_log",False) and (block==target or block==start_b+1 or (block-start_b)%self.config.get("log_block_interval",1000)==0):
                    self.log.info("REPLAY_BLOCK_PROGRESS",episode=ep.episode_id,coin=ep.coin,snapshot=ref.path,
                                  from_block=start_b,through_block=block,target_block=target,
                                  applied_blocks=block-start_b,source_events=applied_events,live_order_records=len(book.orders),
                                  elapsed_seconds=round(_clock.monotonic()-replay_started,3))
            try:
                replay_blocks(book, source, start_b, target, dense=(mode=="dense"), progress=progress)
            except BookError as e:
                if e.source:
                    raise
                # An empty dense file has no block carrying a source annotation.
                # Keep its diff path in the error, instead of blaming the snapshot.
                chunks = _diff_chunks(section, start_b+1, target, start_t, ep.last.time_ms)
                paths = "; ".join(str(c["path"]) for c in chunks)
                raise BookError(e.code, e.message, paths or "<diff stream>") from e
        # Only a complete successful target advances the cache. Failed working
        # copies are discarded; an earlier successful cached state is preserved.
        self.cache[cache_key] = (ref, target, ep.last.time_ms, book.copy())
        self.stats["replay_commits"]+=1
        return book


CSV1_FIELDS = ["episode_id","event_id","time","block_number","coin","direction","price","qty","episode_start","episode_end","start_block","end_block","event_count","total_qty","entry_vwap","partial_episode","partial_reason","split_before","split_after","status","skip_reason","source_file","source_line"]
CSV2_FIELDS = ["episode_id","coin","direction","tn","target_block","book_state_point","book_side","snapshot_file","snapshot_block","order_count","total_depth","orders_json","status","skip_reason","error_file"]
CSV3_FIELDS = ["episode_id","coin","direction","total_qty","entry_vwap","filled_qty","unfilled_qty","exit_vwap","la","depth_insufficient","partial_episode","status","skip_reason","fill_trace_json"]


def _csv_value(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Decimal):
        return dec_text(v)
    return v


def _write_csv_row(writer, row):
    writer.writerow({k:_csv_value(v) for k,v in row.items()})


def write_csv1_episode(ep: Episode, writer):
    common = {"episode_id":ep.episode_id,"coin":ep.coin,"direction":ep.direction,"status":ep.status,"skip_reason":ep.skip_reason}
    sums = {"episode_start":time_text(ep.first.time_ms),"episode_end":time_text(ep.last.time_ms),"start_block":ep.first.block_number,"end_block":ep.last.block_number,"event_count":len(ep.fills),"total_qty":ep.total_qty,"entry_vwap":ep.entry_vwap,"partial_episode":bool(ep.partial_reasons),"partial_reason":";".join(ep.partial_reasons),"split_before":ep.split_before,"split_after":ep.split_after}
    for f in ep.fills:
        _write_csv_row(writer, {**common,**sums,"event_id":f.event_id,"time":time_text(f.time_ms),"block_number":f.block_number,"price":f.price,"qty":f.qty,"source_file":f.source_file,"source_line":f.source_line})


def write_episode(ep: Episode, writers: list, *, include_csv1=True):
    if include_csv1: write_csv1_episode(ep,writers[0])
    common = {"episode_id":ep.episode_id,"coin":ep.coin,"direction":ep.direction,"status":ep.status,"skip_reason":ep.skip_reason}
    b = ep.book_result
    _write_csv_row(writers[1], {**common,"tn":time_text(ep.last.time_ms),"target_block":ep.last.block_number,"book_state_point":"block_end","book_side":"bid" if ep.direction=="long" else "ask","snapshot_file":ep.snapshot.path if ep.snapshot else "","snapshot_block":ep.snapshot.block_number if ep.snapshot else "","order_count":b["order_count"] if b else "","total_depth":b["total_depth"] if b else "","orders_json":json_text(b["orders"]) if b else "","error_file":ep.error_file})
    la = ep.la_result
    _write_csv_row(writers[2], {**common,"total_qty":ep.total_qty,"entry_vwap":ep.entry_vwap,"filled_qty":la["filled_qty"] if la else "","unfilled_qty":la["unfilled_qty"] if la else "","exit_vwap":la["exit_vwap"] if la else "","la":la["la"] if la else "","depth_insufficient":la["depth_insufficient"] if la else "","partial_episode":bool(ep.partial_reasons),"fill_trace_json":json_text(la["fill_trace"]) if la else ""})


def plan_from_csv1(path, episodes):
    """Deduplicate the materialized draft, checking its Tn/coin/block contract."""
    by_id={ep.episode_id:ep for ep in episodes};seen=set();planned=[]
    with Path(path).open(encoding="utf-8-sig",newline="") as f:
        for row in csv.DictReader(f):
            ep=by_id[row["episode_id"]]
            if row["coin"]!=ep.coin or int(row["end_block"])!=ep.last.block_number or parse_time(row["episode_end"])!=ep.last.time_ms:
                raise ValueError("csv1 draft episode plan differs from grouped fills")
            if ep.episode_id not in seen:seen.add(ep.episode_id);planned.append(ep)
    if seen!=set(by_id):raise ValueError("csv1 draft omits an episode")
    return planned


def commit_episode_outputs(ep, writers, handles):
    write_episode(ep,writers,include_csv1=False)
    # Release only after BOTH output writes and all flushes have succeeded.
    for h in handles:h.flush()
    ep.book_result=None
    ep.la_result=None


def finalize_csv1(path, episodes, original_handle):
    """Replace pending draft with final statuses without retaining heavy results."""
    temp=None
    try:
        with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",newline="",dir=Path(path).parent,prefix=".csv1_",suffix=".tmp",delete=False) as f:
            temp=Path(f.name);writer=csv.DictWriter(f,fieldnames=CSV1_FIELDS);writer.writeheader()
            for ep in episodes:write_csv1_episode(ep,writer)
            f.flush();os.fsync(f.fileno())
        original_handle.close()
        os.replace(temp,path)
    finally:
        if temp is not None and temp.exists():temp.unlink()


def run_screen(config: dict, output_dir: Path | str) -> dict:
    """Group/draft csv1, extract demanded coins once, replay, flush/release, finalize.

    The four core outputs live in output_dir; extraction cache is separate.

    An existing core output is never overwritten without config overwrite=true.
    A failed episode produces status rows; a configuration/programming failure is
    fatal and recorded as RUN_FAILED. No resume/append silently duplicates results.
    """
    d1, dn = parse_time(config["data_start"]), parse_time(config["data_end"])
    if dn-d1 < 2*BUFFER_MS:
        raise ValueError("data range must span at least two hours for one-hour buffers")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    names = ["csv1.csv","csv2.csv","csv3.csv","run.log"]
    # Output collision protection is not an input-file preflight.
    if not config.get("overwrite", False) and any((out/n).exists() for n in names):
        raise FileExistsError("core output already exists; choose a new directory or --overwrite")
    log = RunLog(out/"run.log", console=config.get("console_log", True),append=config.get("append_run_log",False))
    handles = []
    try:
        log.info("RUN_START", version=VERSION, data_start=time_text(d1),data_end=time_text(dn),core_start=time_text(d1+BUFFER_MS),core_end=time_text(dn-BUFFER_MS),input_preflight=False)
        if config.get("detailed_log",False):
            log.info("RUN_CONFIG",configuration=config,python=sys.executable,source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        writers=[]
        for name, fields in zip(names[:3], [CSV1_FIELDS,CSV2_FIELDS,CSV3_FIELDS]):
            f=open(out/name,"w",encoding="utf-8",newline="")
            handles.append(f)
            writer=csv.DictWriter(f,fieldnames=fields)
            writer.writeheader()
            f.flush()
            writers.append(writer)
        log.info("PHASE_START", phase="read_fills_and_group")
        episodes=build_episodes(iter_fills(config,log),d1,dn)
        log.info("PHASE_DONE", phase="read_fills_and_group",episodes=len(episodes),selected_fills=sum(len(e.fills) for e in episodes),partial_episodes=sum(bool(e.partial_reasons) for e in episodes))
        for ep in episodes:write_csv1_episode(ep,writers[0])
        handles[0].flush()
        log.info("CSV1_DRAFT_WRITTEN",episodes=len(episodes),fills=sum(len(e.fills) for e in episodes))
        planned=plan_from_csv1(out/"csv1.csv",episodes)
        if config.get("auto_input_discovery"):
            from input_discovery import discover_book_inputs
            discover_book_inputs(config, planned, log)
        manager=ReplayManager({**config,"rmp_cache_dir":config.get("rmp_cache_dir",str(out.parent/".rmp_l4_cache"))},log)
        manager.prepare(planned)
        if config.get("local_diff_preparation"):
            from prepare_local_diffs import prepare_diff_inputs
            log.info("PHASE_START",phase="prepare_demanded_diff_inputs")
            manager.config["_prepared_coin_diffs"]=prepare_diff_inputs(config,planned,manager,log)
            log.info("PHASE_DONE",phase="prepare_demanded_diff_inputs")
        summary={"episodes":len(episodes),"fills":sum(len(e.fills) for e in episodes),"calculated":0,"skipped":0,"depth_insufficient":0,"partial_episodes":sum(bool(e.partial_reasons) for e in episodes)}
        log.info("PHASE_START", phase="replay_compute_write")
        # Preserve increasing targets for a safe forward-only per-coin cache.
        ordered=sorted(planned,key=lambda e:(e.last.block_number,e.last.time_ms,e.episode_id))
        for i,ep in enumerate(ordered,1):
            log.info("EPISODE_START",index=i,total=len(episodes),episode=ep.episode_id,coin=ep.coin,direction=ep.direction,tn=time_text(ep.last.time_ms),target_block=ep.last.block_number,partial_episode=bool(ep.partial_reasons))
            try:
                book=manager.get(ep)
                side="bid" if ep.direction=="long" else "ask"
                orders=book.side_orders(side)
                with localcontext() as ctx:
                    ctx.prec = CALC_PRECISION
                    total_depth = sum((o.qty for o in orders), Decimal(0))
                ep.book_result={"order_count":len(orders),"total_depth":total_depth,"orders":[{"oid":o.oid,"price":dec_text(o.price),"qty":dec_text(o.qty)} for o in orders]}
                if config.get("detailed_log",False):
                    log.info("BOOK_READY",episode=ep.episode_id,coin=ep.coin,target_block=ep.last.block_number,
                             selected_side=side,side_order_count=len(orders),live_order_records=len(book.orders),
                             total_depth=dec_text(total_depth),best_price=dec_text(orders[0].price) if orders else None)
                ep.la_result=simulate_close(book,ep.direction,ep.total_qty,ep.entry_vwap)
                if config.get("detailed_log",False):
                    for fill_index,trace in enumerate(ep.la_result["fill_trace"],1):
                        log.info("SIMULATED_FILL",episode=ep.episode_id,coin=ep.coin,index=fill_index,**trace)
                ep.status="empty_book" if ep.la_result["filled_qty"]==0 else "partial_depth" if ep.la_result["depth_insufficient"] else "ok"
                summary["calculated"]+=1
                summary["depth_insufficient"]+=int(ep.la_result["depth_insufficient"])
                log.info("EPISODE_DONE",episode=ep.episode_id,status=ep.status,entry_vwap=dec_text(ep.entry_vwap),filled_qty=dec_text(ep.la_result["filled_qty"]),la=dec_text(ep.la_result["la"]),partial_episode=bool(ep.partial_reasons))
            except (BookError,InputProblem,OSError,EOFError,UnicodeError) as e:
                ep.status="skipped"
                ep.skip_reason=f"{getattr(e,'code',type(e).__name__)}: {getattr(e,'message',str(e))}"
                ep.error_file=str(getattr(e,"source","") or (ep.snapshot.path if ep.snapshot else ""))
                summary["skipped"]+=1
                log.warning("EPISODE_SKIPPED",episode=ep.episode_id,reason=ep.skip_reason,file=ep.error_file)
            commit_episode_outputs(ep,writers,handles)
            if config.get("detailed_log",False):
                log.info("CSV_OUTPUT_FLUSHED",episode=ep.episode_id,status=ep.status,csv2_row=i,csv3_row=i,
                         csv2_bytes=(out/"csv2.csv").stat().st_size,csv3_bytes=(out/"csv3.csv").stat().st_size)
            manager.finish_episode(ep)
            book=None;orders=None
            log.info("EPISODE_RESULTS_RELEASED",episode=ep.episode_id)
        finalize_csv1(out/"csv1.csv",ordered,handles[0])
        log.info("CSV1_FINALIZED",episodes=len(episodes))
        log.info("CACHE_SUMMARY",replay_entries=len(manager.cache),**manager.stats,**manager.extraction.stats)
        log.info("PHASE_DONE",phase="replay_compute_write",**summary)
        log.info("RUN_DONE",elapsed_seconds=round(_clock.monotonic()-log.started,3),**summary)
        return summary
    except Exception as e:
        log.warning("RUN_FAILED",reason=type(e).__name__,detail=str(e))
        raise
    finally:
        for h in handles:
            h.close()
        log.close()


EXAMPLE_CONFIG = {
    "data_start":"2026-10-01T00:00:00Z",
    "data_end":"2026-10-02T00:00:00Z",
    "fills":{"format":"node","hlp_addresses":["REPLACE_WITH_VERIFIED_HLP_OWNER_ADDRESS"],"path_template":"/data/node_fills_by_block/hourly/{date}/{hour}"},
    "diffs":{"coverage_mode":"dense","path_template":"/data/node_raw_book_diffs/hourly/{date}/{hour}"},
    "snapshots":[{"path":"/data/before.rmp","block_number":123456,"time":"2026-09-30T23:50:00Z"}],
    "rmp_cache_dir":"./rmp_l4_cache","chain":"Mainnet"
}


def _resolve_config_paths(config: dict, base: Path) -> dict:
    def resolve(s):
        p=Path(str(s)).expanduser()
        return str(p if p.is_absolute() else base/p)
    for secname in ("fills","diffs"):
        sec=config[secname]
        if "path_template" in sec:
            sec["path_template"]=resolve(sec["path_template"])
        for c in sec.get("chunks",[]):
            c["path"]=resolve(c["path"])
    for s in config["snapshots"]:
        s["path"]=resolve(s["path"])
    if config.get("rmp_cache_dir"):
        config["rmp_cache_dir"]=resolve(config["rmp_cache_dir"])
    if config.get("hl_node"):
        config["hl_node"]=resolve(config["hl_node"])
    return config


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description="HLP episodes + block-end per-order replay + LA. See --print-framework and --example-config.")
    parser.add_argument("--config",help="JSON configuration; relative paths resolve beside this file")
    parser.add_argument("--output",default="hlp_output",help="four core outputs; rmp_cache_dir is separate")
    parser.add_argument("--overwrite",action="store_true")
    parser.add_argument("--print-framework",action="store_true")
    parser.add_argument("--print-codex-prompt",action="store_true")
    parser.add_argument("--print-input-contract",action="store_true")
    parser.add_argument("--example-config",action="store_true")
    args=parser.parse_args(argv)
    if args.print_framework:
        print(FRAMEWORK)
        return 0
    if args.print_codex_prompt:
        print(FRAMEWORK+"\n"+INPUT_CONTRACT+"\n"+CODEX_PROMPT)
        return 0
    if args.print_input_contract:
        print(INPUT_CONTRACT)
        return 0
    if args.example_config:
        print(json.dumps(EXAMPLE_CONFIG,ensure_ascii=False,indent=2))
        return 0
    if not args.config:
        parser.error("--config is required unless printing the embedded instructions")
    try:
        cp=Path(args.config).resolve()
        with open(cp,encoding="utf-8-sig") as f:
            config=json.load(f)
        config=_resolve_config_paths(config,cp.parent)
        if args.overwrite:
            config["overwrite"]=True
        summary=run_screen(config,Path(args.output))
        print(json.dumps(summary,ensure_ascii=False))
        return 0
    except Exception as e:
        print(f"ERROR {type(e).__name__}: {e}",file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())
