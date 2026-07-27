"""
db.py — single shared MongoDB Atlas connection for the whole app.

Every other file (app_backend.py, memory_context.py, memory_manager.py)
imports `get_db()` from here instead of opening its own connection.
pymongo's MongoClient is thread-safe and pools connections internally,
so one client instance, created once at import time, is correct — do
not create a new MongoClient per request.

Requires the MONGODB_URI env var to be set (Render: Environment tab;
PythonAnywhere: your WSGI config, same place the other API keys live).
"""

import os
from pymongo import MongoClient

_client = None
_db = None


def get_db():
    """
    Returns the shared Database object, creating the connection on first
    call. Safe to call from any module, any number of times.
    """
    global _client, _db

    if _db is not None:
        return _db

    uri = os.environ.get("MONGODB_URI")
    if not uri:
        raise RuntimeError(
            "MONGODB_URI is not set. On Render, add it under the "
            "service's Environment tab. Locally/PythonAnywhere, set it "
            "wherever your other env vars (API keys) live."
        )

    _client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    # Database name is taken from here, not from the URI — keeps the
    # URI itself generic (no need to edit it if you ever rename the DB).
    _db = _client["roleplay_app"]

    return _db
