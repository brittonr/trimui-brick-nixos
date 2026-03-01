# Firmware

Build system for TrimUI Brick boot firmware. Combines vendor blobs with
mainline U-Boot to produce a bootable boot\_package.

## Files

| File | Source | Size | Description |
|------|--------|------|-------------|
| `boot0.bin` | Vendor (extracted) | 64 KB | BROM-loaded SPL — initializes LPDDR3 DRAM, clocks, MMC. Cannot be built from source. |
| `boot_package.bin` | Vendor (extracted) | 8 MB | Template boot\_package container. Used as the base for assembly — ATF, SCP, DTB, and item directory are preserved; U-Boot slot is replaced. |
| `trimui-brick_defconfig` | Ours | 2 KB | U-Boot defconfig — Liontron H-A133L base with TrimUI LPDDR3 DRAM timings and `reg-shift=2` debug UART fix. |
| `build-boot-package.py` | Ours | 10 KB | Assembly script — patches ATF, builds AArch32→AArch64 RMR shim, inserts mainline U-Boot, recomputes checksum. |
| `u-boot.nix` | Ours | — | Nix derivation — cross-compiles mainline U-Boot 2026.01 from source. |
| `boot-package.nix` | Ours | — | Nix derivation — runs `build-boot-package.py` to assemble the final boot\_package. |

## Vendor Blobs

`boot0.bin` and `boot_package.bin` are extracted from the TrimUI TG3040
stock SD card recovery image. They are **not** covered by this project's
GPL-2.0-or-later license — they are included solely for interoperability
with the Allwinner A133 SoC boot ROM.

These blobs cannot be built from source because:

- **boot0**: Contains Allwinner's proprietary DRAM training code. The
  mainline DRAM driver has a 16-byte write offset bug caused by missing
  write leveling/training (see `re_tools/RE_PLAN.md`).

- **boot\_package.bin**: The container template preserves the vendor ATF
  (ARM Trusted Firmware), SCP (ARISC coprocessor firmware), and DTB. No
  mainline ATF platform exists for the A133. Only items [0] (U-Boot) and
  [1] (ATF binary patches) are modified by our build.

See `re_tools/RE_PLAN.md` for the path to eliminating these dependencies.

## Build

```bash
nix build .#boot-package   # Assemble boot_package (U-Boot + patched ATF + shim)
nix build .#u-boot          # Cross-compile mainline U-Boot only
```
