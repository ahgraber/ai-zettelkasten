# 001 - Content Collection & Archiving

## Status

10 December 2024 - Accepted 9 July 2025 - Updated

<!-- Proposed/Accepted/Revised/Deprecated/Superseded -->

## Context

In order to run RAG, the system must have access to content.
Most of the content I'm interested in is content from the web.

Is the code to handle content collection and archiving part of this project, or should it leverage other existing projects?

## Decision

### Selected Approach: **[Karakeep](https://karakeep.app/)**

Use [Karakeep](https://karakeep.app/), a self-hostable bookmarking and archive application.
Fall back to [Jina Reader](https://jina.ai/reader/) or [Exa Contents API](https://docs.exa.ai/reference/get-contents) for content extraction if Karakeep fails.

### Rationale

Karakeep is self-hostable and has a strong dev community.
The application supports archiving (via [monolith](https://github.com/Y2Z/monolith)), and has an accessible API (and [community-led python SDK](https://github.com/thiswillbeyourgithub/karakeep_python_api)) for accessing content.
Additionally, Karakeep supports webhook for triggering external services when bookmarks are created, changed or crawled.

### Alternative Considered

#### Option 1: [ArchiveBox](https://github.com/ArchiveBox/ArchiveBox/tree/dev)

Pros:

- All-in-one tool provides pretty much every scraping technique I could possibly need.
- Runs as CLI tool, python package, and browser.
  Hostable with Docker.

Cons:

- Feels like way more than I need.
- Current version with `abx-plugin` ecosystem is still under development (stable is v0.7, dev is v0.8, anticipated next stable release is v0.9)
- ArchiveBox maintainer is now (as of April 2025) working for a startup, so ArchiveBox progress will slow.
- Do I really need the ArchiveBox package manager?

Reason for not selecting:

- I want to try it myself (ArchiveBox will be a good source for inspiration / comparison)
- Trying to minimize this project's dependencies

#### Option 2: [Firecrawl](https://www.firecrawl.dev/)

Pros:

- Service and [self-hosted](https://github.com/mendableai/firecrawl/blob/main/SELF_HOST.md)
  - service supposedly handles captchas and bot detection
- All-in-one does scraping, crawling, and parsing
- Integrations with LLM/AI Frameworks

Cons:

- Service costs $ (though it seems reasonable), given the goal is to scrape (not crawl) a limited set of pages
- Self-hosted version does not handle bot detection or captchas

#### Option 3: [Jina Reader](https://jina.ai/reader/)

Pros:

- Service and [self-hosted](https://github.com/jina-ai/reader)
  - service supposedly handles captchas and bot detection
- All-in-one does scraping and extracts text content as markdown
- Free and paid

Cons:

- Free version is rate-limited
- This is an extractor more than an archiver.
  If the idea is to archive the exact content, then parse (so as parsing improves I don't have to re-scrape), Jina Reader (and services like it) do not fit the archiving requirement.

#### Option 4: [Exa Contents API](https://docs.exa.ai/reference/get-contents)

Pros:

- Service
  - service supposedly handles captchas and bot detection
- All-in-one does scraping and extracts text content
- Available summarizer integration
- Can crawl subpages

Cons:

- Personal version is rate-limited (5 RPS / 12 RPM)
- No free / selfhosted version; [not inexpensive](https://exa.ai/pricing)
- Like Jina, this is an extractor more than an archiver.
  If the idea is to archive the exact content, then parse (so as parsing improves I don't have to re-scrape), Jina Reader (and services like it) do not fit the archiving requirement.

## Implementation Details

<!-- Technical specifications
Required resources
Estimated timeline
Key implementation steps -->

## Related ADRs

- [002 - Content Parsing](./002-content-parsing.md)
