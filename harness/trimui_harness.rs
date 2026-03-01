// SPDX-License-Identifier: GPL-2.0-or-later
//! TrimUI Brick NixOS Flashing Harness
//!
//! XIAO RP2350 firmware that provides:
//! - USB CDC port 0: Bidirectional UART bridge to A133 console (115200 8N1)
//! - USB CDC port 1: Control channel for reset GPIOs
//!
//! XIAO RP2350 Pin Assignments (D-label → GPIO number):
//!   D6 / GPIO0  — UART0 TX → A133 RX
//!   D7 / GPIO1  — UART0 RX ← A133 TX
//!   D2 / GPIO28 — Reset (push-pull, drives NPN/NFET transistor)
//!
//! Control protocol (CDC port 1, text lines):
//!   "reset1 assert"    — drive GPIO high (transistor on, reset asserted)
//!   "reset1 release"   — drive GPIO low (transistor off, reset released)
//!   "reset pulse <ms>" — pulse GPIO high for N ms (default 100)
//!   "status"           — report current state
//!   "ping"             — responds with "pong"
//!   "reboot"           — reboot XIAO into BOOTSEL mode
//!   "uart"             — report UART byte/error counters

#![no_std]
#![no_main]

use core::sync::atomic::{AtomicBool, AtomicU32, Ordering};

use defmt::*;
use embassy_executor::Spawner;
use embassy_rp::bind_interrupts;
use embassy_rp::gpio::{Level, Output};
use embassy_rp::peripherals::{UART0, USB};
use embassy_rp::uart::{BufferedInterruptHandler, BufferedUart, BufferedUartRx, BufferedUartTx, Config as UartConfig};
use embassy_rp::usb::{Driver, InterruptHandler as UsbInterruptHandler};
use embassy_time::Timer;
use embassy_usb::class::cdc_acm::{CdcAcmClass, State};
use embassy_usb::driver::EndpointError;
use embassy_usb::UsbDevice;
use static_cell::StaticCell;
use {defmt_rtt as _, panic_probe as _};

#[unsafe(link_section = ".bi_entries")]
#[used]
pub static PICOTOOL_ENTRIES: [embassy_rp::binary_info::EntryAddr; 4] = [
    embassy_rp::binary_info::rp_program_name!(c"TrimUI Harness"),
    embassy_rp::binary_info::rp_program_description!(c"UART bridge + reset control for TrimUI Brick A133"),
    embassy_rp::binary_info::rp_cargo_version!(),
    embassy_rp::binary_info::rp_program_build_attribute!(),
];

bind_interrupts!(struct UsbIrqs {
    USBCTRL_IRQ => UsbInterruptHandler<USB>;
});

bind_interrupts!(struct UartIrqs {
    UART0_IRQ => BufferedInterruptHandler<UART0>;
});

type MyDriver = Driver<'static, USB>;

// UART counters (reported via "uart" command on control channel)
static UART_RX_BYTES: AtomicU32 = AtomicU32::new(0);
static UART_TX_BYTES: AtomicU32 = AtomicU32::new(0);
static UART_ERR_BREAK: AtomicU32 = AtomicU32::new(0);
static UART_ERR_FRAMING: AtomicU32 = AtomicU32::new(0);
static UART_ERR_OVERRUN: AtomicU32 = AtomicU32::new(0);
static UART_ERR_OTHER: AtomicU32 = AtomicU32::new(0);
static RESET1_STATE: AtomicBool = AtomicBool::new(false);

#[embassy_executor::main]
async fn main(spawner: Spawner) {
    info!("TrimUI Harness starting...");
    let p = embassy_rp::init(Default::default());

    // --- UART setup ---
    let mut uart_config = UartConfig::default();
    uart_config.baudrate = 115200;

    static UART_TX_BUF: StaticCell<[u8; 256]> = StaticCell::new();
    static UART_RX_BUF: StaticCell<[u8; 256]> = StaticCell::new();
    let uart = BufferedUart::new(
        p.UART0,
        p.PIN_0,
        p.PIN_1,
        UartIrqs,
        UART_TX_BUF.init([0; 256]),
        UART_RX_BUF.init([0; 256]),
        uart_config,
    );
    let (uart_tx, uart_rx) = uart.split();

    // Pull-up on RX AFTER uart init (which overwrites pad config)
    embassy_rp::pac::PADS_BANK0.gpio(1).modify(|w| {
        w.set_pue(true);
        w.set_pde(false);
    });

    // --- Reset GPIO: D2=GPIO28 ---
    // Reset GPIO directly drives A133 reset line (active-low):
    // GPIO LOW  = reset asserted (device held in reset)
    // GPIO HIGH = reset released (device boots)
    let reset1 = Output::new(p.PIN_28, Level::High);



    // --- USB setup ---
    let driver = Driver::new(p.USB, UsbIrqs);

    let mut config = embassy_usb::Config::new(0xc0de, 0xca5e);
    config.manufacturer = Some("TrimUI");
    config.product = Some("Harness UART+Reset");
    config.serial_number = Some("TRIMUI001");
    config.max_power = 100;
    config.max_packet_size_0 = 64;

    static CONFIG_DESC: StaticCell<[u8; 512]> = StaticCell::new();
    static BOS_DESC: StaticCell<[u8; 256]> = StaticCell::new();
    static CONTROL_BUF: StaticCell<[u8; 128]> = StaticCell::new();

    let mut builder = embassy_usb::Builder::new(
        driver,
        config,
        CONFIG_DESC.init([0; 512]),
        BOS_DESC.init([0; 256]),
        &mut [],
        CONTROL_BUF.init([0; 128]),
    );

    // CDC 0: UART bridge
    static CDC0_STATE: StaticCell<State> = StaticCell::new();
    let cdc_uart = CdcAcmClass::new(&mut builder, CDC0_STATE.init(State::new()), 64);

    // CDC 1: Control channel
    static CDC1_STATE: StaticCell<State> = StaticCell::new();
    let cdc_ctrl = CdcAcmClass::new(&mut builder, CDC1_STATE.init(State::new()), 64);

    let usb = builder.build();

    // Split UART CDC into independent tx/rx
    let (cdc_uart_tx, cdc_uart_rx) = cdc_uart.split();

    // Spawn tasks
    spawner.spawn(usb_task(usb).unwrap());
    spawner.spawn(uart_rx_task(uart_rx, cdc_uart_tx).unwrap());
    spawner.spawn(uart_tx_task(uart_tx, cdc_uart_rx).unwrap());
    spawner.spawn(ctrl_task(cdc_ctrl, reset1).unwrap());

    info!("TrimUI Harness ready.");
    loop {
        Timer::after_secs(3600).await;
    }
}

#[embassy_executor::task]
async fn usb_task(mut usb: UsbDevice<'static, MyDriver>) -> ! {
    usb.run().await
}

// --- UART RX → USB CDC TX ---
// Always drains UART to prevent overrun. Forwards to USB only when CDC is connected.
#[embassy_executor::task]
async fn uart_rx_task(mut uart_rx: BufferedUartRx, mut cdc_tx: embassy_usb::class::cdc_acm::Sender<'static, MyDriver>) -> ! {
    use embedded_io_async::Read;

    let mut buf = [0u8; 64];
    loop {
        match uart_rx.read(&mut buf).await {
            Ok(n) if n > 0 => {
                UART_RX_BYTES.fetch_add(n as u32, Ordering::Relaxed);
                // Always try to forward to CDC; ignore errors if not connected
                match cdc_tx.write_packet(&buf[..n]).await {
                    Ok(()) => {}
                    Err(_) => {}
                }
            }
            Ok(_) => {}
            Err(embassy_rp::uart::Error::Break) => {
                UART_ERR_BREAK.fetch_add(1, Ordering::Relaxed);
            }
            Err(embassy_rp::uart::Error::Framing) => {
                UART_ERR_FRAMING.fetch_add(1, Ordering::Relaxed);
            }
            Err(embassy_rp::uart::Error::Overrun) => {
                UART_ERR_OVERRUN.fetch_add(1, Ordering::Relaxed);
            }
            Err(_) => {
                UART_ERR_OTHER.fetch_add(1, Ordering::Relaxed);
            }
        }
    }
}

// --- USB CDC RX → UART TX ---
#[embassy_executor::task]
async fn uart_tx_task(mut uart_tx: BufferedUartTx, mut cdc_rx: embassy_usb::class::cdc_acm::Receiver<'static, MyDriver>) -> ! {
    use embedded_io_async::Write;

    let mut buf = [0u8; 64];
    loop {
        cdc_rx.wait_connection().await;
        loop {
            match cdc_rx.read_packet(&mut buf).await {
                Ok(n) if n > 0 => {
                    UART_TX_BYTES.fetch_add(n as u32, Ordering::Relaxed);
                    let _ = uart_tx.write_all(&buf[..n]).await;
                    let _ = uart_tx.flush().await;
                }
                Ok(_) => {}
                Err(EndpointError::Disabled) => break,
                Err(_) => {}
            }
        }
    }
}

// --- Control channel: owns reset GPIOs directly ---
#[embassy_executor::task]
async fn ctrl_task(
    class: CdcAcmClass<'static, MyDriver>,
    mut rst1: Output<'static>,
) -> ! {

    let (mut tx, mut rx) = class.split();
    let mut cmd_buf = [0u8; 128];
    let mut cmd_len: usize;

    loop {
        rx.wait_connection().await;
        cmd_len = 0;

        'connected: loop {
            let mut buf = [0u8; 64];
            match rx.read_packet(&mut buf).await {
                Ok(n) => {
                    for &b in &buf[..n] {
                        if b == b'\n' || b == b'\r' {
                            if cmd_len > 0 {
                                let response = process_cmd(
                                    &cmd_buf[..cmd_len],
                                    &mut rst1,
                                ).await;
                                cmd_len = 0;
                                if let Err(EndpointError::Disabled) = tx.write_packet(response).await {
                                    break 'connected;
                                }
                            }
                        } else if cmd_len < cmd_buf.len() {
                            cmd_buf[cmd_len] = b;
                            cmd_len += 1;
                        }
                    }
                }
                Err(EndpointError::Disabled) => break 'connected,
                Err(_) => {}
            }
        }
    }
}

async fn process_cmd<'a>(
    cmd: &[u8],
    rst1: &mut Output<'static>,
) -> &'a [u8] {
    let cmd = core::str::from_utf8(cmd).unwrap_or("");
    let cmd = cmd.trim();

    match cmd {
        "ping" => b"pong\n",
        "reboot" => {
            embassy_rp::rom_data::reset_to_usb_boot(0, 0);
            b"OK\n"
        }
        "reset1 assert" => {
            rst1.set_low(); // LOW = reset asserted
            RESET1_STATE.store(true, Ordering::Relaxed);
            b"OK reset1 asserted (LOW)\n"
        }
        "reset1 release" => {
            rst1.set_high(); // HIGH = reset released
            RESET1_STATE.store(false, Ordering::Relaxed);
            b"OK reset1 released (HIGH)\n"
        }
        "status" => {
            if RESET1_STATE.load(Ordering::Relaxed) {
                b"STATUS reset1=asserted\n"
            } else {
                b"STATUS reset1=released\n"
            }
        }
        "uart" => {
            let brk = UART_ERR_BREAK.load(Ordering::Relaxed);
            let frm = UART_ERR_FRAMING.load(Ordering::Relaxed);
            let ovr = UART_ERR_OVERRUN.load(Ordering::Relaxed);
            let rx = UART_RX_BYTES.load(Ordering::Relaxed);
            if brk > 0 || frm > 0 || ovr > 0 {
                if brk > 0 { b"UART ERRORS (break)\n" }
                else if frm > 0 { b"UART ERRORS (framing)\n" }
                else { b"UART ERRORS (overrun)\n" }
            } else if rx > 0 {
                b"UART rx=active\n"
            } else {
                b"UART idle\n"
            }
        }
        _ if cmd.starts_with("reset pulse") => {
            let ms_str = cmd.strip_prefix("reset pulse").unwrap_or("").trim();
            let ms: u64 = parse_u64(ms_str).unwrap_or(100);
            info!("Reset pulse {}ms", ms);
            rst1.set_low();  // LOW = assert
            RESET1_STATE.store(true, Ordering::Relaxed);
            Timer::after_millis(ms).await;
            rst1.set_high(); // HIGH = release
            RESET1_STATE.store(false, Ordering::Relaxed);
            b"OK reset pulse\n"
        }
        _ => b"ERR unknown\n",
    }
}

fn parse_u64(s: &str) -> Option<u64> {
    if s.is_empty() { return None; }
    let mut val: u64 = 0;
    for b in s.bytes() {
        if b.is_ascii_digit() {
            val = val.checked_mul(10)?.checked_add((b - b'0') as u64)?;
        } else {
            return None;
        }
    }
    Some(val)
}
