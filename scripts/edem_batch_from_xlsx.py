#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Backward-compatible wrapper for edem_batch.py.

Keeps the legacy `edem_batch_from_xlsx.py` CLI entry point working seamlessly while
delegating all parsing, cloning, and queue generation to the generic `edem_batch.py`.
"""
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
import edem_batch  # noqa: E402


if __name__ == '__main__':
    edem_batch.main()
