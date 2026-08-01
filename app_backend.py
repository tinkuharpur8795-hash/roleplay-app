import os
from collections import OrderedDict
import re
import time
import json
from json_repair import repair_json
import base64
import requests
import threading
import subprocess
import webbrowser
import platform
import difflib
import random
import string
from datetime import datetime, timezone, timedelta

# Fixed UTC+5:30 offset for India — no DST in India, so a fixed offset is
# simpler and more robust than relying on the server having tzdata/zoneinfo
# installed (PythonAnywhere's containers run in UTC regardless of user tz).
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
from io import BytesIO
from PIL import Image
from openai import OpenAI
import shutil
import numpy as np
import concurrent.futures
import httpx
try:
    import faiss
except ImportError:
    faiss = None
from dotenv import load_dotenv
import memory_manager
from memory_manager import AdvancedMemoryManager

from emotional_memory import EmotionalBeatBank
from scene_state import SceneState
from action_engine import ActionEngine
from genre_config import get_genre_config, get_momentum_text
from memory_store import CompanionMemoryStore
from expectation_memory import ExpectationMemory
from memory_context import MemoryContextEngine
from db import get_db

try:
    import speech_recognition as sr
except ImportError:

    sr = None


# Phase 4 — cheap, rule-based first pass at spotting "the user just mentioned
# a future plan worth checking back in on" (an exam, appointment, trip, etc).
# Deliberately conservative: a handful of clear patterns rather than broad
# guessing, since a noisy/wrong reminder is more annoying than a missed one.
# An LLM-based classifier can replace this later without touching the call
# site (_maybe_store_expectation) or anything downstream of it.
_FUTURE_EVENT_PATTERNS = [
    re.compile(r"\bi(?:'m| am) (?:going|planning) to (?:the |a |an )?([a-z][a-z' ]{2,40}?)\s+(?:tomorrow|tonight|later|this (?:week|weekend)|next \w+)", re.IGNORECASE),
    re.compile(r"\bi(?:'ve| have) got (?:an?|my) ([a-z][a-z' ]{2,40}?)\s+(?:tomorrow|tonight|later|this (?:week|weekend)|next \w+)", re.IGNORECASE),
    re.compile(r"\bi have (?:an?|my) ([a-z][a-z' ]{2,40}?)\s+(?:tomorrow|tonight|later|this (?:week|weekend)|next \w+)", re.IGNORECASE),
]

class RoleplayBackend:

    def __init__(self, backend=None):
        load_dotenv()

        # Directories
        self.BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        self.HISTORY_DIR = os.path.join(self.BASE_DIR, "histories")
        self.IMAGES_DIR = os.path.join(self.BASE_DIR, "images")
        self.MEMORY_DIR = os.path.join(self.BASE_DIR, "memory")
        self.SUMMARY_DIR = os.path.join(self.BASE_DIR, "summaries")
        self.CHARACTERS_DIR = os.path.join(self.BASE_DIR, "characters")
        self.USER_MESSAGES_LOG = os.path.join(self.BASE_DIR, "user_messages_log.txt")
        self.ENGLISH_FEEDBACK_FILE = os.path.join(self.BASE_DIR, "english_feedback.txt")

        for d in [self.HISTORY_DIR, self.IMAGES_DIR, self.MEMORY_DIR, self.SUMMARY_DIR, self.CHARACTERS_DIR]:
            os.makedirs(d, exist_ok=True)

        self.advanced_memory = AdvancedMemoryManager(self.BASE_DIR)

        # Phase 2 — consolidated companion memory (facts, relationship state,
        # reminders) in a single file per chat. Not read by generate_reply
        # yet — that wiring is Phase 4. Purely additive at this point.
        self.companion_memory = CompanionMemoryStore(self.BASE_DIR)
        self.expectations = ExpectationMemory(self.companion_memory)

        self._beat_bank_cache = {}

        # Memory/scene/summary/prompt-building logic lives in memory_context.py
        # now. This engine reads/writes the same attributes on `self` as
        # before (MEMORY_DIR, ram_scene_cache, enable_* flags, etc.) via its
        # own `self.backend` reference.
        self.memory_context = MemoryContextEngine(self)



        # ── PERMANENT NPC DATABASE ───────────────────────────
        self.NPC_DB_FILE = os.path.join(self.BASE_DIR, "npc_profiles.json")
        if not os.path.exists(self.NPC_DB_FILE):
            default_npcs = {
                "aria": {"description": "She is the 6-year-old daughter of the user. She is sweet and playful.", "allow_tease": False},
                "mike": {"description": "The user's good friend. Usually hangs out in group settings.", "allow_tease": False},
                "adam": {"description": "Another friend of the user. Likes playing games like guess the food.", "allow_tease": True}
            }
            with open(self.NPC_DB_FILE, "w", encoding="utf-8") as f:
                json.dump(default_npcs, f, indent=4)

        # Load them into memory once when the app starts
        with open(self.NPC_DB_FILE, "r", encoding="utf-8") as f:
            self.PERMANENT_NPCS = json.load(f)
        # ───────────────────────────────────────────────────────────────────────
        self.action_engine = ActionEngine()
        # State dictionaries
        self.USER_NAME = "Jack"  # Set your global user name here
        self.USER_GENDER = "male"
        self.CHARACTERS = {}

        self.CHARACTER_IDS = {}
        self.CHARACTER_SETTINGS = {}
        self.CHARACTER_MODES = {}
        self.CHARACTER_FIRST_MESSAGES = {}
        self.CHARACTER_EXAMPLES = {}
        self.CURRENT_CHAT = {"character": None, "chat_id": None, "path": None, "messages": []}
        self.CHARACTER_PHYSICAL_DETAILS = {}
        self.CHARACTER_DEFAULT_CLOTHING = {}
        self.CHARACTER_CORE_TRAITS = {}
        self.CHARACTER_VOICE_POOL = {}
        self.memory_lock = threading.Lock()
        self._prefetched_memory = None
        self._prefetch_lock = threading.Lock()
        self.summary_writer_lock = threading.Lock()
        self.brain_writer_lock = threading.Lock()
        self.cancel_flag = threading.Event()
        self.BRAIN_MODEL = "Groq LLaMA 3.3 70B  (Brain)"
        self.show_thoughts = True
        self.debug_mode = True # Set True to write debug_last_prompt.json and debug_scene_prompt.txt
        self.enable_summary_engine = True   # chronicle / current_arc, runs every 20 turns — KEEP ON
        self.enable_brain_update = False   # scene state / emotional state / facts / emotional beat capture
        self.enable_vector_recall = True # synchronous flashback recall fallback
        self.enable_scene_context_injection = False  # scene state / story context / facts block in prompt
        self.enable_emotional_beat_recall = False    # surfacing EmotionalBeatBank entries in the prompt
        self.ram_state_cache = None
        self.ram_facts_cache = {}
        self.ram_scene_cache = {}
        self.CHARACTER_SCENARIOS = {}
        self.CHARACTER_GENRES = {}
        self.CHARACTER_CUSTOM_RULES = {}
        self.CHARACTER_TYPES = {} 

        self.api_clients_cache = {}
        # --- NEW: Global HTTP Client for Connection Pooling ---
        self.http_client = httpx.Client(
            limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=120.0),
            timeout=httpx.Timeout(20.0)
        )
        self.PROACTIVE_WINDOWS = {
        # kind: (start_hour, start_minute, end_hour, end_minute) — all in IST
            "morning":     (7, 0, 9, 0),
            "missing_you": (13, 0, 17, 0),
            "night":       (21, 0, 23, 0),
        }
        # API Keys & Models
        self.CLOUD_APIS = [
            {"name": "OpenRouter API 1", "base_url": "https://openrouter.ai/api/v1", "api_key": os.getenv("OPENROUTER_KEY_1")},
            {"name": "OpenRouter API 2", "base_url": "https://openrouter.ai/api/v1", "api_key": os.getenv("OPENROUTER_KEY_2")},
            {"name": "OpenRouter API 3", "base_url": "https://openrouter.ai/api/v1", "api_key": os.getenv("OPENROUTER_KEY_3")}
        ]
        self.current_api_index = 0

        # --- NEW: Dual Groq API Rotation ---
        self.GROQ_APIS = [
            {"name": "Groq API 1", "api_key": os.getenv("GROQ_KEY_1")},
            {"name": "Groq API 2", "api_key": os.getenv("GROQ_KEY_2")}
        ]
        self.current_groq_index = 0
        self.MEGANOVA_KEY = os.getenv("MEGANOVA_KEY")
        self.GEMINI_KEY = os.getenv("GEMINI_API_KEY")
        self.MISTRAL_KEY = os.getenv("MISTRAL_API_KEY")
        self.DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
        self.NVIDIA_KEY = os.getenv("NVIDIA_NIM_KEY")
        self.PUTER_TOKEN = os.getenv("PUTER_TOKEN")
        self.JIEKOU_KEY = os.getenv("JIEKOU_API_KEY")
        self.COHERE_KEY = os.getenv("COHERE_API_KEY")
        self.PINECONE_KEY = os.getenv("PINECONE_API_KEY")
        self.MORPH_KEY = os.getenv("MORPH_API_KEY")
        self.MODEL_OPTIONS = {
            # === FRONT-END CHAT MODELS (Creative, Fast, Roleplay-Focused) ===
            "Jiekou grok-4-1-fast-reasoning": {
                "type": "cloud",
                "provider": "jiekou",  # <--- ADD THIS
                "name": "grok-4-1-fast-reasoning",
                "size_b": 671,
            },
            
            
            "NVIDIA z ai": {
                "type": "cloud",
                "provider": "nvidia",
                "name": "z-ai/glm-5.2",
                "size_b": 340,
            },
            
            "Cloud Mistral Nemo  (Chat)": {
                "type": "cloud",
                "provider": "openrouter",
                "name": "mistralai/mistral-nemo",
                "providers": ["DeepInfra"],
                "size_b": 12,
                },
            "Cloud deepseek/deepseek-v4-flash": {
                "type": "cloud",
                "provider": "openrouter",
                "name": "deepseek/deepseek-v4-flash",
                "providers": ["DeepInfra", "streamlake/fp8"],
                "size_b": 100,
                },
            
            
         
           
            "Groq LLaMA 3.3 70B  (Brain)": {
                "type": "cloud", "provider": "groq",
                "name": "llama-3.3-70b-versatile",
                "size_b": 70,
                },
            "Groq qwen/qwen3.6-27b": {
                "type": "cloud", "provider": "groq",
                "name": "qwen/qwen3.6-27b",
                "size_b": 8, 
                },
                        
            "Meganova 70B-Euryale": {
                "type": "cloud",
                "provider": "meganova",
                "name": "Sao10K/L3-70B-Euryale-v2.1",
                "size_b": 12,
                },
         
           "Mistral Mixtral 8x22B": {
                "type": "cloud",
                "provider": "mistral",
                "name": "open-mixtral-8x22b",
                "size_b": 141, # 8x22B totals 141B parameters (39B active)
            },
            "Mistral Large": {
                "type": "cloud",
                "provider": "mistral",
                "name": "mistral-large-latest",
                "size_b": 123,
            },
            "Mistral Mixtral medium 22B": {
                "type": "cloud",
                "provider": "mistral",
                "name": "mistral-medium-3-5",
                "size_b": 141, 
            },
                            
            "mistral-small-2501": {
                "type": "cloud",
                "provider": "mistral",
                "name": "mistral-small-2501",
                "size_b": 24,
            },
            "Cohere Command R+": {
                "type": "cloud",
                "provider": "cohere",
                "name": "command-r-plus-08-2024",
                "size_b": 104,
            },
            "Cohere Command A+": {
                "type": "cloud",
                "provider": "cohere",
                "name": "command-a-03-2025",
                "size_b": 35,
            },
            "Morph MiniMax M3": {
                "type": "cloud",
                "provider": "morph",
                "name": "morph-minimax3-428b",
                "size_b": 428,
            },
            }
    


        self.emoji_pattern = re.compile("["
            u"\U0001F600-\U0001F64F" u"\U0001F300-\U0001F5FF"
            u"\U0001F680-\U0001F6FF" u"\U0001F1E0-\U0001F1FF"
            u"\U00002700-\U000027BF" u"\U000024C2-\U0001F251"
            "]+", flags=re.UNICODE)

        # Initialization routines
        self.load_characters()
    def _make_path(self, character, chat_id):
        return f"{character}::{chat_id}"

    def _parse_path(self, pseudo_path):
        parts = pseudo_path.split("::")
        return parts[0], int(parts[1])

    def toggle_summary_engine(self, state: bool):
        """Hook this up to your UI toggle button."""
        self.enable_summary_engine = state
        status = "ENABLED" if state else "DISABLED"
        print(f"[⚙️ CONFIG] Summary Engine & Background Groq Calls: {status}")

    def warm_up_model(self, model_choice):
        """Runs silently in the background to wake up a serverless model."""
        def _warm():
            print(f"[🔥 WARM-UP] Pinging Bytez to wake up {model_choice}...")

            # 1. Tiny input: No chat history, no system prompt, just 2 words.
            messages = [{"role": "user", "content": "Wake up."}]

            # 2. Tiny output: Force the model to stop after 1 single token.
            settings = {"max_tokens": 1, "temperature": 0.1}

            try:
                # 3. Fire the request. We don't care about the response text.
                generator = self.call_selected_model(messages, settings, model_choice)
                for _ in generator:
                    pass # Just consume the stream silently
                print(f"[✅ WARM-UP COMPLETE] {model_choice} is now in active VRAM!")

            except Exception as e:
                print(f"[⚠️ WARM-UP FAILED] {e}")

        # Fire and forget! This runs in a background thread so your UI doesn't freeze.
        threading.Thread(target=_warm, daemon=True).start()

    def _preload_ram_caches(self, character, chat_id):
        """Loads MongoDB state, facts, and scene data into RAM once when a chat is opened."""
        try:
            db = get_db()
            doc_id = f"{character}_{chat_id}"
            doc = db.chat_states.find_one({"_id": doc_id})

            if doc:
                # Load Scene Data into RAM
                scene_data = doc.get("scene", {})
                self.ram_scene_cache = scene_data
                
                # Load Physical State into RAM using your existing from_dict method
                if scene_data:
                    self.ram_state_cache = SceneState.from_dict(scene_data)
                else:
                    self.ram_state_cache = SceneState()

                # Load Facts into RAM
                self.ram_facts_cache = doc.get("facts", {})
            else:
                self.ram_scene_cache = {}
                self.ram_state_cache = SceneState()
                self.ram_facts_cache = {}

            print(f"[⚡ RAM CACHE] Pre-loaded state, facts, and scene from DB for {character}.")
        except Exception as e:
            print(f"[⚠️ DB ERROR] Failed to preload RAM caches: {e}")
            self.ram_scene_cache = {}
            self.ram_state_cache = SceneState()
            self.ram_facts_cache = {}


    def load_characters(self):
        for filename in os.listdir(self.CHARACTERS_DIR):
            if filename.endswith(".json"):
                file_path = os.path.join(self.CHARACTERS_DIR, filename)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)

                        # Support both V1 and V2 Character Card specs
                        char_data = data.get("data", data)

                        # Extract name safely
                        name = data.get("name", char_data.get("name", ""))

                        if name:
                            # Robust extraction for personality and prompts
                            sys_prompt = data.get("system_prompt", char_data.get("description", char_data.get("system_prompt", "")))
                            personality = data.get("personality", char_data.get("personality", ""))
                            scenario = data.get("scenario", char_data.get("scenario", ""))
                            examples = data.get("ex_dialogues", char_data.get("mes_example", ""))
                            core_trait = data.get("core_trait", char_data.get("core_trait", ""))
                            self.CHARACTER_CORE_TRAITS[name] = core_trait

                            # Replace template variables
                            personality = personality.replace("{{char}}", name).replace("{{user}}", self.USER_NAME)
                            scenario = scenario.replace("{{char}}", name).replace("{{user}}", self.USER_NAME)
                            examples = examples.replace("{{char}}", name).replace("{{user}}", self.USER_NAME)


                            # Extract physical details safely
                            # Extract physical details safely
                            physical = data.get("physical_details", char_data.get("physical_details", ""))
                            self.CHARACTER_PHYSICAL_DETAILS[name] = physical
                            starting_clothes = data.get("clothing", char_data.get("default_clothing", {}))
                            self.CHARACTER_DEFAULT_CLOTHING[name] = starting_clothes
                            master_prompt = f"{sys_prompt}\n\n{personality}\n\n" if sys_prompt or personality else ""
                            self.CHARACTERS[name] = master_prompt.strip()
                            self.CHARACTER_EXAMPLES[name] = examples if examples else ""

                            self.CHARACTER_SCENARIOS[name] = scenario if scenario else ""
                            self.CHARACTER_IDS[name] = data.get("id", filename.replace(".json", ""))
                            self.CHARACTER_SETTINGS[name] = data.get("settings", {})
                            self.CHARACTER_MODES[name] = data.get("mode", "general")
                            self.CHARACTER_GENRES[name] = data.get("genre", char_data.get("genre", "romance"))
                            self.CHARACTER_CUSTOM_RULES[name] = data.get("custom_rule", char_data.get("custom_rule", ""))

                            # FIX: Robustly find the first message using standard community keys
                            raw_first_msg = data.get("first_message", char_data.get("first_mes", char_data.get("greeting", "")))

                            # Strip asterisks to enforce the novel-style formatting rule from turn 0
                            clean_first_msg = raw_first_msg.replace("*", "")

                            self.CHARACTER_FIRST_MESSAGES[name] = clean_first_msg.replace("{{char}}", name).replace("{{user}}", self.USER_NAME)
                except Exception as e:
                    print(f"Error loading {filename}: {e}")

    # ════════════════════════════════════════════════════════════════════
    #  INBUILT CHARACTER PROMPT WRITER
    #  Turns a one-line vibe ("a grumpy bodyguard") into a fully expanded
    #  character card via an uncensored JSON-mode LLM (DeepSeek → Mistral),
    #  saves it to CHARACTERS_DIR, and activates it as the live chat partner.
    # ════════════════════════════════════════════════════════════════════

    CHARACTER_SCHEMA_FIELDS = [
        "name", "age", "occupation", "personality_traits", "appearance",
        "backstory", "romantic_dynamic", "greeting", "system_prompt",
    ]

    CHARACTER_WRITER_SYSTEM_PROMPT = """You are a professional character writer for an adult interactive roleplay platform.
Given a short character idea or archetype, invent a complete, original, fully fleshed-out character.

Output ONLY a single valid JSON object — no markdown code fences, no commentary, no preamble, no text before or after — matching EXACTLY this schema:

{
  "name": "Full name",
  "age": "Age",
  "occupation": "Occupation",
  "personality_traits": ["Trait 1", "Trait 2", "Trait 3"],
  "appearance": "2-sentence physical description",
  "backstory": "1-paragraph backstory with motivations",
  "romantic_dynamic": "How they behave in a romantic context",
  "greeting": "Their opening chat message to the user",
  "system_prompt": "3-paragraph deep roleplay instructions detailing tone, rules, and restrictions for the actor LLM."
}

Rules:
- ALL NINE fields above are required and must be non-empty. Never omit a field, never leave one blank, never stop early.
- Use exactly these field names, lowercase, with underscores — do not rename, nest, or wrap them under any other key.
- "personality_traits" must be a JSON array of at least 3 short trait words/phrases.
- "appearance" must be exactly two sentences.
- "backstory" is a single paragraph (4-8 sentences) with a clear, specific motivation.
- "greeting" is written in first person, fully in-character, addressed to {user_name}, with no narrator framing.
- "system_prompt" is exactly three paragraphs: (1) voice & tone, (2) roleplay behavior rules — stay in character, pacing, formatting conventions, never break the fourth wall, (3) boundaries — the character remains fictional, consent-aware, and the scenario stays between consenting adults.
- Be specific and vivid. Avoid generic filler. Do not reuse the user's exact wording verbatim as field values — expand on it.
- Keep every field reasonably concise so the full JSON object fits comfortably in one response. Finish the JSON object completely — a cut-off or partial object is a failure.
- Output raw JSON only.

Example of a correctly-shaped response (for a totally different idea — invent your own content, never copy this):
{
  "id": "jules_001",
  "name": "Jules",

  "first_message": "*It's a Sunday morning and I have been cleaning for hours. I am wearing my light blue sun dress, it's my favorite because you always give me compliments in it. It's almost noon and I decide to come into your room to get your laundry.* \"Good morning sunshine,\" *I announce with a smile as I burst through the door.*",
  "system_prompt": "You are Jules. Write the next reply from Jules in this never-ending conversation between Jules and Jack. Gestures and other non-verbal actions are written between asterisks (for example, *waves hello* or *moves closer*).",
  "personality": "Jules is a lonely widow who married at 18 and lost her husband a year later in a car accident that also took his sister and her husband. Jules chose to adopt and raise their child, Jack. Jules has not been with anyone romantically or sexually since she lost her husband, and she longs to be touched and to feel wanted. Jules loves Jack more than anything and would do anything for them. Jules is Jack's young, fun loving aunt who raises and lives with Jack.",
  "scenario": "Jules is Jack's young, fun loving aunt who raises and lives with Jack.",


  "settings": {
    "temperature": 0.8,
    "top_p": 0.9,
    "top_k": 30,
    "repetition_penalty": 1.1,
    "max_tokens": 1000

    }
  },
  "ex_dialogues": "{{user}}: *I hold your hand and pull on top of me* Good morning, Darling.\n{{char}}: *I let out a surprised little laugh as you pull me on top of you, my hands instinctively going to your shoulders to steady myself. I can feel the warmth of your body through the thin fabric of my dress.* \"Well good morning to you too,\"* I say with a playful grin, my voice a little breathless.* \"I was just coming to get your laundry, but I suppose I can take a break. You know how much I love snuggling with my favorite nephew.\" *I wiggle a bit to get comfortable on top of you, the movement making my dress ride up slightly to expose more of my bare legs.*"

}"""

    def _sanitize_character_filename(self, name: str) -> str:
        """Turns a character name into a clean, unique, filesystem-safe id."""
        base = re.sub(r"[^a-zA-Z0-9_\- ]", "", name or "").strip()
        base = re.sub(r"\s+", "_", base).lower()
        if not base:
            base = f"character_{int(time.time())}"

        candidate = base
        counter = 1
        while os.path.exists(os.path.join(self.CHARACTERS_DIR, f"{candidate}.json")):
            counter += 1
            candidate = f"{base}_{counter}"
        return candidate
    def edit_character_profile(self, char_id, updated_data, image_bytes=None, image_ext="png"):
        """
        Updates an existing character's JSON file and optionally saves a new avatar image.
        """
        # 1. Locate the existing character file
        char_file = os.path.join(self.CHARACTERS_DIR, f"{char_id}.json")
        if not os.path.exists(char_file):
            return {"status": "error", "message": f"Character file {char_id}.json not found."}

        # 2. Load and update the JSON data
        try:
            with open(char_file, "r", encoding="utf-8") as f:
                char_json = json.load(f)

            # Merge the updated fields into the existing character JSON
            for key, value in updated_data.items():
                char_json[key] = value

            # Save the updated JSON back to the file
            with open(char_file, "w", encoding="utf-8") as f:
                json.dump(char_json, f, indent=2, ensure_ascii=False)
        except Exception as e:
            return {"status": "error", "message": f"Failed to update JSON: {e}"}

        # 3. Save the new image if one was uploaded
        if image_bytes:
            try:
                # Save the image using the character's ID as the filename
                image_path = os.path.join(self.IMAGES_DIR, f"{char_id}.{image_ext}")
                with open(image_path, "wb") as f:
                    f.write(image_bytes)
            except Exception as e:
                return {"status": "error", "message": f"Failed to save image: {e}"}

        # 4. Reload characters into RAM so the changes take effect immediately
        self.load_characters()

        return {"status": "success", "message": f"Character '{char_id}' updated successfully."}

    def get_character_gallery(self, char_id):
        """
        Returns the list of extra gallery image filenames for a character
        (the main avatar is handled separately via character_image routes).
        Scans IMAGES_DIR directly rather than keeping a separate manifest
        file, so the list is always consistent with what's actually on disk.
        """
        prefix = f"{char_id}_gallery_"
        try:
            filenames = [f for f in os.listdir(self.IMAGES_DIR) if f.startswith(prefix)]
        except FileNotFoundError:
            return []
        filenames.sort()
        return filenames

    def add_character_gallery_image(self, char_id, image_bytes, image_ext="png"):
        """Saves a new gallery image for a character to disk."""
        if image_ext not in ("png", "jpg", "jpeg", "webp", "gif"):
            image_ext = "png"
        filename = f"{char_id}_gallery_{int(time.time() * 1000)}.{image_ext}"
        path = os.path.join(self.IMAGES_DIR, filename)
        try:
            with open(path, "wb") as f:
                f.write(image_bytes)
        except Exception as e:
            return {"status": "error", "message": f"Failed to save image: {e}"}
        return {"status": "success", "filename": filename}

    def delete_character_gallery_image(self, char_id, filename):
        """Deletes a gallery image file, only if it actually belongs to this character."""
        safe_name = os.path.basename(filename)  # guard against path traversal
        if not safe_name.startswith(f"{char_id}_gallery_"):
            return {"status": "error", "message": "This image does not belong to this character's gallery."}
        path = os.path.join(self.IMAGES_DIR, safe_name)
        if not os.path.exists(path):
            return {"status": "error", "message": "Image not found."}
        try:
            os.remove(path)
        except Exception as e:
            return {"status": "error", "message": f"Failed to delete image: {e}"}
        return {"status": "success"}

    def _get_character_writer_client(self, base_url: str, api_key: str) -> OpenAI:
        """Reuses the same connection-pooled OpenAI-compatible client pattern as the other providers."""
        cache_key = f"charwriter_{base_url}_{api_key}"
        if cache_key not in self.api_clients_cache:
            self.api_clients_cache[cache_key] = OpenAI(
                base_url=base_url,
                api_key=api_key,
                max_retries=0,
                http_client=self.http_client,
            )
        return self.api_clients_cache[cache_key]

    def _call_character_writer_api(self, base_url: str, api_key: str, model_name: str, messages: list) -> str:
        """Single non-streaming JSON-mode completion call. Returns the raw text content."""
        client = self._get_character_writer_client(base_url, api_key)
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=0.85,
                max_tokens=4096,
                response_format={"type": "json_object"},
                stream=False,
            )
        except Exception as e:
            # Some OpenAI-compatible backends reject the response_format param —
            # retry once without it before giving up on this provider.
            print(f"[⚠️ CHAR WRITER] response_format rejected ({e}); retrying without it.")
            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=0.85,
                max_tokens=4096,
                stream=False,
            )

        choice = response.choices[0]
        content = choice.message.content or ""
        finish_reason = getattr(choice, "finish_reason", None)

        # Always log a preview — this is the fastest way to diagnose a bad
        # generation from the Activity Log panel without re-running anything.
        preview = content[:600].replace("\n", " ")
        print(f"[🧠 CHAR WRITER] finish_reason={finish_reason!r} | raw preview: {preview}{'...' if len(content) > 600 else ''}")

        if finish_reason == "length":
            raise ValueError(
                "The model's response was cut off before it finished (hit the token limit). "
                "This usually clears up on retry — try generating again."
            )

        return content

    def _unwrap_character_dict(self, parsed) -> dict:
        """
        Some models nest the character under a single wrapper key
        (e.g. {"character": {...}} or {"data": {...}}) despite instructions
        not to. If the top level doesn't look like our schema but a nested
        dict does, automatically unwrap it.
        """
        if not isinstance(parsed, dict):
            return parsed

        direct_hits = sum(1 for f in self.CHARACTER_SCHEMA_FIELDS if f in parsed)
        if direct_hits >= 3:
            return parsed

        for value in parsed.values():
            if isinstance(value, dict):
                nested_hits = sum(1 for f in self.CHARACTER_SCHEMA_FIELDS if f in value)
                if nested_hits >= 3:
                    return value

        return parsed

    def _parse_character_json(self, raw_content: str) -> dict:
        """Parses the model's raw text into a dict, repairing near-miss JSON if needed."""
        if not raw_content or not raw_content.strip():
            raise ValueError("Empty response from the character-writer API.")

        text = raw_content.strip()
        # Strip markdown fences some models still add despite JSON-mode instructions
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()

        parsed = None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # Fallback: best-effort repair for slightly malformed JSON (trailing
            # commas, an unterminated string/object from truncation, etc.)
            try:
                repaired = repair_json(text, return_objects=True)
                if isinstance(repaired, dict):
                    parsed = repaired
            except Exception:
                pass

        if parsed is None:
            raise ValueError("Could not parse a valid JSON character object from the model's response.")

        return self._unwrap_character_dict(parsed)

    def _validate_character_schema(self, data: dict) -> None:
        """Ensures every required field exists and is non-empty; coerces minor shape issues."""
        if not isinstance(data, dict):
            raise ValueError("Generated character data is not a JSON object.")

        missing = [f for f in self.CHARACTER_SCHEMA_FIELDS if not data.get(f)]
        if missing:
            received_keys = ", ".join(data.keys()) or "none"
            raise ValueError(
                f"Generated character is missing required field(s): {', '.join(missing)}. "
                f"Keys actually received: {received_keys}"
            )

        traits = data.get("personality_traits")
        if isinstance(traits, str):
            data["personality_traits"] = [t.strip() for t in re.split(r",|\n", traits) if t.strip()]
        if not isinstance(data.get("personality_traits"), list) or not data["personality_traits"]:
            raise ValueError("'personality_traits' must be a non-empty list.")

    def generate_character_json(self, user_vibe_input: str) -> dict:
        """
        Sends a short character idea/archetype to an uncensored, JSON-mode-capable
        LLM (DeepSeek first, Mistral Large as fallback) and returns a fully expanded
        character profile dict matching CHARACTER_SCHEMA_FIELDS.

        Raises ValueError for bad input / unparseable output, RuntimeError if no
        provider is configured or every provider fails.
        """
        vibe = (user_vibe_input or "").strip()
        if not vibe:
            raise ValueError("Please describe the character you want to create.")
        if len(vibe) > 4000:
            vibe = vibe[:4000]

        system_prompt = self.CHARACTER_WRITER_SYSTEM_PROMPT.replace("{user_name}", self.USER_NAME)
        user_prompt = (
            f'Create a fully fleshed-out character based on this idea: "{vibe}"\n\n'
            f"Remember: output ONLY the JSON object described in your instructions."
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # Provider order: DeepSeek-V3 first (cheap, permissive, OpenAI-compatible),
        # Mistral Large as the fallback. Skips any provider with no key configured.
        providers = []
        if getattr(self, "DEEPSEEK_KEY", None):
            providers.append(("deepseek", "https://api.deepseek.com", self.DEEPSEEK_KEY, "deepseek-chat"))
        if getattr(self, "MISTRAL_KEY", None):
            providers.append(("mistral", "https://api.mistral.ai/v1", self.MISTRAL_KEY, "mistral-large-latest"))

        if not providers:
            raise RuntimeError(
                "No character-writer API key configured. Set DEEPSEEK_API_KEY or MISTRAL_API_KEY in your .env"
            )

        last_error = None
        for provider_name, base_url, api_key, model_name in providers:
            try:
                raw_content = self._call_character_writer_api(base_url, api_key, model_name, messages)
                character_data = self._parse_character_json(raw_content)
                self._validate_character_schema(character_data)
                print(f"[✅ CHAR WRITER] Generated '{character_data.get('name', '?')}' via {provider_name}.")
                return character_data
            except Exception as e:
                last_error = e
                print(f"[⚠️ CHAR WRITER] {provider_name} failed: {e}")
                continue

        raise RuntimeError(f"Character generation failed on all providers. Last error: {last_error}")

    def save_character_card(self, character_data: dict) -> tuple:
        """
        Converts a generated character dict into the card format load_characters()
        understands, sanitizes the filename from the character's name, writes it
        to CHARACTERS_DIR, and returns (character_name, filepath).
        """
        name = str(character_data.get("name", "")).strip() or "Unnamed Character"
        char_id = self._sanitize_character_filename(name)

        traits = character_data.get("personality_traits", [])
        traits_line = ", ".join(traits) if isinstance(traits, list) else str(traits)

        # Fold the structured fields into the "personality" block that
        # load_characters() concatenates onto system_prompt as the master prompt.
        personality_block = (
            f"Age: {character_data.get('age', 'Unknown')}\n"
            f"Occupation: {character_data.get('occupation', 'Unknown')}\n"
            f"Personality traits: {traits_line}\n"
            f"Appearance: {character_data.get('appearance', '')}\n"
            f"Romantic dynamic: {character_data.get('romantic_dynamic', '')}"
        )

        card = {
            "id": char_id,
            "name": name,
            "system_prompt": character_data.get("system_prompt", ""),
            "personality": personality_block,
            "scenario": character_data.get("backstory", ""),
            "first_message": character_data.get("greeting", ""),
            "genre": "romance",
            "mode": "general",
            "settings": {},
            "metadata": {
                "auto_generated": True,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "raw": character_data,
            },
        }

        filepath = os.path.join(self.CHARACTERS_DIR, f"{char_id}.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(card, f, indent=2, ensure_ascii=False)

        print(f"[✅ CHAR WRITER] Saved character card → {filepath}")
        return name, filepath

    def create_character_from_vibe(self, user_vibe_input: str) -> dict:
        """
        Full pipeline for the 'Inbuilt Character Prompt Writer' feature:
        idea → AI-generated character JSON → saved card → reloaded into memory
        → new chat started with this character active.

        Returns a dict that can be sent straight back to the frontend as JSON.
        Raises ValueError / RuntimeError on failure (let the route layer catch these).
        """
        character_data = self.generate_character_json(user_vibe_input)
        name, filepath = self.save_character_card(character_data)

        # Re-scan CHARACTERS_DIR so the new card is registered in every in-memory dict
        self.load_characters()

        # Start a fresh chat session with the new character as the active one —
        # this seeds the first message with their greeting and preloads RAM caches.
        chat_data = self.create_new_chat(name)

        return {
            "status": "ok",
            "character": name,
            "greeting": self.CHARACTER_FIRST_MESSAGES.get(name, character_data.get("greeting", "")),
            "chat_id": chat_data.get("chat_id"),
            "path": chat_data.get("path"),
            "filepath": filepath,
            "characters": sorted(self.CHARACTERS.keys()),
        }

    # --- Path Resolvers ---
    def get_chats_dir(self, character):
        cid = self.CHARACTER_IDS.get(character, "unknown")
        dir_path = os.path.join(self.BASE_DIR, "chats", cid)
        os.makedirs(dir_path, exist_ok=True)
        return dir_path

    def get_chat_path(self, character, chat_id):
        return os.path.join(self.get_chats_dir(character), f"chat_{chat_id}.json")

    

    def get_sync_snapshot(self, character, chat_id, since=0.0):
        """
        Phase 3 — returns what's changed for one chat since a given unix
        timestamp. Deliberately simple: this app runs one active chat at a
        time (see CURRENT_CHAT), so a full multi-device sync protocol isn't
        needed yet.
        """
        result = {
            "chat_changed": False,
            "memory_changed": False,
            "chat": None,
            "memory": None,
            "server_time": time.time(),
        }

        try:
            db = get_db()
            # Ensure chat_id is properly cast for DB queries
            chat_data = db.chats.find_one({"character": character, "chat_id": int(chat_id)})
            
            if chat_data:
                chat_updated_at = chat_data.get("updated_at", 0)
                if chat_updated_at > since:
                    if "_id" in chat_data:
                        del chat_data["_id"]
                    chat_data["path"] = self._make_path(character, chat_id)
                    result["chat_changed"] = True
                    result["chat"] = chat_data
        except Exception as e:
            print(f"[⚠️ SYNC] Could not read chat from DB: {e}")

        cid = self.CHARACTER_IDS.get(character, character)
        try:
            # Companion Memory is preserved. If it has been migrated internally, 
            # this will still safely pull the dict and check the timestamp.
            mem_record = self.companion_memory.load(cid, str(chat_id))
            if mem_record and mem_record.get("updated_at", 0) > since:
                result["memory_changed"] = True
                result["memory"] = mem_record
        except Exception as e:
            print(f"[⚠️ SYNC] Could not read companion memory: {e}")

        return result

    def get_memory_file(self, character, chat_id):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context.get_memory_file(character, chat_id)

    def get_summary_file(self, character, chat_id):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context.get_summary_file(character, chat_id)

    def delete_chat_memory(self, character, chat_id):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context.delete_chat_memory(character, chat_id)

    def _maybe_store_expectation(self, character, chat_id, user_text, current_turn):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context._maybe_store_expectation(character, chat_id, user_text, current_turn)

    def get_chat_list(self, character):
        db = get_db()
        cursor = db.chats.find({"character": character}, {"title": 1, "chat_id": 1}).sort("chat_id", 1)
        
        chat_list = []
        for doc in cursor:
            chat_list.append({
                "title": doc.get("title", f"Chat {doc['chat_id']}"), 
                "path": self._make_path(character, doc["chat_id"])
            })
        return chat_list

    def get_chats_for_character(self, character):
        """
        Same rich per-chat fields as get_all_chats() (title, preview,
        last_activity, etc.) but scoped to a single character's own chat
        directory — used by the in-chat sidebar now that it only shows the
        currently open character's chats instead of scanning every
        character on every page load.
        """
        db = get_db()
        cursor = db.chats.find(
            {"character": character}, 
            {"chat_id": 1, "title": 1, "updated_at": 1, "messages": {"$slice": -1}}
        ).sort("updated_at", -1)
        
        chats = []
        for doc in cursor:
            messages = doc.get("messages", [])
            last_msg = messages[0] if messages else None

            if last_msg is None:
                preview, is_placeholder = "No messages yet", True
            elif last_msg.get("role") == "assistant":
                preview, is_placeholder = last_msg.get("content", ""), False
            else:
                preview, is_placeholder = "Sent a message", True

            last_activity = doc.get("updated_at", 0)

            chats.append({
                "character":              character,
                "chat_id":                doc["chat_id"],
                "path":                   self._make_path(character, doc["chat_id"]),
                "title":                  doc.get("title", f"Chat {doc['chat_id']}"),
                "preview":                preview,
                "preview_is_placeholder": is_placeholder,
                "last_activity":          last_activity,
            })

        return chats

    def get_all_chats(self):
        """
        Returns every saved chat across every character, most-recently-active
        first — powers a universal chat history list (all characters mixed
        together, like a normal messaging inbox) instead of a per-character
        list. "Activity" is the chat file's own mtime, since save_current_chat()
        rewrites the whole file on every turn, so mtime tracks the true last
        message time even though individual messages aren't timestamped.
        """
        db = get_db()
        cursor = db.chats.find(
            {}, 
            {"character": 1, "chat_id": 1, "title": 1, "updated_at": 1, "messages": {"$slice": -1}}
        ).sort("updated_at", -1)
        
        all_chats = []
        for doc in cursor:
            character = doc.get("character", "Unknown")
            messages  = doc.get("messages", [])
            last_msg  = messages[0] if messages else None

            if last_msg is None:
                preview, is_placeholder = "No messages yet", True
            elif last_msg.get("role") == "assistant":
                preview, is_placeholder = last_msg.get("content", ""), False
            else:
                preview, is_placeholder = "Sent a message", True

            last_activity = doc.get("updated_at", 0)

            all_chats.append({
                "character":              character,
                "chat_id":                doc["chat_id"],
                "path":                   self._make_path(character, doc["chat_id"]),
                "title":                  doc.get("title", f"Chat {doc['chat_id']}"),
                "preview":                preview,
                "preview_is_placeholder": is_placeholder,
                "last_activity":          last_activity,
            })

        return all_chats

    def edit_message_in_chat(self, path, index, new_content, expected_role=None):
        """
        Overwrites the content of a single message (by its position in the
        messages array) in a saved chat file. Powers the hover-to-edit
        pencil on any message bubble, past or present.
        """
        try:
            character, chat_id = self._parse_path(path)
        except Exception:
            return {"status": "error", "message": "Invalid chat path format."}

        db = get_db()
        data = db.chats.find_one({"character": character, "chat_id": chat_id})
        
        if not data:
            return {"status": "error", "message": "Chat file not found."}

        messages = data.get("messages", [])
        if not isinstance(index, int) or index < 0 or index >= len(messages):
            return {"status": "error", "message": "That message no longer matches this position — try reloading the chat."}

        if expected_role and messages[index].get("role") != expected_role:
            return {"status": "error", "message": "That message has changed since you opened the editor — reload the chat and try again."}

        messages[index]["content"] = new_content

        try:
            db.chats.update_one(
                {"character": character, "chat_id": chat_id},
                {"$set": {"messages": messages, "updated_at": time.time()}}
            )
        except Exception as e:
            return {"status": "error", "message": f"Could not save changes: {e}"}

        # Keep the in-RAM session in sync if this happens to be the chat
        # currently loaded, so future replies see the edited context too.
        if self.CURRENT_CHAT.get("path") == path:
            current_messages = self.CURRENT_CHAT.get("messages", [])
            if index < len(current_messages):
                current_messages[index]["content"] = new_content

        return {"status": "success"}
    def create_new_chat(self, character):
        db = get_db()

        # Find the highest chat_id currently used for this character
        pipeline = [{"$match": {"character": character}}, {"$group": {"_id": None, "max_id": {"$max": "$chat_id"}}}]
        result = list(db.chats.aggregate(pipeline))
        chat_id = (result[0]["max_id"] if result else 0) + 1
        
        path = self._make_path(character, chat_id)
        first_msg = self.CHARACTER_FIRST_MESSAGES.get(character, "")
        initial_messages = [{"role": "assistant", "content": first_msg, "turn_id": 0}] if first_msg else []

        # Genre-keyed mood pool — each genre has its own set of session tints
        _session_genre = self.CHARACTER_GENRES.get(character, "romance")

        # Safely fetch the genre config and the mood pool
        _gc = get_genre_config(_session_genre)

        # [REMOVED RANDOM TINT] Let the context or first interaction define the mood.
        session_tint = ""
        data = {
            "chat_id": chat_id, "character": character, "title": f"Chat {chat_id}",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "messages": initial_messages,
            "session_tint": session_tint, "updated_at": time.time()
        }

        db.chats.insert_one(data)

        self.CURRENT_CHAT = data
        self.CURRENT_CHAT["path"] = path
        self.memory_cooldowns = {}

        initial_state = SceneState()

        # Build opening clothing state from character defaults
        char_clothing = self.CHARACTER_DEFAULT_CLOTHING.get(character, {})
        if isinstance(char_clothing, dict) and char_clothing:
            worn_items = [str(item) for item in char_clothing.values() if item and str(item).lower() not in ["nothing", "none"]]
            if worn_items:
                initial_state.physical_state = f"Standing normally, wearing {', '.join(worn_items)}."

        cid         = self.CHARACTER_IDS.get(character, "unknown")
        chat_id_str = str(chat_id)
        
        # Initialize the memory documents via your rewritten manager
        self.advanced_memory.initialize_chat_memory(character, chat_id_str)

        # Apply initial physical state and relationship tags to the DB
        rel_config = self.CHARACTER_SETTINGS.get(character, {}).get("relationship", {})
        rel_tag = rel_config.get("tag", "stranger") if rel_config else "stranger"
        
        try:
            db.chat_states.update_one(
                {"_id": f"{character}_{chat_id_str}"},
                {"$set": {
                    "scene.physical_state": initial_state.physical_state,
                    "scene.relationship_tag": rel_tag,
                    "scene.relationship_stage": "EARLY"
                }},
                upsert=True
            )
            if rel_config:
                print(f"[✅ REL SEED] Tag '{rel_tag}' seeded for {character}.")
        except Exception as e:
            print(f"[⚠️ REL SEED] Could not seed relationship tag: {e}")

        self._preload_ram_caches(character, chat_id_str)

        return data
        
    def load_chat(self, path):
        try:
            character, chat_id = self._parse_path(path)
            db = get_db()
            chat = db.chats.find_one({"character": character, "chat_id": chat_id})
            
            if not chat:
                return None

            # Strip the MongoDB specific _id so JSON serialization elsewhere doesn't break
            if "_id" in chat:
                del chat["_id"]

            chat["path"] = path
            chat.setdefault("messages", [])

            # ── TURN-ID BACKFILL ──────────────────────────────────────────────

            msgs = chat["messages"]
            needs_save = any(m.get("turn_id") is None for m in msgs)

            # Phase 3 — legacy chats won't have updated_at yet. 
            # Backfill it (using current time as a reasonable stand-in).
            if "updated_at" not in chat:
                chat["updated_at"] = time.time()
                needs_save = True

            if needs_save:
                turn_counter = 0
                i = 0
                while i < len(msgs):
                    if msgs[i].get("turn_id") is None:
                        if msgs[i]["role"] == "user":
                            turn_counter += 1
                            msgs[i]["turn_id"] = turn_counter
                            if (i + 1 < len(msgs)
                                    and msgs[i + 1]["role"] == "assistant"
                                    and msgs[i + 1].get("turn_id") is None):
                                msgs[i + 1]["turn_id"] = turn_counter
                                i += 2
                                continue
                        else:
                            turn_counter += 1
                            msgs[i]["turn_id"] = turn_counter
                    i += 1
                
                db.chats.update_one(
                    {"character": character, "chat_id": chat_id},
                    {"$set": {"messages": chat["messages"], "updated_at": chat["updated_at"]}}
                )
                print(f"[✅ MIGRATION] Backfilled turn_ids/updated_at for legacy chat: {path}")
            # ─────────────────────────────────────────────────────────────────

            # Clear per-session caches on chat switch to prevent state bleed
            self.CURRENT_CHAT = chat
            self.memory_cooldowns = {}

            # --- PRELOAD INTO RAM ---
            self._preload_ram_caches(chat["character"], str(chat["chat_id"]))
            return chat
        except Exception as e:
            print("LOAD ERROR:", e)
            return None
        
    def save_current_chat(self):
        if self.CURRENT_CHAT.get("path") and self.CURRENT_CHAT.get("messages") is not None:
            # Phase 3 — stamp every write so /sync can tell a client whether
            # this chat has changed since their last poll (`since=<ts>`).
            self.CURRENT_CHAT["updated_at"] = time.time()
            
            try:
                db = get_db()
                
                # Create a copy without "_id" or "path" for the DB to avoid schema pollution
                data_to_save = dict(self.CURRENT_CHAT)
                data_to_save.pop("_id", None)
                data_to_save.pop("path", None)

                db.chats.update_one(
                    {"character": data_to_save["character"], "chat_id": data_to_save["chat_id"]},
                    {"$set": data_to_save},
                    upsert=True
                )
            except Exception as e:
                print(f"[⚠️ DB ERROR] Could not save current chat: {e}")

    def delete_chat(self, path):
        try:
            character, chat_id = self._parse_path(path)
            db = get_db()
            
            result = db.chats.delete_one({"character": character, "chat_id": chat_id})
            
            if result.deleted_count > 0:
                if self.CURRENT_CHAT.get("path") == path:
                    self.CURRENT_CHAT = {"character": None, "chat_id": None, "path": None, "messages": []}
        except Exception as e:
            print(f"[⚠️ DB ERROR] Error deleting chat: {e}")
    @property
    def current_turn(self):
        """
        Dynamically calculates the current turn based on the active chat history.
        This ensures that when an undo happens (and messages are popped),
        the current_turn automatically and instantly rolls back.
        """
        msgs = self.CURRENT_CHAT.get("messages", [])
        if not msgs:
            return 0

        # Safely find the highest turn_id in the current reality
        return max([m.get("turn_id", 0) for m in msgs], default=0)
    def _get_groq_client(self, api: dict) -> OpenAI:
        key = api["api_key"]
        if key not in self.api_clients_cache:
            self.api_clients_cache[key] = OpenAI(
                base_url="https://api.groq.com/openai/v1",
                api_key=key,
                max_retries=0,
                http_client=self.http_client # <-- CRITICAL: Injects persistent connection
            )
        return self.api_clients_cache[key]
    # --- LLM API Calling ---
    def call_selected_model(self, messages, settings, model_choice):
        model_info = self.MODEL_OPTIONS.get(model_choice)
        if not model_info: return "Error: Selected model configuration not found."

        provider = model_info.get("provider", "openrouter")
        model_name = model_info["name"]

        

        if provider == "openrouter":
            locked_providers = model_info.get("providers", None)
            return self._call_cloud_with_rotation(model_name, messages, settings, locked_providers)
        elif provider == "groq":
            return self._call_groq_with_rotation(model_name, messages, settings)
        elif provider == "morph":  # <--- ADD THIS BLOCK
            return self._call_morph(model_name, messages, settings)
        elif provider == "meganova":
            return self._call_meganova(model_name, messages, settings)
        elif provider == "google":
            return self._call_google(model_name, messages, settings)
        elif provider == "mistral":
            return self._call_mistral(model_name, messages, settings)
        elif provider == "nvidia":
            return self._call_nvidia(model_name, messages, settings)
        elif provider == "puter":
            return self._call_puter(model_name, messages, settings)
        elif provider == "jiekou":
            return self._call_jiekou(model_name, messages, settings)
        elif provider == "modal":
            return self._call_modal_endpoint(model_info["url"], messages, settings)
        elif provider == "cohere":
            return self._call_cohere(model_name, messages, settings)
        return "Error: Unknown provider."

    def _call_groq_with_rotation(self, model_name, messages, settings):
        total_apis = len(self.GROQ_APIS)

        for _ in range(total_apis):
            api = self.GROQ_APIS[self.current_groq_index]

            if not api["api_key"]:
                self.current_groq_index = (self.current_groq_index + 1) % total_apis
                continue

            try:
                client = self._get_groq_client(api)

                response = client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    temperature=settings.get("temperature", 0.7),
                    max_tokens=settings.get("max_tokens", 500),
                    top_p=settings.get("top_p", 0.9),
                    stream=True,
                    stream_options={"include_usage": False} # ⚡ Tell the server to stop doing math
                )


                for chunk in response:
                    if hasattr(chunk, 'choices') and len(chunk.choices) > 0 and chunk.choices[0].delta.content is not None:
                        yield chunk.choices[0].delta.content
                return # Success! Exit the loop.

            except Exception as e:
                print(f"API {api['name']} failed: {e}")
                # Move to the next key if this one fails (e.g., rate limited)
                self.current_groq_index = (self.current_groq_index + 1) % total_apis

        yield "Error: All Groq APIs failed or reached their rate limits."

    def _call_morph(self, model_name, messages, settings):
        if not getattr(self, "MORPH_KEY", None) or not self.MORPH_KEY:
            yield "Error: MORPH_API_KEY not found in environment (.env)"
            return

        try:
            cache_key = "morph_" + self.MORPH_KEY
            if cache_key not in self.api_clients_cache:
                self.api_clients_cache[cache_key] = OpenAI(
                    base_url="https://api.morphllm.com/v1",
                    api_key=self.MORPH_KEY,
                    max_retries=0,
                    http_client=self.http_client
                )
            client = self.api_clients_cache[cache_key]

            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=settings.get("temperature", 0.7),
                max_tokens=settings.get("max_tokens", 500),
                top_p=settings.get("top_p", 0.9),
                stream=True
            )

            first_token_time = None
            MAX_GENERATION_TIME = 20.0

            for chunk in response:
                current_time = time.time()

                # Auto-kill tripwire logic to keep app responsive
                if first_token_time is None:
                    first_token_time = current_time
                elif (current_time - first_token_time) > MAX_GENERATION_TIME:
                    yield "\n\n[⚠️ AUTO-KILL: Model was too slow. Connection severed.]"
                    response.close()
                    break

                # Safely check if delta exists
                if hasattr(chunk, 'choices') and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    if delta is not None and getattr(delta, 'content', None) is not None:
                        yield delta.content
            return

        except Exception as e:
            yield f"Error: Morph API failed: {e}"

    def _call_meganova(self, model_name, messages, settings):
        if not hasattr(self, "MEGANOVA_KEY") or not self.MEGANOVA_KEY:
            yield "Error: MEGANOVA_KEY not found in environment (.env)"
            return

        try:
            # Re-use the http_client connection pool for speed
            cache_key = "meganova_" + self.MEGANOVA_KEY
            if cache_key not in self.api_clients_cache:
                self.api_clients_cache[cache_key] = OpenAI(
                    base_url="https://inference.meganova.ai/v1",
                    api_key=self.MEGANOVA_KEY,
                    max_retries=0,
                    http_client=self.http_client

                )
            client = self.api_clients_cache[cache_key]

            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=settings.get("temperature", 0.7),
                max_tokens=settings.get("max_tokens", 500),
                top_p=settings.get("top_p", 0.9),
                stream=True
            )

            first_token_time = None
            MAX_GENERATION_TIME = 20.0

            for chunk in response:
                current_time = time.time()

                # Auto-kill tripwire logic
                if first_token_time is None:
                    first_token_time = current_time
                elif (current_time - first_token_time) > MAX_GENERATION_TIME:
                    yield f"\n\n[⚠️ AUTO-KILL: Model was too slow. Connection severed.]"
                    response.close()
                    break

                if hasattr(chunk, 'choices') and len(chunk.choices) > 0 and chunk.choices[0].delta.content is not None:
                    yield chunk.choices[0].delta.content
            return

        except Exception as e:
            yield f"Error: Meganova API failed: {e}"

    def _call_google(self, model_name, messages, settings):
        if not getattr(self, "GEMINI_KEY", None) or not self.GEMINI_KEY:
            yield "Error: GEMINI_API_KEY not found in environment (.env)"
            return

        try:
            # Re-use the http_client connection pool via the OpenAI-compatible endpoint
            cache_key = "google_" + self.GEMINI_KEY
            if cache_key not in self.api_clients_cache:
                self.api_clients_cache[cache_key] = OpenAI(
                    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                    api_key=self.GEMINI_KEY,
                    max_retries=0

                )
            client = self.api_clients_cache[cache_key]

            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=settings.get("temperature", 0.7),
                max_tokens=settings.get("max_tokens", 500),
                top_p=settings.get("top_p", 0.9),
                stream=True
            )

            first_token_time = None
            MAX_GENERATION_TIME = 20.0

            for chunk in response:
                current_time = time.time()

                # Auto-kill tripwire logic
                if first_token_time is None:
                    first_token_time = current_time
                elif (current_time - first_token_time) > MAX_GENERATION_TIME:
                    yield f"\n\n[⚠️ AUTO-KILL: Model was too slow. Connection severed.]"
                    response.close()
                    break

                # THE FIX: Safely check if delta exists before looking for content
                if hasattr(chunk, 'choices') and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    if delta is not None and getattr(delta, 'content', None) is not None:
                        yield delta.content
            return

        except Exception as e:
            yield f"Error: Google AI Studio API failed: {e}"
    def _call_mistral(self, model_name, messages, settings):
        if not getattr(self, "MISTRAL_KEY", None) or not self.MISTRAL_KEY:
            yield "Error: MISTRAL_API_KEY not found in environment (.env)"
            return

        try:
            # Re-use the http_client connection pool via the OpenAI-compatible endpoint
            cache_key = "mistral_" + self.MISTRAL_KEY
            if cache_key not in self.api_clients_cache:
                self.api_clients_cache[cache_key] = OpenAI(
                    base_url="https://api.mistral.ai/v1",
                    api_key=self.MISTRAL_KEY,
                    max_retries=0,
                    http_client=self.http_client

                )
            client = self.api_clients_cache[cache_key]

            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=settings.get("temperature", 0.7),
                max_tokens=settings.get("max_tokens", 500),
                top_p=settings.get("top_p", 0.9),
                stream=True
            )

            first_token_time = None
            MAX_GENERATION_TIME = 20.0

            for chunk in response:
                current_time = time.time()

                # Auto-kill tripwire logic
                if first_token_time is None:
                    first_token_time = current_time
                elif (current_time - first_token_time) > MAX_GENERATION_TIME:
                    yield f"\n\n[⚠️ AUTO-KILL: Model was too slow. Connection severed.]"
                    response.close()
                    break

                # Safely check if delta exists before looking for content
                if hasattr(chunk, 'choices') and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    if delta is not None and getattr(delta, 'content', None) is not None:
                        yield delta.content
            return

        except Exception as e:
            yield f"Error: Mistral API failed: {e}"
    def _call_nvidia(self, model_name, messages, settings):
        if not getattr(self, "NVIDIA_KEY", None) or not self.NVIDIA_KEY:
            yield "Error: NVIDIA_NIM_KEY not found in environment (.env)"
            return

        try:
            # Re-use the http_client connection pool via the OpenAI-compatible endpoint
            cache_key = "nvidia_" + self.NVIDIA_KEY
            if cache_key not in self.api_clients_cache:
                self.api_clients_cache[cache_key] = OpenAI(
                    base_url="https://integrate.api.nvidia.com/v1",
                    api_key=self.NVIDIA_KEY,
                    max_retries=0,
                    http_client=self.http_client

                )
            client = self.api_clients_cache[cache_key]

            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=settings.get("temperature", 0.7),
                max_tokens=settings.get("max_tokens", 500),
                top_p=settings.get("top_p", 0.9),
                stream=True
            )

            first_token_time = None
            MAX_GENERATION_TIME = 20.0

            for chunk in response:
                current_time = time.time()

                # Auto-kill tripwire logic
                if first_token_time is None:
                    first_token_time = current_time
                elif (current_time - first_token_time) > MAX_GENERATION_TIME:
                    yield f"\n\n[⚠️ AUTO-KILL: Model was too slow. Connection severed.]"
                    response.close()
                    break

                # Safely check if delta exists before looking for content
                if hasattr(chunk, 'choices') and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    if delta is not None and getattr(delta, 'content', None) is not None:
                        yield delta.content
            return

        except Exception as e:
            yield f"Error: NVIDIA NIM API failed: {e}"
    def _call_puter(self, model_name, messages, settings):
        if not getattr(self, "PUTER_TOKEN", None) or not self.PUTER_TOKEN:
            yield "Error: PUTER_TOKEN not found in environment (.env)"
            return

        try:
            # Re-use the http_client connection pool via Puter's OpenAI endpoint
            cache_key = "puter_" + self.PUTER_TOKEN
            if cache_key not in self.api_clients_cache:
                self.api_clients_cache[cache_key] = OpenAI(
                    base_url="https://api.puter.com/puterai/openai/v1",
                    api_key=self.PUTER_TOKEN,
                    max_retries=0,
                    http_client=self.http_client
                )
            client = self.api_clients_cache[cache_key]

            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=settings.get("temperature", 0.7),
                max_tokens=settings.get("max_tokens", 500),
                top_p=settings.get("top_p", 0.9),
                stream=True
            )

            first_token_time = None
            MAX_GENERATION_TIME = 20.0

            for chunk in response:
                current_time = time.time()

                # Auto-kill tripwire logic
                if first_token_time is None:
                    first_token_time = current_time
                elif (current_time - first_token_time) > MAX_GENERATION_TIME:
                    yield f"\n\n[⚠️ AUTO-KILL: Model was too slow. Connection severed.]"
                    response.close()
                    break

                # Safely check if delta exists
                if hasattr(chunk, 'choices') and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    if delta is not None and getattr(delta, 'content', None) is not None:
                        yield delta.content
            return

        except Exception as e:
            yield f"Error: Puter API failed: {e}"
    def _call_jiekou(self, model_name, messages, settings):
        if not getattr(self, "JIEKOU_KEY", None) or not self.JIEKOU_KEY:
            yield "Error: JIEKOU_API_KEY not found in environment (.env)"
            return

        try:
            cache_key = "jiekou_" + self.JIEKOU_KEY
            if cache_key not in self.api_clients_cache:
                self.api_clients_cache[cache_key] = OpenAI(
                    base_url="https://api.jiekou.ai/openai",
                    api_key=self.JIEKOU_KEY,
                    max_retries=0,
                    http_client=self.http_client

                )
            client = self.api_clients_cache[cache_key]

            # --- NEW: Add top-level cache flag for proxies ---
            extra_body = {}
            if "claude" in model_name.lower():
                extra_body["cache_control"] = {"type": "ephemeral"}

            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=settings.get("temperature", 0.7),
                max_tokens=settings.get("max_tokens", 500),
                top_p=settings.get("top_p", 0.9),
                extra_body=extra_body, # <-- INJECT IT HERE
                stream=True
            )

            first_token_time = None
            MAX_GENERATION_TIME = 20.0
            for chunk in response:
                current_time = time.time()

                # Auto-kill tripwire logic to keep app responsive
                if first_token_time is None:
                    first_token_time = current_time
                elif (current_time - first_token_time) > MAX_GENERATION_TIME:
                    yield f"\n\n[⚠️ AUTO-KILL: Model was too slow. Connection severed.]"
                    response.close()
                    break

                # Safely parse streaming chunks
                if hasattr(chunk, 'choices') and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    if delta is not None and getattr(delta, 'content', None) is not None:
                        yield delta.content
            return

        except Exception as e:
            import traceback  # <--- ADD THIS
            print(f"\n🚨 JIEKOU ERROR TRACE 🚨\n{traceback.format_exc()}\n")
            yield f"Error: Jiekou AI API failed: {e}"
    def _call_cohere(self, model_name: str, messages: list, settings: dict):
        """
        Handles streaming requests to Cohere using the OpenAI Compatibility Layer.
        Utilizes the class's existing client cache and http_client pool.
        """
        if not getattr(self, "COHERE_KEY", None) or not self.COHERE_KEY:
            yield "Error: COHERE_API_KEY not found in environment (.env)"
            return

        try:
            # Maintain connection pooling via api_clients_cache
            cache_key = "cohere_" + self.COHERE_KEY
            if cache_key not in self.api_clients_cache:
                self.api_clients_cache[cache_key] = OpenAI(
                    base_url="https://api.cohere.ai/compatibility/v1",
                    api_key=self.COHERE_KEY,
                    max_retries=0,
                    http_client=self.http_client

                )

            client = self.api_clients_cache[cache_key]

            # Execute the streaming chat completion request
            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=settings.get("temperature", 0.3),
                max_tokens=settings.get("max_tokens", 1000),
                stream=True
            )

            # Yield token chunks as they arrive from the stream
            for chunk in response:
                if (
                    hasattr(chunk, 'choices')
                    and len(chunk.choices) > 0
                    and chunk.choices[0].delta.content is not None
                ):
                    yield chunk.choices[0].delta.content

        except Exception as e:
            yield f"Error: Cohere API failed: {e}"
    def _call_modal_endpoint(self, url, messages, settings):
        try:
            # Extract the user's latest content safely
            prompt_text = messages[-1]["content"] if messages else ""

            payload = {
                "prompt": prompt_text,
                "temperature": settings.get("temperature", 0.7),
                "max_tokens": settings.get("max_tokens", 500)
            }

            # Using the httpx client to handle the POST stream seamlessly
            with httpx.Client() as client:
                response = client.post(url, json=payload, timeout=45.0)
                response.raise_for_status()
                result = response.json()

                # Yield back text to support your generator pipeline architecture
                yield result.get("text", "")

        except Exception as e:
            yield f"Error connecting to Modal engine matrix: {e}"

    def _call_cloud_with_rotation(self, model_name, messages, settings, locked_providers=None):
        total_apis = len(self.CLOUD_APIS)
        for _ in range(total_apis):
            api = self.CLOUD_APIS[self.current_api_index]
            if not api["api_key"]:
                self.current_api_index = (self.current_api_index + 1) % total_apis
                continue
            try:
                # Use a similar caching method for OpenRouter
                cache_key = api["api_key"]
                if cache_key not in self.api_clients_cache:
                    self.api_clients_cache[cache_key] = OpenAI(
                        base_url=api["base_url"],
                        api_key=cache_key,
                        max_retries=0

                    )
                temp_client = self.api_clients_cache[cache_key]
                extra_body = {}
                if "repetition_penalty" in settings: extra_body["repetition_penalty"] = settings["repetition_penalty"]
                if "top_k" in settings: extra_body["top_k"] = settings["top_k"]

                # --- NEW: Add Auto-Caching for OpenRouter Claude ---
                if "claude" in model_name.lower():
                    extra_body["cache_control"] = {"type": "ephemeral"}

                if locked_providers:
                    extra_body["provider"] = {
                        "order": locked_providers,
                        "allow_fallbacks": False
                    }
                    print(f"[🔒 LOCK] {model_name} → {locked_providers}")

                response = temp_client.chat.completions.create(
                    model=model_name, messages=messages,
                    temperature=settings.get("temperature", 0.7),
                    max_tokens=settings.get("max_tokens", 500),
                    top_p=settings.get("top_p", 0.9),
                    extra_body=extra_body, stream=True # <-- REMOVE the old timeout=60 from here
                )
                for chunk in response:
                    if hasattr(chunk, 'choices') and len(chunk.choices) > 0 and chunk.choices[0].delta.content is not None:
                        yield chunk.choices[0].delta.content
                return

            except Exception as e:
                err_msg = str(e)
                if locked_providers and ("provider" in err_msg.lower() or "no endpoints" in err_msg.lower()):
                    yield f"Error: Provider '{locked_providers}' unavailable for '{model_name}'. Not falling back to protect credits."
                    return
                print(f"API {api['name']} failed: {e}")
                self.current_api_index = (self.current_api_index + 1) % total_apis

        yield "Error: All cloud APIs failed."
    def get_fact_string(self, character, chat_id):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context.get_fact_string(character, chat_id)

    def get_active_lore(self, text, char_lorebook):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context.get_active_lore(text, char_lorebook)

    def retrieve_relevant_memory(self, recent_messages, user_msg, character, chat_id):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context.retrieve_relevant_memory(recent_messages, user_msg, character, chat_id)

    def _get_beat_bank(self, character, chat_id):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context._get_beat_bank(character, chat_id)

    def _is_significant_memory(self, text):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context._is_significant_memory(text)

    def _background_index_memory(self, character, chat_id, user_text, bot_text, turn_id):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context._background_index_memory(character, chat_id, user_text, bot_text, turn_id)

    def _build_facts_text(self):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context._build_facts_text()

    def _build_scene_state_block(self, character_name, chat_id, use_scene_brain):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context._build_scene_state_block(character_name, chat_id, use_scene_brain)

    def _build_story_context(self, story_summary, rel_tag, scene_data_cache, genre, character_name):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context._build_story_context(story_summary, rel_tag, scene_data_cache, genre, character_name)

    def _build_narrative_wrapper(self, character_name, gc, turn_count,
                                  active_npcs, user_intent, tint_sentence, core_trait=""):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context._build_narrative_wrapper(character_name, gc, turn_count, active_npcs, user_intent, tint_sentence, core_trait)

    def _build_voice_blocks(self, character_name, turn_count, story_summary):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context._build_voice_blocks(character_name, turn_count, story_summary)

    def _get_current_datetime_str(self):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context._get_current_datetime_str()

    def _describe_time_gap(self, last_dt):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context._describe_time_gap(last_dt)

    def _build_companion_system_prompt(self, character_name, character_prompt, custom_rule_block,
                                        voice_and_examples, background_text, user_gender, memory_block,
                                        expectation_hint=""):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context._build_companion_system_prompt(character_name, character_prompt, custom_rule_block, voice_and_examples, background_text, user_gender, memory_block, expectation_hint)

    def build_structured_prompt(self, character_name, character_prompt, chat_id, recalled_memory, recent_messages, user_message, story_summary="", scene_data_cache=None, model_choice="", expectation_hint=""):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context.build_structured_prompt(character_name, character_prompt, chat_id, recalled_memory, recent_messages, user_message, story_summary, scene_data_cache, model_choice, expectation_hint)

    def post_process_response(self, text, char_name):
        """
        Cleans the AI output by removing hidden thoughts, AI-isms, and UI artifacts.
        """
        # 1. ALWAYS obliterate Qwen's <think> blocks (prevents them from poisoning the prompt history)
        text = re.sub(r'(?i)<think>.*?(?:</think>|$)', '', text, flags=re.DOTALL)

        # 2. Remove standard brackets based on toggle
        if not getattr(self, 'show_thoughts', False):
            text = re.sub(r'(?i)\[THOUGHTS?:.*?(?:\]|\n\n)', '', text, flags=re.DOTALL)
            text = re.sub(r'(?i)\*THOUGHTS?:.*?(?:\*|\n\n)', '', text, flags=re.DOTALL)
            text = re.sub(r'(?i)(?:^|\n)THOUGHTS?:.*?(?:\n\n|$)', '', text, flags=re.DOTALL)

        text = text.strip()

        # 2. Scrub standard AI-style apologies or meta-talk (Removed ^ so it catches them anywhere)
        text = re.sub(r"(?i)(As an AI|I am an AI|As a language model|I cannot fulfill).*?(?:\n|$)", "", text).strip()

        # 3. Clean up UI artifacts and character prefixes (Now catches *Name*: and **Name**:)
        text = re.sub(fr"^(\*{{0,2}}{char_name}\*{{0,2}}):\s*", "", text, flags=re.IGNORECASE).strip()

        # 4. Anti-Hallucination: Stop the bot from speaking for the user
        if f"{self.USER_NAME}:" in text:
            text = text.split(f"{self.USER_NAME}:")[0].strip()
        if "User:" in text:  # Fallback just in case
            text = text.split("User:")[0].strip()


        return text

    # --- Background Brain Tasks ---  full_prompt_messages = self.build_structured_prompt(

    # Changed signature to accept turn_id, current_arc, and model_choice
    def run_due_summaries(self, max_jobs=5, min_unsummarized=20):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context.run_due_summaries(max_jobs, min_unsummarized)

    def _load_proactive_schedule(self):
        # Migrated from proactive_schedule.json (local file, wiped on every
        # Render restart) to a single document in the `app_state` Mongo
        # collection. Same shape as before: a dict keyed by
        # "character:chat_id" -> {"date": ..., "kinds": {...}}.
        try:
            db = get_db()
            doc = db.app_state.find_one({"_id": "proactive_schedule"})
            return doc.get("schedule", {}) if doc else {}
        except Exception as e:
            print(f"[⚠️ SCHEDULE] Could not load from Mongo: {e}")
            return {}

    def _save_proactive_schedule(self, data):
        try:
            db = get_db()
            db.app_state.update_one(
                {"_id": "proactive_schedule"},
                {"$set": {"schedule": data}},
                upsert=True,
            )
        except Exception as e:
            print(f"[⚠️ SCHEDULE] Could not save to Mongo: {e}")

    def _random_time_in_window(self, day, start_h, start_m, end_h, end_m):
        """Random tz-aware (IST) datetime on `day` within [start, end)."""
        start_dt = datetime(day.year, day.month, day.day, start_h, start_m, tzinfo=IST)
        end_dt = datetime(day.year, day.month, day.day, end_h, end_m, tzinfo=IST)
        span_seconds = int((end_dt - start_dt).total_seconds())
        offset = random.randint(0, max(span_seconds, 0))
        return start_dt + timedelta(seconds=offset)

    def _window_end_dt(self, day, kind):
        _, _, end_h, end_m = self.PROACTIVE_WINDOWS[kind]
        return datetime(day.year, day.month, day.day, end_h, end_m, tzinfo=IST)

    def _get_or_create_today_schedule(self, character, chat_id):
        """
        Returns today's schedule entry for this character/chat, generating
        fresh random target times (and resetting sent-flags) the first time
        it's touched on a new calendar day (IST).
        """
        key = f"{character}:{chat_id}"
        today_str = datetime.now(IST).date().isoformat()
        schedule = self._load_proactive_schedule()
        entry = schedule.get(key)

        if not entry or entry.get("date") != today_str:
            today = datetime.now(IST).date()
            now = datetime.now(IST)
            entry = {"date": today_str, "kinds": {}}
            for kind, window in self.PROACTIVE_WINDOWS.items():
                target_dt = self._random_time_in_window(today, *window)
                # If this window has already fully closed by the time we're
                # first creating today's schedule (e.g. the cron is set up,
                # or first hit, partway through the day), mark it pre-sent
                # so it's silently skipped for today instead of firing late
                # the moment the check runs — it'll get a fresh, in-window
                # target tomorrow.
                already_missed = now > self._window_end_dt(today, kind)
                entry["kinds"][kind] = {"target": target_dt.isoformat(), "sent": already_missed}
            schedule[key] = entry
            self._save_proactive_schedule(schedule)

        return entry

    PROACTIVE_MIN_QUIET_MINUTES = 10  # don't interrupt (or race) an active conversation

    def check_and_send_due_proactive_messages(self, character, chat_id, model_choice=None):
        """
        Called on every /cron/proactive_check hit. Rolls (or fetches) today's
        random target times, and generates+marks-sent any kind whose target
        has passed and hasn't fired yet today.
        """
        key = f"{character}:{chat_id}"
        entry = self._get_or_create_today_schedule(character, chat_id)
        now = datetime.now(IST)
        due = []
        changed = False
        
        # Flex query: check both int and str chat_id to prevent type mismatches
        db = get_db()
        chat_id_val = int(chat_id) if str(chat_id).isdigit() else chat_id
        chat_doc = db.chats.find_one(
            {"character": character, "$or": [{"chat_id": chat_id_val}, {"chat_id": str(chat_id)}]},
            {"updated_at": 1}
        )

        for kind, info in entry["kinds"].items():
            if info["sent"]:
                continue
            target_dt = datetime.fromisoformat(info["target"])
            if now < target_dt:
                continue  # not due yet
            if now > self._window_end_dt(now.date(), kind):
                info["sent"] = True
                changed = True
                continue

            if chat_doc and "updated_at" in chat_doc:
                last_active = datetime.fromtimestamp(chat_doc["updated_at"], tz=IST)
                minutes_quiet = (now - last_active).total_seconds() / 60
                if minutes_quiet < self.PROACTIVE_MIN_QUIET_MINUTES:
                    continue

            text = self.generate_proactive_message(character, chat_id, kind, model_choice)
            due.append((kind, text))
            info["sent"] = True
            changed = True
 
        if changed:
            schedule = self._load_proactive_schedule()
            schedule[key] = entry
            self._save_proactive_schedule(schedule)

        return due 
    
    def generate_proactive_message(self, character, chat_id, kind, model_choice=None):
        """
        Synchronous, non-streaming companion message for external triggers
        (proactive scheduling). Reads the chat directly from the database 
        since this can fire with no live session open.
        """
        if not model_choice:
            model_choice = list(self.MODEL_OPTIONS.keys())[0]

        char_prompt = self.CHARACTERS.get(character, "")
        if not char_prompt:
            raise ValueError(f"Unknown character: {character}")

        db = get_db()
        chat_id_val = int(chat_id) if str(chat_id).isdigit() else chat_id
        chat_data = db.chats.find_one({
            "character": character,
            "$or": [{"chat_id": chat_id_val}, {"chat_id": str(chat_id)}]
        })
        
        if not chat_data:
            raise ValueError(f"No chat record found in DB for {character}/{chat_id}")

        recent_history = chat_data.get("messages", [])
        
        last_updated_ts = chat_data.get("updated_at", time.time())
        last_msg_dt = datetime.fromtimestamp(last_updated_ts, tz=IST)
        time_gap_hint = self._describe_time_gap(last_msg_dt)

        cid = self.CHARACTER_IDS.get(character, "unknown")
        summary_data = self.advanced_memory.load_summary(cid, chat_id)

        char_settings = self.CHARACTER_SETTINGS.get(character, {})
        gen_settings = {
            "temperature": char_settings.get("temperature", 0.82),
            "top_p": char_settings.get("top_p", 0.85),
            "top_k": char_settings.get("top_k", 40),
            "repetition_penalty": char_settings.get("repetition_penalty", 1.05),
            "max_tokens": char_settings.get("max_tokens", 300),
        }

        expectation_hint = ""
        try:
            due = self.expectations.get_due_followups(cid, chat_id, limit=1)
            if due:
                expectation_hint = self.expectations.format_hint(due[0])
        except Exception as e:
            print(f"[⚠️ EXPECTATION] proactive due-check failed: {e}")

        if expectation_hint:
            synthetic_prompt = (
                f"[You're reaching out to {self.USER_NAME} first, completely unprompted. "
                f"Something specific has been on your mind — see below. Bring it up "
                f"naturally, the way you actually would, not as a checklist item. Stay "
                f"fully in character.]"
            )
        else:
            synthetic_prompt = (
                f"[You're reaching out to {self.USER_NAME} first, completely unprompted — nothing "
                f"happened, no one messaged you, you just felt like it. Stay fully in character and "
                f"let the real time of day and your own personality decide what that looks like — "
                f"sweet, teasing, sarcastic, low-key, annoyed they've been quiet, whatever's actually "
                f"true to you right now. Don't default to a generic greeting; say something real.]"
            )

        combined_hint = f"{time_gap_hint}\n{expectation_hint}".strip() if expectation_hint else time_gap_hint

        full_prompt_messages = self.build_structured_prompt(
            character_name=character,
            character_prompt=char_prompt,
            chat_id=chat_id,
            recalled_memory="",
            recent_messages=recent_history,
            user_message=synthetic_prompt,
            story_summary=summary_data,
            scene_data_cache=self.ram_scene_cache or {},
            model_choice=model_choice,
            expectation_hint=combined_hint,
        )

        response = self.call_selected_model(full_prompt_messages, gen_settings, model_choice)

        full_reply = ""
        if hasattr(response, "__iter__") and not isinstance(response, str):
            for chunk in response:
                if chunk:
                    full_reply += chunk
        else:
            full_reply = response or ""

        full_reply = self.post_process_response(full_reply, character)
        if full_reply.strip().startswith("Error:"):
            raise RuntimeError(full_reply.strip())

        new_turn_id = (recent_history[-1].get("turn_id", 0) + 1) if recent_history else 1
        
        # Append the new message and stamp the time
        chat_data["messages"].append({"role": "assistant", "content": full_reply, "turn_id": new_turn_id})
        chat_data["updated_at"] = time.time()
        
        # Strip _id to avoid schema issues, then update DB
        chat_data.pop("_id", None)
        db.chats.update_one(
            {"character": character, "chat_id": int(chat_id)},
            {"$set": chat_data}
        )

        return full_reply
    def generate_morning_message(self, character, chat_id, model_choice=None):
        """Kept for backward compatibility — thin wrapper around the
        generalized generate_proactive_message(). Prefer calling that
        directly, or (better) let /cron/proactive_check drive all three
        message kinds off the random daily schedule."""
        return self.generate_proactive_message(character, chat_id, "morning", model_choice)
    def _is_repetitive_reply(self, new_text, recent_history, lookback=3, threshold=0.6):
        """
        Checks new_text against the bot's own last few replies. A smaller
        model (e.g. Mistral Nemo 12B on the Telegram path) can get anchored
        on the fixed voice-fingerprint example pinned into every system
        prompt (see _build_voice_blocks) and just parrot it near-verbatim,
        ignoring what the user actually said — this shows up as near-
        identical replies turn after turn regardless of the input.
        difflib's ratio() is a cheap, dependency-free similarity score (1.0
        = identical text); no need for embeddings/an LLM call just to
        detect "this is basically the same line as last time."
        """
        recent_assistant = [
            m["content"] for m in recent_history[-lookback * 2:]
            if m.get("role") == "assistant" and m.get("content")
        ]
        for prior in recent_assistant:
            ratio = difflib.SequenceMatcher(None, new_text, prior).ratio()
            if ratio >= threshold:
                return True
        return False

    def generate_reply_sync(self, user_text, character, chat_id, model_choice=None):
        """
        Non-streaming counterpart to generate_reply(), for triggers that
        aren't the web UI's SSE connection (Telegram webhook, in this case).
        Reads/writes the chat directly from MongoDB.
        """
        # Read fallback model from environment variable with safe default
        FALLBACK_MODEL = os.getenv("TELEGRAM_FALLBACK_MODEL", "Groq LLaMA 3.3 70B  (Brain)").strip()

        if not model_choice:
            model_choice = os.getenv("TELEGRAM_PRIMARY_MODEL", list(self.MODEL_OPTIONS.keys())[0]).strip()

        char_prompt = self.CHARACTERS.get(character, "")
        if not char_prompt:
            raise ValueError(f"Unknown character: {character}")

        db = get_db()
        chat_data = db.chats.find_one({"character": character, "chat_id": int(chat_id)})
        
        if not chat_data:
            raise ValueError(f"No chat record found in DB for {character}/{chat_id}")

        last_updated_ts = chat_data.get("updated_at", time.time())
        last_msg_dt = datetime.fromtimestamp(last_updated_ts, tz=IST)
        time_gap_hint = self._describe_time_gap(last_msg_dt)

        recent_history = chat_data.get("messages", [])

        cid = self.CHARACTER_IDS.get(character, "unknown")
        summary_data = self.advanced_memory.load_summary(cid, chat_id)

        char_settings = self.CHARACTER_SETTINGS.get(character, {})
        gen_settings = {
            "temperature": char_settings.get("temperature", 0.82),
            "top_p": char_settings.get("top_p", 0.85),
            "top_k": char_settings.get("top_k", 40),
            "repetition_penalty": char_settings.get("repetition_penalty", 1.05),
            "max_tokens": char_settings.get("max_tokens", 650),
        }

        recalled_memory = ""
        if getattr(self, 'enable_vector_recall', False):
            recalled_memory = self.retrieve_relevant_memory(recent_history, user_text, character, chat_id)

        full_prompt_messages = self.build_structured_prompt(
            character_name=character,
            character_prompt=char_prompt,
            chat_id=chat_id,
            recalled_memory=recalled_memory, 
            recent_messages=recent_history,
            user_message=user_text,
            story_summary=summary_data,
            scene_data_cache=self.ram_scene_cache or {},
            model_choice=model_choice,
            expectation_hint=time_gap_hint,
        )

        def _try_model(model_name):
            response = self.call_selected_model(full_prompt_messages, gen_settings, model_name)
            text = ""
            if hasattr(response, "__iter__") and not isinstance(response, str):
                for chunk in response:
                    if chunk:
                        text += chunk
            else:
                text = response or ""
            return self.post_process_response(text, character)

        full_reply = _try_model(model_choice)

        if full_reply.strip().startswith("Error:"):
            print(f"[⚠️ TELEGRAM] Primary model '{model_choice}' failed, trying fallback '{FALLBACK_MODEL}'")
            full_reply = _try_model(FALLBACK_MODEL)
        elif self._is_repetitive_reply(full_reply, recent_history):
            print(f"[⚠️ TELEGRAM] Primary model '{model_choice}' looked stuck parroting a recent line, "
                  f"retrying with fallback '{FALLBACK_MODEL}'")
            retry_reply = _try_model(FALLBACK_MODEL)
            if not retry_reply.strip().startswith("Error:"):
                full_reply = retry_reply

        if full_reply.strip().startswith("Error:"):
            raise RuntimeError(full_reply.strip())

        last_turn_id = recent_history[-1].get("turn_id", 0) if recent_history else 0
        chat_data["messages"].append({"role": "user", "content": user_text, "turn_id": last_turn_id + 1})
        chat_data["messages"].append({"role": "assistant", "content": full_reply, "turn_id": last_turn_id + 2})
        chat_data["updated_at"] = time.time()

        # Update MongoDB
        chat_data.pop("_id", None)
        db.chats.update_one(
            {"character": character, "chat_id": int(chat_id)},
            {"$set": chat_data}
        )

        return full_reply
    def update_story_summary(self, character, chat_id, full_history, turn_id, current_arc, model_choice):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context.update_story_summary(character, chat_id, full_history, turn_id, current_arc, model_choice)

    def _call_brain_with_fallback(self, brain_msgs, brain_settings):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context._call_brain_with_fallback(brain_msgs, brain_settings)

    def _call_with_groq_mistral_fallback(self, messages, settings, model_choice):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context._call_with_groq_mistral_fallback(messages, settings, model_choice)

    def _run_background_brain_update(self, character, chat_id, recent_messages, turn_id, current_arc="", total_msgs=0):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context._run_background_brain_update(character, chat_id, recent_messages, turn_id, current_arc, total_msgs)

    def _post_generation_filter(self, reply: str, character: str, chat_id: str, scene_data: dict = None) -> dict:
        """Lightweight post-generation filter — Persona breaks only."""
        reply_lower = reply.lower()

        # ── Category 3 — Persona break ───────────────────────────────────────
        _persona_break_phrases = [
            "as an ai", "i am an ai", "i'm an ai", "as a language model",
            "i cannot roleplay", "i must clarify", "i should mention",
            "i'm just a", "as your assistant"
        ]
        for phrase in _persona_break_phrases:
            if phrase in reply_lower:
                return {
                    "clean":      False,
                    "category":   3,
                    "reason":     f"Persona break phrase detected: '{phrase}'",
                    "hard_block": (
                            f"[PERSONA GUARDRAIL: You are the living, breathing entity {character}. "
                            f"Rewrite your response purely from the gritty, first-person perspective "
                            f"of the character in the scene.]"
                        )
                }

        # Return clean for everything else to allow flexible, uninhibited RP
        return {"clean": True, "category": 0, "reason": "", "hard_block": ""}

    def _prefetch_memory(self, recent_history, last_user_text, character, chat_id):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context._prefetch_memory(recent_history, last_user_text, character, chat_id)

    def stop_generation(self):
        """Triggers the tripwire to sever the API connection mid-stream."""
        self.cancel_flag.set()
        print("[⏹️ INTERRUPT] Cancel signal sent!")

    def generate_reply(self, user_text, character, model_choice, on_chunk, on_complete, on_error):
        import time
        t_start = time.time()
        print("\n" + "="*40)
        print("=== 🚀 STARTING NEW TURN DIAGNOSTICS ===")
        print("="*40)
        

        try:
           
            with open(self.USER_MESSAGES_LOG, "a", encoding="utf-8") as f:
                f.write(user_text + "\n")

            
            chat_id = self.CURRENT_CHAT.get("chat_id", "default")
            char_prompt = self.CHARACTERS.get(character, "")
            char_settings = self.CHARACTER_SETTINGS.get(character, {})

            cid = self.CHARACTER_IDS.get(character, "unknown")
            recent_history = self.CURRENT_CHAT.get("messages", [])

           
            scene_file = self.advanced_memory.get_scene_file(character, chat_id)

           
            summary_data = self.advanced_memory.load_summary(cid, chat_id)

           
            scene_data_cache = self.ram_scene_cache or {}

            t_disk = time.time()
            print(f"[DIAG 7] Disk reads done. Waiting for prefetch_lock...")

            with self._prefetch_lock:
                print("[DIAG 8] Entered prefetch_lock! Clearing memory...")
                recalled_memory = self._prefetched_memory or ""
                self._prefetched_memory = None

            
            if getattr(self, 'enable_vector_recall', False) and not recalled_memory:
                print("[DIAG 10] Running retrieve_relevant_memory()...")
                recalled_memory = self.retrieve_relevant_memory(recent_history, user_text, character, chat_id)

            
            last_updated_ts = self.CURRENT_CHAT.get("updated_at")
            last_msg_dt = datetime.fromtimestamp(last_updated_ts, tz=IST) if last_updated_ts else None
            time_gap_hint = self._describe_time_gap(last_msg_dt) if last_msg_dt else ""

            
            expectation_hint = ""
            try:
                expectation_hint = self.expectations.check_expectation_trigger(
                    cid, chat_id, user_text, self.current_turn
                ) or ""
            except Exception as e:
                print(f"[⚠️ EXPECTATION] check_trigger failed: {e}")

            if time_gap_hint:
                expectation_hint = f"{time_gap_hint}\n{expectation_hint}".strip() if expectation_hint else time_gap_hint

            gen_settings = {
                "temperature": char_settings.get("temperature", 0.82),
                "top_p": char_settings.get("top_p", 0.85),
                "top_k": char_settings.get("top_k", 40),
                "repetition_penalty": char_settings.get("repetition_penalty", 1.05),
                "max_tokens": char_settings.get("max_tokens", 650)
            }
            char_type = self.CHARACTER_TYPES.get(character, "roleplay")

            
            if char_type == "companion":
                full_prompt_messages = self.memory_context.build_structured_prompt(
                    character_name=character,
                    character_prompt=char_prompt,
                    chat_id=chat_id,
                    recalled_memory=recalled_memory,
                    recent_messages=recent_history,
                    user_message=user_text,
                    story_summary=summary_data,
                    scene_data_cache=scene_data_cache,
                    model_choice=model_choice,
                    expectation_hint=expectation_hint
                )
            else:
                full_prompt_messages = self.memory_context.build_legacy_prompt(
                    character_name=character,
                    character_prompt=char_prompt,
                    chat_id=chat_id,
                    recalled_memory=recalled_memory,
                    recent_messages=recent_history,
                    user_message=user_text,
                    story_summary=summary_data,
                    scene_data_cache=scene_data_cache,
                    model_choice=model_choice,
                    expectation_hint=expectation_hint
                )
            

            if self.debug_mode:
                payload_log_path = os.path.join(self.BASE_DIR, "debug_last_prompt.json")
                payload_snapshot = [dict(m) for m in full_prompt_messages]
                import threading # Just in case it wasn't at the top
                threading.Thread(
                    target=lambda: json.dump(payload_snapshot, open(payload_log_path, "w", encoding="utf-8"), indent=4, ensure_ascii=False),
                    daemon=True
                ).start()

            

            new_turn_id = self.current_turn + 1
            self.CURRENT_CHAT["messages"].append({"role": "user", "content": user_text, "turn_id": new_turn_id})
            self.save_current_chat()
            # Arm the tripwire for this new generation
            self.cancel_flag.clear()

            t_api_start = time.time()
            print(f"[⏱️ TTFT Tracker] Starting API Call... (Pre-API overhead: {t_api_start - t_start:.3f}s)")

            response_generator = self.call_selected_model(full_prompt_messages, gen_settings, model_choice)
            if not response_generator:
                on_error("No reply received.")
                return

            full_reply = ""
            first_token_flag = False

            in_thought_block = False
            in_qwen_thought = False
            hide_thoughts = not getattr(self, 'show_thoughts', False)

            for chunk in response_generator:
                # --- NEW: Check if the user hit stop ---
                if self.cancel_flag.is_set():
                    print("[⏹️ CONNECTION SEVERED] Stopping stream to save credits.")
                    response_generator.close() # <--- This kills the HTTP socket!
                    break

                if chunk:
                    if not first_token_flag:
                        t_first_chunk = time.time()
                        print(f"[⏱️ TTFT Tracker] API Response Time: {t_first_chunk - t_api_start:.3f}s")
                        print(f"[⏱️ TTFT Tracker] Total App Latency: {t_first_chunk - t_start:.3f}s")
                        first_token_flag = True

                    full_reply += chunk

                    # ALWAYS hide Qwen's <think> tags from the live UI stream
                    if "<think>" in chunk or "<think>" in full_reply[-10:].lower():
                        in_qwen_thought = True

                    if in_qwen_thought:
                        if "</think>" in chunk or "</think>" in full_reply[-12:].lower():
                            in_qwen_thought = False
                        continue # Skip yielding to UI

                    if hide_thoughts:
                        # Fast O(1) state machine instead of O(N^2) regex
                        if "[" in chunk or "*THOUGHT" in full_reply[-15:].upper():
                            in_thought_block = True

                        if in_thought_block:
                            if "]" in chunk or "\n\n" in full_reply[-5:]:
                                in_thought_block = False
                            continue # Skip yielding to UI while inside a thought

                    # Yield ONLY the new chunk, not the whole string
                    on_chunk(chunk)
            # ==========================================
            # POST-PROCESS AND FINISH
            # ==========================================
            clean_reply = self.post_process_response(full_reply, character)

            # ── FIX 2: POST-GENERATION FILTER ────────────────────────────────

            _filter_result = self._post_generation_filter(
                clean_reply, character, chat_id, scene_data=scene_data_cache
            )
            if not _filter_result["clean"] and _filter_result["category"] in (1, 3):
                print(
                    f"[🛡️ FILTER CAT-{_filter_result['category']}] "
                    f"{_filter_result['reason']} — triggering single regen."
                )
                _regen_messages = [dict(m) for m in full_prompt_messages]
                _regen_messages[0]["content"] = (
                    _filter_result["hard_block"] + "\n\n"
                    + _regen_messages[0]["content"]
                )
                _regen_generator = self.call_selected_model(
                    _regen_messages, gen_settings, model_choice
                )
                if _regen_generator:
                    _regen_raw = ""
                    for chunk in _regen_generator:
                        if chunk:
                            _regen_raw += chunk
                            display_text = _regen_raw
                            # ALWAYS hide <think> in continuation stream
                    display_text = re.sub(r'(?i)<think>.*?(?:</think>|$)', '', display_text, flags=re.DOTALL)

                    if not getattr(self, 'show_thoughts', False):
                        display_text = re.sub(r'(?i)\[THOUGHTS?:.*?(\]|$)', '', display_text, flags=re.DOTALL)
                        display_text = re.sub(r'(?i)\*THOUGHTS?:.*?(\*|\n\n|$)', '', display_text, flags=re.DOTALL)
                        display_text = re.sub(r'(?i)(?:^|\n)THOUGHTS?:.*?(\n\n|$)', '', display_text, flags=re.DOTALL)

                    on_chunk(display_text.strip())
                    if _regen_raw and "Error:" not in _regen_raw:
                        full_reply  = _regen_raw
                        clean_reply = self.post_process_response(full_reply, character)
                        print(f"[✅ FILTER] Regen succeeded for CAT-{_filter_result['category']}.")
                    else:
                        print(f"[⚠️ FILTER] Regen failed — keeping original reply.")
            # ─────────────────────────────────────────────────────────────────

            # ==========================================
            # UPDATE HISTORY
            # ==========================================
            # clean_reply is the single source of truth — already fully processed
            # by post_process_response (THOUGHTS, AI phrases, prefixes, asterisks).
            self.CURRENT_CHAT["messages"].append({"role": "assistant", "content": clean_reply, "turn_id": new_turn_id})
            self.save_current_chat()
            # ---------------------------------------------------

            on_complete(clean_reply)
            # ==========================================
            # BACKGROUND TASKS
            # ==========================================

            # 0. Phase 4 — cheap rule-based scan for a new reminder-worthy
            # mention in the user's message. Backgrounded even though it's
            # fast, to keep the hot path exclusively about the reply itself.
            threading.Thread(
                target=self._maybe_store_expectation,
                args=(character, chat_id, user_text, new_turn_id),
                daemon=True
            ).start()

            # 1. Background Vector Indexing (FAISS) — DISABLED: vector memory is
            # now a no-op stand-in (see memory_manager.NullVectorMemory), so this
            # background thread used to spend CPU indexing into a store nothing
            # ever reads from. Re-enable only if real vector recall comes back.
            threading.Thread(
                target=self._background_index_memory,
                args=(character, chat_id, user_text, clean_reply, new_turn_id),
                daemon=True
            ).start()

            # 1b. Background Brain Update (scene state, emotional state, facts,
            # and — via significant_shift_detected — the EmotionalBeatBank).
            # Toggle: self.enable_brain_update
            if getattr(self, 'enable_brain_update', False):
                if self.brain_writer_lock.acquire(blocking=False):
                    def _brain_task_with_release(character, chat_id, messages, turn_id, arc, total_msgs):
                        try:
                            self._run_background_brain_update(character, chat_id, messages, turn_id, arc, total_msgs)
                        finally:
                            self.brain_writer_lock.release()

                    # Only the latest exchange — the brain prompt is scoped to
                    # "[RECENT EXCHANGE]", not the whole conversation.
                    _brain_recent = self.CURRENT_CHAT["messages"][-2:]
                    threading.Thread(
                        target=_brain_task_with_release,
                        args=(
                            character,
                            chat_id,
                            _brain_recent,
                            new_turn_id,
                            summary_data.get("current_arc", ""),
                            len(self.CURRENT_CHAT["messages"]),
                        ),
                        daemon=True
                    ).start()
                else:
                    print("[⚠️ BRAIN THREAD] Brain writer lock held — skipping this turn's brain update.")

            # 2. Trigger Story Summary (Narrative Arc)
            # Check the toggle before doing any math or spawning threads
            if getattr(self, 'enable_summary_engine', True):
                last_index = summary_data.get("last_summarized_index", 0)
                unsummarized_messages = len(self.CURRENT_CHAT["messages"]) - last_index

                if unsummarized_messages >= 20:
                    if self.summary_writer_lock.acquire(blocking=False):
                        def _summary_task_with_release(character, chat_id, messages, turn_id, arc, model):
                            try:
                                self.update_story_summary(character, chat_id, messages, turn_id, arc, model)
                            finally:
                                self.summary_writer_lock.release()

                        threading.Thread(
                            target=_summary_task_with_release,
                            args=(
                                character,
                                chat_id,
                                self.CURRENT_CHAT["messages"],
                                new_turn_id,
                                summary_data.get("current_arc", ""),
                                self.BRAIN_MODEL
                            ),
                            daemon=True
                        ).start()
                    else:
                        print("[⚠️ SUMMARY THREAD] Summary writer lock held — skipping this turn's story summary.")
            else:
                # Silently skip the 3 Groq calls when the toggle is off
                pass

        except Exception as e:
            import traceback
            print("\n" + "="*50)
            print("🚨 FATAL ERROR IN GENERATE_REPLY 🚨")
            traceback.print_exc()
            print("="*50 + "\n")
            on_error(str(e))
    def _estimate_tokens(self, text):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context._estimate_tokens(text)

    def get_dynamic_history(self, recent_messages, max_history_tokens=2000, max_turns=4):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context.get_dynamic_history(recent_messages, max_history_tokens, max_turns)

    def generate_scene(self, character, model_choice):
        # Moved to memory_context.py (MemoryContextEngine) — see that file.
        return self.memory_context.generate_scene(character, model_choice)

    def generate_image_pollinations(self, prompt):
        # 1. Aggressively truncate the prompt!
        # Cloudflare and Nginx servers reject URLs with massively long paths.
        short_prompt = prompt[:400].strip()

        # 2. Clean and encode the prompt
        safe_prompt = requests.utils.quote(short_prompt)

        # 3. Pull the API key securely from your .env file
        api_key = os.getenv("POLLINATIONS_KEY", "")

        # 4. Use the new Gen endpoint with the key, enhance, and nologo flags
        url = f"https://gen.pollinations.ai/image/{safe_prompt}?model=grok-imagine&width=1024&height=1024&enhance=true&nologo=true&key={api_key}"

        try:
            # Increased timeout to 60s as per your working script
            resp = requests.get(url, timeout=60)

            # This triggers an exception if the server returns a 4xx or 5xx error
            resp.raise_for_status()

            # 5. Verify the response is actually an image before saving
            if 'image' in resp.headers.get('content-type', ''):
                image_bytes = resp.content

                # Save it to your images directory
                path = os.path.join(self.IMAGES_DIR, f"pollinations_{int(time.time())}.png")
                Image.open(BytesIO(image_bytes)).convert("RGBA").save(path)

                return path
            else:
                return f"ERROR: Server did not return an image. Status {resp.status_code}."

        except requests.exceptions.HTTPError as e:
            # We catch the HTTP error specifically so we DO NOT print the giant URL to the UI
            return f"ERROR: Image server rejected the request (Status {e.response.status_code})."
        except Exception as e:
            # Generic fallback that keeps the UI clean
            return "ERROR: Could not connect to the image generation server right now."

    def strip_emojis(self, text):
        return self.emoji_pattern.sub("", text)

    def analyze_english(self, model_choice):
        if not os.path.exists(self.USER_MESSAGES_LOG) or os.path.getsize(self.USER_MESSAGES_LOG) == 0: return None
        messages = open(self.USER_MESSAGES_LOG, "r", encoding="utf-8").read().strip()
        prompt = f"You are an expert English tutor. Review these raw messages. Identify grammatical/spelling mistakes. Provide a structured, encouraging summary.\n\nUser Messages:\n{messages}"
        feedback = self.call_selected_model([{"role": "system", "content": "You are a precise English tutor."}, {"role": "user", "content": prompt}], {"temperature": 0.2, "max_tokens": 1000}, model_choice)
        with open(self.ENGLISH_FEEDBACK_FILE, "w", encoding="utf-8") as f: f.write(feedback)
        open(self.USER_MESSAGES_LOG, "w").close()
        return feedback

    def get_english_feedback(self):
        if os.path.exists(self.ENGLISH_FEEDBACK_FILE):
            return open(self.ENGLISH_FEEDBACK_FILE, "r", encoding="utf-8").read()
        return None

    def listen_voice(self):
        if sr is None: return "ERROR: SpeechRecognition not installed."
        try:
            recognizer = sr.Recognizer()
            with sr.Microphone() as source:
                recognizer.adjust_for_ambient_noise(source, duration=0.8)
                audio = recognizer.listen(source, timeout=8, phrase_time_limit=20)
            return recognizer.recognize_google(audio)
        except sr.WaitTimeoutError: return "ERROR: Listening timed out."
        except sr.UnknownValueError: return "ERROR: Could not understand audio."
        except Exception as e: return f"ERROR: {e}"

    def impersonate_user(self, character, model_choice):
        """
        Generates a plausible next message FROM the user's perspective.
        Prompt is structured as a labelled transcript so the LLM has no
        role-confusion — it is not continuing an assistant turn, it is
        generating a human response to the character's last message.
        """
        msgs = self.CURRENT_CHAT.get("messages", [])[-6:]

        # Build a plain-text transcript so role structure does not bleed into output
        transcript_lines = []
        for m in msgs:
            content = m["content"].replace("{{user}}", self.USER_NAME).replace("{{char}}", character)
            # Strip character action formatting for clarity
            content = re.sub(r'\[THOUGHTS?:.*?(?:\]|\n\n)', '', content, flags=re.DOTALL).strip()
            if m["role"] == "user":
                transcript_lines.append(f"{self.USER_NAME}: {content}")
            elif m["role"] == "assistant":
                transcript_lines.append(f"{character}: {content}")

        transcript = "\n".join(transcript_lines)

        # Fresh prompt — LLM is asked to generate user text, not continue character
        prompt = (
            f"The following is a conversation between {self.USER_NAME} and {character}.\n\n"
            f"{transcript}\n\n"
            f"Write ONLY the next short message {self.USER_NAME} would say. "
            f"Write in {self.USER_NAME}'s voice — casual, human, natural. "
            f"Do NOT write as {character}. Do NOT use asterisks or action formatting. "
            f"Do NOT include any prefix like '{self.USER_NAME}:'. "
            f"Output the message text only."
        )

        context = [
            {"role": "system", "content": f"You generate realistic human dialogue for {self.USER_NAME} in a roleplay conversation. Output only the message text, nothing else."},
            {"role": "user", "content": prompt}
        ]

        reply = "".join(self.call_selected_model(
            context,
            {"temperature": 0.8, "max_tokens": 80},
            model_choice
        ))

        # Strip any prefix the model accidentally included
        cleaned = re.sub(r'<\|.*?\|>', '', reply)
        cleaned = re.sub(
            r'^(' + re.escape(self.USER_NAME) + r'|User|Human|Assistant|' + re.escape(character) + r'):\s*',
            '', cleaned, flags=re.IGNORECASE
        ).strip().strip('"')

        return cleaned
    def undo(self):
        msgs = self.CURRENT_CHAT.get("messages", [])
        if not msgs: return None

        idx = len(msgs) - 1
        if msgs[idx]["role"] == "assistant":
            idx -= 1

        if idx < 0 or msgs[idx]["role"] != "user":
            return None  # nothing safe to undo — leave history untouched

        if msgs[-1]["role"] == "assistant": msgs.pop()
        prev = msgs.pop()
        self.save_current_chat()

        character = self.CURRENT_CHAT.get("character")
        chat_id = self.CURRENT_CHAT.get("chat_id")
        cid = self.CHARACTER_IDS.get(character, "unknown")

        summary_data = self.advanced_memory.load_summary(cid, chat_id)
        checkpoints = summary_data.get("checkpoints", {})
        checkpoint_restored = False

        if checkpoints:
            valid_turns = [int(t) for t in checkpoints.keys() if int(t) <= self.current_turn]

            if valid_turns:
                best_turn = max(valid_turns)
                best_checkpoint = checkpoints[str(best_turn)]

                # Restore Summary Data (Narrative)
                summary_data["current_arc"] = best_checkpoint["current_arc"]
                summary_data["chronicle"] = list(best_checkpoint["chronicle"])
                summary_data["last_summarized_index"] = best_checkpoint["last_summarized_index"]

                # Restore Physical State to MongoDB and RAM
                if "physical_state" in best_checkpoint:
                    db = get_db()
                    db.chat_states.update_one(
                        {"_id": f"{character}_{chat_id}"},
                        {"$set": {"scene": best_checkpoint["physical_state"]}},
                        upsert=True
                    )
                    
                    restored_state = SceneState.from_dict(best_checkpoint["physical_state"])
                    self.ram_state_cache = restored_state  # keep RAM in sync with restored DB state

                # Prune future checkpoints that we just erased
                summary_data["checkpoints"] = {t: c for t, c in checkpoints.items() if int(t) <= self.current_turn}
                self.advanced_memory.save_summary(cid, chat_id, summary_data)

                checkpoint_restored = True
                print(f"[✅ UNDO] State restored locally from Turn {best_turn}. API call skipped.")

        return prev.get("content", "")

        
    def regenerate_reply(self, character, model_choice, on_chunk, on_complete, on_error):
        # Step 1: Use the robust undo() method to revert the timeline.
        # This safely pops the messages AND restores the tiered summary and physical SceneState.
        last_user_text = self.undo()

        if not last_user_text:
            # With the fixed undo(), reaching here means CURRENT_CHAT["messages"]
            # was NOT touched — nothing to restore.
            print("[⚠️ RETRY] undo() found nothing safe to revert; history left untouched.")
            on_error("No user message found to regenerate from.")
            return

        # Step 2: Clean the Vector DB strictly by the newly reverted current_turn.
        # This absolutely prevents stranded memories from injecting as flashbacks.
        try:
            chat_id = self.CURRENT_CHAT.get("chat_id", "default")
            vec_db = self.advanced_memory.get_vector_memory(character, chat_id)
            original_len = len(vec_db.memories)
            vec_db.purge_after_turn(self.current_turn)
            print(f"[🔄 RETRY] Requested purge of memories after turn {self.current_turn} (had {original_len} before purge).")
        except Exception as e:
            print(f"[⚠️ RETRY] Could not trim vector memory: {e}")

        # Step 2b: Same cleanup for the EmotionalBeatBank — an undone turn's beat
        # must not keep resurfacing as a "memory" of something that no longer happened.
        try:
            beat_bank = self._get_beat_bank(character, chat_id)
            with beat_bank._lock:
                original_beat_len = len(beat_bank.beats)
                beat_bank.beats = [b for b in beat_bank.beats if b.get("turn_id", 0) <= self.current_turn]
                if len(beat_bank.beats) < original_beat_len:
                    beat_bank._save()
                    print(f"[🔄 RETRY] Purged {original_beat_len - len(beat_bank.beats)} rejected beats from Emotional Bank.")
        except Exception as e:
            print(f"[⚠️ RETRY] Could not trim emotional beat bank: {e}")

        # Step 3: Generate a fresh reply with the perfectly clean state
        self.generate_reply(
            last_user_text,
            character,
            model_choice,
            on_chunk,
            on_complete,
            on_error
        )
    def generate_continuation(self, character, model_choice, on_chunk, on_complete, on_error):
        """Streams a continuation of the exact last assistant message without creating a new block."""
        try:
            msgs = self.CURRENT_CHAT.get("messages", [])
            if not msgs or msgs[-1]["role"] != "assistant":
                on_error("No assistant message to continue.")
                return

            chat_id = self.CURRENT_CHAT.get("chat_id", "default")
            char_prompt = self.CHARACTERS.get(character, "")
            char_settings = self.CHARACTER_SETTINGS.get(character, {})
            gen_settings = {"temperature": char_settings.get("temperature", 0.8), "top_p": char_settings.get("top_p", 0.9), "top_k": char_settings.get("top_k", 40), "repetition_penalty": char_settings.get("repetition_penalty", 1.1), "max_tokens": char_settings.get("max_tokens", 500)}

            last_user_msg = ""
            for m in reversed(msgs):
                if m["role"] == "user":
                    last_user_msg = m["content"]
                    break

            recalled_memory = self.retrieve_relevant_memory(msgs[:-1], last_user_msg, character, chat_id)

            cid = self.CHARACTER_IDS.get(character, "unknown")
            summary_data = self.advanced_memory.load_summary(cid, chat_id)

            full_prompt_messages = self.build_structured_prompt(
                character, char_prompt, chat_id, recalled_memory, msgs[:-1], last_user_msg,
                story_summary=summary_data
            )
            
            # Inject the incomplete last message and force the model to continue it
            full_prompt_messages.append({"role": "assistant", "content": msgs[-1]["content"]})
            _cont_genre = self.CHARACTER_GENRES.get(character, "romance")
            _cont_cue   = get_genre_config(_cont_genre)["continuation_cue"]
            full_prompt_messages.append({"role": "user", "content": _cont_cue})
            
            response_generator = self.call_selected_model(full_prompt_messages, gen_settings, model_choice)
            if not response_generator:
                on_error("No reply received.")
                return
                
            added_text = ""
            for chunk in response_generator:
                if chunk:
                    added_text += chunk
                    display_text = added_text

                    if not getattr(self, 'show_thoughts', False):
                        display_text = re.sub(r'(?i)\[THOUGHTS?:.*?(\]|$)', '', display_text, flags=re.DOTALL)
                        display_text = re.sub(r'(?i)\*THOUGHTS?:.*?(\*|\n\n|$)', '', display_text, flags=re.DOTALL)
                        display_text = re.sub(r'(?i)(?:^|\n)THOUGHTS?:.*?(\n\n|$)', '', display_text, flags=re.DOTALL)

                    on_chunk(display_text.strip())

            clean_added = self.post_process_response(added_text, character)

            # Append seamlessly to the existing history
            self.CURRENT_CHAT["messages"][-1]["content"] += " " + clean_added
            self.save_current_chat()
            
            on_complete(clean_added)
        except Exception as e:
            on_error(str(e))