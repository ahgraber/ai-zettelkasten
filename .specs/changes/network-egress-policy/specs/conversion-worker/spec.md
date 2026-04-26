# Delta for conversion-worker

## ADDED Requirements

### Requirement: Containment-check subprocess artifact metadata

The conversion subprocess produces a metadata record describing where the converted Markdown and any extracted figures were written within the job's workspace.
The parent process consumes those filenames to read and upload artifacts.
The system SHALL treat the boundary between the conversion subprocess and the parent process as a trust boundary: every filename the parent receives from subprocess-produced metadata SHALL be containment-checked against the job's workspace directory before the parent reads, opens, or uploads the file.

A filename received from subprocess metadata SHALL be rejected if any of the following hold:

- It is an absolute path.
- It contains a path separator (`/` or `\`) or a parent-traversal segment (`..`).
- The path obtained by composing it with the workspace directory and resolving symlinks does not lie within the resolved workspace directory.

Rejection SHALL surface as a typed error and SHALL fail the job as a non-retryable failure; the parent SHALL NOT read or upload the rejected path.

#### Scenario: Standard subprocess-produced filename accepted

- **GIVEN** the conversion subprocess records `markdown_filename = "output.md"` and `figure_files = ["figure-001.png", "figure-002.png"]` in metadata
- **WHEN** the parent prepares to read and upload the artifacts
- **THEN** every filename passes the containment check, the artifacts are read from inside the workspace, and the upload proceeds

#### Scenario: Parent-traversal filename rejected

- **GIVEN** the conversion subprocess records `markdown_filename = "../../etc/hostname"` in metadata
- **WHEN** the parent prepares to read the markdown artifact
- **THEN** a typed containment error is raised, no `open()` is issued against the traversal path, the job is failed as non-retryable, and no upload is performed

#### Scenario: Absolute-path filename rejected

- **GIVEN** the conversion subprocess records `markdown_filename = "/etc/hostname"` in metadata
- **WHEN** the parent prepares to read the markdown artifact
- **THEN** a typed containment error is raised regardless of whether the path exists; the parent SHALL NOT compute `workspace / "/etc/hostname"` and read the resulting absolute path

#### Scenario: Backslash-separator filename rejected

- **GIVEN** the conversion subprocess records `figure_files = ["..\\..\\etc\\hostname"]` in metadata
- **WHEN** the parent prepares to read the figure artifacts
- **THEN** a typed containment error is raised because the filename contains a path separator

#### Scenario: Symlink-escape filename rejected

- **GIVEN** a workspace contains a symlink `escape -> /etc` and the conversion subprocess records `figure_files = ["escape/hostname"]` in metadata
- **WHEN** the parent prepares to read the figure
- **THEN** the resolved path falls outside the resolved workspace directory and the containment check rejects the filename
