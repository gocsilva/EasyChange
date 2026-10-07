from __future__ import annotations

import argparse
from collections.abc import Sequence

from easychange.ui.main_window import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", nargs="?", default=".")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--machine",
        action="store_true",
        help="Start in high-contrast Machine Mode",
    )
    mode.add_argument(
        "--human",
        action="store_true",
        help="Force normal human GUI mode, ignoring persisted Machine Mode state",
    )
    parser.add_argument(
        "--hid",
        action="store_true",
        help="Start with compact, OCR-friendly HID results",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.human and args.hid:
        raise SystemExit("--human cannot be combined with --hid")
    machine_mode: bool | None
    if args.machine:
        machine_mode = True
    elif args.human:
        machine_mode = False
    else:
        machine_mode = None
    return run(args.workspace, machine_mode=machine_mode, hid_mode=args.hid)


if __name__ == "__main__":
    raise SystemExit(main())
