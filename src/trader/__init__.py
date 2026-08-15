"""trading-agent: an educational, human-approved LLM trading pipeline.

Read the layers in this order if you're new here:

1. ``trader.domain``   — the vocabulary (models) and the rules (risk gate). Pure Python.
2. ``trader.ports``    — the interfaces the app needs from the outside world.
3. ``trader.adapters`` — real implementations of those interfaces (Alpaca, Claude, Telegram, SQLite).
4. ``trader.app``      — the use cases that wire it together (propose, approve, report).
5. ``trader.cli``      — the command-line entry point.
"""

__version__ = "0.1.0"
