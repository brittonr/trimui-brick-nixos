#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Verify a modified boot_package against the original vendor boot_package.

Compares item magic strings, headers, and directory entries to ensure
the modified package preserves the vendor container format that boot0
validates during loading.

Usage:
  python3 verify_boot_package.py <original.bin> <modified.bin>
"""

import struct
import sys

def verify(orig_path, mod_path):
    orig = open(orig_path, 'rb').read()
    mod = open(mod_path, 'rb').read()

    # Check all items' magic strings
    items = [
        ("u-boot",  0x000800, 0x0C0000),
        ("monitor", 0x0C0800, 0x01130C),
        ("scp",     0x0D1C00, 0x014008),
        ("dtb",     0x0E6000, 0x025600),
    ]

    for name, offset, length in items:
        orig_magic = orig[offset+4:offset+16]
        mod_magic = mod[offset+4:offset+16]
        orig_first = struct.unpack_from('<I', orig, offset)[0]
        mod_first = struct.unpack_from('<I', mod, offset)[0]
        match = orig[offset:offset+length] == mod[offset:offset+length]
        print(f"{name:10s} @ 0x{offset:06x}: orig_magic={orig_magic} mod_magic={mod_magic} first=0x{mod_first:08x} data_match={match}")

    # Check package header
    print(f"\nPackage header comparison:")
    for off in range(0, 0x40, 4):
        ov = struct.unpack_from('<I', orig, off)[0]
        mv = struct.unpack_from('<I', mod, off)[0]
        marker = " <-- DIFFERENT" if ov != mv else ""
        print(f"  +0x{off:02x}: orig=0x{ov:08x}  mod=0x{mv:08x}{marker}")

    # Check item directory entries
    print(f"\nItem directory entries:")
    ITEM_DIR_SIZE = 0x170
    for i in range(4):
        base = 0x40 + i * ITEM_DIR_SIZE
        for off in range(0, 0x60, 4):
            ov = struct.unpack_from('<I', orig, base+off)[0]
            mv = struct.unpack_from('<I', mod, base+off)[0]
            if ov != 0 or mv != 0:
                marker = " <-- DIFFERENT" if ov != mv else ""
                print(f"  item[{i}]+0x{off:02x}: orig=0x{ov:08x}  mod=0x{mv:08x}{marker}")

    # Check u-boot item header area
    print(f"\nU-boot item header (0x800-0x840):")
    for off in range(0, 0x40, 4):
        ov = struct.unpack_from('<I', orig, 0x800+off)[0]
        mv = struct.unpack_from('<I', mod, 0x800+off)[0]
        marker = " <-- DIFFERENT" if ov != mv else ""
        print(f"  +0x{off:02x}: orig=0x{ov:08x}  mod=0x{mv:08x}{marker}")


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <original_boot_package.bin> <modified_boot_package.bin>")
        sys.exit(1)
    verify(sys.argv[1], sys.argv[2])
