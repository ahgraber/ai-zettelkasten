"""
Investigates whether an LLM can reconstruct the correct order of an OCR-processed document.

Given arxiv.org papers that are available both as PDF and HTML, we can compare the text extracted from the HTML version (which is structured and correct) with the text from the OCR-processed version (which may be jumbled).

For papers where the OCR text is jumbled, we test whether an LLM can reconstruct the proper text order.

We treat the HTML-extract as ground truth and compare the OCR and OCR+LLM-reordered text against it using ROUGE-L, kendall's tau, and "dolma" similarity metrics.

# References

- https://github.com/allenai/olmocr
- https://github.com/allenai/olmocr/blob/main/scripts/eval/dolma_refine/metrics.py
- https://edist.readthedocs.io/en/latest/ and https://gitlab.ub.uni-bielefeld.de/bpaassen/python-edit-distances

"""

# %%
import asyncio
from dataclasses import dataclass
import enum
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Optional

from dotenv import load_dotenv
import httpx  # NOQA: E402
from pydantic import BaseModel, Field, ValidationError
from setproctitle import setproctitle
from tqdm.auto import tqdm

# %%
# Add the src directory to the path so we can import treadmill
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aizk.utilities.arxiv import AsyncArxivClient, get_arxiv_paper_metadata
from aizk.utilities.file_utils import to_valid_fname
from aizk.utilities.limiters import SlidingWindowRateLimiter
from aizk.utilities.url_utils import (
    arxiv_abs_url,
    arxiv_html_url,
    arxiv_pdf_url,
    get_arxiv_id,
    is_arxiv_url,
    standardize_arxiv,
    to_arxiv_export_url,
)

# %%
# define python process name
setproctitle(Path(__file__).stem)

# Set up logging
logging.basicConfig(level=logging.INFO)

treadmill_logger = logging.getLogger("treadmill")
treadmill_logger.setLevel(logging.DEBUG)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# set httpx logging to warnings and errors only
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.WARNING)
httpx_logger.propagate = False

datadir = Path("./data/reorder_experiment/")
datadir.mkdir(parents=True, exist_ok=True)

load_dotenv()

# %%
ids = [
    "1706.03762",  # Attn is all you need
    "2405.09673",  # Lora learns/forgets less
    "2402.17764",  # 1-bit llms
    "2404.16130",  # Graphrag
    "2404.19756",  # KAN
    "2405.07987",  # platonic representation
    "2408.09869",  # docling
    "2409.02060",  # OLMoE
    "2410.14632",  # Diverging preferences
    "2411.15124",  # Tulu 3
    "2501.00663",  # TITANS
    "2501.12948",  # Deepseek R-1
    "2501.12370",  # scaling MoE
    "2502.16982",  # MUON
    "2502.09992",  # LL diffusion M
    "2503.06639",  # RLVR
    "2503.14499",  # METR long task
    "2504.12285",  # bitnet
    "2504.20879",  # leaderboard illusion
    "2504.12501",  # RLHF
    "2505.00026",  # LLM theory of mind
    "2506.13109",  # Agent ICL
    "2506.21521",  # potemkin understanding
    "2507.19457",  # GEPA
    "2508.14025",  # Ask good questions
    "2508.15734",  # google environmental impact
    "2509.04259",  # RL's Razor
]

# %%
# scrape from arxiv
# save to ./data/reorder_experiment/{arxiv_id}/

client = AsyncArxivClient()
paper_metadata = await client.get_paper_metadata(ids)
if len(ids) != len(paper_metadata):
    logger.error("Some paper metadata could not be retrieved.")

# %%
for id_, metadata in zip(ids, paper_metadata):
    if id_ not in metadata.get("id", ""):
        logger.warning(f"Skipping {id_}: metadata::ID mismatch: {metadata.get('id')}")
        continue

    savedir = datadir / id_
    savedir.mkdir(parents=True, exist_ok=True)
    print(f"Arxiv ID: {id_}")
    print(f"Title: {metadata['title']}")
    title = to_valid_fname(metadata["title"])

    htmlfile = savedir / f"{title}.html"
    if htmlfile.exists():
        logger.info("HTML file for %s already exists, skipping download.", id_)
    else:
        logger.debug("Saving html for %s...", id_)
        html = await client._get_paper_content(arxiv_html_url(id_))
        with open(htmlfile, "w", encoding="utf-8") as f:
            f.write(html)
        time.sleep(3)

    pdffile = savedir / f"{title}.pdf"
    if pdffile.exists():
        logger.info("PDF file for %s already exists, skipping download.", id_)
    else:
        logger.debug("Saving PDF for %s...", id_)
        pdf = await client._get_paper_pdf(arxiv_pdf_url(id_))
        with open(pdffile, "wb") as f:
            f.write(pdf)
        time.sleep(3)

logger.info("Download complete.")


# %%
def extract_article_content(html_content: str) -> str:
    import bs4

    soup = bs4.BeautifulSoup(html_content, "html.parser")
    article = soup.find("article", class_="ltx_document")
    if not article:
        raise ValueError("No article found in HTML content.")
    return str(article)


# %%
# process HTML to get reference text
from io import BytesIO  # NOQA: E402

from markitdown import (  # NOQA: E402
    MarkItDown,
    StreamInfo,
)

mid = MarkItDown()

for html in datadir.glob("*/**/*.html"):
    with open(html, "r", encoding="utf-8") as f:
        html_content = f.read()

    article = extract_article_content(html_content)
    article_html_bytes = article.encode("utf-8")
    stream_info = StreamInfo(extension=".html")
    result = mid.convert(BytesIO(article_html_bytes), stream_info=stream_info)
    md = result.text_content

    article_file = html.parent / "markitdown_reference.md"
    with open(article_file, "w", encoding="utf-8") as f:
        f.write(md)

# %%
async_client = httpx.AsyncClient(timeout=90)
base_url = os.environ.get("DOCLING_BASE_URL", "http://localhost:5001")
endpoint = base_url + "/v1/convert/file/async"
overwrite: bool = True

# https://docling-project.github.io/docling/reference/cli/
# https://github.com/docling-project/docling-serve/blob/main/docs/usage.md
MARKDOWN_PAGE_BREAK_PLACEHOLDER = "\n\n---PAGE BREAK---\n\n"
BASE_DOCLING_PARAMS = {
    # "from_formats": ["html", "image", "pdf", "md"],
    "to_formats": ["md"],  # , "html", "doctags"],
    "image_export_mode": "placeholder",
    "do_ocr": True,
    "force_ocr": False,
    "ocr_engine": "auto",
    "ocr_lang": ["en"],
    "pdf_backend": "dlparse_v4",
    "table_mode": "accurate",
    "abort_on_error": False,
    "md_page_break_placeholder": MARKDOWN_PAGE_BREAK_PLACEHOLDER,
    "do_table_structure": True,
    "do_code_enrichment": False,
    "do_formula_enrichment": True,
    "do_picture_description": False,  # Not for alignment experiment
    # "pipeline": "vlm",
    # "vlm_pipeline_model": "granite_docling_vllm",
}


# %%
# https://github.com/docling-project/docling-serve/blob/main/docling_serve/datamodel/responses.py
class TaskStatus(str, enum.Enum):
    """Enumeration of possible task statuses.

    ref:
    - https://github.com/docling-project/docling-jobkit/blob/main/docling_jobkit/datamodel/task_meta.py
    - https://github.com/docling-project/docling/blob/main/docling/datamodel/base_models.py
    """

    SUCCESS = "success"
    PENDING = "pending"
    STARTED = "started"
    FAILURE = "failure"
    PARTIAL_SUCCESS = "partial_success"
    SKIPPED = "skipped"


class DoclingConversionContent(BaseModel):
    """Content from Docling conversion.

    ref: https://github.com/docling-project/docling-jobkit/blob/main/docling_jobkit/datamodel/result.py
    """

    filename: str
    md_content: Optional[str] = None
    json_content: Optional[str] = None
    html_content: Optional[str] = None
    text_content: Optional[str] = None
    doctags_content: Optional[str] = None


class DoclingTask(BaseModel):
    """Response model for tracking Docling conversion tasks.

    ref: https://github.com/docling-project/docling-serve/blob/main/docling_serve/datamodel/responses.py
    """

    task_id: str
    status: TaskStatus = Field(alias="task_status")
    source_file: Optional[str] = None


class DoclingResult(BaseModel):
    """Response model for Docling conversion result.

    ref: https://github.com/docling-project/docling-jobkit/blob/main/docling_jobkit/datamodel/result.py
    """

    status: TaskStatus
    source_file: Optional[str] = None
    document: DoclingConversionContent


# %%
async def update_status(task: DoclingTask) -> DoclingTask:
    """Update task status from Docling server."""
    response = await async_client.get(f"{base_url}/v1/status/poll/{task.task_id}")
    payload = response.json()

    if payload.get("detail") is not None:
        raise ValueError(f"Error fetching result for task {task.task_id}: {payload['detail']}")

    update = DoclingTask.model_validate(payload)
    update.source_file = task.source_file
    return update


async def fetch_result(task: DoclingTask) -> DoclingResult:
    """Fetch updated task status from Docling server."""
    response = await async_client.get(f"{base_url}/v1/result/{task.task_id}")
    payload = response.json()

    result = DoclingResult.model_validate(payload)
    result.source_file = task.source_file
    return result


async def process_docling_tasks(tasks: list[DoclingTask], save_file_name: str, interval: int = 3):
    """Process tasks until all are complete."""

    while tasks:
        logger.info("%d tasks remaining...", len(tasks))
        pending_tasks: list[DoclingTask] = []
        for task in tasks:
            update = await update_status(task)
            if update.status == TaskStatus.FAILURE:
                logger.error("Task %s failed.", update.task_id)
                continue

            if update.status == TaskStatus.SUCCESS:
                logger.info("Task %s for %s complete, saving result...", update.task_id, update.source_file)
                result = await fetch_result(update)
                source_file = Path(result.source_file)

                with open(source_file.parent / save_file_name, "w", encoding="utf-8") as f:
                    f.write(result.document.md_content)

                continue

            pending_tasks.append(update)

        tasks = pending_tasks
        if tasks:
            await asyncio.sleep(interval)

    logger.info("Docling reference extraction complete.")


# %%
start = time.monotonic()
tasks = []
for file in datadir.glob("*/**/*.html"):
    if not overwrite:
        mdfile = file.parent / "docling_reference.md"
        if mdfile.exists():
            logger.info("Docling reference file for %s exists, skipping.", file)
            continue

    with open(file, "r", encoding="utf-8") as f:
        html_content = f.read()

    article = extract_article_content(html_content)

    logger.info("Submitting %s to Docling...", file)
    files = {"files": (file.name, article.encode("utf-8"), "text/html")}
    response = await async_client.post(
        endpoint,
        files=files,
        data=BASE_DOCLING_PARAMS,
    )

    task_info = DoclingTask.model_validate(response.json())
    task_info.source_file = str(file)  # Track the original file path with the task
    tasks.append(task_info)

logger.info("Submitted %d, awaiting completion...", len(tasks))
time.sleep(10)

await process_docling_tasks(tasks, save_file_name="docling_reference.md", interval=3)
stop = time.monotonic()
logger.info("Docling reference extraction took %.2f minutes.", (stop - start) / 60)

# %%
start = time.monotonic()
tasks = []
for file in datadir.glob("*/**/*.pdf"):
    if not overwrite:
        mdfile = file.parent / "docling_reference.md"
        if mdfile.exists():
            logger.info("Docling reference file for %s exists, skipping.", file)
            continue

    logger.info("Submitting %s to Docling...", file)
    with open(file, "rb") as f:
        files = {"files": (file.name, f, "application/pdf")}
        response = await async_client.post(
            endpoint,
            files=files,
            data=BASE_DOCLING_PARAMS,
        )

    task_info = DoclingTask.model_validate(response.json())
    task_info.source_file = str(file)  # Track the original file path with the task
    tasks.append(task_info)

logger.info("Submitted %d, awaiting completion...", len(tasks))
time.sleep(10)

await process_docling_tasks(tasks, save_file_name="docling_ocr.md", interval=3)
stop = time.monotonic()
logger.info("Docling OCR/PDF extraction took %.2f minutes.", (stop - start) / 60)

# %%a
# run pdf-ocr-reorder through gpt-5 family, gemini-2.5-flash-lite to try reordering
# %% [markdown]
# ## Evaluation
# We use ROUGE-L, Kendall's Tau, and Dolma similarity to compare the texts, and report character error rate (CER) and word error rate (WER) for the OCR text for completeness
#
# - ROUGE-L F1: captures how much of the content overlaps and in-order subsequences. since ROUGE-L focuses on longest common subsequences, we may need to break by document structure (e.g., headings) to get meaningful results
# - Kendall's Tau: measures rank correlation of token indices, indicating how well the order of elements in one sequence matches the order in the other
# - Dolma document_edit_similarity: examines how matched two texts are after optimal alignment (ignoring where mismatches occur)
# - Dolma paragraph_edit_similarity:


# %%
from jiwer import cer, wer  # NOQA: E402

import pandas as pd  # NOQA: E402

from aizk.metrics.ocr import kendall_tau_score, rouge_3_score, rouge_l_score, sequence_alignment_score  # NOQA: E402

# %%
ref_name = "docling_reference"
ocr_name = "docling_ocr"

savefile = datadir / "ocr_evaluation_results.csv"
if savefile.exists():
    df = pd.read_csv(savefile)
else:
    df = pd.DataFrame(
        columns=[
            "arxiv_id",
            "path",
            "reference",
            "comparison",
            "cer",
            "wer",
            "rouge-3",
            "rouge-l",
            "kendall-tau",
            "alignment",
        ]
    )
    df.to_csv(datadir / "ocr_evaluation_results.csv", index=False, header=True)

for dir in tqdm(sorted(datadir.iterdir())):  # NOQA: A001
    if not dir.is_dir():
        continue
    if dir.name.startswith("."):
        continue
    if str(dir) in df["path"].tolist():
        logger.debug(f"Skipping {dir.name}, already evaluated.")
        continue

    logger.info(f"Evaluating {dir.name}...")
    start = time.monotonic()
    # ref_file = dir / "markitdown_reference.md"
    ref_file = dir / f"{ref_name}.md"
    ocr_file = dir / f"{ocr_name}.md"

    with open(ref_file, "r", encoding="utf-8") as f:
        ref_text = f.read()

    with open(ocr_file, "r", encoding="utf-8") as f:
        ocr_text = f.read()

    # remove markdown page break placeholders
    ocr_text = ocr_text.replace(MARKDOWN_PAGE_BREAK_PLACEHOLDER, "")

    # lower is better
    logger.debug("Assessing conversion error rates...")
    cer_vals = cer(ref_text, ocr_text)
    wer_vals = wer(ref_text, ocr_text)

    # higher is better
    logger.debug("Assessing ROUGE scores...")
    r3 = rouge_3_score(ref_text, ocr_text)
    rl = rouge_l_score(ref_text, ocr_text)
    logger.debug("Assessing Kendall's Tau score...")
    kt = kendall_tau_score(ref_text, ocr_text)
    logger.debug("Assessing sequence alignment score...")
    align = sequence_alignment_score(ref_text, ocr_text)

    stop = time.monotonic()
    logger.info("Evaluation for %s (%d characters) took %.2f minutes.", dir.name, len(ref_text), (stop - start) / 60)

    logger.debug("Saving results to csv...")
    _df = pd.DataFrame(
        {
            "arxiv_id": dir.name,
            "path": str(dir),
            "reference": ref_name,
            "comparison": ocr_name,
            "cer": cer_vals,
            "wer": wer_vals,
            "rouge-3": r3,
            "rouge-l": rl,
            "kendall-tau": kt,
            "alignment": align,
        },
        index=[
            0,
        ],
    )
    _df.to_csv(datadir / "ocr_evaluation_results.csv", index=False, header=False, mode="a")

# %%
pd.read_csv(datadir / "ocr_evaluation_results.csv").set_index("arxiv_id")[
    ["rouge-3", "rouge-l", "kendall-tau", "alignment"]
].corr()

# %%
# TODO
# split by pages
# use LLM to reorder OCR text
# re-evaluate reordered text
# compare results


# %%
import mlflow  # NOQA: E402
from pydantic_ai import Agent  # NOQA: E402
from pydantic_ai.models.openrouter import OpenRouterModel  # NOQA: E402
from pydantic_ai.providers.openrouter import OpenRouterProvider  # NOQA: E402

# TODO: check if mlflow is running; raise exception if not
# `mlflow ui --backend-store-uri sqlite:///mlflow.db --port 5000`
mlflow.set_tracking_uri(os.environ.get("AIZK_MLFLOW_TRACKING_URI", "http://127.0.0.1:5000"))
mlflow.set_experiment("reorder_experiment")
mlflow.pydantic_ai.autolog()

REORDER_FILE_SUFFIX = "reorder_with_prior"


class PageReorderResult(BaseModel):
    """Structured response describing a reordered OCR page."""

    ordered_markdown: str = Field(
        ...,
        description="Markdown content for the page once the sentences are restored to a sensible reading order.",
    )
    reasoning: str = Field(
        ...,
        description="Short summary of the steps taken to repair the reading order.",
    )


reorder_instructions = """
You are cataloguing OCR'd research papers. Each input is a single page whose sentences, bullet markers, figures, and tables may be shuffled because of OCR errors.
Reconstruct a coherent reading order in Markdown. Preserve math delimiters, section headings, and code fences. Keep all substantive content, but do not fabricate new information.
Return content that satisfies the PageReorderResult schema and never include commentary outside the structured fields.""".strip()

reorder_agent = Agent(
    model=OpenRouterModel(
        "openai/gpt-5-mini",
        provider=OpenRouterProvider(api_key=os.environ["_OPENROUTER_API_KEY"]),
    ),
    output_type=PageReorderResult,
    instructions=reorder_instructions,
)

reorder_rate_limiter = SlidingWindowRateLimiter(
    max_requests=20,
    window_seconds=60,
)


async def reorder_page(
    agent: Agent[None, PageReorderResult],
    *,
    page_index: int,
    total_pages: int,
    page_text: str,
    prior_tail: Optional[str] = None,
) -> str:
    """Run the Pydantic AI agent on a single OCR page.

    Args:
        agent: Configured OCR reordering agent.
        arxiv_id: ArXiv identifier for logging context.
        page_index: Zero-based page index within the document.
        total_pages: Total number of pages in the document.
        page_text: Raw OCR text for the page.
        source_filename: Name of the source markdown file for provenance.

    Returns:
        Markdown text reordered into a coherent reading sequence.
    """

    if not page_text.strip():
        return ""

    prompt = f"""
You are repairing OCR output from an ArXiv paper.
The following text is from page {page_index + 1} of {total_pages}.
Restore the logical reading order while preserving headings, math, tables, and citations.
Use Markdown formatting and keep the wording faithful to the provided text."
""".strip()

    if prior_tail:
        prompt += f"""
The prior page ended with:
{prior_tail.strip()}
""".strip()

    prompt += f"""
Raw OCR page content:
{page_text.strip()}
""".strip()

    await reorder_rate_limiter.acquire()
    result = await agent.run(user_prompt=prompt)
    return result.output.ordered_markdown.strip()


def split_markdown_pages(document_text: str) -> list[str]:
    """Split markdown text into individual pages using the placeholder delimiter."""

    if not document_text:
        return []
    return [segment.strip("\n") for segment in document_text.split(MARKDOWN_PAGE_BREAK_PLACEHOLDER)]


def join_markdown_pages(pages: list[str]) -> str:
    """Recombine page segments into a single markdown document with placeholders."""

    if not pages:
        return ""
    normalized = [page.rstrip() for page in pages]
    combined = MARKDOWN_PAGE_BREAK_PLACEHOLDER.join(normalized)
    return combined.strip() + "\n"


# %%
overwrite = False
start = time.monotonic()
tasks = []
source_filename = "docling_ocr.md"
for source_file in tqdm(sorted(datadir.glob(f"*/**/{source_filename}"))):
    outfile = source_file.with_name(f"{source_file.stem}_{REORDER_FILE_SUFFIX}{source_file.suffix}")
    if not overwrite:  # NOQA: SIM102
        if outfile.exists():
            logger.info("Reordered Docling OCR file for %s exists, skipping.", source_file)
            continue

    raw_markdown = source_file.read_text(encoding="utf-8")
    pages = split_markdown_pages(raw_markdown)
    total_pages = len(pages)
    if total_pages == 0:
        logger.warning("%s: OCR file is empty, skipping LLM reorder.", source_file)
        continue

    page_tasks = []
    for idx, page in enumerate(pages):
        prior_tail_text = None
        if idx > 0:
            prior = pages[idx - 1].split("\n")[-5:]  # last 5 lines of prior page
            prior_tail = "\n".join(prior)
        task = reorder_page(
            reorder_agent,
            page_index=idx,
            total_pages=total_pages,
            page_text=page,
            prior_tail=prior_tail_text,
        )
        page_tasks.append(task)

    page_results = await asyncio.gather(*page_tasks, return_exceptions=True)  # type: ignore[misc]

    reordered_pages: list[str] = []
    for idx, (page_text, result) in enumerate(zip(pages, page_results)):
        if isinstance(result, ValidationError):  # pragma: no cover - defensive fallback
            logger.exception("%s: Validation error while reordering page %d", source_file, idx + 1)
            reordered_pages.append(page_text.strip())
            continue
        if isinstance(result, Exception):  # pragma: no cover - defensive fallback
            logger.exception("%s: Unexpected error while reordering page %d", source_file, idx + 1)
            reordered_pages.append(page_text.strip())
            continue
        assert isinstance(result, str)
        reordered_pages.append(result)

    combined_markdown = join_markdown_pages(reordered_pages)
    outfile.write_text(combined_markdown, encoding="utf-8")
    logger.info("Saved reordered OCR markdown to %s", outfile)

stop = time.monotonic()
logger.info("OCR reorder took %.2f minutes.", (stop - start) / 60)


# %%
ref_name = "docling_reference"
ocr_name = f"docling_ocr_{REORDER_FILE_SUFFIX}"

savefile = datadir / "ocr_reorder_results_prior.csv"
if savefile.exists():
    df = pd.read_csv(savefile)
else:
    df = pd.DataFrame(
        columns=[
            "arxiv_id",
            "path",
            "reference",
            "comparison",
            "cer",
            "wer",
            "rouge-3",
            "rouge-l",
            "kendall-tau",
            "alignment",
        ]
    )
    df.to_csv(datadir / "ocr_reorder_results_prior.csv", index=False, header=True)

for dir in tqdm(sorted(datadir.iterdir())):  # NOQA: A001
    if not dir.is_dir():
        continue
    if dir.name.startswith("."):
        continue
    if str(dir) in df["path"].tolist():
        logger.debug(f"Skipping {dir.name}, already evaluated.")
        continue

    logger.info(f"Evaluating {dir.name}...")
    start = time.monotonic()
    # ref_file = dir / "markitdown_reference.md"
    ref_file = dir / f"{ref_name}.md"
    ocr_file = dir / f"{ocr_name}.md"

    with open(ref_file, "r", encoding="utf-8") as f:
        ref_text = f.read()

    with open(ocr_file, "r", encoding="utf-8") as f:
        ocr_text = f.read()

    # remove markdown page break placeholders
    ocr_text = ocr_text.replace(MARKDOWN_PAGE_BREAK_PLACEHOLDER, "")

    # lower is better
    logger.debug("Assessing conversion error rates...")
    cer_vals = cer(ref_text, ocr_text)
    wer_vals = wer(ref_text, ocr_text)

    # higher is better
    logger.debug("Assessing ROUGE scores...")
    r3 = rouge_3_score(ref_text, ocr_text)
    rl = rouge_l_score(ref_text, ocr_text)
    logger.debug("Assessing Kendall's Tau score...")
    kt = kendall_tau_score(ref_text, ocr_text)
    logger.debug("Assessing sequence alignment score...")
    align = sequence_alignment_score(ref_text, ocr_text)

    stop = time.monotonic()
    logger.info("Evaluation for %s (%d characters) took %.2f minutes.", dir.name, len(ref_text), (stop - start) / 60)

    logger.debug("Saving results to csv...")
    _df = pd.DataFrame(
        {
            "arxiv_id": dir.name,
            "path": str(dir),
            "reference": ref_name,
            "comparison": ocr_name,
            "cer": cer_vals,
            "wer": wer_vals,
            "rouge-3": r3,
            "rouge-l": rl,
            "kendall-tau": kt,
            "alignment": align,
        },
        index=[
            0,
        ],
    )
    _df.to_csv(datadir / "ocr_reorder_results_prior.csv", index=False, header=False, mode="a")

# %%
pd.read_csv(datadir / "ocr_reorder_results_prior.csv").set_index("arxiv_id")[
    ["rouge-3", "rouge-l", "kendall-tau", "alignment"]
].describe()

# %%
pd.read_csv(datadir / "ocr_reorder_results_prior.csv").set_index("arxiv_id")[
    ["rouge-3", "rouge-l", "kendall-tau", "alignment"]
].corr()

# %% [markdown]
# ## Results
#
# Default Docling OCR results seem to be of fairly high quality already; using an LLM to reorder the text provides at best marginal improvement but more frequently degrades quality.
