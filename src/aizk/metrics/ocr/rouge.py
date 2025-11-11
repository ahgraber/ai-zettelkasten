"""ROUGE is a family of metrics that evaluate the overlap in ngrams between two texts.

ROUGE-n measures the overlap of n-grams between the reference and the generated text (the number of n consecutive words that appear in both).

- recall: count(n-grams) that appear in both / count(n-grams) in reference
- precision: count(n-grams) that appear in both / number of n-grams in generated
- F1: harmonic mean of precision and recall

ROUGE-L measures the longest common subsequence (LCS) between the reference and generated text, capturing sentence-level structure similarity.
Note that ROUGE-L does not require consecutive matches, only in-sequence matches.
"""

from rouge_score import rouge_scorer

RLSCORER = rouge_scorer.RougeScorer(["rougeL"], use_stemmer=False)

R3SCORER = rouge_scorer.RougeScorer(["rouge3"], use_stemmer=False)


def rouge_3_score(ref_text: str, ocr_text: str) -> float:
    """Compute ROUGE-3 F1 score between reference and OCR text."""
    score = R3SCORER.score(ref_text, ocr_text)["rouge3"].fmeasure  # in [0,1]
    return score


def rouge_l_score(ref_text: str, ocr_text: str) -> float:
    """Compute ROUGE-L F1 score between reference and OCR text."""
    score = RLSCORER.score(ref_text, ocr_text)["rougeL"].fmeasure  # in [0,1]
    return score
