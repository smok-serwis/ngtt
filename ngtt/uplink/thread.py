import logging
from concurrent.futures import Future

import minijson
from satella.coding import wraps, for_argument, silence_excs
from satella.coding.optionals import Optional
from satella.coding.predicates import x
from satella.coding.sequences import index_of
from satella.instrumentation import Traceback
from satella.time import ExponentialBackoff

from ..orders import Order

import typing as tp
import select
from satella.coding.concurrent import TerminableThread

from ..exceptions import DataStreamSyncFailed, ConnectionFailed
from ..protocol import NGTTHeaderType
from .connection import NGTTSocket

logger = logging.getLogger(__name__)


def must_be_connected(fun):
    @wraps(fun)
    def outer(self, *args, **kwargs):
        if self.current_connection is None:
            self.connect()
            raise ConnectionFailed(True)
        return fun(self, *args, **kwargs)

    return outer


def encode_data(y) -> bytes:
    return minijson.dumps(y)


class NGTTConnection(TerminableThread):
    """
    A thread maintaining connection in the background.

    Note that instantiating this object is the same as calling start. You do not need to call
    start on this object after you initialize it.

    :ivar connected (bool) is connection opened
    """

    def __init__(self, cert_file: str, key_file: str,
                 on_new_order: tp.Callable[[Order], None]):
        super().__init__(name='ngtt uplink')
        self.on_new_order = on_new_order
        self.cert_file = cert_file
        self.stopped = False
        self.key_file = key_file
        self.current_connection = None
        self.currently_running_ops = []  # type: tp.List[tp.Tuple[NGTTHeaderType, bytes, Future]]
        self.op_id_to_op = {}  # type: tp.Dict[int, Future]
        logger.info('NGTT starting up')
        self.start()

    def stop(self, wait_for_completion: bool = True):
        """
        Stop this thread and the connection

        :param wait_for_completion: whether to wait for thread to terminate
        """
        if self.stopped:
            return
        self.terminate()
        if wait_for_completion:
            self.join()
        self.stopped = True

    def close(self):
        self.stop()
        if self.current_connection is not None:
            self.current_connection.close()
            self.current_connection = None
            self.op_id_to_op = {}

    @property
    @silence_excs(AttributeError, returns=False)
    def connected(self) -> bool:
        return self.current_connection.connected

    def connect(self):
        if self.connected:
            return
        eb = ExponentialBackoff(1, 30, self.safe_sleep)
        while not self.terminating and not self.connected:
            try:
                self.current_connection = NGTTSocket(self.cert_file, self.key_file)
                self.current_connection.connect()
            except ConnectionFailed as e:
                logger.warning('Failure reconnecting', exc_info=e)
                eb.failed()
                eb.sleep()

        if self.terminating:
            return

        self.op_id_to_op = {}
        for h_type, data, fut in self.currently_running_ops:
            id_ = self.current_connection.id_assigner.allocate_int()
            self.current_connection.send_frame(id_, h_type, data)
            self.op_id_to_op[id_] = fut
        logger.debug('Successfully connected')

    @must_be_connected
    @for_argument(None, encode_data)
    def sync_pathpoints(self, data) -> Future:
        """
        Try to synchronize pathpoints.

        This will survive multiple reconnection attempts.

        :param data: exactly the same thing that you would submit to POST
        at POST https://api.smok.co/v1/device/
        :return: a Future telling you whether this succeeds or fails
        """
        fut = Future()
        fut.set_running_or_notify_cancel()
        tid = self.current_connection.id_assigner.allocate_int()
        self.currently_running_ops.append((NGTTHeaderType.DATA_STREAM, data, fut))
        try:
            self.current_connection.send_frame(tid, NGTTHeaderType.DATA_STREAM, data)
        except ConnectionFailed:
            self.cleanup()
            raise
        self.op_id_to_op[tid] = fut
        return fut

    def inner_loop(self):
        logger.debug('Inner loop')
        self.current_connection.try_ping()
        ccon = [self.current_connection]
        rx, wx, ex = select.select(ccon,
                                   ccon if self.current_connection.wants_write else [], [],
                                   timeout=5)
        if not rx:
            return
        if wx:
            self.current_connection.try_send()
        frame = self.current_connection.recv_frame()
        if frame is None:
            logger.debug('Received nothing')
            return
        logger.debug('Received %s', frame)
        if frame.packet_type == NGTTHeaderType.PING:
            self.current_connection.got_ping()
        elif frame.packet_type == NGTTHeaderType.ORDER:
            try:
                data = minijson.loads(frame.data)
            except ValueError:
                logger.error('Received invalid JSON over the wire')
                raise ConnectionFailed('Got invalid JSON')
            order = Order(data, frame.tid, self.current_connection)
            self.on_new_order(order)
        elif frame.packet_type in (
                NGTTHeaderType.DATA_STREAM_REJECT, NGTTHeaderType.DATA_STREAM_CONFIRM):
            if frame.tid in self.op_id_to_op:
                # Assume it's a data stream running
                fut = self.op_id_to_op.pop(frame.tid)

                index = index_of(x[2] == fut, self.currently_running_ops)
                del self.currently_running_ops[index]

                if frame.packet_type == NGTTHeaderType.DATA_STREAM_CONFIRM:
                    fut.set_result(None)
                elif frame.packet_type == NGTTHeaderType.DATA_STREAM_REJECT:
                    fut.set_exception(DataStreamSyncFailed())
        elif frame.packet_type == NGTTHeaderType.SYNC_BAOB_RESPONSE:
            if frame.tid in self.op_id_to_op:
                fut = self.op_id_to_op.pop(frame.tid)

                index = index_of(x[2] == fut, self.currently_running_ops)
                del self.currently_running_ops[index]

                fut.set_result(frame.real_data)

    def loop(self) -> None:
        try:
            while not self.connected and not self.terminating:
                self.connect()
                logger.debug('Reconnecting')
            if self.terminating:
                return
            self.inner_loop()
        except ConnectionFailed as e:
            logger.debug('Connection failed, retrying', exc_info=e)
            self.cleanup()
            try:
                self.connect()
            except ConnectionFailed:
                pass

    def cleanup(self):
        Optional(self.current_connection).close()
        self.current_connection = None

    @must_be_connected
    @for_argument(None, encode_data)
    def sync_baobs(self, baobs) -> Future:
        """
        Request to synchronize BAOBs

        :param baobs: a dictionary of locally kept BAOB name => local version (tp.Dict[str, int])
        :return: a Future that will receive a result of dict
        {"download": [.. list of BAOBs to download from the server ..],
         "upload": [.. list of BAOBs to upload to the server ..]}

        :raises ConnectionFailed: connection failed
        """
        fut = Future()
        fut.set_running_or_notify_cancel()
        tid = self.current_connection.id_assigner.allocate_int()
        self.currently_running_ops.append((NGTTHeaderType.SYNC_BAOB_REQUEST, baobs, fut))
        try:
            self.current_connection.send_frame(tid, NGTTHeaderType.SYNC_BAOB_REQUEST, baobs)
        except ConnectionFailed:
            self.cleanup()
            raise
        self.op_id_to_op[tid] = fut
        return fut

    @must_be_connected
    @for_argument(None, encode_data)
    def stream_logs(self, data: tp.List) -> None:
        """
        Stream logs to the server

        This will work on a best-effort basis.

        :param data: the same thing that you would PUT /v1/device/device_logs
        """
        try:
            self.current_connection.send_frame(0, NGTTHeaderType.LOGS, data)
        except ConnectionFailed:
            self.cleanup()
            raise
