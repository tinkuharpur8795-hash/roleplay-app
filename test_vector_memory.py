"""
test_vector_memory.py

Standalone sanity check for QdrantVectorMemory — run this directly
(not through the Flask app) to confirm the whole pipeline works before
trusting it inside a real chat: embed -> upsert -> semantic search ->
turn-based filtering -> purge.

Usage:
    export QDRANT_URL=...
    export QDRANT_API_KEY=...
    export MISTRAL_API_KEY=...
    export MISTRAL_EMBED_MODEL=mistral-embed   # or leave default
    export MISTRAL_EMBED_DIM=1024              # must match the model
    python3 -u test_vector_memory.py
"""

import os
import sys
import time

from qdrant_vector_memory import QdrantVectorMemory

BANK_ID = "test_bank_delete_me"  # isolated so this never touches real chat data


def check(label, condition):
    status = "✅ PASS" if condition else "❌ FAIL"
    print(f"{status} — {label}")
    if not condition:
        FAILURES.append(label)


FAILURES = []


def main():
    for var in ("QDRANT_URL", "QDRANT_API_KEY", "MISTRAL_API_KEY"):
        if not os.environ.get(var):
            print(f"Missing env var {var} — set it before running this test.")
            sys.exit(1)

    print(f"Using embed model: {os.environ.get('MISTRAL_EMBED_MODEL', 'mistral-embed')}")
    print(f"Bank id: {BANK_ID}\n")

    vec = QdrantVectorMemory(storage_path_prefix=BANK_ID, bank_id=BANK_ID)

    # --- 0. Clean slate: purge anything left over from a previous run ---
    vec.purge_after_turn(-1)
    time.sleep(1)  # give Qdrant a moment to apply the delete
    check("starts empty after cleanup", len(vec.memories) == 0)

    # --- 1. Add a few distinct, semantically separable memories ---
    vec.add_memory(
        "{{user}}: My dog Biscuit ran away last week and I've been really worried. || {{char}}: That sounds so stressful, I hope Biscuit turns up safe.",
        role="exchange", timestamp=time.time(), turn_id=1,
    )
    vec.add_memory(
        "{{user}}: I just got promoted to senior engineer at work! || {{char}}: That's amazing, congratulations!",
        role="exchange", timestamp=time.time(), turn_id=2,
    )
    vec.add_memory(
        "{{user}}: We're planning a trip to Goa next month. || {{char}}: Goa in that season should be beautiful.",
        role="exchange", timestamp=time.time(), turn_id=3,
    )
    time.sleep(1)  # Qdrant Cloud free tier can lag slightly on indexing

    check("memory count reflects 3 adds", len(vec.memories) == 3)

    # --- 2. Semantic search: a query about the dog should surface memory #1
    #        and NOT rank the promotion/travel ones above it ---
    results = vec.search("did biscuit ever come back home?", top_k=2)
    check("search returns results", len(results) > 0)
    if results:
        top_text = results[0]["text"]
        check("top hit is the dog memory, not an unrelated one", "Biscuit" in top_text)
        print(f"    top hit turn_id={results[0]['turn_id']} score={results[0]['score']:.4f}")
        print(f"    text: {top_text[:80]}...")

    # --- 3. purge_after_turn: simulate an undo back to turn 2 ---
    # This is the exact operation regenerate_reply() calls -- turn 3
    # (the Goa memory) should disappear, turns 1-2 should survive.
    vec.purge_after_turn(2)
    time.sleep(1)
    check("purge_after_turn drops count from 3 to 2", len(vec.memories) == 2)

    remaining = vec.search("goa trip", top_k=5)
    check("purged memory no longer retrievable", not any("Goa" in r["text"] for r in remaining))

    # --- 4. Final cleanup so this test never pollutes real data ---
    vec.purge_after_turn(-1)
    time.sleep(1)
    check("cleanup leaves bank empty", len(vec.memories) == 0)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {FAILURES}")
        sys.exit(1)
    else:
        print("All checks passed — embed -> store -> search -> purge pipeline is working.")


if __name__ == "__main__":
    main()