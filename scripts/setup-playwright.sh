#! /usr/bin/env bash

uv add playwright


echo "PLAYWRIGHT_BROWSERS_PATH=\"$PWD/bin/pw-browsers\"" >> .env
python -m playwright install --with-deps chromium

echo "CHROME_USER_DATA=\"$PWD/chromium-profile\"" >> .env
mkdir -p "$CHROME_USER_DATA"
