import os
import socket
import ssl
import tempfile
import time
import typing as tp
from ssl import SSLContext, PROTOCOL_TLS_CLIENT, SSLError, CERT_REQUIRED

from satella.coding import silence_excs, rethrow_as, Closeable, wraps
from satella.coding.concurrent import IDAllocator
from satella.files import read_in_file

from .certificates import get_device_info, get_dev_ca_cert, get_root_cert
from ..exceptions import ConnectionFailed
from ..protocol import NGTTHeaderType, STRUCT_LHH, env_to_hostname, NGTTFrame

PING_INTERVAL_TIME = 30


def must_be_connected(fun):
    @wraps(fun)
    def outer(self, *args, **kwargs):
        if not self.connected:
            self.connect()
        return fun(self, *args, **kwargs)

    return outer


class NGTTSocket(Closeable):
    __slots__ = ('host', 'cert_file', 'key_file', 'socket',
                 'buffer', 'w_buffer', 'ping_id', 'last_read', 'connected',
                 'chain_file_name', 'id_assigner')

    @property
    def wants_write(self) -> bool:
        return bool(self.w_buffer)

    def __init__(self, cert_file: str, key_file: str):
        super().__init__()
        environment = get_device_info(read_in_file(cert_file))[1]
        self.host = env_to_hostname(environment)
        self.cert_file = cert_file
        self.key_file = key_file
        self.socket = None
        self.buffer = bytearray()
        self.w_buffer = bytearray()
        self.ping_id = None
        self.last_read = None
        self.connected = False

        with tempfile.NamedTemporaryFile('wb', delete=False) as chain_file:
            chain_file.write(read_in_file(self.cert_file))
            chain_file.write(b'\n')
            chain_file.write(get_dev_ca_cert())
            chain_file.write(b'\n')
            chain_file.write(get_root_cert())
        self.chain_file_name = chain_file.name
        self.id_assigner = IDAllocator(start_at=1)

    @rethrow_as(ssl.SSLError, ConnectionFailed)
    @silence_excs(ssl.SSLWantWriteError)
    @must_be_connected
    def send_frame(self, tid: int, header: NGTTHeaderType, data: bytes = b'') -> None:
        """
        Schedule a frame to be sent

        :param tid: transaction ID
        :param header: packet type
        :param data: data to send
        """
        if self.closed:
            return
        self.w_buffer.extend(STRUCT_LHH.pack(len(data), tid, header.value))
        self.w_buffer.extend(data)
        data_sent = self.socket.send(self.w_buffer)
        del self.w_buffer[:data_sent]

    @rethrow_as(ssl.SSLError, ConnectionFailed)
    @silence_excs(ssl.SSLWantWriteError)
    @must_be_connected
    def try_send(self):
        """
        Try to send some data
        """
        if self.w_buffer:
            try:
                data_sent = self.socket.send(self.w_buffer)
                del self.w_buffer[:data_sent]
            except socket.timeout:
                return

    @must_be_connected
    def try_ping(self):
        if time.monotonic() - self.last_read > PING_INTERVAL_TIME and self.ping_id is None:
            self.ping_id = self.id_assigner.allocate_int()
            self.send_frame(self.ping_id, NGTTHeaderType.PING, b'')

    @must_be_connected
    def got_ping(self):
        if self.ping_id is not None:
            self.id_assigner.mark_as_free(self.ping_id)
            self.ping_id = None

    def fileno(self) -> int:
        return self.socket.fileno()

    @rethrow_as(ssl.SSLError, ConnectionFailed)
    @silence_excs(ssl.SSLWantReadError)
    @must_be_connected
    def recv_frame(self) -> tp.Optional[NGTTFrame]:
        """
        Receive a frame from remote socket

        :raises ConnectionFailed: connection closed
        :return: a tuple of transaction ID, header type, data
        """
        data = self.socket.recv(512)
        if not data:
            raise ConnectionFailed()
        self.last_read = time.monotonic()
        self.buffer.extend(data)
        if len(self.buffer) > STRUCT_LHH.size:
            length, tid, h_type = STRUCT_LHH.unpack(self.buffer[:STRUCT_LHH.size])
            if len(self.buffer) < STRUCT_LHH.size + length:
                return
            data = self.buffer[STRUCT_LHH.size:STRUCT_LHH.size + length]
            del self.buffer[:STRUCT_LHH.size + length]
            return NGTTFrame(tid, NGTTHeaderType(h_type), data)

    def close(self, wait_for_me: bool = True):
        if super().close():
            self.disconnect()
            os.unlink(self.chain_file_name)

    def disconnect(self):
        """
        Disconnect from the remote host
        """
        if self.socket is not None:
            self.socket.close()
            self.socket = None
            self.connected = False

    def connect(self):
        """
        Connect to remote host

        :raises SSLError: an error occurred
        """
        if self.connected:
            return
        ssl_context = SSLContext(PROTOCOL_TLS_CLIENT)
        ssl_context.load_verify_locations(cadata=get_root_cert().decode('utf-8'))
        ssl_context.load_cert_chain(self.chain_file_name, self.key_file)
        ssl_context.verify_mode = CERT_REQUIRED
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        ssl_sock = ssl_context.wrap_socket(sock, server_hostname=self.host)
        with rethrow_as((socket.error, SSLError), ConnectionFailed):
            ssl_sock.connect((self.host, 2408))
            ssl_sock.do_handshake()
        self.socket = ssl_sock
        self.socket.setblocking(False)
        self.last_read = time.monotonic()
        self.buffer = bytearray()
        self.w_buffer = bytearray()
        self.connected = True
