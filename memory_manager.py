import os
import json
import threading
import traceback
from collections import OrderedDict


class NullVectorMemory:
    """
    Drop-in stand-in for the old FAISS/sentence-transformers VectorMemory.
    Removed to drop the torch / sentence-transformers / faiss dependency
    chain entirely — it was blowing past PythonAnywhere's disk quota on
    install, and semantic recall wasn't something this app relied on.

    Kept compatible with every attribute app_backend.py actually touches:
    .memories, .search(), .add_memory(), and ._lock (used directly by
    regenerate_reply()'s undo-cleanup code). .memories starts and stays
    empty, so the FAISS-specific rebuild branch in that method is simply
    never reached — no changes needed there.
    """
    def __init__(self, storage_path_prefix):
        self.memories = []
        self._lock = threading.RLock()

    def add_memory(self, text, role, timestamp, turn_id=0, active=True):
        pass

    def search(self, query, top_k=3):
        return []

    def purge_after_turn(self, turn_id):
        pass


from db import get_db  # Import your shared DB connection
class DirectPineconeMemory:
    """
    Lightweight wrapper replacing the deleted pinecone_vector_memory.py.
    Since memory_context.py now handles saving and searching directly,
    this class only exists to handle the purge_after_turn() requirement
    when the user clicks 'Undo'.
    """
    def __init__(self, character_id, chat_id):
        self.character_id = character_id
        self.chat_id = str(chat_id)
        # Dummy property to prevent len() crashes in app_backend's regenerate_reply
        self.memories = [] 

    def purge_after_turn(self, turn_id):
        import os
        from pinecone import Pinecone
        try:
            pc_key = os.getenv("PINECONE_API_KEY")
            if not pc_key:
                return
            
            proxy_url = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
            pc_kwargs = {"api_key": pc_key}
            if proxy_url:
                pc_kwargs["proxy_url"] = proxy_url
                
            pc = Pinecone(**pc_kwargs)
            active_indexes = pc.list_indexes().names()
            
            if active_indexes:
                index = pc.Index(active_indexes[0])
                # Delete vectors for this chat strictly greater than the undone turn
                index.delete(filter={
                    "character": {"$eq": self.character_id},
                    "chat_id": {"$eq": self.chat_id},
                    "turn_id": {"$gt": turn_id}
                })
                print(f"[🔄 PINECONE] Purged undone memories after turn {turn_id}.")
        except Exception as e:
            print(f"[⚠️ PINECONE] Could not purge undone vectors: {e}")



class AdvancedMemoryManager:
    def __init__(self, base_dir):
        # Base dir kept for compatibility, but we no longer need os.makedirs
        self.base_dir = base_dir
        self.lock = threading.Lock()
        self._vector_cache = OrderedDict()

    # We keep these signature methods so nothing upstream breaks, 
    # but we will bypass them internally.
    def get_fact_file(self, character_id, chat_id):
        return f"{character_id}_{chat_id}_facts"

    def get_scene_file(self, character_id, chat_id):
        return f"{character_id}_{chat_id}_scene"
        
    def get_summary_file(self, character_id, chat_id):
        return f"{character_id}_{chat_id}_summary"

    def get_vector_memory(self, character_id, chat_id):
        """Returns the initialized VectorMemory instance for a specific chat from cache."""
        cache_key = f"{character_id}_{chat_id}"
        
        with self.lock:
            # Check if we already loaded the vector memory for this chat
            if cache_key in self._vector_cache:
                self._vector_cache.move_to_end(cache_key)  # mark as recently used
                return self._vector_cache[cache_key]

            backend = os.environ.get("VECTOR_MEMORY_BACKEND", "null").lower()
            if backend == "pinecone":
                # Link to our new native class instead of the deleted file
                vector_instance = DirectPineconeMemory(character_id, chat_id)
            else:
                # Default to the safe, no-op Null Vector Memory
                vector_instance = NullVectorMemory("dummy_prefix")

            self._vector_cache[cache_key] = vector_instance
            if len(self._vector_cache) > 20:
                self._vector_cache.popitem(last=False)  # evict least-recently-used

            return vector_instance
        
    def load_summary(self, character_id, chat_id):
        """Thread-safe loading of the tiered summary from MongoDB."""
        db = get_db()
        with self.lock:
            doc = db.summaries.find_one({"_id": f"{character_id}_{chat_id}"})
            if doc and "data" in doc:
                return doc["data"]
            return {"chronicle": [], "current_arc": ""}

    def save_summary(self, character_id, chat_id, summary_data):
        """Thread-safe saving of the tiered summary to MongoDB."""
        db = get_db()
        with self.lock:
            try:
                db.summaries.update_one(
                    {"_id": f"{character_id}_{chat_id}"},
                    {"$set": {
                        "character": character_id, 
                        "chat_id": chat_id, 
                        "data": summary_data
                    }},
                    upsert=True
                )
            except Exception as e:
                print(f"[⚠️ MANAGER] Summary Save Error: {e}")

    def initialize_chat_memory(self, character_id, chat_id):
        """Creates the baseline memory structures in MongoDB for a new chat."""
        db = get_db()
        doc_id = f"{character_id}_{chat_id}"
        
        with self.lock:
            # Upsert baseline facts and scene if they don't exist
            db.chat_states.update_one(
                {"_id": doc_id},
                {"$setOnInsert": {
                    "facts": {
                        "user_profile": {},
                        "character_discoveries": {},
                        "milestones": []
                    },
                    "scene": {
                        "current_disposition": "Unknown starting dynamic.",
                        "structured_scene": {},
                        "pending_shift": "",
                        "relationship_tag": "stranger",
                        "relationship_stage": "EARLY",
                    }
                }},
                upsert=True
            )