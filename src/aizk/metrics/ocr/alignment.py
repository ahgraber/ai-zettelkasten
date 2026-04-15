from pydantic import BaseModel

try:
    from sequence_align.pairwise import alignment_score, hirschberg

    _IMPORT_ERROR: ImportError | None = None
except ImportError as _e:
    alignment_score = None  # type: ignore[assignment]
    hirschberg = None  # type: ignore[assignment]
    _IMPORT_ERROR = _e

_INSTALL_HINT = "sequence-align is required for sequence alignment scoring. Install with: `uv sync --extra eval`"


def _require() -> None:
    if _IMPORT_ERROR is not None:
        raise ModuleNotFoundError(_INSTALL_HINT) from _IMPORT_ERROR


class TransitionCosts(BaseModel):
    match_score: float = 1.0
    mismatch_score: float = -1.0
    indel_score: float = -1.0


DEFAULT_COSTS = TransitionCosts()


def sequence_alignment_score(
    ref_text: str,
    ocr_text: str,
    costs: TransitionCosts = DEFAULT_COSTS,
) -> float:
    """Example usage of alignment_score with Hirschberg alignment."""
    _require()
    # use a rare unicode char as gap token
    gap_tokens = ["␣", "▓", "■", "▢", "▣", "▤", "▥", "▦"]  # NOQA: S105

    ref_aligned, ocr_aligned, gap_token = None, None, None

    for gap in gap_tokens:
        try:
            ref_aligned, ocr_aligned = hirschberg(
                ref_text,
                ocr_text,
                gap=gap,
                **costs.model_dump(),
            )
            gap_token = gap
            break
        except ValueError as e:
            message = str(e)
            if "Gap entry" in message and gap in message:
                continue
            raise e

    if ref_aligned is None or ocr_aligned is None or gap_token is None:
        raise ValueError(
            "All tested gap tokens exist in document. Sanitize document before alignment.",
        )

    score = alignment_score(
        ref_aligned,
        ocr_aligned,
        gap=gap_token,
        **costs.model_dump(),
    )
    return score


### Ref: https://github.com/allenai/olmocr/blob/main/scripts/eval/dolma_refine/metrics.py
# insert, delete, match, subst = 0.0, 0.0, 0.0, 0.0
# for ref_tok, ocr_tok in zip(ref_aligned, ocr_aligned):
#     if ref_tok == gap_used:
#         insert += 1
#     elif ocr_tok == gap_used:
#         delete += 1
#     elif ref_tok == ocr_tok:
#         match += 1
#     else:
#         subst += 1

# if total := insert + delete + match + subst:
#     return match / total
# return 0.0
