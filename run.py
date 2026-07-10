"""Entry point for the capture service.

Usage:
    python run.py                      # use defaults / config.toml if present
    python run.py --config config.toml
    python run.py --no-ocr             # skip OCR (just store screenshots)
    python run.py --db mydata.db
"""
from __future__ import annotations

import argparse
import os

from livingpc.config import load
from livingpc.service import run


def main() -> None:
    parser = argparse.ArgumentParser(description="livingpc capture service")
    parser.add_argument("--config", default="config.toml", help="path to TOML config")
    parser.add_argument("--db", help="override db_path")
    parser.add_argument("--blob-dir", help="override blob_dir")
    parser.add_argument("--no-ocr", action="store_true", help="disable OCR")
    args = parser.parse_args()

    config = load(args.config if os.path.exists(args.config) else None)
    if args.db:
        config.db_path = args.db
    if args.blob_dir:
        config.blob_dir = args.blob_dir
    if args.no_ocr:
        config.ocr_enabled = False

    run(config)


if __name__ == "__main__":
    main()
