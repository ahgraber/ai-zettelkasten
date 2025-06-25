# %%
import json
import logging
import os
from pathlib import Path
import typing as t
from uuid import UUID, uuid4

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
    GitHubExtractor,
    PlaywrightExtractor,
    PlaywrightSettings,
    PostlightExtractor,
    PostlightSettings,
    SingleFileExtractor,
    SingleFileSettings,
    StaticFileExtractor,
)
from aizk.extractors.chrome import detect_playwright_chromium
from aizk.utilities import SlidingWindowRateLimiter, basic_log_config, get_repo_path

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
alimiter = SlidingWindowRateLimiter(5, 7)

# %%
sources = [
    Source(uuid=uuid4(), url="https://aimlbling-about.ninerealmlabs.com/blog/for-some-definition-of-open/"),
    Source(uuid=uuid4(), url="https://github.com/ahgraber/homelab-gitops-k3s/tree/main"),
    Source(uuid=uuid4(), url="https://arxiv.org/abs/2501.00656"),
]

# %% [markdown]
# ## ArXiv

# %%
arxive_settings = ArxivSettings()
arxiv_extractor = ArxivExtractor(
    config=arxive_settings,
    data_dir=demodir / "arxiv",
    ensure_data_dir=True,
)


@alimiter
async def rate_limited_arxiv_extractor(*args, **kwargs):
    return await arxiv_extractor(*args, **kwargs)


# %% [markdown]
# ## gitingest

# %%
github_extractor = GitHubExtractor(
    data_dir=demodir / "gitingest",
    ensure_data_dir=True,
)


@alimiter
async def rate_limited_github_extractor(*args, **kwargs):
    return await github_extractor(*args, **kwargs)


# %% [markdown]
# ## Postlight-parser

# %%
postlight_settings = PostlightSettings()
postlight_extractor = PostlightExtractor(
    config=postlight_settings,
    data_dir=demodir / "postlight-parser",
    ensure_data_dir=True,
)


@alimiter
async def rate_limited_postlight_extractor(*args, **kwargs):
    return await postlight_extractor(*args, **kwargs)


# %% [markdown]
# ## SingleFile

# %%
chrome_settings = ChromeSettings(binary=str(detect_playwright_chromium()))
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
    data_dir=demodir / "playwright",
    ensure_data_dir=True,
)


@alimiter
async def rate_limited_playwright_extractor(*args, **kwargs):
    return await playwright_extractor(*args, **kwargs)


# %% [markdown]
# ## Run extractions

# %%
source = sources[0]
await rate_limited_postlight_extractor(source)
# NOTE: requires captcha / bot detection

# %%
# await rate_limited_chrome_extractor(source)

# %%
await rate_limited_singlefile_extractor(source)

# %%
await rate_limited_playwright_extractor(source)

# %%
