from pydantic import BaseModel
from sequence_align.pairwise import alignment_score, hirschberg

GAP_TOKEN = "▓"  # NOQA: S105 # use a rare unicode char as gap token


class TransitionCosts(BaseModel):
    match_score: float = 1.0
    mismatch_score: float = -1.0
    indel_score: float = -1.0


default_costs = TransitionCosts()


def sequence_alignment_score(ref_text: str, ocr_text: str, costs: TransitionCosts = default_costs) -> float:
    """Example usage of alignment_score with Hirschberg alignment."""
    ref_aligned, ocr_aligned = hirschberg(
        ref_text,
        ocr_text,
        gap=GAP_TOKEN,
        **costs.model_dump(),
    )
    score = alignment_score(
        ref_aligned,
        ocr_aligned,
        gap=GAP_TOKEN,
        **costs.model_dump(),
    )
    return score


### Ref: https://github.com/allenai/olmocr/blob/main/scripts/eval/dolma_refine/metrics.py
# insert, delete, match, subst = 0.0, 0.0, 0.0, 0.0
# for ref_tok, ocr_tok in zip(ref_aligned, ocr_aligned):
#     if ref_tok == GAP_TOKEN:
#         insert += 1
#     elif ocr_tok == GAP_TOKEN:
#         delete += 1
#     elif ref_tok == ocr_tok:
#         match += 1
#     else:
#         subst += 1

# if total := insert + delete + match + subst:
#     return match / total
# return 0.0
