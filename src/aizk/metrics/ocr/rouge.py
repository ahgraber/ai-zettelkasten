"""ROUGE is a family of metrics that evaluate the overlap in ngrams between two texts.

ROUGE-n measures the overlap of n-grams between the reference and the generated text (the number of n consecutive words that appear in both).

- recall: count(n-grams) that appear in both / count(n-grams) in reference
- precision: count(n-grams) that appear in both / number of n-grams in generated
- F1: harmonic mean of precision and recall

ROUGE-L measures the longest common subsequence (LCS) between the reference and generated text, capturing sentence-level structure similarity.
Note that ROUGE-L does not require consecutive matches, only in-sequence matches.
"""

try:
    from rouge_score import rouge_scorer

    _IMPORT_ERROR: ImportError | None = None
except ImportError as _e:
    rouge_scorer = None  # type: ignore[assignment]
    _IMPORT_ERROR = _e

_INSTALL_HINT = "rouge-score is required for OCR ROUGE scoring. Install with: `uv sync --extra eval`"

_R3SCORER = None
_RLSCORER = None


def _require() -> None:
    if _IMPORT_ERROR is not None:
        raise ModuleNotFoundError(_INSTALL_HINT) from _IMPORT_ERROR


def rouge_3_score(ref_text: str, ocr_text: str) -> float:
    """Compute ROUGE-3 F1 score between reference and OCR text."""
    _require()
    global _R3SCORER
    if _R3SCORER is None:
        _R3SCORER = rouge_scorer.RougeScorer(["rouge3"], use_stemmer=False)
    score = _R3SCORER.score(ref_text, ocr_text)["rouge3"].fmeasure  # in [0,1]
    return score


def rouge_l_score(ref_text: str, ocr_text: str) -> float:
    """Compute ROUGE-L F1 score between reference and OCR text."""
    _require()
    global _RLSCORER
    if _RLSCORER is None:
        _RLSCORER = rouge_scorer.RougeScorer(["rougeL"], use_stemmer=False)
    score = _RLSCORER.score(ref_text, ocr_text)["rougeL"].fmeasure  # in [0,1]
    return score
