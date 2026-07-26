import struct
import zlib

from .errors import ProtocolError


def png_size(data: bytes) -> tuple[int, int]:
    # A truncated capture -- a partial read from a device -- can carry a valid
    # signature and still stop before the dimensions, which unpacking reports
    # as a struct error rather than as the bad capture it is.
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ProtocolError("capture did not return a PNG")
    return struct.unpack(">II", data[16:24])


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    if len(rgb) != width * height * 3:
        raise ProtocolError("RGB framebuffer size does not match dimensions")

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return (
            struct.pack(">I", len(payload))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    stride = width * 3
    rows = b"".join(
        b"\x00" + rgb[offset : offset + stride] for offset in range(0, len(rgb), stride)
    )
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 6))
        + chunk(b"IEND", b"")
    )
