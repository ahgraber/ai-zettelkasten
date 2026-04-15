"""Claimify extraction orchestrator: Selection -> Disambiguation -> Decomposition."""

from __future__ import annotations

from _claimify.io import ensure_punkt_tab
from _claimify.models import Section, SentenceContext


def build_sentence_contexts(
    section: Section,
    section_idx: int,
    *,
    p: int,
    f: int,
) -> list[SentenceContext]:
    """Build per-sentence context windows for a section using NLTK sentence tokenization.

    Args:
        section: The section to tokenize.
        section_idx: Ordinal index of `section` within its parent document.
        p: Number of preceding sentences to include in the window.
        f: Number of following sentences to include in the window.
    """
    ensure_punkt_tab()
    from nltk.tokenize import sent_tokenize

    sentences = sent_tokenize(section.content)
    contexts: list[SentenceContext] = []
    for i, sentence in enumerate(sentences):
        preceding = " ".join(sentences[max(0, i - p) : i])
        following = " ".join(sentences[i + 1 : i + 1 + f])
        contexts.append(
            SentenceContext(
                sentence=sentence,
                preceding=preceding,
                following=following,
                excerpt=section.content,
                section_idx=section_idx,
                sentence_idx=i,
            )
        )
    return contexts
