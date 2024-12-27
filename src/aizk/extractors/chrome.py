# ruff: NOQA: E731
"""ChromeExtractor, ChromeHTMLExtractor, ChromePDFExtractor, ChromeScreenshotExtractor.

- ref: https://github.com/ArchiveBox/ArchiveBox/blob/dev/archivebox/pkgs/abx-plugin-chrome/abx_plugin_chrome/dom.py
- ref: https://github.com/ArchiveBox/ArchiveBox/blob/dev/archivebox/pkgs/abx-plugin-chrome/abx_plugin_chrome/pdf.py
- ref: https://github.com/ArchiveBox/ArchiveBox/blob/dev/archivebox/pkgs/abx-plugin-chrome/abx_plugin_chrome/screenshot.py
"""

import itertools
import logging
import os
from pathlib import Path
from subprocess import CalledProcessError  # , run
import sys
from typing import Any, List, Tuple, override

from pydantic import ConfigDict, Field, TypeAdapter
from pydantic_settings import BaseSettings, SettingsConfigDict

from aizk.datamodel.schema import ScrapeStatus, Source, ValidatedURL
from aizk.extractors.base import ExtractionError, Extractor
from aizk.extractors.utils import atomic_write, dedupe
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
from aizk.utilities.process import run

logger = logging.getLogger(__name__)


CHROMIUM_BINARY_NAMES_LINUX = [
    "chromium",
    "chromium-browser",
    "chromium-browser-beta",
    "chromium-browser-unstable",
    "chromium-browser-canary",
    "chromium-browser-dev",
]
CHROMIUM_BINARY_NAMES_MACOS = ["Chromium"]
CHROMIUM_BINARY_FULL_MACOS = [
    f"/Applications/{name}.app/Contents/MacOS/{name}" for name in CHROMIUM_BINARY_NAMES_MACOS
]
CHROMIUM_BINARY_NAMES = CHROMIUM_BINARY_NAMES_LINUX + CHROMIUM_BINARY_NAMES_MACOS + CHROMIUM_BINARY_FULL_MACOS

CHROME_BINARY_NAMES_LINUX = [
    "google-chrome",
    "google-chrome-stable",
    "google-chrome-beta",
    "google-chrome-canary",
    "google-chrome-unstable",
    "google-chrome-dev",
    "chrome",
]
CHROME_BINARY_NAMES_MACOS = [
    "Google Chrome",
    "Google Chrome Canary",
]
CHROME_BINARY_FULL_MACOS = [f"/Applications/{name}.app/Contents/MacOS/{name}" for name in CHROME_BINARY_NAMES_MACOS]
CHROME_BINARY_NAMES = CHROME_BINARY_NAMES_LINUX + CHROME_BINARY_NAMES_MACOS + CHROME_BINARY_FULL_MACOS

CHROME_SAVE_ACTIONS = {"html": "--dump-dom", "pdf": "--print-to-pdf", "image": "--screenshot"}

CHROME_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/118.0.0.0 Safari/537.36"  # "Chrome/131.0.0.0 Safari/537.36"
)


def detect_playwright_chromium() -> Path | None:
    """Find Chromium installed by playwright if exists."""
    import subprocess

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


def detect_system_chrome(syspath: SysPATH | None = None) -> Path | None:
    """Find system Chrome/Chromium if exists in default locations."""
    for bin_name in CHROME_BINARY_NAMES + CHROMIUM_BINARY_NAMES:
        try:
            abspath = find_binary_abspath(bin_name, syspath=syspath if syspath else DEFAULT_ENV_PATH)
        except FileNotFoundError:
            pass
        else:
            return abspath

    # raise FileNotFoundError("Could not find Chrome/Chromium binary on default system paths. Is it installed?")
    logger.error("Could not find Chrome/Chromium binary on default system paths. Is it installed?")
    return None


class ChromeSettings(BaseSettings):
    """Default Chrome settings."""

    # Chrome Binary
    binary: str = Field(default=str(detect_playwright_chromium()))
    timeout: int = Field(default=90, ge=15, lt=3600)  # global process timeout - 10

    # Cookies & Auth
    chrome_user_agent: str = Field(CHROME_USER_AGENT)

    chrome_profile_dir: Path | None = Field(default=Path.cwd() / ".profile" / "chrome")
    chrome_profile_name: str = Field(default="Default")

    # Chrome Options Tuning
    headless: bool = Field(default=True)
    sandbox: bool = Field(default=True)  # false if in docker
    resolution: str = Field(default="1440,2000")
    pageload_timeout: int = Field(default=10, ge=5, lt=3600)  # wait for page to finish loading
    check_ssl_validity: bool = Field(default=True)

    # save_dom: bool = Field(default=True)
    # save_pdf: bool = Field(default=True)
    # save_png: bool = Field(default=True)

    default_args: List[str] = Field(
        default=[
            "--disable-sync",
            "--no-pings",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-default-apps",
            "--ash-no-nudges",
            "--disable-infobars",
            "--disable-blink-features=AutomationControlled",
            "--js-flags=--random-seed=1157259159",
            "--deterministic-mode",
            "--deterministic-fetch",
            "--start-maximized",
            "--test-type=gpu",
            "--disable-search-engine-choice-screen",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
            "--suppress-message-center-popups",
            "--disable-client-side-phishing-detection",
            "--disable-domain-reliability",
            "--disable-component-update",
            "--disable-datasaver-prompt",
            "--disable-hang-monitor",
            "--disable-session-crashed-bubble",
            "--disable-speech-synthesis-api",
            "--disable-speech-api",
            "--disable-print-preview",
            "--safebrowsing-disable-auto-update",
            "--deny-permission-prompts",
            "--disable-external-intent-requests",
            "--disable-notifications",
            "--disable-desktop-notifications",
            "--noerrdialogs",
            "--disable-popup-blocking",
            "--disable-prompt-on-repost",
            "--silent-debugger-extension-api",
            "--block-new-web-contents",
            "--metrics-recording-only",
            "--disable-breakpad",
            "--run-all-compositor-stages-before-draw",
            "--use-fake-device-for-media-stream",  # provide fake camera if site tries to request camera access
            "--simulate-outdated-no-au=Tue, 31 Dec 2099 23:59:59 GMT",  # ignore chrome updates
            "--force-gpu-mem-available-mb=4096",  # allows for longer full page screenshots
            "--password-store=basic",
            "--use-mock-keychain",
            "--disable-cookie-encryption",
            "--allow-legacy-extension-manifests",
            "--disable-gesture-requirement-for-media-playback",
            "--font-render-hinting=none",
            "--force-color-profile=srgb",
            "--disable-partial-raster",
            "--disable-skia-runtime-opts",
            "--disable-2d-canvas-clip-aa",
            "--disable-lazy-loading",
            "--disable-renderer-backgrounding",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-ipc-flooding-protection",
            "--disable-extensions-http-throttling",
            "--disable-field-trial-config",
            "--disable-back-forward-cache",
        ]
    )
    extra_args: List[str] = Field(default=[])

    # Extractor Toggles
    # OVERWRITE: bool = Field(default=False)

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

    @property
    def cli_args(self) -> str:
        """Provide args in CLI-friendly newline-delimited list."""
        return "\n".join(self.chrome_args())

    def chrome_args(self, **options) -> List[str]:
        """Build a chrome shell command with arguments."""
        # Chrome CLI flag documentation:
        # - https://developer.chrome.com/docs/chromium/headless
        # - https://peter.sh/experiments/chromium-command-line-switches/

        options = self.model_copy(update=options)

        cmd_args = [
            *options.default_args,
            *options.extra_args,
            f"--user-agent={options.chrome_user_agent}",
            f"--window-size={options.resolution}",
            f"--timeout={options.pageload_timeout * 1000}",  # pageload_timeout is milliseconds
        ]

        if options.headless:
            cmd_args += ["--headless=new"]  # expects chrome version >= 112

        if not options.sandbox:
            # assume this means we are running inside a docker container
            # in docker, GPU support is limited, sandboxing is unnecessary,
            # and SHM is limited to 64MB by default (which is too low to be usable).
            cmd_args += (
                "--no-sandbox",
                "--no-zygote",
                "--disable-dev-shm-usage",
                "--disable-software-rasterizer",
                "--disable-sync",
                # "--password-store=basic",
            )

        if not options.check_ssl_validity:
            cmd_args += ("--disable-web-security", "--ignore-certificate-errors")

        if options.chrome_profile_dir:
            # remove SingletonLock file
            lockfile = options.chrome_profile_dir / options.chrome_profile_name / "SingletonLock"
            lockfile.unlink(missing_ok=True)

            cmd_args.append(f"--user-data-dir={options.chrome_profile_dir}")
            cmd_args.append(f"--profile-directory={options.chrome_profile_name or 'Default'}")

            # if chrome profile is set has no preferences, let chrome know it is normal
            if not os.path.isfile(options.chrome_profile_dir / options.chrome_profile_name / "Preferences"):
                cmd_args.remove("--no-first-run")
                cmd_args.append("--first-run")

        return dedupe(cmd_args)


class ChromeExtractor(Extractor):
    """Chrome extractor."""

    name: str = "chrome"
    default_filename: str = "output.html"
    save_action: str = "None"
    config: ChromeSettings

    def __init__(
        self,
        config: ChromeSettings | dict[str, Any] | None = None,
        out_dir: Path | str | None = None,
    ):
        config = self.validate_config(config or {})
        binary = config.binary or detect_playwright_chromium() or detect_system_chrome()

        super().__init__(
            config=config,
            binary=binary,
            out_dir=out_dir or Path.cwd() / "data" / self.name,
        )

        self.cleanup()

    @override
    def validate_config(self, cfg: ChromeSettings | dict[str, Any]) -> ChromeSettings:
        """Validate the extractor config."""
        return ChromeSettings.model_validate(cfg)

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

    def cmd(self, url: ValidatedURL | str) -> List[str]:
        """Generate CLI command."""
        cmd = [
            str(self.binary),
            *self.config.chrome_args(),
            CHROME_SAVE_ACTIONS[self.save_action],  # "--dump-dom",
            url,
        ]
        return cmd

    @override
    def run(self, url: ValidatedURL | str, out_dir: Path) -> str | bytes:
        cmd = self.cmd(url)
        logger.debug(f"{cmd=}")
        result = run(  # NOQA: S603
            cmd,  # NOQA: S607
            cwd=out_dir,
            capture_output=True,
            text=True,
            timeout=self.config.timeout,
        )

        try:
            result.check_returncode()  # raises error if failed
        except CalledProcessError as e:
            self.cleanup()
            raise ExtractionError(f"{self.name} extraction of {url} failed:\n'{result.stderr}'") from e

        return result.stdout


class ChromeHTMLExtractor(ChromeExtractor):
    """Chrome extractor that saves page HTML."""

    name: str = "chrome-html"
    default_filename: str = "output.html"
    save_action: str = "html"
    config: ChromeSettings

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        out_dir: Path | str | None = None,
    ):
        super().__init__(config=config, out_dir=out_dir)

    @override
    def run(self, url: ValidatedURL | str, out_dir: Path) -> str | bytes:
        cmd = self.cmd(url)
        logger.debug(f"{cmd=}")
        result = run(  # NOQA: S603
            cmd,  # NOQA: S607
            cwd=out_dir,
            capture_output=True,
            text=True,
            timeout=self.config.timeout,
        )

        try:
            result.check_returncode()  # raises error if failed
        except CalledProcessError as e:
            self.cleanup()
            raise ExtractionError(f"{self.name} extraction of {url} failed:\n'{result.stderr}'") from e

        return result.stdout


class ChromePDFExtractor(ChromeExtractor):
    """Chrome extractor that saves page as PDF."""

    name: str = "chrome-pdf"
    default_filename: str = "output.pdf"
    save_action: str = "pdf"
    config: ChromeSettings

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        out_dir: Path | str | None = None,
    ):
        super().__init__(config=config, out_dir=out_dir)

    @override
    def run(self, url: ValidatedURL | str, out_dir: Path) -> str | bytes:
        cmd = self.cmd(url)
        logger.debug(f"{cmd=}")
        result = run(  # NOQA: S603
            cmd,  # NOQA: S607
            cwd=out_dir,
            capture_output=True,
            text=True,
            timeout=self.config.timeout,
        )

        try:
            result.check_returncode()  # raises error if failed
        except CalledProcessError as e:
            self.cleanup()
            raise ExtractionError(f"{self.name} extraction of {url} failed:\n'{result.stderr}'") from e

        return result.stdout

    # TODO: override __call__


class ChromeScreenshotExtractor(ChromeExtractor):
    """Chrome extractor that saves screenshot of page."""

    name: str = "chrome-png"
    default_filename: str = "screenshot.png"
    save_action: str = "image"
    config: ChromeSettings

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        out_dir: Path | str | None = None,
    ):
        super().__init__(config=config, out_dir=out_dir)

    @override
    def run(self, url: ValidatedURL | str, out_dir: Path) -> str | bytes:
        cmd = self.cmd(url)
        logger.debug(f"{cmd=}")
        result = run(  # NOQA: S603
            cmd,  # NOQA: S607
            cwd=out_dir,
            capture_output=True,
            text=True,
            timeout=self.config.timeout,
        )

        try:
            result.check_returncode()  # raises error if failed
        except CalledProcessError as e:
            self.cleanup()
            raise ExtractionError(f"{self.name} extraction of {url} failed:\n'{result.stderr}'") from e

        return result.stdout
