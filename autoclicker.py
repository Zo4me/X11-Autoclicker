#!/usr/bin/env python3

import argparse
import random
import sys
import threading
import time

from evdev import InputDevice, UInput, ecodes, list_devices

KNOWN_BUTTON_NAMES = (
    "BTN_LEFT", "BTN_RIGHT", "BTN_MIDDLE",
    "BTN_SIDE", "BTN_EXTRA", "BTN_FORWARD", "BTN_BACK", "BTN_TASK",
)
_KNOWN_BUTTON_CODES = {
    getattr(ecodes, name): name for name in KNOWN_BUTTON_NAMES if hasattr(ecodes, name)
}


def resolve_ecode(name: str) -> int:
    if not (name.startswith("BTN_") or name.startswith("KEY_")):
        raise SystemExit(
            f"'{name}' doesn't look like a button/key name "
            f"(expected something starting with BTN_ or KEY_, e.g. BTN_EXTRA)."
        )
    try:
        return getattr(ecodes, name)
    except AttributeError:
        raise SystemExit(
            f"Unknown button/key name: '{name}'. Names are case-sensitive "
            f"(e.g. BTN_EXTRA, not btn_extra)."
        )


def open_all_devices():
    # Your device may fail here. Please explicitly write in "help" if it does. *rare*
    devices = []
    for path in list_devices():
        try:
            devices.append(InputDevice(path))
        except OSError:
            continue
    return devices


def describe_device(dev: InputDevice) -> str:
    caps = dev.capabilities().get(ecodes.EV_KEY, [])
    named = [_KNOWN_BUTTON_CODES[c] for c in caps if c in _KNOWN_BUTTON_CODES]
    cap_str = ", ".join(named) if named else "no recognized BTN_* codes"
    return f"{dev.path}\t{dev.name}\t[{cap_str}]"


def list_devices_and_exit():
    devices = open_all_devices()
    if not devices:
        print("No input devices found. Check permissions on /dev/input/* "
              "(you usually need to be in the 'input' group).")
        sys.exit(1)
    print(f"Found {len(devices)} input device(s):\n")
    for dev in devices:
        print(describe_device(dev))
        dev.close()
    sys.exit(0)


def find_mouse_device(device_name, trigger_name, trigger_code, click_name, click_code):
    candidates = []
    for dev in open_all_devices():
        if device_name and dev.name != device_name:
            dev.close()
            continue

        caps = dev.capabilities().get(ecodes.EV_KEY, [])
        if trigger_code in caps and click_code in caps:
            candidates.append(dev)
        else:
            dev.close()

    if not candidates:
        hint = f" named '{device_name}'" if device_name else ""
        raise RuntimeError(
            f"No device{hint} with both required buttons "
            f"({trigger_name}, {click_name}) was found.\n"
            f"Run with --list-devices to see what's connected, and confirm "
            f"your user can read /dev/input/* (usually the 'input' group)."
        )

    if len(candidates) == 1:
        chosen = candidates[0]
    else:
        print("Multiple candidate devices found:\n")
        for i, dev in enumerate(candidates):
            print(f"  [{i}] {describe_device(dev)}")
        chosen_idx = None
        while chosen_idx is None:
            choice = input(f"\nSelect device [0-{len(candidates) - 1}]: ").strip()
            if choice.isdigit() and 0 <= int(choice) < len(candidates):
                chosen_idx = int(choice)
            else:
                print("Invalid selection, try again.")
        chosen = candidates[chosen_idx]
        for i, dev in enumerate(candidates):
            if i != chosen_idx:
                dev.close()

    print(f"Using device: {chosen.path} ({chosen.name})")
    return chosen


class AutoClicker:
    def __init__(self, ui: UInput, click_button: int, interval_min: float, interval_max: float):
        self.ui = ui
        self.click_button = click_button
        self.interval_min = interval_min
        self.interval_max = interval_max
        self._clicking = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._clicking.set()
        self._thread = threading.Thread(target=self._click_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._clicking.clear()
        if self._thread:
            self._thread.join()
            self._thread = None

    def _click_loop(self):
        while self._clicking.is_set():
            self._click()
            interval = random.uniform(self.interval_min, self.interval_max)
            end_time = time.monotonic() + interval
            while self._clicking.is_set() and time.monotonic() < end_time:
                time.sleep(0.001)

    def _click(self):
        self.ui.write(ecodes.EV_KEY, self.click_button, 1)
        self.ui.syn()
        time.sleep(0.005)
        self.ui.write(ecodes.EV_KEY, self.click_button, 0)
        self.ui.syn()


def parse_args():
    p = argparse.ArgumentParser(
        description="Configurable evdev/uinput autoclicker (X11, Linux).",
        epilog="Example: %(prog)s --cps 10 --jitter 2 --trigger BTN_SIDE",
    )
    p.add_argument("--cps", type=float, default=10.0,
                    help="Target clicks per second (default: 10.0)")
    p.add_argument("--jitter", type=float, default=3.0,
                    help="Jitter around the interval, in milliseconds, applied "
                         "as +/- this amount (default: 3.0)")
    p.add_argument("--device", type=str, default=None,
                    help="Exact device name to match. If flag is prompted, "
                         "you must add the exact device name. ")
    p.add_argument("--trigger", type=str, default="BTN_EXTRA",
                    help="Button that arms/disarms clicking (default: BTN_EXTRA)")
    p.add_argument("--button", type=str, default="BTN_LEFT",
                    help="Button that gets synthetically clicked (default: BTN_LEFT)")
    p.add_argument("--list-devices", action="store_true",
                    help="List connected input devices and their recognized "
                         "button capabilities, then exit")
    return p.parse_args()


def main():
    args = parse_args()

    if args.list_devices:
        list_devices_and_exit()

    if args.cps <= 0:
        raise SystemExit("--cps stands for 'clicks per second', it must have a higher value than 0. Decimal is accepted. ")

    trigger_code = resolve_ecode(args.trigger)
    click_code = resolve_ecode(args.button)

    base_interval = 1.0 / args.cps
    jitter_seconds = args.jitter / 1000.0
    interval_min = base_interval - jitter_seconds
    interval_max = base_interval + jitter_seconds

    if interval_min <= 0:
        raise SystemExit(
            f"--jitter ({args.jitter}ms) is too large for --cps ({args.cps}); "
            f"You would somehow manage to produce a 'negative' jitter. Lower the flag "
            f"or lower the CPS."
        )

    try:
        mouse = find_mouse_device(args.device, args.trigger, trigger_code, args.button, click_code)
    except RuntimeError as e:
        raise SystemExit(str(e))

    ui = UInput(
        {
            ecodes.EV_KEY: [click_code],
            ecodes.EV_REL: [ecodes.REL_X, ecodes.REL_Y],
        },
        name="autoclicker-virtual-mouse",
    )

    clicker = AutoClicker(ui, click_code, interval_min, interval_max)

    print(f"Target: {args.cps} CPS (interval {interval_min * 1000:.1f}-{interval_max * 1000:.1f}ms)")
    print(f"Listening on: {mouse.path} ({mouse.name})")
    print(f"Hold {args.trigger} to auto-click {args.button}. Ctrl+C to exit.")

    try:
        for event in mouse.read_loop():
            if event.type == ecodes.EV_KEY and event.code == trigger_code:
                if event.value == 1:
                    clicker.start()
                elif event.value == 0:
                    clicker.stop()
    except KeyboardInterrupt:
        print("\nExiting.")
    except OSError as e:
        print(f"\nDevice error, possibly disconnected: {e}")
    finally:
        clicker.stop()
        ui.close()
        mouse.close()


if __name__ == "__main__":
    main()
