# Contributing

First off, thank you for taking the time to contribute. prodagent is a
**teaching-grade** agent kernel: it is intentionally small, dependency-free,
and optimized to be read from top to bottom. Keeping it that way matters more
than adding features.

## What kind of contributions are most welcome

- **"I got stuck here" issues.** If a concept, comment, or doc is unclear,
  open an issue — confusing spots for learners are treated as bugs.
- Bug reports with a minimal, offline reproduction (no real API key needed;
  use `ScriptedLlm`).
- Doc/typo fixes, clearer comments, additional tests.
- New examples that demonstrate a kernel primitive without changing it.

## Before opening a PR

1. Make sure tests pass:

   ```bash
   pip install pytest pytest-asyncio
   PYTHONPATH=. python -m pytest tests/ -q
   ```

2. Keep the kernel (`src/kernel/`) free of third-party dependencies. New
   capabilities belong in `src/runtime/` as replaceable strategies, not in the
   kernel.
3. Run the linters: `make lint` (and `make format` to auto-fix).
4. Prefer many small, well-named primitives over one big abstraction. If a
   new concept can be expressed by composing existing ones, do that instead.

## Style

- Public code, identifiers, comments, and docstrings are in English; the
  narrative docs under `docs/zh` are Chinese, `docs/en` English.
- Every new public object gets a short docstring explaining *why* it exists.

## Opening a pull request

- Describe the problem first, then the change.
- Link the related issue.
- Keep the diff focused; one concern per PR.

By contributing, you agree your contributions are licensed under the project's
MIT License.
