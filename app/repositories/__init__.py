"""
Repositories — data access layer.

Each repository wraps database operations for a single aggregate:
- webhook_repository.py → CRUD for webhook events
- agent_repository.py   → CRUD for agents (with upsert)
- call_repository.py    → CRUD for call records (with upsert)
"""
