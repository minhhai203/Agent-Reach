"""Cho phép chạy `python -m video_tracker`."""

import sys

from video_tracker.cli import main

if __name__ == "__main__":
    sys.exit(main())
