# SPDX-License-Identifier: GPL-2.0-or-later
# TrimUI Brick (TG3040) - Allwinner A133 (sun50iw10) handheld
#
# Boot chain:
#   BROM → vendor boot0 (DRAM init) → vendor ATF (patched) →
#   AArch32 shim → RMR → AArch64 trampoline → mainline U-Boot 2026.01 →
#   extlinux.conf → Linux kernel
#
# SD card layout:
#   Sector 16 (8KB):      vendor boot0.bin
#   Sector 32800 (16MB):  modified boot_package (mainline U-Boot + patched ATF)
#   Partition 1 (~128MB): FAT32 /boot (extlinux.conf, kernel, initrd, DTB)
#   Partition 2 (rest):   ext4 NixOS rootfs
#
{ config, lib, pkgs, ... }:

with lib;

let
  cfg = config.hardware.trimui-brick;
in {
  options.hardware.trimui-brick = {
    enable = mkEnableOption "TrimUI Brick (Allwinner A133) hardware support";
  };

  config = mkIf cfg.enable {

    # --- Boot ---

    boot = {
      # Kernel 6.18+ required — has sun50i-a133-liontron-h-a133l.dtb
      kernelPackages = pkgs.linuxPackages_6_18;

      # U-Boot loads extlinux.conf from /boot
      loader.grub.enable = false;
      loader.generic-extlinux-compatible.enable = true;

      consoleLogLevel = 7;  # verbose — we need to see everything

      kernelParams = [
        "console=ttyS0,115200n8"
        "earlycon=uart,mmio32,0x05000000"
        "rootwait"
        "nohibernate"
        # Prevent kernel from disabling "unused" regulators/clocks —
        # boot0 configured the hardware, we don't have proper PMIC driver
        "regulator_ignore_unused"
        "clk_ignore_unused"
        # LSM stack (NixOS default)
        "lsm=landlock,yama,bpf"
      ];

      # Minimal initrd modules for SD card boot
      # sunxi-mmc and phy-sun4i-usb are builtins in 6.18
      initrd.availableKernelModules = [
        "ext4"
        "mmc_block"
      ];

      supportedFilesystems = mkForce [ "vfat" "ext4" ];
    };

    # --- Device Tree ---
    # Apply TrimUI Brick overlay to the Liontron H-A133L DTB:
    #   - Disable PSCI (vendor ATF can't handle AArch64 SMC)
    #   - Disable AXP803 PMIC (TrimUI has AXP2202)
    #   - Add fixed 3.3V regulator for SD card
    #   - Limit SD card speed to 25MHz
    #   - Disable eMMC (needs AXP2202 for voltage)
    #   - Remove pinctrl supply references

    hardware.deviceTree = {
      enable = true;
      filter = "sun50i-a133-liontron-h-a133l.dtb";
      overlays = [
        {
          name = "trimui-brick";
          dtsFile = ../dtb/trimui-brick.dtso;
        }
      ];
    };

    # --- Filesystems ---

    fileSystems."/" = {
      device = "/dev/disk/by-label/NIXOS_ROOT";
      fsType = "ext4";
      # commit=120: flush journal every 2 minutes instead of every 5 seconds
      # barrier=0: disable write barriers (we accept crash-risk for SD stability)
      options = [ "noatime" "nodiratime" "commit=120" "barrier=0" ];
    };

    fileSystems."/boot" = {
      device = "/dev/disk/by-label/NIXOS_BOOT";
      fsType = "vfat";
      options = [ "noatime" "umask=0077" ];
    };

    # --- Hardware ---

    hardware = {
      enableRedistributableFirmware = true;
      graphics.enable = false;  # PowerVR GE8300 — no open driver
    };

    # --- Networking ---

    networking = {
      wireless.enable = true;   # RTL8189FTV — needs out-of-tree driver
      useDHCP = true;
      # Reduce DHCP timeout — WiFi driver doesn't work yet, so DHCP on wlan0
      # waits the full timeout (1m47s) generating retry log entries.
      dhcpcd.wait = "background";
    };

    # --- Services ---

    services.openssh = {
      enable = true;
      settings = {
        PermitRootLogin = "yes";
        PasswordAuthentication = true;
      };
    };

    # Serial console — primary interactive console
    systemd.services."serial-getty@ttyS0" = {
      enable = true;
      wantedBy = [ "getty.target" ];
    };

    # No framebuffer/display driver yet — disable ALL VT gettys.
    # Without a working console, getty@tty1 autologin crash-loops,
    # generating sustained journal writes that overwhelm the SD card
    # (~90s: "Card stuck being busy" → I/O errors → read-only remount).
    services.logind.settings.Login = {
      NAutoVTs = 0;
      ReserveVT = 0;
    };
    # Mask getty on all VTs — wantedBy override alone is insufficient
    systemd.services."getty@tty1".wantedBy = lib.mkForce [];
    # Also mask the entire getty template to prevent logind from spawning VT gettys
    systemd.targets."getty".wantedBy = lib.mkForce [];
    systemd.services."autovt@".unitConfig.ConditionPathExists = "/dev/null-nonexistent";

    # --- SD card write reduction ---
    # The SD card fails under sustained writes (~90s into boot: "Card stuck being busy",
    # cascading I/O errors, journal abort, read-only remount). Minimize writes:

    # Journal to RAM only — no persistent journal on SD card
    services.journald.extraConfig = ''
      Storage=volatile
      RuntimeMaxUse=16M
      ForwardToConsole=yes
      TTYPath=/dev/ttyS0
      MaxLevelConsole=info
    '';

    # tmpfs for temp directories
    fileSystems."/tmp" = {
      device = "tmpfs";
      fsType = "tmpfs";
      options = [ "nosuid" "nodev" "size=64M" ];
    };
    fileSystems."/var/tmp" = {
      device = "tmpfs";
      fsType = "tmpfs";
      options = [ "nosuid" "nodev" "size=32M" ];
    };
    fileSystems."/var/log" = {
      device = "tmpfs";
      fsType = "tmpfs";
      options = [ "nosuid" "nodev" "size=32M" ];
    };

    # --- Trim fat ---

    powerManagement.enable = true;
    services.power-profiles-daemon.enable = false;
    documentation.enable = false;
    documentation.nixos.enable = false;
    services.xserver.enable = false;
    security.sudo.wheelNeedsPassword = false;

    # --- Users ---

    users.users.root.initialPassword = "trimui";
  };
}
