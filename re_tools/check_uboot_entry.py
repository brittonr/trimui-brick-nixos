#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Analyze U-Boot dual-entry (AArch32/AArch64) header and branch targets.

Usage:
  python3 check_uboot_entry.py <u-boot-dtb.bin>
"""

import struct
import sys

uboot_path = sys.argv[1] if len(sys.argv) > 1 else "u-boot-dtb.bin"
with open(uboot_path, "rb") as f:
    uboot = f.read()

# Decode the AArch64 entry at +0x4
arm64_b = struct.unpack_from('<I', uboot, 4)[0]
off = (arm64_b & 0x3FFFFFF) << 2
print(f"+0x004: 0x{arm64_b:08x} = B +0x{off:x} → lands at offset 0x{4+off:x}")

# Look at what's at the AArch64 branch target
target = 4 + off
print(f"\nCode at AArch64 _start (offset 0x{target:x}):")
for i in range(target, min(target+64, len(uboot)), 4):
    v = struct.unpack_from('<I', uboot, i)[0]
    print(f"  +0x{i:03x}: 0x{v:08x}")

# Look at the ARM32 entry at +0x0
arm32_b = struct.unpack_from('<I', uboot, 0)[0]
off32 = ((arm32_b & 0xFFFFFF) << 2) + 8
print(f"\n+0x000: 0x{arm32_b:08x} = ARM32 B +0x{off32:x} → lands at offset 0x{off32:x}")
print(f"\nCode at ARM32 RMR trampoline target (offset 0x{off32:x}):")
for i in range(off32, min(off32+64, len(uboot)), 4):
    v = struct.unpack_from('<I', uboot, i)[0]
    print(f"  +0x{i:03x}: 0x{v:08x}")

# Check the header area between the two entries
print(f"\nHeader area +0x00 to +0x50:")
for i in range(0, 0x50, 4):
    v = struct.unpack_from('<I', uboot, i)[0]
    print(f"  +0x{i:03x}: 0x{v:08x}")

# What address does _start expect? Check for adr/adrp to CONFIG_TEXT_BASE
print(f"\nLooking for CONFIG_TEXT_BASE=0x4a000000 references in first 4KB...")
for i in range(0, min(4096, len(uboot)), 4):
    v = struct.unpack_from('<I', uboot, i)[0]
    # Check for MOV/MOVZ loading 0x4a00
    if (v & 0xFFE0001F) == 0xD2A94000:  # MOVZ Xn, #0x4a00, LSL#16
        rd = v & 0x1F
        print(f"  +0x{i:03x}: MOVZ X{rd}, #0x4a00, LSL#16 (0x4a000000)")
