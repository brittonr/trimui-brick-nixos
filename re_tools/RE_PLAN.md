# Vendor Blob Reverse Engineering Plan

## Goal: Blob-Free Boot

Eliminate all vendor binary dependencies from the boot chain.
Currently three vendor blobs:

| Blob | Size | Function | Can Replace? |
|------|------|----------|-------------|
| boot0.bin | 64 KB | SPL: DRAM init, MMC, boot_package load | YES — mainline SPL already works, DRAM training is the only gap |
| monitor.bin | 70 KB | ATF: EL3 runtime, PSCI | YES — write mainline ATF platform |
| SCP firmware | 80 KB | ARISC: power management | DEFER — not needed for basic boot |

## Critical Discovery

**The A133 BROM accepts mainline U-Boot SPL.** The eGON.BT0 header format
works fine — SPL loads, executes, and initializes the DRAM controller.

The ONLY remaining blocker is a **16-byte write offset bug** in the mainline
DRAM driver caused by **missing write leveling and write training**. Fix this
and the entire vendor boot chain can be replaced.

## Phase 1: Fix DRAM Write Training (eliminates boot0 + ATF)

### Step 1: Capture Vendor PHY Register Dump

**Tool exists:** `patch-boot0-phydump.py` (in sibling `uboot-a133` repo) patches vendor boot0
to dump all PHY (2048 regs) and CTL (1024 regs) registers after DRAM init.

**Action:**
1. Flash patched boot0 to SD card
2. Capture UART output with Glasgow (115200 baud, ~30s, expect ~3000 lines)
3. Parse into register map for comparison

**Why:** The vendor boot0 produces working DRAM. Comparing its register state
with mainline's reveals exactly which registers the training modifies.

### Step 2: Capture Mainline PHY Register Dump

**Action:** Add equivalent PHY/CTL register dump to mainline `dram_sun50i_a133.c`
after `mctl_calibrate_phy()` returns but before normal boot continues.

**Code:** Add to `mctl_core_init()`:
```c
/* Debug: dump PHY and CTL registers */
for (int i = 0; i < 0x800; i += 4)
    printf("PHY %03x=%08x\n", i, readl(SUNXI_DRAM_PHY0_BASE + i));
for (int i = 0; i < 0x400; i += 4)
    printf("CTL %03x=%08x\n", i, readl(SUNXI_DRAM_CTL0_BASE + i));
```

### Step 3: Diff Register Maps

Compare vendor vs mainline PHY register state. The differences will be in:
- Write leveling results (DQS delays per byte lane)
- Read/write training results (DQ delays per bit)
- PHY configuration bits set during training

### Step 4: Apply Static Compensation (Quick Fix)

If the vendor register values are consistent across boots (they should be
for identical hardware), apply them as static values in mainline:

```c
/* Apply vendor-derived write training values */
writel(vendor_val, SUNXI_DRAM_PHY0_BASE + 0xXXX);
```

This bypasses the need to implement the training algorithm — just use the
result. The existing `mctl_phy_dx_delay_compensation()` already does this
pattern for read delays.

### Step 5: Implement Training (Proper Fix)

If static values don't work (vary per chip), implement the actual training.
The four vendor training functions have been disassembled:

| Function | Address | Size | Mainline Status |
|----------|---------|------|-----------------|
| write_leveling | 0x23fb4 | 268 B | TODO stub |
| read_training | 0x240c0 | 212 B | TODO stub |
| unknown_training | 0x243a4 | 904 B | TODO stub |
| write_training | 0x2472c | 844 B | TODO stub |

Reference: `re_tools/training_disasm.txt` — full annotated disassembly.

The H616 driver has implementations of all four that can serve as a
starting point. The PHY register layout is similar (same base 0x04830000).

## Phase 2: Mainline ATF Platform (eliminates RMR shim)

Once mainline SPL works with proper DRAM, write a `plat/allwinner/sun50i_a133`
platform for ARM Trusted Firmware. This eliminates:
- Vendor ATF binary
- AArch32→AArch64 RMR shim
- AArch64 trampoline
- All binary patching

Base on `plat/allwinner/sun50i_h616` (same generation).

Key platform-specific details extracted from vendor ATF strings:
- Platform source: `plat/sun50iw10p1/`
- PSCI: standard v1.0 (full off/on/suspend)
- GIC: standard setup
- SCP communication via hwmsgbox

## Phase 3: SCP/Crust (multi-core, suspend)

Port the open-source `crust` firmware to A133 for ARISC coprocessor.
Required for CPU_ON (multi-core) and suspend/resume.
Not needed for basic single-core boot.

## Experiment Checklist

When hardware is available:

- [ ] Flash `boot0_sdcard_patched.bin` (PHY dump patcher) to SD sector 16
- [ ] Capture vendor PHY dump via UART (expect 3000+ lines of "PHY xxx=xxxxxxxx")
- [ ] Add PHY dump to mainline A133 DRAM driver
- [ ] Build and flash mainline SPL with PHY dump
- [ ] Capture mainline PHY dump
- [ ] Diff the two dumps
- [ ] Apply static register overrides for training results
- [ ] Test if DRAM write offset is fixed
- [ ] If yes: build full mainline SPL → ATF → U-Boot chain
- [ ] If no: RE the write_leveling function at 0x23fb4

## Files

| File | Purpose |
|------|---------|
| `disasm_training.py` | Generate annotated disassembly of training functions |
| `monitor.bin` | Vendor ATF binary, standalone copy for RE analysis. Also embedded in `firmware/boot_package.bin` at offset `0xC0800` (which the build script patches at build time). |
| `vendor-dtb3.dtb` | Vendor DTB extracted from stock firmware (reference for hardware description) |
| `vendor-kernel.dts` | Vendor kernel DTS decompiled from stock firmware (reference for pin/peripheral config) |
| `gen-sunxi-mbr.py` | Generates vendor sunxi-MBR format (reference tool, not used by the NixOS image build) |
| `analyze_monitor.py` | ATF binary analysis — searches for SPSR, SCR, ERET patterns |
| `disasm_monitor.py` | Monitor disassembly — MOVZ #0x1d3, SPSR strings |
| `disasm_switch.py` | ATF AArch32/AArch64 execution state switch logic |
| `find_scr.py` | Find SCR_EL3 read/write locations in vendor ATF |
| `check_uboot_entry.py` | Analyze U-Boot dual-entry (AArch32/AArch64) header |
| `trace_caller.py` | Trace callers of the EP setup function in vendor ATF |
