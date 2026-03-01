// SPDX-License-Identifier: GPL-2.0-or-later
//
// Development tooling — provided for reference, not general use.
// This is a pi (https://github.com/mariozechner/pi) custom tool extension that
// requires specific hardware (XIAO RP2350 harness + SDWire) to function.
//
/**
 * TrimUI Brick Test Harness Extension
 *
 * Automates the build → flash → boot → capture cycle for the TrimUI Brick (TG3040).
 *
 * Hardware:
 *   - TrimUI Harness (USB c0de:ca5e) — dual CDC ACM:
 *     - ttyACM0 (interface 0): UART passthrough (115200 8N1)
 *     - ttyACM1 (interface 2): Control port (reset assert/release/pulse, status, ping)
 *   - SDWire SD card mux — switches SD between host (/dev/sdc) and DUT
 *
 * Control port protocol (ttyACM1, text, \r\n terminated):
 *   status         → "STATUS reset=released" | "STATUS reset=asserted"
 *   reset assert   → "OK reset asserted"     (hold SoC in reset)
 *   reset release  → "OK reset released"     (release SoC from reset)
 *   reset pulse    → "OK reset pulse"        (assert then auto-release)
 *   ping           → "pong"
 *
 * SD layout:
 *   Sector 16:     vendor boot0.bin (65536 bytes) — DRAM init
 *   Sector 32800:  modified boot_package — contains test payload
 *
 * Test cycle:
 *   1. Build boot_package (Python script)
 *   2. SDWire → TS, dd boot0+boot_package to SD, SDWire → DUT
 *   3. Reset pulse via harness control port
 *   4. Capture UART output from harness UART port
 *   5. Analyze boot log for success/failure markers
 */

import { Type } from "@mariozechner/pi-ai";
import { StringEnum } from "@mariozechner/pi-ai";
import { Text, TruncatedText, truncateToWidth } from "@mariozechner/pi-tui";
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import * as path from "node:path";
import * as fs from "node:fs";
import { spawn } from "node:child_process";

// ── Serial output sanitizer ────────────────────────────────────────
// Raw UART captures contain terminal escape sequences from systemd (OSC 3008,
// CSI cursor queries, DCS responses, etc.) that confuse TUI width calculation.
// Strip them before rendering since we apply our own styling.
function stripTermEscapes(line: string): string {
	return line
		// OSC: ESC ] ... (BEL | ESC \)
		.replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)?/g, "")
		// DCS: ESC P ... (ESC \)
		.replace(/\x1bP[^\x1b]*(?:\x1b\\)?/g, "")
		// CSI: ESC [ ... final byte
		.replace(/\x1b\[[0-9;?!]*[A-Za-z]/g, "")
		// Other ESC sequences (2-char)
		.replace(/\x1b[^[\]P]/g, "")
		// Stray control chars (but keep \t \n \r)
		.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "");
}

// ── Configuration ──────────────────────────────────────────────────
// Resolve project root relative to this extension file (harness/ is one level down)
const PROJECT_DIR = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..");
const FIRMWARE_DIR = path.join(PROJECT_DIR, "firmware");
const SERIAL_DIR = path.join(PROJECT_DIR, "serial_logs");
const SCRIPTS_DIR = path.join(PROJECT_DIR, "scripts");

const DEFAULT_SD_DEV = "/dev/sdc";
const BOOT0_BIN = `${FIRMWARE_DIR}/boot0.bin`;
const BOOT_PKG_BIN = `${FIRMWARE_DIR}/boot_package_mainline_uboot.bin`;
const BOOT0_SECTOR = 16;
const BOOTPKG_SECTOR = 32800;

const HARNESS_VID = "c0de";
const HARNESS_PID = "ca5e";
const UART_BAUD = 115200;

// ── Device detection ───────────────────────────────────────────────

interface HarnessPorts {
	uart: string;   // e.g. /dev/ttyACM0
	ctrl: string;   // e.g. /dev/ttyACM1
}

function findHarnessPorts(): HarnessPorts | null {
	// Look for ttyACM devices matching our VID:PID
	const acmDevs: { dev: string; iface: string }[] = [];
	for (const entry of fs.readdirSync("/dev").filter(n => n.startsWith("ttyACM"))) {
		const dev = `/dev/${entry}`;
		try {
			// /sys/class/tty/ttyACMN/device → symlink to USB interface dir
			const ifaceDir = fs.realpathSync(`/sys/class/tty/${entry}/device`);
			// USB device dir is parent of interface dir
			const usbDir = path.dirname(ifaceDir);

			const vid = fs.readFileSync(path.join(usbDir, "idVendor"), "utf-8").trim();
			const pid = fs.readFileSync(path.join(usbDir, "idProduct"), "utf-8").trim();
			if (vid === HARNESS_VID && pid === HARNESS_PID) {
				const ifaceNum = fs.readFileSync(path.join(ifaceDir, "bInterfaceNumber"), "utf-8").trim();
				acmDevs.push({ dev, iface: ifaceNum });
			}
		} catch { /* skip */ }
	}

	if (acmDevs.length < 2) return null;

	// Interface 00 = UART, Interface 02 = Control
	acmDevs.sort((a, b) => a.iface.localeCompare(b.iface));
	const uart = acmDevs.find(d => d.iface === "00");
	const ctrl = acmDevs.find(d => d.iface === "02");
	if (!uart || !ctrl) return null;
	return { uart: uart.dev, ctrl: ctrl.dev };
}

// ── State ──────────────────────────────────────────────────────────
interface HarnessState {
	sdwireMode: "ts" | "dut" | "unknown";
	lastBuild?: string;
	lastFlash?: string;
	lastCapture?: string;
	lastTestResult?: string;
}

let state: HarnessState = { sdwireMode: "unknown" };

// ── Boot log analysis ──────────────────────────────────────────────
interface BootAnalysis {
	hasOutput: boolean;
	boot0Started: boolean;
	bootSource: "sd" | "emmc" | "unknown";
	dramInit: boolean;
	bootPkgLoaded: boolean;
	atfStarted: boolean;
	atfSpsr?: string;
	customCodeOutput: string[];
	kernelStarted: boolean;
	errors: string[];
	summary: string;
}

function analyzeBootLog(log: string): BootAnalysis {
	const lines = log.split("\n");
	const result: BootAnalysis = {
		hasOutput: log.trim().length > 50,
		boot0Started: false,
		bootSource: "unknown",
		dramInit: false,
		bootPkgLoaded: false,
		atfStarted: false,
		customCodeOutput: [],
		kernelStarted: false,
		errors: [],
		summary: "",
	};

	for (const line of lines) {
		if (line.includes("HELLO! BOOT0")) result.boot0Started = true;
		if (line.includes("card no is 0")) result.bootSource = "sd";
		if (line.includes("card no is 2")) result.bootSource = "emmc";
		if (line.includes("DRAM simple test OK")) result.dramInit = true;
		if (line.includes("Loading boot-pkg Succeed")) result.bootPkgLoaded = true;
		if (line.includes("BL3-1:")) result.atfStarted = true;

		const spsrMatch = line.match(/spsr\s*=\s*(0x[0-9a-fA-F]+)/);
		if (spsrMatch) result.atfSpsr = spsrMatch[1];

		if (/^V3!/.test(line.trim())) result.customCodeOutput.push(line.trim());
		if (/^15:/.test(line.trim())) result.customCodeOutput.push(line.trim());
		if (/^64:/.test(line.trim())) result.customCodeOutput.push(line.trim());
		if (/^32:/.test(line.trim())) result.customCodeOutput.push(line.trim());
		if (/S3=|S6=|CPU_ON/.test(line)) result.customCodeOutput.push(line.trim());

		if (line.includes("Starting kernel")) result.kernelStarted = true;

		if (/error|fail|panic|abort|crash/i.test(line) && !/tspd_fast/.test(line)) {
			result.errors.push(line.trim());
		}
	}

	const parts: string[] = [];
	if (!result.hasOutput) {
		parts.push("❌ No serial output captured");
	} else {
		if (result.boot0Started) parts.push("✓ BOOT0");
		parts.push(
			result.bootSource === "sd" ? "✓ SD boot" :
			result.bootSource === "emmc" ? "⚠ eMMC boot" : "? boot source"
		);
		if (result.dramInit) parts.push("✓ DRAM");
		if (result.bootPkgLoaded) parts.push("✓ boot_package");
		if (result.atfStarted) parts.push(`✓ ATF (spsr=${result.atfSpsr ?? "?"})`);
		if (result.customCodeOutput.length > 0) parts.push(`✓ Custom: ${result.customCodeOutput[0]}`);
		if (result.kernelStarted) parts.push("✓ Kernel");
		if (result.errors.length > 0) parts.push(`⚠ ${result.errors.length} error(s)`);
	}
	result.summary = parts.join(" | ");
	return result;
}

// ── Extension ──────────────────────────────────────────────────────
export default function (pi: ExtensionAPI) {

	async function exec(cmd: string, args: string[], opts?: {
		signal?: AbortSignal; timeout?: number;
	}) {
		return pi.exec(cmd, args, { signal: opts?.signal, timeout: opts?.timeout ?? 30000 });
	}

	async function sdwireSwitch(mode: "ts" | "dut", signal?: AbortSignal): Promise<string> {
		const r = await exec("sdwire-cli", ["switch", mode], { signal, timeout: 15000 });
		if (r.code !== 0) throw new Error(`SDWire switch ${mode} failed: ${r.stderr || r.stdout}`);
		state.sdwireMode = mode;
		return mode === "ts" ? "SD → Host (TS)" : "SD → TrimUI (DUT)";
	}

	/** Send a command to the harness control port, return response line */
	async function harnessCtrl(cmd: string, ctrlPort: string, signal?: AbortSignal): Promise<string> {
		// Use stty + echo + read via a small shell script for reliability
		const script = `
			exec 3<>"${ctrlPort}"
			stty -F "${ctrlPort}" 115200 raw -echo
			printf '%s\\r\\n' '${cmd}' >&3
			timeout 2 head -n1 <&3
			exec 3>&-
		`;
		const r = await exec("bash", ["-c", script], { signal, timeout: 5000 });
		return r.stdout.trim();
	}

	/** Capture UART data with streaming updates via onUpdate callback */
	async function captureUartStreaming(
		uartPort: string,
		durationSecs: number,
		signal: AbortSignal | undefined,
		onUpdate: ((partial: any) => void) | undefined,
	): Promise<string> {
		// Configure port first
		await exec("stty", ["-F", uartPort, String(UART_BAUD), "raw", "-echo", "-hupcl", "clocal"], { signal, timeout: 3000 });

		return new Promise<string>((resolve) => {
			let logContent = "";
			let lineCount = 0;
			const startTime = Date.now();

			const proc = spawn("cat", [uartPort], { stdio: ["ignore", "pipe", "ignore"] });

			const sendUpdate = () => {
				const elapsed = Math.round((Date.now() - startTime) / 1000);
				const lines = logContent.split("\n");
				lineCount = lines.length;
				// Show last 25 lines of output
				const tailLines = lines.slice(-25).join("\n");
				const byteCount = Buffer.byteLength(logContent);
				onUpdate?.({
					content: [{ type: "text", text: tailLines || "(waiting for data...)" }],
					details: {
						streaming: true,
						elapsed,
						duration: durationSecs,
						bytes: byteCount,
						lines: lineCount,
						tailLines,
					},
				});
			};

			// Throttled update — every 250ms at most
			let updateTimer: ReturnType<typeof setInterval> | null = null;
			let pendingUpdate = false;

			const scheduleUpdate = () => {
				pendingUpdate = true;
				if (!updateTimer) {
					updateTimer = setInterval(() => {
						if (pendingUpdate) {
							sendUpdate();
							pendingUpdate = false;
						}
					}, 250);
				}
			};

			proc.stdout.on("data", (chunk: Buffer) => {
				logContent += chunk.toString("utf-8");
				scheduleUpdate();
			});

			// Also send periodic updates even without data (to show elapsed time)
			const tickTimer = setInterval(() => {
				sendUpdate();
			}, 2000);

			const cleanup = () => {
				if (updateTimer) { clearInterval(updateTimer); updateTimer = null; }
				clearInterval(tickTimer);
				try { proc.kill("SIGTERM"); } catch {}
				// Final update
				sendUpdate();
				resolve(logContent);
			};

			// Timeout
			const timeoutId = setTimeout(cleanup, durationSecs * 1000);

			// Signal abort
			if (signal) {
				signal.addEventListener("abort", () => {
					clearTimeout(timeoutId);
					cleanup();
				}, { once: true });
			}

			proc.on("error", () => {
				clearTimeout(timeoutId);
				cleanup();
			});

			proc.on("exit", () => {
				// Process exited before timeout (e.g., port disconnected)
				clearTimeout(timeoutId);
				cleanup();
			});
		});
	}

	function timestamp(): string {
		const d = new Date();
		return d.toISOString().replace(/[:.]/g, "").replace("T", "_").slice(0, 15);
	}

	function findLatestBuildScript(): string | undefined {
		try {
			const files = fs.readdirSync(SCRIPTS_DIR)
				.filter(f => f.match(/^build_boot_package_v\d+/))
				.sort();
			return files.length > 0 ? path.join(SCRIPTS_DIR, files[files.length - 1]) : undefined;
		} catch { return undefined; }
	}

	// ── Tool ────────────────────────────────────────────────────────

	pi.registerTool({
		name: "trimui_harness",
		label: "TrimUI Harness",
		description: [
			"TrimUI Brick test harness. Automates the build → flash → boot → capture cycle.",
			"",
			"Actions:",
			'  "flash"   - Switch SDWire to TS, flash boot0+boot_package to SD, switch to DUT.',
			'  "capture" - Capture UART serial output for duration_secs (default 30). Power-cycle TrimUI after starting.',
			'  "test"    - Full cycle: optional build → flash → capture → analyze. Power-cycle TrimUI when prompted by capture.',
			'  "build"   - Run a boot_package build script (default: latest v* script).',
			'  "analyze" - Analyze an existing serial log file for boot markers.',
			'  "status"  - Show current harness state.',
			"",
			"Hardware: SDWire SD mux + Glasgow UART (3.3V, 115200, RX=A0 TX=A1).",
			"SD layout: boot0 at sector 16, boot_package at sector 32800.",
			"After 'flash' or 'test', power-cycle the TrimUI to boot from the new SD image.",
		].join("\n"),
		parameters: Type.Object({
			action: StringEnum(["flash", "capture", "test", "build", "analyze", "status"] as const, {
				description: "Harness action to perform",
			}),
			build_script: Type.Optional(Type.String({
				description: "Path to build script (default: auto-detect latest). Use 'skip' to skip build in 'test' action.",
			})),
			duration_secs: Type.Optional(Type.Number({
				description: "UART capture duration in seconds (default: 30)",
			})),
			log_file: Type.Optional(Type.String({
				description: "Serial log file to analyze (for 'analyze' action)",
			})),
			sd_dev: Type.Optional(Type.String({
				description: "SD card block device (default: /dev/sdc)",
			})),
		}),

		async execute(_toolCallId, params, signal, onUpdate, _ctx) {
			const { action, build_script, duration_secs, log_file, sd_dev } = params as {
				action: "flash" | "capture" | "test" | "build" | "analyze" | "status";
				build_script?: string;
				duration_secs?: number;
				log_file?: string;
				sd_dev?: string;
			};

			const sdDev = sd_dev ?? DEFAULT_SD_DEV;
			const captureSecs = duration_secs ?? 30;

			// Find harness ports
			const ports = findHarnessPorts();

			try {
				switch (action) {

				// ── STATUS ───────────────────────────────────────
				case "status": {
					// Harness
					let harnessInfo = "not detected";
					let resetState = "unknown";
					if (ports) {
						harnessInfo = `UART=${ports.uart} CTRL=${ports.ctrl}`;
						try {
							resetState = await harnessCtrl("status", ports.ctrl, signal);
						} catch {}
					}

					// SDWire
					let sdwireInfo = "unknown";
					try {
						const r = await exec("sdwire-cli", ["state"], { signal, timeout: 10000 });
						sdwireInfo = r.stdout.trim();
						if (r.stdout.toLowerCase().includes("host")) state.sdwireMode = "ts";
						else if (r.stdout.toLowerCase().includes("target")) state.sdwireMode = "dut";
					} catch {}

					// SD
					let sdInfo = "not found";
					try {
						const r = await exec("lsblk", ["-dno", "SIZE", sdDev], { signal, timeout: 5000 });
						if (r.code === 0) sdInfo = `${sdDev} (${r.stdout.trim()})`;
					} catch {}

					// Firmware
					const boot0Ok = fs.existsSync(BOOT0_BIN);
					const pkgOk = fs.existsSync(BOOT_PKG_BIN);
					const pkgSize = pkgOk ? `${fs.statSync(BOOT_PKG_BIN).size} bytes` : "N/A";
					const latestScript = findLatestBuildScript();

					return {
						content: [{ type: "text", text: [
							"TrimUI Brick Test Harness Status",
							"─────────────────────────────────",
							`Harness:       ${harnessInfo}`,
							`Reset:         ${resetState}`,
							`SDWire:        ${sdwireInfo}`,
							`SD card:       ${sdInfo}`,
							`boot0.bin:     ${boot0Ok ? "✓" : "✗ missing"}`,
							`boot_package:  ${pkgOk ? `✓ ${pkgSize}` : "✗ missing"}`,
							`Latest script: ${latestScript ? path.basename(latestScript) : "none"}`,
							`Last build:    ${state.lastBuild ?? "none"}`,
							`Last flash:    ${state.lastFlash ?? "none"}`,
							`Last capture:  ${state.lastCapture ?? "none"}`,
							`Last result:   ${state.lastTestResult ?? "none"}`,
						].join("\n") }],
						details: { action, ports, state: { ...state } },
					};
				}

				// ── BUILD ────────────────────────────────────────
				case "build": {
					let script = build_script;
					if (!script || script === "auto") {
						script = findLatestBuildScript();
					}
					if (!script) {
						return { content: [{ type: "text", text: "No build scripts found in scripts/" }], isError: true };
					}
					if (!fs.existsSync(script)) {
						return { content: [{ type: "text", text: `Build script not found: ${script}` }], isError: true };
					}

					onUpdate?.({ content: [{ type: "text", text: `Building: ${path.basename(script)}...` }] });
					const r = await exec("nix-shell", ["-p", "python3", "--run", `python3 ${script}`], { signal, timeout: 60000 });
					state.lastBuild = path.basename(script);

					if (r.code !== 0) {
						return {
							content: [{ type: "text", text: `Build failed:\n${r.stdout}\n${r.stderr}` }],
							details: { action, script, error: true },
							isError: true,
						};
					}

					return {
						content: [{ type: "text", text: `Build succeeded: ${path.basename(script)}\n\n${r.stdout}` }],
						details: { action, script, state: { ...state } },
					};
				}

				// ── FLASH ────────────────────────────────────────
				case "flash": {
					const steps: string[] = [];

					// Switch to TS
					onUpdate?.({ content: [{ type: "text", text: "SDWire → TS..." }] });
					steps.push(await sdwireSwitch("ts", signal));
					await new Promise(r => setTimeout(r, 2000));

					// Verify SD
					const lsblk = await exec("lsblk", ["-dno", "SIZE", sdDev], { signal, timeout: 5000 });
					if (lsblk.code !== 0) {
						return { content: [{ type: "text", text: `SD not found at ${sdDev}` }], isError: true };
					}
					steps.push(`SD: ${sdDev} (${lsblk.stdout.trim()})`);

					// Check firmware
					if (!fs.existsSync(BOOT0_BIN)) {
						return { content: [{ type: "text", text: `boot0.bin missing: ${BOOT0_BIN}` }], isError: true };
					}
					if (!fs.existsSync(BOOT_PKG_BIN)) {
						return { content: [{ type: "text", text: `boot_package missing: ${BOOT_PKG_BIN}\nRun build first.` }], isError: true };
					}

					// Flash boot0
					onUpdate?.({ content: [{ type: "text", text: "Flashing boot0..." }] });
					const dd1 = await exec("dd", [`if=${BOOT0_BIN}`, `of=${sdDev}`, "bs=512", `seek=${BOOT0_SECTOR}`, "conv=notrunc"], { signal, timeout: 30000 });
					if (dd1.code !== 0) {
						return { content: [{ type: "text", text: `Flash boot0 failed:\n${dd1.stderr}` }], isError: true };
					}
					steps.push(`boot0 → sector ${BOOT0_SECTOR}`);

					// Flash boot_package
					onUpdate?.({ content: [{ type: "text", text: "Flashing boot_package..." }] });
					const dd2 = await exec("dd", [`if=${BOOT_PKG_BIN}`, `of=${sdDev}`, "bs=512", `seek=${BOOTPKG_SECTOR}`, "conv=notrunc"], { signal, timeout: 30000 });
					if (dd2.code !== 0) {
						return { content: [{ type: "text", text: `Flash boot_package failed:\n${dd2.stderr}` }], isError: true };
					}
					steps.push(`boot_package → sector ${BOOTPKG_SECTOR}`);

					// Sync
					await exec("sync", [], { signal, timeout: 10000 });
					steps.push("sync");

					// Switch to DUT
					onUpdate?.({ content: [{ type: "text", text: "SDWire → DUT..." }] });
					steps.push(await sdwireSwitch("dut", signal));
					state.lastFlash = timestamp();

					return {
						content: [{ type: "text", text: `Flash complete:\n${steps.map(s => `  • ${s}`).join("\n")}\n\nUse 'capture' action to reset and capture boot output.` }],
						details: { action, steps, state: { ...state } },
					};
				}

				// ── CAPTURE ──────────────────────────────────────
				case "capture": {
					if (!ports) {
						return { content: [{ type: "text", text: "TrimUI Harness not found (USB c0de:ca5e). Check connection." }], isError: true };
					}

					const ts = timestamp();
					const logFile = path.join(SERIAL_DIR, `boot_test_${ts}.log`);

					// Pulse reset to reboot TrimUI
					onUpdate?.({ content: [{ type: "text", text: "Resetting TrimUI..." }] });
					const resetResp = await harnessCtrl("reset pulse", ports.ctrl, signal);

					// Capture UART with streaming updates
					const logContent = await captureUartStreaming(ports.uart, captureSecs, signal, onUpdate);

					// Save
					fs.mkdirSync(SERIAL_DIR, { recursive: true });
					fs.writeFileSync(logFile, logContent);
					state.lastCapture = logFile;

					// Analyze
					const analysis = analyzeBootLog(logContent);
					state.lastTestResult = analysis.summary;

					const logLines = logContent.split("\n");
					let displayLog = logContent;
					if (logLines.length > 130) {
						displayLog = [
							...logLines.slice(0, 100),
							`\n... (${logLines.length - 120} lines omitted) ...\n`,
							...logLines.slice(-20),
						].join("\n");
					}

					return {
						content: [{ type: "text", text: [
							`UART capture complete (${captureSecs}s)`,
							`Reset: ${resetResp}`,
							`Log: ${logFile} (${Buffer.byteLength(logContent)} bytes, ${logLines.length} lines)`,
							``,
							`Analysis: ${analysis.summary}`,
							``,
							`── Serial Output ──`,
							displayLog || "(no output)",
						].join("\n") }],
						details: { action, logFile, analysis, resetResp, state: { ...state } },
					};
				}

				// ── TEST (full cycle) ────────────────────────────
				case "test": {
					if (!ports) {
						return { content: [{ type: "text", text: "TrimUI Harness not found (USB c0de:ca5e). Check connection." }], isError: true };
					}

					const steps: string[] = [];
					const ts = timestamp();

					// 1. Build
					if (build_script !== "skip") {
						let script = build_script;
						if (!script || script === "auto") script = findLatestBuildScript();
						if (script && fs.existsSync(script)) {
							onUpdate?.({ content: [{ type: "text", text: `Building: ${path.basename(script)}...` }] });
							const br = await exec("nix-shell", ["-p", "python3", "--run", `python3 ${script}`], { signal, timeout: 60000 });
							if (br.code !== 0) {
								return { content: [{ type: "text", text: `Build failed:\n${br.stdout}\n${br.stderr}` }], isError: true };
							}
							state.lastBuild = path.basename(script);
							steps.push(`Built: ${path.basename(script)}`);
						}
					} else {
						steps.push("Build: skipped");
					}

					// 2. Flash
					onUpdate?.({ content: [{ type: "text", text: "SDWire → TS, flashing..." }] });
					await sdwireSwitch("ts", signal);
					await new Promise(r => setTimeout(r, 2000));

					const lsblk = await exec("lsblk", ["-dno", "SIZE", sdDev], { signal, timeout: 5000 });
					if (lsblk.code !== 0) {
						return { content: [{ type: "text", text: `SD not found at ${sdDev}` }], isError: true };
					}

					if (!fs.existsSync(BOOT0_BIN) || !fs.existsSync(BOOT_PKG_BIN)) {
						return { content: [{ type: "text", text: "Firmware files missing. Run build first." }], isError: true };
					}

					const dd1 = await exec("dd", [`if=${BOOT0_BIN}`, `of=${sdDev}`, "bs=512", `seek=${BOOT0_SECTOR}`, "conv=notrunc"], { signal, timeout: 30000 });
					if (dd1.code !== 0) return { content: [{ type: "text", text: `Flash boot0 failed:\n${dd1.stderr}` }], isError: true };
					steps.push(`boot0 → sector ${BOOT0_SECTOR}`);

					const dd2 = await exec("dd", [`if=${BOOT_PKG_BIN}`, `of=${sdDev}`, "bs=512", `seek=${BOOTPKG_SECTOR}`, "conv=notrunc"], { signal, timeout: 30000 });
					if (dd2.code !== 0) return { content: [{ type: "text", text: `Flash boot_package failed:\n${dd2.stderr}` }], isError: true };
					steps.push(`boot_package → sector ${BOOTPKG_SECTOR}`);

					await exec("sync", [], { signal, timeout: 10000 });

					// 3. Switch to DUT
					onUpdate?.({ content: [{ type: "text", text: "SDWire → DUT..." }] });
					await sdwireSwitch("dut", signal);
					state.lastFlash = ts;
					steps.push("SDWire → DUT");

					// Small delay for SD card to be detected
					await new Promise(r => setTimeout(r, 500));

					// 4. Reset
					onUpdate?.({ content: [{ type: "text", text: "Resetting TrimUI..." }] });
					const resetResp = await harnessCtrl("reset pulse", ports.ctrl, signal);
					steps.push(`Reset: ${resetResp}`);

					// 5. Capture with streaming updates
					const logContent = await captureUartStreaming(ports.uart, captureSecs, signal, onUpdate);

					const logFile = path.join(SERIAL_DIR, `boot_test_${ts}.log`);
					fs.mkdirSync(SERIAL_DIR, { recursive: true });
					fs.writeFileSync(logFile, logContent);
					state.lastCapture = logFile;
					steps.push(`Captured ${captureSecs}s → ${path.basename(logFile)}`);

					// 6. Analyze
					const analysis = analyzeBootLog(logContent);
					state.lastTestResult = analysis.summary;
					steps.push(`Result: ${analysis.summary}`);

					// 7. Switch back to TS
					await sdwireSwitch("ts", signal);

					const logLines = logContent.split("\n");
					let displayLog = logContent;
					if (logLines.length > 130) {
						displayLog = [
							...logLines.slice(0, 100),
							`\n... (${logLines.length - 120} lines omitted) ...\n`,
							...logLines.slice(-20),
						].join("\n");
					}

					return {
						content: [{ type: "text", text: [
							"TrimUI Boot Test Complete",
							"═════════════════════════",
							...steps.map(s => `  • ${s}`),
							"",
							`Log: ${logFile}`,
							``,
							`Analysis: ${analysis.summary}`,
							...(analysis.customCodeOutput.length > 0 ? [
								"", "Custom code output:",
								...analysis.customCodeOutput.map(s => `  >> ${s}`),
							] : []),
							...(analysis.errors.length > 0 ? [
								"", "Errors:",
								...analysis.errors.slice(0, 10).map(s => `  ⚠ ${s}`),
							] : []),
							"", "── Serial Output ──",
							displayLog || "(no output)",
						].join("\n") }],
						details: { action, steps, analysis, logFile, state: { ...state } },
					};
				}

				// ── ANALYZE ──────────────────────────────────────
				case "analyze": {
					const file = log_file ?? state.lastCapture;
					if (!file) return { content: [{ type: "text", text: "No log file specified and no previous capture." }], isError: true };
					if (!fs.existsSync(file)) return { content: [{ type: "text", text: `Not found: ${file}` }], isError: true };

					const content = fs.readFileSync(file, "utf-8");
					const analysis = analyzeBootLog(content);

					return {
						content: [{ type: "text", text: [
							`Analysis of: ${file}`,
							`Lines: ${content.split("\n").length}`,
							``,
							`Summary: ${analysis.summary}`,
							``,
							`Boot0:        ${analysis.boot0Started ? "✓" : "✗"}`,
							`Boot source:  ${analysis.bootSource}`,
							`DRAM:         ${analysis.dramInit ? "✓" : "✗"}`,
							`boot_package: ${analysis.bootPkgLoaded ? "✓" : "✗"}`,
							`ATF BL31:     ${analysis.atfStarted ? `✓ (spsr=${analysis.atfSpsr ?? "?"})` : "✗"}`,
							`Custom code:  ${analysis.customCodeOutput.length > 0 ? analysis.customCodeOutput.join(", ") : "none"}`,
							`Kernel:       ${analysis.kernelStarted ? "✓" : "✗"}`,
							`Errors:       ${analysis.errors.length || "none"}`,
							...(analysis.errors.length > 0 ? ["", ...analysis.errors.slice(0, 10).map(e => `  ⚠ ${e}`)] : []),
						].join("\n") }],
						details: { action, file, analysis },
					};
				}

				} // switch
			} catch (err: any) {
				return {
					content: [{ type: "text", text: `Harness error: ${err?.message ?? String(err)}` }],
					details: { action, error: true },
					isError: true,
				};
			}

			return { content: [{ type: "text", text: "Unknown action" }], isError: true };
		},

		// ── Rendering ────────────────────────────────────────────

		renderCall(args, theme) {
			const { action, build_script, duration_secs } = args as any;
			let text = theme.fg("toolTitle", theme.bold("trimui "));
			switch (action) {
				case "test":
					text += theme.fg("accent", "▶ TEST");
					if (build_script === "skip") text += theme.fg("dim", " (skip build)");
					if (duration_secs) text += theme.fg("dim", ` ${duration_secs}s`);
					break;
				case "flash": text += theme.fg("warning", "⚡ FLASH"); break;
				case "capture":
					text += theme.fg("accent", "📡 CAPTURE ");
					text += theme.fg("dim", `${duration_secs ?? 30}s`);
					break;
				case "build":
					text += theme.fg("muted", "🔨 BUILD");
					if (build_script) text += " " + theme.fg("dim", path.basename(build_script));
					break;
				case "analyze": text += theme.fg("muted", "🔍 ANALYZE"); break;
				case "status": text += theme.fg("muted", "ℹ STATUS"); break;
				default: text += theme.fg("dim", action);
			}
			return new Text(text, 0, 0);
		},

		renderResult(result, { expanded, isPartial }, theme) {
			if (isPartial) {
				const details = result.details as any;
				// Streaming UART capture — show live serial output
				if (details?.streaming) {
					const elapsed = details.elapsed ?? 0;
					const duration = details.duration ?? 0;
					const bytes = details.bytes ?? 0;
					const lines = details.lines ?? 0;
					const pct = duration > 0 ? Math.round((elapsed / duration) * 100) : 0;
					// Progress bar
					const barLen = 20;
					const filled = Math.round(barLen * pct / 100);
					const bar = "█".repeat(filled) + "░".repeat(barLen - filled);

					let header = theme.fg("warning",
						`📡 ${bar} ${elapsed}s/${duration}s  ${lines} lines  ${bytes} bytes`
					);

					// Show last lines of serial output
					const tailLines = details.tailLines ?? "";
					if (tailLines) {
						const serialLines = tailLines.split("\n").slice(-15);
						const serialText = serialLines.map((l: string) =>
							theme.fg("dim", stripTermEscapes(l))
						).join("\n");
						header += "\n" + serialText;
					} else {
						header += "\n" + theme.fg("dim", "(waiting for serial data...)");
					}
					return new TruncatedText(header, 0, 0);
				}
				// Non-streaming partial update
				const txt = result.content?.[0]?.type === "text" ? (result.content[0] as any).text : "Working...";
				return new Text(theme.fg("warning", `⏳ ${txt}`), 0, 0);
			}
			const details = result.details as any;
			if (details?.error || result.isError) {
				const msg = result.content?.[0]?.type === "text" ? (result.content[0] as any).text : "Error";
				return new Text(theme.fg("error", `✗ ${msg}`), 0, 0);
			}
			const analysis = details?.analysis as BootAnalysis | undefined;
			switch (details?.action) {
				case "test":
				case "capture": {
					let text = analysis?.hasOutput
						? theme.fg("success", "✓ Complete")
						: theme.fg("warning", "⚠ No output");
					if (analysis?.summary) text += "\n  " + theme.fg("dim", analysis.summary);
					if (expanded) {
						const full = result.content?.[0]?.type === "text" ? (result.content[0] as any).text : "";
						if (full) {
							// Strip raw terminal escapes from serial output before rendering
							const sanitized = full.split("\n").map(stripTermEscapes).join("\n");
							text += "\n\n" + theme.fg("dim", sanitized);
						}
					}
					return new TruncatedText(text, 0, 0);
				}
				case "flash": {
					let text = theme.fg("success", "✓ Flash complete");
					if (expanded) {
						for (const s of (details?.steps ?? [])) text += "\n  " + theme.fg("dim", s);
					}
					return new Text(text, 0, 0);
				}
				default: {
					const fb = result.content?.[0]?.type === "text" ? (result.content[0] as any).text : "Done";
					const lines = fb.split("\n");
					const summary = lines.length > 5 && !expanded
						? lines.slice(0, 5).join("\n") + theme.fg("dim", `\n... (${lines.length - 5} more)`)
						: fb;
					return new Text(theme.fg("success", "✓ ") + summary, 0, 0);
				}
			}
		},
	});

	// ── Session state ────────────────────────────────────────────

	pi.on("session_start", async (_event, ctx) => {
		for (const entry of ctx.sessionManager.getBranch()) {
			if (entry.type === "message" && entry.message.role === "toolResult" && entry.message.toolName === "trimui_harness") {
				const d = entry.message.details as any;
				if (d?.state) Object.assign(state, d.state);
			}
		}
		const ports = findHarnessPorts();
		ctx.ui.setStatus("trimui", ports ? "🎮 TrimUI Harness" : "🎮 TrimUI (no harness)");
	});

	pi.on("tool_execution_end", async (event, ctx) => {
		if (event.toolName === "trimui_harness") {
			const sd = state.sdwireMode === "dut" ? "DUT" : state.sdwireMode === "ts" ? "TS" : "?";
			ctx.ui.setStatus("trimui", `🎮 SD:${sd}${state.lastTestResult ? ` | ${state.lastTestResult.slice(0, 40)}` : ""}`);
		}
	});
}
