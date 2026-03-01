# SPDX-License-Identifier: GPL-2.0-or-later
# Build the boot_package for TrimUI Brick
#
# Combines:
#   - Vendor boot_package.bin (container template + ATF + SCP + DTB)
#   - Mainline u-boot-dtb.bin (built from source)
#   - AArch32→AArch64 RMR shim + trampoline (generated)
#   - ATF binary patches (RMR SMC handler)
#   - U-Boot binary patches (VBAR_EL3 NOP, SCR_EL3.SMD clear)
#
# Vendor blobs that cannot be built from source:
#   - boot_package.bin: container format + ATF BL31 + SCP firmware + vendor DTB
#   - boot0.bin: BROM-loaded SPL (used separately, not part of this derivation)
#
{ runCommand, python3, u-boot-trimui-brick }:

runCommand "trimui-brick-boot-package" {
  nativeBuildInputs = [ python3 ];
} ''
  mkdir -p $out
  python3 ${./build-boot-package.py} \
    ${./boot_package.bin} \
    ${u-boot-trimui-brick}/u-boot-dtb.bin \
    $out/boot_package_mainline_uboot.bin

  # Also provide boot0 for the SD image builder
  cp ${./boot0.bin} $out/boot0.bin
''
