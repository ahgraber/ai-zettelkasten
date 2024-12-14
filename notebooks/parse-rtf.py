# %%
import asyncio
import logging
from pathlib import Path
import re
import urllib.parse as urlparse

import aiohttp
from bs4 import BeautifulSoup
import chardet
import pypandoc

import pandas as pd

logging.basicConfig()
logger = logging.getLogger(__name__)

# %%
data_dir = Path(__file__).parents[1] / "data"
treadmill = data_dir / "treadmill"


# %%
def decode_text(file: Path) -> str:
    """Decode file with unknown encoding."""
    with file.open("rb") as f:
        blob = f.read()
        encoding = chardet.detect(blob)
        logger.info(encoding)
        enc = encoding["encoding"]

        return blob.decode(enc)


def clean_textblob(text: str) -> str:
    """Clean raw input for more reliable url extraction."""
    # remove newlines and multiple spaces
    text = " ".join(text.split())
    # remove xml tags with optional paren wrap (<xml>...</xml>)
    text = re.sub(r"<[^>]+?>|<\/[^>]+?>\)?", "", text)
    return text


# https://gist.github.com/dperini/729294
dperini_regex = (
    r"(?:http|ftp)s?://"  # http:// or https://
    r"(?![-_])(?:[-\w\u00a1-\uffff]{0,63}[^-_]\.)+"  # domain...
    r"(?:[a-z\u00a1-\uffff]{2,}\.?)"  # tld
    r"(?:[/?#]\S*)?"  # path
    # r"(?:[/?#]\S+?)?"  # path  uses +? to match as few as possible
)


def extract_url(text: str) -> list[str]:
    """Identify urls in text."""
    pattern = re.compile(dperini_regex, re.IGNORECASE)
    matches = re.findall(pattern, text)
    return matches


def extract_md_url(text: str) -> list[tuple[str, str]]:
    """Identify markdown-style urls (i.e., [title](url) )."""
    pattern = re.compile(
        r"(?:\[|\\\[)"  # initial '['
        r"([\s\S]*?)"  # title text
        r"(?:(?:\]|\\\])\()"  # middle ']('
        f"({dperini_regex})"  # url
        r"(?:\))",  # final ')'
        re.IGNORECASE,
    )
    # Find all matches
    matches = re.findall(pattern, text)

    # # clean multispaces / newlines
    matches = [(" ".join(title.split()), url) for title, url in matches]

    return matches


def clean_title(title: str) -> str:
    """Clean titles.

    Some titles still need cleaning after parsing:
    "There's An AI: The Best AI Tools Directory\\]([https://theresanai.com/" --> "There's An AI: The Best AI Tools Directory"
    "\\[2407.20516\\] Machine Unlearning in Generative AI: A Survey\\]([https://arxiv.org/abs/2407.20516" --> "[2407.20516] Machine Unlearning in Generative AI: A Survey
    """
    # replace extra escapes
    title = title.replace("\\", "")
    # split on possible markdown-url divider ']('
    title = title.split("](")[0]

    return title


def safelink_to_url(url: str) -> str:
    safelinks_str = "https://nam11.safelinks.protection.outlook.com"  # typos:disable
    if safelinks_str not in url:
        return url
    else:
        # Try unquote first (for general URL decoding)
        try:
            decoded = urlparse.unquote(url)
        except ValueError:
            # If unquote fails, try unquote_plus (for '+' encoding)
            decoded = urlparse.unquote_plus(url)

        pattern = re.compile(f"{safelinks_str}\\/\\?url=(.*?)&data=")
        matches = re.findall(pattern, decoded)

        if matches:
            return matches[0]
        else:
            raise ValueError(f"Could not find safelinks url in {decoded}")


def emergentmind_to_arxiv(url: str) -> str:
    """Convert emergentmind links to arxiv.org."""
    pattern = re.compile(r"(?:emergentmind.com/papers/)(\d+\.\d+)", re.IGNORECASE)
    if matches := re.findall(pattern, url):
        return f"https://arxiv.org/abs/{matches[0]}"
    else:
        return url


def standardize_arxiv(url: str) -> str:
    """Point to standard arxiv abstract pages."""
    pattern = re.compile(r"(?:arxiv.org/[a-z]+?/)(\d+\.\d+)", re.IGNORECASE)
    if matches := re.findall(pattern, url):
        return f"https://arxiv.org/abs/{matches[0]}"
    else:
        return url


def clean_url(url: str) -> str:
    """Clean url after identification."""

    # sometimes urls have weird markdown-like artifacts
    # "...)[–](...,    ...)[—](...,    ...)['](...,    ...)['](...,    ...)[\\](...,    ...)[�](..."
    # _split = re.split(r"\)\[[^\w\d]+?\]\(", url, flags=re.IGNORECASE)
    _split = re.split(r"\)\[[^\]\(]+?\]\(", url, flags=re.IGNORECASE)
    if _split:
        url = _split[0]

    url = safelink_to_url(url)
    url = emergentmind_to_arxiv(url)
    url = standardize_arxiv(url)
    return url


# %%
# read in rtf files and convert to markdown
for file in (treadmill / "rtf").rglob("*.rtf"):
    md = pypandoc.convert_text(
        decode_text(file),  # pandoc expects utf-8 encoded files
        "gfm",  # github format markdown
        format="rtf",  # text format, since convert_text can't infer from filename
    )

    outfile = file.parent.stem if ".rtfd" in str(file) else file.stem
    with (treadmill / "md" / (outfile + ".md")).open("w") as out:
        out.write(md)


# %%
dfs = []
for file in (treadmill / "md").glob("*.md"):
    with file.open("r") as f:
        text = f.read()

    dfs.append(
        pd.DataFrame(
            {
                "title": clean_title(title),
                "url": clean_url(url),
            }
            for title, url in extract_md_url(clean_textblob(text))
        )
    )

df = pd.concat(dfs, ignore_index=True)
df = df.groupby("url").head(1).reset_index(drop=True)  # drop duplicate links
# df.to_csv(treadmill / "links.csv", index=False)  # TODO: intelligent overwrite?


# %%
async def get_webpage_title(url):
    try:
        # Create an async HTTP session
        # Send an async GET request to the webpage
        async with (
            aiohttp.ClientSession() as session,
            session.get(url) as response,
        ):
            # Check for successful response
            response.raise_for_status()

            # Read the HTML content
            html = await response.text()

            # Parse the HTML
            soup = BeautifulSoup(html, "html.parser")

            # Extract the title
            title = soup.title.string if soup.title else None

            return url, title

    except Exception as e:
        return f"Error fetching {url}: {e}"


titles = asyncio.run(asyncio.gather(*[get_webpage_title(url) for url in df["url"]]))

# %%
df = (
    df.set_index("url")
    .join(pd.DataFrame([{"url": t[0], "get_title": t[1]} for t in titles if len(t) == 2]).set_index("url"))
    .reset_index()
)

df["title"] = df["get_title"].fillna(df["title"]).apply(lambda title: " ".join(title.split()))
df = df.drop(columns=["get_title"])
df.to_csv(treadmill / "links.csv", index=False)

# %%
