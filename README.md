# NixOS on TrimUI Brick (TG3040)

> Built at [Aurora Sprint](https://aurorasprint.com) — the boot chain was
> reverse engineered and developed by Claude (via [pi](https://github.com/mariozechner/pi))
> with physical access to the hardware through an automated DUT test harness.
> 21 iterations of boot\_package scripts were flashed, power-cycled, and analyzed
> without human hands touching the hardware. See `harness/` and `FINDINGS.md`.

NixOS SD card image for the TrimUI Brick handheld gaming device.

## Device

- **SoC**: Allwinner A133 (sun50iw10p1) — 4× Cortex-A53, AArch64
- **RAM**: 1 GB LPDDR3 @ 672 MHz
- **Storage**: 64 GB eMMC + SD card slot
- **Display**: 1024×768 LCD (DSI, jd9168 panel)
- **UART**: 115200 baud, snps,dw-apb-uart @ 0x05000000 (reg-shift=2)
- **PMIC**: AXP2202 (no mainline driver)
- **WiFi**: RTL8189FTV (no mainline driver)
- **GPU**: PowerVR GE8300 (no open driver)

## Boot Chain

Eight stages from power-on to userspace. The chain is complex because the
mainline DRAM driver has a write training bug (requiring vendor boot0), and
the vendor ATF enters U-Boot in AArch32 EL1 while mainline U-Boot requires
AArch64 EL3.

```
BROM ──► boot0 ──► boot_package ──► ATF BL31 ──► AArch32 shim
  │         │           │               │              │
  │         │           │               │              ▼
  │         │           │               │         RMR warm reset
  │         │           │               │              │
  │         │           │               │              ▼
  │         │           │               │      AArch64 trampoline
  │         │           │               │              │
  │         │           │               │              ▼
  │         │           │               │     Mainline U-Boot
  │         │           │               │              │
  │         │           │               │              ▼
  │         │           │               │      Linux kernel + NixOS
  │         │           │               │
  sili-   vendor     vendor base      vendor
  con     blob       rebuilt          2 patches
```

### Stage-by-stage breakdown

| # | Component | Source | Modified? | What it does |
|---|-----------|--------|-----------|--------------|
| 0 | **BROM** | Allwinner silicon | No (mask ROM) | Reads SD sector 16, validates eGON header (magic + checksum), loads boot0 to SRAM |
| 1 | **boot0** (vendor SPL) | Vendor blob | No | Initializes LPDDR3 DRAM (672 MHz, 1024 MB), clocks, MMC. Reads boot\_package from SD sector 32800, loads items to DRAM |
| 2 | **boot\_package** | Vendor base, rebuilt | Yes — items replaced, checksum recomputed | sunxi-package container with 4 items: U-Boot, ATF, SCP, DTB. Our build script replaces items [0] and [1] |
| 3 | **AArch32 shim + trampoline** | Ours (new code) | N/A | Bridges AArch32 EL1 → AArch64 EL3 via RMR. Copies U-Boot to CONFIG\_TEXT\_BASE before jumping |
| 4 | **U-Boot** | Mainline | Config only | Full U-Boot 2026.01. Loads kernel + initrd + DTB from SD via extlinux.conf |
| 5 | **ATF BL31** | Vendor blob | Yes — 2 binary patches | EL3 runtime, PSCI services. Patched with custom RMR SMC handler |
| 6 | **SCP firmware** | Vendor blob | No | ARISC coprocessor firmware (power management) |
| 7 | **Linux kernel** | Mainline (nixpkgs) | No | Currently loads but silent after "Starting kernel ..." |
| 8 | **NixOS** | NixOS + custom module | Hardware module | initrd → stage-2 → systemd |

### Component details

#### Stage 0 — BROM (Allwinner mask ROM)

Burned into the SoC. Checks SD card first, then eMMC. Validates boot0 header at
SD sector 16 — eGON.BT0 magic, checksum using the BROM stamp algorithm (`0x5f0a6c39`).

**Note:** The A133 BROM actually accepts mainline U-Boot SPL (eGON format). The real
blocker for eliminating vendor boot0 is missing DRAM write training in the mainline
driver (see `re_tools/RE_PLAN.md`). We use vendor boot0 because it has working DRAM
training; mainline's `dram_sun50i_a133.c` has a 16-byte write offset bug.

#### Stage 1 — boot0 (vendor SPL)

- **File**: `firmware/boot0.bin` (65536 bytes)
- **Origin**: Extracted from stock TG3040 SD recovery image
- **Commit**: `01e601494d` (Allwinner BSP)
- **Load address**: SRAM 0x20000

Initializes DRAM, then reads the boot\_package from SD sector 32800. Validates the
package checksum and loads each item (U-Boot, ATF, SCP, DTB) to its specified DRAM
address. Used as-is — identical binary on both SD and eMMC.

#### Stage 2 — boot\_package (rebuilt container)

- **Base**: Vendor `boot_package.bin` from stock firmware
- **Build script**: `scripts/build_boot_package_v20_core0_rmr.py`
- **Output**: `firmware/boot_package_mainline_uboot.bin`

Vendor sunxi-package format with 4 item directory entries. We preserve the container
structure, item sub-headers (boot0 validates magic strings), and SCP/DTB items unchanged.
Items [0] (u-boot) and [1] (monitor) are replaced/patched. Package checksum at `+0x14`
is recomputed.

```
boot_package layout:
  +0x000: "sunxi-package" header (magic, checksum, item count, total length)
  +0x040: item directory (4 × 0x170 bytes)
  +0x800: item[0] — u-boot slot (load addr 0x4A000000)
    +0x000..0x63F: vendor sub-header (preserved for boot0 validation)
    +0x640:        AArch32 shim (ours)
    +0x1000:       AArch64 trampoline (ours, copied to 0x4A100000 at runtime)
    +0x2000:       u-boot-dtb.bin (mainline, ~697 KB)
  +0xC0800: item[1] — ATF BL31 (vendor, 2 patches)
  +0xD1C00: item[2] — SCP/ARISC firmware (vendor, unmodified)
  +0xE6000: item[3] — DTB (vendor, unmodified)
```

#### Stage 3 — AArch32 shim + AArch64 trampoline (ours)

The core problem: vendor ATF does `ERET` to `0x4A000000` with `SPSR_EL3=0x1d3`
(AArch32 SVC EL1). Mainline U-Boot requires AArch64 EL3. Direct ATF patching of
SPSR/SCR\_EL3 was attempted in v7–v12 and never worked (all silent hang).

The solution uses ARM's Reset Management Register (RMR) to switch execution state:

1. **AArch32 shim** at `0x4A000640` (core 0, EL1):
   - Copies the AArch64 trampoline from `0x4A001000` to `0x4A100000` (above U-Boot)
   - Cleans D-cache (`DCCMVAC`) for trampoline + U-Boot regions
   - `SMC(0x8400FF00, 0x4A100000, 0x08100040)` → triggers RMR via patched ATF

2. **AArch64 trampoline** at `0x4A100000` (core 0, EL3 after warm reset):
   - Copies U-Boot from `0x4A002000` to `0x4A000000` (`CONFIG_TEXT_BASE`)
   - Jumps to `0x4A000000`

The copy step is necessary because `CONFIG_POSITION_INDEPENDENT` is not set —
U-Boot uses absolute addresses linked at `CONFIG_TEXT_BASE` and will crash if
run from any other address.

#### Stage 4 — Mainline U-Boot

- **Repository**: `https://source.denx.de/u-boot/u-boot.git` (mainline)
- **Version**: 2026.01
- **Binary**: `u-boot-dtb.bin` (~707 KB)
- **Config**: `trimui-brick_defconfig` (based on `liontron-h-a133l_defconfig` + LPDDR3 DRAM timings)

A133 support was added to mainline U-Boot by Andre Przywara (Arm) and Cody Eksal:

| Commit | Description | Author |
|--------|-------------|--------|
| `7d1936aef7c` | CLK: A100/A133 CCU driver | Andre Przywara |
| `17c1add3277` | Pinctrl: A100/A133 pin descriptions | Andre Przywara |
| `fb4c3b2a049` | SoC: A100/A133 integration (SPL, clocks, UART, MMC) | Andre Przywara |
| `7a337270c07` | DRAM: A133 init code (DDR4 + LPDDR4, 1722 lines) | Cody Eksal |
| `2b2783a1c07` | DTS: MMC max-frequency 150 MHz | Andre Przywara |
| `1cc93d42b24` | DTS: Liontron H-A133L board | Andre Przywara |
| `be5038f168e` | Defconfig: Liontron H-A133L | Andre Przywara |
| `cef5636d5ab` | Fix: DRAM address variable type (on `master`) | Andre Przywara |

Our config changes (no source patches):

- `CONFIG_DEBUG_UART_SHIFT=2` — A133 UART has `reg-shift=2` (was 0, caused hang)
- LPDDR3 DRAM timing parameters from vendor boot0 (TrimUI uses LPDDR3, not Liontron's LPDDR4)
- `CONFIG_DEBUG_UART_BASE=0x05000000`, `CONFIG_DEBUG_UART_ANNOUNCE=y`

**Not used**: mainline SPL (`sunxi-spl.bin`) — the BROM loads it fine, but the
mainline DRAM driver has a 16-byte write offset bug (missing write training).
We only use `u-boot-dtb.bin` (U-Boot proper without SPL).

To rebuild U-Boot from mainline:
```bash
git clone https://source.denx.de/u-boot/u-boot.git
cp firmware/trimui-brick_defconfig u-boot/configs/
cd u-boot
make trimui-brick_defconfig
make CROSS_COMPILE=aarch64-linux-gnu- u-boot-dtb.bin
```

#### Stage 5 — Vendor ATF BL31 (patched)

- **Binary**: `re_tools/monitor.bin` (70412 bytes)
- **Version**: BL3-1 v1.0(debug):406d5ac, Built 2022-05-07
- **Load address**: `0x48000000` (DRAM)

Two binary patches applied by our build script:

1. **Exception vector redirect** at `monitor+0x10614`:
   - Original: `B.EQ 0xC064` (normal AArch32 SMC handler)
   - Patched: `B.EQ 0x10624` (our custom handler first)

2. **Custom RMR SMC handler** at `monitor+0x10624` (was NOP space):
   ```
   if (SMC function_id == 0x8400FF00):
       write RVBAR[core] = X1    # set reset vector
       RMR_EL3 = 3               # AA64 + warm reset
       WFI                       # core resets to AArch64 EL3 at RVBAR
   else:
       fall through to normal handler
   ```

**Why not mainline ATF**: No A100/A133 platform exists in mainline ARM Trusted Firmware.
Available Allwinner platforms are sun50i\_a64, sun50i\_h6, sun50i\_h616, sun50i\_r329.
The vendor ATF loads to DRAM (`0x48000000`); mainline expects SRAM (`0x00104000`).
Writing a new platform from scratch would be required.

### Why this complexity?

Five constraints create this Rube Goldberg boot chain:

1. **Mainline DRAM driver is broken** → vendor boot0 required (has working write training)
2. **Vendor ATF enters U-Boot in AArch32 EL1** → mainline U-Boot needs AArch64 EL3
3. **ATF SPSR/SCR\_EL3 patching doesn't work** → RMR is the only way to switch state
4. **U-Boot isn't position-independent** → trampoline must copy it to `CONFIG_TEXT_BASE`
5. **No mainline ATF for A133** → vendor ATF (with patches) required

The minimum vendor dependencies are **boot0** and **ATF**. Both could theoretically
be eliminated if: (a) someone fixes the 16-byte write offset in mainline's DRAM
training (see `re_tools/RE_PLAN.md`), and (b) someone writes an A133 platform for
mainline ATF.

## SD Card Layout

```
Sector 0:       MBR (Linux partition table)
Sector 16:      boot0.bin (vendor SPL, 65536 bytes)
Sector 32800:   boot_package_mainline_uboot.bin (rebuilt, ~1 MB)
Sector 65536:   Partition 1 — FAT32 "NIXOS_BOOT" (128 MB — kernel, initrd, DTB, extlinux.conf)
Sector 327680:  Partition 2 — ext4 "NIXOS_ROOT" (NixOS rootfs)
```

## Building

Everything is built with Nix — U-Boot is cross-compiled from mainline source,
the boot\_package is assembled automatically, and the SD image includes all firmware.

```bash
# Build the complete SD card image (includes firmware, kernel, NixOS rootfs)
nix build .#sd-image

# Build individual components
nix build .#u-boot         # Cross-compile mainline U-Boot
nix build .#boot-package   # Assemble boot_package (U-Boot + patched ATF + shim)

# Flash to SD card
dd if=result/nixos-trimui-brick.img of=/dev/sdX bs=4M status=progress
# Or use the helper script:
nix run .#write-sd -- /dev/sdX
```

## Default Credentials

The image auto-logs in on the serial console (`ttyS0`). For SSH or manual login:

- **User**: `gamer`
- **Password**: `gamer`

The user has `sudo` access via the `wheel` group. Change the password or add SSH
keys in `configuration.nix` before deploying on untrusted networks.

## Current Status

- ✅ Mainline U-Boot 2026.01 boots (A133 CPU detected, 1 GiB DRAM, MMC found)
- ✅ U-Boot loads NixOS kernel + initrd from SD card via extlinux.conf
- ✅ Linux 6.18 boots with earlycon + console output
- ✅ NixOS boots to multi-user login (systemd, SSH, auto-login on ttyS0)
- ⚠️ MMC I/O errors after ~90s — SD card writes fail, filesystem remounts read-only
- ⚠️ Single-core only — PSCI disabled (vendor ATF can't handle AArch64 SMC)
- ⚠️ PMIC disabled — hardware has AXP2202, DTB declares AXP803 (no mainline driver)
- ⚠️ DTB fixes applied via overlay, not yet upstreamed

## Project Structure

```
firmware/               Firmware build system + vendor blobs (see firmware/README.md)
  u-boot.nix              Nix derivation: cross-compile mainline U-Boot
  boot-package.nix        Nix derivation: assemble boot_package
  build-boot-package.py   Boot_package assembly script (shim, trampoline, ATF patches)
  trimui-brick_defconfig  U-Boot defconfig (LPDDR3 timings for TrimUI)
  boot0.bin               Vendor SPL (DRAM init, 64 KB) — cannot be built from source
  boot_package.bin        Original vendor boot_package (template for assembly)
scripts/                Analysis and utility scripts
  crack_checksum.py       Checksum algorithm investigation
  verify_boot_package.py  Boot_package verification tool
  archive/                Historical build scripts (v2–v21)
re_tools/               Reverse engineering tools and extracted binaries (see re_tools/RE_PLAN.md)
  monitor.bin             Vendor ATF BL31 binary (70 KB, standalone copy for RE)
  analyze_monitor.py      ATF binary analysis
  disasm_monitor.py       Monitor disassembly
  disasm_switch.py        AArch32/64 switch logic
  disasm_training.py      DRAM training function disassembly
  find_scr.py             SCR_EL3 write locations
  check_uboot_entry.py    U-Boot dual-entry analysis
  trace_caller.py         EP setup caller trace
  gen-sunxi-mbr.py        Vendor sunxi-MBR format generator (reference tool)
  vendor-dtb3.dtb         Vendor DTB (reference, not used by build)
  vendor-kernel.dts       Vendor kernel DTS (reference)
harness/                DUT test harness (automated flash + capture)
  trimui_harness.rs       XIAO RP2350 firmware (Embassy, dual USB CDC)
  trimui-harness.ts       pi extension for the coding agent
  README.md               Wiring, protocol, build instructions
dtb/                    Device tree sources
  trimui-brick.dtso       DTB overlay for TrimUI Brick
serial_logs/            UART capture logs from boot tests (gitignored, generated by harness)
modules/                NixOS hardware modules
  trimui-brick.nix        TrimUI Brick hardware config
image/                  SD card image builder
  build-image.nix         NixOS SD image derivation
configuration.nix       NixOS system configuration
flake.nix               Nix flake entry point
FINDINGS.md             Append-only technical findings log
LICENSE                 GPL-2.0-or-later (code); vendor blobs excluded
```

## Address Map

| Component | Address | Notes |
|-----------|---------|-------|
| SRAM (boot0) | `0x00020000` | boot0 load/execute address |
| ATF BL31 | `0x48000000` | Vendor ATF in DRAM |
| U-Boot | `0x4A000000` | `CONFIG_TEXT_BASE` |
| DRAM base | `0x40000000` | 1 GB LPDDR3 |
| UART0 | `0x05000000` | reg-shift=2, reg-io-width=4 |
| CCU | `0x03001000` | Clock control unit |
| PIO | `0x0300B000` | GPIO/pinctrl |
| RVBAR core 0 | `0x08100040` | Reset vector base address |
| RVBAR core 1 | `0x08100048` | Reset vector base address |

## Known Issues

### MMC I/O errors (~90s after boot)
SD card write errors cascade after ~90 seconds of uptime. The `sunxi-mmc` driver
reports "Card stuck being busy" → HW reset timeout → I/O errors → ext4 remounts
read-only. Mitigated by volatile journal and tmpfs mounts, but root cause (timing,
power, or card quality) is unresolved.

### Single-core operation
PSCI is disabled in the DTB overlay. Vendor ATF handles AArch32 SMC (EC=0x13) but
crashes on AArch64 SMC (EC=0x17 → data abort). The kernel runs on core 0 only.
Fix requires either patching ATF's AArch64 exception handler or implementing
spin-table with a proper release address.

### PMIC mismatch
The Liontron H-A133L reference board has AXP803; TrimUI Brick has AXP2202. The
PMIC is disabled in the DTB to prevent the AXP803 driver from writing wrong
registers (which causes system shutdown after ~2.7s). Boot0 configures correct
voltages, so this is safe for basic operation.

## Authors

- **brittonr** — hardware wrangling, DUT harness, pi orchestration
- **adeci** — hardware wrangling, DUT harness, pi orchestration
- **murdoa** — hardware wrangling, DUT harness, pi orchestration
- **Claude** (via [pi](https://github.com/mariozechner/pi)) — reverse engineering,
  boot chain development, Nix derivations, 21 iterations of boot\_package debugging

## Acknowledgements

Thanks to [Matthew Croughan](https://github.com/MatthewCroughan) for
[nixos-a133](https://github.com/MatthewCroughan/nixos-a133) and in-person guidance.

## Related Repositories

- **nixos-a133**: `https://github.com/MatthewCroughan/nixos-a133` — NixOS on Allwinner A133 (reference)
- **U-Boot mainline**: `https://source.denx.de/u-boot/u-boot.git` — A133 support merged to master
- **linux-sunxi wiki**: `https://linux-sunxi.org/A133` — Allwinner A133 community documentation
