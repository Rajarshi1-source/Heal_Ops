"""Valkey/Redis: the queue, per-resource locks, dedup, and read-through caches.

Everything here is disposable. If it is wiped, HealOps loses in-flight queue position and cold
caches. It does not lose approvals, plans, executions, or the audit chain.
"""
