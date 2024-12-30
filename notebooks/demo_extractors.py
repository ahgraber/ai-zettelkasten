# %%
import json
import logging
import os
from pathlib import Path
import typing as t
from uuid import UUID

from IPython.core.getipython import get_ipython  # NOQA: E402
from IPython.core.interactiveshell import InteractiveShell  # NOQA: E402

from aizk.datamodel.schema import Source
from aizk.extractors import (
    STATICFILE_EXTENSIONS,
    ArxivExtractor,
    ArxivSettings,
    ChromeExtractor,
    ChromeSettings,
    ExtractionError,
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
from aizk.utilities import AsyncTimeWindowRateLimiter, TimeWindowRateLimiter, basic_log_config, get_repo_path
from aizk.utilities.async_helpers import synchronize

# %%
ipython: InteractiveShell | None = get_ipython()
if ipython is not None:
    ipython.run_line_magic("load_ext", "autoreload")
    ipython.run_line_magic("autoreload", "2")

# %%
basic_log_config()

# # set root logger to debug
# logging.getLogger().setLevel(logging.DEBUG)

# set all aizk to debug
for _logger in [logging.getLogger(name) for name in logging.root.manager.loggerDict if name.startswith("aizk")]:
    _logger.setLevel(logging.DEBUG)

logger = logging.getLogger(__file__)
logger.setLevel(logging.DEBUG)

# %%
repo = get_repo_path(__file__)

datadir = repo / "data"
datadir.mkdir(exist_ok=True)

demodir = Path(__file__).parent / "demo"

# %%
# 5 requests every 7 seconds
# limiter = TimeWindowRateLimiter(5, 7)
alimiter = AsyncTimeWindowRateLimiter(5, 7)

# %%
# url = "https://www.bloomberg.com/graphics/2023-generative-ai-bias/"
uuid = UUID("b046e81f-1928-4c00-ba93-89bd7e933891")
url = "https://aimlbling-about.ninerealmlabs.com/blog/for-some-definition-of-open/"
source = Source(uuid=uuid, url=url)

# %% [markdown]
# ## ArXiv

# %%
arxive_settings = ArxivSettings()
arxiv_extractor = ArxivExtractor(
    config=arxive_settings,
    out_dir=demodir / "arxiv",
    ensure_out_dir=True,
)


@alimiter
async def rate_limited_arxiv_extractor(*args, **kwargs):
    return await arxiv_extractor(*args, **kwargs)


# %% [markdown]
# ## Postlight-parser

# %%
postlight_settings = PostlightSettings()
postlight_extractor = PostlightExtractor(
    config=postlight_settings,
    out_dir=demodir / "postlight-parser",
    ensure_out_dir=True,
)


@alimiter
async def rate_limited_postlight_extractor(*args, **kwargs):
    return await postlight_extractor(*args, **kwargs)


# %% [markdown]
# ## Chrome

# %%
chrome_profile = os.environ["CHROME_USER_DATA"]  # './chromium-profile'
chrome_settings = ChromeSettings(binary=str(detect_playwright_chromium()))
chrome_extractor = ChromeExtractor(
    config=chrome_settings,
    out_dir=demodir / "chrome",
    ensure_out_dir=True,
)


@alimiter
async def rate_limited_chrome_extractor(*args, **kwargs):
    return await chrome_extractor(*args, **kwargs)


# %% [markdown]
# ## SingleFile

# %%
singlefile_settings = SingleFileSettings()
singlefile_extractor = SingleFileExtractor(
    config=singlefile_settings,
    chrome_config=chrome_settings,  # reuse from above
    out_dir=demodir / "singlefile",
    ensure_out_dir=True,
)


@alimiter
async def rate_limited_singlefile_extractor(*args, **kwargs):
    return await singlefile_extractor(*args, **kwargs)


# %% [markdown]
# ## Playwright

# %%
playwright_settings = PlaywrightSettings()
playwright_extractor = PlaywrightExtractor(
    config=playwright_settings,
    out_dir=demodir / "playwright",
    ensure_out_dir=True,
)


@alimiter
async def rate_limited_playwright_extractor(*args, **kwargs):
    return await playwright_extractor(*args, **kwargs)


# %% [markdown]
# ## Run extractions

# %%
# await rate_limited_postlight_extractor(source)
synchronize(rate_limited_postlight_extractor, source)
# NOTE: requires captcha / bot detection

# %%
# await rate_limited_chrome_extractor(source)
synchronize(rate_limited_chrome_extractor, source)

# %%
# await rate_limited_singlefile_extractor(source)
synchronize(rate_limited_singlefile_extractor, source)

# %%
# await rate_limited_playwright_extractor(source)
synchronize(rate_limited_playwright_extractor, source)

# %%
