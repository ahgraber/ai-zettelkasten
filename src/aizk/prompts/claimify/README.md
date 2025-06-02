# Claimify

Claim extraction is the basis for _faithfulness_ metrics by which we can determine whether what the LLM has synthesized is supported by substantiating content or evidence. Claim extraction is also difficult and general LLMs do poorly on this task, in part due to poor specification on what makes a valid claim. Microsoft Research has published their research project `Claimify` that significantly improves the validity of claim extraction by following 5 principles:

1. The claims should capture all verifiable content in the source text and exclude unverifiable content
2. Each claim should be entailed (i.e., fully supported) by the source text.
3. Each claim should be understandable on its own, without additional context.
4. Each claim should minimize the risk of excluding critical context.
5. The system should flag cases where ambiguity cannot be resolved.

While Claimify is for noncommercial/research use (I'm not actually sure if it's even publicly available through Microsoft), their process and prompts are available in the whitepaper [here: [2502.10855] Towards Effective Extraction and Evaluation of Factual Claims](https://arxiv.org/abs/2502.10855). Note that this is an intensive/expensive process, with each extraction requiring multiple LLM calls to split and contextualize, select, disambiguate, and decompose claims, and further calls to evaluate entailment, coverage, and decontextualization.

## References

- [Claimify: Extracting high-quality claims from language model outputs - Microsoft Research](https://www.microsoft.com/en-us/research/blog/claimify-extracting-high-quality-claims-from-language-model-outputs/#_ftnref2)
- [[2502.10855] Towards Effective Extraction and Evaluation of Factual Claims](https://arxiv.org/abs/2502.10855)
