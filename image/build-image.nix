# SPDX-License-Identifier: GPL-2.0-or-later
# Build SD card image for TrimUI Brick (Allwinner A133)
#
# SD layout (all sizes in 512-byte sectors):
#
#   Sector 0:       MBR partition table
#   Sector 16:      vendor boot0.bin (DRAM init, 128 sectors = 64KB)
#   Sector 32800:   modified boot_package (mainline U-Boot + patched ATF)
#   Sector 65536:   Partition 1 — FAT32 "NIXOS_BOOT" (128MB)
#                     /extlinux/extlinux.conf
#                     /nixos/<hash>-Image
#                     /nixos/<hash>-initrd
#                     /dtbs/allwinner/sun50i-a133-liontron-h-a133l.dtb (patched)
#   Sector 327680:  Partition 2 — ext4 "NIXOS_ROOT" (rest of card)
#                     NixOS root filesystem
#
# The vendor blobs (boot0, boot_package) live in the gap before partition 1.
# U-Boot reads extlinux.conf from the first FAT/ext4 partition it finds on mmc0.
#
{ pkgs, lib, config, bootPackage }:

let
  sectorSize = 512;

  # Vendor firmware offsets (must match boot0 expectations)
  boot0Sector    = 16;         #    8 KB — vendor BROM loads from here
  bootPkgSector  = 32800;     # 16.4 MB — boot0 loads boot_package from here

  # Partition layout
  bootStartSector  = 65536;   #   32 MB — well clear of boot_package
  bootSizeSectors  = 262144;  #  128 MB — kernel (~62MB) + initrd (~11MB) + DTBs + headroom
  rootStartSector  = 327680;  #  160 MB

  kernel   = config.boot.kernelPackages.kernel;
  initrd   = config.system.build.initialRamdisk;
  toplevel = config.system.build.toplevel;

  # Device tree: use the patched DTB from hardware.deviceTree if available,
  # otherwise fall back to the kernel's built-in DTB
  dtbSource = if config.hardware.deviceTree.enable
    then config.hardware.deviceTree.package
    else kernel;

  kernelParams = lib.concatStringsSep " " config.boot.kernelParams;
  closureInfo  = pkgs.closureInfo { rootPaths = [ toplevel ]; };

  # Paths for the boot partition — use nix store hashes for uniqueness
  kernelName = builtins.baseNameOf (toString kernel);
  initrdName = builtins.baseNameOf (toString initrd);

  extlinuxConf = pkgs.writeText "extlinux.conf" ''
    DEFAULT nixos-default
    TIMEOUT 30
    MENU TITLE NixOS on TrimUI Brick (A133)

    LABEL nixos-default
      MENU LABEL NixOS - Default
      LINUX /nixos/${kernelName}-Image
      INITRD /nixos/${initrdName}-initrd
      FDT /dtbs/allwinner/sun50i-a133-liontron-h-a133l.dtb
      APPEND init=${toplevel}/init ${kernelParams}
  '';

in pkgs.runCommand "nixos-trimui-brick-sd" {
  nativeBuildInputs = with pkgs; [
    dosfstools    # mkfs.vfat, mcopy
    e2fsprogs     # mkfs.ext4
    mtools        # mcopy, mmd
    util-linux    # sfdisk
    zstd
  ];
} ''
  set -euo pipefail
  mkdir -p $out

  # --- Calculate sizes ---
  closureSize=$(cat ${closureInfo}/total-nar-size)
  # 30% overhead for ext4 metadata + 256MB headroom
  rootfsSizeBytes=$(( closureSize * 130 / 100 + 256 * 1024 * 1024 ))
  rootfsSectors=$(( rootfsSizeBytes / ${toString sectorSize} ))
  totalSectors=$(( ${toString rootStartSector} + rootfsSectors ))
  totalBytes=$(( totalSectors * ${toString sectorSize} ))

  echo "=== TrimUI Brick NixOS SD Image ==="
  echo "  Kernel:    ${kernel} ($(basename ${kernel}/Image))"
  echo "  Initrd:    ${initrd}"
  echo "  DTB src:   ${dtbSource}"
  echo "  Toplevel:  ${toplevel}"
  echo "  Closure:   $(( closureSize / 1024 / 1024 )) MB"
  echo "  Rootfs:    $(( rootfsSizeBytes / 1024 / 1024 )) MB"
  echo "  Total:     $(( totalBytes / 1024 / 1024 )) MB"
  echo ""

  # Find DTB — overlay output omits dtbs/ prefix, kernel keeps it
  DTB_FILE=""
  for candidate in \
    "${dtbSource}/dtbs/allwinner/sun50i-a133-liontron-h-a133l.dtb" \
    "${dtbSource}/allwinner/sun50i-a133-liontron-h-a133l.dtb"; do
    if [ -f "$candidate" ]; then
      DTB_FILE="$candidate"
      break
    fi
  done
  if [ -z "$DTB_FILE" ]; then
    echo "ERROR: DTB not found in ${dtbSource}"
    echo "Available files:"
    find ${dtbSource}/ -name "*.dtb" | head -20
    exit 1
  fi
  echo "  DTB file:  $DTB_FILE"

  img=$TMPDIR/nixos-trimui-brick.img
  truncate -s $totalBytes $img

  # --- MBR partition table ---
  # Two partitions: FAT32 boot + ext4 root
  # The vendor firmware area (sectors 16..65535) is outside any partition
  sfdisk $img <<EOF
label: dos
unit: sectors

start=${toString bootStartSector}, size=${toString bootSizeSectors}, type=0c
start=${toString rootStartSector}, type=83
EOF

  # --- Write vendor firmware ---
  echo "Writing boot0 to sector ${toString boot0Sector}..."
  dd if=${bootPackage}/boot0.bin of=$img \
    bs=${toString sectorSize} seek=${toString boot0Sector} conv=notrunc status=none

  echo "Writing boot_package to sector ${toString bootPkgSector}..."
  dd if=${bootPackage}/boot_package_mainline_uboot.bin of=$img \
    bs=${toString sectorSize} seek=${toString bootPkgSector} conv=notrunc status=none

  # --- Build FAT32 boot partition ---
  echo "Building boot partition..."
  bootImg=$TMPDIR/boot.img
  bootBytes=$(( ${toString bootSizeSectors} * ${toString sectorSize} ))
  truncate -s $bootBytes $bootImg
  mkfs.vfat -F 32 -n NIXOS_BOOT $bootImg

  # Create directory structure
  mmd -i $bootImg ::/extlinux
  mmd -i $bootImg ::/nixos
  mmd -i $bootImg ::/dtbs
  mmd -i $bootImg ::/dtbs/allwinner

  # Copy extlinux.conf
  mcopy -i $bootImg ${extlinuxConf} ::/extlinux/extlinux.conf

  # Copy kernel
  mcopy -i $bootImg ${kernel}/Image ::/nixos/${kernelName}-Image

  # Copy initrd
  mcopy -i $bootImg ${initrd}/initrd ::/nixos/${initrdName}-initrd

  # Copy DTB (from patched device tree package)
  mcopy -i $bootImg \
    $DTB_FILE \
    ::/dtbs/allwinner/sun50i-a133-liontron-h-a133l.dtb

  # Write boot partition to image
  dd if=$bootImg of=$img \
    bs=${toString sectorSize} seek=${toString bootStartSector} conv=notrunc status=none

  # --- Build ext4 root partition ---
  echo "Building rootfs..."
  rootDir=$TMPDIR/rootfs
  mkdir -p $rootDir/{nix/store,etc,tmp,var,run,proc,sys,dev,home}
  mkdir -p $rootDir/nix/var/nix/profiles
  chmod 1777 $rootDir/tmp

  # Copy Nix store closure
  cat ${closureInfo}/store-paths | while read sp; do
    cp -a "$sp" $rootDir/nix/store/
  done

  # System profile link
  ln -sf ${toplevel} $rootDir/nix/var/nix/profiles/system

  # Top-level init symlink (kernel init= points here)
  ln -sf ${toplevel}/init $rootDir/init

  # NixOS marker
  touch $rootDir/etc/NIXOS

  # Build ext4 image
  rootImg=$TMPDIR/rootfs.img
  truncate -s $rootfsSizeBytes $rootImg
  mkfs.ext4 -L NIXOS_ROOT -d $rootDir -F -m 1 -O ^metadata_csum $rootImg

  # Write rootfs to image
  echo "Writing rootfs to image..."
  dd if=$rootImg of=$img \
    bs=${toString sectorSize} seek=${toString rootStartSector} conv=notrunc status=progress

  # --- Compress ---
  echo "Compressing..."
  zstd -T0 -3 $img -o $out/nixos-trimui-brick.img.zst

  # Also keep uncompressed for dd
  cp $img $out/nixos-trimui-brick.img

  echo ""
  echo "=== Build complete ==="
  ls -lh $out/
  echo ""
  echo "Flash with:"
  echo "  dd if=result/nixos-trimui-brick.img of=/dev/sdX bs=4M status=progress"
''
