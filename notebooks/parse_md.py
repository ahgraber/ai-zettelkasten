# %%
import asyncio
import logging
from pathlib import Path
import re
import urllib.parse as urlparse

import aiohttp
from bs4 import BeautifulSoup

import pandas as pd

from aizk.utilities import basic_log_config
from aizk.utilities.parse import (
    URL_REGEX,
    clean_link_title,
    clean_url,
    extract_md_url,
    find_all_urls,
)

basic_log_config()
logger = logging.getLogger(__name__)

# %%
data_dir = Path(__file__).parents[1] / "data"
treadmill = data_dir / "source" / "treadmill"


# %%
def clean_textblob(text: str) -> str:
    """Clean raw input for more reliable url extraction."""
    # remove newlines and multiple spaces
    text = " ".join(text.split())
    # remove xml tags with optional paren wrap (<xml>...</xml>)
    text = re.sub(r"<[^>]+?>|<\/[^>]+?>\)?", "", text)
    return text


# %%
dfs = []
for file in sorted((treadmill).rglob("*.md")):
    with file.open("r") as f:
        text = f.read()

    dfs.append(
        pd.DataFrame(
            {
                "title": clean_link_title(title),
                "url": clean_url(url),
            }
            for title, url in extract_md_url(clean_textblob(text))
        )
    )

df = pd.concat(dfs, ignore_index=True)
df = df.groupby("url").head(1).reset_index(drop=True)  # drop duplicate links

df.to_csv(treadmill / "treadmill_2024.csv", index=False)


# # %%
# async def get_webpage_title(url):
#     try:
#         # Create an async HTTP session
#         # Send an async GET request to the webpage
#         async with (
#             aiohttp.ClientSession() as session,
#             session.get(url) as response,
#         ):
#             # Check for successful response
#             response.raise_for_status()

#             # Read the HTML content
#             html = await response.text()

#             # Parse the HTML
#             soup = BeautifulSoup(html, "html.parser")

#             # Extract the title
#             title = soup.title.string if soup.title else None

#             return url, title

#     except Exception as e:
#         return f"Error fetching {url}: {e}"


# import nest_asyncio  # NOQA: E402

# nest_asyncio.apply()

# titles = asyncio.run(asyncio.gather(*[get_webpage_title(url) for url in df["url"]]))


# # %%
# df = (
#     df.set_index("url")
#     .join(pd.DataFrame([{"url": t[0], "get_title": t[1]} for t in titles if len(t) == 2]).set_index("url"))
#     .reset_index()
# )

# df["title"] = df["get_title"].fillna(df["title"]).apply(lambda title: " ".join(title.split()))
# df = df.drop(columns=["get_title"])
# df.to_csv(treadmill / "treadmill_2024.csv", index=False)

# %%
len(df[df["url"].str.contains("arxiv.org")])
len(df[df["url"].str.contains("github.com")])
len(df[df["url"].str.contains("news.ycombinator.com")])

# %%
social = ["linkedin", "x.com", "twitter.com"]
len(df[df["url"].str.contains("|".join(social), na=False)])

# %%
medium = ["medium.com", "towardsdatascience", "archive.is", "substack"]
len(df[df["url"].str.contains("|".join(medium), na=False)])

# %%
