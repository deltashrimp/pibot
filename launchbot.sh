#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1

if [ ! -f .venv/bin/python ]; then
    echo "Virtual environment not found. Run:"
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

exec ./.venv/bin/python source/pibot.py
