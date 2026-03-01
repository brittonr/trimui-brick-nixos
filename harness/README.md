# TrimUI Brick Test Harness

Hardware + software for automated build → flash → boot → capture cycles,
driven by [pi](https://github.com/mariozechner/pi) + Claude without human
hands touching the hardware.

## Hardware

**Seeed XIAO RP2350** running Embassy firmware, wired to:

- **A133 UART** (115200 8N1) — GPIO0 (TX) / GPIO1 (RX)
- **A133 reset line** — GPIO28 drives NPN/NFET transistor (active-low)
- **SDWire** SD card multiplexer — switches SD between host and DUT

The XIAO appears as a USB composite device (`c0de:ca5e`) with two CDC ACM
serial ports:

| Port | Interface | Function |
|------|-----------|----------|
| ttyACM0 | 0 | Bidirectional UART bridge to A133 console |
| ttyACM1 | 2 | Control channel (reset, status) |

## Control Protocol

Text commands on ttyACM1 (control port), `\r\n` terminated:

```
ping                → pong
status              → STATUS reset1=released
reset1 assert       → OK reset1 asserted (LOW)
reset1 release      → OK reset1 released (HIGH)
reset pulse <ms>    → OK reset pulse     (assert, wait, release)
uart                → UART rx=active | UART idle | UART ERRORS (...)
reboot              → (XIAO reboots to BOOTSEL)
```

## Test Cycle

A single `trimui_harness test` command from pi runs:

1. **Build** — Nix builds U-Boot + boot\_package from source
2. **Flash** — SDWire switches SD to host, `dd` writes boot0 + boot\_package, switches back to DUT
3. **Reset** — Control port sends `reset pulse` to reboot the A133
4. **Capture** — UART port streams serial output for N seconds with live progress
5. **Analyze** — Boot log is parsed for markers (BOOT0, DRAM, ATF, kernel, errors)

This loop ran 21 iterations (v2–v21) to develop the boot chain from first
contact to NixOS login.

## Files

| File | Description |
|------|-------------|
| `trimui_harness.rs` | XIAO RP2350 firmware (Embassy, no\_std Rust) |
| `trimui-harness.ts` | pi extension — `trimui_harness` tool for the coding agent |

## Building the Firmware

The firmware is an [Embassy](https://embassy.dev/) example for the RP2350.
Build with the Embassy rp235x examples setup:

```bash
# In an Embassy checkout with rp235x examples configured
cargo build --release --bin trimui_harness --target thumbv8m.main-none-eabihf
# Convert to UF2 and flash: hold BOOTSEL, plug in XIAO, copy .uf2 to RPI-RP2 drive
```

## Wiring

```
XIAO RP2350          TrimUI Brick (A133)
─────────────        ───────────────────
D6 (GPIO0/TX)  ───→  UART RX
D7 (GPIO1/RX)  ←───  UART TX
D2 (GPIO28)    ───→  Reset (via NPN transistor to A133 /RESET)
GND            ───── GND
```
