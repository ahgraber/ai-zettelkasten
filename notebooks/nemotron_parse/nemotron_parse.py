"""Test nemotron-parse 1.1.

Given an image, NVIDIA Nemotron Parse v1.1 produces structured annotations, including formatted text, bounding-boxes and the corresponding semantic classes, ordered according to the document's reading flow. It overcomes the shortcomings of traditional OCR technologies that struggle with complex document layouts with structural variability, and helps transform unstructured documents into actionable and machine-usable representations.

Ref:

- https://arxiv.org/abs/2511.20478
- https://huggingface.co/nvidia/NVIDIA-Nemotron-Parse-v1.1

Requirements:
albumentations==1.4.1
einops==0.8.1
numpy==1.26.4
open_clip_torch
opencv_python==4.8.0.74
opencv_python_headless==4.9.0.80
Pillow==10.2.0
torch==2.1.2
torchvision==0.16.2
transformers==4.51.3
timm==1.0.22
torchmetrics==1.3.1
"""

# %%
from contextlib import contextmanager
from itertools import batched
import os
from pathlib import Path
import re
import tempfile

from PIL import Image, ImageDraw
from postprocessing import extract_classes_bboxes, postprocess_text, transform_bbox_to_original
import pypdfium2 as pdfium

import torch
from transformers import AutoConfig, AutoImageProcessor, AutoModel, AutoProcessor, AutoTokenizer, GenerationConfig


# %%
def get_torch_device():
    """Return the appropriate torch device (cuda, mps, or cpu)."""
    if torch.cuda.is_available():
        return torch.device("cuda:0")
    elif torch.backends.mps.is_available():
        return torch.device("mps")
    else:
        return torch.device("cpu")


def get_project_root() -> Path:
    """Return the repository/project root directory.

    Walks upward from this file looking for common root markers.
    Falls back to this file's parent directory if none are found.
    """
    this_file = Path(__file__).resolve()

    for candidate in this_file.parents:
        if (candidate / "pyproject.toml").is_file() or (candidate / ".git").exists() or (candidate / ".venv").exists():
            return candidate

    return this_file.parent


def pdf_to_image(pdf_path: Path, dpi=300):
    """Convert a PDF into a list of PIL Image objects.

    Args:
        pdf_path (str): Path to the input PDF file.
        dpi (int): Resolution (dots per inch) for the PNG render (default 300).

    Returns:
        list[PIL.Image.Image]: List of PIL Image objects for each page.
    """
    images = []
    scale = dpi / 72.0
    with pdfium.PdfDocument(str(pdf_path)) as doc:
        for page in doc:
            image = page.render(scale=scale).to_pil().convert("RGB")
            images.append(image)

    return images


# %%
# Load model and processor
model_path = "nvidia/NVIDIA-Nemotron-Parse-v1.1"  # Or use a local path
device = get_torch_device()  # "cuda:0"
if not torch.cuda.is_available():
    raise ValueError("This model requires a CUDA-capable GPU.")

model = AutoModel.from_pretrained(model_path, trust_remote_code=True, torch_dtype=torch.bfloat16).to(device).eval()
tokenizer = AutoTokenizer.from_pretrained(model_path)
processor = AutoProcessor.from_pretrained(model_path, trust_remote_code=True)

task_prompt = "</s><s><predict_bbox><predict_classes><output_markdown>"

generation_config = GenerationConfig.from_pretrained(model_path, trust_remote_code=True)

# Nemotron Parse v1.1 can return partially-populated `past_key_values` when
# `use_cache=True`, which breaks Hugging Face generation internals.
# Disabling cache avoids `AttributeError: 'NoneType' object has no attribute 'shape'`.
generation_config.use_cache = False

# %%
# convert pdf to png images
project_root = get_project_root()
pdf = project_root / "data/pdf/2508.14025v1.pdf"
images = pdf_to_image(pdf)

# %%
# Process images
decoded_texts = []
for batch in batched(images, n=4):
    text_input = [task_prompt] * len(batch)
    inputs = processor(images=list(batch), text=text_input, return_tensors="pt").to(device)
    prompt_ids = processor.tokenizer.encode(task_prompt, return_tensors="pt", add_special_tokens=False).cuda()

    outputs = model.generate(**inputs, generation_config=generation_config)
    decoded_texts.extend(processor.batch_decode(outputs, skip_special_tokens=True))

# %%
# Extract classes, bounding boxes and texts
generated_text = decoded_texts[0]
image = images[0]

classes, bboxes, texts = extract_classes_bboxes(generated_text)
bboxes = [transform_bbox_to_original(bbox, image.width, image.height) for bbox in bboxes]

# %%
classes, bboxes, texts = [], [], []
for image, page in zip(images, decoded_texts):
    cls, bb, txt = extract_classes_bboxes(page)
    classes.extend(cls)
    bboxes.extend([transform_bbox_to_original(bbox, image.width, image.height) for bbox in bb])
    texts.extend(txt)

# %%
# Specify output formats for postprocessing
table_format = "markdown"  # latex | HTML | markdown
text_format = "markdown"  # markdown | plain
blank_text_in_figures = True  # remove text inside 'Picture' class
texts = [
    postprocess_text(
        text,
        cls=cls,
        table_format=table_format,
        text_format=text_format,
        blank_text_in_figures=blank_text_in_figures,
    )
    for text, cls in zip(texts, classes)
]

for cl, txt in zip(classes, texts):
    print(cl, ": ", txt)


# %%
