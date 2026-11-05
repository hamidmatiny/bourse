"""Benchmark-only adversary agents (ADR-0042). Not part of the honest roster.

These containers exist so BOURSE-Bench can measure catch rates. They must never be documented
as production agents. Attacker endpoints are sinkhole names (*.attacker.example); nothing
on bourse_internal can reach the internet.
"""
