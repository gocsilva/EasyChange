from __future__ import annotations

import argparse

from easychange.ui.main_window import run


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("workspace", nargs="?", default=".")
    args = parser.parse_args()
    return run(args.workspace)


if __name__ == "__main__": raise SystemExit(main())
