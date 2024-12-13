# %%
import datetime
import hashlib

from docling.document_converter import DocumentConverter
from sqlmodel import Field, Session, SQLModel, create_engine

from ..datamodel.schema import *

# %%
converter = DocumentConverter()


# %%
def scrape_url(session: Session, url: str):
    """Scrape url."""
    try:
        src = SourceLink(url)
    except Exception:
        ...

    try:
        # Perform actual scraping
        content = converter.convert(str(src.url))

        # Update record status
        record = session.scalar(select(Source).where(Source.url == src.url))
        session.query(Source).filter(Source.url == src.url).update(values={})

        record.scrape_status = ScrapeStatus("COMPLETE")
        record.content_hash = hashlib.md5(content.encode()).hexdigest()  # NOQA: S324
        record.scraped_at = datetime.datetime.now(datetime.timezone.utc)

        session.commit()

    except Exception as e:
        # Handle scraping failure
        record = session.scalar(select(Source).where(Source.url == src.url))

        record.scrape_status = "ERROR"
        record.error_message = str(e)

        session.commit()
        raise

    return content


# %%
# %%
# source = "https://arxiv.org/pdf/2408.09869"  # PDF path or URL
# source = "https://arxiv.org/html/2408.09869v3"
source = "https://reinforcedknowledge.com/transformers-attention-is-all-you-need/"
result = converter.convert(source)

print(result.document.export_to_markdown())  # output: "### Docling Technical Report[...]"
