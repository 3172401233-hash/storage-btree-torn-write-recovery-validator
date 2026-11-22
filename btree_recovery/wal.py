"""Write-ahead log frames.

Each frame carries a full after-image of the page it updates, which makes
replay idempotent: applying the same frame twice is a no-op. A frame is
only usable if its own checksum is valid and its LSN is at or below the
WAL durability boundary (frames past it belong to a truncated tail and
were never persisted).
"""

import struct
import zlib
from dataclasses import dataclass

FRAME_META_STRUCT = struct.Struct("<QQQ")  # lsn, page_id, prev_page_lsn


@dataclass
class WALFrame:
    lsn: int
    page_id: int
    prev_page_lsn: int
    image: bytes
    checksum: int = 0

    def compute_checksum(self) -> int:
        meta = FRAME_META_STRUCT.pack(self.lsn, self.page_id,
                                      self.prev_page_lsn)
        return zlib.crc32(meta + self.image) & 0xFFFFFFFF

    @property
    def valid(self) -> bool:
        return self.checksum == self.compute_checksum()


def build_frame(lsn, page_id, prev_page_lsn, image):
    frame = WALFrame(lsn=lsn, page_id=page_id,
                     prev_page_lsn=prev_page_lsn, image=image)
    frame.checksum = frame.compute_checksum()
    return frame
