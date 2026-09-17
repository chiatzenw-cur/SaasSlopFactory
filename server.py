#!/usr/bin/env python
"""Entrypoint.  python server.py  ->  http://127.0.0.1:7310

Keys are read from, in order: the real environment, $DESCLES_RUNTIME_ENV,
or <repo>/.keys.env.  Values are never printed.
"""
import os
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from descles import config, console  # noqa: E402

loaded = config.load_keys()
if loaded:
    print("keys loaded:", ", ".join(loaded))

console.serve()
