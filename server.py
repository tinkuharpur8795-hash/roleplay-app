from flask import Flask, request, jsonify, send_from_directory, send_file, Response
from flask_cors import CORS
from app_backend import RoleplayBackend
import os
import sys
import time
import json
import queue
import threading
import traceback
import uuid
from functools import wraps

FRONTEND_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path='')
CORS(app)

# Any route error becomes JSON, not Flask's default HTML error page —
# otherwise fetch(...).json() on the frontend throws a confusing
# "Unexpected token '<'" instead of showing the real problem.
@app.errorhandler(404)
def handle_404(e):
    return jsonify({"error": "Not found", "path": request.path}), 404

@app.errorhandler(Exception)
def handle_exception(e):
    # Catches ALL unhandled crashes and returns a tiny JSON instead of a giant HTML page
    print(f"[⚠️ SERVER ERROR] Unhandled error on {request.path}:\n{traceback.format_exc()}")
    return jsonify({"error": "Internal server error", "path": request.path}), 500

# ══════════════════════════════════════════════════════════════════
# LIVE LOG INTERCEPTOR
# Redirects every print() from backend into a thread-safe queue.
# The /logs SSE endpoint drains this queue to the browser in
# real time — no polling, no missed lines.
# ══════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════
# LIVE LOG — POLLING BUFFER
# A permanently-open SSE connection is fine on a multi-worker/local
# setup, but on a single-worker host (e.g. PythonAnywhere free tier)
# it would occupy the ONE worker forever, blocking every other
# request on the site. So instead: every print() gets appended to a
# small rolling buffer with an incrementing id, and the browser polls
# a lightweight endpoint every few seconds for anything new. Each
# poll request returns immediately — no connection is ever held open.
# ══════════════════════════════════════════════════════════════════

from collections import deque

_log_buffer   = deque(maxlen=300)  # list of (id, text) tuples
_log_next_id  = 0
_log_lock     = threading.Lock()

class _LogInterceptor:
    """Wraps sys.stdout so every print() also lands in the rolling buffer."""
    def __init__(self, original):
        self._original = original

    def write(self, text):
        self._original.write(text)          # still prints to the console/logs
        if text.strip():
            global _log_next_id
            with _log_lock:
                _log_next_id += 1
                _log_buffer.append((_log_next_id, text.rstrip()))

    def flush(self):
        self._original.flush()

sys.stdout = _LogInterceptor(sys.stdout)

# ══════════════════════════════════════════════════════════════════
# IDLE TIMER
# Shuts down the main server after IDLE_MINUTES of no HTTP activity.
# The launcher.pyw process stays alive so the next bookmark click
# restarts everything automatically.
# ══════════════════════════════════════════════════════════════════

IDLE_MINUTES   = 300
_last_activity = time.time()
_idle_lock     = threading.Lock()

# Only self-exit when explicitly running via the local bookmark-launcher
# (launcher.pyw stays alive and restarts the app automatically on the next
# click). On PythonAnywhere or any other host, nothing is watching to
# restart a killed process — os._exit(0) there just means "dead until
# someone opens the PythonAnywhere dashboard and reloads it manually."
_ENABLE_IDLE_SELF_EXIT = os.environ.get("LOCAL_BOOKMARK_LAUNCHER", "").strip() == "1"

def _touch_activity():
    """Call on every incoming request to reset the idle clock."""
    global _last_activity
    with _idle_lock:
        _last_activity = time.time()

def _idle_watchdog():
    """Background thread — checks every 60 s, kills server if idle too long (local-launcher mode only)."""
    global _last_activity
    while True:
        time.sleep(60)
        with _idle_lock:
            idle_seconds = time.time() - _last_activity
        if idle_seconds >= IDLE_MINUTES * 60:
            if _ENABLE_IDLE_SELF_EXIT:
                print(f"\n[⏱️ IDLE] No activity for {IDLE_MINUTES} minutes — shutting down server.")
                print("[⏱️ IDLE] Click the bookmark to restart automatically.\n")
                os._exit(0)   # hard exit — Flask has no clean shutdown API
            else:
                # Log once, then reset the clock so this doesn't spam every
                # 60s afterward. Intentionally does NOT exit — see note above.
                print(f"[⏱️ IDLE] {IDLE_MINUTES} min idle. Self-exit is disabled "
                      "(set LOCAL_BOOKMARK_LAUNCHER=1 if this is the local bookmark setup).")
                with _idle_lock:
                    _last_activity = time.time()
        # Silent — no countdown spam in the activity log

threading.Thread(target=_idle_watchdog, daemon=True).start()

# ══════════════════════════════════════════════════════════════════
# API TOKEN AUTH — Phase 5. Simple static per-device token, appropriate
# for a single-user app. Set it via the PythonAnywhere "Web" tab's
# environment variable config (or your WSGI file):
#     COMPANION_API_TOKEN = <a long random string you generate>
# Android sends it as:  Authorization: Bearer <token>
#
# SCOPE — deliberately NOT applied to every route: the existing browser
# frontend's JS isn't among the files I have, so I can't add the token
# header to its fetch() calls without risking silently breaking it. This
# protects the NEW Android-facing endpoints only (/sync, /reminders/due,
# /chat_poll_start, /chat_poll_status). When you're ready to lock down
# the whole API, add this same header to the existing frontend's fetch
# calls (or share app.js and I'll wire it in properly) and apply
# @require_api_token to the remaining routes.
# ══════════════════════════════════════════════════════════════════

API_TOKEN = os.environ.get("COMPANION_API_TOKEN", "").strip()
if not API_TOKEN:
    print("[⚠️ AUTH] COMPANION_API_TOKEN is not set. Android-facing endpoints "
          "(/sync, /reminders/due, /chat_poll_*) will reject every request "
          "until it's set — intentional, so nothing is silently unprotected.")

def require_api_token(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
        if not API_TOKEN or token != API_TOKEN:
            return jsonify({"status": "error", "message": "Unauthorized"}), 401
        return view_func(*args, **kwargs)
    return wrapped

# ── Activity hook: update timestamp on every request ─────────────
import time as _time_mod

@app.before_request
def before_every_request():
    _touch_activity()

# ── Boot backend AFTER interceptor is installed so boot logs appear ──
backend = RoleplayBackend()

# ══════════════════════════════════════════════════════════════════
# STATIC SERVING
# ══════════════════════════════════════════════════════════════════

@app.route('/')
def serve_index():
    return send_from_directory(FRONTEND_DIR, 'index.html')

@app.route('/<path:filename>')
def serve_static(filename):
    return send_from_directory(FRONTEND_DIR, filename)

@app.route('/serve_image')
def serve_image():
    path = request.args.get('path', '')
    if not path or not os.path.exists(path):
        return jsonify({"error": "Image not found"}), 404
    return send_file(path, mimetype='image/png')

# ══════════════════════════════════════════════════════════════════
# LIVE LOG — SSE STREAM
# Browser connects once via EventSource('/logs').
# Each backend print() is pushed as a 'data: ...\n\n' event.
# ══════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════
# LIVE LOG — POLL ENDPOINT
# Browser calls this every few seconds with the last id it has seen.
# Returns immediately with any newer lines — never holds the
# connection open, so it's safe on a single-worker host.
# ══════════════════════════════════════════════════════════════════

@app.route('/logs_poll')
def poll_logs():
    since = request.args.get('since', 0, type=int)
    with _log_lock:
        new_lines = [{"id": i, "text": t} for i, t in _log_buffer if i > since]
        latest_id = _log_buffer[-1][0] if _log_buffer else since
    return jsonify({"lines": new_lines, "latest_id": latest_id})


# ══════════════════════════════════════════════════════════════════
# SHOW THOUGHTS TOGGLE
# Mirrors the CTkSwitch in Tkinter settings that sets
# backend.show_thoughts = True/False
# ══════════════════════════════════════════════════════════════════

@app.route('/toggle_thoughts', methods=['POST'])
def toggle_thoughts():
    data   = request.get_json()
    enable = data.get('enable', False)
    backend.show_thoughts = bool(enable)
    print(f"[⚙️ THOUGHTS] Show thoughts set to: {backend.show_thoughts}")
    return jsonify({"show_thoughts": backend.show_thoughts})

@app.route('/get_thoughts_state', methods=['GET'])
def get_thoughts_state():
    return jsonify({"show_thoughts": getattr(backend, 'show_thoughts', False)})

# ══════════════════════════════════════════════════════════════════
# DROPDOWNS
# ══════════════════════════════════════════════════════════════════

@app.route("/characters", methods=["GET"])
def get_characters():
    return jsonify(list(backend.CHARACTERS.keys()))

@app.route("/models", methods=["GET"])
def get_models():
    return jsonify(list(backend.MODEL_OPTIONS.keys()))

# ══════════════════════════════════════════════════════════════════
# CHARACTER EDITING
# Powers the "edit" pencil button on the home page character grid:
#   GET  /characters_full     -> [{name, id, description}, ...] so the
#                                 grid can build avatar image URLs and
#                                 show each card's description text
#   GET  /character/<name>    -> raw character JSON, for the editor
#   GET  /character_image/<id>-> the character's avatar file, if any
#   POST /edit_character      -> saves edited JSON + optional avatar
# ══════════════════════════════════════════════════════════════════

def _safe_char_id(char_id):
    """Rejects anything that isn't a plain filename segment (no path traversal)."""
    if not char_id or "/" in char_id or "\\" in char_id or ".." in char_id:
        return None
    return char_id

_AVATAR_EXTENSIONS = ("png", "jpg", "jpeg", "webp", "gif")

def _build_name_to_file_id_map():
    """
    Scans CHARACTERS_DIR and maps character display name -> actual on-disk
    filename (without .json). We deliberately don't use backend.CHARACTER_IDS
    for this — that dict stores each card's internal "id" field, which can
    drift from the real filename on hand-edited or imported cards (e.g. the
    JSON says "id": "aeryel_001" but the file is actually "aeryel.json").
    Only the real filename is safe to use for save/image lookups.
    """
    mapping = {}
    for filename in os.listdir(backend.CHARACTERS_DIR):
        if not filename.endswith(".json"):
            continue
        file_path = os.path.join(backend.CHARACTERS_DIR, filename)
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        char_data = data.get("data", data)
        name = data.get("name", char_data.get("name", ""))
        if name:
            mapping[name] = filename[:-5]  # strip ".json"
    return mapping

@app.route("/characters_full", methods=["GET"])
def get_characters_full():
    name_to_id = _build_name_to_file_id_map()
    result = []
    for name in backend.CHARACTERS.keys():
        char_id = name_to_id.get(name, backend.CHARACTER_IDS.get(name, name))
        description = ""
        safe_id = _safe_char_id(char_id)
        if safe_id:
            char_file = os.path.join(backend.CHARACTERS_DIR, f"{safe_id}.json")
            try:
                with open(char_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # Some cards nest fields under a "data" key — same unwrap
                # the chat page's character panel uses.
                info = data.get("data", data)
                description = info.get("description") or info.get("backstory") or info.get("appearance") or ""
            except Exception:
                pass
        result.append({"name": name, "id": char_id, "description": description})
    return jsonify(result)

@app.route("/character/<path:name>", methods=["GET"])
def get_character(name):
    try:
        name_to_id = _build_name_to_file_id_map()
        char_id = _safe_char_id(name_to_id.get(name, backend.CHARACTER_IDS.get(name, name)))
        if not char_id or not os.path.exists(os.path.join(backend.CHARACTERS_DIR, f"{char_id}.json")):
            print(f"[✏️ EDIT] '{name}' has no matching file on disk. Known names: {list(name_to_id.keys())}")
            return jsonify({"error": f"Character '{name}' not found."}), 404

        char_file = os.path.join(backend.CHARACTERS_DIR, f"{char_id}.json")
        if not os.path.exists(char_file):
            print(f"[✏️ EDIT] Expected file does not exist: {char_file}")
            return jsonify({"error": f"Character file for '{name}' not found on disk ({char_id}.json)."}), 404

        with open(char_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        return jsonify({"id": char_id, "data": data})
    except Exception as e:
        print(f"[⚠️ EDIT] get_character('{name}') failed:\n{traceback.format_exc()}")
        return jsonify({"error": f"Server error while loading character: {e}"}), 500

@app.route("/character_image/<path:char_id>", methods=["GET"])
def get_character_image(char_id):
    try:
        safe_id = _safe_char_id(char_id)
        if not safe_id:
            return jsonify({"error": "Invalid character id."}), 400

        # Optional ?file=<gallery_filename> serves a specific gallery image
        # instead of the main avatar. Only filenames this character's own
        # gallery list actually owns are allowed — no arbitrary file access.
        requested_file = request.args.get("file")
        if requested_file:
            safe_name = os.path.basename(requested_file)
            gallery = backend.get_character_gallery(safe_id)
            if safe_name not in gallery:
                return jsonify({"error": "Image not found in this character's gallery."}), 404
            path = os.path.join(backend.IMAGES_DIR, safe_name)
            if os.path.exists(path):
                return send_file(path)
            return jsonify({"error": "Image file missing on disk."}), 404

        for ext in _AVATAR_EXTENSIONS:
            path = os.path.join(backend.IMAGES_DIR, f"{safe_id}.{ext}")
            if os.path.exists(path):
                return send_file(path)

        return jsonify({"error": "No image set for this character."}), 404
    except Exception as e:
        print(f"[⚠️ EDIT] get_character_image('{char_id}') failed:\n{traceback.format_exc()}")
        return jsonify({"error": f"Server error while loading image: {e}"}), 500

@app.route("/character_gallery/<path:char_id>", methods=["GET"])
def get_character_gallery(char_id):
    """
    Returns the character's full image set, main avatar first, as ready-to-use
    URLs: [{"url": "...", "filename": null|"..."}, ...]. The frontend gallery
    just renders this list in order — it never has to know about the
    avatar/gallery storage split on disk.
    """
    try:
        safe_id = _safe_char_id(char_id)
        if not safe_id:
            return jsonify({"error": "Invalid character id."}), 400

        images = []
        for ext in _AVATAR_EXTENSIONS:
            if os.path.exists(os.path.join(backend.IMAGES_DIR, f"{safe_id}.{ext}")):
                images.append({"url": f"/character_image/{safe_id}", "filename": None})
                break

        for filename in backend.get_character_gallery(safe_id):
            images.append({
                "url": f"/character_image/{safe_id}?file={filename}",
                "filename": filename
            })

        return jsonify({"id": safe_id, "images": images})
    except Exception as e:
        print(f"[⚠️ EDIT] get_character_gallery('{char_id}') failed:\n{traceback.format_exc()}")
        return jsonify({"error": f"Server error while loading gallery: {e}"}), 500

@app.route("/add_character_image", methods=["POST"])
def add_character_image():
    try:
        char_id = _safe_char_id(request.form.get("char_id", ""))
        if not char_id:
            return jsonify({"status": "error", "message": "Missing or invalid char_id."}), 400

        uploaded = request.files.get("image")
        if not uploaded or not uploaded.filename:
            return jsonify({"status": "error", "message": "No image file provided."}), 400

        ext = uploaded.filename.rsplit(".", 1)[-1].lower() if "." in uploaded.filename else "png"
        if ext not in _AVATAR_EXTENSIONS:
            ext = "png"
        image_bytes = uploaded.read()

        result = backend.add_character_gallery_image(char_id, image_bytes, image_ext=ext)
        print(f"[✏️ EDIT] add_character_gallery_image('{char_id}') -> {result.get('status')}")
        status_code = 200 if result.get("status") == "success" else 400
        return jsonify(result), status_code
    except Exception as e:
        print(f"[⚠️ EDIT] add_character_image() failed:\n{traceback.format_exc()}")
        return jsonify({"status": "error", "message": f"Server error while adding image: {e}"}), 500

@app.route("/delete_character_image", methods=["POST"])
def delete_character_image():
    try:
        payload = request.get_json(silent=True) or request.form
        char_id = _safe_char_id(payload.get("char_id", ""))
        filename = payload.get("filename", "")

        if not char_id:
            return jsonify({"status": "error", "message": "Missing or invalid char_id."}), 400
        if not filename:
            return jsonify({"status": "error", "message": "Missing filename."}), 400

        result = backend.delete_character_gallery_image(char_id, filename)
        print(f"[✏️ EDIT] delete_character_gallery_image('{char_id}', '{filename}') -> {result.get('status')}")
        status_code = 200 if result.get("status") == "success" else 400
        return jsonify(result), status_code
    except Exception as e:
        print(f"[⚠️ EDIT] delete_character_image() failed:\n{traceback.format_exc()}")
        return jsonify({"status": "error", "message": f"Server error while deleting image: {e}"}), 500

@app.route("/edit_character", methods=["POST"])
def edit_character():
    try:
        char_id = _safe_char_id(request.form.get("char_id", ""))
        raw_json = request.form.get("data", "")

        if not char_id:
            return jsonify({"status": "error", "message": "Missing or invalid char_id."}), 400
        if not raw_json:
            return jsonify({"status": "error", "message": "Missing character data."}), 400

        try:
            updated_data = json.loads(raw_json)
        except Exception as e:
            return jsonify({"status": "error", "message": f"Invalid JSON: {e}"}), 400

        if not isinstance(updated_data, dict):
            return jsonify({"status": "error", "message": "Character data must be a JSON object."}), 400

        image_bytes = None
        image_ext = "png"
        uploaded = request.files.get("image")
        if uploaded and uploaded.filename:
            ext = uploaded.filename.rsplit(".", 1)[-1].lower() if "." in uploaded.filename else "png"
            image_ext = ext if ext in _AVATAR_EXTENSIONS else "png"
            image_bytes = uploaded.read()
            print(f"[✏️ EDIT] Received image for '{char_id}': {uploaded.filename} ({len(image_bytes)} bytes) -> {char_id}.{image_ext}")

            # Remove any stale avatar saved under a different extension so
            # /character_image doesn't keep serving the old picture.
            for other_ext in _AVATAR_EXTENSIONS:
                if other_ext != image_ext:
                    stale_path = os.path.join(backend.IMAGES_DIR, f"{char_id}.{other_ext}")
                    if os.path.exists(stale_path):
                        os.remove(stale_path)

        result = backend.edit_character_profile(char_id, updated_data, image_bytes=image_bytes, image_ext=image_ext)
        print(f"[✏️ EDIT] edit_character_profile('{char_id}') -> {result}")
        status_code = 200 if result.get("status") == "success" else 400
        return jsonify(result), status_code
    except Exception as e:
        print(f"[⚠️ EDIT] edit_character() failed:\n{traceback.format_exc()}")
        return jsonify({"status": "error", "message": f"Server error while saving: {e}"}), 500

# ══════════════════════════════════════════════════════════════════
# INBUILT CHARACTER PROMPT WRITER
# Takes a one-line vibe ("a grumpy bodyguard"), runs it through
# DeepSeek/Mistral in JSON mode, saves the resulting card to
# CHARACTERS_DIR, reloads the character list, and starts a fresh
# chat with the new character active — all in one round trip.
# ══════════════════════════════════════════════════════════════════

@app.route("/create_character", methods=["POST"])
def create_character():
    data = request.get_json(silent=True) or {}
    vibe = (data.get("vibe") or "").strip()
    if not vibe:
        return jsonify({"status": "error", "error": "Please describe the character you want to create."}), 400
    try:
        result = backend.create_character_from_vibe(vibe)
        return jsonify(result)
    except (ValueError, RuntimeError) as e:
        print(f"[⚠️ CHAR WRITER] {e}")
        return jsonify({"status": "error", "error": str(e)}), 400
    except Exception as e:
        print(f"[⚠️ CHAR WRITER] Unexpected error: {e}")
        return jsonify({"status": "error", "error": "Unexpected server error while generating the character."}), 500

# ══════════════════════════════════════════════════════════════════
# CHAT MANAGEMENT
# ══════════════════════════════════════════════════════════════════

@app.route("/new_chat", methods=["POST"])
def new_chat():
    data      = request.get_json()
    character = data.get("character")
    if not character:
        return jsonify({"status": "error", "first_message": ""}), 400
    chat_data = backend.create_new_chat(character)
    first_msg = backend.CHARACTER_FIRST_MESSAGES.get(character, "")
    return jsonify({
        "status": "ok",
        "first_message": first_msg,
        "chat_id": chat_data.get("chat_id"),
        "path": chat_data.get("path"),
    })

@app.route("/chat_list", methods=["POST"])
def chat_list():
    data      = request.get_json()
    character = data.get("character")
    chats     = backend.get_chats_for_character(character) if character else []
    for c in chats:
        c["char_id"] = backend.CHARACTER_IDS.get(c["character"], c["character"])
    return jsonify(chats)

@app.route("/chat_history_all", methods=["GET"])
def chat_history_all():
    """Powers the universal (all-characters, most-recent-first) sidebar list."""
    try:
        chats = backend.get_all_chats()
        name_to_id = _build_name_to_file_id_map()
        for c in chats:
            c["char_id"] = name_to_id.get(c["character"], backend.CHARACTER_IDS.get(c["character"], c["character"]))
        return jsonify(chats)
    except Exception as e:
        print(f"[⚠️ CHAT HISTORY] chat_history_all() failed:\n{traceback.format_exc()}")
        return jsonify({"error": f"Server error while loading chat history: {e}"}), 500

@app.route("/load_chat", methods=["POST"])
def load_chat():
    data = request.get_json()
    path = data.get("path")
    chat = backend.load_chat(path)
    if chat:
        return jsonify({
            "status":    "ok",
            "messages":  chat.get("messages", []),
            "chat_id":   chat.get("chat_id"),
            "character": chat.get("character")
        })
    return jsonify({"status": "error"}), 400

def _is_safe_chat_path(path):
    """Confirms a client-supplied chat path matches the new MongoDB pseudo-path format (character::id)."""
    try:
        if not path or "::" not in path:
            return False
        character, chat_id = path.split("::")
        int(chat_id) # Ensure the chat_id segment is actually a valid number
        return True
    except Exception:
        return False
    
@app.route("/edit_message", methods=["POST"])
def edit_message():
    """Powers the hover-to-edit pencil on any message bubble (past or present)."""
    try:
        data    = request.get_json(force=True) or {}
        path    = data.get("path")
        index   = data.get("index")
        role    = data.get("role")
        content = data.get("content")

        # Removed os.path.exists; solely rely on our new pseudo-path string validator
        if not path or not _is_safe_chat_path(path):
            return jsonify({"status": "error", "message": "Invalid chat path format."}), 400
        if not isinstance(index, int):
            return jsonify({"status": "error", "message": "Missing or invalid message index."}), 400
        if content is None:
            return jsonify({"status": "error", "message": "Missing message content."}), 400

        result = backend.edit_message_in_chat(path, index, content, expected_role=role)
        status_code = 200 if result.get("status") == "success" else 400
        return jsonify(result), status_code
    except Exception as e:
        print(f"[⚠️ EDIT MSG] edit_message() failed:\n{traceback.format_exc()}")
        return jsonify({"status": "error", "message": f"Server error while saving message: {e}"}), 500
# ── DELETE — full deep scrub matching Tkinter action_delete_chat ──
@app.route("/delete_chat", methods=["POST"])
def delete_chat():
    data    = request.get_json()
    path    = data.get("path")
    char    = data.get("character", "")
    chat_id = str(data.get("chat_id", ""))

    backend.delete_chat(path)

    if char and chat_id:
        result = backend.delete_chat_memory(char, chat_id)
        print(f"[🗑️ DELETE] {char} chat {chat_id} — deleted: {len(result['deleted'])}, errors: {len(result['errors'])}")
        return jsonify({"status": "ok", "deleted": result["deleted"], "errors": result["errors"]})

    return jsonify({"status": "ok"})

# ══════════════════════════════════════════════════════════════════
# SYNC — Phase 3. Lets a client (web or Android) poll "what changed
# since I last checked" for one chat, instead of re-fetching everything
# every time. Intentionally simple: one chat at a time, timestamp-based —
# not a full bidirectional multi-device sync protocol (that's a distinct,
# larger project, deliberately deferred).
# ══════════════════════════════════════════════════════════════════
@app.route("/sync", methods=["GET"])
@require_api_token
def sync():
    char    = request.args.get("character", "")
    chat_id = request.args.get("chat_id", "")
    since   = request.args.get("since", "0")

    if not char or not chat_id:
        return jsonify({"status": "error", "message": "character and chat_id are required."}), 400

    try:
        since_ts = float(since)
    except ValueError:
        return jsonify({"status": "error", "message": "since must be a unix timestamp."}), 400

    snapshot = backend.get_sync_snapshot(char, chat_id, since=since_ts)
    return jsonify({"status": "ok", **snapshot})

# ══════════════════════════════════════════════════════════════════
# REMINDERS — Phase 4 groundwork for proactive/notification work.
# This does NOT send anything anywhere — it just answers "which pending
# reminders are old enough in real time to be worth surfacing" for one
# chat. A future notification job can poll this and decide what to do.
# ══════════════════════════════════════════════════════════════════
@app.route("/reminders/due", methods=["GET"])
@require_api_token
def reminders_due():
    char    = request.args.get("character", "")
    chat_id = request.args.get("chat_id", "")

    if not char or not chat_id:
        return jsonify({"status": "error", "message": "character and chat_id are required."}), 400

    cid = backend.CHARACTER_IDS.get(char, char)
    due = backend.expectations.get_due_reminders(cid, chat_id)
    return jsonify({"status": "ok", "due": due})

# ══════════════════════════════════════════════════════════════════
# CRON — Phase 6. External, cron-triggerable summarization sweep.
#
# The existing in-request background thread (generate_reply's "Trigger
# Story Summary" block) still handles the common case and is UNCHANGED —
# this is a safety net for it, not a replacement. A daemon thread has no
# guarantee of finishing if the process is killed/recycled mid-run, which
# is a real risk on PythonAnywhere free tier. Point a PythonAnywhere
# Scheduled Task (or any external cron) at this, e.g. every 15-30 minutes:
#
#     curl -H "Authorization: Bearer <COMPANION_API_TOKEN>" \
#          "https://<your-app>.pythonanywhere.com/cron/summarize"
# ══════════════════════════════════════════════════════════════════
@app.route("/cron/summarize", methods=["GET", "POST"])
@require_api_token
def cron_summarize():
    try:
        max_jobs = int(request.args.get("max_jobs", 5))
    except ValueError:
        max_jobs = 5

    try:
        result = backend.run_due_summaries(max_jobs=max_jobs)
        print(f"[⏱️ CRON] Summary sweep — checked: {result['checked']}, "
              f"summarized: {len(result['summarized'])}, errors: {len(result['errors'])}")
        
        safe_errors = [str(err)[:80] + "..." for err in result.get('errors', [])]
        result['errors'] = safe_errors
        return jsonify({"status": "ok", **result}), 200
    except Exception as e:
        print(f"[⚠️ CRON SUMMARIZE] failed:\n{traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)[:150]}), 200
# ══════════════════════════════════════════════════════════════════
# TELEGRAM MORNING MESSAGE — externally cron-triggered proactive ping.
# Point cron-job.org (or any external scheduler) at this daily:
#
#     curl -H "Authorization: Bearer <COMPANION_API_TOKEN>" \
#          "https://<your-app>.pythonanywhere.com/cron/morning_message?character=<name>&chat_id=<id>"
# ══════════════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

def send_telegram_message(text):
    import requests
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[⚠️ TELEGRAM] Bot token or chat ID not set — skipping send.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        return resp.ok
    except requests.exceptions.ProxyError:
        print(f"[⚠️ TELEGRAM PROXY] PythonAnywhere proxy rejected the connection (503). Could not send: {text}")
        return False
    except Exception as network_e:
        print(f"[⚠️ TELEGRAM ERROR] Failed to send message: {network_e}")
        return False

@app.route("/cron/morning_message", methods=["GET", "POST"])
@require_api_token
def cron_morning_message():
    character = request.args.get("character", "")
    chat_id   = request.args.get("chat_id", "")
    model     = request.args.get("model", list(backend.MODEL_OPTIONS.keys())[0])

    if not character or not chat_id:
        return jsonify({"status": "error", "message": "character and chat_id are required."}), 400

    try:
        message_text = backend.generate_morning_message(character, chat_id, model)
        sent = send_telegram_message(message_text)
        print(f"[☀️ MORNING] Generated for {character}/{chat_id} — Telegram sent: {sent}")
        return jsonify({"status": "ok", "sent": sent, "text": message_text})
    except Exception as e:
        print(f"[⚠️ MORNING] cron_morning_message() failed:\n{traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)}), 500
# ══════════════════════════════════════════════════════════════════
# PROACTIVE MESSAGE CHECK — random-time morning / night / missing-you
# pings. Supersedes the fixed-time /cron/morning_message above: instead
# of one daily cron per message at a fixed clock time, point an external
# scheduler (cron-job.org) at THIS route every ~10-15 minutes. Each hit
# is cheap (one small JSON read) unless a message is actually due — the
# random target time for each of the three kinds is rolled once per
# calendar day (IST) and persisted, so it survives across cron hits and
# each kind only fires once per day. Old route can be removed from your
# cron-job.org dashboard once this one is wired up.
#
#     curl -H "Authorization: Bearer <COMPANION_API_TOKEN>" \
#          "https://<your-app>.pythonanywhere.com/cron/proactive_check?character=<name>&chat_id=<id>"
# ══════════════════════════════════════════════════════════════════

@app.route("/cron/proactive_check", methods=["GET", "POST"])
@require_api_token
def cron_proactive_check():
    character = request.args.get("character", "")
    chat_id   = request.args.get("chat_id", "")
    model     = request.args.get("model", list(backend.MODEL_OPTIONS.keys())[0])

    if not character or not chat_id:
        return jsonify({"status": "error", "message": "character and chat_id are required."}), 200

    try:
        due = backend.check_and_send_due_proactive_messages(character, chat_id, model)
        results = []
        for kind, text in due:
            sent = send_telegram_message(text)
            print(f"[🔔 PROACTIVE] {kind} for {character}/{chat_id} — Telegram sent: {sent}")
            safe_text = text[:80] + "..." if len(text) > 80 else text
            results.append({"kind": kind, "sent": sent, "text": safe_text})
        return jsonify({"status": "ok", "fired": results}), 200
        
    except ValueError as ve:
        print(f"[⚠️ PROACTIVE] Skipped: {ve}")
        return jsonify({"status": "skipped", "message": str(ve)[:120]}), 200
        
    except Exception as e:
        print(f"[⚠️ PROACTIVE] cron_proactive_check() failed:\n{traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)[:150]}), 200
# ══════════════════════════════════════════════════════════════════
# TELEGRAM WEBHOOK — two-way chat via Telegram, bypassing the web app.
# Register the webhook ONCE (not called by your app — just a one-off
# setup step you run yourself):
#
#     https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<your-app>.pythonanywhere.com/telegram/webhook
#
# TELEGRAM_CHARACTER / TELEGRAM_CHAT_ID_APP are the fixed character +
# chat_id this bot always replies as/into — set once as env vars,
# matching the "one character only" decision.
# ══════════════════════════════════════════════════════════════════
TELEGRAM_CHARACTER    = os.environ.get("TELEGRAM_CHARACTER", "").strip()
TELEGRAM_CHAT_ID_APP  = os.environ.get("TELEGRAM_CHAT_ID_APP", "").strip()
TELEGRAM_MODEL        = os.environ.get("TELEGRAM_PRIMARY_MODEL", os.environ.get("TELEGRAM_MODEL", "Cloud Mistral Nemo  (Chat)")).strip()
@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    try:
        update = request.get_json(force=True) or {}
        message = update.get("message", {})
        sender_chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "")

        # Security: only ever respond to YOUR chat id, so nobody else who
        # discovers the bot's username can talk to it.
        if not sender_chat_id or sender_chat_id != TELEGRAM_CHAT_ID:
            print(f"[⚠️ TELEGRAM] Ignored message from unrecognized chat id: {sender_chat_id}")
            return jsonify({"status": "ignored"})

        if not text:
            return jsonify({"status": "ignored"})

        reply_text = backend.generate_reply_sync(
            user_text=text,
            character=TELEGRAM_CHARACTER,
            chat_id=TELEGRAM_CHAT_ID_APP,
            model_choice=TELEGRAM_MODEL,
        )

        send_telegram_message(reply_text)
        print(f"[💬 TELEGRAM] Replied as {TELEGRAM_CHARACTER}")
        return jsonify({"status": "ok"})

    except Exception as e:
        print(f"[⚠️ TELEGRAM] telegram_webhook() failed:\n{traceback.format_exc()}")
        send_telegram_message(f"⚠️ Reply failed, nothing was saved — try again: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ══════════════════════════════════════════════════════════════════
# WHATSAPP WEBHOOK — Twilio Sandbox (Easier alternative to Meta)
# ══════════════════════════════════════════════════════════════════
@app.route("/whatsapp/twilio", methods=["POST"])
def twilio_whatsapp():
    try:
        # Twilio sends incoming message data as form-urlencoded values
        sender_phone = request.values.get('From', '')
        text = request.values.get('Body', '')

        # Security: Ignore empty messages
        if not text:
            return "<Response></Response>", 200, {'Content-Type': 'text/xml'}

        # Route the message through your existing LLM pipeline
        reply_text = backend.generate_reply_sync(
            user_text=text,
            character=TELEGRAM_CHARACTER,  # Reusing your active Telegram character
            chat_id=sender_phone,          # Using the phone number to isolate this chat's memory
            model_choice=TELEGRAM_MODEL,   # Reusing the dynamic Telegram model config
        )

        print(f"[💬 WHATSAPP] Replied to {sender_phone} as {TELEGRAM_CHARACTER}")

        # Twilio expects an XML response to send the message back to your phone
        xml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Message>{reply_text}</Message>
        </Response>"""
        
        return xml_response, 200, {'Content-Type': 'text/xml'}

    except Exception as e:
        print(f"[⚠️ WHATSAPP] Twilio webhook failed:\n{traceback.format_exc()}")
        return "<Response></Response>", 500, {'Content-Type': 'text/xml'}
# ══════════════════════════════════════════════════════════════════
# CORE CHAT — STREAMING SSE
# Mirrors Tkinter's on_chunk() live token updates.
# generate_reply() runs in a background thread, pushes each token
# into a queue. The Flask generator drains that queue and yields
# SSE events. The browser reads them with fetch + ReadableStream
# and updates a live bubble word by word.
#
# SSE event format:
#   data: {"type":"chunk","text":"..."}   — token arrived
#   data: {"type":"done","text":"..."}    — full clean reply
#   data: {"type":"error","text":"..."}   — backend error
# ══════════════════════════════════════════════════════════════════
@app.route("/chat", methods=["POST"])

def chat():
    data      = request.get_json()
    user_text = data.get("message", "")
    character = data.get("character", "")
    model     = data.get("model", list(backend.MODEL_OPTIONS.keys())[0])

    # 1. ADD BACKPRESSURE: Max 15 chunks in the queue.
    # The fast LLM thread will block if the UI falls behind.
    chunk_queue = queue.Queue(maxsize=15)

    # 2. DISCONNECT SIGNAL
    client_disconnected = threading.Event()

    def on_chunk(text):
        if client_disconnected.is_set():
            # If user clicked stop, kill the backend generation thread
            raise InterruptedError("Client disconnected")
        try:
            # Block for up to 5 seconds waiting for UI to catch up
            chunk_queue.put(("chunk", text), timeout=5.0)
        except queue.Full:
            client_disconnected.set()
            raise InterruptedError("Client stream blocked")

    def on_complete(final):
        if client_disconnected.is_set(): return
        reply = (final or "").strip()
        if not reply:
            msgs = backend.CURRENT_CHAT.get("messages", [])
            for msg in reversed(msgs):
                if msg.get("role") == "assistant" and msg.get("content", "").strip():
                    reply = msg["content"].strip()
                    break
        if not reply:
            reply = "..."
        chunk_queue.put(("done", reply))

    def on_error(msg):
        if not client_disconnected.is_set():
            chunk_queue.put(("error", msg))

    threading.Thread(
        target=backend.generate_reply,
        args=(user_text, character, model, on_chunk, on_complete, on_error),
        daemon=True
    ).start()
    def generate():
        try:
            while True:
                try:
                    event_type, text = chunk_queue.get(timeout=120)
                except queue.Empty:
                    # ⚠️ MUST BE ENCODED ⚠️
                    yield "event: error\ndata: Request timed out.\n\n".encode('utf-8')
                    break

                safe_text = text.replace('\n', '\\n') if text else ""

                # ⚠️ MUST BE ENCODED ⚠️
                yield f"event: {event_type}\ndata: {safe_text}\n\n".encode('utf-8')

                if event_type in ("done", "error"):
                    break
        except GeneratorExit:
            # CATCH DISCONNECT: If user clicks "Stop" or closes tab
            client_disconnected.set()
            # Drain the queue to instantly unblock the backend thread so it aborts
            while not chunk_queue.empty():
                try:
                    chunk_queue.get_nowait()
                except queue.Empty:
                    break


    return Response(
        generate(),
        mimetype="text/event-stream",
        direct_passthrough=True,
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        }
    )


# ══════════════════════════════════════════════════════════════════
# CHAT — POLLING FALLBACK (Phase 5)
# SSE (/chat above) needs one connection held open for the entire reply,
# which mobile carrier networks sometimes buffer or silently kill mid-
# stream — the client then has no way to tell "still generating" apart
# from "connection died." This is the alternative: start the job (returns
# instantly), then poll a cheap short-lived endpoint until it's done.
# Each poll is independent, so a dropped request just gets retried on the
# next one — no lost reply, no ambiguous state.
#
#   POST /chat_poll_start  -> {"status": "ok", "job_id": "..."}
#   GET  /chat_poll_status?job_id=...
#       -> {"status": "pending"}
#       -> {"status": "done", "reply": "..."}
#       -> {"status": "error", "message": "..."}
# ══════════════════════════════════════════════════════════════════

_poll_jobs = {}
_poll_jobs_lock = threading.Lock()
_POLL_JOB_TTL_SECONDS = 600  # stale/abandoned jobs swept on the next start call

def _cleanup_old_poll_jobs():
    cutoff = time.time() - _POLL_JOB_TTL_SECONDS
    with _poll_jobs_lock:
        stale = [jid for jid, job in _poll_jobs.items() if job["created_at"] < cutoff]
        for jid in stale:
            del _poll_jobs[jid]

@app.route("/chat_poll_start", methods=["POST"])
@require_api_token
def chat_poll_start():
    data      = request.get_json()
    user_text = data.get("message", "")
    character = data.get("character", "")
    model     = data.get("model", list(backend.MODEL_OPTIONS.keys())[0])

    _cleanup_old_poll_jobs()

    job_id = uuid.uuid4().hex
    with _poll_jobs_lock:
        _poll_jobs[job_id] = {"status": "pending", "reply": None, "error": None, "created_at": time.time()}

    def on_chunk(text):
        pass  # polling clients only need the final result, not live tokens

    def on_complete(final):
        reply = (final or "").strip()
        if not reply:
            msgs = backend.CURRENT_CHAT.get("messages", [])
            for msg in reversed(msgs):
                if msg.get("role") == "assistant" and msg.get("content", "").strip():
                    reply = msg["content"].strip()
                    break
        with _poll_jobs_lock:
            _poll_jobs[job_id]["status"] = "done"
            _poll_jobs[job_id]["reply"] = reply or "..."

    def on_error(msg):
        with _poll_jobs_lock:
            _poll_jobs[job_id]["status"] = "error"
            _poll_jobs[job_id]["error"] = msg

    threading.Thread(
        target=backend.generate_reply,
        args=(user_text, character, model, on_chunk, on_complete, on_error),
        daemon=True
    ).start()

    return jsonify({"status": "ok", "job_id": job_id})

@app.route("/chat_poll_status", methods=["GET"])
@require_api_token
def chat_poll_status():
    job_id = request.args.get("job_id", "")
    with _poll_jobs_lock:
        job = _poll_jobs.get(job_id)

    if not job:
        return jsonify({"status": "error", "message": "Unknown or expired job_id"}), 404
    if job["status"] == "pending":
        return jsonify({"status": "pending"})
    if job["status"] == "error":
        return jsonify({"status": "error", "message": job["error"]})
    return jsonify({"status": "done", "reply": job["reply"]})


# ══════════════════════════════════════════════════════════════════
# UTILITY ACTIONS
# ══════════════════════════════════════════════════════════════════

@app.route("/retry", methods=["POST"])
def retry():
    data      = request.get_json()
    character = data.get("character", "")
    model     = data.get("model", list(backend.MODEL_OPTIONS.keys())[0])

    # 1. ADD BACKPRESSURE
    chunk_queue = queue.Queue(maxsize=15)

    # 2. DISCONNECT SIGNAL
    client_disconnected = threading.Event()

    def on_chunk(text):
        if client_disconnected.is_set():
            raise InterruptedError("Client disconnected")
        try:
            chunk_queue.put(("chunk", text), timeout=5.0)
        except queue.Full:
            client_disconnected.set()
            raise InterruptedError("Client stream blocked")

    def on_complete(final):
        if client_disconnected.is_set(): return
        reply = (final or "").strip()
        if not reply:
            reply = "..."
        chunk_queue.put(("done", reply))

    def on_error(msg):
        if not client_disconnected.is_set():
            chunk_queue.put(("error", msg))

    # Run in background so the generator can drain the queue
    threading.Thread(
        target=backend.regenerate_reply,
        args=(character, model, on_chunk, on_complete, on_error),
        daemon=True
    ).start()

    def generate():
        try:
            while True:
                try:
                    event_type, text = chunk_queue.get(timeout=120)
                except queue.Empty:
                    yield "event: error\ndata: Request timed out.\n\n".encode('utf-8')
                    break

                safe_text = text.replace('\n', '\\n') if text else ""
                yield f"event: {event_type}\ndata: {safe_text}\n\n".encode('utf-8')

                if event_type in ("done", "error"):
                    break

        except GeneratorExit:
            # 3. CATCH DISCONNECT
            client_disconnected.set()
            while not chunk_queue.empty():
                try:
                    chunk_queue.get_nowait()
                except queue.Empty:
                    break

    return Response(
        generate(),
        mimetype="text/event-stream",
        direct_passthrough=True,
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        }
    )

@app.route("/undo", methods=["POST"])
def undo():
    try:
        removed = backend.undo()
        return jsonify({"removed_user_message": removed or ""})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
@app.route("/continue", methods=["POST"])
def continue_reply():
    data      = request.get_json()
    character = data.get("character", "")
    model     = data.get("model", list(backend.MODEL_OPTIONS.keys())[0])

    # 1. ADD BACKPRESSURE
    chunk_queue = queue.Queue(maxsize=15)

    # 2. DISCONNECT SIGNAL
    client_disconnected = threading.Event()

    def on_chunk(text):
        if client_disconnected.is_set():
            raise InterruptedError("Client disconnected")
        try:
            chunk_queue.put(("chunk", text), timeout=5.0)
        except queue.Full:
            client_disconnected.set()
            raise InterruptedError("Client stream blocked")

    def on_complete(final):
        if client_disconnected.is_set(): return
        chunk_queue.put(("done", (final or "").strip()))

    def on_error(msg):
        if not client_disconnected.is_set():
            chunk_queue.put(("error", msg))

    threading.Thread(
        target=backend.generate_continuation,
        args=(character, model, on_chunk, on_complete, on_error),
        daemon=True
    ).start()

    def generate():
        try:
            while True:
                try:
                    event_type, text = chunk_queue.get(timeout=120)
                except queue.Empty:
                    yield "event: error\ndata: Request timed out.\n\n".encode('utf-8')
                    break

                safe_text = text.replace('\n', '\\n') if text else ""
                yield f"event: {event_type}\ndata: {safe_text}\n\n".encode('utf-8')

                if event_type in ("done", "error"):
                    break
        except GeneratorExit:
            # 3. CATCH DISCONNECT
            client_disconnected.set()
            while not chunk_queue.empty():
                try:
                    chunk_queue.get_nowait()
                except queue.Empty:
                    break

    return Response(
        generate(),
        mimetype="text/event-stream",
        direct_passthrough=True, # Added this bypass
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        }
    )

@app.route("/impersonate", methods=["POST"])
def impersonate():
    data      = request.get_json()
    character = data.get("character", "")
    model     = data.get("model", list(backend.MODEL_OPTIONS.keys())[0])
    try:
        suggested_text = backend.impersonate_user(character, model)
        return jsonify({"suggested": suggested_text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/scene", methods=["POST"])
def scene():
    data      = request.get_json()
    character = data.get("character", "")
    model     = data.get("model", list(backend.MODEL_OPTIONS.keys())[0])
    try:
        result     = backend.generate_scene(character, model)
        if "error" in result:
            return jsonify({"error": result["error"]}), 500
        image_path = result.get("image")
        image_url  = f"/serve_image?path={image_path}" if image_path else None
        return jsonify({"text": result.get("text", ""), "image_url": image_url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/settings", methods=["POST"])
def settings():
    data          = request.get_json()
    character     = data.get("character", "")
    model         = data.get("model", "")
    char_settings = backend.CHARACTER_SETTINGS.get(character, {})
    mode          = backend.CHARACTER_MODES.get(character, "general").capitalize()
    rel_mode      = char_settings.get("relationship_mode", "normal").capitalize()
    model_info    = backend.MODEL_OPTIONS.get(model, {})
    provider      = model_info.get("provider", "Unknown").capitalize()
    if provider.lower() == "openrouter":
        active_api      = backend.CLOUD_APIS[backend.current_api_index]["name"]
        connection_info = f"{provider} ({active_api})"
    elif provider.lower() == "groq":
        active_api      = backend.GROQ_APIS[backend.current_groq_index]["name"]
        connection_info = f"{provider} ({active_api})"
    else:
        connection_info = f"{provider} (Local Device)"
    msgs      = backend.CURRENT_CHAT.get("messages", [])
    full_text = " ".join([m.get("content", "") for m in msgs])
    token_est = int(len(full_text.split()) * 1.3)
    return jsonify({
        "active_model":      model,
        "connection":        connection_info,
        "story_mode":        mode,
        "relationship_mode": rel_mode,
        "temperature":       char_settings.get("temperature", 0.8),
        "top_p":             char_settings.get("top_p", 0.9),
        "max_tokens":        char_settings.get("max_tokens", 500),
        "rep_penalty":       char_settings.get("repetition_penalty", 1.1),
        "estimated_tokens":  token_est,
        "show_thoughts":     getattr(backend, 'show_thoughts', False),
    })

@app.route("/open_folder", methods=["POST"])
def open_folder():
    import platform, subprocess
    path = backend.BASE_DIR
    try:
        if platform.system() == "Windows":
            os.startfile(path)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
# ══════════════════════════════════════════════════════════════════
# WARM-UP PING
# Wakes up serverless GPUs (like Bytez) in the background
# ══════════════════════════════════════════════════════════════════
@app.route('/warmup', methods=['POST'])
def warmup_endpoint():
    data = request.json
    model = data.get("model")
    if model:
        # Pings the backend function we added to app_backend.py
        backend.warm_up_model(model)
    return jsonify({"status": "warming up"})

# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n✅ Server running — open your browser at: http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
