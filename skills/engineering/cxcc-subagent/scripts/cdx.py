#!/usr/bin/env python3
"""Fast entry point for the cdx agent supervisor."""
import sys
from cdx_version import VERSION

if __name__ == "__main__":
    if sys.argv[1:] in (["--version"], ["-v"], ["-V"]):
        print(VERSION)
    else:
        from cdx_core import main
        sys.exit(main())
