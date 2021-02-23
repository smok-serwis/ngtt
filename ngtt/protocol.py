import typing as tp
import struct

from satella.coding.structures import HashableIntEnum
from .exceptions import InvalidFrame


class NGTTHeaderType(HashableIntEnum):
    PING = 0        #: a PING, sent by the client, server responds alike
    ORDER = 1       #: sent by the server
    ORDER_CONFIRM = 2   #: sent by the client
    LOGS = 3            #: sent by the client
    DATA_STREAM = 4             #: sent by the client
    DATA_STREAM_CONFIRM = 5     #: sent by the server
    DATA_STREAM_REJECT = 6      #: sent by the server
    SYNC_BAOB_REQUEST = 7       #: sent by the client
    SYNC_BAOB_RESPONSE = 8      #: sent by the server


STRUCT_LHH = struct.Struct('>LHH')


class NGTTFrame:
    def __init__(self, tid: int, packet_type: NGTTHeaderType, data: bytes):
        self.tid = tid
        self.packet_type = packet_type
        self.data = data

    def __len__(self):
        return STRUCT_LHH.size + len(self.data)

    def __bytes__(self):
        return STRUCT_LHH.pack(len(self.data), self.tid, self.packet_type.value)

    @classmethod
    def from_bytes(cls, b: tp.Union[bytes, bytearray]) -> 'NGTTFrame':
        length, tid, h_type = STRUCT_LHH.unpack(b[:STRUCT_LHH.size])
        if h_type > NGTTHeaderType.DATA_STREAM_REJECT.value:
            raise InvalidFrame('Unrecognized packet type %s' % (h_type, ))
        return NGTTFrame(tid, NGTTHeaderType(h_type), b[STRUCT_LHH.size:STRUCT_LHH.size+length])


def env_to_hostname(env: int) -> str:
    if env == 0:
        return 'api.smok.co'
    elif env == 1:
        return 'api.test.smok-serwis.pl'
    else:
        return 'http-api'
