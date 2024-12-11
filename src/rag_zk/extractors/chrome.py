# ruff: NOQA: E731
import json
import logging
import os
from pathlib import Path
import platform
from subprocess import PIPE, CalledProcessError, CompletedProcess, Popen, TimeoutExpired, run
from typing import Any, List, Tuple, override

from pydantic import ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from rag_zk.datamodel.schema import ScrapeStatus, Source, ValidatedURL
from rag_zk.extractors.base import ExtractionError, Extractor
from rag_zk.extractors.utils import atomic_write, dedupe
from rag_zk.utilities.path_helpers import (
    DEFAULT_ENV_PATH,
    PATHStr,
    find_binary_abspath,
    get_local_bin,
    path_is_executable,
    path_is_file,
    symlink_to_bin,
)

logger = logging.getLogger(__name__)


CHROMIUM_BINARY_NAMES_LINUX = [
    "chromium",
    "chromium-browser",
    "chromium-browser-beta",
    "chromium-browser-unstable",
    "chromium-browser-canary",
    "chromium-browser-dev",
]
CHROMIUM_BINARY_NAMES_MACOS = ["/Applications/Chromium.app/Contents/MacOS/Chromium"]
CHROMIUM_BINARY_NAMES = CHROMIUM_BINARY_NAMES_LINUX + CHROMIUM_BINARY_NAMES_MACOS

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
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
]
CHROME_BINARY_NAMES = CHROME_BINARY_NAMES_LINUX + CHROME_BINARY_NAMES_MACOS

CHROME_SAVE_ACTIONS = {"html": "--dump-dom", "pdf": "--print-to-pdf", "image": "--screenshot"}


def autodetect_system_chrome_install(PATH: PATHStr | None = None) -> Path:  # NOQA: N803
    """Find system Chrome/Chromium if exists in default locations."""
    for bin_name in CHROME_BINARY_NAMES + CHROMIUM_BINARY_NAMES:
        try:
            abspath = find_binary_abspath(bin_name, PATH=PATH if PATH else DEFAULT_ENV_PATH)
        except FileNotFoundError:
            pass
        else:
            return abspath

    # return None
    raise FileNotFoundError("Could not find Chrome/Chromium binary on default system paths. Is it installed?")


class ChromeSettings(BaseSettings):
    """Default Chrome settings."""

    # USE_CHROME: bool = Field(default=True)
    # CHROME_BINARY: str = Field(default="chrome")

    # Chrome Binary
    CHROME_DEFAULT_ARGS: List[str] = Field(
        default=[
            "--disable-sync",
            "--no-pings",
            "--no-first-run",  # dont show any first run ui / setup prompts
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
    CHROME_EXTRA_ARGS: List[str] = Field(default=[])

    # Chrome Options Tuning
    CHROME_TIMEOUT: int = Field(default=45, ge=15, lt=3600)  # global process timeout - 10
    CHROME_HEADLESS: bool = Field(default=True)
    CHROME_SANDBOX: bool = Field(default=True)  # false if in docker
    CHROME_RESOLUTION: str = Field(default="1440,2000")
    CHROME_CHECK_SSL_VALIDITY: bool = Field(default=True)

    # Cookies & Auth
    CHROME_USER_AGENT: str = Field(
        default=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/118.0.0.0 Safari/537.36"
        )
    )
    CHROME_USER_DATA_DIR: Path | None = Field(default=Path.cwd() / ".profile" / "chrome")
    CHROME_PROFILE_NAME: str = Field(default="Default")

    # Extractor Toggles
    SAVE_SCREENSHOT: bool = Field(default=True)
    SAVE_DOM: bool = Field(default=True)
    SAVE_PDF: bool = Field(default=True)

    OVERWRITE: bool = Field(default=False)

    def validate(self):
        """Validate settings."""
        if self.CHROME_TIMEOUT < 15:
            logger.error(
                f"Warning: TIMEOUT is set too low! "
                f"(currently set to TIMEOUT={self.CHROME_TIMEOUT} seconds).\n"
                "Chrome will fail to archive all sites if set to less than ~15 seconds."
            )

        # if user has specified a user data dir, make sure its valid
        if self.CHROME_USER_DATA_DIR:
            try:
                (Path(self.CHROME_USER_DATA_DIR) / self.CHROME_PROFILE_NAME).mkdir(exist_ok=True, parents=True)
            except Exception as e:
                logger.debug(e)
                pass

            # check to make sure user_data_dir/<profile_name> exists
            if not os.path.isdir(self.CHROME_USER_DATA_DIR / self.CHROME_PROFILE_NAME):
                logger.error(
                    f"Could not find profile '{self.CHROME_PROFILE_NAME}' "
                    f"in CHROME_USER_DATA_DIR {self.CHROME_USER_DATA_DIR}\n"
                    "Make sure you set it to a Chrome user data directory containing a Default profile folder."
                )

                # show special hint if they made the common mistake of putting /Default at the end of the path
                if str(self.CHROME_USER_DATA_DIR).endswith("/Default"):
                    logger.error(
                        'Try removing /Default from the end e.g. CHROME_USER_DATA_DIR="{}"'.format(
                            str(self.CHROME_USER_DATA_DIR).rsplit("/Default", 1)[0]
                        )
                    )

                self.CHROME_USER_DATA_DIR = None

    @property
    def CHROME_ARGS(self) -> str:  # NOQA: D102, N802
        return "\n".join(self.chrome_args())

    def chrome_args(self, **options) -> List[str]:
        """Build a chrome shell command with arguments."""
        # Chrome CLI flag documentation: https://peter.sh/experiments/chromium-command-line-switches/

        options = self.model_copy(update=options)

        cmd_args = [*options.CHROME_DEFAULT_ARGS, *options.CHROME_EXTRA_ARGS]

        if options.CHROME_HEADLESS:
            cmd_args += ["--headless"]  # expects chrome version >= 111

        if not options.CHROME_SANDBOX:
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

        # set window size for screenshot/pdf/etc. rendering
        cmd_args += ("--window-size={}".format(options.CHROME_RESOLUTION),)

        if not options.CHROME_CHECK_SSL_VALIDITY:
            cmd_args += ("--disable-web-security", "--ignore-certificate-errors")

        if options.CHROME_USER_AGENT:
            cmd_args += ("--user-agent={}".format(options.CHROME_USER_AGENT),)

        # this no longer works on newer chrome versions, just causes chrome to hang indefinitely:
        # if options.CHROME_TIMEOUT:
        #   cmd_args += ('--timeout={}'.format(options.CHROME_TIMEOUT * 1000),)

        if options.CHROME_USER_DATA_DIR:
            # remove SingletonLock file
            lockfile = options.CHROME_USER_DATA_DIR / options.CHROME_PROFILE_NAME / "SingletonLock"
            lockfile.unlink(missing_ok=True)

            cmd_args.append("--user-data-dir={}".format(options.CHROME_USER_DATA_DIR))
            cmd_args.append("--profile-directory={}".format(options.CHROME_PROFILE_NAME or "Default"))

            # if CHROME_USER_DATA_DIR is set but folder is empty, create a new profile inside it
            if not os.path.isfile(options.CHROME_USER_DATA_DIR / options.CHROME_PROFILE_NAME / "Preferences"):
                logger.debug(
                    "Creating new Chrome profile in: {}".format(
                        str(Path(options.CHROME_USER_DATA_DIR) / options.CHROME_PROFILE_NAME)
                    )
                )
                cmd_args.remove("--no-first-run")
                cmd_args.append("--first-run")

        return dedupe(cmd_args)


CHROME_CONFIG = ChromeSettings()


class ChromeExtractor(Extractor):
    """Chrome extractor."""

    name: str = "chrome"
    default_filename: str = "output.html"
    config: ChromeSettings

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        out_dir: Path | str | None = None,
    ):
        # super().__init__(
        #     out_dir=out_dir or Path.cwd() / "data" / self.name
        # )
        self.binary = self.init_binary()
        # self.config = config or {}
        self.config = self.validate_config(config or {})
        self.out_dir = out_dir or Path.cwd() / "data" / ChromeExtractor.name

    @override
    def init_binary(
        self,
        bin_path_or_name="Chrome",  # ignored; for equivalent signatures
        syspath: PATHStr | None = None,
    ) -> Path:
        chrome_bin = autodetect_system_chrome_install(PATH=syspath)
        # symlink = symlink_to_bin(chrome_bin)
        # return symlink
        return chrome_bin

    @override
    def cleanup(self):
        """Clean up any state or runtime files that Chrome leaves behind when killed by a timeout or other error."""
        try:
            linux_lock_file = Path("~/.config/chromium/SingletonLock").expanduser()
            linux_lock_file.unlink(missing_ok=True)
        except Exception as e:
            logger.debug(f"Ignoring exception {e}")
            pass

        if self.config.CHROME_USER_DATA_DIR:
            try:
                (self.config.CHROME_USER_DATA_DIR / "SingletonLock").unlink(missing_ok=True)
            except Exception as e:
                logger.debug(f"Ignoring exception {e}")
                pass

    @override
    def validate_config(self, c: BaseSettings | dict[str, Any]) -> ChromeSettings:
        """Validate the extractor config."""
        return ChromeSettings.model_validate(c)


class ChromeHTMLExtractor(ChromeExtractor):
    """Chrome html extractor."""

    name: str = "chrome-html"
    default_filename: str = "output.html"
    config: ChromeSettings

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        out_dir: Path | str | None = None,
    ):
        super().__init__(config=config, out_dir=out_dir)

    @override
    def run(self, url: ValidatedURL | str):
        # Get HTML version of article
        cmd = [
            str(self.binary),
            *CHROME_CONFIG.chrome_args(),
            "--dump-dom",
            url,
        ]
        logger.debug(f"{cmd=}")
        result = run(  # NOQA: S603
            cmd,
            capture_output=True,
            timeout=self.config.CHROME_TIMEOUT,
        )

        try:
            result.check_returncode()  # raises error if failed
        except CalledProcessError as e:
            raise ExtractionError(f"{self.name} extraction of {url} failed:\n'{result.stderr.decode()}'") from e

        return result.stdout

    @override
    def validate_extract(self, extract) -> Tuple[ScrapeStatus, str]:
        try:
            content = extract.decode()
        except Exception as e:
            return ScrapeStatus("ERROR"), f"Failed to decode content: {e}"

        if content is None or len(content) < 5:
            return ScrapeStatus("ERROR"), "Process completed successfully but content is missing."

        return ScrapeStatus("COMPLETE"), content
