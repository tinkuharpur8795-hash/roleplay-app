import os
import time
from dotenv import load_dotenv
from pinecone import Pinecone, ServerlessSpec

# 1. Load keys
load_dotenv()
api_key = os.getenv("PINECONE_API_KEY")

if not api_key:
    print("❌ Error: PINECONE_API_KEY not found in .env")
    exit()

pc = Pinecone(api_key=api_key)
index_name = "roleplay-memory"

# Grab the dimension from your .env, default to Mistral's 1024
dimension = int(os.getenv("MISTRAL_EMBED_DIM", "1024"))

existing_indexes = pc.list_indexes().names()

if index_name not in existing_indexes:
    print(f"🚀 Creating new Pinecone index: '{index_name}' (Dimension: {dimension})...")
    print("⏳ This usually takes about 10-30 seconds on Pinecone's server...")
    
    # 2. Create a serverless index on the free tier
    pc.create_index(
        name=index_name,
        dimension=dimension,
        metric="cosine",  # Standard metric for text embeddings
        spec=ServerlessSpec(
            cloud="aws",
            region="us-east-1"
        )
    )
    
    # 3. Wait for the server to finish provisioning it
    while not pc.describe_index(index_name).status['ready']:
        time.sleep(1)
        
    print("✅ Index created and ready to accept data!")
else:
    print(f"✅ Index '{index_name}' already exists.")