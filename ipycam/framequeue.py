#!/usr/bin/env python3
"""
Bounded drop-oldest frame queue.

A small reusable primitive that decouples a fast producer (the capture thread)
from a slower consumer (an encoder / socket writer). ``put`` NEVER blocks: when
the queue is full the oldest item is evicted and a drop is counted, so a stalled
consumer can never apply back-pressure to the producer. ``get`` blocks the
consumer until an item is available (or a timeout elapses).

This gives "latest-wins" semantics -- the buffer always holds the most recent N
items, delivered oldest-first -- which is exactly what a live video pipeline
wants: never freeze the capture loop, just drop stale frames for whichever
output cannot keep up.
"""

import threading
from collections import deque
from typing import Any, Optional


class FrameQueue:
    """Thread-safe bounded queue with non-blocking, drop-oldest ``put``.

    Args:
        maxsize: Maximum number of buffered items (>= 1). Defaults to 2, which
            keeps latency low (at most one stale frame buffered) while still
            absorbing brief consumer stalls.
    """

    def __init__(self, maxsize: int = 2):
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self.maxsize = maxsize
        self._deque: deque = deque()
        self._cond = threading.Condition()
        self._dropped = 0
        self._closed = False

    def put(self, item: Any) -> bool:
        """Enqueue ``item`` without ever blocking.

        If the queue is full the oldest buffered item is evicted to make room
        and the drop counter is incremented.

        Returns:
            True if the item was stored without dropping anything, False if the
            oldest item had to be evicted (queue was full).
        """
        with self._cond:
            dropped = False
            if len(self._deque) >= self.maxsize:
                self._deque.popleft()
                self._dropped += 1
                dropped = True
            self._deque.append(item)
            self._cond.notify()
            return not dropped

    def get(self, timeout: Optional[float] = None) -> Optional[Any]:
        """Block until an item is available, then return it (oldest-first).

        Args:
            timeout: Maximum seconds to wait. ``None`` waits indefinitely (until
                an item arrives or the queue is closed).

        Returns:
            The next item, or ``None`` if the timeout elapsed with nothing
            available, or the queue was closed while empty (shutdown sentinel).
        """
        with self._cond:
            if not self._deque and not self._closed:
                self._cond.wait(timeout)
            if not self._deque:
                return None
            return self._deque.popleft()

    def get_latest(self, timeout: Optional[float] = None) -> Optional[Any]:
        """Like ``get`` but discards all but the newest buffered item.

        Useful for consumers that only ever care about the freshest frame and
        want to skip any backlog that built up while they were busy.
        """
        with self._cond:
            if not self._deque and not self._closed:
                self._cond.wait(timeout)
            if not self._deque:
                return None
            item = self._deque.pop()
            skipped = len(self._deque)
            if skipped:
                self._dropped += skipped
                self._deque.clear()
            return item

    def close(self) -> None:
        """Mark the queue closed and wake every waiting consumer.

        Waiting ``get`` calls return promptly (``None`` once drained) so worker
        threads can observe shutdown without waiting out their timeout.
        """
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def clear(self) -> None:
        """Discard all buffered items (does not count them as drops)."""
        with self._cond:
            self._deque.clear()

    @property
    def closed(self) -> bool:
        with self._cond:
            return self._closed

    @property
    def dropped(self) -> int:
        """Total items dropped because the queue was full."""
        with self._cond:
            return self._dropped

    def qsize(self) -> int:
        """Current number of buffered items (approximate under concurrency)."""
        with self._cond:
            return len(self._deque)

    def __len__(self) -> int:
        return self.qsize()


# Alias: "latest-wins" is the whole point of this queue, so expose a name that
# reads that way at call sites that want to be explicit about the semantics.
LatestFrameQueue = FrameQueue
