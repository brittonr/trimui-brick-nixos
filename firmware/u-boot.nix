# SPDX-License-Identifier: GPL-2.0-or-later
# Build mainline U-Boot for TrimUI Brick (Allwinner A133)
#
# Uses trimui-brick_defconfig: Liontron H-A133L base + LPDDR3 DRAM timings
# + debug UART with reg-shift=2.
#
# Only u-boot-dtb.bin is used — mainline SPL boots but has a DRAM write
# training bug (16-byte offset), so we use vendor boot0 for DRAM init.
#
{ lib
, stdenv
, fetchurl
, bison
, flex
, swig
, python3
, openssl
, bc
, ncurses
, dtc
, gnutls
, pkg-config
, pkgsCross
}:

let
  aarch64 = pkgsCross.aarch64-multiplatform;
  crossPrefix = "aarch64-unknown-linux-gnu-";
in stdenv.mkDerivation rec {
  pname = "u-boot-trimui-brick";
  version = "2026.01";

  src = fetchurl {
    url = "https://ftp.denx.de/pub/u-boot/u-boot-${version}.tar.bz2";
    hash = "sha256-tg1YZc79vHXajaQVbFbEWOAN51pJuAwaLlipbjCtDVQ=";
  };

  nativeBuildInputs = [
    aarch64.buildPackages.gcc
    aarch64.buildPackages.binutils
    bison flex swig python3 python3.pkgs.setuptools
    openssl bc ncurses dtc gnutls pkg-config
  ];

  postPatch = ''
    cp ${./trimui-brick_defconfig} configs/trimui-brick_defconfig
  '';

  configurePhase = ''
    make CROSS_COMPILE=${crossPrefix} trimui-brick_defconfig
  '';

  buildPhase = ''
    make CROSS_COMPILE=${crossPrefix} -j$NIX_BUILD_CORES u-boot-dtb.bin
  '';

  installPhase = ''
    mkdir -p $out
    cp u-boot-dtb.bin $out/
    cp .config $out/u-boot.config
  '';

  meta = with lib; {
    description = "Mainline U-Boot for TrimUI Brick (Allwinner A133, LPDDR3)";
    homepage = "https://source.denx.de/u-boot/u-boot";
    license = licenses.gpl2Plus;
    platforms = [ "x86_64-linux" "aarch64-linux" ];
  };
}
