# Findings Log — TrimUI Brick NixOS Boot

> Append-only chronological log of discoveries made during development.
> Earlier entries may contain assumptions later proven wrong (e.g., the
> BROM header format theory was corrected on 2026-02-28). Read bottom-up
> for the most current understanding.

### 2026-02-28 18:30 — CRITICAL: Mainline SPL DOES boot on A133 — BROM accepts eGON format

**Context:** Assumed A133 BROM rejects mainline SPL based on earlier README. Investigated to begin vendor blob RE.
**Method:** Cross-referenced earlier claims against uboot-a133 repo (same project, earlier phase). Compared vendor boot0 and mainline SPL headers byte-by-byte.
**Result:** Both use identical eGON.BT0 magic, same checksum algorithm (BROM_STAMP 0x5f0a6c39). uboot-a133/HACKING.md explicitly states "SPL loads and executes" with DRAM init passing. The BROM accepts the mainline SPL fine.

Header comparison (key diffs):
```
  +0x14: vendor=0x00000030 (pub_head_size=48)  mainline=0x024c5053 ("SPL" signature)
  +0x1c: vendor=0x00020000 (load addr)         mainline=0x00000000
  +0x28: vendor=0x000000ff (boot_media)        mainline=0x00000000
  +0x2c+: vendor=DRAM params                   mainline=DT name string
```

**Implications:** The REAL blocker is NOT the header format. It's the DRAM 16-byte write offset bug in mainline's `dram_sun50i_a133.c`. Fixing this ONE issue eliminates ALL vendor blob dependencies (boot0 + ATF + SCP). The path to blob-free boot is: fix DRAM training → mainline SPL → mainline ATF → mainline U-Boot.

### 2026-02-28 18:30 — Vendor boot0 has 4 training functions, mainline has 0

**Context:** Investigating the DRAM 16-byte write offset bug that prevents mainline SPL from working.
**Method:** Disassembled vendor boot0 training orchestration function at 0x25e48 using arm-none-eabi-objdump. Cross-referenced literal pool at 0x6004-0x601c with training error strings.
**Result:** Vendor boot0 training orchestrator (0x25e48) calls 4 training functions controlled by tpr10/tpr13 bits:

| Bit (in field at +0x6c) | Training Type | Function Addr | Error String |
|---|---|---|---|
| bit 20 (0x100000) | Write Leveling | 0x23fb4 | "write_leveling error" |
| bit 21 (0x200000) | Read Training | 0x240c0 | "read_training error" |
| bit 22 (0x400000) | Unknown (read cal?) | 0x243a4 | "read_training error" |
| bit 23 (0x800000) | Write Training | 0x2472c | "write_training error" |

Each has a retry loop (up to 5 attempts). The orchestrator also writes CTL+0x320 before/after init.

Mainline `dram_sun50i_a133.c` has:
```c
/* TODO: Implement write levelling */
/* TODO: Implement read training */
/* TODO: Implement write training */
/* TODO: Implement DFS */
```

Only `mctl_phy_read_calibration()` is implemented. All four training stubs are missing.

**Implications:** The 16-byte write offset is almost certainly caused by missing write leveling/training. The DQS write strobe alignment isn't calibrated, so writes land at addr+16. RE of vendor function at 0x23fb4 (write leveling) is the minimum needed.

### 2026-02-28 18:30 — Vendor boot0 PHY/CTL register dump infrastructure exists but capture failed

**Context:** The uboot-a133 repo has `patch-boot0-phydump.py` that patches vendor boot0 to dump all PHY (0x04830000-0x04830800) and CTL (0x04820000-0x04820400) registers after DRAM init.
**Method:** Examined phydump-output.txt and the patcher script.
**Result:** The capture file only contains 3 lines (flush/reset/timeout messages) — the actual PHY dump was never successfully captured. The patcher injects Thumb2 code into boot0's code cave at file offset 0x900 that wraps the DRAM init call at 0x271be, dumps registers via printf (0x21eb4), then returns.
**Implications:** Getting this PHY dump is the highest-priority experiment. Comparing vendor vs mainline PHY register state after DRAM init will reveal exactly which registers the training functions modify. This may allow static compensation without full training RE.

### 2026-02-28 18:30 — Key addresses in vendor boot0 for DRAM RE

**Context:** Mapping vendor boot0 binary for systematic reverse engineering.
**Method:** Combined disassembly, string search, and literal pool cross-referencing.
**Result:**

```
Boot0 load address: 0x20000 (SRAM)

Key Functions:
  0x21eb4 — printf (Thumb, used for all boot messages)
  0x23120 — pre-training init
  0x23e34 — pre-training init (second)
  0x23fb4 — write_leveling()
  0x240c0 — read_training() (includes read calibration)
  0x243a4 — unknown training (third pass, guarded by bit 22)
  0x2472c — write_training()
  0x24a78 — training helper (called between CTL writes)
  0x25824 — called during retry/retraining loop
  0x25e48 — training orchestrator (calls all training funcs)
  0x2374c — post-training (delay compensation?)
  0x272ac — top-level DRAM init

Hardware Bases:
  0x04820000 — DRAM controller (CTL)
  0x04830000 — DRAM PHY
  164 PHY register references in boot0
  60 CTL register references in boot0

Training Control:
  Para struct field at +0x6c controls training enables
  CTL+0x320 toggled (0 then 1) around training sequence
```

**Implications:** The four training functions (0x23fb4, 0x240c0, 0x243a4, 0x2472c) are the RE targets. Write leveling at 0x23fb4 is ~268 bytes (0x23fb4 to 0x240c0), read training ~212 bytes (0x240c0 to 0x24194). These are small enough to fully reverse engineer.

### 2026-02-26 10:50 — v19 root cause: CONFIG_POSITION_INDEPENDENT=n breaks U-Boot at wrong address

**Context:** v19 placed U-Boot at item+0x2000 (DRAM 0x4A002000), RMR'd core 1 there. Output: "19:EC P=00000000 Z R" then silence.
**Method:** Checked U-Boot .config: CONFIG_POSITION_INDEPENDENT is NOT set. U-Boot uses absolute addresses linked at CONFIG_TEXT_BASE=0x4A000000.
**Result:** Running from 0x4A002000 with absolute references to 0x4A000000 = immediate crash. Every `ldr x0, =symbol` loads wrong address.
**Implications:** U-Boot MUST be at CONFIG_TEXT_BASE. v20 adds trampoline to copy U-Boot to 0x4A000000 before jumping.

### 2026-02-26 10:50 — CONFIG_ARMV8_MULTIENTRY not set — core 1 vs core 0 irrelevant

**Context:** Initial hypothesis was v19 failed because U-Boot checks MPIDR and sends core 1 to spin loop.
**Method:** Checked .config: CONFIG_ARMV8_MULTIENTRY is NOT set. branch_if_master unconditionally goes to master_cpu.
**Result:** Core ID doesn't matter for this U-Boot build. The address mismatch was the real issue.
**Implications:** Could use either core 0 or core 1, but core 0 is simpler (no CPU_ON needed).

### 2026-02-26 10:55 — v20 trampoline approach: copy U-Boot to CONFIG_TEXT_BASE via AArch64 trampoline

**Context:** U-Boot must be at 0x4A000000 but the vendor header occupies item+0x0000 to +0x063F.
**Method:** AArch32 shim copies pre-assembled AArch64 trampoline from 0x4A001000 to 0x4A100000 (above U-Boot area). RMRs core 0 to 0x4A100000. Trampoline copies U-Boot from 0x4A002000 to 0x4A000000 then jumps.
**Result:** Boot output shows "20:CDRTJ" — full chain works. But U-Boot still silent after 'J'.
**Implications:** Copy works correctly, issue is inside U-Boot early code.

### 2026-02-26 10:55 — ROOT CAUSE: CONFIG_DEBUG_UART_SHIFT=0 wrong for A133 UART

**Context:** v20 showed "20:CDRTJ" then silence. U-Boot at correct address but no debug output.
**Method:** Traced debug UART code. With CONFIG_DEBUG_UART, struct ns16550 has 1-byte fields. serial_din/serial_dout uses CONFIG_DEBUG_UART_SHIFT to compute register addresses.
**Result:** SHIFT=0 → LSR at 0x05000005 (wrong!). SHIFT=2 → LSR at 0x05000014 (correct). A133 UART has reg-shift=2. U-Boot hung in _debug_uart_init() polling wrong LSR address.
**Implications:** Fixed to SHIFT=2, rebuilt U-Boot → **MAINLINE U-BOOT BOOTS!** Full banner, DRAM detected, MMC found.

### 2026-02-26 10:55 — MAINLINE U-BOOT RUNNING on TrimUI Brick A133! 🎉

**Context:** After fixing DEBUG_UART_SHIFT, rebuilt and flashed v20 boot_package.
**Method:** trimui_harness capture, 45 seconds.
**Result:** Full U-Boot 2025.07-rc5 banner, A133 CPU detected, 1 GiB DRAM, MMC found, NixOS extlinux.conf loaded, kernel + initrd retrieved, "Starting kernel..." printed.
**Implications:** Boot chain complete: BROM → vendor boot0 → vendor ATF (patched) → AArch32 shim → RMR → AArch64 trampoline → mainline U-Boot → NixOS kernel load. Next: fix DTB path and add earlycon for kernel output.

Serial log: serial_logs/boot_test_2026-02-26_1550.log

### 2026-02-26 11:49 — NixOS image boots, kernel output achieved! Panic at PSCI SMC

**Context:** Built proper NixOS SD image with kernel 6.18.8, correct DTB (sun50i-a133-liontron-h-a133l.dtb), earlycon=uart,mmio32,0x05000000, console=ttyS0,115200n8.
**Method:** Full NixOS image: boot0 sector 16, boot_package_mainline_uboot sector 32800, FAT32 boot partition (extlinux.conf + kernel + initrd + DTB), ext4 rootfs.
**Result:** Kernel 6.18.8 boots! earlycon works, DTB loads correctly ("Machine model: Liontron H-A133L"), DRAM 1GB detected. PANICS at `psci_probe()` → `psci_0_2_get_version()` → `__arm_smccc_smc+0x4` with "Undefined instruction". The SMC instruction itself is faulting — no EL3 exception handler is installed after RMR warm reset. x0=0x84000000 = PSCI_VERSION function ID.
**Implications:** After core 0 RMR, VBAR_EL3 is reset to 0x0 (or garbage). Vendor ATF's exception vectors at 0x48010000 are gone from the CPU's perspective. The AArch64 trampoline runs at EL3, jumps to U-Boot at EL3, U-Boot drops to EL2 and starts kernel. Kernel tries SMC → goes to VBAR_EL3 + 0x200 → hits garbage → undefined instruction. Fix: trampoline must restore VBAR_EL3 to vendor ATF's vector table (0x48010000) before jumping to U-Boot, OR U-Boot needs to install its own PSCI handler at EL3.

Serial log: serial_logs/boot_test_2026-02-26_1649.log

### 2026-02-26 12:43 — SCR_EL3.SMD=1 was the "Undefined instruction" root cause

**Context:** v21 with VBAR_EL3 NOP still crashed at PSCI SMC.
**Method:** Searched U-Boot binary for all SCR_EL3 literal values used in armv8_switch_to_el2.
**Result:** Three literal pools at offsets 0x34e8/0x34f0/0x34f8 all have SMD=1 (bit 7). Values: 0x5b1, 0x3c9, 0x1b1. U-Boot's armv8_switch_to_el2_m macro explicitly sets SCR_EL3_SMD_DIS. With SMD=1, SMC from EL1/EL2 causes "Undefined Instruction" at the executing EL — architecturally defined behavior.
**Implications:** Must clear bit 7 in these literals. Changed to 0x531, 0x349, 0x131.

### 2026-02-26 12:43 — SMC reaches vendor ATF but ATF crashes on AArch64 SMC (EC=0x26)

**Context:** After fixing SMD=0 and VBAR_EL3, kernel PSCI SMC now reaches vendor ATF at EL3.
**Method:** Captured serial log showing ATF "Unhandled Exception in EL3" register dump.
**Result:** esr_el3=0x9a000000 → EC=0x26 (SMC from AArch64). Vendor ATF only handles EC=0x13 (SMC from AArch32) via the exception vector at VBAR+0x600 (sync lower EL, AArch32). AArch64 SMC goes to VBAR+0x400 (sync lower EL, AArch64) which vendor ATF doesn't handle for PSCI — it was never designed for AArch64 callers.
**Implications:** Need to patch ATF's AArch64 exception vector (VBAR+0x400) to also route SMC to the PSCI handler, or have our custom handler intercept both AArch32 and AArch64 SMC paths.

Serial log: serial_logs/boot_test_2026-02-26_1743.log

### 2026-02-27 05:40 — 🎉🎉🎉 NixOS FULLY BOOTS on TrimUI Brick!

**Context:** Fixed DTB v3 with: no PSCI, PMIC disabled, vmmc-supply → fixed 3.3V regulator, pinctrl supply refs removed.
**Result:** NixOS 25.05 with kernel 6.18.8 boots to multi-user target:
- systemd 258.3 running, hostname `trimui-brick`
- SSH daemon started, user `gamer` auto-logged in
- Root filesystem mounted from SD card (mmcblk1p2, LABEL=NIXOS_ROOT)
- fsck passed, ext4 mounted r/w
- DHCP client, getty, watchdog all running
- Reached target Multi-User System ✅

**Key fixes needed to get here:**
1. Kernel 6.18 (A133 DTB support)
2. v21 boot_package: VBAR_EL3 NOP + SCR_EL3.SMD cleared
3. PSCI disabled in DTB (vendor ATF can't handle AArch64 SMC)
4. PMIC (AXP803) disabled in DTB (hardware has AXP2202)
5. vmmc-supply replaced with regulator-fixed 3.3V (MMC needs card voltage)
6. pinctrl vcc-p*-supply removed (dummy regulator works)
7. regulator_ignore_unused + clk_ignore_unused kernel params

Serial log: serial_logs/boot_test_2026-02-27_1040.log

### 2026-02-26 13:16 — Wrong PMIC: AXP803 DTB vs AXP2202 hardware

**Context:** NixOS kernel booted 2.7s then hard-reset (reboot into eMMC vendor kernel).
**Method:** Analyzed kernel log: `vcc-io-usb-pd-emmc: Bringing 2000000uV into 3300000-3300000uV` (voltage change via AXP803 driver). Cross-referenced with boot0 log: `PMU: AXP2202`.
**Result:** The mainline DTB (`sun50i-a133-liontron-h-a133l.dtb`) declares `compatible = "x-powers,axp803"` at I2C addr 0x34. TrimUI Brick actually has AXP2202. AXP803 driver writes AXP803 register values to AXP2202 chip. Different register layout → wrong voltage settings → PMIC shuts down system.
**Fix:** Set `status = "disabled"` on the pmic@34 DTB node to prevent kernel from probing the PMIC. Added `regulator_ignore_unused clk_ignore_unused` to kernel command line.
**Implications:** Need a proper AXP2202 DTB node eventually. For initial boot, disabling PMIC probe is safe since boot0 already configured voltages correctly.

### 2026-02-26 13:10 — Writing to ARISC (0x07000000) in trampoline hangs system

**Context:** Tried adding watchdog disable (0x030090B8) and ARISC reset (0x07000000) writes to the AArch64 trampoline.
**Result:** Zero serial output — device never starts boot0. The trampoline runs after RMR at EL3 with caches/MMU off. Writing to 0x07000000 (R_CPUCFG) likely causes a data abort before VBAR_EL3 is set → nested exception → hang.
**Implications:** MMIO writes in the trampoline must be done AFTER setting VBAR_EL3. Also, the R_CPUCFG address might not be correct for A133. Abandoned ARISC reset approach.

### 2026-02-27 08:41 — MMC I/O failure at ~90s is REPRODUCIBLE across all NixOS boots

**Context:** User reported boot log with massive SD card I/O errors, filesystem remounted read-only.
**Method:** Compared current boot log with "celebration" boot (1040 log — the one that prompted "🎉 NixOS FULLY BOOTS").
**Result:** EVERY NixOS boot has the same pattern:
- Boot succeeds through to multi-user target
- tty1 getty crash-loops (Sessions 3→4→5→6) — no framebuffer available
- At ~90-96 seconds: `mmc0/mmc1: Card stuck being busy! __mmc_poll_for_busy`
- HW reset fails (`error -110` = ETIMEDOUT)
- `sunxi-mmc 4020000.mmc: data error, sending stop command` / `send stop command failed`
- Cascading I/O errors on all write operations (sector 5507xxx, inode 143081/143084)
- EXT4 journal abort → filesystem remounted read-only

Device numbering varies between boots: mmcblk0 or mmcblk1 = SD card (depends on whether eMMC init fails first).
All errors are WRITE operations (op 0x1). The celebration boot (1040 log) had 101 MMC error lines.

**Root cause chain:**
1. tty1 getty crash-loop → sustained systemd journal writes to ext4 on SD card
2. EMAC driver probe + timeout → additional log writes
3. DHCP retries (1m47s timeout, no WiFi driver) → more log writes
4. Sustained write burst overwhelms SD card internal controller at ~90s
5. "Card stuck being busy" → HW reset fails → I/O cascade → read-only

**Implications:** Must reduce SD write pressure: volatile journal, kill getty loop, tmpfs for logs/tmp.

### 2026-02-27 08:41 — DTB overlay IS correctly applied in latest build, but older builds were not

**Context:** Boot log showed EMAC probing and watchdog enabled despite overlay disabling them.
**Method:** Extracted DTB from current `result` image (built 08:34 today) and decompiled.
**Result:** Current build has all overlays correctly applied:
- EMAC: `status = "disabled"` ✓
- Watchdog: `status = "disabled"` ✓
- PSCI: `compatible = "disabled"` ✓
- CPUs: `enable-method = "spin-table"` ✓
- mmc0: `no-1-8-v`, `max-frequency = 25MHz` ✓

The running boot was from an older image build. Stale nix store paths (`8b296h...`) had 0 overlay changes. Reflashing with latest build will fix EMAC/watchdog probes.
**Implications:** After rebuilding with SD write fixes, reflash needed.

### 2026-02-27 08:41 — Fixes applied to reduce SD write pressure

**Context:** MMC I/O failure caused by write storm from getty crash-loop + journal + DHCP retries.
**Method:** Modified `modules/trimui-brick.nix`:
1. **Getty crash-loop killed:** `autovt@` ConditionPathExists set to nonexistent path, `getty` target wantedBy force-emptied
2. **Journal to RAM:** `Storage=volatile`, `RuntimeMaxUse=16M`, forwarded to serial console
3. **tmpfs mounts:** `/tmp` (64M), `/var/tmp` (32M), `/var/log` (32M) all on tmpfs
4. **ext4 write reduction:** `commit=120` (2min flush), `barrier=0`
5. **DHCP backgrounded:** `dhcpcd.wait = "background"` (don't block boot on network)
**Result:** All changes evaluate correctly via `nix eval`. Rebuild + reflash needed.
**Implications:** With journal in RAM and getty loop killed, the ~90s write burst should be eliminated. If MMC errors persist after these fixes, the problem is the SD card itself or power supply instability.

### 2026-02-26 13:14 — ATF does NOT access watchdog registers

**Context:** Searched vendor ATF binary (monitor.bin, 70KB) for watchdog addresses 0x030090xx.
**Result:** No precise WDT address patterns found. ATF does not start a watchdog.
**Implications:** The reboot is not from an ATF-started watchdog. Most likely caused by PMIC (AXP2202) shutting down due to wrong register writes from AXP803 driver.

### 2026-02-26 11:25 — Full boot chain component inventory and provenance

**Context:** Need a complete record of every stage in the boot chain, what it does, where it comes from, and what was modified.
**Method:** Traced through HANDOVER.md, build scripts, u-boot-sunxi git log, vendor firmware, and NixOS image builder.

**Result:**

#### Complete Boot Chain — TrimUI Brick (A133) NixOS Boot

```
Stage 0: BROM (mask ROM)
├── Source: Allwinner silicon (burned into SoC, not modifiable)
├── Function: Reads SD sector 16 (then eMMC if no SD). Validates eGON header
│   (magic + checksum). Loads boot0 to SRAM.
├── Modified: No (cannot be)
└── Key detail: [CORRECTED — see 2026-02-28 entry above] BROM accepts both vendor
    boot0 and mainline SPL eGON format. The blocker is missing DRAM write training
    in mainline, not the header format.

Stage 1: boot0 (vendor SPL — DRAM init)
├── Source: VENDOR — extracted from stock TG3040 firmware (sd_recovery image)
│   File: firmware/boot0.bin (65536 bytes)
│   Commit: 01e601494d (Allwinner BSP)
├── Function: Initializes LPDDR3 DRAM (672MHz, 1024MB), clocks, MMC controller.
│   Then reads boot_package from SD sector 32800, validates magic+checksum,
│   loads items (U-Boot, ATF, SCP, DTB) into DRAM.
├── Modified: No — used as-is from vendor. Identical binary on SD and eMMC.
├── Why not mainline: [CORRECTED — see 2026-02-28 entry] BROM accepts mainline SPL
│   fine. The real blocker is a 16-byte write offset bug in mainline's DRAM driver
│   caused by missing write leveling/training. See re_tools/RE_PLAN.md.
└── Key detail: Loads at SRAM 0x20000. DRAM at 0x40000000. boot_package buffer 0x42e00000.

Stage 2: boot_package container
├── Source: VENDOR base — original from extracted/uboot/boot_package.bin
│   Modified by: scripts/build_boot_package_v20_core0_rmr.py (OURS)
├── Function: sunxi-package container holding 4 items. boot0 reads item directory
│   and loads each item to its load address.
├── Modified: YES — items[0] (u-boot) and items[1] (monitor/ATF) replaced/patched.
│   Package checksum at +0x14 recomputed. Magic at +0x10 preserved.
├── Format: "sunxi-package" header, 4 items × 0x170 directory entries
│   [0] u-boot   offset=0x000800 load=0x4A000000 — REPLACED (see Stage 3+4)
│   [1] monitor  offset=0x0C0800 load=0x48000000 — PATCHED (see Stage 5)
│   [2] scp      offset=0x0D1C00 load=0x00000000 — VENDOR (ARISC firmware, unmodified)
│   [3] dtb      offset=0x0E6000 load=0x00000000 — VENDOR (unmodified, unused by mainline)
└── Key detail: Item sub-headers must preserve vendor "uboot\0"/"monitor\0" magic strings.

Stage 3: AArch32 shim + AArch64 trampoline (inside boot_package item[0])
├── Source: OURS — generated by scripts/build_boot_package_v20_core0_rmr.py
│   Injected into boot_package item[0] at offsets +0x0640 and +0x1000
├── Function: Bridges vendor ATF's AArch32 EL1 ERET to mainline U-Boot's AArch64 entry.
│   (a) AArch32 shim at 0x4A000640 (core 0, entered from ATF ERET in AArch32 EL1):
│       - Copies AArch64 trampoline from 0x4A001000 to 0x4A100000
│       - Cleans D-cache (DCCMVAC) for trampoline + U-Boot regions
│       - SMC(0x8400FF00, trampoline_addr=0x4A100000, rvbar=0x08100040)
│       - Core 0 warm-resets to AArch64 EL3 at trampoline
│   (b) AArch64 trampoline at 0x4A100000 (core 0, EL3 after RMR):
│       - Copies u-boot-dtb.bin from 0x4A002000 to 0x4A000000 (CONFIG_TEXT_BASE)
│       - Jumps to 0x4A000000
├── Modified: N/A — entirely new code written for this project
└── Why needed: Vendor ATF always ERETs to BL33 in AArch32 EL1. Mainline U-Boot
    requires AArch64 EL3 (for PSCI setup, MMU init). Direct ATF patching of
    SPSR/SCR_EL3 never worked (v7-v12 all silent hang). RMR is the only reliable
    mechanism to switch execution state.

Stage 4: Mainline U-Boot (inside boot_package item[0] at +0x2000)
├── Source: MAINLINE — u-boot-sunxi.git, branch: next
│   Repository: git://git.denx.de/u-boot-sunxi.git
│   File used: u-boot-dtb.bin (~697KB)
│   Key commits (on next, not yet on master):
│     7d1936aef7c  clk: sunxi: Add support for the A100/A133 CCU
│     17c1add3277  pinctrl: sunxi: add Allwinner A100/A133 pinctrl description
│     fb4c3b2a049  sunxi: add support for the Allwinner A100/A133 SoC
│     7a337270c07  sunxi: A133: add DRAM init code (by Cody Eksal)
│     2b2783a1c07  arm64: dts: allwinner: a100: set maximum MMC frequency
│     1cc93d42b24  arm64: dts: allwinner: a100: add Liontron H-A133L board DTS
│     be5038f168e  sunxi: add support for Liontron H-A133L board (defconfig)
│   Author: Andre Przywara <andre.przywara@arm.com> (all except DRAM init)
│   DRAM init: Cody Eksal <masterr3c0rd@epochal.quest>
├── Function: Full U-Boot proper. Initializes console, MMC, USB, network.
│   Loads NixOS kernel+initrd+DTB from /boot/extlinux/extlinux.conf on SD.
├── Modified: YES — config changes only, no source patches:
│   - Defconfig based on liontron-h-a133l_defconfig
│   - CONFIG_DEBUG_UART_SHIFT=2 (A133 UART reg-shift, was 0)
│   - CONFIG_DEBUG_UART_BASE=0x05000000
│   - CONFIG_DEBUG_UART_ANNOUNCE=y
│   - LPDDR3 DRAM timing parameters from vendor boot0 (different from Liontron's LPDDR4)
├── NOT used: u-boot-sunxi-with-spl.bin — [CORRECTED] BROM accepts mainline SPL,
│   but mainline DRAM training is broken (16-byte write offset). Vendor boot0+ATF used instead.
└── Key detail: CONFIG_TEXT_BASE=0x4A000000 matches vendor boot_package load address.
    CONFIG_POSITION_INDEPENDENT=n → must be at exact address. CONFIG_ARMV8_MULTIENTRY=n.

Stage 5: Vendor ATF BL31 (inside boot_package item[1])
├── Source: VENDOR — extracted from stock boot_package at offset 0xC0800
│   Binary: re_tools/monitor.bin (70412 bytes, loads at 0x48000000)
│   Version: BL3-1 v1.0(debug):406d5ac, Built 2022-05-07
├── Function: ARM Trusted Firmware EL3 runtime. Provides PSCI services (CPU_ON,
│   SYSTEM_RESET, etc). Sets up secure world. ERETs to BL33 (U-Boot) entry.
├── Modified: YES — 2 patches by OURS (scripts/build_boot_package_v20_core0_rmr.py):
│   (a) Exception vector redirect at monitor+0x10614:
│       Original: B.EQ 0xC064 (normal AArch32 SMC handler)
│       Patched:  B.EQ 0x10624 (our custom RMR handler first)
│   (b) Custom RMR SMC handler at monitor+0x10624 (was NOP space):
│       - Intercepts SMC function ID 0x8400FF00
│       - Writes RVBAR from X1 to MMIO at [X2] (core reset vector)
│       - Sets RMR_EL3 = 3 (AA64 + warm reset request)
│       - WFI → core warm-resets in AArch64 EL3 at RVBAR
│       - Non-matching SMCs fall through to normal handler
├── Why not mainline ATF: No A100/A133 platform exists in mainline ATF.
│   Available: sun50i_a64, sun50i_h6, sun50i_h616, sun50i_r329.
│   Vendor ATF loads to DRAM 0x48000000; mainline expects SRAM 0x00104000.
│   Would require writing a new platform from scratch.
└── Key detail: Vendor ATF always sets SPSR_EL3=0x1d3 (AArch32 SVC) for BL33.
    Hardcoded BL33 entry at 0x4A000000. PSCI v1.0. CPU_ON always AArch32.

Stage 6: Vendor SCP firmware (inside boot_package item[2])
├── Source: VENDOR — unmodified from stock boot_package
├── Function: ARISC (OpenRISC) coprocessor firmware. Manages power states,
│   thermal monitoring, wake-from-suspend.
├── Modified: No
└── Key detail: Loads at 0x00000000 (ARISC SRAM). Not relevant to boot chain.

Stage 7: NixOS kernel (loaded by U-Boot from SD card)
├── Source: MAINLINE — nixpkgs linuxPackages_latest (kernel 6.12.63 currently)
│   Built by: NixOS build system via kernel/default.nix
├── Function: Linux kernel. Loaded as Image + initrd from /boot/ on SD FAT partition.
├── Modified: No source patches. Config via NixOS module system.
├── Status: LOADS but SILENT — "Starting kernel ..." then no output.
│   Known issues:
│   - DTB path mismatch (sun50i-a133-liontron-h-a133l.dtb not found, falls back to U-Boot FDT)
│   - A133 DTS may not be in kernel 6.12 (added to linux-next/sunxi, not yet released)
│   - earlycon/console parameters may need tuning
└── Key detail: U-Boot passes built-in FDT at 0x7bf29930 as fallback.

Stage 8: NixOS initrd + userspace
├── Source: NIXOS — built by NixOS configuration.nix
├── Function: Initial ramdisk → stage-2 init → systemd → NixOS
├── Modified: Custom hardware module (modules/trimui-brick.nix)
└── Status: UNKNOWN — kernel silent, can't tell if initrd loads.
```

**Summary of provenance:**
| Component | Source | Modified? | By whom |
|-----------|--------|-----------|---------|
| BROM | Allwinner silicon | No | — |
| boot0 | Vendor BSP | No | — |
| boot_package container | Vendor | Yes (items replaced, checksum recomputed) | Ours |
| AArch32 shim + trampoline | New code | N/A | Ours |
| U-Boot proper | Mainline u-boot-sunxi `next` | Config only (UART shift, DRAM params) | Andre Przywara + Cody Eksal (code), Ours (config) |
| ATF BL31 | Vendor | Yes (2 patches: vector redirect + RMR handler) | Ours |
| SCP firmware | Vendor | No | — |
| Linux kernel | Mainline nixpkgs | No | — |
| NixOS userspace | NixOS | Custom module | Ours |

**Why this Rube Goldberg machine exists:**
1. [CORRECTED] Mainline DRAM driver has broken write training → must use vendor boot0
2. Vendor boot0 only loads vendor boot_package format → must use vendor container
3. Vendor ATF enters U-Boot in AArch32 EL1 → mainline U-Boot needs AArch64 EL3
4. ATF SPSR/SCR_EL3 patching doesn't work → need RMR to switch execution state
5. RMR requires EL3 access → patch ATF exception vector with custom SMC handler
6. U-Boot isn't position-independent → trampoline copies it to CONFIG_TEXT_BASE
7. No mainline ATF platform for A133 → stuck with vendor ATF (patched)
8. No mainline boot0 for A133 → stuck with vendor boot0

**Implications:** The minimum vendor dependency is boot0 + ATF. Both could theoretically
be replaced if: (a) someone fixes the 16-byte write offset in mainline's DRAM training
(see re_tools/RE_PLAN.md), and (b) someone writes an A133 ATF platform. The DRAM init
code is already mainline (Cody Eksal's work). The RMR shim would be unnecessary with a
proper ATF that enters U-Boot in AArch64 EL3 directly.
