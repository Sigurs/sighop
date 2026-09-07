"""The request handlers, one module per area of DESIGN.md §8.

Each module exposes a `router` that `web/app.py` mounts. Nothing here holds
state: the state seam is passed in through the application, and every write goes
through the same repository call the equivalent `sighop` subcommand makes, so a
rule has one implementation rather than two.

Importing this package imports no route module, for the same reason
`sighop.web` imports no submodule.
"""
