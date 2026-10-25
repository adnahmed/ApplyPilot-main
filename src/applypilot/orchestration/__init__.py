"""Temporal orchestration boundary. Importing this package does not start a client.

Workflow modules contain only deterministic orchestration. Database, browser,
model and filesystem dependencies are imported inside Activity implementations.
"""
