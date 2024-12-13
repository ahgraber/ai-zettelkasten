#!/usr/bin/env bash

npx /Users/mithras/_code/ai-zk/node_modules/.bin/single-file "https://sqlmodel.tiangolo.com/tutorial/insert/#create-a-session" \
--debug-messages-file=./debug.txt \
--errors-file=./errors.txt \
--error-traces-disabled=false \
--browser-arg="--headless=new" \
--browser-executable-path="/Applications/Chromium.app/Contents/MacOS/Chromium" \
--dump-content



# --browser-executable-path="/Applications/Chromium.app/Contents/MacOS/Chromium" \
# --browser-arg=--"user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36" \
# --browser-arg="--user-data-dir=./chromium-profile" \
# --browser-arg="--load-extension=uBOLite.chromium.mv3" \
# --browser-arg="--window-size=1440,2000" \
