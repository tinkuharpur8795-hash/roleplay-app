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


class AdvancedMemoryManager:
    """
    Orchestrates the multi-layered memory system:
    - Long-term facts (JSON)
    - Scene state (JSON)
    - Semantic episodic recall (Vector DB)
    """
    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.memory_dir = os.path.join(self.base_dir, "advanced_memory")
        self.vector_dir = os.path.join(self.memory_dir, "vector_stores")
        self.facts_dir = os.path.join(self.memory_dir, "long_term_facts")
        self.scene_dir = os.path.join(self.memory_dir, "scene_states")
        
        # Ensure directories exist
        for d in [self.memory_dir, self.vector_dir, self.facts_dir, self.scene_dir]:
            os.makedirs(d, exist_ok=True)
            
        self.lock = threading.Lock()
        
        # Cache to store initialized NullVectorMemory instances — bounded
        # LRU (max 20) so it doesn't grow forever as more chats are opened
        self._vector_cache = OrderedDict()

    def get_fact_file(self, character_id, chat_id):
        return os.path.join(self.facts_dir, f"{character_id}_{chat_id}_facts.json")

    def get_scene_file(self, character_id, chat_id):
        return os.path.join(self.scene_dir, f"{character_id}_{chat_id}_scene.json")

    def get_vector_memory(self, character_id, chat_id):
        """Returns the initialized VectorMemory instance for a specific chat from cache.

        Backend is chosen by the VECTOR_MEMORY_BACKEND env var so this can
        be flipped on/off without a code change:
            unset / "null"  -> NullVectorMemory (current default, no-op)
            "qdrant"        -> QdrantVectorMemory (Qdrant Cloud + OpenRouter
                               embeddings, see qdrant_vector_memory.py)
        """
        cache_key = f"{character_id}_{chat_id}"
        
        with self.lock:
            # Check if we already loaded the vector memory for this chat
            if cache_key in self._vector_cache:
                self._vector_cache.move_to_end(cache_key)  # mark as recently used
                return self._vector_cache[cache_key]

            # If not, initialize it, cache it, and return it
            prefix = os.path.join(self.vector_dir, cache_key)

            backend = os.environ.get("VECTOR_MEMORY_BACKEND", "null").lower()
            if backend == "pinecone":
                try:
                    from pinecone_vector_memory import PineconeVectorMemory
                    vector_instance = PineconeVectorMemory(prefix, bank_id=cache_key)
                except Exception as e:
                    print(f"[⚠️ MANAGER] Pinecone vector memory init failed, falling back to null: {e}")
                    traceback.print_exc()
                    vector_instance = NullVectorMemory(prefix)
            elif backend == "qdrant":
                try:
                    from qdrant_vector_memory import QdrantVectorMemory
                    vector_instance = QdrantVectorMemory(prefix, bank_id=cache_key)
                except Exception as e:
                    # Never let a Qdrant/Cloud hiccup take down chat itself --
                    # fall back to the no-op stub for this instance and log it.
                    print(f"[⚠️ MANAGER] Qdrant vector memory init failed, falling back to null: {e}")
                    traceback.print_exc()
                    vector_instance = NullVectorMemory(prefix)
            else:
                vector_instance = NullVectorMemory(prefix)

            self._vector_cache[cache_key] = vector_instance
            if len(self._vector_cache) > 20:
                self._vector_cache.popitem(last=False)  # evict least-recently-used

            return vector_instance
    def get_summary_file(self, character_id, chat_id):
        """Resolves the centralized path for the Tiered Summary JSON."""
        return os.path.join(self.scene_dir, f"{character_id}_{chat_id}_summary.json")

    def load_summary(self, character_id, chat_id):
        """
        Thread-safe loading of the tiered summary.
        Returns a fresh dictionary structure if the file doesn't exist.
        """
        summary_file = self.get_summary_file(character_id, chat_id)
        with self.lock:
            if os.path.exists(summary_file):
                try:
                    with open(summary_file, 'r', encoding='utf-8') as f:
                        return json.load(f)
                except Exception as e:
                    print(f"[⚠️ MANAGER] Summary Load Error: {e}")
            return {"chronicle": [], "current_arc": ""}

    def save_summary(self, character_id, chat_id, summary_data):
        """Thread-safe saving of the tiered summary dictionary."""
        summary_file = self.get_summary_file(character_id, chat_id)
        with self.lock:
            try:
                with open(summary_file, 'w', encoding='utf-8') as f:
                    json.dump(summary_data, f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"[⚠️ MANAGER] Summary Save Error: {e}")
    def initialize_chat_memory(self, character_id, chat_id):
        """Creates the baseline memory structures for a new chat."""
        fact_file = self.get_fact_file(character_id, chat_id)
        scene_file = self.get_scene_file(character_id, chat_id)

        with self.lock:
            if not os.path.exists(fact_file):
                baseline_facts = {
                    "user_profile": {},
                    "character_discoveries": {},
                    "milestones": []
                }
                with open(fact_file, 'w', encoding='utf-8') as f:
                    json.dump(baseline_facts, f, indent=2)
                    
            if not os.path.exists(scene_file):
                baseline_scene = {
                    "current_disposition": "Unknown starting dynamic.",
                    "structured_scene": {},
                    "pending_shift": "",
                    "relationship_tag":   "stranger",
                    "relationship_stage": "EARLY",
                }
                with open(scene_file, 'w', encoding='utf-8') as f:
                    json.dump(baseline_scene, f, indent=2)