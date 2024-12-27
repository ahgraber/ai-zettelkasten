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
import itertools
import logging
import os
from pathlib import Path
from subprocess import CalledProcessError  # , run
import sys
from typing import Any, List, Tuple, override

from playwright.async_api import Playwright, async_playwright
from playwright_stealth import Stealth  # https://github.com/Mattwmaster58/playwright_stealth
from pydantic import ConfigDict, Field, TypeAdapter
from pydantic_settings import BaseSettings, SettingsConfigDict

from aizk.datamodel.schema import ScrapeStatus, Source, ValidatedURL
from aizk.extractors.base import ExtractionError, Extractor
from aizk.extractors.chrome import CHROME_USER_AGENT, detect_playwright_chromium, detect_system_chrome
from aizk.extractors.utils import atomic_write, dedupe, download_file, get_write_mode, validate_file
from aizk.utilities.path_helpers import (
    DEFAULT_ENV_PATH,
    ExecPath,
    SysPATH,
    find_binary_abspath,
    get_local_bin_dir,
    path_is_dir,
    path_is_executable,
    path_is_file,
    symlink_to_bin,
)

logger = logging.getLogger(__file__)


browser_dir_path = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "~/Library/Caches/ms-playwright")


class PlaywrightSettings(BaseSettings):
    """Default Playwright / Chromium settings."""

    # Chrome Binary - not really used except for ensuring it exists
    binary: str = Field(default=str(detect_playwright_chromium()))
    timeout: int = Field(default=90, ge=15, lt=3600)  # global process timeout - 10

    chrome_user_agent: str = Field(default=CHROME_USER_AGENT)

    chrome_profile_dir: Path | None = Field(default=Path.cwd() / ".profile" / "chrome")
    chrome_profile_name: str = Field(default="Default")

    # Chrome Options Tuning
    headless: bool = Field(default=True)
    sandbox: bool = Field(default=True)  # false if in docker
    resolution: tuple[int, int] = Field(default=(1440, 2000))
    pageload_timeout: int = Field(default=10, ge=5, lt=3600)  # wait for page to finish loading

    save_dom: bool = Field(default=True)
    save_pdf: bool = Field(default=True)
    save_png: bool = Field(default=True)

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


class PlaywrightExtractor(Extractor):
    """Playwright / Chromium extractor."""

    name: str = "pw_chromium"
    default_filename: str = "content.html"
    config: PlaywrightSettings

    def __init__(
        self,
        config: PlaywrightSettings | dict[str, Any] | None = None,
        out_dir: Path | str | None = None,
        loop: asyncio.BaseEventLoop | None = None,
    ):
        config = self.validate_config(config or {})
        binary = config.binary or detect_playwright_chromium()

        super().__init__(
            config=config,
            binary=binary,
            out_dir=out_dir or Path.cwd() / "data" / self.name,
        )

        if loop is None:
            try:
                self.loop = asyncio.get_running_loop()
            except RuntimeError:
                self.loop = asyncio.new_event_loop()
        else:
            self.loop = loop

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

                with atomic_write(out_dir / "content.html", binary_mode=get_write_mode(html_content) == "wb") as f:
                    f.write(html_content)

            # Save PDF - returns bytes if no path provided
            if self.config.save_pdf:
                pdf_content = await page.pdf(
                    # path="./content.pdf",
                    format="Letter",
                    display_header_footer=False,
                )

                with atomic_write(out_dir / "content.pdf", binary_mode=get_write_mode(pdf_content) == "wb") as f:
                    f.write(pdf_content)

            # Save screenshot - returns bytes if no path provided
            if self.config.save_png:
                await page.emulate_media(media="screen", color_scheme="light")
                img_content = await page.screenshot(
                    # path="./content.png",
                    full_page=True,
                    scale="device",
                )

                with atomic_write(out_dir / "content.png", binary_mode=get_write_mode(pdf_content) == "wb") as f:
                    f.write(img_content)

            await context.close()

    def __call__(self, source: Source) -> Source:
        """Execute extraction pipeline."""
        src = source.model_copy()

        out_dir_uuid = self.out_dir / str(src.uuid)
        out_dir_uuid.mkdir(exist_ok=True)
        src.scraped_at = datetime.datetime.now(datetime.timezone.utc)

        try:
            self.loop.create_task(self.run(src.url, out_dir_uuid))

            # these are in reverse priority for file/hash representation
            # 'html' is preferred; evaluated last
            scrape_statuses = []
            if self.config.save_png:
                file_path = out_dir_uuid / "content.png"
                if self.validate_file(file_path):
                    scrape_statuses.append(True)
                else:
                    logger.error(f"Error during png validation for {src.url}")

            if self.config.save_pdf:
                file_path = out_dir_uuid / "content.pdf"
                if self.validate_file(file_path):
                    scrape_statuses.append(True)
                else:
                    logger.error(f"Error during pdf validation for {src.url}")

            if self.config.save_dom:
                file_path = out_dir_uuid / "content.html"
                if self.validate_file(file_path):
                    scrape_statuses.append(True)
                else:
                    logger.error(f"Error during html validation for {src.url}")

            if any(scrape_statuses):
                src.scrape_status = ScrapeStatus.COMPLETE
                src.content_hash = self.hash(file_path)
                src.file = str(file_path)

        except Exception as e:
            src.scrape_status = ScrapeStatus.ERROR
            src.error_message = str(e)

            with (out_dir_uuid / "errors.txt").open("a") as f:
                lines = [str(src.scraped_at), f"Failed to extract url {src.url}", f"Error: {str(e)}"]
                f.writelines(line + os.linesep for line in lines)

            self.cleanup()

        return src
