"""Usage ledger: per-call consumption rows as product data.

Quota stays the enforcement authority; the ledger is the durable
per-user / per-model / per-feature consumption history that a later
usage UI only has to read. One chokepoint feeds it — the same provider
wrappers that feed spans and metrics.
"""
