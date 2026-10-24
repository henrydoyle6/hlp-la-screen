#!/usr/bin/env python3
"""HLP 筛选器的可重复模拟、对抗测试和真实订单簿一致性检验。

与 hlp_la_screen.py 放在同一目录。仅需 Python 3.10+；读取 .lz4/.zst 时
分别需要 lz4/zstandard。原始 .rmp/.rmp.zip ABCI state 使用共享原生 RMP 提取器；所有入口复用同一引擎。

常用命令：
  python test_order_book_replay.py
  python test_order_book_replay.py --verbose
  python test_order_book_replay.py --demo
  python test_order_book_replay.py --demo --demo-output ./examples
  python test_order_book_replay.py --compare \
    --before-state before.rmp --before-block 123000 \
    --before-time 2026-10-20T00:00:00Z \
    --after-state after.rmp --target-block 124000 --coin BTC \
    --diffs diffs_00.jsonl diffs_01.jsonl

--compare 先只使用 before-state 和 diff 完成回放，再读取 after-state。
after-state 只作为验证答案，绝不作为还原起点、补洞数据或回退快照。
PASS 的范围是指定币种双方所有活跃订单的 oid、方向、价格和剩余数量逐笔
一致，不表示账户、仓位、oracle、触发条件、队列时间等整个 ABCI state 一致。
同价位订单不会合并；数值使用 Decimal 比较，不使用浮点容差。

默认 --diff-mode dense 要求起点之后每个 block 均有一条完整记录，允许
events=[]。--diff-mode file 适用于已经确认连续文件覆盖的稀疏变更流：
缺 block 行不自动等于数据缺失；缺文件、坏 JSON 或实际回放错误仍失败。
file 模式不能由这些文件本身证明所有空白 block 的覆盖，因此比较结果会
明确记录此限制。输入 diff 格式为按 block 合批的 JSONL，每条包含
block_number 和 events。逐事件流应先按真实 block 顺序合批，不能凭行号
把不同数据流的事件对齐。

退出码：单元测试 0=通过、1=失败；真实检验 0=逐单一致、1=不一致、
2=输入/还原失败。模拟测试通过不等于真实 ABCI 数据验收通过。
"""

from __future__ import annotations

import argparse
import bz2
import contextlib
import csv
import gzip
import io
import json
import lzma
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable
import unittest

import hlp_la_screen as app


D = Decimal


def parse_time_ms(value: str | int) -> int:
    """数值为毫秒；无时区 ISO 时间按 UTC 解释，与 fixtures 一致。"""
    if isinstance(value, int):
        return value
    if value.lstrip("-").isdigit():
        return int(value)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = dt.astimezone(timezone.utc) - epoch
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def decimal_text(value: Decimal | str | int) -> str:
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    if not number.is_finite():
        raise ValueError("订单价格/数量必须为有限数值")
    if number == 0:
        return "0"
    return format(number, "f").rstrip("0").rstrip(".") if "." in format(number, "f") else format(number, "f")


@contextlib.contextmanager
def open_data_text(path: Path):
    """只读本地文件；不下载数据、不搜索替代快照。"""
    suffix = path.suffix.lower()
    if suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            yield stream
    elif suffix == ".bz2":
        with bz2.open(path, "rt", encoding="utf-8") as stream:
            yield stream
    elif suffix in (".xz", ".lzma"):
        with lzma.open(path, "rt", encoding="utf-8") as stream:
            yield stream
    elif suffix == ".lz4":
        try:
            import lz4.frame  # type: ignore
        except ImportError as exc:
            raise ValueError("读取 .lz4 需要安装 lz4") from exc
        with lz4.frame.open(path, "rt", encoding="utf-8") as stream:
            yield stream
    elif suffix in (".zst", ".zstd"):
        try:
            import zstandard  # type: ignore
        except ImportError as exc:
            raise ValueError("读取 .zst 需要安装 zstandard") from exc
        with path.open("rb") as raw:
            with zstandard.ZstdDecompressor().stream_reader(raw) as reader:
                with io.TextIOWrapper(reader, encoding="utf-8") as stream:
                    yield stream
    else:
        with path.open("r", encoding="utf-8-sig") as stream:
            yield stream


def read_diff_blocks(paths: Iterable[str | Path], *, start_block: int | None = None,
                     target_block: int | None = None) -> Iterable[dict[str, Any]]:
    """按调用者给定文件顺序流式读取，只检验所需范围；不能排序/补造 block。"""
    for file_path in paths:
        path = Path(file_path)
        with open_data_text(path) as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line, parse_float=Decimal)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise ValueError(f"{path}:{line_number}: 无法解析 diff JSON: {exc}") from exc
                if not isinstance(item, dict):
                    raise ValueError(f"{path}:{line_number}: 需要 {{block_number, events:[...]}} 格式")
                if "gap" in item:
                    gap = item["gap"]
                    if isinstance(gap, dict):
                        first, last = gap.get("start_block"), gap.get("end_block")
                        if type(first) is int and type(last) is int and first <= last:
                            if start_block is not None and last < start_block:
                                continue
                            if target_block is not None and first > target_block:
                                return
                    raise ValueError(f"{path}:{line_number}: 所需diff含缺口或缺口范围不明: {gap}")
                height = item.get("block_number")
                if type(height) is not int:
                    raise ValueError(f"{path}:{line_number}: block_number 必须为整数")
                if start_block is not None and height < start_block:
                    continue
                if target_block is not None and height > target_block:
                    return
                if not isinstance(item.get("events"), list):
                    raise ValueError(f"{path}:{line_number}: 所需block的events必须为数组")
                item = dict(item)
                item.setdefault("source_file", str(path))
                item.setdefault("source_line", line_number)
                yield item


def active_order_map(book: Any) -> dict[str, dict[str, str]]:
    """独立构造比较键，保留全部 oid，不按价格档聚合。"""
    result: dict[str, dict[str, str]] = {}
    for side in ("bid", "ask"):
        for order in book.side_orders(side):
            oid = str(order.oid)
            if oid in result:
                raise ValueError(f"重复活跃订单 ID: {oid}")
            result[oid] = {
                "side": str(order.side),
                "price": decimal_text(order.price),
                "qty": decimal_text(order.qty),
            }
    return result


def compare_order_maps(actual: dict[str, dict[str, str]], expected: dict[str, dict[str, str]], max_differences: int = 30) -> dict[str, Any]:
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = []
    for oid in sorted(set(actual) & set(expected)):
        fields = {}
        for field in ("side", "price", "qty"):
            av, ev = actual[oid][field], expected[oid][field]
            equal = (av == ev) if field == "side" else (D(av) == D(ev))
            if not equal:
                fields[field] = {"replayed": av, "expected": ev}
        if fields:
            changed.append({"oid": oid, "differences": fields})
    mismatch_count = len(missing) + len(extra) + len(changed)
    return {
        "status": "PASS" if mismatch_count == 0 else "FAIL",
        "comparison_scope": "all_active_bid_and_ask_orders:oid,side,price,remaining_qty",
        "replayed_order_count": len(actual),
        "expected_order_count": len(expected),
        "mismatch_count": mismatch_count,
        "missing_order_count": len(missing),
        "extra_order_count": len(extra),
        "changed_order_count": len(changed),
        "missing_orders": [{"oid": oid, "expected": expected[oid]} for oid in missing[:max_differences]],
        "extra_orders": [{"oid": oid, "replayed": actual[oid]} for oid in extra[:max_differences]],
        "changed_orders": changed[:max_differences],
        "difference_examples_limit_per_category": max_differences,
    }


def compare_real_snapshots(*, before_state: str | Path, after_state: str | Path, before_block: int,
                           before_time: str | int, target_block: int, coin: str,
                           diffs: list[str | Path], diff_mode: str = "dense",
                           max_differences: int = 30) -> dict[str, Any]:
    if target_block <= before_block:
        raise ValueError("target_block 必须大于 before_block")
    if Path(before_state).resolve() == Path(after_state).resolve():
        raise ValueError("前后快照必须是不同的输入文件")
    if diff_mode not in ("dense", "file"):
        raise ValueError("diff_mode 只能是 dense 或 file")
    if max_differences < 1:
        raise ValueError("max_differences 必须为正整数")

    # 关键隔离：以下回放结束之前，既不打开也不转换 after_state。
    before_ref = app.SnapshotRef(str(before_state), before_block, parse_time_ms(before_time))
    replayed = app.load_snapshot(before_ref, coin)
    blocks = read_diff_blocks(diffs, start_block=before_block + 1, target_block=target_block)
    app.replay_blocks(replayed, blocks, before_block, target_block, dense=(diff_mode == "dense"))
    actual = active_order_map(replayed)

    # time_ms=0 是“比较端不参与快照选取”的占位值，不是声称目标快照时间为 epoch。
    after_ref = app.SnapshotRef(str(after_state), target_block, 0)
    expected_book = app.load_snapshot(after_ref, coin)
    result = compare_order_maps(actual, active_order_map(expected_book), max_differences)
    result.update({
        "coin": coin,
        "before_block": before_block,
        "target_block": target_block,
        "before_state": str(before_state),
        "expected_state": str(after_state),
        "expected_state_used_only_after_replay": True,
        "diff_mode": diff_mode,
        "coverage_note": "每个 block 包括空 events 均须存在" if diff_mode == "dense" else "输入文件覆盖由调用者保证；不从缺 block 行推断缺失",
        "not_compared": ["accounts", "positions", "oracle", "order_timestamps", "queue_order", "untriggered_orders", "other_ABCI_fields"],
    })
    return result


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def compare_multiple_snapshots(*, before_state, after_state, before_block,
        before_time, target_block, coins, diffs, max_differences=30):
    """Batch native extraction and diff decoding; all replay finishes before any answer read."""
    from rmp_l4 import ExtractionCache, ExtractionError, is_rmp
    from input_discovery import rmp_header
    from state_store import Cache
    from prepare_local_diffs import DiffCache
    if target_block<=before_block or Path(before_state).resolve()==Path(after_state).resolve():
        raise ValueError('Distinct snapshots and a forward target are required')
    coins=sorted(set(coins))
    if not coins or max_differences<1:raise ValueError('Specify coins and a positive difference limit')
    class QuietLog:
        def info(self,*args,**kwargs):pass
        warning=info
        def heartbeat(self,*args):pass
    with tempfile.TemporaryDirectory(prefix='hlp_exact_comparison_') as directory:
        cache=Cache(Path(directory)/'cache.sqlite',QuietLog())
        try:
            before=app.SnapshotRef(str(before_state),before_block,parse_time_ms(before_time))
            extractor=ExtractionCache(cache,QuietLog())
            if is_rmp(before.path):
                header=rmp_header(before.path)
                if header['block_number']!=before.block_number or header['time_ms']!=before.time_ms:
                    raise app.BookError('SNAPSHOT_METADATA_MISMATCH','Before metadata differs',before.path)
                entries=extractor.ensure(before,set(coins))
            else:entries=None
            project=DiffCache({'diffs':{'chunks':[{'path':str(p)} for p in diffs]}},cache,coins,target_block,QuietLog())
            for coin in coins:
                if entries is not None:
                    entry=entries[coin]
                    if isinstance(entry,ExtractionError):raise app.BookError(entry.code,str(entry),before.path)
                    book=app._book_from_payload(cache.read('extract',entry),before,coin)
                else:book=app.load_snapshot(before,coin)
                app.replay_blocks(book,project.between(before,target_block,0),before_block,target_block,dense=True)
                with cache.db:cache.write('comparison_actual',coin,active_order_map(book))
                del book
            # Answer isolation: no after path was opened or probed before this boundary.
            after_time=0
            if is_rmp(after_state):
                header=rmp_header(after_state)
                if header['block_number']!=target_block:raise app.BookError('SNAPSHOT_HEIGHT_MISMATCH','Answer target mismatch',str(after_state))
                after_time=header['time_ms']
            after=app.SnapshotRef(str(after_state),target_block,after_time)
            expected_entries=extractor.ensure(after,set(coins)) if is_rmp(after.path) else None
            results=[]
            for coin in coins:
                if expected_entries is not None:
                    entry=expected_entries[coin]
                    if isinstance(entry,ExtractionError):raise app.BookError(entry.code,str(entry),after.path)
                    expected=app._book_from_payload(cache.read('extract',entry),after,coin)
                else:expected=app.load_snapshot(after,coin)
                result=compare_order_maps(cache.read('comparison_actual',coin),active_order_map(expected),max_differences)
                result['coin']=coin;results.append(result)
                del expected
            return dict(status='PASS' if all(r['status']=='PASS' for r in results) else 'FAIL',
                before_block=before_block,target_block=target_block,before_state=str(before_state),
                expected_state=str(after_state),expected_state_used_only_after_replay=True,diff_mode='dense',
                results=results,cache_counts=cache.stats)
        finally:cache.close()


def write_jsonl(path: Path, values: Iterable[Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for value in values:
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")
    return path


def native_order(oid: int | str, price: str, qty: str, *, coin: str = "BTC", side: str = "B", trigger: bool = False) -> dict[str, Any]:
    return {"coin": coin, "oid": oid, "side": side, "limitPx": price, "sz": qty, "isTrigger": trigger}


def new_diff(oid: int | str, price: str, qty: str, *, coin: str = "BTC", side: str = "Bid") -> dict[str, Any]:
    return {"coin": coin, "oid": oid, "side": side, "px": price, "raw_book_diff": {"new": {"sz": qty}}}


def update_diff(oid: int | str, old_qty: str, new_qty: str, *, coin: str = "BTC") -> dict[str, Any]:
    return {"coin": coin, "oid": oid, "raw_book_diff": {"update": {"origSz": old_qty, "newSz": new_qty}}}


def remove_diff(oid: int | str, *, coin: str = "BTC") -> dict[str, Any]:
    return {"coin": coin, "oid": oid, "raw_book_diff": "remove"}


def block(height: int, *events: dict[str, Any]) -> dict[str, Any]:
    return {"block_number": height, "events": list(events)}


class BookReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hlp_book_tests_")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.before = write_json(self.root / "before.json", [["BTC", [[
            native_order(1, "100", "2"), native_order(2, "100.0", "3"),
            native_order(3, "99", "4"), native_order(900, "110", "100", trigger=True),
        ], [native_order(4, "102", "5", side="A")]]]])

    def book(self, **kwargs):
        return app.load_snapshot(app.SnapshotRef(str(self.before), 100, 1000), "BTC", **kwargs)

    def test_same_price_orders_stay_separate_and_trigger_is_excluded(self):
        book_ = self.book()
        self.assertEqual([str(o.oid) for o in book_.side_orders("bid")], ["1", "2", "3"])
        self.assertEqual([o.qty for o in book_.side_orders("bid")], [D("2"), D("3"), D("4")])
        self.assertNotIn("900", active_order_map(book_))

    def test_new_update_remove_and_absolute_new_size(self):
        book_ = self.book()
        book_.apply_diff(new_diff(5, "101", "2"))
        book_.apply_diff(update_diff(1, "2", "0.5"))
        book_.apply_diff(remove_diff(2))
        self.assertEqual(active_order_map(book_), {
            "1": {"side": "bid", "price": "100", "qty": "0.5"},
            "3": {"side": "bid", "price": "99", "qty": "4"},
            "4": {"side": "ask", "price": "102", "qty": "5"},
            "5": {"side": "bid", "price": "101", "qty": "2"},
        })

    def test_zero_update_then_remove_is_not_double_removal(self):
        book_ = self.book()
        book_.apply_diff(update_diff(1, "2", "0"))
        self.assertNotIn("1", active_order_map(book_))
        book_.apply_diff(remove_diff(1))
        self.assertNotIn("1", active_order_map(book_))

    def test_bid_descending_and_ask_ascending(self):
        book_ = self.book()
        book_.apply_diff(new_diff(5, "101", "1"))
        book_.apply_diff(new_diff(6, "103", "1", side="Ask"))
        book_.apply_diff(new_diff(7, "101.5", "1", side="Ask"))
        self.assertEqual([o.price for o in book_.side_orders("bid")], [D("101"), D("100"), D("100"), D("99")])
        self.assertEqual([o.price for o in book_.side_orders("ask")], [D("101.5"), D("102"), D("103")])

    def test_copy_isolated_from_later_replay(self):
        book_ = self.book()
        cloned = book_.copy()
        cloned.apply_diff(update_diff(1, "2", "1"))
        self.assertEqual(book_.orders["1"].qty, D("2"))
        self.assertEqual(cloned.orders["1"].qty, D("1"))

    def test_decimal_precision_is_not_float_rounded(self):
        book_ = self.book()
        book_.apply_diff(new_diff(5, "0.1000000000000000001", "0.3000000000000000003"))
        book_.apply_diff(update_diff(5, "0.3000000000000000003", "0.2999999999999999999"))
        self.assertEqual(book_.orders["5"].price, D("0.1000000000000000001"))
        self.assertEqual(book_.orders["5"].qty, D("0.2999999999999999999"))

    def test_unknown_update_fails_instead_of_creating_fake_order(self):
        with self.assertRaises(app.BookError):
            self.book().apply_diff(update_diff(9999, "1", "2"))

    def test_unknown_remove_fails_instead_of_ignoring_data_loss(self):
        with self.assertRaises(app.BookError):
            self.book().apply_diff(remove_diff(9999))

    def test_duplicate_new_order_fails(self):
        with self.assertRaises(app.BookError):
            self.book().apply_diff(new_diff(1, "100", "2"))

    def test_old_quantity_mismatch_fails_only_when_strict_mode_is_requested(self):
        with self.assertRaises(app.BookError):
            self.book(strict_orig_sz=True).apply_diff(update_diff(1, "999", "1"))

    def test_default_update_uses_absolute_new_size_without_assuming_orig_sz_semantics(self):
        book_ = self.book()
        book_.apply_diff(update_diff(1, "999", "1.25"))
        self.assertEqual(book_.orders["1"].qty, D("1.25"))

    def test_other_coin_does_not_change_this_book(self):
        book_ = self.book()
        initial = active_order_map(book_)
        book_.apply_diff(remove_diff(9999, coin="ETH"))
        self.assertEqual(active_order_map(book_), initial)

    def test_replay_includes_all_changes_in_target_block_not_next_block(self):
        book_ = self.book()
        app.replay_blocks(book_, [
            block(101, new_diff(5, "101", "2")),
            block(102),
            block(103, update_diff(1, "2", "0.5"), remove_diff(2)),
            block(104, update_diff(5, "2", "999")),
        ], 100, 103, dense=True)
        self.assertEqual(book_.orders["1"].qty, D("0.5"))
        self.assertNotIn("2", active_order_map(book_))
        self.assertEqual(book_.orders["5"].qty, D("2"))

    def test_new_update_remove_within_one_block_obeys_event_order(self):
        book_ = self.book()
        app.replay_blocks(book_, [block(101, new_diff(5, "101", "2"), update_diff(5, "2", "1"), remove_diff(5))], 100, 101)
        self.assertNotIn("5", active_order_map(book_))

    def test_missing_middle_diff_block_fails(self):
        with self.assertRaises(app.BookError):
            app.replay_blocks(self.book(), [block(101), block(103)], 100, 103, dense=True)

    def test_missing_diff_error_names_the_diff_file(self):
        later = block(103)
        later["source_file"] = "actual_diffs.jsonl"
        with self.assertRaises(app.BookError) as context:
            app.replay_blocks(self.book(), [block(101), later], 100, 103, dense=True)
        self.assertIn("actual_diffs.jsonl", context.exception.source)

    def test_missing_target_diff_block_fails(self):
        with self.assertRaises(app.BookError):
            app.replay_blocks(self.book(), [block(101), block(102)], 100, 103, dense=True)

    def test_empty_blocks_are_valid_complete_coverage(self):
        book_ = self.book()
        expected = active_order_map(book_)
        app.replay_blocks(book_, [block(101), block(102), block(103)], 100, 103, dense=True)
        self.assertEqual(active_order_map(book_), expected)

    def test_snapshot_selection_never_uses_same_or_future_block(self):
        refs = [app.SnapshotRef("old.json", 90, 1000), app.SnapshotRef("recent.json", 99, 2000),
                app.SnapshotRef("same_block.json", 100, 2500), app.SnapshotRef("future.json", 101, 4000)]
        chosen = app.select_snapshot(refs, 3000, 100)
        self.assertEqual(chosen.path, "recent.json")

    def test_snapshot_at_exact_tn_is_not_before_tn(self):
        with self.assertRaises(app.BookError):
            app.select_snapshot([app.SnapshotRef("same_time.json", 99, 3000)], 3000, 100)

    def test_nearest_before_snapshot_selected_not_an_arbitrary_older_one(self):
        refs = [app.SnapshotRef("old.json", 80, 1000), app.SnapshotRef("new.json", 90, 2000)]
        self.assertEqual(app.select_snapshot(refs, 3000, 100).path, "new.json")

    def test_height_wrapper_and_user_order_wrapper(self):
        native = [100, [["BTC", [[["0xabc", native_order(1, "100", "2")]], []]]]]
        path = write_json(self.root / "wrapped.json", native)
        actual = app.load_snapshot(app.SnapshotRef(str(path), 100, 1000), "BTC")
        self.assertEqual(active_order_map(actual), {"1": {"side": "bid", "price": "100", "qty": "2"}})

    def test_snapshot_height_mismatch_is_not_silently_ignored(self):
        path = write_json(self.root / "wrong_height.json", [101, [["BTC", [[native_order(1, "100", "2")], []]]]])
        with self.assertRaises(app.BookError):
            app.load_snapshot(app.SnapshotRef(str(path), 100, 1000), "BTC")

    def test_present_empty_coin_book_is_valid(self):
        path = write_json(self.root / "empty.json", [["BTC", [[], []]]])
        actual = app.load_snapshot(app.SnapshotRef(str(path), 100, 1000), "BTC")
        self.assertEqual(active_order_map(actual), {})

    def test_negative_quantity_is_rejected(self):
        with self.assertRaises(app.BookError):
            self.book().apply_diff(new_diff(5, "100", "-1"))

    def test_nonfinite_price_is_rejected(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value), self.assertRaises(app.BookError):
                self.book().apply_diff(new_diff(5, value, "1"))

    def test_update_cannot_change_existing_side_or_price_silently(self):
        for extra in ({"side": "Ask"}, {"px": "999"}):
            event = update_diff(1, "2", "1")
            event.update(extra)
            with self.subTest(extra=extra), self.assertRaises(app.BookError):
                self.book().apply_diff(event)

    def test_native_rmp_uses_shared_metadata_and_remaining_quantity(self):
        import msgpack
        from test_support import fixture
        native=self.root/'native.rmp'
        native.write_bytes(msgpack.packb(fixture(),use_bin_type=True))
        book=app.load_snapshot(app.SnapshotRef(str(native),100,parse_time_ms('2026-01-01T00:00:00Z')),'BTC')
        self.assertEqual(book.orders['111'].qty,D('2.5'))
        self.assertEqual(book.orders['111'].price,D('12.345'))


class IndependentComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hlp_compare_tests_")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.before = write_json(self.root / "before.json", [100, [["BTC", [[
            native_order(1, "100", "2"), native_order(2, "100", "3"),
        ], [native_order(3, "102", "5", side="A")]]]]])
        self.diffs = write_jsonl(self.root / "diffs.jsonl", [
            block(101, update_diff(1, "2", "1.25")),
            block(102, remove_diff(2), new_diff(4, "99", "4")),
            block(103, update_diff(3, "5", "2.5")),
        ])
        # Expected 状态由测试手工写出，不能调用被测 replay 来生成答案。
        self.after = write_json(self.root / "after.json", [103, [["BTC", [[
            native_order(1, "100", "1.25"), native_order(4, "99", "4"),
        ], [native_order(3, "102", "2.5", side="A")]]]]])

    def compare(self, **overrides):
        kwargs = dict(before_state=self.before, after_state=self.after, before_block=100, before_time=1000,
                      target_block=103, coin="BTC", diffs=[self.diffs])
        kwargs.update(overrides)
        return compare_real_snapshots(**kwargs)

    def test_exact_order_by_order_expected_snapshot_passes(self):
        # 起点已包含的旧块不参与回放；即使旧events坏了，已知范围也不应扩大。
        old_prefix = [{"block_number": 99, "events": "bad"},
                      {"gap": {"start_block": 100, "end_block": 100}}]
        original = self.diffs.read_text(encoding="utf-8")
        self.diffs.write_text("".join(json.dumps(item) + "\n" for item in old_prefix) + original, encoding="utf-8")
        result = self.compare()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["expected_order_count"], 3)
        self.assertEqual(result["mismatch_count"], 0)

    def test_same_price_same_quantity_but_different_oid_fails(self):
        after = write_json(self.root / "wrong_oid.json", [103, [["BTC", [[
            native_order(777, "100", "1.25"), native_order(4, "99", "4"),
        ], [native_order(3, "102", "2.5", side="A")]]]]])
        result = self.compare(after_state=after)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["missing_order_count"], 1)
        self.assertEqual(result["extra_order_count"], 1)

    def test_wrong_quantity_fails_without_float_tolerance(self):
        after = write_json(self.root / "wrong_qty.json", [103, [["BTC", [[
            native_order(1, "100", "1.2500000000000000001"), native_order(4, "99", "4"),
        ], [native_order(3, "102", "2.5", side="A")]]]]])
        result = self.compare(after_state=after)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["changed_order_count"], 1)

    def test_missing_diff_fails_before_expected_snapshot_can_rescue_replay(self):
        broken = write_jsonl(self.root / "missing.jsonl", [block(101), block(103)])
        with self.assertRaises(app.BookError):
            self.compare(diffs=[broken])

    def test_expected_is_not_opened_when_replay_fails(self):
        # 若程序提前读取 expected，会先报 FileNotFoundError；正确顺序先报缺 diff。
        broken = write_jsonl(self.root / "missing.jsonl", [block(101), block(103)])
        with self.assertRaises(app.BookError):
            self.compare(diffs=[broken], after_state=self.root / "must_not_be_read.json")

    def test_reordered_equal_decimal_text_is_not_a_false_mismatch(self):
        after = write_json(self.root / "reordered.json", [103, [["BTC", [[
            native_order(4, "99.00", "4.000"), native_order(1, "100.0", "1.2500"),
        ], [native_order(3, "102.000", "2.500", side="A")]]]]])
        self.assertEqual(self.compare(after_state=after)["status"], "PASS")

    def test_malformed_line_after_completed_target_is_not_read(self):
        with self.diffs.open("a", encoding="utf-8") as stream:
            stream.write('{"deliberately_broken_future_line":\n')
        self.assertEqual(self.compare()["status"], "PASS")


def fill(event_id: str, time: str, height: int, coin: str = "BTC", direction: str = "long", price: str = "100", qty: str = "1") -> dict[str, Any]:
    return {"event_id": event_id, "time": time, "block_number": height, "coin": coin, "direction": direction, "price": price, "qty": qty}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def truth(value: Any) -> bool:
    return str(value).lower() in ("true", "1", "yes")


def make_pipeline_config(root: Path, events: list[dict[str, Any]], *, books: Any | None = None,
                         diff_events: dict[int, list[dict[str, Any]]] | None = None,
                         max_block: int | None = None) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    if books is None:
        books = [
            ["BTC", [[native_order(1, "100", "2"), native_order(2, "100", "3"), native_order(3, "99", "20")],
                     [native_order(4, "110", "20", side="A")]]],
            ["ETH", [[native_order(10, "20", "20", coin="ETH")], [native_order(11, "21", "20", coin="ETH", side="A")]]],
            ["SOL", [[], []]],
        ]
    snap = write_json(root / "snapshot_100.json", [100, books])
    fills = write_jsonl(root / "fills.jsonl", events)
    max_height = max_block or max([int(event["block_number"]) for event in events] + [101])
    diff_events = diff_events or {}
    diff_path = write_jsonl(root / "diffs.jsonl", [block(height, *diff_events.get(height, [])) for height in range(101, max_height + 1)])
    return {
        "data_start": "2026-01-01T00:00:00Z",
        "data_end": "2026-01-01T06:00:00Z",
        "fills": {"format": "normalized", "hlp_addresses": [], "chunks": [
            {"path": str(fills), "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T06:00:01Z"},
        ]},
        "diffs": {"coverage_mode": "dense", "chunks": [{"path": str(diff_path), "start_block": 101, "end_block": max_height}]},
        "snapshots": [{"path": str(snap), "block_number": 100, "time": "2026-01-01T00:00:00Z"}],
        "progress_every": 1,
    }


class ScreeningIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hlp_screen_tests_")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def run_config(self, config):
        from test_support import run_fixture
        output=self.root/('result_'+str(len(list(self.root.glob('result_*')))))
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            summary=run_fixture(config,output)
        self.assertEqual({p.name for p in output.iterdir()}, {'csv1.csv','csv2.csv','csv3.csv','run.log','progress.sqlite','cache.sqlite'})
        return summary,*([r for r in read_csv(output/f'csv{n}.csv') if r['record_type']!='issue'] for n in (1,2,3)),(output/'run.log').read_text('utf-8')

    def test_coin_direction_independent_exact_30_minutes_and_one_ms_over(self):
        events = [
            fill("a", "2026-01-01T01:00:00Z", 101),
            fill("eth", "2026-01-01T01:10:00Z", 102, "ETH", "long", "20"),
            fill("short", "2026-01-01T01:15:00Z", 103, "BTC", "short", "100", "2"),
            fill("b", "2026-01-01T01:25:00Z", 104, price="110", qty="3"),
            fill("c", "2026-01-01T01:55:00Z", 105, price="90"),
            fill("d", "2026-01-01T02:25:00.001Z", 106),
        ]
        _, rows, _, profits, _ = self.run_config(make_pipeline_config(self.root, events))
        by_event = {row["event_id"]: row for row in rows}
        self.assertEqual(by_event["a"]["episode_id"], by_event["b"]["episode_id"])
        self.assertEqual(by_event["b"]["episode_id"], by_event["c"]["episode_id"])
        self.assertNotEqual(by_event["c"]["episode_id"], by_event["d"]["episode_id"])
        self.assertEqual(len({row["episode_id"] for row in rows}), 4)
        target = next(row for row in profits if row["episode_id"] == by_event["a"]["episode_id"])
        self.assertEqual(D(target["total_qty"]), D("5"))
        self.assertEqual(D(target["entry_vwap"]), D("104"))
        self.assertEqual(D(target["la"]), D("-20"))

    def test_same_block_three_fills_remain_three_rows(self):
        events = [fill(str(i), "2026-01-01T01:00:00Z", 101, qty=str(i)) for i in (1, 2, 3)]
        _, rows, books, profits, _ = self.run_config(make_pipeline_config(self.root, events))
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(books), 1)
        self.assertEqual(len(profits), 1)
        self.assertEqual(D(profits[0]["total_qty"]), D("6"))

    def test_episode_quantity_and_book_depth_preserve_long_decimal_tail(self):
        tiny_tail_qty = "1.000000000000000000000000000001"
        expected_total = D("3.000000000000000000000000000001")
        books = [["BTC", [[native_order(1, "100", tiny_tail_qty), native_order(2, "100", "2")], []]]]
        events = [fill("a", "2026-01-01T01:00:00Z", 101, qty=tiny_tail_qty),
                  fill("b", "2026-01-01T01:20:00Z", 102, qty="2")]
        _, rows, book_rows, profits, _ = self.run_config(make_pipeline_config(self.root, events, books=books))
        self.assertTrue(all(D(row["total_qty"]) == expected_total for row in rows))
        self.assertEqual(D(book_rows[0]["total_depth"]), expected_total)
        self.assertEqual(D(profits[0]["total_qty"]), expected_total)
        self.assertEqual(D(profits[0]["filled_qty"]), expected_total)
        self.assertEqual(D(profits[0]["la"]), D("0"))

    def test_weighted_entry_partial_last_order_and_order_level_output(self):
        books = [["BTC", [[native_order(1, "101", "4"), native_order(2, "100", "3"), native_order(3, "100", "3")], []]]]
        events = [fill("a", "2026-01-01T01:00:00Z", 101, price="99", qty="6"),
                  fill("b", "2026-01-01T01:20:00Z", 102, price="101.5", qty="4")]
        _, _, book_rows, profits, _ = self.run_config(make_pipeline_config(self.root, events, books=books))
        self.assertEqual(D(profits[0]["entry_vwap"]), D("100"))
        self.assertEqual(D(profits[0]["exit_vwap"]), D("100.4"))
        self.assertEqual(D(profits[0]["la"]), D("4"))
        orders = json.loads(book_rows[0]["orders_json"])
        self.assertEqual(len(orders), 3)
        self.assertEqual({str(order["oid"]) for order in orders}, {"1", "2", "3"})

    def test_last_price_level_can_be_partially_filled(self):
        books = [["BTC", [[native_order(1, "101", "4"), native_order(2, "100", "6")], []]]]
        events = [fill("a", "2026-01-01T01:00:00Z", 101, qty="5")]
        _, _, _, profits, _ = self.run_config(make_pipeline_config(self.root, events, books=books))
        self.assertEqual(D(profits[0]["filled_qty"]), D("5"))
        self.assertEqual(D(profits[0]["exit_vwap"]), D("100.8"))
        self.assertEqual(D(profits[0]["la"]), D("4"))
        self.assertFalse(truth(profits[0]["depth_insufficient"]))

    def test_insufficient_depth_uses_full_episode_entry_and_only_filled_quantity(self):
        books = [["BTC", [[native_order(1, "99", "3"), native_order(2, "98", "2")], []]]]
        events = [fill("a", "2026-01-01T01:00:00Z", 101, price="99", qty="6"),
                  fill("b", "2026-01-01T01:20:00Z", 102, price="101.5", qty="4")]
        _, _, _, profits, _ = self.run_config(make_pipeline_config(self.root, events, books=books))
        row = profits[0]
        self.assertEqual(D(row["entry_vwap"]), D("100"))
        self.assertEqual(D(row["filled_qty"]), D("5"))
        self.assertEqual(D(row["unfilled_qty"]), D("5"))
        self.assertEqual(D(row["exit_vwap"]), D("98.6"))
        self.assertEqual(D(row["la"]), D("-7"))
        self.assertTrue(truth(row["depth_insufficient"]))

    def test_short_consumes_asks_and_positive_la_means_profit(self):
        books = [["BTC", [[], [native_order(1, "98", "3", side="A"), native_order(2, "99", "2", side="A")]]]]
        events = [fill("s", "2026-01-01T01:00:00Z", 101, direction="short", price="100", qty="5")]
        _, _, book_rows, profits, _ = self.run_config(make_pipeline_config(self.root, events, books=books))
        self.assertEqual(D(profits[0]["exit_vwap"]), D("98.4"))
        self.assertEqual(D(profits[0]["la"]), D("8"))
        self.assertIn(book_rows[0]["book_side"].lower(), ("ask", "asks"))

    def test_empty_book_is_zero_execution_not_missing_data(self):
        events = [fill("e", "2026-01-01T01:00:00Z", 101, "SOL", qty="5")]
        _, _, books, profits, _ = self.run_config(make_pipeline_config(self.root, events))
        self.assertEqual(json.loads(books[0]["orders_json"]), [])
        self.assertEqual(D(profits[0]["filled_qty"]), D("0"))
        self.assertEqual(profits[0]["exit_vwap"], "")
        self.assertEqual(D(profits[0]["la"]), D("0"))
        self.assertEqual(profits[0]["reason_code"], "")

    def test_any_event_inside_includes_buffer_records_and_tn_outside_core(self):
        events = [fill("before", "2026-01-01T00:40:00Z", 101),
                  fill("inside", "2026-01-01T01:05:00Z", 102),
                  fill("late_inside", "2026-01-01T04:50:00Z", 103),
                  fill("late_buffer", "2026-01-01T05:15:00Z", 104),
                  fill("buffer_only", "2026-01-01T05:50:00Z", 105, "ETH", price="20")]
        _, rows, books, profits, _ = self.run_config(make_pipeline_config(self.root, events))
        self.assertEqual({row["event_id"] for row in rows}, {"before", "inside", "late_inside", "late_buffer"})
        self.assertEqual(len(profits), 2)
        self.assertIn("104", {row["target_block"] for row in books})

    def test_official_range_endpoints_are_inclusive(self):
        events = [fill("left", "2026-01-01T01:00:00Z", 101), fill("right", "2026-01-01T05:00:00Z", 102)]
        _, rows, _, profits, _ = self.run_config(make_pipeline_config(self.root, events))
        self.assertEqual({row["event_id"] for row in rows}, {"left", "right"})
        self.assertEqual(len(profits), 2)

    def test_outer_boundary_partial_episode_is_still_calculated(self):
        events = [fill(str(i), f"2026-01-01T{hour:02d}:{minute:02d}:00Z", 101 + i)
                  for i, (hour, minute) in enumerate([(0, 10), (0, 30), (0, 50), (1, 10)])]
        config = make_pipeline_config(self.root, events)
        config["snapshots"][0]["time"] = "2025-12-31T23:59:00Z"
        _, rows, _, profits, _ = self.run_config(config)
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(profits), 1)
        self.assertTrue(truth(profits[0]["partial_episode"]))
        self.assertNotEqual(profits[0]["la"], "")
        self.assertEqual(profits[0]["reason_code"], "")

    def test_short_fills_gap_splits_but_both_sides_are_calculated(self):
        events = [fill("before", "2026-01-01T01:00:00Z", 101), fill("after", "2026-01-01T01:15:00Z", 102)]
        config = make_pipeline_config(self.root, events)
        before = write_jsonl(self.root / "fills_before.jsonl", events[:1])
        after = write_jsonl(self.root / "fills_after.jsonl", events[1:])
        config["fills"]["chunks"] = [
            {"path": str(before), "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:05:00Z"},
            {"path": str(self.root / "missing_fills.jsonl"), "start": "2026-01-01T01:05:00Z", "end": "2026-01-01T01:10:00Z"},
            {"path": str(after), "start": "2026-01-01T01:10:00Z", "end": "2026-01-01T06:00:01Z"},
        ]
        _, rows, _, profits, log = self.run_config(config)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row["episode_id"] for row in rows}), 2)
        self.assertEqual(len(profits), 2)
        self.assertTrue(all(row["la"] != "" and row["reason_code"] == "" for row in profits))
        self.assertIn("missing_fills.jsonl", log)

    def test_target_block_diff_is_applied_not_previous_block(self):
        events = [fill("a", "2026-01-01T01:00:00Z", 102, qty="5")]
        config = make_pipeline_config(self.root, events, diff_events={102: [remove_diff(1), remove_diff(2)]})
        _, _, books, profits, _ = self.run_config(config)
        self.assertEqual(books[0]["target_block"], "102")
        self.assertEqual(books[0]["book_state_point"], "block_end")
        self.assertEqual(D(profits[0]["exit_vwap"]), D("99"))
        self.assertEqual(D(profits[0]["la"]), D("-5"))

    def test_missing_diff_does_not_use_after_tn_snapshot(self):
        events = [fill("a", "2026-01-01T01:00:00Z", 103)]
        config = make_pipeline_config(self.root, events)
        write_jsonl(Path(config["diffs"]["chunks"][0]["path"]), [block(101), block(103)])
        future = write_json(self.root / "future_snapshot.json", [104, [["BTC", [[native_order(1, "999", "999")], []]]]])
        config["snapshots"].append({"path": str(future), "block_number": 104, "time": "2026-01-01T01:01:00Z"})
        _, rows, books, profits, _ = self.run_config(config)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(books), 1)
        self.assertEqual(len(profits), 1)
        self.assertEqual(profits[0]["la"], "")
        self.assertTrue(profits[0]["reason_code"])
        self.assertNotEqual(books[0]["snapshot_file"], str(future))
        self.assertIn("diffs.jsonl", books[0]["error_file"])

    def test_later_episode_can_use_its_own_valid_prior_snapshot(self):
        events = [fill("early", "2026-01-01T01:00:00Z", 103), fill("late", "2026-01-01T02:00:00Z", 106)]
        config = make_pipeline_config(self.root, events)
        write_jsonl(Path(config["diffs"]["chunks"][0]["path"]), [block(101), block(103), block(104), block(105), block(106)])
        later = write_json(self.root / "later_snapshot.json", [104, [["BTC", [[native_order(1, "101", "10")], []]]]])
        config["snapshots"].append({"path": str(later), "block_number": 104, "time": "2026-01-01T01:30:00Z"})
        _, rows, _, profits, _ = self.run_config(config)
        ids = {row["event_id"]: row["episode_id"] for row in rows}
        by_id = {row["episode_id"]: row for row in profits}
        self.assertEqual(by_id[ids["early"]]["la"], "")
        self.assertEqual(D(by_id[ids["late"]]["la"]), D("1"))

    def test_declared_old_gap_before_selected_snapshot_does_not_skip_episode(self):
        events = [fill("a", "2026-01-01T01:00:00Z", 103)]
        config = make_pipeline_config(self.root, events)
        newer = write_json(self.root / "snapshot_102.json", [102, [["BTC", [[native_order(1, "101", "2")], []]]]])
        config["snapshots"].append({"path": str(newer), "block_number": 102, "time": "2026-01-01T00:30:00Z"})
        write_jsonl(Path(config["diffs"]["chunks"][0]["path"]), [
            block(101), {"gap": {"start_block": 102, "end_block": 102, "reason": "known old gap"}}, block(103),
        ])
        _, _, books, profits, _ = self.run_config(config)
        self.assertEqual(books[0]["snapshot_block"], "102")
        self.assertEqual(profits[0]["reason_code"], "")
        self.assertEqual(D(profits[0]["la"]), D("1"))

    def test_bad_old_events_before_selected_snapshot_do_not_skip_episode(self):
        events = [fill("a", "2026-01-01T01:00:00Z", 103)]
        config = make_pipeline_config(self.root, events)
        newer = write_json(self.root / "snapshot_102.json", [102, [["BTC", [[native_order(1, "101", "2")], []]]]])
        config["snapshots"].append({"path": str(newer), "block_number": 102, "time": "2026-01-01T00:30:00Z"})
        write_jsonl(Path(config["diffs"]["chunks"][0]["path"]), [{"block_number": 101, "events": "bad"}, block(103)])
        _, _, books, profits, _ = self.run_config(config)
        self.assertEqual(books[0]["snapshot_block"], "102")
        self.assertEqual(profits[0]["reason_code"], "")
        self.assertEqual(D(profits[0]["la"]), D("1"))

    def test_right_outer_partial_episode_is_still_calculated(self):
        events = [fill(str(i), f"2026-01-01T{h:02d}:{m:02d}:00Z", 101 + i)
                  for i, (h, m) in enumerate([(1, 0), (1, 30), (2, 0), (2, 30), (2, 50)])]
        config = make_pipeline_config(self.root, events)
        config["data_end"] = "2026-01-01T03:00:00Z"
        # 分片覆盖到06时，但D_n后已知时间的坏字段或明确范围gap不能改变已选episode。
        later_bad = fill("out_of_range", "2026-01-01T03:10:00Z", 106, qty="BAD")
        later_gap = {"gap": {"start": "2026-01-01T03:12:00Z", "end": "2026-01-01T03:20:00Z"}}
        write_jsonl(Path(config["fills"]["chunks"][0]["path"]), [*events, later_bad, later_gap])
        _, rows, books, profits, _ = self.run_config(config)
        self.assertEqual(len(rows), 5)
        self.assertEqual(books[0]["target_block"], "105")
        self.assertTrue(truth(profits[0]["partial_episode"]))
        self.assertNotEqual(profits[0]["la"], "")
        self.assertTrue(all(row["split_after"] == "" for row in rows))

    def test_more_than_30_minute_outer_silence_is_not_marked_partial(self):
        events = [fill("a", "2026-01-01T05:00:00Z", 101), fill("b", "2026-01-01T05:29:59.999Z", 102)]
        _, _, _, profits, _ = self.run_config(make_pipeline_config(self.root, events))
        self.assertFalse(truth(profits[0]["partial_episode"]))

    def test_empty_fills_file_is_not_a_missing_file_gap(self):
        events = [fill("before", "2026-01-01T01:00:00Z", 101), fill("after", "2026-01-01T01:15:00Z", 102)]
        config = make_pipeline_config(self.root, events)
        before = write_jsonl(self.root / "before.jsonl", events[:1])
        empty = write_jsonl(self.root / "present_empty.jsonl", [])
        after = write_jsonl(self.root / "after.jsonl", events[1:])
        config["fills"]["chunks"] = [
            {"path": str(before), "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:05:00Z"},
            {"path": str(empty), "start": "2026-01-01T01:05:00Z", "end": "2026-01-01T01:10:00Z"},
            {"path": str(after), "start": "2026-01-01T01:10:00Z", "end": "2026-01-01T06:00:01Z"},
        ]
        _, rows, _, profits, _ = self.run_config(config)
        self.assertEqual(len({row["episode_id"] for row in rows}), 1)
        self.assertEqual(D(profits[0]["total_qty"]), D("2"))

    def test_missing_future_diff_file_is_not_prechecked_for_earlier_target(self):
        events = [fill("a", "2026-01-01T01:00:00Z", 101)]
        config = make_pipeline_config(self.root, events)
        config["diffs"]["chunks"].append({"path": str(self.root / "future_missing.jsonl"), "start_block": 102, "end_block": 999})
        _, _, _, profits, _ = self.run_config(config)
        self.assertEqual(D(profits[0]["la"]), D("0"))
        self.assertEqual(profits[0]["reason_code"], "")

    def test_malformed_later_diff_line_only_skips_later_episode(self):
        events = [fill("early", "2026-01-01T01:00:00Z", 101),
                  fill("later", "2026-01-01T02:00:00Z", 103)]
        config = make_pipeline_config(self.root, events)
        path = Path(config["diffs"]["chunks"][0]["path"])
        path.write_text(json.dumps(block(101)) + '\n{"broken":\n' + json.dumps(block(103)) + "\n", encoding="utf-8")
        _, rows, _, profits, _ = self.run_config(config)
        episode_ids = {row["event_id"]: row["episode_id"] for row in rows}
        by_id = {row["episode_id"]: row for row in profits}
        self.assertEqual(D(by_id[episode_ids["early"]]["la"]), D("0"))
        self.assertEqual(by_id[episode_ids["early"]]["reason_code"], "")
        self.assertEqual(by_id[episode_ids["later"]]["la"], "")
        self.assertTrue(by_id[episode_ids["later"]]["reason_code"])

    def test_missing_diff_file_only_skips_its_required_replay(self):
        events = [fill("a", "2026-01-01T01:00:00Z", 103)]
        config = make_pipeline_config(self.root, events)
        first = write_jsonl(self.root / "diff101.jsonl", [block(101)])
        third = write_jsonl(self.root / "diff103.jsonl", [block(103)])
        config["diffs"]["chunks"] = [
            {"path": str(first), "start_block": 101, "end_block": 101},
            {"path": str(self.root / "missing_diff102.jsonl"), "start_block": 102, "end_block": 102},
            {"path": str(third), "start_block": 103, "end_block": 103},
        ]
        _, rows, books, profits, log = self.run_config(config)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(books), 1)
        self.assertEqual(profits[0]["la"], "")
        self.assertIn("missing_diff102.jsonl", log)
        self.assertIn("missing_diff102.jsonl", books[0]["error_file"])
        with self.subTest("present_but_empty_dense_stream_reports_diff_file"):
            empty = write_jsonl(self.root / "present_empty_diffs.jsonl", [])
            config["diffs"]["chunks"] = [{"path": str(empty), "start_block": 101, "end_block": 103}]
            config["overwrite"] = True
            _, _, books, profits, _ = self.run_config(config)
            self.assertEqual(profits[0]["la"], "")
            self.assertIn("present_empty_diffs.jsonl", books[0]["error_file"])

    def test_production_dense_coverage_is_not_weakened_by_legacy_file_mode(self):
        events = [fill("a", "2026-01-01T01:00:00Z", 103)]
        config = make_pipeline_config(self.root, events)
        config["diffs"]["coverage_mode"] = "file"
        write_jsonl(Path(config["diffs"]["chunks"][0]["path"]), [block(101)])
        _, _, _, profits, _ = self.run_config(config)
        self.assertEqual(profits[0]["status"], "skipped")
        self.assertEqual(profits[0]["la"], "")

    def test_node_fills_filter_receiver_address_and_backstop_method(self):
        base = [fill("placeholder", "2026-01-01T01:00:00Z", 101)]
        config = make_pipeline_config(self.root, base)
        hlp = "0x" + "1" * 40
        victim = "0x" + "2" * 40
        third = "0x" + "3" * 40
        stamp = parse_time_ms("2026-01-01T01:00:00Z")

        def native_fill(tid, method, side="B", liquidated=victim):
            item = {"coin": "BTC", "side": side, "px": "100", "sz": "2", "time": stamp, "tid": tid}
            if method is not None:
                item["liquidation"] = {"method": method, "liquidatedUser": liquidated}
            return item

        records = [[hlp, native_fill(1, "backstop")], [victim, native_fill(1, "backstop", "A")],
                   [hlp, native_fill(2, "market")], [hlp, native_fill(3, None)],
                   [third, native_fill(4, "backstop", liquidated=hlp)]]
        write_jsonl(Path(config["fills"]["chunks"][0]["path"]), [{"block_number": 101, "block_time": "2026-01-01T01:00:00Z", "events": records}])
        config["fills"]["format"] = "node"
        config["fills"]["hlp_addresses"] = [hlp]
        _, rows, _, profits, _ = self.run_config(config)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["direction"], "long")
        self.assertEqual(D(profits[0]["total_qty"]), D("2"))


def print_demo_case(title: str, config: dict[str, Any], output: Path) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)
    print("正式范围：data_start+1小时 到 data_end-1小时，双闭端。")
    print("输入配置：")
    print(json.dumps(config, ensure_ascii=False, indent=2))
    print("已提供的 fills：")
    for chunk in config["fills"]["chunks"]:
        path = Path(chunk["path"])
        if path.exists():
            print(f"\n{path.name}\n{path.read_text(encoding='utf-8').rstrip()}")
        else:
            print(f"\n{path.name}: 故意缺失，{chunk['start']}—{chunk['end']}")
    print("初始/候选快照（仅展示本程序生成的模拟输入）：")
    for snapshot in config["snapshots"]:
        path = Path(snapshot["path"])
        print(f"\n{path.name}\n{path.read_text(encoding='utf-8').rstrip()}")
    print("raw book diff 输入：")
    for chunk in config["diffs"]["chunks"]:
        path = Path(chunk["path"])
        if path.exists():
            print(f"\n{path.name}\n{path.read_text(encoding='utf-8').rstrip()}")
        else:
            print(f"\n{path.name}: 故意缺失，block {chunk['start_block']}—{chunk['end_block']}")
    from test_support import run_fixture
    summary = run_fixture(config, output)
    print("运行汇总：")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    for name in ("csv1.csv", "csv2.csv", "csv3.csv", "run.log"):
        print(f"\n--- {name} ---")
        print((output / name).read_text(encoding="utf-8-sig").rstrip())


def run_demo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    normal = root / "01_weighted_and_partial_depth"
    books = [
        ["BTC", [[native_order(1, "101", "4"), native_order(2, "100", "3"), native_order(3, "100", "3")],
                 [native_order(4, "102", "3", side="A"), native_order(5, "103", "2", side="A")]]],
        ["SOL", [[], []]],
    ]
    events = [
        fill("long_1", "2026-01-01T01:00:00Z", 101, price="99", qty="6"),
        fill("short_1", "2026-01-01T01:10:00Z", 102, direction="short", price="105", qty="10"),
        fill("long_2", "2026-01-01T01:20:00Z", 103, price="101.5", qty="4"),
        fill("empty", "2026-01-01T02:00:00Z", 104, coin="SOL", price="20", qty="5"),
    ]
    config = make_pipeline_config(normal, events, books=books)
    print_demo_case("案例1：交错多空分组、数量加权均价、同价独立订单、部分成交和有效空簿", config, normal / "output")
    print("独立人工预期：BTC多头 Q=10、接管均价100、平仓均价100.4、LA=+4；")
    print("BTC空头 Q=10、只成交5、平仓均价102.4、LA=+13、剩余5；SOL成交0、LA=0。")

    gaps = root / "02_fills_gap_boundary"
    events = [fill("left", "2026-01-01T01:00:00Z", 101), fill("right", "2026-01-01T01:15:00Z", 102)]
    config = make_pipeline_config(gaps, events)
    left = write_jsonl(gaps / "before.jsonl", events[:1])
    right = write_jsonl(gaps / "after.jsonl", events[1:])
    config["fills"]["chunks"] = [
        {"path": str(left), "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:05:00Z"},
        {"path": str(gaps / "missing.jsonl"), "start": "2026-01-01T01:05:00Z", "end": "2026-01-01T01:10:00Z"},
        {"path": str(right), "start": "2026-01-01T01:10:00Z", "end": "2026-01-01T06:00:01Z"},
    ]
    print_demo_case("案例2：只有15分钟间隔，但中间 fills 缺失，因此两个 episode 都正常计算", config, gaps / "output")
    print("独立人工预期：两个 episode，均 Q=1、LA=0；不因 fills 缺口跳过。")

    boundary = root / "03_partial_episode_at_outer_boundary"
    events = [fill(str(i), f"2026-01-01T{h:02d}:{m:02d}:00Z", 101 + i)
              for i, (h, m) in enumerate([(0, 10), (0, 30), (0, 50), (1, 10)])]
    config = make_pipeline_config(boundary, events)
    config["snapshots"][0]["time"] = "2025-12-31T23:59:00Z"
    print_demo_case("案例3：链式 episode 穿过一小时缓冲，保留已知部分并标记 partial", config, boundary / "output")
    print("独立人工预期：4条接管归一个 episode，Q=4、LA=0，partial_episode=true，仍计算。")

    missing = root / "04_diff_gap_and_no_future_rescue"
    events = [fill("skip", "2026-01-01T01:00:00Z", 103), fill("resume", "2026-01-01T02:00:00Z", 106)]
    config = make_pipeline_config(missing, events)
    write_jsonl(Path(config["diffs"]["chunks"][0]["path"]), [block(101), block(103), block(104), block(105), block(106)])
    later = write_json(missing / "snapshot_104.json", [104, [["BTC", [[native_order(1, "101", "10")], []]]]])
    config["snapshots"].append({"path": str(later), "block_number": 104, "time": "2026-01-01T01:30:00Z"})
    print_demo_case("案例4：103目标缺diff 102不能用之后快照补救；106目标可用自身Tn之前的104快照", config, missing / "output")
    print("独立人工预期：第一 episode 的 LA 留空并说明缺diff；第二 episode LA=+1。")

    replay_case = root / "05_forward_replay_block_end_and_partial_last_order"
    books = [["BTC", [[native_order(1, "101", "4"), native_order(2, "100", "3"),
                       native_order(3, "99", "10")], [native_order(10, "105", "7", side="A")]]]]
    events = [fill("takeover", "2026-01-01T01:00:00Z", 102, price="100", qty="6")]
    config = make_pipeline_config(replay_case, events, books=books, diff_events={
        101: [update_diff(1, "4", "1"), new_diff(4, "103", "2")],
        102: [remove_diff(2), new_diff(5, "100", "4"), update_diff(5, "4", "2")],
    })
    print_demo_case("案例5：真实逐订单变更顺序的模拟，完整应用Tn所在block全部diff后才平仓", config, replay_case / "output")
    print("独立人工还原顺序：snapshot100买单 oid1=101×4、oid2=100×3、oid3=99×10；")
    print("B101把oid1剩余量改为1，并新增oid4=103×2；B102删除oid2、新增oid5=100×4，再把oid5改为2。")
    print("B102块末买单从优到劣：oid4=103×2、oid1=101×1、oid5=100×2、oid3=99×10。")
    print("卖出6的顺序：2×103 + 1×101 + 2×100 + 1×99 = 606；最后一单只成交1，保留余量9。")
    print("独立人工预期：Q=6、接管均价100、平仓均价101、LA=+6；csv2仍记录模拟前完整订单量。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="生成完整模拟输入，运行筛选并打印4个核心输出")
    mode.add_argument("--compare", action="store_true", help="用真实前快照+diff还原，再与后快照逐订单比较")
    parser.add_argument("--verbose", action="store_true", help="逐项显示单元测试名称")
    parser.add_argument("--demo-output", type=Path, help="保留模拟输入及运行输出的目录；默认用临时目录")
    parser.add_argument("--before-state", type=Path)
    parser.add_argument("--after-state", type=Path)
    parser.add_argument("--before-block", type=int)
    parser.add_argument("--before-time")
    parser.add_argument("--target-block", type=int)
    parser.add_argument("--coin")
    parser.add_argument("--coins", nargs="+", help="多币种共用一次原生提取/解码；全部完成回放后再读取答案")
    parser.add_argument("--diffs", nargs="+", type=Path)
    parser.add_argument("--diff-mode", default="dense", choices=("dense", "file"))
    parser.add_argument("--max-differences", type=int, default=30)
    args = parser.parse_args(argv)
    if args.compare:
        required = ("before_state", "after_state", "before_block", "before_time", "target_block", "diffs")
        absent = [name for name in required if getattr(args, name) is None]
        if absent:
            parser.error("--compare 缺少参数: " + ", ".join("--" + name.replace("_", "-") for name in absent))
        if bool(args.coin)==bool(args.coins):parser.error('Specify exactly one of --coin or --coins')
        try:
            if args.coins:
                if args.diff_mode!='dense':parser.error('--coins requires dense coverage')
                result=compare_multiple_snapshots(before_state=args.before_state,after_state=args.after_state,
                    before_block=args.before_block,before_time=args.before_time,target_block=args.target_block,
                    coins=args.coins,diffs=args.diffs,max_differences=args.max_differences)
            else:
                result = compare_real_snapshots(before_state=args.before_state, after_state=args.after_state,
                    before_block=args.before_block, before_time=args.before_time, target_block=args.target_block,
                    coin=args.coin, diffs=args.diffs,
                    diff_mode=args.diff_mode, max_differences=args.max_differences)
        except (app.BookError, OSError, ValueError, TypeError) as exc:
            print(json.dumps({"status": "ERROR", "error_type": type(exc).__name__, "reason": str(exc)}, ensure_ascii=False, indent=2))
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "PASS" else 1
    if args.demo:
        if args.demo_output:
            run_demo(args.demo_output)
        else:
            with tempfile.TemporaryDirectory(prefix="hlp_examples_") as folder:
                run_demo(Path(folder))
        return 0
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
