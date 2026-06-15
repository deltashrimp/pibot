#!/bin/bash

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1

echo "" > logs/logs.log
echo "[]" > logs/telemetry.json
