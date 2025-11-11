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

from pydantic import BaseModel, Field, ValidationError

# %%
# Add the src directory to the path so we can import treadmill
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aizk.arxiv import AsyncArxivClient, get_arxiv_paper_metadata
from aizk.utilities.file_utils import to_valid_fname
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
# Set up logging
logging.basicConfig(level=logging.INFO)

treadmill_logger = logging.getLogger("treadmill")
treadmill_logger.setLevel(logging.DEBUG)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

datadir = Path("./data/reorder_experiment/")
datadir.mkdir(parents=True, exist_ok=True)

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
    if not htmlfile.exists():
        print("Saving html...")
        html = await client._get_paper_content(arxiv_html_url(id_))
        with open(htmlfile, "w", encoding="utf-8") as f:
            f.write(html)
        time.sleep(3)

    pdffile = savedir / f"{title}.pdf"
    if not pdffile.exists():
        print("Saving PDF...")
        pdf = await client._get_paper_pdf(arxiv_pdf_url(id_))
        with open(pdffile, "wb") as f:
            f.write(pdf)
        time.sleep(3)

print("Download complete.")


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
# process with docling, olmocr2
from dotenv import load_dotenv  # NOQA: E402
import httpx  # NOQA: E402

load_dotenv()

# set httpx logging to warnings and errors only
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.WARNING)
httpx_logger.propagate = False

# %%
async_client = httpx.AsyncClient(timeout=90)
base_url = os.environ.get("DOCLING_BASE_URL", "http://localhost:5001")
endpoint = base_url + "/v1/convert/file/async"

# https://docling-project.github.io/docling/reference/cli/
# https://github.com/docling-project/docling-serve/blob/main/docs/usage.md
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


async def process_tasks(tasks: list[DoclingTask | DoclingResult], save_file_name: str, interval: int = 3):
    """Process tasks until all are complete."""

    while tasks:
        logger.info("%d tasks remaining...", len(tasks))
        for i, task in enumerate(tasks):
            update = await update_status(task)
            if update.status == TaskStatus.FAILURE:
                logger.error(f"Task {update.task_id} failed.")
                tasks.remove(task)
                continue

            elif update.status == TaskStatus.SUCCESS:
                logger.info("Task %s for %s complete, saving result...", update.task_id, update.source_file)
                result = await fetch_result(update)
                source_file = Path(result.source_file)
                with open(source_file.parent / save_file_name, "w", encoding="utf-8") as f:
                    f.write(result.document.md_content)

                tasks.remove(task)
                continue

            else:
                tasks[i] = update

        await asyncio.sleep(interval)

    logger.info("Docling reference extraction complete.")


# %%
start = time.monotonic()
tasks = []
for file in datadir.glob("*/**/*.html"):
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

await process_tasks(tasks, save_file_name="docling_reference.md", interval=3)
stop = time.monotonic()
logger.info("Docling reference extraction took %.2f minutes.", (stop - start) / 60)

# %%
start = time.monotonic()
tasks = []
for file in datadir.glob("*/**/*.pdf"):
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

await process_tasks(tasks, save_file_name="docling_ocr.md", interval=3)
stop = time.monotonic()
logger.info("Docling OCR/PDF extraction took %.2f minutes.", (stop - start) / 60)
