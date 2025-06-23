# %%
import json
import logging
import os
from pathlib import Path
import shutil
import typing as t
from urllib.parse import quote, unquote, urlparse

# %%
from docling.document_converter import DocumentConverter  # NOQA: E402
from IPython.core.getipython import get_ipython  # NOQA: E402
from IPython.core.interactiveshell import InteractiveShell  # NOQA: E402
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

# %%
# source = "https://arxiv.org/pdf/2408.09869"  # PDF path or URL
# source = "https://arxiv.org/html/2408.09869v3"
source = "https://reinforcedknowledge.com/transformers-attention-is-all-you-need/"
converter = DocumentConverter()
result = converter.convert(source)

print(result.document.export_to_markdown())  # output: "### Docling Technical Report[...]"
