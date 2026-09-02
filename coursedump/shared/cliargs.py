"""ArgumentParser whose usage errors follow the CLI exit-code contract.

Stock argparse exits with 2 for invalid arguments. This subclass changes only
that code to 1 while preserving usage and stderr output. Subparsers inherit the
class automatically.
"""
from __future__ import annotations

import argparse
import sys


class ArgumentParser(argparse.ArgumentParser):
    """Behave like argparse.ArgumentParser but exit 1 for usage errors."""

    def error(self, message: str):
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")
