import argparse
import asyncio
import datetime
import hashlib
from itertools import batched
import logging
import os
from pathlib import Path
import re
import subprocess
import sys

import dotenv
from sqlmodel import Field, Session, SQLModel, create_engine
from tqdm.asyncio import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

from aizk.core.database import (
    add_urls_to_backlog,
    get_db_engine,
    get_pending_sources,
    initialize_database,
    update_scraped_sources,
)
from aizk.datamodel.schema import *
from aizk.datamodel.schema import ScrapeStatus, Source
from aizk.extractors import (
    STATICFILE_EXTENSIONS,
    ArxivExtractor,
    ArxivSettings,
    ChromeExtractor,
    ChromeSettings,
    ExtractionError,
    # Extractor,
    ExtractorSettings,
    PlaywrightExtractor,
    PlaywrightSettings,
    PostlightExtractor,
    PostlightSettings,
    SingleFileExtractor,
    SingleFileSettings,
    StaticFileExtractor,
)
from aizk.extractors.chrome import detect_playwright_chromium
from aizk.utilities import (
    LOG_FMT,
    SlidingWindowRateLimiter,
    basic_log_config,
    # logging_redirect_tqdm,
    path_is_dir,
    path_is_file,
    process_manager,
)
from aizk.utilities.url_helpers import find_all_urls, is_social_url

chromium = detect_playwright_chromium()


with process_manager("chromium"), process_manager("zsh"):
    subprocess.run(str(chromium))  # NOQA: S603

# chrome_profile = os.environ["CHROME_USER_DATA"]  # './chromium-profile'
# chrome_settings = ChromeSettings(binary=str(detect_playwright_chromium()))
# # pw_settings = PlaywrightSettings(binary=str(detect_playwright_chromium()))

# cmd = [
#     str(chromium),
#     f"--user-data-dir={str(chrome_profile or chrome_settings.chrome_profile_dir)}",
#     f"--profile_directory={str(chrome_settings.chrome_profile_name or 'Default')}",
# ]
# with process_manager("chromium"), process_manager("zsh"):
#     subprocess.run(cmd)
