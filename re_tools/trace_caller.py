#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Trace callers of the EP setup function in vendor ATF (monitor.bin)."""

import struct
import os

monitor_path = os.path.join(os.path.dirname(__file__), 'monitor.bin')
with open(monitor_path, 'rb') as f:
    data = f.read()

# The function at 0x140c is called from 0x17cc
# Let me find the function that contains 0x17cc and dump it
# Find the function start by looking backwards for a common prologue
print("=== Function containing 0x17cc (caller of 0x140c) ===")
for i in range(0x1780, 0x1840, 4):
    val = struct.unpack_from('<I', data, i)[0]
    desc = ""
    
    if (val >> 26) == 0b100101:  # BL
        off = (val & 0x3FFFFFF) << 2
        if off & 0x8000000: off -= 0x10000000
        target = i + off
        desc = f"BL 0x{target:x}"
    elif (val >> 26) == 0b000101:  # B
        off = (val & 0x3FFFFFF) << 2
        if off & 0x8000000: off -= 0x10000000
        target = i + off
        desc = f"B 0x{target:x}"
    elif val == 0xd65f03c0:
        desc = "RET"
    elif (val >> 24) & 0xFF == 0x54:
        cond = val & 0xF
        imm19 = (val >> 5) & 0x7FFFF
        if imm19 & 0x40000: imm19 -= 0x80000
        target = i + (imm19 << 2)
        conds = ['EQ','NE','CS','CC','MI','PL','VS','VC','HI','LS','GE','LT','GT','LE']
        desc = f"B.{conds[cond]} 0x{target:x}"
    elif (val >> 24) & 0x7F in [0x34, 0x35, 0xB4, 0xB5]:
        sf = (val >> 31) & 1
        op = (val >> 24) & 1
        imm19 = (val >> 5) & 0x7FFFF
        rt = val & 0x1F
        if imm19 & 0x40000: imm19 -= 0x80000
        target = i + (imm19 << 2)
        rn = f"X{rt}" if sf else f"W{rt}"
        desc = f"{'CBNZ' if op else 'CBZ'} {rn}, 0x{target:x}"
    elif (val >> 23) & 0x1FF == 0b101001010:  # MOVZ
        sf = (val >> 31) & 1
        hw = (val >> 21) & 3
        imm16 = (val >> 5) & 0xFFFF
        rd = val & 0x1F
        rn = f"X{rd}" if sf else f"W{rd}"
        desc = f"MOVZ {rn}, #0x{imm16:x}" + (f", LSL #{hw*16}" if hw else "")
    elif (val & 0xFFE0FFE0) == 0x2A0003E0:  # MOV Wn, Wm
        rd = val & 0x1F
        rm = (val >> 16) & 0x1F
        desc = f"MOV W{rd}, W{rm}"
    elif (val & 0xFFE0FFE0) == 0xAA0003E0:  # MOV Xn, Xm
        rd = val & 0x1F
        rm = (val >> 16) & 0x1F
        desc = f"MOV X{rd}, X{rm}"
    
    marker = " <-- CALLS 0x140c" if i == 0x17cc else ""
    print(f"  0x{i:04x}: 0x{val:08x}  {desc}{marker}")

# Now let me look at 0x17b0-0x17d0 area more closely to find who calls this
# and where X2 (execution state) comes from
print("\n=== Broader context 0x1740-0x17d0 ===")
for i in range(0x1740, 0x17d0, 4):
    val = struct.unpack_from('<I', data, i)[0]
    desc = ""
    if (val >> 26) == 0b100101:
        off = (val & 0x3FFFFFF) << 2
        if off & 0x8000000: off -= 0x10000000
        desc = f"BL 0x{i+off:x}"
    elif (val >> 26) == 0b000101:
        off = (val & 0x3FFFFFF) << 2
        if off & 0x8000000: off -= 0x10000000
        desc = f"B 0x{i+off:x}"
    elif (val & 0xFFE0FFE0) == 0x2A0003E0:
        rd = val & 0x1F
        rm = (val >> 16) & 0x1F
        desc = f"MOV W{rd}, W{rm}"
    elif (val & 0xFFE0FFE0) == 0xAA0003E0:
        rd = val & 0x1F
        rm = (val >> 16) & 0x1F
        desc = f"MOV X{rd}, X{rm}"
    elif (val >> 24) & 0x7F in [0x34, 0x35, 0xB4, 0xB5]:
        sf = (val >> 31) & 1
        op = (val >> 24) & 1
        imm19 = (val >> 5) & 0x7FFFF
        rt = val & 0x1F
        if imm19 & 0x40000: imm19 -= 0x80000
        target = i + (imm19 << 2)
        rn = f"X{rt}" if sf else f"W{rt}"
        desc = f"{'CBNZ' if op else 'CBZ'} {rn}, 0x{target:x}"
    # LDR Wn, [Xm, #imm]
    elif (val >> 22) & 0x3FF == 0x2E5:
        imm12 = (val >> 10) & 0xFFF
        rn = (val >> 5) & 0x1F
        rt = val & 0x1F
        desc = f"LDR W{rt}, [X{rn}, #0x{imm12*4:x}]"
    elif (val >> 22) & 0x3FF == 0x3E5:
        imm12 = (val >> 10) & 0xFFF
        rn = (val >> 5) & 0x1F
        rt = val & 0x1F
        desc = f"LDR X{rt}, [X{rn}, #0x{imm12*8:x}]"
    print(f"  0x{i:04x}: 0x{val:08x}  {desc}")
