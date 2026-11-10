"""The held wait behind ``GET .../live/state/?since=<rev>&wait=1`` (KAN-168).

Pure: no Django. The view hands in how to read the published ``rev`` (the
cache key) and, when the cache is not shared between workers, how to read
it from the database; this module only decides when to look and when to
give up. Tested with a fake clock in ``tests/test_live_longpoll.py``.

The constants are read at call time, so a test can shorten them.
"""
import time
from typing import Callable

# How often the cache key is read while a request is held. 200 ms is the
# added latency budget on top of the network.
TICK = 0.2

# How often the database is re-read when the cache is per process
# (LocMemCache: dev, tests, a prod box without REDIS_URL). An op committed by
# another worker never reaches this worker's key, so without this a waiter
# would sleep through it until the timeout.
DB_EVERY = 2.0

# The client aborts a held request this many seconds after the server's own
# wait (LIVE_LONGPOLL_WAIT) would have answered 204: room for the proxy and a
# slow 4G round trip. Shipped to the client as ``longpoll_abort_ms``, so the
# number lives here and nowhere in JS. Also the TTL of the one-poll marker.
ABORT_SLACK = 5


def wait_for_change(
    since: int,
    *,
    read_cache: Callable[[], object],
    read_db: Callable[[], object] | None = None,
    wait: float,
    tick: float | None = None,
    db_every: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] | None = None,
):
    """Wait until the published rev differs from ``since``; return it, or
    ``None`` after ``wait`` seconds.

    A cache read of ``None`` (the key expired, was evicted or flushed) is
    "don't know", never a change. ``read_db`` is only consulted every
    ``db_every`` seconds; pass ``None`` when the cache is shared and the
    loop must not touch the database at all.
    """
    tick = TICK if tick is None else tick
    db_every = DB_EVERY if db_every is None else db_every
    # Looked up at call time so a test can patch ``time.sleep`` here.
    sleep = sleep or time.sleep

    start = clock()
    deadline = start + wait
    next_db = start + db_every
    while True:
        remaining = deadline - clock()
        if remaining <= 1e-9:
            return None
        sleep(min(tick, remaining))

        value = read_cache()
        if value is not None and value != since:
            return value

        if read_db is not None and clock() >= next_db - 1e-9:
            value = read_db()
            if value is not None and value != since:
                return value
            next_db = clock() + db_every
