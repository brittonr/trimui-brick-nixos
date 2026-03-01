#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Analyze vendor ATF (monitor.bin) binary — search for SPSR, SCR, ERET patterns."""

import struct
import os

monitor_path = os.path.join(os.path.dirname(__file__), 'monitor.bin')
with open(monitor_path, 'rb') as f:
    data = f.read()

# First word is ARM branch instruction
branch = struct.unpack_from('<I', data, 0)[0]
print(f'Branch instruction: 0x{branch:08x}')
offset = ((branch & 0x00FFFFFF) << 2) + 8
print(f'Branch target offset: 0x{offset:x}')

# Look at item sub-header
print(f'Magic: {data[4:16]}')
print(f'Offset 0x0c: 0x{struct.unpack_from("<I", data, 0x0c)[0]:08x}')
print(f'Offset 0x10: 0x{struct.unpack_from("<I", data, 0x10)[0]:08x}')
print(f'Offset 0x14: 0x{struct.unpack_from("<I", data, 0x14)[0]:08x}')
print(f'Offset 0x18: 0x{struct.unpack_from("<I", data, 0x18)[0]:08x}')
print(f'Offset 0x1c: {data[0x1c:0x2c]}')
print(f'Offset 0x2c: 0x{struct.unpack_from("<I", data, 0x2c)[0]:08x}')

# Search for 0x1d3 pattern in the binary
print(f'\nSearching for SPSR value 0x1d3...')
for i in range(0, len(data)-3, 4):
    val = struct.unpack_from('<I', data, i)[0]
    if val == 0x1d3:
        print(f'  Exact 0x000001d3 at offset 0x{i:x}')
    elif val & 0xFFFF == 0x01d3 and val != 0:
        print(f'  Lower 16-bit match at offset 0x{i:x}: 0x{val:08x}')

# Also search byte-level for 0xd3 0x01 (little-endian 0x1d3)
print(f'\nByte-level search for d3 01...')
for i in range(len(data)-1):
    if data[i] == 0xd3 and data[i+1] == 0x01:
        context = data[max(0,i-2):i+6].hex()
        print(f'  Found at offset 0x{i:x}: ...{context}...')

# The ATF code is AArch64 (it runs at EL3). Search for movz/movk instructions
# that load 0x1d3 into a register for SPSR_EL3
# AArch64 MOVZ encoding: 0xD2800000 | (imm16 << 5) | Rd
# For 0x1d3: MOVZ Xn, #0x1d3 = 0xD2803A60 | Rd
print(f'\nSearching for AArch64 MOV #0x1d3 instructions...')
for i in range(0, len(data)-3, 4):
    val = struct.unpack_from('<I', data, i)[0]
    # MOVZ Wn, #0x1d3 = 0x52803A6n
    # MOVZ Xn, #0x1d3 = 0xD2803A6n
    if (val & 0xFFFFFFE0) == 0x52803A60:
        rd = val & 0x1F
        print(f'  MOVZ W{rd}, #0x1d3 at offset 0x{i:x}: 0x{val:08x}')
    if (val & 0xFFFFFFE0) == 0xD2803A60:
        rd = val & 0x1F
        print(f'  MOVZ X{rd}, #0x1d3 at offset 0x{i:x}: 0x{val:08x}')

# Also search for MSR SPSR_EL3, Xn instructions
# MSR SPSR_EL3, Xt = 0xD51E4000 | Xt
print(f'\nSearching for MSR SPSR_EL3, Xn instructions...')
for i in range(0, len(data)-3, 4):
    val = struct.unpack_from('<I', data, i)[0]
    if (val & 0xFFFFFFE0) == 0xD51E4000:
        rt = val & 0x1F
        print(f'  MSR SPSR_EL3, X{rt} at offset 0x{i:x}: 0x{val:08x}')
    # Also MRS Xt, SPSR_EL3 = 0xD53E4000 | Xt
    if (val & 0xFFFFFFE0) == 0xD53E4000:
        rt = val & 0x1F
        print(f'  MRS X{rt}, SPSR_EL3 at offset 0x{i:x}: 0x{val:08x}')

# Search for MSR ELR_EL3, Xn = 0xD51E4020 | Xt
print(f'\nSearching for MSR ELR_EL3, Xn instructions...')
for i in range(0, len(data)-3, 4):
    val = struct.unpack_from('<I', data, i)[0]
    if (val & 0xFFFFFFE0) == 0xD51E4020:
        rt = val & 0x1F
        print(f'  MSR ELR_EL3, X{rt} at offset 0x{i:x}: 0x{val:08x}')

# Search for ERET instruction = 0xD69F03E0
print(f'\nSearching for ERET instructions...')
for i in range(0, len(data)-3, 4):
    val = struct.unpack_from('<I', data, i)[0]
    if val == 0xD69F03E0:
        print(f'  ERET at offset 0x{i:x}')
        # Show context around ERET
        for j in range(max(0, i-32), min(len(data)-3, i+8), 4):
            v = struct.unpack_from('<I', data, j)[0]
            marker = ' <-- ERET' if j == i else ''
            print(f'    0x{j:x}: 0x{v:08x}{marker}')
