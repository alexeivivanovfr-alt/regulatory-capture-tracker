"""Quick fix: load FAA rules into Neo4j in batches with progress output."""

import json
from neo4j import GraphDatabase

print("Connecting to Neo4j...")
driver = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "capturetracker"))
print("Connected!")

print("Reading JSON file...")
with open("data/raw/federal_register/faa_rules.json") as f:
    docs = json.load(f)
print(f"Found {len(docs)} documents")

# Filter and clean docs first
rules = []
for doc in docs:
    if doc.get("type") not in ["Rule", "Proposed Rule"]:
        continue
    raw_ids = doc.get("docket_ids", [])
    docket_id = raw_ids[0].replace("Docket No. ", "").strip() if raw_ids else doc.get("document_number", "")
    if not docket_id:
        continue
    rules.append({
        "id": docket_id,
        "title": doc.get("title", "")[:200],
        "doc_type": doc.get("type", ""),
        "pub_date": doc.get("publication_date", ""),
        "significant": bool(doc.get("significant", False)),
        "citation": doc.get("citation", "")
    })

print(f"Filtered to {len(rules)} rules, loading in batches...")

BATCH_SIZE = 100
loaded = 0

with driver.session() as session:
    for i in range(0, len(rules), BATCH_SIZE):
        batch = rules[i:i + BATCH_SIZE]
        session.run("""
            UNWIND $batch AS doc
            MERGE (r:Rule {id: doc.id})
            SET r.title = doc.title,
                r.agency = 'FAA',
                r.doc_type = doc.doc_type,
                r.publication_date = doc.pub_date,
                r.significant = doc.significant,
                r.citation = doc.citation,
                r.capture_score = 0.0
        """, batch=batch)
        loaded += len(batch)
        print(f"  Loaded {loaded}/{len(rules)} rules...")

print(f"\nDone! Loaded {loaded} rules into Neo4j")
driver.close()
