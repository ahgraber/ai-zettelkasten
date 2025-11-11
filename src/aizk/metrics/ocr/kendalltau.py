"""Kendall's Tau (τ) measures how much two sequences agree on ordering, assuming corresponding items have been matched.

It is a rank-correlation metric that counts concordant vs discordant pairs between two ranked lists. Given two sequences of the same items (e.g. two orderings of document blocks or tokens), τ is defined as (concordant-discordant)/C(n,2). Equivalently, the Kendall distance is the number of adjacent swaps needed to transform one ordering into the other (the "bubble-sort" distance).

Ref:
- https://arxiv.org/abs/2405.15523v2
- https://link.springer.com/article/10.1007/s11042-025-20736-y
"""

from rapidfuzz import fuzz

from scipy.stats import kendalltau


def kt_tokenize(s: str):
    """Simple whitespace/punct split; keep only non-empty tokens."""
    import re

    return [t for t in re.findall(r"\w+|\S", s.lower()) if t.strip()]


def kt_token_alignment(ref_tokens, ocr_tokens, fuzzy_cutoff=90):
    """Greedy token alignment for kendall-tau calculation.

    1) For each ref token, find the first not-yet-used OCR token that matches
       (exact first, else fuzzy with RapidFuzz >= cutoff).
    2) Return two aligned index lists of equal length: positions in ref vs positions in OCR.
    """
    from collections import defaultdict

    used = set()
    idx_ref, idx_ocr = [], []

    positions = defaultdict(list)
    for i, tok in enumerate(ocr_tokens):
        positions[tok].append(i)

    for i, tok in enumerate(ref_tokens):
        # exact match first
        cand = next((j for j in positions.get(tok, []) if j not in used), None)
        if cand is None:
            # fuzzy search within a window: check neighbors around last used index (optional)
            best_j, best_score = None, 0
            for j, o_tok in enumerate(ocr_tokens):
                if j in used:
                    continue
                score = fuzz.QRatio(tok, o_tok)  # 0..100
                if score > best_score:
                    best_score, best_j = score, j
            cand = best_j if best_score >= fuzzy_cutoff else None
        if cand is not None:
            used.add(cand)
            idx_ref.append(i)
            idx_ocr.append(cand)
    return idx_ref, idx_ocr


def kendall_tau_score(ref_text: str, ocr_text: str) -> float:
    """Compute normalized Kendall-τ score between reference and OCR text."""
    ref_tokens = kt_tokenize(ref_text)
    ocr_tokens = kt_tokenize(ocr_text)

    idx_ref, idx_ocr = kt_token_alignment(ref_tokens, ocr_tokens)
    if len(idx_ref) >= 2:  # kendalltau needs at least 2 items
        tau, _ = kendalltau(idx_ref, idx_ocr)  # [-1, 1]
        tau_norm = (tau + 1) / 2 if tau is not None else 0.0
    else:
        tau_norm = 0.0 if ref_tokens and ocr_tokens else 1.0  # degenerate case handling

    return tau_norm
