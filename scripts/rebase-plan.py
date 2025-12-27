#!/usr/bin/env python3
"""GIT_SEQUENCE_EDITOR script for feature/ingest branch cleanup.

Usage:
    GIT_SEQUENCE_EDITOR="python3 scripts/rebase-plan.py" git rebase -i 64afd255c7af

This script:
1. Reads the rebase todo list
2. Reorders non-adjacent squash group members to be adjacent
3. Marks squash targets as 'fixup'
4. Inserts 'exec git commit --amend -m "..."' after groups/rewords that need new messages
"""

import sys

# --- Squash groups: first commit is the anchor, rest get moved after it and marked fixup ---
# Each tuple: (anchor_hash_prefix, [follower_hash_prefixes], new_message_or_None)
SQUASH_GROUPS = [
    # S1: Config pair (not adjacent — 0b9be0b between)
    (
        "046df3ec7656",
        ["292358247751"],
        None,  # message already correct
    ),
    # S2: Whitespace fix pair (adjacent)
    (
        "299261682073",
        ["07ef2e14cef7"],
        None,
    ),
    # S3: Config snapshot pair (adjacent)
    (
        "b076f1c0519a",
        ["18300f90b615"],
        "fix(conversion): write config snapshot to manifest for idempotency and replayability",
    ),
    # S4: precrawledArchive pair (not adjacent — 5 commits between)
    (
        "8e98582c706c",
        ["be710e7e2c14"],
        "feat(worker): handle precrawledArchive assets in conversion worker",
    ),
    # S5: Shutdown pair (adjacent)
    (
        "49994cc5ff48",
        ["e9551b06b1c9"],
        "feat(worker): add graceful shutdown on SIGTERM/SIGINT",
    ),
    # S6: source_cleanup pair (adjacent)
    (
        "a80ded7e93b0",
        ["055be5289ae7"],
        "feat(source_cleanup): resolve SQLite URL and clean up notebook",
    ),
    # S7: Worker cancel pair (adjacent)
    (
        "b239323c58bb",
        ["a9853e53f4e6"],
        "fix(worker): stop processing and skip finalizing cancelled jobs",
    ),
    # S8: Backpressure pair (not adjacent — bf15ea1 between)
    (
        "489ea861c90d",
        ["93c27bb5b68e"],
        None,
    ),
    # S9: OCR deps pair (adjacent)
    (
        "102d36e95896",
        ["818eb3e40d00"],
        "chore(deps): add OCR and evaluation dependencies",
    ),
    # S10: Reorder experiment sequence (adjacent)
    (
        "85341e9b2219",
        ["41bf3cfa5db6", "3d36dcb68706", "4dec7d193dcc"],
        "feat(reorder_experiment): add OCR reorder evaluation with ROUGE and Kendall's Tau metrics",
    ),
    # S11: GPU docs pair (not adjacent — many commits between)
    (
        "fcbdc8185e05",
        ["fe0dabe8909c"],
        "docs: add NVIDIA GPU setup guides for Podman on Debian 13",
    ),
    # S12: Worker process mgmt specs (adjacent)
    (
        "1b40c4346708",
        ["72cc0e5d1968"],
        "feat(spec): define spec and implementation plan for robust worker process management",
    ),
    # S13: Alembic sequence (adjacent)
    (
        "6efa1c5a0bb9",
        ["6c725d4fbed6", "728a6c428596", "4255e1b04ea8"],
        "feat(conversion/db): add Alembic migrations with baseline schema and round-trip tests",
    ),
    # S14: Worker extract sequence (adjacent)
    (
        "1c335f62b62d",
        ["7c05750cf887", "af3147b248c8", "68f533baa1d4"],
        "refactor(conversion): decompose worker god module into focused submodules",
    ),
    # S15: Config threading (adjacent)
    (
        "088dfbffc692",
        ["b339bbaeb65d", "0e0d8e6806ad"],
        "refactor(conversion): thread config through API and worker, remove fallbacks",
    ),
    # B1: Worker fixes (adjacent, same day)
    (
        "c44649dfedb6",
        ["7c21abd2d829", "f4c117e96997"],
        "fix(worker): adopt type-safe retryability, log timeout phases, and finalize lifecycle tests",
    ),
    # B2: Agents docs (5 days apart, within a week)
    (
        "c4b138dd1b62",
        ["23080fe5fe3a"],
        "docs(agents): revise instructions and add error handling best practices",
    ),
]

# --- Standalone rewords: hash_prefix -> new_message ---
REWORDS = {
    "5698433f832d": "docs: update copilot instructions",
    "d06f54393865": "docs: update AI agent instructions",
    "723b078bbb73": "chore: adopt spec-driven development workflow",
    "e9217d229b65": "chore: remove legacy spec files",
    "23f0fd17d859": "fix(lint): correct pseudorandom typo in ruff config",
    "b0df5b0c91c1": "feat(conversion): add provenance and idempotency enhancements",
}

# Build lookup structures
follower_to_anchor = {}  # follower_prefix -> anchor_prefix
all_followers = set()
anchor_messages = {}  # anchor_prefix -> new_message (if any)

for anchor, followers, msg in SQUASH_GROUPS:
    for f in followers:
        follower_to_anchor[f] = anchor
        all_followers.add(f)
    if msg:
        anchor_messages[anchor] = msg


def hash_match(line_hash, prefix):
    """Check if a commit hash in a todo line matches our prefix."""
    return line_hash.startswith(prefix) or prefix.startswith(line_hash)


def find_prefix(full_hash):
    """Find which known prefix matches this hash, if any."""
    for anchor, followers, _ in SQUASH_GROUPS:
        if hash_match(full_hash, anchor):
            return anchor
        for f in followers:
            if hash_match(full_hash, f):
                return f
    for rw_prefix in REWORDS:
        if hash_match(full_hash, rw_prefix):
            return rw_prefix
    return None


def escape_msg(msg):
    """Escape a commit message for shell use in exec lines (double-quote context)."""
    return msg.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")


def process_todo(lines):
    """Process the rebase todo list."""
    # Parse todo lines, preserving comments
    entries = []  # (action, hash, rest_of_line, original_line)
    comment_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            comment_lines.append(line)
            continue
        parts = stripped.split(None, 2)
        if len(parts) >= 2:
            action, commit_hash = parts[0], parts[1]
            rest = parts[2] if len(parts) > 2 else ""
            entries.append((action, commit_hash, rest, line))

    # Step 1: For non-adjacent groups, move followers right after their anchor
    # Build a list of (hash, entry) preserving order
    ordered = list(entries)

    for anchor, followers, _ in SQUASH_GROUPS:
        # Find anchor position
        anchor_idx = None
        for i, (_, h, _, _) in enumerate(ordered):
            if hash_match(h, anchor):
                anchor_idx = i
                break
        if anchor_idx is None:
            print(f"WARNING: anchor {anchor} not found in todo", file=sys.stderr)
            continue

        # Move each follower right after anchor (in order)
        insert_after = anchor_idx
        for f in followers:
            # Find follower
            follower_idx = None
            for i, (_, h, _, _) in enumerate(ordered):
                if hash_match(h, f):
                    follower_idx = i
                    break
            if follower_idx is None:
                print(f"WARNING: follower {f} not found in todo", file=sys.stderr)
                continue
            if follower_idx == insert_after + 1:
                # Already in position
                insert_after = follower_idx
                continue
            # Remove from current position and insert after anchor
            entry = ordered.pop(follower_idx)
            # Recalculate insert position (may have shifted)
            for i, (_, h, _, _) in enumerate(ordered):
                if hash_match(h, anchor):
                    anchor_idx = i
                    break
            # Find the last already-placed follower
            insert_pos = anchor_idx + 1
            for prev_f in followers:
                if prev_f == f:
                    break
                for i, (_, h, _, _) in enumerate(ordered):
                    if hash_match(h, prev_f):
                        insert_pos = i + 1
                        break
            ordered.insert(insert_pos, entry)
            insert_after = insert_pos

    # Step 2: Generate output
    output_lines = []
    for action, commit_hash, rest, original in ordered:
        prefix = find_prefix(commit_hash)

        if prefix and prefix in all_followers:
            # This is a follower — mark as fixup
            output_lines.append(f"fixup {commit_hash} {rest}")
        elif prefix and prefix in anchor_messages:
            # This is an anchor that needs a new message — pick then exec amend
            output_lines.append(f"pick {commit_hash} {rest}")
            msg = escape_msg(anchor_messages[prefix])
            output_lines.append(
                f'exec git commit --amend -m "$(printf \'%s\\n\\nAI-assistant: Claude Code\' "{msg}")"'
            )
        elif prefix and prefix in REWORDS:
            # Standalone reword
            msg = escape_msg(REWORDS[prefix])
            output_lines.append(f"pick {commit_hash} {rest}")
            output_lines.append(
                f'exec git commit --amend -m "$(printf \'%s\\n\\nAI-assistant: Claude Code\' "{msg}")"'
            )
        else:
            # Normal pick — keep as is
            output_lines.append(f"pick {commit_hash} {rest}")

    # Add comments at the end
    output_lines.extend(comment_lines)
    return output_lines


def main():
    todo_file = sys.argv[1]
    with open(todo_file) as f:
        lines = f.readlines()

    result = process_todo(lines)

    with open(todo_file, "w") as f:
        f.write("\n".join(result) + "\n")


if __name__ == "__main__":
    main()
