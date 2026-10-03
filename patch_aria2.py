#!/usr/bin/env python3
"""Idempotent one-line patch: raise connections from 16 to 512; native speed logic."""
from pathlib import Path
import sys


def patch(root):
    path = Path(root) / 'src/OptionHandlerFactory.cc'
    before = 'TEXT_MAX_CONNECTION_PER_SERVER,\n                                              "1", 1, 16, \'x\'))'
    after = before.replace('1, 16,', '1, 512,')
    text = path.read_text()
    if after in text:
        return
    if text.count(before) != 1:
        raise SystemExit(f'Refusing unexpected upstream source: {path}')
    path.write_text(text.replace(before, after, 1))


if __name__ == '__main__':
    patch(sys.argv[1])
