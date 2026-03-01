# SPDX-License-Identifier: GPL-2.0-or-later
{
  description = "NixOS for TrimUI Brick (Allwinner A133) — mainline U-Boot + mainline kernel";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      supportedSystems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
      targetSystem = "aarch64-linux";
    in {
      nixosConfigurations.trimui-brick = nixpkgs.lib.nixosSystem {
        system = targetSystem;
        modules = [
          ./modules/trimui-brick.nix
          ./configuration.nix
          ({ lib, ... }: {
            nixpkgs.hostPlatform = lib.mkDefault "aarch64-linux";
          })
        ];
      };

      packages = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          nixosConfig = self.nixosConfigurations.trimui-brick.config;

          u-boot = pkgs.callPackage ./firmware/u-boot.nix {};
          bootPackage = pkgs.callPackage ./firmware/boot-package.nix {
            u-boot-trimui-brick = u-boot;
          };
        in {
          inherit u-boot;
          boot-package = bootPackage;

          sd-image = import ./image/build-image.nix {
            inherit pkgs bootPackage;
            lib = nixpkgs.lib;
            config = nixosConfig;
          };

          write-sd = pkgs.writeShellScriptBin "write-trimui-sd" ''
            set -euo pipefail
            if [ $# -ne 1 ]; then
              echo "Usage: write-trimui-sd /dev/sdX"
              echo "WARNING: This will overwrite all data on the device!"
              exit 1
            fi
            DEVICE="$1"
            if [ ! -b "$DEVICE" ]; then
              echo "Error: $DEVICE is not a block device"
              exit 1
            fi
            IMAGE="${self.packages.${system}.sd-image}/nixos-trimui-brick.img"
            if [ ! -f "$IMAGE" ]; then
              IMAGE="${self.packages.${system}.sd-image}/nixos-trimui-brick.img.zst"
            fi
            if [ ! -f "$IMAGE" ]; then
              echo "Error: Could not find SD image"
              exit 1
            fi
            echo "Writing $IMAGE to $DEVICE..."
            echo "This will DESTROY all data on $DEVICE!"
            read -p "Are you sure? (yes/no) " confirm
            if [ "$confirm" != "yes" ]; then
              echo "Aborted."
              exit 1
            fi
            if [[ "$IMAGE" == *.zst ]]; then
              ${pkgs.zstd}/bin/zstd -d -c "$IMAGE" | dd of="$DEVICE" bs=4M status=progress
            else
              dd if="$IMAGE" of="$DEVICE" bs=4M status=progress
            fi
            sync
            echo "Done! Insert SD card into TrimUI Brick and power on."
          '';

          default = self.packages.${system}.sd-image;
        }
      );

      devShells = forAllSystems (system:
        let pkgs = import nixpkgs { inherit system; };
        in {
          default = pkgs.mkShell {
            buildInputs = with pkgs; [
              dtc dosfstools e2fsprogs mtools parted util-linux zstd file python3
              git-lfs
            ];
            shellHook = ''
              git-lfs install --local > /dev/null 2>&1 || true
              echo "TrimUI Brick NixOS dev shell"
              echo "  nix build .#sd-image       - Build complete SD card image"
              echo "  nix build .#u-boot         - Build U-Boot only"
              echo "  nix build .#boot-package   - Build boot_package only"
              echo "  nix run .#write-sd         - Write image to SD card"
            '';
          };
        }
      );
    };
}
