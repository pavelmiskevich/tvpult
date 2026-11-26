#!/bin/sh
# The built wheel ships everything the remote needs at run time, and installs and runs in a clean environment
# (not the source tree): catches a missing data file or dependency. Used by CI and by the release workflow.
# Run after `uv build`, from the repository root.
set -eu

wheel=$(ls dist/*.whl)
for file in tvpult/static/index.html tvpult/devices.example.toml tvpult/locales/en.json tvpult/locales/ru.json \
    tvpult/apps.json; do
    unzip -l "$wheel" | grep -q "$file" || { echo "::error::$file is missing from $wheel"; exit 1; }
done

venv=$(mktemp -d)/wheel-check
uv venv --quiet "$venv"
uv pip install --quiet --python "$venv" "$wheel"
cd "$(dirname "$venv")"
"$venv/bin/tvpult" --version
"$venv/bin/tvpult" catalog > /dev/null
"$venv/bin/tvpult" keys > /dev/null
