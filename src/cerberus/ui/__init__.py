"""Cerberus's own UI: a read-only web app served from the box in the van.

A separate process from the collector (``python -m cerberus.ui``), so a UI
fault can never stop collection. It reads the database read-only and the live
snapshot the collector writes to kv (cerberus.live).
"""
