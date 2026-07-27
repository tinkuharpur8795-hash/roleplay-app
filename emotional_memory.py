import os
import json
import time
import threading


class EmotionalBeatBank:
    """
    Append-only store of emotionally significant exchanges, per character+chat.

    Why this exists separately from `current_arc` / `chronicle`:
    those are fact-compressed on purpose (that's what makes them cheap to keep
    feeding into every prompt over a long conversation). Compression is lossy
    by design, and the first thing it throws away is *how* something felt,
    not *that* it happened.

    Beats in this bank are written once and never rewritten or summarized.
    A beat is small (one exchange, not a whole scene), so keeping even 40-60
    of them costs very little context budget while preserving the original
    emotional texture indefinitely.
    """

    MAX_BEATS = 60  # generous ceiling; beats are 1-2 sentences each

    def __init__(self, base_dir, character, chat_id):
        # base_dir kept for signature compatibility with memory_context.py
        self.character = character
        self.chat_id = str(chat_id)
        self._lock = threading.Lock()
        self.beats = self._load()

    def _load(self):
        try:
            from db import get_db
            db = get_db()
            doc = db.emotional_beats.find_one({"_id": f"{self.character}_{self.chat_id}"})
            if doc and "beats" in doc:
                return doc.get("beats", [])
        except Exception as e:
            print(f"[⚠️ BEATS DB] Load error: {e}")
        return []
    
    def _save(self):
        try:
            from db import get_db
            db = get_db()
            db.emotional_beats.update_one(
                {"_id": f"{self.character}_{self.chat_id}"},
                {"$set": {
                    "beats": self.beats, 
                    "character": self.character, 
                    "chat_id": self.chat_id
                }},
                upsert=True
            )
        except Exception as e:
            print(f"[⚠️ BEATS DB] Save error: {e}")
            
    def add_beat(self, turn_id, verbatim, trigger="", surface_affect="",
                 concealed_feeling="", label="", intensity=1):
        """
        Append a beat. `verbatim` should be the actual exchange text (already
        anonymized to {{user}}/{{char}} if that's your convention) — not a
        paraphrase. This text is never edited after being written.
        """
        if not verbatim or not verbatim.strip():
            return
        with self._lock:
            if any(b["turn_id"] == turn_id for b in self.beats):
                return  # already captured this turn

            self.beats.append({
                "turn_id": turn_id,
                "timestamp": time.time(),
                "label": label or (trigger[:40] if trigger else "unlabeled moment"),
                "trigger": trigger,
                "surface_affect": surface_affect,
                "concealed_feeling": concealed_feeling,
                "verbatim": verbatim.strip(),
                "intensity": intensity,
            })

            if len(self.beats) > self.MAX_BEATS:
                # Trim lowest-intensity/oldest first, then restore turn order
                self.beats.sort(key=lambda b: (b["intensity"], b["turn_id"]))
                self.beats = self.beats[-self.MAX_BEATS:]
                self.beats.sort(key=lambda b: b["turn_id"])

            self._save()

    def get_relevant(self, query_text="", current_turn=0, max_results=3, min_gap_turns=6):
        """
        Return the beats most worth resurfacing right now.

        Scoring blends keyword overlap with the current message against raw
        emotional intensity. Beats from the last `min_gap_turns` are skipped
        by default since they're presumably still visible in the raw chat
        history — resurfacing them would just be noise.
        """
        if not self.beats:
            return []

        query_words = {w.lower().strip(".,!?\"'") for w in query_text.split() if len(w) > 3}

        scored = []
        for b in self.beats:
            if current_turn and (current_turn - b["turn_id"]) < min_gap_turns:
                continue
            text_words = {
                w.lower().strip(".,!?\"'")
                for w in (b["verbatim"] + " " + b.get("trigger", "")).split()
            }
            overlap = len(query_words & text_words)
            score = overlap * 3 + b.get("intensity", 1)
            scored.append((score, b))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [b for _, b in scored[:max_results]]

    def format_for_prompt(self, beats):
        if not beats:
            return ""
        lines = []
        for b in beats:
            tag = f"[{b['label']}] " if b.get("label") else ""
            lines.append(f"- {tag}{b['verbatim']}")
        return (
            "\n[Emotional memory — moments that still linger for you, "
            "in your own words:]\n" + "\n".join(lines) + "\n\n"
        )
