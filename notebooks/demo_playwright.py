# %%
import asyncio
import logging
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID

from dotenv import load_dotenv
from IPython.core.getipython import get_ipython  # NOQA: E402
from IPython.core.interactiveshell import InteractiveShell  # NOQA: E402
from playwright.async_api import Playwright, async_playwright
from playwright_stealth import Stealth  # https://github.com/Mattwmaster58/playwright_stealth

from aizk.datamodel.schema import Source
from aizk.extractors.chrome import detect_playwright_chromium, detect_system_chrome
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
from aizk.utilities.process import TimeWindowRateLimiter

# %%
ipython: InteractiveShell | None = get_ipython()
if ipython is not None:
    ipython.run_line_magic("load_ext", "autoreload")
    ipython.run_line_magic("autoreload", "2")

# %%
logger = logging.getLogger(__file__)

# %%
repo = subprocess.check_output(  # NOQA: S603
    ["git", "rev-parse", "--show-toplevel"],  # NOQA: S607
    cwd=Path(__file__).parent,
    encoding="utf-8",
).strip()
repo = Path(repo).expanduser().resolve()

datadir = repo / "data"
datadir.mkdir(exist_ok=True)


# %%
loop = asyncio.get_event_loop()
limiter = TimeWindowRateLimiter(5, 7)  # 5 requests every 7 seconds

# %%
# url = "https://www.bloomberg.com/graphics/2023-generative-ai-bias/"
uuid = UUID("b046e81f-1928-4c00-ba93-89bd7e933891")
url = "https://aimlbling-about.ninerealmlabs.com/blog/for-some-definition-of-open/"
source = Source(uuid=uuid, url=url)

# %%
# TODO:
# playwright download standard chrome
# open standard chrome to configure user profile and cookie jar

# TODO
# [arXiv API Access - arXiv info](https://info.arxiv.org/help/api/index.html)
# [Full Text via S3 - arXiv info](https://info.arxiv.org/help/bulk_data_s3.html)

# %%
_ = load_dotenv()

browser_dir_path = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "~/Library/Caches/ms-playwright")


chromium_path = detect_playwright_chromium()

chrome_profile_dir = Path(os.environ["CHROME_USER_DATA"])
if str(chrome_profile_dir).endswith("/Default"):
    raise ValueError("Chrome profile dir / user data dir should not include '/Default' at the end.")

_ = path_is_dir(chrome_profile_dir)
_ = path_is_dir(chrome_profile_dir / "Default")

# %%
# use async in jupyter

# Chromium's user data directory is the parent directory of the "Profile Path" seen at chrome://version
user_data_dir = str(chrome_profile_dir)
ubo_extension_path = str(browser_dir_path / "uBOLite.chromium.mv3")

CHROME_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/118.0.0.0 Safari/537.36"  # "Chrome/131.0.0.0 Safari/537.36"
)


# %%
async def run_ephemeral(url: str, dom: bool = True, pdf: bool = True, png: bool = True):
    fname = "run1"

    # async with async_playwright() as pw:
    async with Stealth().use_async(async_playwright()) as pw:
        browser = await pw.chromium.launch(
            channel="chromium",  # use "new" headless mode to enable headless extensions
            headless=True,
        )
        context = await browser.new_context(
            # emulate high-DPI
            viewport={"width": 2560, "height": 1440},
            device_scale_factor=2,
        )
        page = await context.new_page()

        # wait until extensions and init scripts are initialized
        await page.wait_for_timeout(1000)

        # load page and wait until network activity stops(or time limit reached)
        await page.goto(url)
        await page.wait_for_load_state("networkidle", timeout=30_000)  # 30s timeout

        # Get the final DOM content
        if dom:
            html_content = await page.content()

            # Save DOM
            print("Save html")
            with open(f"./{fname}.html", "w", encoding="utf-8") as f:
                f.write(html_content)

        # Save PDF - returns bytes if no path provided
        if pdf:
            print("Save pdf")
            await page.pdf(path=f"./{fname}.pdf", format="Letter", display_header_footer=False)

        # Save screenshot - returns bytes if no path provided
        if png:
            print("Save png")
            await page.emulate_media(media="screen", color_scheme="light")
            await page.screenshot(path=f"./{fname}.png", timeout=30_000, full_page=True, scale="device")
            # await page.emulate_media(media="screen", color_scheme="dark")
            # await page.screenshot(path=f"./{fname}_dark.png", timeout=30_000, full_page=True, scale="device")

        await context.close()


# %%
# add task to current loop
# url = "https://arh.antoinevastel.com/bots/areyouheadless"
url = "https://bot.sannysoft.com/"
# url = "http://whatsmyuseragent.org/"

loop.create_task(run_ephemeral(url, dom=False, pdf=False, png=True))


# %%
# https://github.com/Mattwmaster58/playwright_stealth?tab=readme-ov-file
# stealth_scripts = StealthConfig()


async def run_with_persistent_context(url: str, dom: bool = True, pdf: bool = True, png: bool = True):
    fname = "run2"
    # https://playwright.dev/python/docs/chrome-extensions
    # load with extension: https://github.com/microsoft/playwright-python/issues/782#issuecomment-879763588
    # NOTE: Google Chrome data dirs and Playwright dirs are not compatible
    # async with async_playwright() as pw:
    async with Stealth().use_async(async_playwright()) as pw:
        print("Init session context")
        context = await pw.chromium.launch_persistent_context(
            channel="chromium",  # use "new" headless mode to enable headless extensions
            headless=True,
            user_agent=CHROME_USER_AGENT,
            user_data_dir=user_data_dir,  # Required for extensions
            args=[
                f"--disable-extensions-except={ubo_extension_path}",
                f"--load-extension={ubo_extension_path}",
                # f"--user-data-dir={user_data_dir}",
            ],
            # emulate high-DPI
            viewport={"width": 2560, "height": 1440},
            device_scale_factor=2,
        )

        # print("Init extension")
        # # manifest v2
        # if len(context.background_pages) == 0:
        #     background_page = await context.wait_for_event("backgroundpage")
        # else:
        #     background_page = context.background_pages[0]

        # # for manifest v3:
        # print(f"{context.service_workers=}")
        # if len(context.service_workers) == 0:
        #     background = await context.wait_for_event("serviceworker")
        # else:
        #     background = context.service_workers[0]

        print("Init page")
        # launch_persistent_context creates pages
        page = await context.new_page()

        # stealth mode (limit blocking due to headless mode)
        # await stealth_async(page)
        # await page.add_init_script("\n".join(stealth_scripts.enabled_scripts))

        # wait until extensions and init scripts are initialized
        await page.wait_for_timeout(1000)

        print(f"Go to {url}")
        # load page and wait until network activity stops(or time limit reached)
        await page.goto(url)
        await page.wait_for_load_state("networkidle", timeout=30_000)  # 30s timeout

        # Get the final DOM content
        if dom:
            html_content = await page.content()

            # Save DOM
            print("Save html")
            with open(f"./{fname}.html", "w", encoding="utf-8") as f:
                f.write(html_content)

        # Save PDF - returns bytes if no path provided
        if pdf:
            print("Save pdf")
            await page.pdf(path=f"./{fname}.pdf", format="Letter", display_header_footer=False)

        # Save screenshot - returns bytes if no path provided
        if png:
            print("Save png")
            await page.emulate_media(media="screen", color_scheme="light")
            await page.screenshot(path=f"./{fname}.png", timeout=30_000, full_page=True, scale="device")
            # await page.emulate_media(media="screen", color_scheme="dark")
            # await page.screenshot(path=f"./{fname}_dark.png", timeout=30_000, full_page=True, scale="device")

        await context.close()


# %%
# add task to current loop
# url = "https://arh.antoinevastel.com/bots/areyouheadless"
url = "https://bot.sannysoft.com/"
# url = "http://whatsmyuseragent.org/"

loop.create_task(run_with_persistent_context(url, dom=False, pdf=False, png=True))

# %%
