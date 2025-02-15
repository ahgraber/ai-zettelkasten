# AI-Zettelkasten CLI

Most settings are configured in `.aizk.env`

## Scrape

```sh
usage: scrape.py [-h] [-e ENV] [-l LAST] [-v]

Scrape URLs.

options:
  -h, --help            show this help message and exit
  -e ENV, --env ENV     Path to a .env file.
  -l LAST, --last LAST  Consider files changed in last n days
  -v, --verbose
```

```sh
# use files changed in past 7 days
uv run -m aizk.cli.scrape -l 7
```

## Playwright-managed Chromium

Chrome and SingleFile extractors rely on Playwright-managed Chromium.

1. Install with:

   ```sh
   playwright install chrome chromium --with-deps
   ```

2. Find path to installed app with:

   ```sh
   playwright install chromium --dry-run | grep "Install location" | xargs | cut -d' ' -f3
   ```

3. Launch Playwright-managed `chromium` and navigate to <https://arxiv.org>, etc. Complete captchas.

   ```sh
   uv run -m aizk.cli.init_chromium
   ```
