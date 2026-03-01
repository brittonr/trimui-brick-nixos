#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Disassemble regions of vendor ATF (monitor.bin) — MOVZ #0x1d3, SPSR strings."""

import struct
import os

monitor_path = os.path.join(os.path.dirname(__file__), 'monitor.bin')
with open(monitor_path, 'rb') as f:
    data = f.read()

# The monitor loads at 0x48000000, code starts at offset 0x100 (branch target)
# But the header takes some space. Let's look at context around the MOVZ instructions

locations = [0x133c, 0x1344, 0x1450, 0x9cb0]

for loc in locations:
    print(f'\n=== Context around 0x{loc:x} (load addr 0x{0x48000000+loc:08x}) ===')
    start = max(0, loc - 48)
    end = min(len(data)-3, loc + 48)
    for i in range(start, end, 4):
        val = struct.unpack_from('<I', data, i)[0]
        marker = ' <-- MOVZ #0x1d3' if i == loc else ''
        print(f'  0x{i:04x} (0x{0x48000000+i:08x}): 0x{val:08x}{marker}')

# Let's also look for the "Next image spsr" string to find the print location
print(f'\n=== Searching for "spsr" string ===')
idx = data.find(b'spsr')
while idx >= 0:
    # Show surrounding string
    start = max(0, idx - 32)
    end = min(len(data), idx + 48)
    print(f'  Found at 0x{idx:x}: {data[start:end]}')
    idx = data.find(b'spsr', idx + 1)

# Search for "Next image" string
print(f'\n=== Searching for "Next image" string ===')
idx = data.find(b'Next image')
while idx >= 0:
    end = min(len(data), idx + 64)
    print(f'  Found at 0x{idx:x}: {data[idx:end]}')
    idx = data.find(b'Next image', idx + 1)
