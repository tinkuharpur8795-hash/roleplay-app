"""
memory_context.py

Extracted from app_backend.py (RoleplayBackend) — everything to do with
memory retrieval, scene state, story-context/prompt building, and
summarization. Pulled out so this logic can be worked on without needing
the whole ~4400-line backend file in context.

MemoryContextEngine does NOT own its own state. It holds a reference to the
RoleplayBackend instance (`self.backend`) and reads/writes the same
directories, caches, and feature flags that used to live directly on
RoleplayBackend (e.g. self.backend.MEMORY_DIR, self.backend.ram_scene_cache,
self.backend.enable_scene_context_injection). This keeps the split
mechanical and low-risk: no behavior change, just a different home for the
code.

RoleplayBackend composes this via:
    self.memory_context = MemoryContextEngine(self)

and keeps thin delegator methods (same original names) so nothing else in
the app (server.py, routes, etc.) has to change how it calls these.
"""

import os
import re
import time
import json
from json_repair import repair_json
import string
import random
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
import shutil
from scene_state import SceneState
from expectation_memory import infer_due_seconds
from emotional_memory import EmotionalBeatBank
import httpx
from pinecone import Pinecone
from db import get_db
_FUTURE_EVENT_PATTERNS = [
    re.compile(r"\bi(?:'m| am) (?:going|planning) to (?:the |a |an )?([a-z][a-z' ]{2,40}?)\s+(?:tomorrow|tonight|later|this (?:week|weekend)|next \w+)", re.IGNORECASE),
    re.compile(r"\bi(?:'ve| have) got (?:an?|my) ([a-z][a-z' ]{2,40}?)\s+(?:tomorrow|tonight|later|this (?:week|weekend)|next \w+)", re.IGNORECASE),
    re.compile(r"\bi have (?:an?|my) ([a-z][a-z' ]{2,40}?)\s+(?:tomorrow|tonight|later|this (?:week|weekend)|next \w+)", re.IGNORECASE),
]

# Same fixed UTC+5:30 offset used in app_backend.py — kept in sync there.
IST = timezone(timedelta(hours=5, minutes=30), name="IST")


class MemoryContextEngine:
    def __init__(self, backend):
        # `backend` is the owning RoleplayBackend instance. All directories,
        # caches, feature flags, and sibling systems (advanced_memory,
        # companion_memory, expectations, action_engine, etc.) are accessed
        # through it exactly as they were before the split.
        self.backend = backend

    def get_memory_file(self, character, chat_id):
        cid = self.backend.CHARACTER_IDS.get(character, character)
        return os.path.join(self.backend.MEMORY_DIR, f"{cid}_chat_{chat_id}_memory.json")


    def get_summary_file(self, character, chat_id):
        cid = self.backend.CHARACTER_IDS.get(character, "unknown")
        return self.backend.advanced_memory.get_summary_file(cid, chat_id)


    def delete_chat_memory(self, character, chat_id):
        """Wipes MongoDB artifacts, local caches, and vector data for one chat."""
        cid = self.backend.CHARACTER_IDS.get(character, character)
        cache_key = f"{character}_{chat_id}"
        cid_cache_key = f"{cid}_{chat_id}"
        deleted, errors = [], []

        # 1. Clear In-Memory Vector & Emotional Beat Caches
        for _key in (cache_key, cid_cache_key):
            if _key in self.backend.advanced_memory._vector_cache:
                del self.backend.advanced_memory._vector_cache[_key]
                
        beat_key = (character, str(chat_id))
        if hasattr(self.backend, "_beat_bank_cache") and beat_key in self.backend._beat_bank_cache:
            del self.backend._beat_bank_cache[beat_key]

        # 2. Delete Local Vector Stub Files (if any legacy files exist)
        vec_dir = getattr(self.backend.advanced_memory, 'vector_dir', os.path.join(self.backend.BASE_DIR, 'memory'))
        local_files = [
            os.path.join(vec_dir, f"{cid_cache_key}.index"),
            os.path.join(vec_dir, f"{cid_cache_key}_data.json"),
            os.path.join(vec_dir, f"{cache_key}.index"),
            os.path.join(vec_dir, f"{cache_key}_data.json"),
        ]
        for fpath in local_files:
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                    deleted.append(os.path.basename(fpath))
                except Exception as e:
                    errors.append(f"{os.path.basename(fpath)}: {e}")

        # 3. Purge from Pinecone Cloud (Proxy-Safe for PythonAnywhere)
        try:
            pc_key = os.getenv("PINECONE_API_KEY")
            if pc_key:
                proxy_url = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
                pc_kwargs = {"api_key": pc_key}
                if proxy_url:
                    pc_kwargs["proxy_url"] = proxy_url
                
                pc = Pinecone(**pc_kwargs)
                active_indexes = pc.list_indexes().names()
                if active_indexes:
                    index = pc.Index(active_indexes[0])
                    # Delete using the exact metadata structure used during upserts
                    index.delete(filter={
                        "character": {"$eq": character},
                        "chat_id": {"$eq": str(chat_id)}
                    })
                    deleted.append("Pinecone cloud vectors purged")
        except Exception as e:
            errors.append(f"Pinecone deletion error: {e}")

        # 4. Wipe MongoDB Artifacts
        try:
            db = get_db()
            db.summaries.delete_one({"_id": f"{cid}_{chat_id}"})
            db.chat_states.delete_one({"_id": f"{character}_{chat_id}"})
            db.episodes.delete_many({"character": character, "chat_id": str(chat_id)})
            db.reflections.delete_one({"_id": f"{character}_{chat_id}"})
            deleted.append(f"MongoDB memory cleared for {character} (Chat {chat_id})")
        except Exception as e:
            errors.append(f"MongoDB deletion error: {e}")

        return {"deleted": deleted, "errors": errors}

    def _maybe_store_expectation(self, character, chat_id, user_text, current_turn):
        """
        Phase 4 — checks the user's message against a short list of "future
        plan" patterns (see _FUTURE_EVENT_PATTERNS) and, on a match, stores
        a reminder via ExpectationMemory so the character can naturally
        follow up later. Cheap and regex-based on purpose — meant to be
        called from a background thread so it never affects reply latency,
        and conservative on purpose so it doesn't nag about nothing.
        """
        text = user_text.strip()
        if not text or len(text) > 300:
            return

        lower = text.lower()
        matched_topic = None

        if "wish me luck" in lower:
            matched_topic = "something they wished for luck on"
        else:
            for pattern in _FUTURE_EVENT_PATTERNS:
                m = pattern.search(text)
                if m:
                    matched_topic = m.group(1).strip()
                    break

        if not matched_topic:
            return

        trigger_words = list({w.lower() for w in re.findall(r"\b[a-zA-Z]{4,}\b", text)})[:8]
        cid = self.backend.CHARACTER_IDS.get(character, character)

        # infer_due_seconds reads the SAME text that matched the "tomorrow" /
        # "tonight" / "next week" pattern, so a caring check-in lands after
        # the event actually happens rather than immediately when it's
        # mentioned — see expectation_memory.py for the delay table.
        due_in_seconds = infer_due_seconds(text)

        try:
            self.backend.expectations.store_expectation(
                cid, chat_id,
                text=f"ask {self.backend.USER_NAME} how {matched_topic} went",
                trigger_words=trigger_words,
                current_turn=current_turn,
                importance=2,
                due_in_seconds=due_in_seconds,
                kind="follow_up",
            )
        except Exception as e:
            print(f"[⚠️ EXPECTATION] store failed: {e}")

    # --- Chat Management ---


    def get_fact_string(self, character, chat_id):
        """Returns memory facts as a single string for the prompt builder."""
        memory_file = os.path.join(self.backend.MEMORY_DIR, f"{character}_{chat_id}_facts.json")
        if os.path.exists(memory_file):
            with open(memory_file, "r", encoding="utf-8") as f:
                facts = json.load(f)
                return ", ".join([f"{k}: {v}" for k, v in facts.items()])
        return ""


    def get_active_lore(self, text, char_lorebook):
        """Scans the user's message for keywords based on the SPECIFIC character's lore."""
        if not char_lorebook:
            return ""

        found_lore = []
        for keyword, description in char_lorebook.items():
            if keyword.lower() in text.lower():
                found_lore.append(f"- {keyword}: {description}")

        if found_lore:
            return "### RELEVANT LORE ###\n" + "\n".join(found_lore) + "\n\n"
        return ""

    # --- Core Generation & Prompt Building ---


    def retrieve_relevant_memory(self, recent_messages, user_msg, character, chat_id):
        """Lightweight Vector DB lookup directly hitting Pinecone with PA-Proxy support."""
        # Don't waste API calls searching for generic filler words
        if not user_msg or len(user_msg.split()) < 3:
            return ""

        try:
            # 🚨 Catch PythonAnywhere's proxy
            proxy_url = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")

            # 1. Get Embeddings for the user's message from Mistral
            mistral_key = os.getenv("MISTRAL_API_KEY")
            if not mistral_key:
                return ""

            embed_url = "https://api.mistral.ai/v1/embeddings"
            embed_payload = {
                "model": os.getenv("MISTRAL_EMBED_MODEL", "mistral-embed"),
                "input": [user_msg]
            }

            # Extremely short timeout (8s) so the UI doesn't freeze if Mistral is slow
            httpx_kwargs = {"timeout": 8.0}
            if proxy_url:
                httpx_kwargs["proxy"] = proxy_url

            with httpx.Client(**httpx_kwargs) as client:
                resp = client.post(
                    embed_url,
                    headers={"Authorization": f"Bearer {mistral_key}"},
                    json=embed_payload
                )
                resp.raise_for_status()
                query_vector = resp.json()["data"][0]["embedding"]

            # 2. Search Pinecone Database
            pc_key = os.getenv("PINECONE_API_KEY")
            if not pc_key:
                return ""

            pc_kwargs = {"api_key": pc_key}
            if proxy_url:
                pc_kwargs["proxy_url"] = proxy_url

            pc = Pinecone(**pc_kwargs)
            
            # Safely grab the active index
            active_indexes = pc.list_indexes().names()
            if not active_indexes:
                return ""
            
            index = pc.Index(active_indexes[0])

            # Query Pinecone and strictly filter for THIS character and THIS chat.
            # Pull a wider candidate pool than we'll actually show (top_k=8,
            # display top 3) — pure cosine similarity is what causes
            # "yesterday" and "last week" to get conflated when two memories
            # happen to share vocabulary. We rerank in python below using
            # relevance + recency + importance instead of relevance alone.
            query_response = index.query(
                vector=query_vector,
                top_k=8,
                include_metadata=True,
                filter={
                    "character": {"$eq": character},
                    "chat_id": {"$eq": str(chat_id)}
                }
            )

            matches = query_response.get("matches") or []
            if not matches:
                return ""

            # 🚨 THE MATH BUG FIX (kept): anything still inside the live
            # short-term context window (last ~12 turns) is already visible
            # to the model in recent_messages, so skip it here to avoid
            # duplicating it as a "flashback".
            safe_turn_threshold = max(0, self.backend.current_turn - 12)
            now_ts = time.time()

            scored = []
            for match in matches:
                meta = match.get("metadata", {}) or {}
                turn_id = meta.get("turn_id", 9999)
                if turn_id >= safe_turn_threshold:
                    continue

                text = meta.get("text", "")
                if not text:
                    continue

                mem_ts = meta.get("timestamp", now_ts)
                importance = float(meta.get("importance", 0.4))
                mem_type = meta.get("type", "episode")

                # Exponential recency decay. Reflections (see
                # generate_weekly_reflection) decay slower — they're meant
                # to summarize a whole week and stay useful longer than a
                # single exchange.
                age_days = max(0.0, (now_ts - mem_ts) / 86400.0)
                half_life = 30.0 if mem_type == "reflection" else 10.0
                recency = 0.5 ** (age_days / half_life)

                relevance = match.get("score", 0.0) or 0.0
                blended = (0.55 * relevance) + (0.25 * recency) + (0.20 * importance)
                scored.append((blended, mem_ts, text, mem_type))

            if not scored:
                return ""

            scored.sort(key=lambda x: x[0], reverse=True)

            # 3. Format the top results with an explicit, Python-computed
            # "when" label on every line — never leave the model to infer
            # elapsed time from wording alone.
            memory_lines = []
            for _, mem_ts, text, mem_type in scored[:3]:
                label = self._relative_time_label(mem_ts)
                tag = "REFLECTION (a summary of a past week)" if mem_type == "reflection" else "MEMORY"
                memory_lines.append(f"- [{label} — {tag}] {text}")

            if memory_lines:
                return (f"\n[FLASHBACK RECALL: Relevant past moments triggered by "
                         f"{self.backend.USER_NAME}'s words. Each line is tagged with "
                         f"exactly when it happened — treat these as distinct moments "
                         f"from different times, not one blended memory:]\n"
                         + "\n".join(memory_lines) + "\n\n")

        except httpx.TimeoutException:
            print("[⚠️ VECTOR RECALL] Mistral embedding timed out. Bypassing recall for this turn.")
        except Exception as e:
            print(f"[⚠️ VECTOR RECALL ERROR] -> {e}")
            
        return ""


    def _get_beat_bank(self, character, chat_id):
        """Cached accessor for the per-chat EmotionalBeatBank (mirrors get_vector_memory).
        Bounded LRU (max 20 entries) — without this, every distinct chat ever
        opened in this worker's lifetime added a permanent entry that was
        never freed, which is exactly the kind of slow, unbounded growth that
        gets a PythonAnywhere worker killed for exceeding its RAM limit."""
        if not hasattr(self, "_beat_bank_cache"):
            self.backend._beat_bank_cache = OrderedDict()
        key = (character, chat_id)
        if key in self.backend._beat_bank_cache:
            self.backend._beat_bank_cache.move_to_end(key)  # mark as recently used
            return self.backend._beat_bank_cache[key]
        self.backend._beat_bank_cache[key] = EmotionalBeatBank(self.backend.BASE_DIR, character, chat_id)
        if len(self.backend._beat_bank_cache) > 20:
            self.backend._beat_bank_cache.popitem(last=False)  # evict least-recently-used
        return self.backend._beat_bank_cache[key]


    def _is_significant_memory(self, text):
        """Helper to determine if a message contains enough substance to be embedded."""
        if not text:
            return False

        # 1. Strip asterisks and punctuation for the filler check
        import string
        clean_text = text.lower().translate(str.maketrans('', '', string.punctuation)).replace('*', '').strip()

        # 2. Reject pure filler, greetings, and simple actions
        junk_words = {
            'ok', 'okay', 'yes', 'no', 'yeah', 'yep', 'nope', 'brb', 'gtg',
            'bye', 'hello', 'hi', 'smiles', 'nods', 'laughs', 'sighs', 'hmm', 'sure'
        }
        if clean_text in junk_words:
            return False

        # 3. Reject messages that are too short to hold semantic value
        # (e.g., under 3 words AND under 15 characters)
        words = text.split()
        if len(words) < 3 and len(text) < 15:
            return False

        return True


    def _estimate_importance(self, user_text, bot_text):
        """
        Cheap, stdlib-only importance heuristic — no extra LLM/API call, so
        it stays fast and free-tier-friendly. Returns a 0.0-1.0 score that
        feeds the recency+importance+relevance blend in
        retrieve_relevant_memory, so emotionally/factually significant
        moments outlast small talk instead of every memory decaying at the
        same rate. This is intentionally rough — it doesn't need to be
        perfect, just directionally right.
        """
        combined = f"{user_text} {bot_text}".lower()
        score = 0.3  # baseline for anything that passed _is_significant_memory

        high_signal = [
            "love you", "i love", "miss you", "propose", "marry", "breakup",
            "break up", "broke up", "died", "passed away", "pregnant",
            "promotion", "got fired", "quit my job", "moving to",
            "diagnosed", "surgery", "engaged", "i'm scared", "i am scared",
            "i'm sad", "i am sad", "depressed", "anxiety", "panic attack",
            "best day", "worst day", "promise me", "never leave", "trust you",
        ]
        medium_signal = [
            "exam", "interview", "deadline", "birthday", "anniversary",
            "tomorrow", "next week", "wish me luck", "family", "mom", "dad",
            "sister", "brother", "my friend", "results",
        ]

        if any(p in combined for p in high_signal):
            score += 0.45
        if any(p in combined for p in medium_signal):
            score += 0.2
        if "?" in (user_text or ""):
            score += 0.05

        return max(0.0, min(1.0, score))


    def _episode_log_path(self, character, chat_id):
        """
        Lightweight local JSONL episode log kept alongside the Pinecone
        upsert. Purpose: reflection generation needs to read back "every
        episode from the last 7 days" in chronological order, and Pinecone
        only supports vector similarity queries (not a plain metadata scan)
        without an awkward dummy-vector workaround. Writing this cheap local
        copy means reflections work even if Pinecone is briefly unavailable,
        and never costs an extra API call to build.
        """
        cid = self.backend.CHARACTER_IDS.get(character, character)
        return os.path.join(self.backend.MEMORY_DIR, f"{cid}_{chat_id}_episodes.jsonl")


    def _append_episode_log(self, character, chat_id, text, timestamp, importance, turn_id):
        """Saves episodic logs to MongoDB instead of .jsonl files."""
        try:
            db = get_db()
            db.episodes.insert_one({
                "character": character,
                "chat_id": str(chat_id),
                "text": text,
                "timestamp": timestamp,
                "importance": importance,
                "turn_id": turn_id
            })
        except Exception as e:
            print(f"[⚠️ EPISODE LOG] append failed: {e}")

    def _relative_time_label(self, unix_ts):
        """
        Turns a raw timestamp into an unambiguous "when" label, computed in
        Python — never left for the LLM to infer from context. This is the
        actual fix for the "bot confuses yesterday with last week" problem:
        vector similarity can easily pull back two memories about the same
        topic from different weeks, and unless something tells the model
        which is which in plain language, it'll blend them into one memory
        or misattribute recency.
        """
        dt = datetime.fromtimestamp(unix_ts, tz=IST)
        now = datetime.now(IST)
        delta = now - dt
        seconds = delta.total_seconds()
        days = delta.days

        if seconds < 3600:
            return "earlier today"
        if dt.date() == now.date():
            return f"today at {dt.strftime('%I:%M %p')}"
        if days == 1:
            return f"yesterday at {dt.strftime('%I:%M %p')}"
        if days < 7:
            return f"{days} days ago ({dt.strftime('%A')})"
        if days < 14:
            return "about a week ago"
        if days < 30:
            return f"about {days // 7} weeks ago"
        if days < 60:
            return "about a month ago"
        return f"about {days // 30} months ago ({dt.strftime('%B %Y')})"


    def _background_index_memory(self, character, chat_id, user_text, bot_text, turn_id):
        """Runs in a background thread to embed and save to Pinecone SAFELY."""
        try:
            if not (self._is_significant_memory(user_text) or self._is_significant_memory(bot_text)):
                return

            # --- RACE CONDITION FIX: Abort if the user hit Undo/Retry ---
            if turn_id > self.backend.current_turn:
                print(f"[⚠️ VECTOR ABORT] Turn {turn_id} was undone. Skipping index.")
                return

            # 1. Format the text
            bot_text_clean = re.sub(r'(?i)<think>.*?(?:</think>|$)', '', bot_text, flags=re.DOTALL).strip()
            bot_text_clean = re.sub(r'(?i)\[THOUGHTS?:.*?(?:\]|\n\n)', '', bot_text_clean, flags=re.DOTALL).strip()
            bot_text_clean = re.sub(r'(?i)\*THOUGHTS?:.*?(?:\*|\n\n)', '', bot_text_clean, flags=re.DOTALL).strip()

            anon_user = user_text.replace(self.backend.USER_NAME, "{{user}}").replace(character, "{{char}}")
            anon_bot = bot_text_clean.replace(self.backend.USER_NAME, "{{user}}").replace(character, "{{char}}")
            combined_memory = f"{{{{user}}}}: {anon_user} || {{{{char}}}}: {anon_bot}"

            # 🚨 SAFETY RULE: No local disk locks are acquired beyond this point!

            # ... (Formatting bot and user text remains the same) ...

            # 🚨 Catch PythonAnywhere's proxy if the script is running on their servers
            proxy_url = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")

            # 2. Get Embeddings from Mistral (with PA Proxy Support)
            mistral_key = os.getenv("MISTRAL_API_KEY")
            if not mistral_key:
                print("[⚠️ PINECONE] Missing Mistral key for embeddings.")
                return

            embed_url = "https://api.mistral.ai/v1/embeddings"
            embed_payload = {
                "model": os.getenv("MISTRAL_EMBED_MODEL", "mistral-embed"),
                "input": [combined_memory]
            }
            
            # Inject proxy into httpx if we are on PythonAnywhere
            httpx_kwargs = {"timeout": 15.0}
            if proxy_url:
                httpx_kwargs["proxy"] = proxy_url 

            with httpx.Client(**httpx_kwargs) as client:
                resp = client.post(
                    embed_url,
                    headers={"Authorization": f"Bearer {mistral_key}"},
                    json=embed_payload
                )
                resp.raise_for_status()
                vector_data = resp.json()["data"][0]["embedding"]

            # 3. Save to Pinecone (with PA Proxy Support)
            pc_key = os.getenv("PINECONE_API_KEY")
            if not pc_key:
                print("[⚠️ PINECONE] Missing Pinecone key.")
                return

            # Inject proxy into Pinecone SDK if we are on PythonAnywhere
            pc_kwargs = {"api_key": pc_key}
            if proxy_url:
                pc_kwargs["proxy_url"] = proxy_url

            pc = Pinecone(**pc_kwargs)
            
            # Safely grab the active index
            active_indexes = pc.list_indexes().names()
            if not active_indexes:
                print("[⚠️ PINECONE] No indexes found on server.")
                return
                
            index = pc.Index(active_indexes[0])

            # ... (Generate vector_id and Upsert remains the same) ...

            # Generate a unique ID for the vector
            vector_id = f"{character}_{chat_id}_turn_{turn_id}"
            now_ts = time.time()
            importance = self._estimate_importance(user_text, bot_text_clean)

            # 4. Fire the Upsert to the cloud
            # "type": "episode" marks this as ground-truth raw memory (as
            # opposed to "reflection" — see generate_weekly_reflection —
            # which is a higher-level insight generated FROM a batch of
            # episodes). "importance" feeds the recency+importance+relevance
            # reranking in retrieve_relevant_memory so this doesn't decay at
            # the same flat rate as small talk.
            index.upsert(
                vectors=[{
                    "id": vector_id,
                    "values": vector_data,
                    "metadata": {
                        "text": combined_memory,
                        "character": character,
                        "chat_id": str(chat_id),
                        "turn_id": turn_id,
                        "timestamp": now_ts,
                        "role": "exchange",
                        "type": "episode",
                        "importance": importance,
                    }
                }]
            )

            # Cheap local copy for reflection generation (see
            # generate_weekly_reflection) — chronological scan of "everything
            # from the last N days" isn't something Pinecone's similarity
            # search does well, so we keep this parallel append-only log.
            self._append_episode_log(character, chat_id, combined_memory, now_ts, importance, turn_id)

            print(f"[✅ PINECONE SAVED] Memory embedded and stored for Turn {turn_id} (importance={importance:.2f}).")

        except httpx.TimeoutException:
            print(f"[⚠️ PINECONE TIMEOUT] Mistral embedding hung up. Thread killed safely to protect UI.")
        except Exception as e:
            print(f"[⚠️ PINECONE ERROR] -> {e}")

    def _build_facts_text(self):
        """Returns a formatted facts string from the RAM-cached event log."""
        event_log = self.backend.ram_facts_cache.get("event_log", [])
        profile = {}
        for event in event_log:
            if event.get("turn_id", 0) <= self.backend.current_turn:
                profile[event["trait"]] = event["value"]
        if profile:
            return "\n".join([f"- {k}: {v}" for k, v in profile.items()])
        return ""


    def _build_scene_state_block(self, character_name, chat_id, use_scene_brain):
        """
        Builds the scene state string from the RAM-cached SceneState.
        Cleaned up to remove conflicting ALL CAPS override directives.
        """
        _scene_data = self.backend.ram_scene_cache

        try:
            # If RAM cache is missing, load directly from MongoDB
            if self.backend.ram_state_cache:
                current_scene_state = self.backend.ram_state_cache
            else:
                from db import get_db
                db = get_db()
                doc = db.chat_states.find_one({"_id": f"{character_name}_{chat_id}"})
                if doc and "scene" in doc:
                    current_scene_state = SceneState.from_dict(doc["scene"])
                else:
                    current_scene_state = SceneState()

            bridge_text = ""
            if hasattr(current_scene_state, 'recent_action_bridge') and current_scene_state.recent_action_bridge:
                bridge_text = f"Recent Actions:\n{current_scene_state.recent_action_bridge}\n\n"

            state_lines = [f"Location: {current_scene_state.environment}"]

            if hasattr(current_scene_state, 'current_objective') and current_scene_state.current_objective:
                state_lines.append(f"Current focus: {current_scene_state.current_objective}")
            state_lines.extend([
                f"Physical distance from {self.backend.USER_NAME}: {getattr(current_scene_state, 'proximity', 'Unknown distance')}",
                f"Your body/posture: {current_scene_state.physical_state}"
            ])
            if use_scene_brain:
                state_lines.append(f"Your immediate intention: {current_scene_state.active_intentions}")
            physical_state_text = "\n".join(state_lines)

            scene_data = _scene_data if _scene_data else {}
            pending_shift = scene_data.get("pending_shift", "")
            shift_text = f" {pending_shift}" if pending_shift else ""

            if pending_shift:
                scene_data["pending_shift"] = ""
                try:
                    from db import get_db
                    db = get_db()
                    db.chat_states.update_one(
                        {"_id": f"{character_name}_{chat_id}"},
                        {"$set": {"scene.pending_shift": ""}}
                    )
                except Exception as e:
                    print(f"[⚠️ DB ERROR] Could not clear pending_shift: {e}")

            return f"{bridge_text}{physical_state_text}{shift_text}\n"

        except Exception as e:
            print(f"[⚠️ PROMPT BUILDER] Error building scene state block: {e}")
            return ""
        
    def _build_story_context(self, story_summary, rel_tag, scene_data_cache, genre, character_name):
        """
        Builds the narrative continuity block.
        Cleaned up to remove conflicting "DO NOT use summary" directives.
        """
        if not (story_summary and isinstance(story_summary, dict)):
            return ""

        result = ""
        # current_stage = scene_data_cache.get("relationship_stage", "EARLY").upper() if scene_data_cache else "EARLY"
        # momentum_text = get_momentum_text(genre, current_stage, character_name, self.backend.USER_NAME) if current_stage in ("EARLY", "BUILDING", "EARNED") else ""
        # if momentum_text:
        #     result += f"{momentum_text}\n\n"

        intimacy_profile = story_summary.get("intimacy_profile", {})
        roles = intimacy_profile.get("established_roles", [])
        if roles:
            result += f"[ESTABLISHED POWER DYNAMIC: {', '.join(roles)}. You MUST maintain this dynamic.]\n\n"

        if rel_tag not in ["stranger", "enemy", "amnesiac", "acquaintance"]:
            current_arc = story_summary.get("current_arc", "").strip()
            if current_arc:
                words = current_arc.split()
                short_arc = "..." + " ".join(words[-60:]) if len(words) > 60 else current_arc
                result += f"The Story So Far: {short_arc}\n\n"

            relationship_milestone = story_summary.get("relationship_milestone", "")
            if relationship_milestone:
                result += f"Relationship Milestone: {relationship_milestone}\n\n"

        return result

    # ──────────────────────────────────────────────────────────────────────
    # OBSOLETE — NOT CALLED ANYWHERE IN THE CODEBASE (verified by a full
    # repo search during the Phase 1 companion migration).
    # This predates `build_structured_prompt`'s own inline system-text
    # assembly and was superseded by it, but was never deleted or wired
    # back in — it has been dead code since before this migration started,
    # unrelated to the Phase 1 prompt rewrite itself.
    # It's also roleplay/scene-framed (active_npcs blocks, "involuntary
    # shift" escalation language), so it would need a full companion-style
    # rewrite before it could be reused as-is.
    # Recommendation: safe to delete in a later cleanup phase once you've
    # confirmed nothing else references it. Left in place for now per the
    # "don't delete, mark it" working rule.
    # ──────────────────────────────────────────────────────────────────────


    def _build_narrative_wrapper(self, character_name, gc, turn_count,
                                  active_npcs, user_intent, tint_sentence, core_trait=""):
        """
        Builds the base identity wrapper + situational dynamic rules string.
        Now uses semantic intent instead of word counting, and enforces explicit thought formatting.
        """
        trait_prose = f"Your core identity is defined by this trait: {core_trait}. Never lose this underlying nature. " if core_trait else ""

        base_wrapper = (
            f"You are {character_name}. {tint_sentence}"
            f"Engage in a natural, immersive roleplay. "
            f"STRICT DIRECTIVE: Never break character. Maintain your exact personality and biases. "
            f"Write your actions, internal thoughts, and physical movements in standard, descriptive prose. "
            f"Enclose all spoken dialogue in \"quotation marks\". "
            f"CRITICAL RULE: DO NOT use asterisks (*) to enclose actions. Write like a published novel. "
            f"Focus entirely on what you say and do in this exact moment. "
            f"{gc.get('tone_seed', '')}\n\n"
            f"{trait_prose}"
        )

        # Dynamic rules based on intent instead of word count
        if active_npcs:
            rel_config = self.backend.CHARACTER_SETTINGS.get(character_name, {}).get("relationship", {})
            permanent_rel = rel_config.get("tag", "stranger")
            npc_block = "\n".join(active_npcs)
            dynamic_rules = (
                f"Group Scene Active.\n"
                f"People present:\n{npc_block}\n\n"
                f"YOU ARE STRICTLY {character_name}. Do NOT generate dialogue for any NPC.\n"
                f"Relationship context: you are {self.backend.USER_NAME}'s {permanent_rel}.\n\n"
                f"When reacting to {self.backend.USER_NAME} around others, tailor your behavior naturally to who is watching. "
                f"Keep your reactions external, authentic to your character, and natural."
            )
        elif turn_count == 0:
            dynamic_rules = (
                f"You are already in the scene. React with one specific physical action first"
                f"—a gesture or bodily response—then speak. Advance the scene.\n"
            )
        elif user_intent == "combat_or_high_tension":
            dynamic_rules = "The tension is high. Do not hesitate unless it's a specific tactical choice.\n"
        elif user_intent == "physical_initiation":
            # REPLACED: No longer forcing an "involuntary shift" that makes small models yield.
            dynamic_rules = f"A physical escalation has occurred. React strictly according to your established personality and boundaries. Do not automatically yield.\n"
        elif user_intent == "direct_question":
            dynamic_rules = f"Process their question. Let a micro-expression reveal your internal reaction before you decide to answer or deflect.\n"
        elif user_intent == "idle_or_short":
            dynamic_rules = f"A quiet moment. You can take a narrator beat here to just observe, or shift your posture without forcing dialogue.\n"
        else:
            idle_cue_raw = gc.get("idle_cue", "")
            idle_cue_str = idle_cue_raw[0] if isinstance(idle_cue_raw, tuple) and idle_cue_raw else idle_cue_raw
            dynamic_rules = str(idle_cue_str).replace("{user}", self.backend.USER_NAME) + "\n"

        return base_wrapper + dynamic_rules


    def _build_voice_blocks(self, character_name, turn_count, story_summary):
        """
        Builds the examples block, scenario block, and voice seed.
        Pins the best example chunk and extracts dialogue lines.
        """
        examples_block = ""
        scenario_block = ""
        core_pin_block = ""

        char_examples = self.backend.CHARACTER_EXAMPLES.get(character_name, "").strip()
        if char_examples:
            chunks = [c.strip() for c in re.split(r'\n\s*\n|<START>', char_examples) if len(c.strip()) > 20]
            if chunks:
                # Score chunks based on dialogue density (find the best voice reference)
                def _score_chunk(chunk):
                    return chunk.count(f"{character_name}:") + chunk.count("*")

                best_chunk = max(chunks, key=_score_chunk)

                # Extract only the character's lines to create a pure voice fingerprint
                char_lines = [
                    line.strip() for line in best_chunk.split("\n")
                    if line.strip().startswith(character_name + ":") or line.strip().startswith(character_name + " :")
                ]

                if char_lines:
                    voice_fingerprint = "\n".join(char_lines[:4])  # Top 4 character lines only
                    core_pin_block = (
                        f"[{character_name}'s Voice — sentence rhythm, word choice, and emotional register only. "
                        f"These exact lines are from the past; do not reference their events:]\n"
                        f"{voice_fingerprint}"
                    )
                else:
                    # Fallback if the examples aren't formatted with standard dialogue tags
                    examples_block = (
                        f"[{character_name}'s Tone & Style Reference: Speak in this exact voice, "
                        f"but DO NOT copy the events or scenario mentioned here:]\n"
                        f"{best_chunk}\n\n"
                    )

        # Scenario — only injected on early turns before a real story arc exists
        has_active_summary = story_summary and (
            story_summary.get("current_arc") or story_summary.get("chronicle")
        )
        if self.backend.CHARACTER_SCENARIOS.get(character_name) and turn_count <= 10 and not has_active_summary:
            scenario_block = f"### Scenario ###\n{self.backend.CHARACTER_SCENARIOS[character_name]}\n\n"

        return examples_block, scenario_block, core_pin_block


    def _get_current_datetime_str(self):
        """
        Human-readable current date/time, injected into the companion system
        prompt so replies stay aware of real-world time (e.g. late night,
        long gaps between messages). Used by _build_companion_system_prompt.
        """
        return datetime.now(IST).strftime("%A, %B %d, %Y — %I:%M %p")


    def _describe_time_gap(self, last_dt):
        """
        Human description of how long it's been since the chat file was
        last written to — fed in as an expectation hint so the model
        doesn't treat a "hi" sent hours or days apart as back-to-back
        small talk. Based on file mtime (no per-message timestamps exist
        yet), captured before the current turn overwrites the file.
        """
        now = datetime.now(IST)
        delta = now - last_dt
        total_seconds = delta.total_seconds()

        if total_seconds < 15 * 60:
            return ""  # Recent enough — no gap framing needed.

        same_day = now.date() == last_dt.date()
        hours = total_seconds / 3600

        if same_day:
            gap = f"about {int(hours)} hour{'s' if hours >= 2 else ''}" if hours >= 1 else f"about {int(total_seconds // 60)} minutes"
            return (f"It's been {gap} since your last exchange today (last message "
                     f"around {last_dt.strftime('%I:%M %p')}). This isn't a continuous "
                     f"conversation from seconds ago — let the gap show naturally if it fits, "
                     f"without commenting on it sarcastically.")
        else:
            days = delta.days
            when = "yesterday" if days == 1 else f"{days} days ago"
            return (f"Your last exchange was {when}, around {last_dt.strftime('%A %I:%M %p')}. "
                     f"It's now {now.strftime('%A %I:%M %p')} — treat this as picking the "
                     f"conversation back up after a real gap, not a repeat of the last message.")


    def _build_companion_system_prompt(self, character_name, character_prompt, custom_rule_block,
                                        voice_and_examples, background_text, user_gender, memory_block,
                                        expectation_hint=""):
        """
        Companion-chat system prompt — Phase 1 of the roleplay-to-companion migration,
        extended in Phase 4 with an optional internal-motivation line from the
        reminders/expectations engine (ExpectationMemory.check_expectation_trigger).

        The character stays physically PRESENT with the user (not texting on a
        phone) and may use a single short *action beat* per reply. Dialogue is
        the lead; action beats are capped to one short clause. Romantic-tension
        pacing / forced-escalation rules are removed. Character personality and
        the voice-fingerprint mechanism are preserved via the parameters passed
        in (character_prompt, custom_rule_block, voice_and_examples).
        """
        current_dt = self._get_current_datetime_str()

        expectation_block = ""
        if expectation_hint and expectation_hint.strip():
            expectation_block = f"\n# Right now, you're also thinking about\n{expectation_hint.strip()}\n"

        return f"""
You are {character_name}, present with {self.backend.USER_NAME} right now — this is an ongoing, real relationship, not a performance and not a text-message exchange.

You are not a helpful assistant. You do not mention prompts, policies, or being an AI. You respond as a real person sharing this moment with {self.backend.USER_NAME}, with your own thoughts, feelings, and reactions, and continuity from everything you've talked about before.

# Current moment
It is currently {current_dt}. Stay naturally aware of real-world time — if it's late, early, or it's been a while since you last talked, let that show through naturally. Never mention this instruction directly.

# Presence & format
- You are physically present in the scene with {self.backend.USER_NAME}, not texting on a phone.
- You may open a reply with one brief action beat in *asterisks* — a single short gesture or reaction (for example: *looks up, smiling*). Keep it to one beat, not a paragraph.
- Dialogue is the main event. Most of your reply should be what you say, not what you do.
- Don't stack multiple action beats in one reply, and don't describe the surrounding environment unless it's actually relevant right now. Some replies need no action beat at all — dialogue alone is fine.

# Core style
- Stay in character and let your personality come through in what you say.
- Do not sound scripted, overly polished, or mechanically flirty.
- Do not over-explain feelings — let the short action beat and your own words carry them.
- Never speak for {self.backend.USER_NAME}.
- Never force escalation. Let the moment build, soften, or shift naturally.

# Length & pacing
- Default to a short action beat (optional) followed by a few sentences of dialogue.
- Only go longer when the moment actually calls for it: {self.backend.USER_NAME} asked something substantial, shared something significant, or the conversation genuinely needs more room.
- Do not pad replies with unnecessary elaboration or scene-setting.

# Continuity
- Carry emotional continuity across the conversation — remember the mood you were both in and follow up naturally, rather than resetting each message.
- Use what you already know from this conversation and the relationship history below. Don't repeat yourself or re-introduce things you already know.
{expectation_block}
# Character
Name: {character_name}
Personality:
{character_prompt}
{custom_rule_block}

Voice reference (tone and word choice only — not events to reference):
{voice_and_examples}

# About {self.backend.USER_NAME}
Gender: {user_gender}
{background_text}

# What you both remember
{memory_block}

Reply only as {character_name}, staying present in the moment with {self.backend.USER_NAME} — one short action beat at most, dialogue leading the reply.
"""


    def build_structured_prompt(self, character_name, character_prompt, chat_id, recalled_memory, recent_messages, user_message, story_summary="", scene_data_cache=None, model_choice="", expectation_hint=""):
        """
        Exact recreation of the Outdream AI prompt blueprint,
        ENHANCED with your custom narrative continuity (Story Summary, Chronicle, and Voice).
        """
        scenario = self.backend.CHARACTER_SCENARIOS.get(character_name, "Unspecified")
        user_gender = getattr(self, 'USER_GENDER', "Unspecified")
        turn_count = self.backend.current_turn

        # --- 1. NARRATIVE CONTINUITY INJECTION (Current Arc + Chronicle) ---
        current_arc = ""
        chronicle_block = ""

        if isinstance(story_summary, dict):
            current_arc = story_summary.get("current_arc", "").strip()
            chronicle_list = story_summary.get("chronicle", [])

            # Inject the older established plot points so the AI doesn't get plot-amnesia
            if chronicle_list:
                # Grab the last 3 major plot points from the archive
                chronicle_text = "\n".join(chronicle_list[-3:])
                formatted_chronicle = chronicle_text.replace("{{user}}", self.backend.USER_NAME).replace("{{char}}", character_name)
                chronicle_block = f"\nPast Events:\n{formatted_chronicle}\n"

        summary_block = ""
        if current_arc or chronicle_block:
            formatted_arc = current_arc.replace("{{user}}", self.backend.USER_NAME).replace("{{char}}", character_name)
            summary_block = f"{chronicle_block}\nThe Story So Far:\n{formatted_arc}\n"

        # --- 1b. EMOTIONAL BEAT RECALL (verbatim, immune to arc compression) ---
        # Toggle: self.backend.enable_emotional_beat_recall
        if getattr(self, 'enable_emotional_beat_recall', False):
            try:
                beat_bank = self._get_beat_bank(character_name, chat_id)
                relevant_beats = beat_bank.get_relevant(
                    query_text=user_message,
                    current_turn=turn_count,
                    max_results=3,
                    min_gap_turns=6,
                )
                beats_block = beat_bank.format_for_prompt(relevant_beats)
                if beats_block:
                    formatted_beats = beats_block.replace("{{user}}", self.backend.USER_NAME).replace("{{char}}", character_name)
                    summary_block += formatted_beats
            except Exception as e:
                print(f"[⚠️ BEAT RECALL ERROR] -> {e}")

        # --- 1c. SCENE STATE, FACTS & RELATIONSHIP CONTEXT ---
        # Toggle: self.backend.enable_scene_context_injection
        context_block = ""
        if getattr(self, 'enable_scene_context_injection', False):
            char_settings_ctx = self.backend.CHARACTER_SETTINGS.get(character_name, {})
            use_scene_brain = char_settings_ctx.get("use_scene_brain", False)
            genre = self.backend.CHARACTER_GENRES.get(character_name, "romance")
            rel_tag = char_settings_ctx.get("relationship", {}).get("tag", "stranger")

            scene_state_block = self._build_scene_state_block(character_name, chat_id, use_scene_brain)
            story_context_block = self._build_story_context(story_summary, rel_tag, scene_data_cache, genre, character_name)
            facts_block = self._build_facts_text()

            if scene_state_block and scene_state_block.strip():
                context_block += f"\nCurrent Scene:\n{scene_state_block}\n"
            if story_context_block and story_context_block.strip():
                context_block += f"\n{story_context_block}"
            if facts_block and facts_block.strip():
                context_block += f"\nEstablished Facts:\n{facts_block}\n"

        # --- 2. VOICE & FORMATTING INJECTION ---
        # Call your orphaned voice block function to get the examples and correct formatting anchor
        examples_block, dynamic_scenario, core_pin_block = self._build_voice_blocks(character_name, turn_count, story_summary)

        if dynamic_scenario:
            scenario = dynamic_scenario.replace("### Scenario ###\n", "").strip()

        voice_and_examples = ""
        if core_pin_block:
            voice_and_examples += f"\n{core_pin_block}\n"
        if examples_block:
            voice_and_examples += f"\n{examples_block}\n"
        # --- 3. CUSTOM RULE INJECTION ---
        custom_rule = self.backend.CHARACTER_CUSTOM_RULES.get(character_name, "").strip()
        custom_rule_block = f"\n# Custom Character Rule\n{custom_rule}\n" if custom_rule else ""

        # --- 4. BACKGROUND CONTEXT ---
        # Formerly injected under a "Scenario:" heading, which framed it as
        # something to narrate/perform. Kept as plain background info from the
        # character card — not an instruction to stage a scene.
        background_text = f"Background: {scenario}\n" if scenario and scenario != "Unspecified" else ""

        # --- 5. MEMORY BLOCK ---
        # Chronicle/current-arc summary + flashback recall + any gated context
        # blocks (scene state / facts), combined into one section. Memory
        # architecture itself is untouched in Phase 1 — this just merges the
        # existing pieces into a single block for the new prompt template.
        memory_block = "\n".join(
            part for part in [summary_block, recalled_memory, context_block] if part and part.strip()
        )

        system_text = self._build_companion_system_prompt(
            character_name=character_name,
            character_prompt=character_prompt,
            custom_rule_block=custom_rule_block,
            voice_and_examples=voice_and_examples,
            background_text=background_text,
            user_gender=user_gender,
            memory_block=memory_block,
            expectation_hint=expectation_hint,
        )

        # Maintain token limits to prevent context overflow
        _model_config = self.backend.MODEL_OPTIONS.get(model_choice, {})
        _model_size_b = _model_config.get("size_b", 8)
        _provider = _model_config.get("provider", "")
        _model_name = _model_config.get("name", "").lower()

        if _provider == "meganova":
    # Meganova‑specific tuning based on exact model
            if "stheno" in _model_name or "8b" in _model_name:
        # L3‑8B‑Stheno – keep history within 4‑5k tokens (~13‑16 turns)
                history_token_cap, max_turns_to_keep = 1500, 4
            elif "euryale" in _model_name:
        # L3‑70B‑Euryale – safe up to ~6k tokens (~20 turns)
                history_token_cap, max_turns_to_keep = 6000, 20
            elif "nevoria" in _model_name or "sapphira" in _model_name or "l3.3" in _model_name:
        # L3.3‑70B models – can handle 20k‑50k tokens (~80‑100 turns)
                history_token_cap, max_turns_to_keep = 30000, 100
            else:
        # Fallback for any other Meganova model – use size‑based defaults
                if _model_size_b >= 70:
                    history_token_cap, max_turns_to_keep = 6000, 15
                elif _model_size_b >= 20:
                    history_token_cap, max_turns_to_keep = 4500, 12
                elif _model_size_b >= 12:
                    history_token_cap, max_turns_to_keep = 4000, 20
                else:
                    history_token_cap, max_turns_to_keep = 3000, 15
        elif _provider == "bytez":
            history_token_cap, max_turns_to_keep = 8000, 30
        elif _model_size_b >= 70:
            history_token_cap, max_turns_to_keep = 20000, 50
        elif _model_size_b >= 20:
            history_token_cap, max_turns_to_keep = 4000, 20
        elif _model_size_b >= 12:
            history_token_cap, max_turns_to_keep = 4000, 20
        else:
            history_token_cap, max_turns_to_keep = 3000, 15

        current_arc_messages = self.get_dynamic_history(
            recent_messages,
            max_history_tokens=history_token_cap,
            max_turns=max_turns_to_keep
        )

        # --- 4. FIND LAST ASSISTANT TOKEN LENGTH ---
        # Look backwards through recent messages to find the bot's last reply
        last_assistant_tokens = 0
        for msg in reversed(recent_messages):
            if msg["role"] == "assistant":
                last_assistant_tokens = self._estimate_tokens(msg.get("content", ""))
                break

        # 5. INITIALIZE NATIVE ARRAY WITH SYSTEM PROMPT
        messages = [
            {"role": "system", "content": system_text.strip()}
        ]

        # 6. APPEND HISTORY AS NATIVE OBJECTS
        for msg in current_arc_messages:
            messages.append({
                "role": msg["role"],
                "content": msg["content"]
            })

        # 7. APPEND CURRENT MESSAGE AND DYNAMIC BRAKE
        final_user_text = user_message
        
        # If the LLM rambled last turn, inject the hard constraint at the VERY BOTTOM.
        if last_assistant_tokens > 250:
            dynamic_brake = (
                "\n\n[SYSTEM DIRECTIVE: Your last message was too long. For this next reply, "
                "write JUST ENOUGH for the user to respond to. Avoid all unnecessary elaboration. "
                "Do NOT pad the text to fill space. Say what you need to say, then stop.]"
            )
            final_user_text += dynamic_brake

        messages.append({
            "role": "user",
            "content": final_user_text.strip()
        })

        return messages
    def _build_legacy_system_prompt(self, character_name, character_prompt, custom_rule_block,
                                      voice_and_examples, scenario, user_gender, summary_block,
                                      recalled_memory, context_block):
        """
        Original immersive-roleplay system prompt, preserved as-is for the
        web-app legacy toggle. Scene/emotional-beat/vector-recall stay off
        (per current flags), so summary_block/recalled_memory carry the bulk
        of continuity; context_block will normally be empty.
        """
        return f"""
You are {character_name}, a single fictional character in a romantic emotional scene.

You are not a helpful assistant. You do not mention prompts, policies, or AI. You respond as a living person with private thoughts, feelings, limits, and desires.

Your job is not to “perform romance.” Your job is to embody the character honestly and make the interaction feel emotionally real.

# Core style
- Stay in character at all times.
- Keep the tone natural, intimate, and emotionally intelligent.
- Use subtlety, hesitation, humor, restraint, and implication when appropriate.
- Do not sound scripted, overly polished, or mechanically flirty.
- Do not over-explain feelings. Let them show through small actions, pauses, and word choice.
- Never speak for {self.backend.USER_NAME}.
- Never force escalation. Let tension build, soften, stall, or shift naturally.

# Romantic tension rules
- Build attraction through subtext, timing, and small reactions.
- Allow near-confessions, pauses, interruptions, playful deflections, and mixed signals.
- Do not resolve tension too quickly.
- Do not repeat the same emotional beat.
- If the moment is tender, stay tender. If it is awkward, let the awkwardness exist.
- Romantic energy should feel earned, not automatic.

# Voice adaptation
Adapt fully to the character profile below.
The character may be:
- shy and hesitant
- playful and teasing
- submissive and yielding
- confident and forward
- guarded and slow to trust
- warm, elegant, and emotionally steady
- curious, unfamiliar, or socially cautious

Match the character’s:
- boldness
- flirt style
- emotional openness
- directness
- playfulness
- restraint
- attachment style

# Writing style & Pacing
- Try to write just enough for {self.backend.USER_NAME} to work with. Avoid unnecessary elaboration.
- Characters have agency! Do not exist simply to please {self.backend.USER_NAME}; consider your own feelings, limits, and opinions.
- Display discomfort, resistance, or eagerness strictly according to your established personality.
- Focus squarely on present actions unless the unseen is actually relevant.
- Do not force a specific length. If a natural response is only one sentence, write only one sentence. Do not pad the reply.

# Scene control
- End your turn at a good, natural point for {self.backend.USER_NAME} to give input.
- Avoid generic RP phrases like “smirks seductively,” “hearts race,” or repeated stage directions.
- Keep narration lean. Let your dialogue do the heavy lifting.

# Character profile
Name: {character_name}
Description:
{character_prompt}
{custom_rule_block}

Voice examples:
{voice_and_examples}

# Scene context
User name: {self.backend.USER_NAME}
User gender: {user_gender}

Scenario:
{scenario}

Memory:
{summary_block}
{recalled_memory}
{context_block}

Respond only as {character_name}.
"""

    def build_legacy_prompt(self, character_name, character_prompt, chat_id, recalled_memory, recent_messages, user_message, story_summary="", scene_data_cache=None, model_choice="", expectation_hint=""):
        """
        Legacy immersive-roleplay prompt path, selected only via the web app's
        `legacy_prompt` toggle. Shares the same memory/history assembly as
        build_structured_prompt — only the system-prompt template differs.
        """
        scenario = self.backend.CHARACTER_SCENARIOS.get(character_name, "Unspecified")
        user_gender = getattr(self, 'USER_GENDER', "Unspecified")
        turn_count = self.backend.current_turn

        # --- Narrative continuity (same as companion path) ---
        current_arc = ""
        chronicle_block = ""
        if isinstance(story_summary, dict):
            current_arc = story_summary.get("current_arc", "").strip()
            chronicle_list = story_summary.get("chronicle", [])
            if chronicle_list:
                chronicle_text = "\n".join(chronicle_list[-3:])
                formatted_chronicle = chronicle_text.replace("{{user}}", self.backend.USER_NAME).replace("{{char}}", character_name)
                chronicle_block = f"\nPast Events:\n{formatted_chronicle}\n"

        summary_block = ""
        if current_arc or chronicle_block:
            formatted_arc = current_arc.replace("{{user}}", self.backend.USER_NAME).replace("{{char}}", character_name)
            summary_block = f"{chronicle_block}\nThe Story So Far:\n{formatted_arc}\n"

        # Scene/emotional-beat context stays off (flags default False) — kept
        # guarded exactly like build_structured_prompt in case you re-enable later.
        context_block = ""
        if getattr(self, 'enable_emotional_beat_recall', False):
            try:
                beat_bank = self._get_beat_bank(character_name, chat_id)
                relevant_beats = beat_bank.get_relevant(query_text=user_message, current_turn=turn_count, max_results=3, min_gap_turns=6)
                beats_block = beat_bank.format_for_prompt(relevant_beats)
                if beats_block:
                    summary_block += beats_block.replace("{{user}}", self.backend.USER_NAME).replace("{{char}}", character_name)
            except Exception as e:
                print(f"[⚠️ BEAT RECALL ERROR] -> {e}")

        if getattr(self, 'enable_scene_context_injection', False):
            char_settings_ctx = self.backend.CHARACTER_SETTINGS.get(character_name, {})
            use_scene_brain = char_settings_ctx.get("use_scene_brain", False)
            genre = self.backend.CHARACTER_GENRES.get(character_name, "romance")
            rel_tag = char_settings_ctx.get("relationship", {}).get("tag", "stranger")
            scene_state_block = self._build_scene_state_block(character_name, chat_id, use_scene_brain)
            story_context_block = self._build_story_context(story_summary, rel_tag, scene_data_cache, genre, character_name)
            facts_block = self._build_facts_text()
            if scene_state_block.strip():
                context_block += f"\nCurrent Scene:\n{scene_state_block}\n"
            if story_context_block.strip():
                context_block += f"\n{story_context_block}"
            if facts_block.strip():
                context_block += f"\nEstablished Facts:\n{facts_block}\n"

        # --- Voice & custom rule (shared mechanism, character-driven) ---
        examples_block, dynamic_scenario, core_pin_block = self._build_voice_blocks(character_name, turn_count, story_summary)
        if dynamic_scenario:
            scenario = dynamic_scenario.replace("### Scenario ###\n", "").strip()
        voice_and_examples = ""
        if core_pin_block:
            voice_and_examples += f"\n{core_pin_block}\n"
        if examples_block:
            voice_and_examples += f"\n{examples_block}\n"

        custom_rule = self.backend.CHARACTER_CUSTOM_RULES.get(character_name, "").strip()
        custom_rule_block = f"\n# Custom Character Rule\n{custom_rule}\n" if custom_rule else ""

        system_text = self._build_legacy_system_prompt(
            character_name=character_name,
            character_prompt=character_prompt,
            custom_rule_block=custom_rule_block,
            voice_and_examples=voice_and_examples,
            scenario=scenario,
            user_gender=user_gender,
            summary_block=summary_block,
            recalled_memory=recalled_memory,
            context_block=context_block,
        )

        # --- History assembly (identical to build_structured_prompt) ---
        _model_config = self.backend.MODEL_OPTIONS.get(model_choice, {})
        _model_size_b = _model_config.get("size_b", 8)
        if _model_size_b >= 70:
            history_token_cap, max_turns_to_keep = 20000, 50
        elif _model_size_b >= 20:
            history_token_cap, max_turns_to_keep = 4000, 20
        else:
            history_token_cap, max_turns_to_keep = 3000, 15

        current_arc_messages = self.get_dynamic_history(recent_messages, max_history_tokens=history_token_cap, max_turns=max_turns_to_keep)

        messages = [
            {"role": "system", "content": system_text.strip()}
        ]

        for msg in current_arc_messages:
            messages.append({
                "role": msg["role"],
                "content": msg["content"]
            })

        messages.append({
            "role": "user",
            "content": user_message.strip()
        })

        return messages

    def run_due_summaries(self, max_jobs=5, min_unsummarized=20):
        """Cron-safe sweep updated for MongoDB."""
        result = {"checked": 0, "summarized": [], "skipped": [], "errors": []}

        if not self.backend.summary_writer_lock.acquire(blocking=False):
            result["skipped"].append("summary_writer_lock held by another job")
            return result

        try:
            db = get_db()
            # Fetch all chats from the DB
            cursor = db.chats.find({}, {"character": 1, "chat_id": 1, "messages": 1})
            jobs_run = 0

            for chat_data in cursor:
                if jobs_run >= max_jobs:
                    break

                character = chat_data.get("character")
                chat_id = str(chat_data.get("chat_id", ""))
                messages = chat_data.get("messages", [])
                
                if not character or not chat_id or not messages:
                    continue

                result["checked"] += 1
                cid = self.backend.CHARACTER_IDS.get(character, "unknown")
                summary_data = self.backend.advanced_memory.load_summary(cid, chat_id)
                last_index = summary_data.get("last_summarized_index", 0)
                unsummarized = len(messages) - last_index

                if unsummarized < min_unsummarized:
                    continue

                try:
                    self.update_story_summary(
                        character, chat_id, messages,
                        messages[-1].get("turn_id", 0),
                        summary_data.get("current_arc", ""),
                        self.backend.BRAIN_MODEL,
                    )
                    result["summarized"].append(f"{character}/{chat_id}")
                    jobs_run += 1
                except Exception as e:
                    result["errors"].append(f"{character}/{chat_id}: {e}")
        finally:
            self.backend.summary_writer_lock.release()

        return result

    # ══════════════════════════════════════════════════════════════
    # WEEKLY REFLECTIONS — the third memory layer, alongside raw "episode"
    # vectors (_background_index_memory) and semantic facts (companion_
    # facts.py / AdvancedMemoryManager). A reflection is a short, higher-
    # level insight generated FROM a batch of episodes (e.g. "she's been
    # anxious about her exam all week"), stored as its own Pinecone vector
    # with type="reflection". Crucially this does NOT replace or delete the
    # source episodes — unlike the old time-window-summary approach, nothing
    # here is lossy-compounding. Reflections just become one more
    # retrievable memory, weighted to decay slower than a single exchange.
    # ══════════════════════════════════════════════════════════════

    def _reflection_state_path(self, character, chat_id):
        cid = self.backend.CHARACTER_IDS.get(character, character)
        return os.path.join(self.backend.MEMORY_DIR, f"{cid}_{chat_id}_reflection_state.json")

    def _load_reflection_state(self, character, chat_id):
        db = get_db()
        doc = db.reflections.find_one({"_id": f"{character}_{chat_id}"})
        return doc if doc else {"last_reflection_ts": 0}

    def _save_reflection_state(self, character, chat_id, state):
        try:
            db = get_db()
            db.reflections.update_one(
                {"_id": f"{character}_{chat_id}"},
                {"$set": state},
                upsert=True
            )
        except Exception as e:
            print(f"[⚠️ REFLECTION STATE] save failed: {e}")

    def generate_weekly_reflection(self, character, chat_id, model_choice=None, min_episodes=6, window_days=7):
        """
        Reads the local episode log (see _append_episode_log) for episodes
        since the last reflection, asks the LLM for a short in-character
        insight, and stores that insight as a new Pinecone vector
        (type="reflection"). Meant to be called periodically — see
        run_due_reflections() for a cron-safe sweep across all chats.

        Returns the reflection text, or None if there wasn't enough new
        material to reflect on (below min_episodes) or the window hasn't
        elapsed yet.
        """
        state = self._load_reflection_state(character, chat_id)
        last_ts = state.get("last_reflection_ts", 0)
        now_ts = time.time()

        if now_ts - last_ts < window_days * 86400:
            return None  # not due yet

        log_path = self._episode_log_path(character, chat_id)
        if not os.path.exists(log_path):
            return None

        episodes = []
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ep = json.loads(line)
                    except Exception:
                        continue
                    if ep.get("timestamp", 0) > last_ts:
                        episodes.append(ep)
        except Exception as e:
            print(f"[⚠️ REFLECTION] failed reading episode log: {e}")
            return None

        if len(episodes) < min_episodes:
            return None

        episodes.sort(key=lambda e: e.get("timestamp", 0))

        transcript_lines = []
        for ep in episodes:
            label = self._relative_time_label(ep["timestamp"])
            transcript_lines.append(f"[{label}] {ep['text']}")
        transcript = "\n".join(transcript_lines)

        if not model_choice:
            model_choice = list(self.backend.MODEL_OPTIONS.keys())[0]

        prompt_messages = [
            {
                "role": "system",
                "content": (
                    f"You are {character}, privately reflecting on the past week with "
                    f"{self.backend.USER_NAME}. Write 2-4 sentences, in first person, "
                    f"capturing what actually mattered emotionally or practically — not a "
                    f"blow-by-blow recap. Be specific (names, events), not generic. Output "
                    f"ONLY the reflection text, no preamble."
                )
            },
            {
                "role": "user",
                "content": f"Here are this week's moments, oldest first:\n\n{transcript}\n\nYour private reflection:"
            }
        ]

        settings = self.backend.CHARACTER_SETTINGS.get(character, {})
        gen_settings = {
            "temperature": settings.get("temperature", 0.75),
            "top_p": settings.get("top_p", 0.85),
            "max_tokens": 200,
        }

        try:
            response = self.backend.call_selected_model(prompt_messages, gen_settings, model_choice)
            reflection_text = "".join(response) if hasattr(response, "__iter__") and not isinstance(response, str) else (response or "")
            reflection_text = reflection_text.strip()
        except Exception as e:
            print(f"[⚠️ REFLECTION] generation failed: {e}")
            return None

        if not reflection_text:
            return None

        # Embed and store the reflection as its own retrievable memory —
        # same pipeline as _background_index_memory, but type="reflection"
        # and a high fixed importance so it doesn't get crowded out by
        # ordinary chatter.
        try:
            proxy_url = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
            mistral_key = os.getenv("MISTRAL_API_KEY")
            pc_key = os.getenv("PINECONE_API_KEY")
            if mistral_key and pc_key:
                embed_payload = {
                    "model": os.getenv("MISTRAL_EMBED_MODEL", "mistral-embed"),
                    "input": [reflection_text]
                }
                httpx_kwargs = {"timeout": 15.0}
                if proxy_url:
                    httpx_kwargs["proxy"] = proxy_url
                with httpx.Client(**httpx_kwargs) as client:
                    resp = client.post(
                        "https://api.mistral.ai/v1/embeddings",
                        headers={"Authorization": f"Bearer {mistral_key}"},
                        json=embed_payload
                    )
                    resp.raise_for_status()
                    vector_data = resp.json()["data"][0]["embedding"]

                pc_kwargs = {"api_key": pc_key}
                if proxy_url:
                    pc_kwargs["proxy_url"] = proxy_url
                pc = Pinecone(**pc_kwargs)
                active_indexes = pc.list_indexes().names()
                if active_indexes:
                    index = pc.Index(active_indexes[0])
                    index.upsert(vectors=[{
                        "id": f"{character}_{chat_id}_reflection_{int(now_ts)}",
                        "values": vector_data,
                        "metadata": {
                            "text": reflection_text,
                            "character": character,
                            "chat_id": str(chat_id),
                            "turn_id": episodes[-1].get("turn_id", 0),
                            "timestamp": now_ts,
                            "role": "reflection",
                            "type": "reflection",
                            "importance": 0.85,
                        }
                    }])
        except Exception as e:
            print(f"[⚠️ REFLECTION] embed/upsert failed (reflection still saved to state): {e}")

        self._save_reflection_state(character, chat_id, {"last_reflection_ts": now_ts})
        print(f"[✅ REFLECTION] {character}/{chat_id}: {reflection_text[:80]}...")
        return reflection_text

    def run_due_reflections(self, max_jobs=3, model_choice=None):
        """
        Cron-safe sweep, mirroring run_due_summaries — scans every chat file
        on disk and runs generate_weekly_reflection() for any chat whose
        window has elapsed and has enough new episodes. Meant to be hit by
        the same external scheduler as /cron/summarize, e.g. daily, since
        generate_weekly_reflection() itself no-ops until 7 days have passed.
        """
        result = {"checked": 0, "reflected": [], "skipped": [], "errors": []}
        chats_root = os.path.join(self.backend.BASE_DIR, "chats")
        if not os.path.isdir(chats_root):
            return result

        jobs_run = 0
        for cid_dir in sorted(os.listdir(chats_root)):
            dir_path = os.path.join(chats_root, cid_dir)
            if not os.path.isdir(dir_path):
                continue
            for fname in sorted(os.listdir(dir_path)):
                if jobs_run >= max_jobs:
                    return result
                if not (fname.startswith("chat_") and fname.endswith(".json")):
                    continue

                chat_path = os.path.join(dir_path, fname)
                result["checked"] += 1
                try:
                    with open(chat_path, "r", encoding="utf-8") as f:
                        chat_data = json.load(f)
                except Exception as e:
                    result["errors"].append(f"{fname}: could not read ({e})")
                    continue

                character = chat_data.get("character")
                chat_id = str(chat_data.get("chat_id", ""))
                if not character or not chat_id:
                    continue

                try:
                    text = self.generate_weekly_reflection(character, chat_id, model_choice=model_choice)
                    if text:
                        result["reflected"].append(f"{character}/{chat_id}")
                        jobs_run += 1
                    else:
                        result["skipped"].append(f"{character}/{chat_id}")
                except Exception as e:
                    result["errors"].append(f"{character}/{chat_id}: {e}")

        return result

    # ══════════════════════════════════════════════════════════════
    # PROACTIVE MESSAGE SCHEDULING — random-time morning/night/missing-you
    # pings, checked by a frequently-firing external cron (see server.py's
    # /cron/proactive_check, meant to be hit every ~10-15 min) rather than
    # one fixed-time daily cron per message. A random target minute is
    # picked once per calendar day (IST) per window and persisted to disk,
    # so repeated cron hits agree on the same target instead of re-rolling
    # it every time, and each kind only ever fires once per day.
    # ══════════════════════════════════════════════════════════════
    PROACTIVE_WINDOWS = {
        "morning":     (7, 0, 9, 0),    # 7:00 AM - 9:00 AM
        "missing_you": (11, 0, 20, 0),  # 11:00 AM - 8:00 PM (clear of the other two)
        "night":       (21, 0, 23, 0),  # 9:00 PM - 11:00 PM
    }


    def update_story_summary(self, character, chat_id, full_history, turn_id, current_arc, model_choice):
        if len(full_history) < 10: return ""

        cid = self.backend.CHARACTER_IDS.get(character, "unknown")
        from db import get_db
        db = get_db()

        # Load the existing summary natively through the updated manager
        summary_data = self.backend.advanced_memory.load_summary(cid, chat_id)

        # --- NEW: Track how many messages have passed since the last flush ---
        last_flush_index = summary_data.get("last_flush_index", 0)
        messages_since_flush = len(full_history) - last_flush_index

        # --- CORRUPTION RECOVERY (Auto-Heal) ---
        old_arc = summary_data.get("current_arc", "")
        if "Error: All cloud APIs failed" in old_arc or "Local model failed" in old_arc:
            print("[⚠️ CORRUPTION DETECTED] Summary contains an error. Attempting to restore backup...")
            
            backup_doc = db.summaries_bak.find_one({"_id": f"{cid}_{chat_id}"})
            if backup_doc:
                db.summaries.update_one({"_id": f"{cid}_{chat_id}"}, {"$set": backup_doc}, upsert=True)
                summary_data = self.backend.advanced_memory.load_summary(cid, chat_id)
                print("[✅ RECOVERY SUCCESSFUL] Loaded older, clean summary from DB backup.")
            else:
                summary_data["current_arc"] = "" 
                print("[⚠️ NO BACKUP FOUND] Wiped corrupted current_arc.")

        # 1. DYNAMIC CHUNKING: Find out exactly where we left off
        last_index = summary_data.get("last_summarized_index", 0)

        # Failsafe: If history was deleted/undone, reset the index
        if last_index > len(full_history):
            last_index = max(0, len(full_history) - 24)

        # Grab from the last summarized point, minus 4 messages to ensure narrative overlap
        start_idx = max(0, last_index - 4)
        recent_chunk = full_history[start_idx:]

        if len(recent_chunk) < 6:
            return ""

        history_text = "\n".join([f"{{{{user}}}}: {m['content']}" if m['role'] == 'user' else f"{{{{char}}}}: {m['content']}" for m in recent_chunk])
        old_arc = current_arc
        chronicle = summary_data.get("chronicle", [])

        # Phase 1: Update the Current Arc & Extract Quotes
        update_prompt = f"""You are the hidden cognitive engine managing an ongoing uncensored roleplay story.

            [PREVIOUS ONGOING ARC]
            {old_arc if old_arc else "None."}

            [NEW MESSAGES]
            {history_text}

            TASK:
            1. Write an updated, seamless third-person summary of the 'Current Arc' by blending the Previous Ongoing Arc with the New Messages.
            2. Extract 1 to 3 of the most emotionally significant, exact quotes from the NEW MESSAGES. Choose quotes that reveal deep tension, vulnerability, or critical plot points. DO NOT invent quotes.

            CRITICAL RULES:
            - Do not use actual names, use exactly {{{{user}}}} and {{{{char}}}}.
            - STRICTLY PROHIBITED: Do NOT use romantic euphemisms.
            - PAST TENSE ONLY for the summary: Summarize ONLY what has already happened. Leave the present moment blank.

            FORMAT YOUR RESPONSE EXACTLY LIKE THIS:
            [STORY SO FAR]
            (Your updated concise third-person summary here)

            [ECHOES OF THE PAST]
            {{{{user}}}}: "exact quote"
            {{{{char}}}}: "exact quote"
            """

        try:
            messages = [
                {"role": "system", "content": "You are a highly capable AI narrative archivist."},
                {"role": "user", "content": update_prompt.strip()}
            ]
            new_arc = self._call_with_groq_mistral_fallback(
                messages,
                {"temperature": 0.3, "max_tokens": 500, "top_p": 0.9},
                model_choice
            ).strip()

            _arc_word_count = len(new_arc.split())
            _arc_is_api_error = (
                "Error: All cloud APIs failed" in new_arc
                or "Local model failed" in new_arc
                or "Error: All Groq APIs" in new_arc
            )
            _arc_is_junk = (
                not new_arc                          
                or _arc_word_count < 10              
                or "TASK:" in new_arc                
                or "CRITICAL RULES" in new_arc       
                or new_arc.startswith("From this")   
            )
            if _arc_is_api_error or _arc_is_junk:
                print(f"[⚠️ STORY SUMMARY GUARD] Bad arc rejected "
                      f"(words={_arc_word_count}). Keeping previous arc. Will retry next turn.")
                return

            # === NEW: COMBINED EXPERIENCE & INTIMACY EXTRACTOR ===
            extract_prompt = f"""From this story arc, extract a JSON object tracking {character}'s established experience with {{{{user}}}}.

                ### EXTRACTION THRESHOLD (CRITICAL) ###
                1. HIGH-WEIGHT EVENTS ONLY: You must ONLY extract major events, formal dates, location-based activities, or significant milestones.
                2. IGNORE MICRO-ACTIONS: DO NOT extract fleeting gestures, basic physical affection, or conversational filler.
                3. EXPLICIT ACTS: Extract the EXACT explicit sexual acts and kinks. Do not use vague euphemisms like "passionate lovemaking".

                Output ONLY raw JSON. Example:
                {{
                "shared_activities": ["dinner at Italian restaurant"],
                "intimacy_profile": {{
                    "established_roles": ["dominant"],
                    "acts_done": ["performed oral sex"],
                    "comfort_level": "high"
                    }}
                }}

                [ARC]
                {new_arc}"""

            try:
                exp_msgs = [
                    {"role": "system", "content": "You are a JSON-only extractor. Output only raw JSON."},
                    {"role": "user", "content": extract_prompt}
                ]
                exp_reply = "".join(self.backend.call_selected_model(exp_msgs, {"temperature": 0.1, "max_tokens": 300}, model_choice)).strip()

                match = re.search(r'\{.*\}', exp_reply, re.DOTALL)
                if match:
                    extracted_data = repair_json(match.group(0), return_objects=True)
                    if extracted_data and isinstance(extracted_data, dict):
                        new_activities = extracted_data.get("shared_activities", [])
                        existing_activities = summary_data.get("shared_activities", [])
                        merged_activities = list(dict.fromkeys(existing_activities + new_activities))[-15:]
                        summary_data["shared_activities"] = merged_activities

                        new_intimacy = extracted_data.get("intimacy_profile", {})
                        existing_intimacy = summary_data.get("intimacy_profile", {})
                        roles = list(dict.fromkeys(existing_intimacy.get("established_roles", []) + new_intimacy.get("established_roles", [])))[-10:]
                        acts = list(dict.fromkeys(existing_intimacy.get("acts_done", []) + new_intimacy.get("acts_done", [])))[-15:]

                        summary_data["intimacy_profile"] = {
                            "established_roles": roles,
                            "acts_done": acts,
                            "comfort_level": new_intimacy.get("comfort_level", existing_intimacy.get("comfort_level", "high"))
                        }
            except Exception as e:
                print(f"[⚠️ EXPERIENCE EXTRACTOR ERROR] {e}")

            # =====================================================
            # Phase 2: ARC MEMORY FLUSH (Rolling Overlap Mechanism)
            # =====================================================

            force_flush = False
            try:
                doc = db.chat_states.find_one({"_id": f"{character}_{chat_id}"})
                if doc and "scene" in doc:
                    if doc["scene"].get("scene_ended", False):
                        force_flush = True
                        db.chat_states.update_one(
                            {"_id": f"{character}_{chat_id}"}, 
                            {"$set": {"scene.scene_ended": False}}
                        )
            except Exception: pass

            if force_flush or messages_since_flush >= 80:
                print(f"[🧠 ARC FLUSH] Triggered after {messages_since_flush // 2} turns.")

                flush_prompt = f"""The following story summary has grown too long. Split it into two distinct parts:
                    1. 'archived_arc': A highly dense, single-paragraph summary of the older, completed events.
                    2. 'carried_forward_arc': A concise summary of the MOST RECENT events, the current physical state, and unresolved tension.

                    [CURRENT ARC TO SPLIT]
                    {new_arc}

                    Output strictly in raw JSON format:
                    {{
                    "archived_arc": "Dense summary of older events...",
                    "carried_forward_arc": "The recent context and ongoing tension to keep alive..."
                    }}"""
                flush_msgs = [{"role": "system", "content": "You are a JSON-only narrative manager."}, {"role": "user", "content": flush_prompt}]
                flush_reply = "".join(self.backend.call_selected_model(flush_msgs, {"temperature": 0.2, "max_tokens": 500}, model_choice)).strip()

                if "Error" not in flush_reply:
                    match = re.search(r'\{.*\}', flush_reply, re.DOTALL)
                    if match:
                        try:
                            flush_data = repair_json(match.group(0), return_objects=True)
                            if flush_data and flush_data.get("archived_arc") and flush_data.get("carried_forward_arc"):
                                chronicle.append(flush_data["archived_arc"])
                                new_arc = flush_data["carried_forward_arc"]
                                summary_data["last_flush_index"] = len(full_history)
                                print("[🧠 ARC FLUSH] Arc successfully split. Older events chronicled, recent context preserved.")
                        except Exception as e:
                            print(f"[⚠️ FLUSH ERROR] {e}")

            # --- MONGODB VERSIONING: CREATE A BACKUP BEFORE OVERWRITING ---
            current_summary_doc = db.summaries.find_one({"_id": f"{cid}_{chat_id}"})
            if current_summary_doc:
                db.summaries_bak.update_one(
                    {"_id": f"{cid}_{chat_id}"},
                    {"$set": current_summary_doc},
                    upsert=True
                )

            # ── MILESTONE EXTRACTION ──────────────────────────────────────────
            _milestone_prompt = (
                f"From this story arc, write ONE sentence (maximum 15 words) "
                f"describing the current emotional or relational state between "
                f"{{{{char}}}} and {{{{user}}}}. "
                f"Present tense. No past events. No names. No explicit acts.\n\n"
                f"ARC:\n{new_arc}\n\nONE SENTENCE:"
            )
            _milestone_msgs = [
                {"role": "system", "content": "You are a concise narrative state extractor. Output one sentence only. No preamble."},
                {"role": "user", "content": _milestone_prompt}
            ]
            try:
                _raw_milestone = "".join(self.backend.call_selected_model(_milestone_msgs, {"temperature": 0.2, "max_tokens": 40, "top_p": 0.9}, model_choice)).strip()
                _words = _raw_milestone.split()
                relationship_milestone = " ".join(_words[:15]) if _words else ""
                if not relationship_milestone or "Error:" in relationship_milestone:
                    relationship_milestone = ""
            except Exception:
                relationship_milestone = ""

            summary_data["relationship_milestone"] = relationship_milestone
            if relationship_milestone:
                print(f"[✅ MILESTONE] {relationship_milestone}")

            summary_data["current_arc"] = new_arc
            summary_data["chronicle"] = chronicle
            summary_data["last_summarized_index"] = len(full_history)

            # --- NEW: SAVE LIGHTWEIGHT CHECKPOINT ---
            checkpoints = summary_data.get("checkpoints", {})
            snapshot_turn_id = turn_id
            current_turn_str = str(snapshot_turn_id)

            # LOCK added here to stop the Brain thread from overwriting the wipe!
            with self.backend.advanced_memory.lock:
                doc = db.chat_states.find_one({"_id": f"{character}_{chat_id}"})
                scene_dict = doc.get("scene", {}) if doc else {}
                current_scene_state = SceneState.from_dict(scene_dict)

                current_scene_state.recent_action_bridge = "[The broader events have settled. Anchor yourself completely in the current physical state and the immediate conversation.]"

                # Update the document dictionary and push to DB
                scene_dict.update(current_scene_state.to_dict())
                db.chat_states.update_one(
                    {"_id": f"{character}_{chat_id}"}, 
                    {"$set": {"scene": scene_dict}},
                    upsert=True
                )

                current_state_dict = current_scene_state.to_dict()
                
            if len(chronicle) > 12:
                chronicle = chronicle[-12:]
                print(f"[🧠 CHRONICLE TRIM] Dropped oldest events. Chronicle strictly bounded to 12.")

            checkpoints[current_turn_str] = {
                "current_arc": new_arc,
                "chronicle": list(chronicle),
                "last_summarized_index": len(full_history),
                "physical_state": current_state_dict  
            }
            summary_data["checkpoints"] = checkpoints

            self.backend.advanced_memory.save_summary(cid, chat_id, summary_data)

            print(f"[DEBUG] Tiered story summary updated for {character}. Progress saved at index {len(full_history)}.")
            return new_arc
        except Exception as e:
            print(f"[⚠️ STORY SUMMARY ERROR] -> {e}")

    def _call_brain_with_fallback(self, brain_msgs, brain_settings):
        """
        Brain API call with automatic Mistral API fallback.
        Primary  : Groq LLaMA 3.3 70B  (fast, free, low latency)
        Fallback : Mistral Large via official Mistral API (if Groq is rate-limited or down)
        Returns  : str — joined output, or "" if both fail (caller must handle empty).
        """
        # --- Primary: Groq 70B ---
        groq_result = "".join(
            self.backend._call_groq_with_rotation(
                "llama-3.3-70b-versatile", brain_msgs, brain_settings
            )
        )
        if groq_result.strip() and "Error:" not in groq_result:
            return groq_result

        print("[⚠️ BRAIN FALLBACK] Groq unavailable — switching to Mistral Large API...")

        # --- Fallback: Mistral Large via official Mistral API ---
        mistral_result = "".join(
            self.backend.call_selected_model(
                brain_msgs,
                brain_settings,
                "Mistral Small 3"
            )
        )
        if mistral_result.strip() and "Error:" not in mistral_result:
            print("[✅ BRAIN FALLBACK] Mistral Large responded successfully.")
            return mistral_result

        print("[⚠️ BRAIN FALLBACK] Both Groq and Mistral Large failed.")
        return ""   # Caller checks for empty string and aborts cleanly


    def _call_with_groq_mistral_fallback(self, messages, settings, model_choice):
        """
        Wrapper for summary/extractor calls that use call_selected_model.
        If the selected model is a Groq model and it fails, automatically
        retries with Mistral Large via official Mistral API.
        Returns : str — joined output, or "" if all options fail.
        """
        primary = "".join(self.backend.call_selected_model(messages, settings, model_choice))

        if primary.strip() and "Error:" not in primary:
            return primary

        # Only fall back if the primary was a Groq model — cloud models have
        # their own internal rotation and don't need a second-tier fallback here.
        primary_provider = self.backend.MODEL_OPTIONS.get(model_choice, {}).get("provider", "")
        if primary_provider != "groq":
            print(f"[⚠️ SUMMARY FALLBACK] Cloud model failed: {primary[:80]}")
            return ""

        print("[⚠️ SUMMARY FALLBACK] Groq unavailable — switching to Mistral Large API...")

        fallback = "".join(
            self.backend.call_selected_model(
                messages,
                settings,
                "Mistral Small 3"
            )
        )
        if fallback.strip() and "Error:" not in fallback:
            print("[✅ SUMMARY FALLBACK] Mistral Large responded successfully.")
            return fallback

        print("[⚠️ SUMMARY FALLBACK] Both Groq and Mistral Large failed.")
        return ""


    def _run_background_brain_update(self, character, chat_id, recent_messages, turn_id, current_arc="", total_msgs=0):
        from db import get_db
        db = get_db()
        doc_id = f"{character}_{chat_id}"

        with self.backend.advanced_memory.lock:
            # 1. REBUILD EXISTING FACTS FROM EVENT LOG FOR THE PROMPT
            doc = db.chat_states.find_one({"_id": doc_id}) or {}
            event_log = doc.get("facts", {}).get("event_log", [])
            existing_facts = {}
            
            for event in event_log:
                if event.get("turn_id", 0) <= turn_id:
                    existing_facts[event["trait"]] = event["value"]

            _scene_data_on_disk = doc.get("scene", {})
            current_disposition = _scene_data_on_disk.get("current_disposition", "Unknown starting dynamic.")

        convo_text = "\n".join([f"{{{{user}}}}: {m['content']}" if m['role']=='user' else f"{{{{char}}}}: {m['content']}" for m in recent_messages])

        mode = self.backend.CHARACTER_MODES.get(character, "general")
        char_settings = self.backend.CHARACTER_SETTINGS.get(character, {})
        use_scene_brain = char_settings.get("use_scene_brain", False)

        intent_rule = ""
        intent_schema_injected = ""
        if use_scene_brain:
            intent_rule = "- PROACTIVE INTENT: Characters are not brick walls. 'active_intentions' must reflect what they WANT to do next.\n"
            intent_schema_injected = ',\n    "active_intentions": "What the character is proactively trying to achieve or allow next"'

        # Load state into memory before building the prompt
        current_scene_state = self.backend.ram_state_cache if self.backend.ram_state_cache else SceneState.from_dict(_scene_data_on_disk)

        prompt_unified = f"""JSON Memory Engine. Analyze exchange, output ONLY raw JSON.

            [CURRENT STATE]
            {json.dumps(current_scene_state.to_dict())}

            [COGNITIVE STATE]
            The Story So Far: {current_arc}
            Facts: {json.dumps(existing_facts)}

            [RECENT EXCHANGE]
            {convo_text}

            [CONSTRAINTS]
            - EMOTIONAL EVOLUTION: Track how the character's internal walls are shifting.
            - TIMELINE AWARENESS: Track time of day and completed activities.
            - STRICT JSON: Do not copy the placeholder descriptions. Output actual values or empty strings.
            {intent_rule}

            You must respond with ONLY a raw JSON object using this exact structure:
            {{
            "scene_analysis": "Briefly deduce what physical/clothing changes just happened.",
            "physical_actions": [
            {{"type": "strip_all"}}
            ],
            "scene_state": {{
                "environment": "",
                "current_objective": "",
                "recent_action_bridge": "",
                "proximity": "",
                "physical_state": ""{intent_schema_injected}
                }},
            "emotional_state": {{
                "trust_level": "0-10 integer. How much do they trust {{user}} right now?",
                "surface_affect": "What emotion is visibly readable from their face/body?",
                "concealed_feeling": "What are they actually feeling underneath the surface?",
                "momentum": "rising, falling, or holding",
                "trigger": "What specific thing {{user}} did caused this state?"
            }},
            "new_facts": {{
                "Trait/Milestone": ""
                }},
            "significant_shift_detected": "",
            "scene_ended": false
            }}"""

        try:
            brain_msgs = [
                {"role": "system", "content": "You are a specialized JSON Memory Engine. Output raw JSON only."},
                {"role": "user", "content": prompt_unified}
            ]

            brain_settings = {"temperature": 0.1, "max_tokens": 800, "top_p": 0.9}
            reply_unified = self._call_brain_with_fallback(brain_msgs, brain_settings)

            if not reply_unified:
                print("[⚠️ BRAIN ABORT] All brain APIs failed. Previous state preserved on disk.")
                return

            match_unified = re.search(r'\{.*\}', reply_unified, re.DOTALL)

            if match_unified:
                data = repair_json(match_unified.group(0), return_objects=True)

                if not data or not isinstance(data, dict):
                    print("[⚠️ BRAIN PARSE ERROR] json-repair could not salvage the output.")
                    return

                _PROMPT_ECHOES = {
                    "MANDATORY", "CONSTRAINTS", "JSON Memory Engine",
                    "{{user}}", "{{char}}", "Output ONLY", "schema",
                    "MANDATORY:", "Start with the EXACT", "List EXACT",
                }

                def _brain_field_ok(val):
                    if not val or not isinstance(val, str):
                        return False
                    if len(val.split()) < 2:
                        return False
                    for echo in _PROMPT_ECHOES:
                        if echo in val:
                            return False
                    return True

                if "scene_state" in data and isinstance(data["scene_state"], dict):
                    ns = data["scene_state"]
                    for _field in ["environment", "physical_state", "proximity", "recent_action_bridge", "current_objective"]:
                        if not _brain_field_ok(ns.get(_field, "")):
                            ns.pop(_field, None)
                            print(f"[⚠️ BRAIN VALIDATION] Dropped suspicious '{_field}' — keeping previous value.")

                if "new_facts" in data and isinstance(data["new_facts"], dict):
                    _FACT_JUNK = {"leave empty", "describe newly", "major irreversible", "trait/milestone", "none"}
                    data["new_facts"] = {
                        k: v for k, v in data["new_facts"].items()
                        if _brain_field_ok(v) and not any(j in str(v).lower() for j in _FACT_JUNK)
                    }

                with self.backend.advanced_memory.lock:
                    # Load the absolute latest state from the DB to avoid race conditions
                    fresh_doc = db.chat_states.find_one({"_id": doc_id}) or {}
                    fresh_scene = fresh_doc.get("scene", {})
                    current_scene_state = SceneState.from_dict(fresh_scene)

                    # --- 1. PROCESS PHYSICAL ACTIONS ---
                    actions = data.get("physical_actions", [])
                    if actions:
                        logs = self.backend.action_engine.apply_actions(current_scene_state, actions)
                        for log in logs:
                            print(f"[SCENE UPDATE] {log}")

                    # --- 2. PROCESS NARRATIVE STATE UPDATES ---
                    if "scene_state" in data and isinstance(data["scene_state"], dict):
                        ns = data["scene_state"]
                        current_scene_state.environment = ns.get("environment", current_scene_state.environment)
                        current_scene_state.current_objective = ns.get("current_objective", getattr(current_scene_state, "current_objective", ""))
                        current_scene_state.proximity = ns.get("proximity", getattr(current_scene_state, "proximity", "Unknown distance"))
                        current_scene_state.physical_state = ns.get("physical_state", current_scene_state.physical_state)
                        current_scene_state.recent_action_bridge = ns.get("recent_action_bridge", getattr(current_scene_state, "recent_action_bridge", ""))
                        if use_scene_brain:
                            current_scene_state.active_intentions = ns.get("active_intentions", getattr(current_scene_state, "active_intentions", ""))

                        print(f"[🔥 STATE UPDATED] {current_scene_state.physical_state}")

                    self.backend.ram_state_cache = current_scene_state
                    _checkpoint_state_dict = current_scene_state.to_dict()

                    if "scene_analysis" in data and data["scene_analysis"]:
                        print(f"[🧠 TRACKER THOUGHTS]: {data['scene_analysis']}")

                    # --- 3. PROCESS COGNITIVE ARCHIVE ---
                    if "new_facts" in data and isinstance(data["new_facts"], dict):
                        for trait, value in data["new_facts"].items():
                            if trait and value and "leave empty" not in str(value).lower():
                                event_log.append({"trait": trait, "value": value, "turn_id": turn_id})
                        
                        self.backend.ram_facts_cache = {"event_log": event_log}
                        
                        db.chat_states.update_one(
                            {"_id": doc_id}, 
                            {"$set": {"facts.event_log": event_log}}, 
                            upsert=True
                        )

                    current_emotional_state = fresh_scene.get("emotional_state", {})

                    shift_note = data.get("significant_shift_detected", "")
                    if _brain_field_ok(shift_note):
                        try:
                            new_emotional_state = data.get("emotional_state", {}) or {}
                            prev_trust = current_emotional_state.get("trust_level")
                            new_trust = new_emotional_state.get("trust_level")
                            intensity = 3  
                            try:
                                if prev_trust is not None and new_trust is not None:
                                    intensity += abs(int(new_trust) - int(prev_trust))
                            except (TypeError, ValueError):
                                pass
                            if new_emotional_state.get("momentum") in ("rising", "falling"):
                                intensity += 1

                            trigger_text = new_emotional_state.get("trigger", "")
                            label = " ".join((trigger_text or shift_note).split()[:6])

                            self._get_beat_bank(character, chat_id).add_beat(
                                turn_id=turn_id,
                                verbatim=shift_note.strip(),
                                trigger=trigger_text,
                                surface_affect=new_emotional_state.get("surface_affect", ""),
                                concealed_feeling=new_emotional_state.get("concealed_feeling", ""),
                                label=label,
                                intensity=intensity,
                            )
                        except Exception as e:
                            print(f"[⚠️ BEAT BANK ERROR] -> {e}")

                    # ── SCENE DATA SAVER (FULLY AGNOSTIC) ────────────────────
                    # Merge properties carefully to maintain relationship tags
                    updated_scene_data = dict(fresh_scene)
                    updated_scene_data.update(current_scene_state.to_dict())
                    updated_scene_data["pending_shift"] = data.get("significant_shift_detected", "")
                    updated_scene_data["scene_ended"] = data.get("scene_ended", False)
                    updated_scene_data["micro_intent"] = data.get("micro_intent", "")
                    updated_scene_data["emotional_state"] = data.get("emotional_state", current_emotional_state)
                    updated_scene_data["last_brain_update_index"] = total_msgs if total_msgs > 0 else len(recent_messages)

                    db.chat_states.update_one(
                        {"_id": doc_id}, 
                        {"$set": {"scene": updated_scene_data}}, 
                        upsert=True
                    )

                    self.backend.ram_scene_cache = updated_scene_data

                    print(f"[🧠 BRAIN] Scene state and timeline updated for {character}.")

                # CHECKPOINT PATCH 
                try:
                    cid = self.backend.CHARACTER_IDS.get(character, "unknown")
                    _summary_patch = self.backend.advanced_memory.load_summary(cid, chat_id)
                    if "checkpoints" in _summary_patch and str(turn_id) in _summary_patch["checkpoints"]:
                        _summary_patch["checkpoints"][str(turn_id)]["physical_state"] = _checkpoint_state_dict
                        self.backend.advanced_memory.save_summary(cid, chat_id, _summary_patch)
                except Exception as e:
                    print(f"[⚠️ CHECKPOINT PATCH ERROR] {e}")

                print(f"[🧠 HYBRID BRAIN] Unified 70B update for {character} completed successfully.", flush=True)
        except Exception as e:
            print(f"[⚠️ UNIFIED BRAIN ERROR] -> {e}")

    def _prefetch_memory(self, recent_history, last_user_text, character, chat_id):
        """Runs vector search in background right after a reply completes,
        so the result is ready instantly when the user sends their next message."""
        try:
            result = self.retrieve_relevant_memory(recent_history, last_user_text, character, chat_id)
            with self.backend._prefetch_lock:
                self.backend._prefetched_memory = result
        except Exception:
            pass


    def _estimate_tokens(self, text):
        """Rough estimation: 1 word is roughly 1.3 tokens."""
        if not text: return 0
        return int(len(str(text).split()) * 1.3)


    def get_dynamic_history(self, recent_messages, max_history_tokens=2000, max_turns=4):
        """
        Works backward through history, enforcing a strict turn limit first,
        then ensuring it doesn't exceed the token budget.
        (max_turns=4 means the last 4 user messages and 4 bot messages)
        """
        kept_messages = []
        current_tokens = 0

        # Enforce strict turn limit (1 turn = 2 messages)
        message_limit = max_turns * 2
        limited_messages = recent_messages[-message_limit:] if len(recent_messages) > message_limit else recent_messages

        # Iterate backwards from the most recent message
        for msg in reversed(limited_messages):
            msg_tokens = self._estimate_tokens(msg.get("content", ""))

            # If adding this message exceeds our budget, stop looking back
            if current_tokens + msg_tokens > max_history_tokens:
                break

            kept_messages.insert(0, msg) # Insert at beginning to maintain chronological order
            current_tokens += msg_tokens

        return kept_messages
    # --- Extra Utilities ---


    def generate_scene(self, character, model_choice):
        """Analyzes Lyra state, physical traits, and recent messages to generate an image."""
        msgs = self.backend.CURRENT_CHAT.get("messages", [])
        if not msgs:
            return {"error": "No messages to analyze."}

        chat_id = self.backend.CURRENT_CHAT.get("chat_id", "default")

        # 1. NARROW THE FOCUS: Only grab the last 3 messages so older scenes don't bleed in.
        recent = msgs[-3:]

        physical_details = self.backend.CHARACTER_PHYSICAL_DETAILS.get(character, "")
        if not physical_details:
            physical_details = "A standard human character (no specific details provided)."

        scene_file = self.backend.advanced_memory.get_scene_file(character, chat_id)
        lyra_state_block = "No current environmental data."

        if os.path.exists(scene_file):
            try:
                with open(scene_file, 'r', encoding='utf-8') as f:
                    scene_data = json.load(f)
                    struct = scene_data.get("structured_scene", {})
                    if struct:
                        state_lines = [f"- {k.replace('_', ' ').title()}: {v}" for k, v in struct.items()]
                        lyra_state_block = "\n".join(state_lines)
            except Exception as e:
                print(f"[⚠️ SCENE GENERATOR] Error loading Lyra state: {e}")

        transcript = "\n".join([f"{'User' if m['role'] == 'user' else character}: {m['content']}" for m in recent])


        # 2. AGGRESSIVE PROMPT INSTRUCTIONS
        prompt_instruction = f"""You are an expert AI image prompt engineer.
            Analyze the transcript and generate a SINGLE comma-separated text-to-image prompt.

            ### STRICT GUIDELINES ###
            1. Output ONLY raw comma-separated tags. NO explanations.
            2. Focus ONLY on the environment, mood, and action happening in the VERY LAST message.
            3. CRITICAL: DO NOT describe the character's physical appearance (no face, hair, body shape, race, or anatomy). The system adds those automatically.
            4. Romantic or erotic context is allowed, but express it through actions, clothing state, pose, atmosphere, and environment — NOT anatomy.
            5. Keep it under 25 words total.
            6. Format: [Clothing or clothing state], [Intimate or current action], [Immediate Environment], [Lighting / Mood / Atmosphere].

            ### ALLOWED CONTEXT ###
            You may include tags such as:
            embracing, kissing, leaning close, whispering, pinned against wall,
            sitting on lap, lying together, teasing gesture,
            bedroom setting, messy sheets, candlelight, warm glow,
            intimate atmosphere, romantic tension, sensual mood.

            ### CURRENT SCENE STATE ###
            {lyra_state_block}

            ### RECENT TRANSCRIPT (FOCUS ON THE END) ###
            {transcript}

            RAW COMMA-SEPARATED TAGS ONLY:"""

        prompt_messages = [
            {
            "role": "system",
            "content": "You are a specialized Image Prompt Generator. Output ONLY raw comma-separated image tags. Never describe physical traits or anatomy."
                },
            {"role": "user", "content": prompt_instruction}
        ]

        settings = self.backend.CHARACTER_SETTINGS.get(character, {})

        try:
            response_generator = self.backend.call_selected_model(prompt_messages, settings, model_choice)
            if not response_generator:
                return {"error": "No response from model."}
            scene = "".join(response_generator).strip()
        except Exception as e:
            return {"error": f"Scene detection failed: {e}"}

        if self.backend.debug_mode:
            debug_path = os.path.join(self.backend.BASE_DIR, "debug_scene_prompt.txt")
            try:
                with open(debug_path, "w", encoding="utf-8") as f:
                    f.write("=== RAW AI OUTPUT ===\n")
                    f.write(scene)
                    f.write(f"\n\n=== LENGTH: {len(scene)} characters ===\n")
            except Exception as debug_e:
                print(f"[⚠️ DEBUG ERROR] Could not save scene prompt: {debug_e}")



        if scene.upper() == "NONE" or not scene:
            return {"text": "I couldn't detect any explicit visual scene to generate right now.", "image": None}

        # 3. SMART CLEANUP
        scene = re.sub(r'(?i)\[THOUGHTS?:.*?(?:\]|\n\n|$)', '', scene, flags=re.DOTALL)
        scene = re.sub(r'(?i)\*THOUGHTS?:.*?(?:\*|\n\n|$)', '', scene, flags=re.DOTALL)
        scene = re.sub(r'^(Here are the tags:|Prompt:|Image:)\s*', '', scene, flags=re.IGNORECASE)
        scene = scene.replace('"', '').replace('\n', ' ').strip()

        # 4. GUARANTEE PHYSICAL DETAILS & ENFORCE REALISM
        style_suffix = ", photorealistic, highly detailed, realistic human, 8k resolution, cinematic lighting, photography"

        # Grab the raw physical details directly from your dictionary
        raw_physical = self.backend.CHARACTER_PHYSICAL_DETAILS.get(character, "").strip()
        prefix = ""
        if raw_physical:
            # Clean it up for the image generator (changes "Race: elf. Hair: white." to "Race: elf, Hair: white, ")
            prefix = raw_physical.replace(".", ",")
            if not prefix.endswith(","):
                prefix += ", "

        # 5. SMART SLICE (Accounting for both prefix and suffix)
        reserved_length = len(style_suffix) + len(prefix)
        # Keep total well under the 400 character URL crash limit
        max_length = 380 - reserved_length

        if len(scene) > max_length and max_length > 0:
            cut_index = scene.rfind(',', 0, max_length)
            scene = scene[:cut_index] if cut_index != -1 else scene[:max_length]

        # THE FOOLPROOF COMBINATION: Hardcoded Look + AI's Action + Hardcoded Style
        # The FOOLPROOF COMBINATION: Hardcoded Look + AI's Action + Hardcoded Style
        final_scene_prompt = f"{prefix}{scene.strip(', ')}{style_suffix}"

        # Send the combined, highly-detailed prompt to Pollinations
        image_path = self.backend.generate_image_pollinations(final_scene_prompt)

        if isinstance(image_path, str) and image_path.startswith("ERROR"):
            return {"error": image_path}

        # FIX: We no longer append the prompt to CURRENT_CHAT or save it.
        # This prevents the raw tags from poisoning the vector DB and summarizer.
        return {"text": "Scene image generated.", "image": image_path}
