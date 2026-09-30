from __future__ import annotations

import argparse

from easychange.ui.main_window import run


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--machine", action="store_true", help="Start in high-contrast Machine Mode")
    parser.add_argument("--hid", action="store_true", help="Start with compact, OCR-friendly HID results")
    args = parser.parse_args()
    return run(args.workspace, machine_mode=True if args.machine else None, hid_mode=args.hid)


if __name__ == "__main__": raise SystemExit(main())
