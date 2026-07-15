"""
Tests for the bounded drop-oldest FrameQueue primitive.

FrameQueue decouples a fast producer from a slower consumer: put() must NEVER
block (drop-oldest when full), get() blocks the consumer, and the whole thing
must be thread-safe under heavy concurrent load without deadlocking or
duplicating/reordering delivered items.
"""

import threading
import time

import pytest

from ipycam.framequeue import FrameQueue, LatestFrameQueue


class TestConstruction:
    def test_default_maxsize(self):
        q = FrameQueue()
        assert q.maxsize == 2

    def test_custom_maxsize(self):
        assert FrameQueue(maxsize=5).maxsize == 5

    def test_invalid_maxsize_rejected(self):
        with pytest.raises(ValueError):
            FrameQueue(maxsize=0)
        with pytest.raises(ValueError):
            FrameQueue(maxsize=-3)

    def test_latest_alias_is_framequeue(self):
        assert LatestFrameQueue is FrameQueue


class TestPutNeverBlocks:
    def test_put_returns_immediately_when_full(self):
        """put() on a full queue returns fast (drop-oldest), never blocks."""
        q = FrameQueue(maxsize=2)
        q.put("a")
        q.put("b")  # now full

        start = time.time()
        for i in range(1000):
            q.put(i)  # every one of these overflows
        elapsed = time.time() - start

        # 1000 overflowing puts should be effectively instant.
        assert elapsed < 0.5
        assert q.qsize() == 2  # never grows past maxsize

    def test_put_evicts_oldest_and_counts_drops(self):
        q = FrameQueue(maxsize=2)
        assert q.put("a") is True   # stored, no drop
        assert q.put("b") is True   # stored, no drop
        assert q.put("c") is False  # full -> evict oldest ("a")
        assert q.put("d") is False  # full -> evict oldest ("b")

        assert q.dropped == 2
        # Oldest survivors are the two most-recent items, oldest-first.
        assert q.get(timeout=1.0) == "c"
        assert q.get(timeout=1.0) == "d"

    def test_no_drops_when_within_capacity(self):
        q = FrameQueue(maxsize=3)
        q.put(1)
        q.get(timeout=1.0)
        q.put(2)
        assert q.dropped == 0


class TestGet:
    def test_get_returns_items_in_order(self):
        q = FrameQueue(maxsize=10)
        for i in range(5):
            q.put(i)
        got = [q.get(timeout=1.0) for _ in range(5)]
        assert got == [0, 1, 2, 3, 4]

    def test_get_timeout_returns_none(self):
        q = FrameQueue(maxsize=2)
        start = time.time()
        assert q.get(timeout=0.1) is None
        # Waited roughly the timeout, then gave up.
        assert 0.05 <= time.time() - start < 1.0

    def test_get_unblocks_when_item_arrives(self):
        q = FrameQueue(maxsize=2)
        results = []

        def consumer():
            results.append(q.get(timeout=2.0))

        t = threading.Thread(target=consumer)
        t.start()
        time.sleep(0.05)  # ensure consumer is blocked in get()
        q.put("hello")
        t.join(timeout=2.0)
        assert not t.is_alive()
        assert results == ["hello"]

    def test_get_returns_none_when_closed_and_empty(self):
        q = FrameQueue(maxsize=2)
        q.close()
        assert q.get(timeout=1.0) is None

    def test_close_wakes_blocked_consumer(self):
        q = FrameQueue(maxsize=2)
        results = []

        def consumer():
            results.append(q.get(timeout=5.0))

        t = threading.Thread(target=consumer)
        t.start()
        time.sleep(0.05)
        q.close()  # must wake the blocked get() promptly
        t.join(timeout=2.0)
        assert not t.is_alive()
        assert results == [None]

    def test_get_latest_discards_backlog(self):
        q = FrameQueue(maxsize=10)
        for i in range(5):
            q.put(i)
        # Only the newest survives; the rest are counted as drops.
        assert q.get_latest(timeout=1.0) == 4
        assert q.qsize() == 0
        assert q.dropped == 4


class TestConcurrencyStress:
    def test_fast_producer_slow_consumer_no_deadlock_no_dupes(self):
        """Producer far outpaces consumer; delivered items stay unique+ordered.

        Drop-oldest means gaps are expected, but every item that IS delivered
        must appear exactly once and in strictly increasing order (no tears,
        no duplicates, no reordering), and nothing may deadlock.
        """
        q = FrameQueue(maxsize=4)
        total = 5000
        received = []
        stop = threading.Event()

        def consumer():
            while not stop.is_set() or q.qsize() > 0:
                item = q.get(timeout=0.05)
                if item is None:
                    continue
                received.append(item)

        t = threading.Thread(target=consumer)
        t.start()

        for i in range(total):
            q.put(i)
            if i % 500 == 0:
                time.sleep(0.001)  # let the consumer catch a few

        # Give the consumer a moment, then signal drain + stop.
        time.sleep(0.1)
        stop.set()
        t.join(timeout=5.0)
        assert not t.is_alive(), "consumer deadlocked"

        # Strictly increasing => no duplicates, no reordering.
        assert all(received[i] < received[i + 1] for i in range(len(received) - 1))
        # Every delivered item was one we actually produced.
        assert all(0 <= x < total for x in received)
        # Accounting: delivered + dropped == produced (nothing vanished/doubled).
        assert len(received) + q.dropped == total

    def test_multiple_producers_single_consumer(self):
        q = FrameQueue(maxsize=8)
        per_thread = 1000
        n_threads = 4
        received = []
        stop = threading.Event()

        def consumer():
            while not stop.is_set() or q.qsize() > 0:
                item = q.get(timeout=0.05)
                if item is not None:
                    received.append(item)

        def producer(pid):
            for i in range(per_thread):
                q.put((pid, i))

        c = threading.Thread(target=consumer)
        c.start()
        producers = [threading.Thread(target=producer, args=(p,)) for p in range(n_threads)]
        for p in producers:
            p.start()
        for p in producers:
            p.join(timeout=5.0)

        time.sleep(0.1)
        stop.set()
        c.join(timeout=5.0)
        assert not c.is_alive()

        # No item delivered twice.
        assert len(received) == len(set(received))
        # Per-producer ordering is preserved within its own sequence.
        for pid in range(n_threads):
            seq = [i for (p, i) in received if p == pid]
            assert seq == sorted(seq)
        # Full accounting across all producers.
        assert len(received) + q.dropped == per_thread * n_threads
