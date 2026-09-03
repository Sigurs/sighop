"""Generate the corpus golden file. Run deliberately; review the diff.

    uv run python -m tests.protocol.generate_golden

Per design D10 this is a script, not a `--update-golden` test flag anyone can
reach for when a test goes red. A generated expectation records whatever the
code did on the day, bugs included, so its output is reviewed as part of the
change that regenerates it — and spot-checked against the independent analysis
in the proposal (advert names, type counts) before being committed as a fixture.
"""

from __future__ import annotations

from pathlib import Path

from tests.protocol.golden import render_corpus

GOLDEN_PATH = Path(__file__).with_name("corpus_golden.txt")


def main() -> None:
    content = render_corpus()
    existing = GOLDEN_PATH.read_text() if GOLDEN_PATH.is_file() else None
    GOLDEN_PATH.write_text(content)
    verb = "unchanged" if existing == content else "written"
    print(f"{GOLDEN_PATH} {verb}: {content.count(chr(10))} lines")
    if existing is not None and existing != content:
        print("Review the diff before committing: it is a change in interpretation.")


if __name__ == "__main__":
    main()
