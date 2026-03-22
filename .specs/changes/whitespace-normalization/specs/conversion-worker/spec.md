# Conversion Worker — Whitespace Normalization Delta

## ADDED

### Requirement: Normalize whitespace in Markdown output

The system SHALL normalize whitespace in the Markdown output before writing to the output file and computing its content hash.
Normalization collapses multiple consecutive spaces to a single space and collapses 3 or more consecutive newlines to exactly 2 newlines.

#### Scenario: Multiple spaces collapsed on write

- **GIVEN** Docling conversion produces Markdown with multiple consecutive spaces
- **WHEN** the worker prepares to write `output.md`
- **THEN** each run of 2+ spaces is collapsed to a single space

#### Scenario: Multiple newlines collapsed on write

- **GIVEN** Docling conversion produces Markdown with 3 or more consecutive newlines
- **WHEN** the worker prepares to write `output.md`
- **THEN** each run of 3+ newlines is collapsed to exactly 2 newlines

#### Scenario: Hash computed on normalized Markdown

- **GIVEN** normalization modifies the Markdown text
- **WHEN** the content hash is computed
- **THEN** the hash is computed over the normalized Markdown, ensuring consistency across reruns with identical input

#### Scenario: Indentation in code blocks preserved

- **GIVEN** the Markdown contains code blocks with intentional indentation
- **WHEN** whitespace normalization is applied
- **THEN** the indentation within code blocks is preserved and not collapsed
