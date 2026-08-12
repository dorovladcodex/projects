"""Offline backtest engine for ByBot alpha research.

This package never imports execution code and cannot reach the exchange. It
replays stored history so that a hypothesis can be rejected before any Demo
run, not after one.

Numeric policy: prices and returns are float64 inside the simulation loop.
The project standard is Decimal, but a five-year hourly replay evaluates tens
of millions of arithmetic operations and float64 carries ~15 significant
digits — far beyond the basis-point resolution these results are read at.
Costs, funding rates and reported aggregates stay Decimal so the accounting a
human reads is exact.
"""
