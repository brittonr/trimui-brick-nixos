#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Disassemble ATF AArch32/AArch64 execution state switch logic."""

import struct
import os

monitor_path = os.path.join(os.path.dirname(__file__), 'monitor.bin')
with open(monitor_path, 'rb') as f:
    data = f.read()

# Disassemble the area around 0x1420-0x14a0 to understand the switch logic
print("=== Detailed context 0x1400-0x14a0 ===")
for i in range(0x1400, 0x14a0, 4):
    val = struct.unpack_from('<I', data, i)[0]
    
    # Try to decode common AArch64 instructions
    desc = ""
    
    # B imm
    if (val >> 26) == 0b000101:
        off = (val & 0x3FFFFFF) << 2
        if off & 0x8000000: off -= 0x10000000
        desc = f"B 0x{i+off:x}"
    # BL imm
    elif (val >> 26) == 0b100101:
        off = (val & 0x3FFFFFF) << 2
        if off & 0x8000000: off -= 0x10000000
        desc = f"BL 0x{i+off:x}"
    # CBZ/CBNZ
    elif (val >> 24) & 0x7F in [0x34, 0x35, 0xB4, 0xB5]:
        sf = (val >> 31) & 1
        op = (val >> 24) & 1
        imm19 = (val >> 5) & 0x7FFFF
        rt = val & 0x1F
        if imm19 & 0x40000: imm19 -= 0x80000
        target = i + (imm19 << 2)
        rn = f"X{rt}" if sf else f"W{rt}"
        desc = f"{'CBNZ' if op else 'CBZ'} {rn}, 0x{target:x}"
    # B.cond
    elif (val >> 24) & 0xFF == 0x54:
        cond = val & 0xF
        imm19 = (val >> 5) & 0x7FFFF
        if imm19 & 0x40000: imm19 -= 0x80000
        target = i + (imm19 << 2)
        conds = ['EQ','NE','CS','CC','MI','PL','VS','VC','HI','LS','GE','LT','GT','LE','AL','NV']
        desc = f"B.{conds[cond]} 0x{target:x}"
    # MOVZ
    elif (val >> 23) & 0x1FF == 0b101001010:
        sf = (val >> 31) & 1
        hw = (val >> 21) & 3
        imm16 = (val >> 5) & 0xFFFF
        rd = val & 0x1F
        shift = hw * 16
        rn = f"X{rd}" if sf else f"W{rd}"
        desc = f"MOVZ {rn}, #0x{imm16:x}" + (f", LSL #{shift}" if shift else "")
    # STR/LDR (unsigned offset)
    elif (val >> 22) & 0x3FF in [0x3E4, 0x3E5]:  # 64-bit STR/LDR
        op = (val >> 22) & 1
        imm12 = (val >> 10) & 0xFFF
        rn = (val >> 5) & 0x1F
        rt = val & 0x1F
        off = imm12 * 8
        desc = f"{'LDR' if op else 'STR'} X{rt}, [X{rn}, #0x{off:x}]"
    # STR/LDR 32-bit
    elif (val >> 22) & 0x3FF in [0x2E4, 0x2E5]:
        op = (val >> 22) & 1
        imm12 = (val >> 10) & 0xFFF
        rn = (val >> 5) & 0x1F
        rt = val & 0x1F
        off = imm12 * 4
        desc = f"{'LDR' if op else 'STR'} W{rt}, [X{rn}, #0x{off:x}]"
    # CMP (SUBS XZR)
    elif (val & 0xFF00001F) == 0xF100001F:
        imm12 = (val >> 10) & 0xFFF
        rn = (val >> 5) & 0x1F
        desc = f"CMP X{rn}, #0x{imm12:x}"
    # RET
    elif val == 0xd65f03c0:
        desc = "RET"
    # ORR (bitmask immediate)
    elif (val >> 23) & 0x1FF == 0b101100100:
        rd = val & 0x1F
        rn = (val >> 5) & 0x1F
        desc = f"ORR X{rd}, X{rn}, #imm (bitmask)"
    
    marker = ""
    if val == 0x52803a60:
        marker = " <-- MOVZ W0, #0x1d3"
    elif val == 0x528078a0:
        marker = " <-- MOVZ W0, #0x3c5 (AArch64!)"
    
    print(f"  0x{i:04x}: 0x{val:08x}  {desc}{marker}")

# Also show the two small functions
print("\n=== Function at 0x133c ===")
for i in range(0x1330, 0x1350, 4):
    val = struct.unpack_from('<I', data, i)[0]
    desc = ""
    if val == 0xd65f03c0: desc = "RET"
    elif val == 0x52803a60: desc = "MOVZ W0, #0x1d3"
    print(f"  0x{i:04x}: 0x{val:08x}  {desc}")

# Check what calls the functions at 0x133c and 0x1344
# Search for BL instructions targeting 0x133c and 0x1344
print("\n=== Callers of 0x133c ===")
for i in range(0, len(data)-3, 4):
    val = struct.unpack_from('<I', data, i)[0]
    if (val >> 26) == 0b100101:  # BL
        off = (val & 0x3FFFFFF) << 2
        if off & 0x8000000: off -= 0x10000000
        target = i + off
        if target == 0x133c:
            print(f"  BL from 0x{i:x}")
        elif target == 0x1344:
            print(f"  BL to 0x1344 from 0x{i:x}")
