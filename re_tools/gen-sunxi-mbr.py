#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Generate a minimal sunxi-MBR for TrimUI Brick.

The sunxi-MBR is a 16KB structure that the vendor U-Boot uses to find
partitions by name (especially the "boot" partition for boot_normal).

Format:
  Header (64 bytes):
    +0x00: CRC32 (4 bytes)
    +0x04: version (4 bytes) = 0x00000200
    +0x08: magic (8 bytes) = "softw411"
    +0x10: reserved
    +0x18: partition count (4 bytes)
    +0x20: reserved
    +0x24: stamp_start (4 bytes) - first partition start sector
    +0x28: reserved
    +0x2c: stamp_size (4 bytes) - first partition size sectors
    +0x30: class_name "DISK" (16 bytes)

  Partition entries (128 bytes each, starting at +0x40):
    +0x00: name (16 bytes, null terminated)
    +0x10: class (4 bytes) - 0x00008000 = normal, 0x00008100 = auto-expand
    +0x20-0x5f: reserved
    +0x60: reserved
    +0x64: addr_lo - start sector of NEXT partition (4 bytes)
    +0x68: reserved
    +0x6c: len_lo - size of NEXT partition in sectors (4 bytes)
    +0x70: class_name "DISK" (16 bytes)
"""

import struct
import sys
import zlib

# Partition layout (in 512-byte sectors, absolute from disk start)
PARTITIONS = [
    # (name, start_sector, size_sectors, class_flag)
    ("bootloader",  1024,   49152,  0x00008000),  # 0.5MB-24.5MB (boot_package area)
    ("env",         50176,  1024,   0x00008000),  # 24.5MB - U-Boot environment
    ("env-redund",  51200,  1024,   0x00008000),  # 25.0MB - redundant env
    ("boot",        52224,  262144, 0x00008000),  # 25.5MB - 128MB Android boot.img
    ("rootfs",      315392, 0,      0x00008100),  # ~154MB+ - auto-expand
]

MBR_SIZE = 16 * 1024  # 16KB


def build_sunxi_mbr():
    buf = bytearray(MBR_SIZE)

    # Header
    struct.pack_into('<I', buf, 0x04, 0x00000200)  # version
    buf[0x08:0x10] = b'softw411'                   # magic
    struct.pack_into('<I', buf, 0x18, len(PARTITIONS))  # partition count

    # First partition info in header
    struct.pack_into('<I', buf, 0x24, PARTITIONS[0][1])  # stamp_start
    struct.pack_into('<I', buf, 0x2c, PARTITIONS[0][2])  # stamp_size
    buf[0x30:0x34] = b'DISK'

    # Partition entries
    for i, (name, start, size, cls) in enumerate(PARTITIONS):
        base = 0x40 + i * 0x80

        # Name (16 bytes)
        name_bytes = name.encode('ascii')[:15]
        buf[base:base + len(name_bytes)] = name_bytes

        # Class flags
        struct.pack_into('<I', buf, base + 0x10, cls)

        # Next partition info (or zeros for last)
        if i + 1 < len(PARTITIONS):
            next_start = PARTITIONS[i + 1][1]
            next_size = PARTITIONS[i + 1][2]
        else:
            next_start = 0
            next_size = 0

        struct.pack_into('<I', buf, base + 0x64, next_start)
        struct.pack_into('<I', buf, base + 0x6c, next_size)
        buf[base + 0x70:base + 0x74] = b'DISK'

    # CRC32 over everything except the CRC field itself
    crc = zlib.crc32(bytes(buf[4:])) & 0xFFFFFFFF
    struct.pack_into('<I', buf, 0x00, crc)

    return bytes(buf)


if __name__ == '__main__':
    sys.stdout.buffer.write(build_sunxi_mbr())
