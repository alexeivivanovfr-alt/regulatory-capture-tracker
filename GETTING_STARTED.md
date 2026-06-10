# Getting Started: Step-by-Step Guide

This walks you through running the pipeline for the first time,
using the Boeing/FAA case as validation.

## Prerequisites

- Python 3.11+
- Docker (for Neo4j)
- An Anthropic API key (for language provenance analysis)
- ~5GB disk space for the FEC data

---

## Step 1: Install

```bash
# Clone the repo
git clone <your-repo-url>
cd regulatory-capture-tracker

# Install Python dependencies
pip install -e ".[dev]"

# Verify installation
python -c "import duckdb, anthropic, neo4j; print('All good')"
```

---

## Step 2: Configure environment

```bash
cp .env.example .env
```

Edit `.env`:
```
ANTHROPIC_API_KEY=sk-ant-...    # Required for language analysis
NEO4J_PASSWORD=capturetracker   # Set your own
```

---

## Step 3: Start Neo4j

```bash
docker-compose up -d neo4j

# Wait ~30 seconds then verify it's running
docker-compose logs neo4j | tail -5

# Open Neo4j browser (optional, for visual exploration)
# http://localhost:7474
# Login: neo4j / capturetracker
```

---

## Step 4: Initialize the database schema

```bash
python -c "
from neo4j import GraphDatabase
import os
driver = GraphDatabase.driver(
    'bolt://localhost:7687',
    auth=('neo4j', os.getenv('NEO4J_PASSWORD', 'capturetracker'))
)
with driver.session() as session:
    schema = open('graph/schema.cypher').read()
    # Run each statement separately
    for stmt in schema.split(';'):
        stmt = stmt.strip()
        if stmt and not stmt.startswith('//') and not stmt.startswith('/*'):
            try:
                session.run(stmt)
            except Exception as e:
                print(f'Skip: {e}')
print('Schema initialized')
"
```

---

## Step 5: Run your first ingestion (Federal Register — no download needed)

```bash
# Fetch FAA rules — uses the public API, no download required
python -m ingestion.federal_register ./data

# You should see:
# Fetching FAA aviation safety rules (2015-2024)...
# Fetched page 1/N (1000 documents so far)
# ...
# ✓ Fetched N documents for federal-aviation-administration
# Saved N documents to data/raw/federal_register/faa_rules.json
```

---

## Step 6: Run lobbying disclosure search

```bash
# Search LDA API for Boeing lobbying (uses public API)
python -m ingestion.lobbying ./data

# You should see Boeing filings and any lobbyists with former FAA positions
```

---

## Step 7: Language provenance analysis (requires API key)

```bash
# Run the built-in Boeing/MCAS example
python -m processing.language_provenance

# You should see an industry influence score and specific passages
# that were adopted from Boeing's comment letter into the final rule
```

---

## Step 8: FEC data (larger download)

```bash
# Start with committee master — small file, fast
python -c "
import asyncio
from pathlib import Path
from ingestion.fec import FECIngester

async def main():
    i = FECIngester(Path('./data'))
    await i.download('cm', '2024')
    i.load_to_duckdb('cm', '2024')
    print('Committee master loaded')

asyncio.run(main())
"
```

Then individual contributions (warning: ~3GB download):
```bash
python -c "
import asyncio
from pathlib import Path
from ingestion.fec import FECIngester, run_boeing_validation

asyncio.run(run_boeing_validation(Path('./data'), '2024'))
"
```

---

## What to expect from the Boeing/FAA validation

If the pipeline is working correctly, you should find:

1. **Lobbying**: Multiple Boeing-related entities spent millions lobbying
   on aviation/transportation issues, contacting the FAA directly

2. **Revolving door**: Multiple lobbyists with former FAA positions
   now working for Boeing or aerospace lobbying firms

3. **Language provenance**: The MCAS certification rules show language
   shifts toward manufacturer self-certification (Boeing's preferred position)
   rather than independent FAA review (the original proposal)

4. **Donations**: Significant Boeing PAC and employee donations to members
   of the House Transportation Committee and Senate Commerce Committee
   (which oversee FAA)

These findings should match what investigative journalists have already
documented — that's how you know the pipeline is working.

---

## Common issues

**Neo4j won't start**: Make sure Docker is running and port 7687 is free
```bash
docker ps
lsof -i :7687
```

**FEC download is slow**: The individual contributions file is ~3GB.
Start with `cm` (committee master) and `pas2` (PAC contributions) first.

**Anthropic API errors**: Check your API key in `.env`.
The language provenance analysis uses ~2000 tokens per rule analyzed.

**DuckDB memory errors**: The `indiv` file is large. 
Add `duckdb.execute("SET memory_limit='4GB'")` if needed.
