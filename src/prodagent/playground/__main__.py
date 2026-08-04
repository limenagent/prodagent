"""Enable `python -m prodagent.playground` — same as the `prodagent` console script."""

from prodagent.playground.server import main

if __name__ == "__main__":
    raise SystemExit(main())
