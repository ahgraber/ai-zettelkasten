"""Playwright/Chrome extractor and stealthing JavaScript snippets.

Ref:

- https://github.com/berstend/puppeteer-extra/tree/master/packages/puppeteer-extra-plugin-stealth/
- https://github.com/paulirish/headless-cat-n-mouse?tab=readme-ov-file
- https://github.com/dgtlmoon/pyppeteerstealth/tree/master

- https://arh.antoinevastel.com/bots/areyouheadless
- https://bot.sannysoft.com/
"""

import asyncio
import datetime
import logging
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, List, Tuple, override

from playwright.async_api import Playwright, async_playwright
from playwright_stealth import Stealth  # https://github.com/Mattwmaster58/playwright_stealth
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aizk.datamodel.schema import ScrapeStatus, Source, ValidatedURL
from aizk.extractors.base import ExtractionError, Extractor
from aizk.extractors.utils import get_write_mode
from aizk.utilities.file_helpers import AtomicWriter
from aizk.utilities.path_helpers import (
    path_is_dir,
    path_is_executable,
)

logger = logging.getLogger(__file__)

CHROME_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/118.0.0.0 Safari/537.36"  # "Chrome/131.0.0.0 Safari/537.36"
)


def detect_playwright_chromium() -> Path | None:
    """Find Chromium installed by playwright if exists."""
    result = subprocess.run(  # NOQA: S603
        [f"{Path(sys.executable).parent}/playwright", "install", "chromium", "--dry-run"],
        capture_output=True,
        text=True,
    )

    try:
        result.check_returncode()  # raises error if failed
    except subprocess.CalledProcessError:
        logging.exception("Error running 'playwright' command: ")

    browser_path = result.stdout.splitlines()[1].replace("Install location:", "").strip()
    browser_path = path_is_dir(browser_path)

    if sys.platform == "darwin":
        for app in browser_path.rglob("*.app/Contents/MacOS/Chromium"):
            if browser := path_is_executable(app):
                return browser
    # TODO Linux


class PlaywrightSettings(BaseSettings):
    """Default Playwright / Chromium settings."""

    model_config = SettingsConfigDict(extra="ignore")

    # Chrome Binary - not really used except for ensuring it exists
    binary: str = Field(default=str(detect_playwright_chromium()))
    timeout: int = Field(default=90, ge=15, lt=3600)  # global process timeout - 10

    chrome_user_agent: str = Field(default=CHROME_USER_AGENT)

    chrome_profile_dir: Path | None = Field(
        default=Path(os.environ.get("CHROME_USER_DATA") or Path.cwd() / ".profile" / "chrome")
    )
    chrome_profile_name: str = Field(default="Default")

    # Chrome Options Tuning
    headless: bool = Field(default=True)
    sandbox: bool = Field(default=True)  # false if in docker
    resolution: tuple[int, int] = Field(default=(1440, 2000))
    pageload_timeout: int = Field(default=10, ge=5, lt=3600)  # wait for page to finish loading

    save_dom: bool = Field(default=True)
    save_pdf: bool = Field(default=True)
    save_png: bool = Field(default=True)

    @model_validator(mode="after")
    def validate(self):
        """Validate settings."""
        if self.timeout <= self.pageload_timeout:
            logger.error(
                f"Global timeout ({self.timeout}) must be longer than pageload timeout ({self.pageload_timeout})"
            )
        if self.pageload_timeout < 5:
            logger.error(
                f"Warning: pageload_timeout is set too low! "
                f"(currently set to pageload_timeout={self.pageload_timeout} seconds).\n"
                "Chrome will fail to fully load the site if set to < 5 second."
            )
        # if user has specified a user data dir, make sure its valid
        if self.chrome_profile_dir:
            path_is_dir(self.chrome_profile_dir)

            # warn if nesting chrome_profile_dir and chrome_profile_name
            # do not want "path/to/profile/dir/Default/Default"
            if str(self.chrome_profile_dir).endswith("/Default"):
                logger.error(
                    "Try removing '/Default' from the end e.g. chrome_profile_dir='{}'".format(
                        str(self.chrome_profile_dir).removesuffix("/Default")
                    )
                )
            path_is_dir(self.chrome_profile_dir / self.chrome_profile_name)

        if not any([self.save_dom, self.save_pdf, self.save_png]):
            logger.error("At least one save method must be true: ['save_dom', 'save_pdf', 'save_png']")

        return self


class PlaywrightExtractor(Extractor):
    """Playwright / Chromium extractor."""

    name: str = "playwright"
    # default_filename: str = "playwright"
    config: PlaywrightSettings

    def __init__(
        self,
        config: PlaywrightSettings | dict[str, Any] | None = None,
        data_dir: Path | str | None = None,
        ensure_data_dir: bool = False,
    ):
        config = self.validate_config(config or {})
        binary = config.binary or detect_playwright_chromium()

        super().__init__(
            config=config,
            binary=binary,
            data_dir=data_dir,
            ensure_data_dir=ensure_data_dir,
        )

        self.cleanup()

    @override
    def validate_config(self, cfg: PlaywrightSettings | dict[str, Any]) -> PlaywrightSettings:
        """Validate the extractor config."""
        return PlaywrightSettings.model_validate(cfg)

    @override
    def cleanup(self):
        """Clean up any state or runtime files that Chrome leaves behind when killed by a timeout or other error."""
        try:
            linux_lock_file = Path("~/.config/chromium/SingletonLock").expanduser()
            linux_lock_file.unlink(missing_ok=True)
        except Exception as e:
            logger.debug(f"Ignoring exception {e}")
            pass

        if self.config.chrome_profile_dir:
            try:
                (self.config.chrome_profile_dir / "SingletonLock").unlink(missing_ok=True)
            except Exception as e:
                logger.debug(f"Ignoring exception {e}")
                pass

    @override
    async def run(self, url: ValidatedURL | str, out_dir: Path):
        # TODO: stealth does not yet support launchPersistentContext
        async with Stealth().use_async(async_playwright()) as pw:
            browser = await pw.chromium.launch(
                channel="chromium",  # use "new" headless mode to enable headless extensions
                headless=self.config.headless,
            )
            context = await browser.new_context(
                # emulate high-DPI
                viewport={"width": self.config.resolution[0], "height": self.config.resolution[1]},
                device_scale_factor=2,
            )
            page = await context.new_page()

            # wait until extensions and init scripts are initialized
            await page.wait_for_timeout(1000)

            # load page and wait until network activity stops(or time limit reached)
            await page.goto(url)
            await page.wait_for_load_state("networkidle", timeout=self.config.pageload_timeout * 1000)

            # Get the final DOM content
            if self.config.save_dom:
                html_content = await page.content()

                async with AtomicWriter(
                    out_dir / "playwright.html", binary_mode=get_write_mode(html_content) == "wb"
                ) as f:
                    await f.write(html_content)

            # Save PDF - returns bytes if no path provided
            if self.config.save_pdf:
                pdf_content = await page.pdf(
                    # path="./playwright.pdf",
                    format="Letter",
                    display_header_footer=False,
                )

                async with AtomicWriter(
                    out_dir / "playwright.pdf", binary_mode=get_write_mode(pdf_content) == "wb"
                ) as f:
                    await f.write(pdf_content)

            # Save screenshot - returns bytes if no path provided
            if self.config.save_png:
                await page.emulate_media(media="screen", color_scheme="light")
                await page.reload(wait_until="load", timeout=self.config.pageload_timeout * 1000)
                img_content = await page.screenshot(
                    # path="./playwright.png",
                    full_page=True,
                    scale="device",
                )

                async with AtomicWriter(
                    out_dir / "playwright.png", binary_mode=get_write_mode(img_content) == "wb"
                ) as f:
                    await f.write(img_content)

            await context.close()

    @override
    async def __call__(self, source: Source) -> Source:
        """Execute extraction pipeline."""
        src = source.model_copy()

        out_dir_uuid = self.data_dir / str(src.uuid)
        out_dir_uuid.mkdir(exist_ok=True)

        src.scraped_at = datetime.datetime.now(datetime.timezone.utc)

        try:
            logger.debug(f"Extracting from {src.url} with {self.__class__}")
            await self.run(src.url, out_dir_uuid)

        except Exception as e:
            src.scrape_status = ScrapeStatus.ERROR
            src.error_message = str(e)

            with (out_dir_uuid / "errors.txt").open("a") as f:
                lines = [
                    str(src.scraped_at),
                    f"Failed to extract url {src.url}",
                    f"Error: {str(e)}",
                ]
                f.writelines(line + os.linesep for line in lines)

            self.cleanup()
            return src

        try:
            logger.debug("Validating extraction...")

            # these are in reverse priority for file/hash representation
            # 'html' is preferred; evaluated last
            scrapes = []
            if self.config.save_png:
                file_path = out_dir_uuid / "playwright.png"
                if self.validate_file(file_path):
                    scrapes.append(file_path)
                else:
                    logger.error(f"Error during png validation for {src.url}")

            if self.config.save_pdf:
                file_path = out_dir_uuid / "playwright.pdf"
                if self.validate_file(file_path):
                    scrapes.append(file_path)
                else:
                    logger.error(f"Error during pdf validation for {src.url}")

            if self.config.save_dom:
                file_path = out_dir_uuid / "playwright.html"
                if self.validate_file(file_path):
                    scrapes.append(file_path)
                else:
                    logger.error(f"Error during html validation for {src.url}")

            if any(scrapes):
                src.scrape_status = ScrapeStatus.COMPLETE
                src.content_hash = self.hash(scrapes[-1])
                src.file = str(file_path)
            else:
                raise ExtractionError("All playwright files failed validation.")  # NOQA: TRY301

        except Exception as e:
            src.scrape_status = ScrapeStatus.ERROR
            src.error_message = str(e)

            with (out_dir_uuid / "errors.txt").open("a") as f:
                lines = [str(src.scraped_at), f"Failed to extract url {src.url}", f"Error: {str(e)}"]
                f.writelines(line + os.linesep for line in lines)

            self.cleanup()

        return src
