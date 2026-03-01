#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Investigate boot_package checksum algorithm.

Tests various checksum approaches against the vendor boot_package to
determine how the BROM/boot0 validates the package integrity.

Result: The checksum at +0x14 uses the BROM stamp algorithm —
sum of all 32-bit words over total_len bytes, with the checksum
field itself set to BROM_STAMP (0x5f0a6c39) during computation.

Usage:
  python3 crack_checksum.py <boot_package.bin>
"""

import struct
import binascii
import sys


def crack(path):
    orig = open(path, 'rb').read()

    stored_10 = struct.unpack_from('<I', orig, 0x10)[0]
    stored_14 = struct.unpack_from('<I', orig, 0x14)[0]
    total_len = struct.unpack_from('<I', orig, 0x24)[0]

    print(f"Stored 0x10 (magic): 0x{stored_10:08x}")
    print(f"Stored 0x14 (checksum): 0x{stored_14:08x}")
    print(f"Total len: 0x{total_len:x}")
    print()

    # BROM stamp algorithm: sum with 0x14 = BROM_STAMP during computation
    BROM_STAMP = 0x5f0a6c39
    d = bytearray(orig[:total_len])
    struct.pack_into('<I', d, 0x14, BROM_STAMP)
    s = 0
    for i in range(0, len(d), 4):
        s = (s + struct.unpack_from('<I', d, i)[0]) & 0xFFFFFFFF
    print(f"BROM stamp sum: 0x{s:08x} match_14={s == stored_14}")

    # Alternative: sum with 0x10 zeroed
    d = bytearray(orig[:total_len])
    struct.pack_into('<I', d, 0x10, 0)
    s = 0
    for i in range(0, len(d), 4):
        s = (s + struct.unpack_from('<I', d, i)[0]) & 0xFFFFFFFF
    print(f"Sum (zero 0x10 only, total_len): 0x{s:08x} match_10={s == stored_10}")

    # Alternative: sum with both zeroed
    d = bytearray(orig[:total_len])
    struct.pack_into('<I', d, 0x10, 0)
    struct.pack_into('<I', d, 0x14, 0)
    s = 0
    for i in range(0, len(d), 4):
        s = (s + struct.unpack_from('<I', d, i)[0]) & 0xFFFFFFFF
    print(f"Sum (zero 0x10+0x14, total_len): 0x{s:08x} match_10={s == stored_10}")

    # CRC32
    print(f"\nCRC32 (total_len): 0x{binascii.crc32(bytearray(orig[:total_len])):08x}")


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <boot_package.bin>")
        sys.exit(1)
    crack(sys.argv[1])
