# Regulatory Capture Tracker

A tool for detecting regulatory capture in US federal agencies by connecting:
- Campaign finance data (FEC)
- Lobbying disclosures (Senate LD-2)
- Federal Register rulemaking
- Revolving door personnel flows
- Regulatory language provenance

## Project Structure

```
regulatory-capture-tracker/
├── README.md
├── pyproject.toml          # Dependencies
├── .env.example            # Environment variables template
│
├── ingestion/              # Data collection
│   ├── fec.py              # FEC bulk data
│   ├── lobbying.py         # Senate LD-2 filings
│   ├── federal_register.py # Federal Register API
│   └── oge.py              # OGE financial disclosures
│
├── processing/             # Data transformation
│   ├── entity_resolution.py  # Match persons across datasets
│   ├── revolving_door.py     # Detect industry↔gov transitions
│   └── language_provenance.py # Compare rule text to industry comments
│
├── graph/                  # Neo4j database layer
│   ├── schema.cypher       # Database schema
│   ├── loader.py           # Load processed data into graph
│   └── queries.py          # Common graph queries
│
├── scoring/                # Capture scoring
│   └── scorer.py           # Combine signals into capture score
│
├── pipeline/               # Orchestration
│   └── flows.py            # Prefect workflow definitions
│
├── api/                    # FastAPI backend
│   └── main.py
│
└── data/                   # Local data storage (gitignored)
    ├── raw/                # Downloaded raw files
    ├── processed/          # Cleaned intermediate data
    └── graph_exports/      # Neo4j exports
```

## Quick Start

```bash
# 1. Clone and install
git clone <repo>
cd regulatory-capture-tracker
pip install -e ".[dev]"

# 2. Configure environment
cp .env.example .env
# Edit .env with your API keys

# 3. Start Neo4j (Docker)
docker-compose up -d neo4j

# 4. Run first ingestion (FEC committee data - small, fast)
python -m ingestion.fec --dataset committees --cycle 2024

# 5. Validate against Boeing/FAA case
python -m pipeline.validate --case boeing_faa
```

## Validation Strategy

Before using this tool to generate new findings, validate it against
the Boeing/FAA MAX case — where regulatory capture is already documented
by journalists. If the pipeline reproduces known findings, it's working.

Known capture events to detect:
- Ali Bahrami: Boeing VP → FAA Associate Administrator (2009) → 
  back to aerospace industry (2018)
- FAA delegating airworthiness certification to Boeing employees
- MCAS system approval without independent review

## Data Sources

| Source | URL | Format | Update Frequency |
|--------|-----|--------|-----------------|
| FEC Bulk Data | fec.gov/data/browse-data | CSV/ZIP | Nightly |
| Senate Lobbying | lda.senate.gov | XML/ZIP | Quarterly |
| Federal Register | federalregister.gov/api | JSON API | Daily |
| OGE Disclosures | oge.gov | PDF/XML | Annual |
| USASpending | usaspending.gov/api | JSON API | Daily |

## Important Notes

- **Entity resolution confidence thresholds**: auto-merge >0.92, human review 0.70-0.92, reject <0.70
- **Never publish findings below 0.65 capture score** without additional human verification
- All data used is public record — no hacking, no scraping behind auth walls
- Causal claims require human editorial judgment; the tool surfaces correlations
