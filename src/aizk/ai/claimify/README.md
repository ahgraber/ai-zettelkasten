# Claimify

Claim extraction is the basis for _faithfulness_ metrics by which we can determine whether what the LLM has synthesized is supported by substantiating content or evidence. Claim extraction is also difficult and general LLMs do poorly on this task, in part due to poor specification on what makes a valid claim. Microsoft Research has published their research project `Claimify` that significantly improves the validity of claim extraction by following 5 principles:

1. The claims should capture all verifiable content in the source text and exclude unverifiable content
2. Each claim should be entailed (i.e., fully supported) by the source text.
3. Each claim should be understandable on its own, without additional context.
4. Each claim should minimize the risk of excluding critical context.
5. The system should flag cases where ambiguity cannot be resolved.

While Claimify is for noncommercial/research use (I'm not actually sure if it's even publicly available through Microsoft), their process and prompts are available in the whitepaper here: [[2502.10855] Towards Effective Extraction and Evaluation of Factual Claims](https://arxiv.org/abs/2502.10855). Note that this is an intensive/expensive process, with each extraction requiring multiple LLM calls to split and contextualize, select, disambiguate, and decompose claims, and further calls to evaluate entailment, coverage, and decontextualization.

## Extraction

### 1. Context Creation

Claimify accepts a question-answer pair as input. It uses NLTK's `sentence tokenizer` to split the answer into sentences. Context is created for each sentence _s_ based on a configurable combination of _p_ preceding sentences, _f_ following sentences, and optional metadata (e.g., the header hierarchy in a Markdownstyle answer).

The parameters _p_ and _f_ can be defined separately for the subsequent stages, allowing each stage to have a distinct context. In practice, _p_ was set to 5 for all stages, and _f_ was set to 5 for selection and 0 for disambiguation and decomposition.

### 2. [selection](./extraction/selection.py)

Next, Claimify uses an LLM to determine whether each sentence contains any verifiable content, in light of its context and the question. When the LLM identifies that a sentence contains both verifiable and unverifiable components, it rewrites the sentence, retaining only the verifiable components.

More specifically, the LLM selects one of the following options:

1. state that the sentence does not contain any verifiable content,
2. return a modified version of the sentence that retains only verifiable content, or
3. return the original sentence, indicating that it does not contain any unverifiable content.

If the LLM selects the first option, the sentence is labeled "No verifiable claims" and excluded from subsequent stages (disambiguation, decomposition).

### 3. [disambiguation](./extraction/disambiguation.py)

Claimify uses an LLM to identify two types of ambiguity. The first is _referential ambiguity_, which occurs when it is unclear what a word or phrase refers to. The second is _structural ambiguity_, which occurs when grammatical structure allows for multiple interpretations.

The LLM is also asked to determine whether each instance of ambiguity can be resolved using the question and the context and selects one of the following options:

1. state the sentence "Cannot be disambiguated",
2. return a clarified version of the sentence, resolving all ambiguity, or
3. returns the original sentence, indicating there was no ambiguity.

If the LLM selects the first option, the sentence and excluded from the Decomposition stage, even if it has unambiguous, verifiable components.

### 4. [decomposition](./extraction/decomposition.py)

Finally, Claimify uses an LLM to decompose each disambiguated sentence into decontextualized factual claims. If it does not return any
claims, the sentence is labeled "No verifiable claims."

Extracted claims may include text in brackets, which typically represents information implied by the question or context but not explicitly stated in the source sentence. A benefit of bracketing is that it flags inferred content, which is inherently less reliable than content
explicitly stated in the source sentence

## Evaluation

1. [entailment](./evaluation/entailment.py)
2. [element extraction](./evaluation/element.py)
3. [element coverage](./evaluation/coverage.py)
4. [decontextualization](./evaluation/decontextualization.py)
5. [invalid sentences](./evaluation/invalid_sentences.py)
6. [invalid claims](./evaluation/invalid_claims.py)

## References

- [Claimify: Extracting high-quality claims from language model outputs - Microsoft Research](https://www.microsoft.com/en-us/research/blog/claimify-extracting-high-quality-claims-from-language-model-outputs/#_ftnref2)
- [[2502.10855] Towards Effective Extraction and Evaluation of Factual Claims](https://arxiv.org/abs/2502.10855)
