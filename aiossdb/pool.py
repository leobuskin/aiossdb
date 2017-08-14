import asyncio
import collections

from .connection import create_connection
from .errors import PoolClosedError
from .log import logger


def create_pool():
    pass


class ConnectionPool:

    def __init__(self, address, *, password=None, parser=None, encoding=None, minsize, maxsize,
                 connection_cls=None, timeout=None, loop=None):
        assert isinstance(minsize, int) and minsize >= 0, ("minsize must be int >=0", minsize, type(minsize))
        assert isinstance(maxsize, int) and maxsize >= minsize, (
            "maxsize must be int >= minsize", maxsize, type(maxsize), minsize)
        if loop is None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        self._address = address
        self._password = password
        self._parser_class = parser
        self._timeout = timeout
        self._loop = loop
        self._used = set()
        self._connection_cls = connection_cls
        self._pool = collections.deque(maxlen=maxsize)
        self._minsize = minsize
        self._maxsize = maxsize
        self._encoding = encoding
        # 用于release后同步各个其他获取新连接的协程，使其开始工作，否则等待条件
        self._cond = asyncio.Condition(lock=asyncio.Lock(loop=loop), loop=loop)

    def execute(self, command, *args, **kwargs):
        conn, address = yield from self.get_connection()
        try:
            fut = conn.execute(command, *args, **kwargs)
        finally:
            self.release(conn)
        return fut

    @property
    def freesize(self):
        return len(self._pool)

    @property
    def size(self):
        return len(self._pool) + len(self._used)

    @asyncio.coroutine
    def get_connection(self):
        """获取连接，要么在空闲连接中直接获取，要么等待直到获得新的连接，
        不论如何获取的连接，都要进入used连接集合中, 然后使用完成之后返回pool可用连接中

        如果连接池自己调用的execute会自动调用release
        如果直接调用这个函数获取的连接，使用完成之后必须显式调用release方法"""
        # 在pool中寻找
        for i in range(self.freesize):
            conn = self._pool.popleft()
            # 如果连接已经关闭，则查找pool中下一个连接
            if conn.closed:
                continue
            self._used.add(conn)
            return conn, conn.address
        # 如果pool中已经没有可用连接了，动态获取连接
        conn = yield from self.new_connection()
        return conn, conn.address

    @asyncio.coroutine
    def new_connection(self):
        """pool中无可用连接，在这里创建新连接填充pool
        有可能本身used的size已经是maxsize，则填充失败，需要监听release方法的信号通知
        来重新填充，最后返回一条连接"""
        if self.closed:
            raise PoolClosedError("Pool is closed")
        with (yield from self._cond):
            # 获得锁之后可能pool已经关闭，所以进行两遍检测
            if self.closed:
                raise PoolClosedError("Pool is closed")
            # 下面获取首先填充pool，然后获取新连接
            # 有可能连接池已经满了，无法获取新连接
            while 1:
                # fill_free创建新连接,直接将pool的可用额度填满
                # 有种情况就是self.size已经满了，但是都是在used，所以就得等待release
                yield from self._fill_free(overall=True)
                if self.freesize:
                    conn = self._pool.popleft()
                    # 排除一些错误情况
                    if conn.closed or conn in self._used:
                        logger.error("conn is closed or conn is in used, {} - {}".format(conn, self._used))
                        continue
                    self._used.add(conn)
                    return conn
                else:
                    # 等待release的释放连接，然后调用notify方法来通知此处
                    yield from self._cond.wait()

    @asyncio.coroutine
    def _fill_free(self, *, overall):
        """填充pool连接池"""
        self._drop_closed()
        stop = self._maxsize if overall else self._minsize
        # 可用连接池最大值也是maxsize
        while self.size < stop:
            try:
                conn = yield from create_connection(self._address, password=self._password,
                                                    encoding=self._encoding, parser=self._parser_class,
                                                    loop=self._loop, timeout=self._timeout,
                                                    connect_cls=self._connection_cls)
            except Exception as e:
                logger.error("create connection encountered error: {}".format(e))
            else:
                self._pool.append(conn)

    def release(self, conn):
        """将没有关闭的连接从used集合放回可用的pool中，或者关闭仍然有命令的连接
        并且给一个信号通知，这样获取新连接的方法就可以获取新连接"""
        # 关闭的时候已经清空pool和used了
        if self.closed:
            raise PoolClosedError("Pool is closed")
        assert conn in self._used, ("Invalid connection, maybe from other pool", conn)
        self._used.remove(conn)
        # 如果连接还有正在执行的命令，log并且关闭
        if not conn.closed and conn._waiters:
            logger.warn("Connection {} has pending commands, closing it".format(conn))
            conn.close()
        # 如果连接没有关闭，放入可用连接池重用
        elif not conn.closed:
            self._pool.append(conn)

        # 在这里提供信号量通知
        asyncio.ensure_future(self._wake_up(), loop=self._loop)

    @asyncio.coroutine
    def _wake_up(self):
        with (yield from self._cond):
            # 通知其他协程开始工作
            self._cond.notify()

    def _drop_closed(self):
        """清除关闭的连接，pool里的和used的"""
        for i in range(self.freesize):
            conn = self._pool[0]
            if conn.closed:
                self._pool.popleft()
            else:
                # 将其转到队列尾端
                self._pool.rotate(1)
        # 使用池中已经关闭的连接
        _closed_used = set()
        for conn in self._used:
            if conn.closed:
                _closed_used.add(conn)
        # 两者求差集
        self._used = self._used.difference(_closed_used)

    def close(self):
        pass

    @property
    def closed(self):
        pass