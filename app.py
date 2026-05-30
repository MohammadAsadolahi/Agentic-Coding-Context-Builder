#!/usr/bin/env python3
"""
app.py — CLI entry point for the two-pass agentic repo map generator.

Usage:
    python app.py --prompt "fix the login bug" [--root /path/to/repo]

Prints the final repo map (identical format to repo_map_generator.py output).
"""

from aider.models import Model
import argparse
import os
import sys

from agentic_mapper import run_agentic_map


def main():
    parser = argparse.ArgumentParser(
        description="Agentic repo map generator."
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Root directory of the repository (default: current directory).",
    )
    parser.add_argument(
        "--prompt",
        required=True,
        help="User prompt describing the task.",
    )
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"Error: {root} is not a directory.", file=sys.stderr)
        sys.exit(1)

    result = run_agentic_map(root, args.prompt)

    # ── Print output identical to repo_map_generator.py main() ────────────
    print(f"{'=' * 70}")
    print("Aider Repo Map Generator (using aider-chat directly)")
    print(f"{'=' * 70}")
    print(f"Root: {result.root}")
    print(f"Model: {result.model_name}")
    print(f"User prompt: {args.prompt!r}")
    print(f"Edit format: {result.edit_format}")
    print(f"map_tokens (from model): {result.map_tokens}")
    print(f"max_input_tokens: {result.max_input_tokens}")
    print(
        f"Found {len(result.rel_files)} source files "
        f"(via {'git' if result.used_git else 'filesystem walk'})"
    )
    for rel_path in result.rel_files[:15]:
        print(f"  {rel_path}")
    if len(result.rel_files) > 15:
        print(f"  ... and {len(result.rel_files) - 15} more")

    print(f"\nChat files: {len(result.chat_rel_fnames)}")
    if result.chat_rel_fnames:
        for f in result.chat_rel_fnames:
            print(f"  {f}")
    print(f"Other files: {len(result.rel_files)}")
    print(f"Max tokens (expanded for no-chat): {result.effective_budget}")

    print("\n--- Prompt conditioning ---")
    print(
        f"  mentioned_idents ({len(result.mentioned_idents)}): "
        f"{result.mentioned_idents[:20]}"
        f"{'...' if len(result.mentioned_idents) > 20 else ''}"
    )
    print(
        f"  mentioned_fnames ({len(result.mentioned_fnames)}): "
        f"{result.mentioned_fnames}"
    )

    if result.used_git and (
        result.recency_boost_fnames or result.cochange_boost_fnames
    ):
        print("\n--- Git-based context enrichment ---")
        if result.recency_boost_fnames:
            print(
                f"  recency_boosts  ({len(result.recency_boost_fnames)}): "
                f"{result.recency_boost_fnames[:10]}"
                f"{'...' if len(result.recency_boost_fnames) > 10 else ''}"
            )
        if result.cochange_boost_fnames:
            print(
                f"  cochange_boosts ({len(result.cochange_boost_fnames)}): "
                f"{result.cochange_boost_fnames[:10]}"
                f"{'...' if len(result.cochange_boost_fnames) > 10 else ''}"
            )

    print("\nGenerating repo map...")
    print(f"\n{'=' * 70}")
    if result.repo_map:
        num_tokens = Model(result.model_name).token_count(result.repo_map)
        print(
            f"REPO MAP OUTPUT ({len(result.repo_map)} chars, "
            f"~{num_tokens} real tokens)"
        )
        print(f"{'=' * 70}")
        try:
            print(result.repo_map)
        except UnicodeEncodeError:
            import io as io_mod

            out = io_mod.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace"
            )
            out.write(result.repo_map)
            out.flush()
    else:
        print("REPO MAP OUTPUT: (empty/null)")
        print(f"{'=' * 70}")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
