#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Max-Token-Saver statusline: shows plugin active status."""
import sys
import os

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[32m"
GREEN_BOLD = "\033[1;32m"


def main():
    sys.stdin.read()
    print(f"{GREEN_BOLD}⚡MTS{RESET} {GREEN}Active{RESET}")


if __name__ == "__main__":
    main()
