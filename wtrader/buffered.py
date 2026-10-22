"""Read-only book workers. The engine consumes results; workers never access SQLite.

One in-flight read per symbol and fixed worker counts bound work during outages.
Held symbols have their own pool so a stalled heartbeat cannot queue ahead of exits.
Candle reads use a separate reader and pool in Engine. No worker submits orders.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import local


class BookUnavailable(ConnectionError):
    """No cached snapshot yet; this is distinct from a failed exchange request."""


class BufferedMarketData:
    def __init__(self, source, max_age_s=8):
        self.source, self.label, self.max_age_s = source, source.label, max_age_s
        self.stream = getattr(source, 'stream', None)
        self.client = getattr(source, 'client', None)
        self.priority = ThreadPoolExecutor(max_workers=2, thread_name_prefix='protected-books')
        self.normal = ThreadPoolExecutor(max_workers=1, thread_name_prefix='heartbeat-books')
        self.pending, self.cache, self.errors = {}, {}, {}
        self.failures = 0
        self.book_via = {}
        self.rest_health = None
        self._local = local()

    def _read(self, symbol):
        if not hasattr(self._local, 'reader'):
            self._local.reader = self.source.fork_reader() if hasattr(self.source, 'fork_reader') else self.source
        reader = self._local.reader
        try:
            book = reader.book(symbol)
            return book, getattr(reader, 'book_via', {}).get(symbol, 'rest'), None, reader.client.health() if hasattr(reader, 'client') else None
        except Exception as e:
            return None, None, type(e).__name__, reader.client.health() if hasattr(reader, 'client') else None

    def markets(self):
        return self.source.markets()  # bootstrap only

    def candles(self, *args):
        return self.source.candles(*args)

    def fork_reader(self):
        return self.source.fork_reader() if hasattr(self.source, 'fork_reader') else self.source

    def watch(self, symbols):
        if hasattr(self.source, 'watch'):
            self.source.watch(symbols)

    def refresh(self, symbols, protected=()):
        # All cache mutation happens on the engine thread.
        for sym, fut in list(self.pending.items()):
            if fut.done():
                del self.pending[sym]
                try:
                    book, via, error, health = fut.result()
                    self.rest_health = health or self.rest_health
                    if error:
                        self.failures += 1
                        self.errors[sym] = {'kind': error, 'at': datetime.now(timezone.utc).isoformat()}
                    else:
                        self.cache[sym] = book
                        self.book_via[sym] = via
                        self.errors.pop(sym, None)
                except Exception as e:
                    self.failures += 1
                    self.errors[sym] = {'kind': type(e).__name__, 'at': datetime.now(timezone.utc).isoformat()}
        for sym in symbols:
            streamed = self.stream.book(sym, self.max_age_s) if self.stream else None
            if streamed is not None:
                self.cache[sym] = streamed
                self.book_via[sym] = "websocket"
                continue
            b = self.cache.get(sym)
            if sym not in self.pending and (b is None or b.age_seconds(datetime.now(timezone.utc)) >= 1):
                self.pending[sym] = (self.priority if sym in protected else self.normal).submit(self._read, sym)

    def book(self, symbol):
        streamed = self.stream.book(symbol, self.max_age_s) if self.stream else None
        b = streamed or self.cache.get(symbol)
        if b is None:
            raise BookUnavailable('book is warming up or unavailable')
        return b  # original timestamp preserved; every execution boundary checks its age

    def status(self):
        return {**(self.source.status() if hasattr(self.source, 'status') else {}),
                'book_via': dict(self.book_via), 'rest': self.rest_health,
                'buffered': True, 'pending_books': sorted(self.pending), 'book_errors': dict(self.errors),
                'book_failures': self.failures}

    def close(self):
        if self.stream:
            self.stream.stop()
        self.priority.shutdown(wait=False, cancel_futures=True)
        self.normal.shutdown(wait=False, cancel_futures=True)
