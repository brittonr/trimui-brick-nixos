#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Find SCR_EL3 read/write locations in vendor ATF (monitor.bin)."""

import struct
import os

monitor_path = os.path.join(os.path.dirname(__file__), 'monitor.bin')
with open(monitor_path, 'rb') as f:
    data = f.read()

# SCR_EL3 = S3_6_C1_C1_0 
# MSR SCR_EL3, Xt = 0xD51E1100 | Xt
# MRS Xt, SCR_EL3 = 0xD53E1100 | Xt
print("=== MSR/MRS SCR_EL3 instructions ===")
for i in range(0, len(data)-3, 4):
    val = struct.unpack_from('<I', data, i)[0]
    if (val & 0xFFFFFFE0) == 0xD51E1100:
        rt = val & 0x1F
        print(f"  MSR SCR_EL3, X{rt} at 0x{i:x}")
        # Show context
        for j in range(max(0, i-40), min(len(data)-3, i+8), 4):
            v = struct.unpack_from('<I', data, j)[0]
            m = " <--" if j == i else ""
            print(f"    0x{j:04x}: 0x{v:08x}{m}")
    if (val & 0xFFFFFFE0) == 0xD53E1100:
        rt = val & 0x1F
        print(f"  MRS X{rt}, SCR_EL3 at 0x{i:x}")

# Also look at what happens around offset 0x268 and 0x280 (callers of the getter functions)
print("\n=== Context around 0x250-0x2b0 (early init, callers of SPSR getters) ===")
for i in range(0x240, 0x2c0, 4):
    val = struct.unpack_from('<I', data, i)[0]
    desc = ""
    if (val >> 26) == 0b100101:  # BL
        off = (val & 0x3FFFFFF) << 2
        if off & 0x8000000: off -= 0x10000000
        target = i + off
        desc = f"BL 0x{target:x}"
    elif val == 0xd65f03c0:
        desc = "RET"
    elif (val >> 23) & 0x1FF == 0b101001010:
        imm16 = (val >> 5) & 0xFFFF
        rd = val & 0x1F
        desc = f"MOVZ W{rd}, #0x{imm16:x}"
    print(f"  0x{i:04x}: 0x{val:08x}  {desc}")

# Look for where the function containing 0x1450 is called
# The function starts at 0x140c. Search for BL to 0x140c
print("\n=== Callers of function at 0x140c ===")
for i in range(0, len(data)-3, 4):
    val = struct.unpack_from('<I', data, i)[0]
    if (val >> 26) == 0b100101:  # BL
        off = (val & 0x3FFFFFF) << 2
        if off & 0x8000000: off -= 0x10000000
        target = i + off
        if target == 0x140c:
            print(f"  BL from 0x{i:x}")
            # Show some context
            for j in range(max(0, i-16), min(len(data)-3, i+4), 4):
                v = struct.unpack_from('<I', data, j)[0]
                m = " <-- BL" if j == i else ""
                print(f"    0x{j:04x}: 0x{v:08x}{m}")
