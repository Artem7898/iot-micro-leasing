#!/usr/bin/env python
"""Compatibility entry point for the single supported Django back office."""

import os
import sys
from pathlib import Path


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "admin_panel"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "admin_panel.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
