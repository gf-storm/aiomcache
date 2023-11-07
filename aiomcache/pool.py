import asyncio
from typing import Any, Awaitable, Callable, NamedTuple, Optional, Set, Tuple

__all__ = ['MemcachePool']


class Connection(NamedTuple):
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter


Connector = Callable[..., Awaitable[Tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


class MemcachePool:

    @classmethod
    def unix(cls, path='/tmp/memcached.sock', **kwargs) -> 'MemcachePool':
        kwargs['connector'] = asyncio.open_unix_connection
        conn_args = kwargs.setdefault('conn_args', {})
        conn_args['path'] = path
        return cls(**kwargs)

    def __init__(self, host: str, port: int, *, minsize: int, maxsize: int,
                 conn_args: Optional[dict[str, Any]] = None,
                 connector: Optional[Connector] = None):
        self.conn_args = conn_args or {}
        if not connector:
            connector = asyncio.open_connection
            self.conn_args['host'] = host
            self.conn_args['port'] = port
        self._connector = connector

        self._minsize = minsize
        self._maxsize = maxsize
        self._pool: asyncio.Queue[Connection] = asyncio.Queue()
        self._in_use: Set[Connection] = set()

    async def clear(self) -> None:
        """Clear pool connections."""
        while not self._pool.empty():
            conn = await self._pool.get()
            self._do_close(conn)

    def _do_close(self, conn: Connection) -> None:
        conn.reader.feed_eof()
        conn.writer.close()

    async def acquire(self) -> Connection:
        """Acquire connection from the pool, or spawn new one
        if pool maxsize permits.

        :return: ``tuple`` (reader, writer)
        """
        while self.size() == 0 or self.size() < self._minsize:
            _conn = await self._create_new_conn()
            if _conn is None:
                break
            self._pool.put_nowait(_conn)

        conn: Optional[Connection] = None
        while not conn:
            _conn = await self._pool.get()
            if _conn.reader.at_eof() or _conn.reader.exception() is not None:
                self._do_close(_conn)
                conn = await self._create_new_conn()
            else:
                conn = _conn

        self._in_use.add(conn)
        return conn

    def release(self, conn: Connection) -> None:
        """Releases connection back to the pool.

        :param conn: ``namedtuple`` (reader, writer)
        """
        self._in_use.remove(conn)
        if conn.reader.at_eof() or conn.reader.exception() is not None:
            self._do_close(conn)
        else:
            self._pool.put_nowait(conn)

    async def _create_new_conn(self) -> Optional[Connection]:
        if self.size() < self._maxsize:
            reader, writer = await self._connector(**self.conn_args)
            if self.size() < self._maxsize:
                return Connection(reader, writer)
            else:
                reader.feed_eof()
                writer.close()
                return None
        else:
            return None

    def size(self) -> int:
        return self._pool.qsize() + len(self._in_use)
