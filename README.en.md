# usb-link-check

A cross-platform CLI tool that determines, as far as is possible, the capability of each element of a USB connection — port, cable and device — and identifies the bottleneck.

[日本語](README.md)

## Read this first: cable capability is inferred, not measured

**A USB cable's capability cannot be measured.** USB cables carry no identifying information (apart from the USB-C eMarker), and even the eMarker belongs to the USB Power Delivery controller, where the OS USB stack cannot read it.

So this tool **infers** cable capability from a physical fact:

```
L = min(P, C, D)

L : the negotiated link speed   … measurable
P : the port's (and hub's) capability … measurable
D : the device's capability     … measurable on Windows
C : the cable's capability      … NOT measurable → inferred from L, P and D
```

Because of this, the tool always separates what it can state with certainty from what it cannot:

- `10 Gbps` — confirmed at that speed
- `5 Gbps 以上（上限不明）` — only known to be at least that fast; upper bound unknown
- `不明` — unknown

It never fills a gap with a plausible-looking number. When it says unknown, the value really is unknown.

## Status

- **Windows: implemented**
- **macOS: not implemented** — waiting for dumps from real hardware, because guessing the output format is not allowed in this project
- Linux: unsupported (exits with an error)

## Install

Requires Python 3.10 or later.

```sh
pip install -e .
```

## Usage

```sh
usb-link-check --list                    # list the USB devices that were found
usb-link-check --list --all              # also list every port, including empty ones
usb-link-check                           # diagnose the USB mass-storage device
usb-link-check --device 0781:5591        # select by VID:PID
usb-link-check --device "Extreme SSD"    # select by substring of the device name
usb-link-check --json                    # emit JSON
usb-link-check --debug                   # write the raw data to stderr
```

If `--device` is omitted and the candidates do not narrow down to one, the tool prints the list and stops **rather than choosing for you** — an ambiguous automatic choice is a source of wrong diagnoses.

### Exit codes

| Code | Meaning |
|---:|---|
| 0 | `OPTIMAL` — running at the fastest speed this configuration allows |
| 1 | `IMPROVABLE` — there is a bottleneck |
| 2 | `NOT_DETECTED` — the target device is not in the USB tree |
| 3 | `UNDETERMINED` — not enough information, or an out-of-scope configuration |
| 4 | Execution error (unsupported OS, insufficient privileges, command failure) |

## How it works

On Windows the tool calls the same path that Microsoft's USBView uses (`SetupDiGetClassDevs` plus the USB IOCTLs) directly through `ctypes`. No external packages, and no administrator privileges.

Two findings from real hardware matter most here; missing either of them produces a wrong diagnosis:

1. **`EX.Speed` does not represent SuperSpeed.** A device running at 5 Gbps still reports `UsbHighSpeed`, so L is determined from `V2.Flags` first.
2. **Port numbers do not map one-to-one onto physical connectors.** A USB3 connector appears as two logical ports, one USB2 and one USB3. P is therefore the union of the protocols supported by the port and by its companion port. Ignore this and a cable bottleneck (A1) is mistaken for a port bottleneck (A2).

See [SPEC.md](SPEC.md) (Japanese) for the full specification.

### The device list

`--list` shows L (the effective link speed), P (port capability) and D (device capability) for each device, and marks each row with one of three states, so that a row the tool cannot judge is never read as "fine":

- `⚠` — confirmed to be linking below its capability (`L < min(P, D)`)
- `?` — undeterminable: P or D has an unknown upper bound, so the tool cannot tell whether the device is at its ceiling or has fallen back
- (unmarked) — confirmed to run at the fastest speed this configuration allows (`L = min(P, D)`)

A trailing `+` means "at least, upper bound unknown". `--all` additionally lists every port, so you can see which physical socket is USB3.

### eMarker estimation

`PortConnectorIsTypeC` tells whether a port is Type-C or Type-A, which lets the tool **estimate** whether the cable carries an eMarker (the USB Type-C specification mandates one in, for example, SuperSpeed-capable C-to-C cables).

| Port | Link speed | eMarker | Certainty |
|---|---|---|---|
| Type-A | any | absent | likely |
| Type-C | 5 Gbps or faster | likely present | likely |
| Type-C | 480 Mbps or slower | unknown | unknown |

This is never stated as fact: a Type-C port may still be paired with an adapter cable. Reading an eMarker directly requires the USB Power Delivery physical layer, which the PC's USB stack does not expose (Windows UCSI is a Phase 2 investigation item).

### Known limitation

The presence of a companion port does not guarantee that SuperSpeed wiring actually reaches that physical connector — a front-panel USB3 header may simply be unplugged. P is then overestimated, and a directly-attached device is reported as cable-limited. The tool cannot tell whether a cable is in use, so its advice names both possibilities.

## Out of scope

Phase 1 does not implement:

- Throughput measurement (MB/s)
- Thunderbolt / USB4 devices
- Cable profiles
- Plug/unplug monitoring

## Development

```sh
pip install -e ".[dev]"
pytest          # the whole suite passes with no USB devices attached
ruff check .
ruff format --check .
```

Tests run against **dumps captured from real hardware** in `tests/fixtures/raw/`; invented dumps are not allowed. Capture new dumps with:

```sh
python tools/collect_dump.py --list                                  # find the VID:PID
python tools/collect_dump.py --label windows_fast --device 0781:5591 # capture
python tools/sanitize_dump.py                                        # replace serial numbers with REDACTED
```

Always run `tools/sanitize_dump.py` before committing a dump (`collect_dump.py` runs it automatically after capturing).

## License

MIT
