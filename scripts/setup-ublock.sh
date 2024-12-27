#! /usr/bin/env bash

# ref: https://github.com/sissbruecker/linkding/blob/master/scripts/setup-ublock.sh
UBO_DIR="$PLAYWRIGHT_BROWSERS_PATH/uBOLite.chromium.mv3"
rm -rf "$UBO_DIR"

# Download uBlock Origin Lite
TAG=$(curl -sL https://api.github.com/repos/uBlockOrigin/uBOL-home/releases/latest | jq -r '.tag_name')
DOWNLOAD_URL=https://github.com/uBlockOrigin/uBOL-home/releases/download/$TAG/$TAG.chromium.mv3.zip
echo "Downloading $DOWNLOAD_URL"
curl -L -o uBOLite.zip "$DOWNLOAD_URL"
unzip uBOLite.zip -d "$UBO_DIR"
rm -f uBOLite.zip

# Patch uBlock Origin Lite to respect rulesets enabled in manifest.json
  # use "sed -i '' ..." on normal mac
sed -i "s/const out = \[ 'default' \];/const out = await dnr.getEnabledRulesets();/" "$UBO_DIR/js/ruleset-manager.js"

# Enable annoyances rulesets in manifest.json
jq '.declarative_net_request.rule_resources |= map(if .id == "annoyances-overlays" or .id == "annoyances-cookies" or .id == "annoyances-social" or .id == "annoyances-widgets" or .id == "annoyances-others" then .enabled = true else . end)' "$UBO_DIR/manifest.json" > temp.json
mv temp.json "$UBO_DIR/manifest.json"

# mkdir -p "./chromium-profile" # created in setup-playwright
