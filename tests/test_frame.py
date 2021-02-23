import unittest

from ngtt.protocol import NGTPHeaderType, NGTTFrame

class TestFrame(unittest.TestCase):g
    def test_frame(self):
        b = '\x00\x00\x00\x02\x00\x01\x00\x00AL'
        frame = NGTTFrame.from_bytes(b)
        self.assertEqual(frame.tid, 1)
        self.assertEqual(frame.packet_type, NGTPHeaderType.ORDER)
        self.assertEqual(frame.data, b'AL')
        self.assertEqual(len(frame), len(b))