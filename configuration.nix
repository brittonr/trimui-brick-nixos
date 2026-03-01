# SPDX-License-Identifier: GPL-2.0-or-later
# NixOS configuration for TrimUI Brick (Allwinner A133)
{ config, pkgs, lib, ... }:

{
  hardware.trimui-brick.enable = true;

  system.stateVersion = "25.05";
  networking.hostName = "trimui-brick";
  time.timeZone = "UTC";
  i18n.defaultLocale = "en_US.UTF-8";

  nix.settings = {
    experimental-features = [ "nix-command" "flakes" ];
    auto-optimise-store = true;
    max-jobs = 1;
    cores = 2;
  };

  users.users.gamer = {
    isNormalUser = true;
    extraGroups = [ "wheel" "video" "audio" "input" ];
    initialPassword = "gamer";
  };

  environment.systemPackages = with pkgs; [
    htop vim tmux file tree
    evtest
    strace dtc i2c-tools
  ];

  # Autologin on serial console
  services.getty.autologinUser = "gamer";

  # Disable unnecessary services for fast boot
  services.udisks2.enable = false;
  programs.command-not-found.enable = false;
  systemd.services.systemd-udev-settle.enable = false;
  systemd.network.wait-online.enable = false;
}
