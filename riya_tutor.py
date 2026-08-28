"""
Riya — Voice-based English Tutor
---------------------------------
A Flask Blueprint that wires together:
  - Groq Whisper (STT)
  - Groq Qwen (LLM, persona = Riya, English tutor)
  - Groq PlayAI TTS (voice reply)
  - MongoDB Atlas (persistent multi-turn memory) — reuses the same Atlas
    cluster your roleplay-companion app already uses, in its own database
    so collections don't collide. Render's free tier has an EPHEMERAL
    filesystem (wiped on every restart/redeploy), so a local SQLite file
    won't survive there — Mongo Atlas is the right call for this host.

Designed to be `register_blueprint`-ed into your EXISTING Flask app on
Render (the same one serving your companion app) so you don't burn a
second free-tier service slot. If you want it standalone, see
`if __name__ == "__main__"` at the bottom.

ENV VARS REQUIRED (set in Render's dashboard under your service's
Environment tab):
    GROQ_API_KEY   - your Groq API key
    MONGODB_URI    - your MongoDB Atlas connection string (reuse the one
                     your companion app already uses)
    RIYA_DB_NAME   - (optional) defaults to "riya_tutor" — keeps Riya's
                     data in its own database, separate from the
                     companion app's collections on the same cluster
"""

import os
import time
import uuid
import requests
from flask import Blueprint, request, jsonify, send_file, render_template
from pymongo import MongoClient
from io import BytesIO

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

STT_MODEL = "whisper-large-v3"          # Groq Whisper
LLM_MODEL = "qwen/qwen3-32b"            # Groq's Qwen offering
TTS_MODEL = "playai-tts"                # Groq TTS (English)
TTS_VOICE = "Aaliyah-PlayAI"            # pick any English PlayAI voice you like

MONGODB_URI = os.environ["MONGODB_URI"]
DB_NAME = os.environ.get("RIYA_DB_NAME", "riya_tutor")

MAX_HISTORY_TURNS = 12  # how many past turns to feed back into the LLM as context

RIYA_SYSTEM_PROMPT = """You are Riya, a warm, patient, encouraging English tutor who speaks \
with the user out loud. Your goals:

1. Hold a natural spoken conversation in English at a level matched to the student.
2. Gently correct grammar/pronunciation-relevant mistakes AFTER responding to their point \
   (don't interrupt the flow of conversation with corrections first).
3. Keep replies short and conversational (2-4 sentences) since they will be spoken aloud — \
   avoid long lists, bullet points, or markdown.
4. Occasionally ask a follow-up question to keep the student talking (talking practice is \
   the goal).
5. If the student writes/says something in Hindi or Hinglish, respond in simple English and \
   encourage them to try rephrasing in English.
6. Remember prior context from this conversation (provided below) and refer back to it \
   naturally, the way a real tutor tracking a student's progress would.
"""

# ---------------------------------------------------------------------------
# Mongo setup
# ---------------------------------------------------------------------------

_mongo_client = MongoClient(MONGODB_URI)
_db = _mongo_client[DB_NAME]
_conversations = _db["conversations"]   # one doc per user, holds a `messages` array
_conversations.create_index("user_id", unique=True)

def _get_history(user_id: str):
    doc = _conversations.find_one({"user_id": user_id})
    if not doc:
        return []
    return doc.get("messages", [])

def _append_turn(user_id: str, role: str, content: str):
    _conversations.update_one(
        {"user_id": user_id},
        {
            "$push": {
                "messages": {
                    "role": role,
                    "content": content,
                    "ts": time.time(),
                }
            },
            "$setOnInsert": {"user_id": user_id, "created_at": time.time()},
        },
        upsert=True,
    )

def _reset_history(user_id: str):
    _conversations.delete_one({"user_id": user_id})

def _trimmed_history_for_llm(user_id: str):
    """Return last MAX_HISTORY_TURNS messages in {role, content} form for the LLM call."""
    history = _get_history(user_id)
    trimmed = history[-MAX_HISTORY_TURNS:]
    return [{"role": m["role"], "content": m["content"]} for m in trimmed]

# ---------------------------------------------------------------------------
# Groq API helpers
# ---------------------------------------------------------------------------

def groq_transcribe(audio_bytes: bytes, filename: str = "audio.webm") -> str:
    """Send audio to Groq Whisper, return transcribed text."""
    url = f"{GROQ_BASE_URL}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    files = {"file": (filename, audio_bytes)}
    data = {"model": STT_MODEL, "language": "en"}
    resp = requests.post(url, headers=headers, files=files, data=data, timeout=60)
    resp.raise_for_status()
    return resp.json()["text"].strip()


def groq_chat(user_id: str, user_text: str) -> str:
    """Call Groq's Qwen chat model with persisted history, return Riya's reply text."""
    url = f"{GROQ_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    messages = [{"role": "system", "content": RIYA_SYSTEM_PROMPT}]
    messages.extend(_trimmed_history_for_llm(user_id))
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 300,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def groq_speak(text: str) -> bytes:
    """Call Groq PlayAI TTS, return raw audio bytes (wav)."""
    url = f"{GROQ_BASE_URL}/audio/speech"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": TTS_MODEL,
        "input": text,
        "voice": TTS_VOICE,
        "response_format": "wav",
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    return resp.content

# ---------------------------------------------------------------------------
# Blueprint / routes
# ---------------------------------------------------------------------------

riya_bp = Blueprint(
    "riya_tutor",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/riya/static",
)


@riya_bp.route("/riya")
def riya_home():
    return render_template("riya.html")


@riya_bp.route("/riya/api/turn", methods=["POST"])
def riya_turn():
    """
    One full voice turn in a single request:
      1. Receives recorded audio (multipart 'audio' field) + 'user_id' field
      2. Transcribes it (STT)
      3. Sends to Qwen with memory (LLM)
      4. Synthesizes Riya's reply (TTS)
      5. Returns JSON {user_text, riya_text} PLUS a turn_id to fetch the audio
    """
    user_id = request.form.get("user_id") or request.remote_addr or "anonymous"
    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"error": "no audio provided"}), 400

    audio_bytes = audio_file.read()

    try:
        user_text = groq_transcribe(audio_bytes, filename=audio_file.filename or "audio.webm")
    except requests.HTTPError as e:
        return jsonify({"error": f"transcription failed: {e}"}), 502

    if not user_text:
        return jsonify({"error": "could not understand audio, please try again"}), 422

    _append_turn(user_id, "user", user_text)

    try:
        riya_text = groq_chat(user_id, user_text)
    except requests.HTTPError as e:
        return jsonify({"error": f"chat failed: {e}"}), 502

    _append_turn(user_id, "assistant", riya_text)

    # Cache the audio for this turn under a short-lived id so the client can fetch it
    turn_id = str(uuid.uuid4())
    try:
        audio_reply = groq_speak(riya_text)
    except requests.HTTPError:
        audio_reply = b""  # fall back to text-only if TTS hiccups
    _TURN_AUDIO_CACHE[turn_id] = audio_reply

    return jsonify({
        "user_text": user_text,
        "riya_text": riya_text,
        "turn_id": turn_id,
    })


# simple in-memory cache: turn_id -> wav bytes (short-lived, fine for a single worker)
_TURN_AUDIO_CACHE = {}


@riya_bp.route("/riya/api/turn-audio/<turn_id>")
def riya_turn_audio(turn_id):
    audio_bytes = _TURN_AUDIO_CACHE.pop(turn_id, None)
    if not audio_bytes:
        return jsonify({"error": "audio not found or already fetched"}), 404
    return send_file(BytesIO(audio_bytes), mimetype="audio/wav")


@riya_bp.route("/riya/api/reset", methods=["POST"])
def riya_reset():
    """Wipe a user's conversation memory (fresh start)."""
    user_id = request.json.get("user_id") if request.is_json else None
    user_id = user_id or request.remote_addr or "anonymous"
    _reset_history(user_id)
    return jsonify({"status": "reset"})


# ---------------------------------------------------------------------------
# Standalone runner (only used if you run this file directly instead of
# merging riya_bp into your existing Flask app)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from flask import Flask
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.register_blueprint(riya_bp)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
