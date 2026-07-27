"""
memory_store.py — Consolidated companion memory (Phase 2)
───────────────────────────────────────────────────────────
Replaces three scattered, mostly-dormant files per chat:
    - AdvancedMemoryManager's `{id}_{chat}_facts.json`
    - ExpectationMemory's now-orphaned `{id}_{chat}_expectations.json`
    - (a "relationship state" concept that didn't have a home before)

...with a single file: `{character_id}_{chat_id}_companion.json`.

Deliberately OUT OF SCOPE for this class (left untouched on purpose):
    - Chronicle / current_arc summary — still lives in
      AdvancedMemoryManager.load_summary/save_summary. That's already a
      single, working, actively-used file; consolidating it here would
      touch the live Phase-1-verified prompt pipeline for no real benefit.
    - SceneState's `{character}_{chat}_state.json` — actively read/written
      throughout app_backend.py (physical_state/environment). NOTE: this
      duplicates the *concept* of AdvancedMemoryManager's dormant
      `{id}_{chat}_scene.json`. Both exist; only the former is live. This
      is a real duplication worth resolving, but it touches live behavior
      and needs its own dedicated pass — flagged for a later phase, not
      fixed here.
"""

import os
import json
import threading
import time


class CompanionMemoryStore:

    SCHEMA_VERSION = 1

    def __init__(self, base_dir):
        self.memory_dir = os.path.join(base_dir, "advanced_memory", "companion_state")
        os.makedirs(self.memory_dir, exist_ok=True)
        self.lock = threading.Lock()

    def _get_file_path(self, character_id, chat_id):
        return os.path.join(self.memory_dir, f"{character_id}_{chat_id}_companion.json")

    def _default_record(self):
        return {
            "schema_version": self.SCHEMA_VERSION,
            "updated_at": time.time(),
            "facts": {
                "user_profile": {},
                "character_discoveries": {},
                "milestones": [],
            },
            "relationship": {
                "stage": "stranger",
                "mood_trend": "neutral",
                "last_topics": [],
            },
            "reminders": [],
        }

    def load(self, character_id, chat_id):
        """Thread-safe load. Returns a fresh default record if none exists yet."""
        path = self._get_file_path(character_id, chat_id)
        with self.lock:
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    # Backfill any keys missing from an older/partial record
                    # so callers never have to defensively .get() every field.
                    default = self._default_record()
                    for key, val in default.items():
                        data.setdefault(key, val)
                    return data
                except Exception as e:
                    print(f"[⚠️ COMPANION MEMORY] Load error: {e}")
            return self._default_record()

    def save(self, character_id, chat_id, record):
        """Thread-safe save. Stamps updated_at on every write."""
        path = self._get_file_path(character_id, chat_id)
        record["updated_at"] = time.time()
        record["schema_version"] = self.SCHEMA_VERSION
        with self.lock:
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(record, f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"[⚠️ COMPANION MEMORY] Save error: {e}")

    def delete(self, character_id, chat_id):
        """Removes this chat's consolidated memory file, if present. Returns True if a file was removed."""
        path = self._get_file_path(character_id, chat_id)
        with self.lock:
            if os.path.exists(path):
                try:
                    os.remove(path)
                    return True
                except Exception as e:
                    print(f"[⚠️ COMPANION MEMORY] Delete error: {e}")
        return False

    def migrate_from_legacy_files(self, character_id, chat_id, fact_file=None, expectations_file=None):
        """
        One-time, NON-DESTRUCTIVE migration helper. Reads the old scattered
        `_facts.json` and `_expectations.json` files if present, and folds
        their data into the new consolidated record. Does NOT delete the
        old files — call this once per chat, verify the merged result looks
        right, then remove the old files yourself once you're satisfied.
        """
        record = self.load(character_id, chat_id)

        if fact_file and os.path.exists(fact_file):
            try:
                with open(fact_file, 'r', encoding='utf-8') as f:
                    old_facts = json.load(f)
                record["facts"]["user_profile"].update(old_facts.get("user_profile", {}))
                record["facts"]["character_discoveries"].update(old_facts.get("character_discoveries", {}))
                record["facts"]["milestones"].extend(old_facts.get("milestones", []))
            except Exception as e:
                print(f"[⚠️ COMPANION MEMORY] Migration (facts) error: {e}")

        if expectations_file and os.path.exists(expectations_file):
            try:
                with open(expectations_file, 'r', encoding='utf-8') as f:
                    old_reminders = json.load(f)
                existing_ids = {r["id"] for r in record["reminders"]}
                for r in old_reminders:
                    if r.get("id") not in existing_ids:
                        record["reminders"].append(r)
            except Exception as e:
                print(f"[⚠️ COMPANION MEMORY] Migration (reminders) error: {e}")

        self.save(character_id, chat_id, record)
        return record
