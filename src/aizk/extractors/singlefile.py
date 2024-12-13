import datetime
import json
import logging
from pathlib import Path
from subprocess import CalledProcessError, run

from aizk.datamodel.schema import ScrapeStatus, Source
from aizk.extractors.utils import (
    atomic_write,
    bin_version,
    find_chrome_binary,
    find_node_binary,
    save_and_hash,
)
from aizk.utilities.path_helpers import add_node_bin_to_PATH, find_binary_abspath

logger = logging.getLogger(__name__)

TIMEOUT = 60

CHROME_BINARY = find_chrome_binary()
CHROME_DEFAULT_OPTIONS = {
    "CHROME_BINARY": CHROME_BINARY,
    "CHROME_VERSION": bin_version(CHROME_BINARY),
    "CHROME_HEADLESS": True,
    "CHROME_USER_AGENT": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "CHROME_USER_DATA_DIR": "./chromium-profile",
    "CHROME_WINDOW_SIZE": "1440,2000",
    "CHROME_TIMEOUT": 0,
    "CHROME_CHECK_SSL_VALIDITY": True,
    "CHROME_ADBLOCK_EXTENSION": "uBOLite.chromium.mv3",
    "CHROME_SANDBOX": True,
}

# SINGLEFILE_BINARY = find_node_binary("single-file")
SINGLEFILE_BINARY = find_binary_abspath("postlight-parser", PATH=add_node_bin_to_PATH())
SINGLEFILE_ARGS = [
    "--dump-content",  # Dump the content of the processed page in the console ('true' when running in Docker) <boolean>
]
SINGLEFILE_TIMEOUT_SEC = TIMEOUT


def chrome_args(**options) -> list[str]:
    """Build chrome shell command arguments."""
    options = {**CHROME_DEFAULT_OPTIONS, **options}
    cmd_args = []

    if options["CHROME_BINARY"] is None:
        raise FileNotFoundError("Could not find any CHROME_BINARY installed on your system")
    # cmd_args = [options["CHROME_BINARY"]]

    if options["CHROME_HEADLESS"]:
        cmd_args.append("--headless=new")  # req: chrome > v111

    if options["CHROME_USER_AGENT"]:
        cmd_args.append("--user-agent={}".format(options["CHROME_USER_AGENT"]))

    if options["CHROME_USER_DATA_DIR"]:
        cmd_args.append("--user-data-dir={}".format(options["CHROME_USER_DATA_DIR"]))

    if options["CHROME_WINDOW_SIZE"]:
        cmd_args.append("--window-size={}".format(options["CHROME_WINDOW_SIZE"]))

    if options["CHROME_TIMEOUT"]:
        cmd_args.append("--timeout={}".format(options["CHROME_TIMEOUT"] * 1000))

    if not options["CHROME_CHECK_SSL_VALIDITY"]:
        cmd_args.extend(["--disable-web-security", "--ignore-certificate-errors"])

    if options["CHROME_ADBLOCK_EXTENSION"]:
        cmd_args.append("--load-extension={}".format(options["CHROME_ADBLOCK_EXTENSION"]))

    if not options["CHROME_SANDBOX"]:
        # assume this means we are running inside a docker container
        # in docker, GPU support is limited, sandboxing is unnecessary,
        # and SHM is limited to 64MB by default (which is too low to be usable).
        cmd_args.extend(
            [
                "--no-sandbox",
                "--no-zygote",
                "--disable-dev-shm-usage",
                "--disable-software-rasterizer",
                "--run-all-compositor-stages-before-draw",
                "--hide-scrollbars",
                "--autoplay-policy=no-user-gesture-required",
                "--no-first-run",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--disable-sync",
            ]
        )

    return cmd_args


def scrape_singlefile(source: Source, out_dir: Path, timeout: int = TIMEOUT) -> Source:
    """Download reader friendly version using @postlight/parser."""
    output_folder = out_dir.expanduser().resolve()

    source.scraped_at = datetime.datetime.now(datetime.timezone.utc)

    chrome_options = chrome_args(CHROME_TIMEOUT=0)

    # SingleFile CLI Docs: https://github.com/gildas-lormeau/SingleFile/tree/master/cli
    chrome_options = [f"--browser-executable-path={CHROME_BINARY}"] + [
        f"--browser-arg={option}" for option in chrome_options
    ]
    singlefile_options = sorted(set(SINGLEFILE_ARGS + chrome_options))  # singlefile does not like duplicate
    # Get HTML version of article
    cmd = [SINGLEFILE_BINARY, source.url] + singlefile_options
    logger.debug(f"{cmd=}")
    result = run(  # NOQA: S603
        cmd,
        capture_output=True,
        timeout=timeout,
    )

    # error states
    try:
        result.check_returncode()  # raises error if failed
    except CalledProcessError:
        source.scrape_status = ScrapeStatus("ERROR")
        source.error_message = "Non-zero exit code from @postlight/parser"
        logger.debug(source.error_message)

    try:
        article_json = json.loads(result.stdout)
    except json.JSONDecodeError:
        source.scrape_status = ScrapeStatus("ERROR")
        source.error_message = "Failed to parse JSON response from @postlight/parser"
        logger.debug(source.error_message)

    if article_json.get("error") or article_json.get("failed") or (article_json.get("content") is None):
        source.scrape_status = ScrapeStatus("ERROR")
        source.error_message = "@postlight/parser was unable to get article HTML from the URL"
        logger.debug(source.error_message)

    # success
    source.scrape_status = ScrapeStatus("COMPLETE")
    fpath = output_folder / "content.html"
    source.content_hash = save_and_hash(fpath, article_json.pop("content"))
    source.file = str(fpath)

    with atomic_write(output_folder / "metadata.json") as f:
        json.dump(article_json, f)

    return source
