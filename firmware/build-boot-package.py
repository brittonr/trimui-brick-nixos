#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""
Build boot_package for TrimUI Brick (Allwinner A133)

Assembles a vendor-format boot_package containing:
  - Mainline U-Boot (patched: VBAR_EL3 NOPed, SCR_EL3.SMD cleared)
  - AArch32→AArch64 RMR shim + trampoline
  - Vendor ATF BL31 (patched: custom RMR SMC handler)
  - Vendor SCP + DTB (unmodified)

Inputs:
  VENDOR_BOOT_PKG  — original vendor boot_package.bin (container template)
  UBOOT_DTB_BIN    — mainline u-boot-dtb.bin

Output:
  boot_package_mainline_uboot.bin

The boot flow after BROM → boot0 → ATF:
  1. ATF ERETs to AArch32 shim at 0x4A000640 (EL1)
  2. Shim copies AArch64 trampoline to 0x4A100000, cleans D-cache
  3. Shim calls SMC(0x8400FF00) → patched ATF writes RVBAR, triggers RMR
  4. Core 0 warm-resets to AArch64 EL3 at trampoline
  5. Trampoline sets VBAR_EL3 to vendor ATF vectors, sets SCR_EL3.RW=1
  6. Trampoline copies U-Boot to CONFIG_TEXT_BASE (0x4A000000), jumps to it
  7. U-Boot boots normally (can't overwrite VBAR_EL3 — NOPed out)
"""

import struct
import sys
import os

# --- Configuration ---

BROM_STAMP = 0x5f0a6c39
MONITOR_OFFSET = 0xC0800
CUSTOM_SMC_ID = 0x8400FF00
RVBAR_CORE0 = 0x08100040
ATF_VBAR = 0x48010000

# U-Boot placement within boot_package item[0]
UBOOT_ITEM_OFFSET = 0x2000
UBOOT_SRC_ADDR = 0x4A000000 + UBOOT_ITEM_OFFSET  # 0x4A002000
UBOOT_DST_ADDR = 0x4A000000  # CONFIG_TEXT_BASE

# Trampoline placement
TRAMPOLINE_ITEM_OFFSET = 0x1000
TRAMPOLINE_SRC_ADDR = 0x4A000000 + TRAMPOLINE_ITEM_OFFSET  # 0x4A001000
TRAMPOLINE_DST_ADDR = 0x4A100000
TRAMPOLINE_COPY_SIZE = 0x200  # 512 bytes (plenty for trampoline)

# U-Boot binary patches (patterns searched, not hardcoded offsets)
MSR_VBAR_EL3_MASK = 0xFFFFFFE0
MSR_VBAR_EL3_VAL = 0xD51EC000
AARCH64_NOP = 0xD503201F

# SCR_EL3 literal values with SMD=1 (bit 7) that need SMD cleared
# These are loaded via LDR from literal pools then written to SCR_EL3
SCR_EL3_SMD_PATCHES = {
    0x5b1: 0x531,
    0x3c9: 0x349,
    0x1b1: 0x131,
}


def checksum_package(data, total_len):
    """Compute vendor boot_package checksum (BROM stamp algorithm)."""
    buf = bytearray(data[:total_len])
    struct.pack_into('<I', buf, 0x14, BROM_STAMP)
    total = sum(struct.unpack_from('<I', buf, i)[0] for i in range(0, total_len, 4))
    return total & 0xFFFFFFFF


# --- AArch64 instruction encoders ---

def a64_movz(rd, imm16, shift=0):
    return 0xD2800000 | ((shift // 16) << 21) | (imm16 << 5) | rd

def a64_movk(rd, imm16, shift=0):
    return 0xF2800000 | ((shift // 16) << 21) | (imm16 << 5) | rd

def a64_movz_w(rd, imm16):
    return 0x52800000 | (imm16 << 5) | rd

def a64_str_w(rt, rn, offset=0):
    return 0xB9000000 | ((offset // 4) << 10) | (rn << 5) | rt

def a64_ldr_x_post(rt, rn, imm):
    return 0xF8400400 | ((imm & 0x1FF) << 12) | (rn << 5) | rt

def a64_str_x_post(rt, rn, imm):
    return 0xF8000400 | ((imm & 0x1FF) << 12) | (rn << 5) | rt

def a64_subs_imm(rd, rn, imm):
    return 0xF1000000 | (imm << 10) | (rn << 5) | rd

def a64_b_gt(offset_words):
    return 0x5400000C | ((offset_words & 0x7FFFF) << 5)

def a64_dsb_sy():
    return 0xD5033F9F

def a64_isb():
    return 0xD5033FDF

def a64_ic_iallu():
    return 0xD508751F

def a64_br(rn):
    return 0xD61F0000 | (rn << 5)

def a64_orr_imm(rd, rn, immr, imms, n=1):
    """ORR Xd, Xn, #imm — simplified for setting bit 10 (RW)."""
    return 0xB2000000 | (n << 22) | (immr << 16) | (imms << 10) | (rn << 5) | rd

def a64_msr_vbar_el3(rt):
    return 0xD51EC000 | rt

def a64_mrs_scr_el3(rt):
    return 0xD53E1100 | rt

def a64_msr_scr_el3(rt):
    return 0xD51E1100 | rt


# --- AArch32 instruction encoders ---

def a32_movw(rd, imm16):
    return 0xE3000000 | (((imm16 >> 12) & 0xF) << 16) | (rd << 12) | (imm16 & 0xFFF)

def a32_movt(rd, imm16):
    return 0xE3400000 | (((imm16 >> 12) & 0xF) << 16) | (rd << 12) | (imm16 & 0xFFF)

def a32_mov32(rd, val):
    return [a32_movw(rd, val & 0xFFFF), a32_movt(rd, (val >> 16) & 0xFFFF)]


# --- Code generators ---

def build_aarch64_trampoline(uboot_size):
    """
    AArch64 trampoline at 0x4A100000 (entered after RMR, EL3, caches off).
    Sets VBAR_EL3, SCR_EL3.RW, copies U-Boot to CONFIG_TEXT_BASE, jumps.
    """
    code = []

    # UART base for diagnostics
    code.append(a64_movz(10, 0x0000))
    code.append(a64_movk(10, 0x0500, shift=16))

    # Print 'T' (trampoline running)
    code.append(a64_movz_w(11, ord('T')))
    code.append(a64_str_w(11, 10))

    # --- Set VBAR_EL3 to vendor ATF exception vectors ---
    code.append(a64_movz(8, ATF_VBAR & 0xFFFF))
    code.append(a64_movk(8, (ATF_VBAR >> 16) & 0xFFFF, shift=16))
    code.append(a64_msr_vbar_el3(8))

    # --- Set SCR_EL3.RW (bit 10) for AArch64 lower ELs ---
    code.append(a64_mrs_scr_el3(8))
    # ORR X8, X8, #(1<<10) — N=1, immr=54, imms=54 encodes bit 10
    code.append(a64_orr_imm(8, 8, 54, 54, n=1))
    code.append(a64_msr_scr_el3(8))
    code.append(a64_isb())

    # --- Copy U-Boot from 0x4A002000 to 0x4A000000 ---
    code.append(a64_movz(0, UBOOT_SRC_ADDR & 0xFFFF))
    code.append(a64_movk(0, (UBOOT_SRC_ADDR >> 16) & 0xFFFF, shift=16))
    code.append(a64_movz(1, UBOOT_DST_ADDR & 0xFFFF))
    code.append(a64_movk(1, (UBOOT_DST_ADDR >> 16) & 0xFFFF, shift=16))

    copy_bytes = (uboot_size + 7) & ~7
    code.append(a64_movz(2, copy_bytes & 0xFFFF))
    code.append(a64_movk(2, (copy_bytes >> 16) & 0xFFFF, shift=16))

    loop_start = len(code)
    code.append(a64_ldr_x_post(3, 0, 8))
    code.append(a64_str_x_post(3, 1, 8))
    code.append(a64_subs_imm(2, 2, 8))
    code.append(a64_b_gt(loop_start - len(code)))

    code.append(a64_dsb_sy())
    code.append(a64_isb())
    code.append(a64_ic_iallu())
    code.append(a64_dsb_sy())
    code.append(a64_isb())

    # Print 'J' (jumping)
    code.append(a64_movz_w(11, ord('J')))
    code.append(a64_str_w(11, 10))

    # Jump to U-Boot
    code.append(a64_movz(0, UBOOT_DST_ADDR & 0xFFFF))
    code.append(a64_movk(0, (UBOOT_DST_ADDR >> 16) & 0xFFFF, shift=16))
    code.append(a64_br(0))

    return code


def build_aarch32_shim(uboot_size):
    """
    AArch32 shim at item+0x640 (core 0, entered from ATF ERET in AArch32 EL1).
    Copies trampoline, cleans D-cache, RMRs core 0.
    """
    code = []
    labels = {}

    def emit(insn): code.append(insn)
    def label(name): labels[name] = len(code)
    def emit_char(ch):
        emit(0xE3A01000 | ord(ch))  # MOV R1, #ch
        emit(0xE5871000)             # STR R1, [R7]

    # UART base
    emit(0xE3A07405)  # MOV R7, #0x05000000
    emit_char('B')
    emit_char(':')

    # Copy trampoline
    emit_char('C')
    code.extend(a32_mov32(2, TRAMPOLINE_SRC_ADDR))
    code.extend(a32_mov32(3, TRAMPOLINE_DST_ADDR))
    code.extend(a32_mov32(4, TRAMPOLINE_SRC_ADDR + TRAMPOLINE_COPY_SIZE))

    label('copy_tramp')
    emit(0xE4925004)  # LDR R5, [R2], #4
    emit(0xE4835004)  # STR R5, [R3], #4
    emit(0xE1520004)  # CMP R2, R4
    emit(0x3A000000 | ((labels['copy_tramp'] - len(code) - 2) & 0xFFFFFF))

    # Clean D-cache for trampoline
    emit_char('D')
    code.extend(a32_mov32(2, TRAMPOLINE_DST_ADDR))
    code.extend(a32_mov32(4, TRAMPOLINE_DST_ADDR + TRAMPOLINE_COPY_SIZE + 64))
    label('clean_tramp')
    emit(0xEE072F3A)  # MCR p15, 0, R2, c7, c10, 1 (DCCMVAC)
    emit(0xE2822040)  # ADD R2, R2, #64
    emit(0xE1520004)  # CMP R2, R4
    emit(0x3A000000 | ((labels['clean_tramp'] - len(code) - 2) & 0xFFFFFF))

    # Clean D-cache for U-Boot source
    code.extend(a32_mov32(2, UBOOT_SRC_ADDR))
    code.extend(a32_mov32(4, UBOOT_SRC_ADDR + uboot_size + 64))
    label('clean_uboot')
    emit(0xEE072F3A)
    emit(0xE2822040)
    emit(0xE1520004)
    emit(0x3A000000 | ((labels['clean_uboot'] - len(code) - 2) & 0xFFFFFF))

    emit(0xF57FF04F)  # DSB SY
    emit(0xF57FF06F)  # ISB SY

    # RMR via custom SMC
    emit_char('R')
    code.extend(a32_mov32(0, CUSTOM_SMC_ID))
    code.extend(a32_mov32(1, TRAMPOLINE_DST_ADDR))
    code.extend(a32_mov32(2, RVBAR_CORE0))
    emit(0xE1600070)  # SMC #0

    # Should never reach here
    emit_char('?')
    emit(0xEAFFFFFE)  # B . (infinite loop)

    return b''.join(struct.pack('<I', insn) for insn in code)


def patch_uboot_binary(uboot_data):
    """Patch U-Boot: NOP VBAR_EL3 writes + clear SCR_EL3.SMD bit."""
    patched = bytearray(uboot_data)
    search_range = min(len(patched), 0x10000)  # patches are in first 64KB

    # Find and NOP all MSR VBAR_EL3 instructions
    vbar_count = 0
    for i in range(0, search_range, 4):
        insn = struct.unpack_from('<I', patched, i)[0]
        if (insn & MSR_VBAR_EL3_MASK) == MSR_VBAR_EL3_VAL:
            struct.pack_into('<I', patched, i, AARCH64_NOP)
            print(f"  NOP'd MSR VBAR_EL3 at U-Boot+0x{i:x}")
            vbar_count += 1
    assert vbar_count >= 2, f"Expected ≥2 MSR VBAR_EL3, found {vbar_count}"

    # Find and clear SMD bit in SCR_EL3 literal values
    smd_count = 0
    for i in range(0, search_range, 4):
        val = struct.unpack_from('<I', patched, i)[0]
        if val in SCR_EL3_SMD_PATCHES:
            new_val = SCR_EL3_SMD_PATCHES[val]
            struct.pack_into('<I', patched, i, new_val)
            print(f"  SCR_EL3 literal at U-Boot+0x{i:x}: 0x{val:x} → 0x{new_val:x}")
            smd_count += 1
    assert smd_count >= 3, f"Expected ≥3 SCR_EL3 literals, found {smd_count}"

    return bytes(patched)


def patch_atf_rmr_handler(pkg):
    """Patch ATF exception vector with custom RMR SMC handler."""
    base = MONITOR_OFFSET

    assert struct.unpack_from('<I', pkg, base + 0x10610)[0] == 0xF1004FDF, \
        "CMP X30, #0x13 not found at ATF+0x10610"
    # 0x10624 must be NOP (unused space we'll overwrite)
    assert struct.unpack_from('<I', pkg, base + 0x10624)[0] == 0xD503201F, \
        "NOP not found at ATF+0x10624 — vendor ATF layout changed?"

    # Redirect B.EQ to custom handler
    struct.pack_into('<I', pkg, base + 0x10614, 0x54000080)

    # Custom RMR handler at 0x10624
    handler = [
        0xD29FE01E,  # MOVZ X30, #0xFF00
        0xF2B0801E,  # MOVK X30, #0x8400, LSL#16
        0xEB1E001F,  # CMP X0, X30
    ]
    bne_offset = (0xC064 - 0x10630) // 4
    handler.append(0x54000001 | ((bne_offset & 0x7FFFF) << 5))
    handler.extend([
        0xB9000041,  # STR W1, [X2]       (RVBAR low)
        0xB900045F,  # STR WZR, [X2, #4]  (RVBAR high = 0)
        0xD5033F9F,  # DSB SY
        0xD5033FDF,  # ISB
        0xD2800060,  # MOVZ X0, #3        (AA64=1, RR=1)
        0xD51EC040,  # MSR RMR_EL3, X0
        0xD5033FDF,  # ISB
        0xD503207F,  # WFI
        0x17FFFFFF,  # B .-4
    ])

    for i, insn in enumerate(handler):
        struct.pack_into('<I', pkg, base + 0x10624 + i * 4, insn)

    return pkg


def main():
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <vendor_boot_package.bin> <u-boot-dtb.bin> <output.bin>")
        sys.exit(1)

    vendor_pkg_path, uboot_path, output_path = sys.argv[1:4]

    with open(vendor_pkg_path, 'rb') as f:
        pkg = bytearray(f.read())
    with open(uboot_path, 'rb') as f:
        uboot = f.read()

    uboot_size = len(uboot)
    total_len = struct.unpack_from('<I', pkg, 0x24)[0]
    print(f"Vendor boot_package: {len(pkg)} bytes, total_len=0x{total_len:x}")
    print(f"U-Boot: {uboot_size} bytes (0x{uboot_size:x})")

    # Patch U-Boot binary
    uboot = patch_uboot_binary(uboot)

    # Clear item code area
    item_start = 0x800
    code_start = item_start + 0x640
    item_end = item_start + 0xC0000
    for i in range(code_start, item_end):
        if i < len(pkg):
            pkg[i] = 0

    # Place U-Boot at item+0x2000
    uboot_offset = item_start + UBOOT_ITEM_OFFSET
    assert uboot_offset + uboot_size <= item_end, "U-Boot doesn't fit in item slot"
    pkg[uboot_offset:uboot_offset + uboot_size] = uboot

    # Build and place AArch64 trampoline at item+0x1000
    tramp_code = build_aarch64_trampoline(uboot_size)
    tramp_bytes = b''.join(struct.pack('<I', insn) for insn in tramp_code)
    tramp_offset = item_start + TRAMPOLINE_ITEM_OFFSET
    assert len(tramp_bytes) <= TRAMPOLINE_COPY_SIZE
    pkg[tramp_offset:tramp_offset + len(tramp_bytes)] = tramp_bytes

    # Build and place AArch32 shim at item+0x640
    shim = build_aarch32_shim(uboot_size)
    assert len(shim) <= (TRAMPOLINE_ITEM_OFFSET - 0x640)
    pkg[code_start:code_start + len(shim)] = shim

    # Patch ATF
    pkg = patch_atf_rmr_handler(pkg)

    # Verify load address
    assert struct.unpack_from('<I', pkg, 0x90)[0] == 0x4A000000

    # Fix checksum
    new_cksum = checksum_package(pkg, total_len)
    struct.pack_into('<I', pkg, 0x14, new_cksum)
    assert struct.unpack_from('<I', pkg, 0x10)[0] == 0x89119800, "Magic corrupted"

    with open(output_path, 'wb') as f:
        f.write(pkg)

    print(f"Output: {output_path} ({len(pkg)} bytes, checksum 0x{new_cksum:08X})")


if __name__ == '__main__':
    main()
