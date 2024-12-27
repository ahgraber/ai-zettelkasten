#!/usr/bin/env bash

# BROWSER_BIN="/Applications/Chromium.app/Contents/MacOS/Chromium"
# BROWSER_BIN="$PLAYWRIGHT_BROWSERS_PATH/chromium_headless_shell-1148/chrome-mac/headless_shell"
BROWSER_BIN="$PLAYWRIGHT_BROWSERS_PATH/chromium-1148/chrome-mac/Chromium.app/Contents/MacOS/Chromium"

# ref: https://github.com/sissbruecker/linkding/blob/master/siteroot/settings/base.pya
URL="https://sqlmodel.tiangolo.com/tutorial/insert/#create-a-session"

npx single-file "$URL" \
--browser-executable-path="$BROWSER_BIN" \
--browser-arg="--headless=new" \
--browser-arg="--user-data-dir=$CHROME_USER_DATA" \
--browser-arg="--load-extension=uBOLite.chromium.mv3" \
--debug-messages-file=./debug.txt \
--errors-file=./errors.txt \
--error-traces-disabled=false \
--dump-content



# --browser-executable-path="/Applications/Chromium.app/Contents/MacOS/Chromium" \
# --browser-arg=--"user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36" \
# --browser-arg="--user-data-dir=./chromium-profile" \
# --browser-arg="--load-extension=uBOLite.chromium.mv3" \
# --browser-arg="--window-size=1440,2000" \
# --browser-arg="--no-sandbox" \
