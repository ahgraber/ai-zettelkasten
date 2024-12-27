#! /usr/bin/env bash

uv add playwright

PLAYWRIGHT_BROWSERS_PATH="$PWD/bin/pw-browsers"
echo "PLAYWRIGHT_BROWSERS_PATH=\"$PLAYWRIGHT_BROWSERS_PATH\"" >> .env

PLAYWRIGHT_BROWSERS_PATH=$PLAYWRIGHT_BROWSERS_PATH python -m playwright install --with-deps chromium

CHROME_USER_DATA="$PWD/chromium-profile"
mkdir -p "$CHROME_USER_DATA"
echo "CHROME_USER_DATA=\"$CHROME_USER_DATA\"" >> .env

echo "Launching Chrome (headfull) for the first time"
eval "$(playwright install chromium --dry-run | grep "Install location" | xargs | cut -d' ' -f3)/chrome-mac/Chromium.app/Contents/MacOS/Chromium --user-data-dir=$CHROME_USER_DATA"
