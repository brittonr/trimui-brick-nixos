#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""
Disassemble vendor boot0 DRAM training functions.

Uses arm-none-eabi-objdump to produce annotated disassembly of the four
training functions in vendor boot0.bin, plus the training orchestrator.

Functions:
  0x23fb4 — write_leveling()
  0x240c0 — read_training()
  0x243a4 — unknown_training() (third pass)
  0x2472c — write_training()
  0x25e48 — training_orchestrator()
  0x2374c — post_training() (delay compensation?)

PHY base:  0x04830000
CTL base:  0x04820000
Load addr: 0x20000
"""

import subprocess
import sys
import struct
import re

BOOT0 = "firmware/boot0.bin"
MEM_BASE = 0x20000
PHY_BASE = 0x04830000
CTL_BASE = 0x04820000

# Try to find arm objdump
OBJDUMP_PATHS = [
    "/nix/store/v55g0zbn8170pnlf5gcz7kf9bk6r99bb-arm-none-eabi-binutils-2.44/bin/arm-none-eabi-objdump",
    "/nix/store/vr5rpamsiydkiqlasagwvac3jihggym9-arm-none-eabi-binutils-2.44/bin/arm-none-eabi-objdump",
    "arm-none-eabi-objdump",
]

FUNCTIONS = [
    ("post_training (delay_compensation?)", 0x2374c, 0x23900),
    ("pre_training_init_1", 0x23e34, 0x23fb4),
    ("write_leveling", 0x23fb4, 0x240c0),
    ("read_training", 0x240c0, 0x24194),
    ("read_calibration?", 0x243a4, 0x2472c),
    ("write_training", 0x2472c, 0x24a78),
    ("training_helper", 0x24a78, 0x24b00),
    ("training_orchestrator", 0x25e48, 0x26020),
]

# Known addresses for annotation
KNOWN_ADDRS = {
    0x21eb4: "printf",
    0x23120: "pre_training_init_2",
    0x23e34: "pre_training_init_1",
    0x23fb4: "write_leveling",
    0x240c0: "read_training",
    0x243a4: "unknown_training",
    0x2472c: "write_training",
    0x24a78: "training_helper",
    0x25824: "retraining_loop",
    0x25e48: "training_orchestrator",
    0x2374c: "post_training",
    0x272ac: "dram_init_top",
}

# Known literal pool values
KNOWN_LITERALS = {
    0x04830000: "PHY_BASE",
    0x04830008: "PHY+0x008",
    0x04830054: "PHY+0x054 (PHY_PGCR)",
    0x04830060: "PHY+0x060",
    0x04830190: "PHY+0x190 (training trigger)",
    0x04830198: "PHY+0x198",
    0x048301a0: "PHY+0x1a0",
    0x048303dc: "PHY+0x3dc",
    0x04830484: "PHY+0x484",
    0x048304cc: "PHY+0x4cc",
    0x048304d0: "PHY+0x4d0",
    0x04830524: "PHY+0x524",
    0x04830528: "PHY+0x528",
    0x0483058c: "PHY+0x58c",
    0x048305e0: "PHY+0x5e0",
    0x04830780: "PHY+0x780",
    0x04830784: "PHY+0x784",
    0x04830788: "PHY+0x788",
    0x04830790: "PHY+0x790",
    0x04830800: "PHY+0x800",
    0x048307b8: "PHY+0x7b8",
    0x048307dc: "PHY+0x7dc",
    0x048307e4: "PHY+0x7e4",
    0x048308e0: "PHY+0x8e0 (WL/WT status)",
    0x04830ae0: "PHY+0xae0 (WL/WT status hi)",
    0x04820000: "CTL_BASE",
    0x04820004: "CTL+0x004 (STAT)",
    0x04820010: "CTL+0x010",
    0x04820014: "CTL+0x014",
    0x04820030: "CTL+0x030 (PWRCTL)",
    0x04820060: "CTL+0x060 (DRAMTMG0)",
    0x04820100: "CTL+0x100 (DRAMTMG16+)",
    0x048201a0: "CTL+0x1a0",
    0x048201b0: "CTL+0x1b0 (DFIUPD0)",
    0x048201bc: "CTL+0x1bc",
    0x048201c0: "CTL+0x1c0",
    0x04820320: "CTL+0x320 (SWCTL)",
    0x04820324: "CTL+0x324 (SWSTAT)",
}


def find_objdump():
    import shutil
    for path in OBJDUMP_PATHS:
        if shutil.which(path):
            return path
    # Try nix store search
    import glob
    matches = glob.glob("/nix/store/*/bin/arm-none-eabi-objdump")
    if matches:
        return matches[0]
    print("ERROR: arm-none-eabi-objdump not found", file=sys.stderr)
    sys.exit(1)


def disasm_range(objdump, boot0_path, start, end):
    """Disassemble a range and return annotated output."""
    cmd = [
        objdump, "-D", "-b", "binary", "-m", "arm",
        "--adjust-vma=0x20000", "-M", "force-thumb",
        f"--start-address=0x{start:x}",
        f"--stop-address=0x{end:x}",
        boot0_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout


def annotate_line(line, data):
    """Add annotations for known addresses and register accesses."""
    annotations = []

    # Check for BL targets
    bl_match = re.search(r'bl\s+0x([0-9a-f]+)', line)
    if bl_match:
        target = int(bl_match.group(1), 16)
        if target in KNOWN_ADDRS:
            annotations.append(f"→ {KNOWN_ADDRS[target]}()")

    # Check for LDR from literal pool
    ldr_match = re.search(r'ldr\s+r\d+,\s+\[pc,\s+#\d+\]\s+@\s+\(0x([0-9a-f]+)\)', line)
    if ldr_match:
        pool_addr = int(ldr_match.group(1), 16)
        file_off = pool_addr - MEM_BASE
        if 0 <= file_off < len(data) - 3:
            val = struct.unpack_from('<I', data, file_off)[0]
            note = f"= 0x{val:08x}"
            if val in KNOWN_LITERALS:
                note += f" ({KNOWN_LITERALS[val]})"
            elif (val & 0xFFFF0000) == PHY_BASE:
                note += f" (PHY+0x{val & 0xFFFF:03x})"
            elif (val & 0xFFFF0000) == CTL_BASE:
                note += f" (CTL+0x{val & 0xFFFF:03x})"
            elif MEM_BASE <= val < MEM_BASE + len(data):
                # Check if it's a string
                str_off = val - MEM_BASE
                if all(32 <= data[str_off + j] < 127 for j in range(min(4, len(data) - str_off))):
                    end = data.index(0, str_off) if 0 in data[str_off:str_off+80] else str_off+40
                    s = data[str_off:end].decode('ascii', errors='replace')
                    note += f' ("{s}")'
            annotations.append(note)

    if annotations:
        line = line.rstrip() + "  ; " + " | ".join(annotations)

    return line


def main():
    objdump = find_objdump()
    with open(BOOT0, 'rb') as f:
        data = f.read()

    print(f"# Vendor boot0 DRAM Training Functions Disassembly")
    print(f"# Boot0: {BOOT0} ({len(data)} bytes)")
    print(f"# Load address: 0x{MEM_BASE:x}")
    print(f"# PHY base: 0x{PHY_BASE:08x}")
    print(f"# CTL base: 0x{CTL_BASE:08x}")
    print(f"# Generated by disasm_training.py")
    print()

    for name, start, end in FUNCTIONS:
        print(f"{'='*72}")
        print(f"# {name}")
        print(f"# Address: 0x{start:x} - 0x{end:x} ({end-start} bytes)")
        print(f"{'='*72}")

        output = disasm_range(objdump, BOOT0, start, end)

        for line in output.split('\n'):
            # Skip header lines
            if line.startswith('firmware') or line.startswith('Disassembly') or not line.strip():
                continue
            if line.startswith('   '):
                line = annotate_line(line, data)
            print(line)

        print()


if __name__ == '__main__':
    main()
