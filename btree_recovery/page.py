"""B-tree page layout, checksums and (de)serialisation.

Page format assumptions (see README.md):

* Fixed page size of ``PAGE_SIZE`` bytes.
* A fixed 42-byte header followed by the slot directory (uint16 offsets),
  then the record body. The remaining bytes are zero padding.
* The checksum is a CRC32 over the whole page with the checksum field
  itself zeroed. A torn (half-written) page therefore always fails
  checksum verification.
"""

import struct
import zlib
from dataclasses import dataclass, field
from typing import Optional, Tuple

PAGE_SIZE = 8192
NULL_PAGE = 0xFFFFFFFF

# page_id(I) page_lsn(Q) checksum(I) flags(I) left(I) right(I) parent(I)
# parent_pointer_lsn(Q) slot_count(H)
HEADER_STRUCT = struct.Struct("<IQIIIIIQH")
CHECKSUM_OFFSET = 12
SLOT_STRUCT = struct.Struct("<H")


def _encode_page_no(value):
    return NULL_PAGE if value is None else value


def _decode_page_no(value):
    return None if value == NULL_PAGE else value


@dataclass
class PageHeader:
    page_id: int
    page_lsn: int
    checksum: int = 0
    flags: int = 0
    left_sibling: Optional[int] = None
    right_sibling: Optional[int] = None
    parent_page: Optional[int] = None
    parent_pointer_lsn: int = 0
    slots: Tuple[int, ...] = field(default_factory=tuple)


def compute_checksum(raw: bytes) -> int:
    """CRC32 over the page with the checksum field zeroed."""
    buf = bytearray(raw)
    struct.pack_into("<I", buf, CHECKSUM_OFFSET, 0)
    return zlib.crc32(bytes(buf)) & 0xFFFFFFFF


def verify_checksum(raw: bytes) -> bool:
    if len(raw) != PAGE_SIZE:
        return False
    stored = struct.unpack_from("<I", raw, CHECKSUM_OFFSET)[0]
    return stored == compute_checksum(raw)


def parse_header(raw: bytes) -> PageHeader:
    (page_id, page_lsn, checksum, flags, left, right, parent,
     parent_pointer_lsn, slot_count) = HEADER_STRUCT.unpack_from(raw, 0)
    slots = []
    offset = HEADER_STRUCT.size
    for _ in range(slot_count):
        slots.append(SLOT_STRUCT.unpack_from(raw, offset)[0])
        offset += SLOT_STRUCT.size
    return PageHeader(
        page_id=page_id,
        page_lsn=page_lsn,
        checksum=checksum,
        flags=flags,
        left_sibling=_decode_page_no(left),
        right_sibling=_decode_page_no(right),
        parent_page=_decode_page_no(parent),
        parent_pointer_lsn=parent_pointer_lsn,
        slots=tuple(slots),
    )


def build_page(page_id, page_lsn, flags=0, left_sibling=None,
               right_sibling=None, parent_page=None, parent_pointer_lsn=0,
               slots=(), body=b""):
    """Serialise a page and stamp a valid checksum."""
    slots = tuple(slots)
    header = HEADER_STRUCT.pack(
        page_id, page_lsn, 0, flags,
        _encode_page_no(left_sibling), _encode_page_no(right_sibling),
        _encode_page_no(parent_page), parent_pointer_lsn, len(slots),
    )
    header += b"".join(SLOT_STRUCT.pack(s) for s in slots)
    raw = bytearray(PAGE_SIZE)
    raw[:len(header)] = header
    end = len(header) + len(body)
    if end > PAGE_SIZE:
        raise ValueError("page body does not fit")
    raw[len(header):end] = body
    struct.pack_into("<I", raw, CHECKSUM_OFFSET, compute_checksum(bytes(raw)))
    return bytes(raw)


class Page:
    """A raw page image plus its parsed header."""

    def __init__(self, raw: bytes):
        if len(raw) != PAGE_SIZE:
            raise ValueError("page image must be exactly PAGE_SIZE bytes")
        self.raw = raw
        self.header = parse_header(raw)

    @property
    def valid(self) -> bool:
        return verify_checksum(self.raw)

    @property
    def body(self) -> bytes:
        start = HEADER_STRUCT.size + SLOT_STRUCT.size * len(self.header.slots)
        return self.raw[start:]

    def with_links(self, left_sibling=None, right_sibling=None) -> "Page":
        """Return a repaired copy with updated sibling pointers."""
        h = self.header
        return Page(build_page(
            h.page_id, h.page_lsn, flags=h.flags,
            left_sibling=h.left_sibling if left_sibling is None else left_sibling,
            right_sibling=h.right_sibling if right_sibling is None else right_sibling,
            parent_page=h.parent_page,
            parent_pointer_lsn=h.parent_pointer_lsn,
            slots=h.slots,
            body=self.body,
        ))
