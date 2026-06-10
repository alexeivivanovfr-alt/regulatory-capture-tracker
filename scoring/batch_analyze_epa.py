"""
Batch EPA Rule Analyzer

Analyzes all significant EPA rules with full text available.
Saves progress after each rule so you can stop and restart anytime.
Skips rules already analyzed.

Usage: python3 scoring/batch_analyze_epa.py
"""

import json
import os
import re
import time
import httpx
from pathlib import Path
from neo4j import GraphDatabase
from rich.console import Console
import anthropic

console = Console()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "capturetracker")

client = anthropic.Anthropic()
driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

PROGRESS_FILE = Path("data/epa_analysis_progress.json")


def get_docket_id(doc):
    ids = doc.get("docket_ids", [])
    raw = ids[0] if ids else doc.get("document_number", "")
    return raw.replace("Docket No. ", "").strip()


def load_progress():
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"analyzed": [], "results": []}


def save_progress(progress):
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


def fetch_rule_text(url, max_chars=3000):
    try:
        response = httpx.get(url, timeout=30, follow_redirects=True)
        response.raise_for_status()
        text = re.sub(r'<[^>]+>', ' ', response.text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:max_chars]
    except Exception:
        return ""


def classify_industry(title):
    title_lower = title.lower()
    if any(w in title_lower for w in ['coal', 'mercury', 'mats', 'combustion residual']):
        return "COAL_INDUSTRY", "Coal industry, electric utilities"
    elif any(w in title_lower for w in ['oil', 'gas', 'methane', 'petroleum', 'refin']):
        return "OIL_GAS_INDUSTRY", "Oil and gas industry"
    elif any(w in title_lower for w in ['chemical', 'tsca', 'toxic substance', 'pfas', 'pesticide']):
        return "CHEMICAL_INDUSTRY", "Chemical manufacturers"
    elif any(w in title_lower for w in ['renewable fuel', 'rfs', 'biofuel', 'ethanol']):
        return "AGRI_FUEL_INDUSTRY", "Agriculture and biofuel industry"
    elif any(w in title_lower for w in ['vehicle', 'motor', 'engine', 'automotive']):
        return "AUTO_INDUSTRY", "Automotive and trucking industry"
    elif any(w in title_lower for w in ['water', 'drinking', 'effluent', 'wastewater']):
        return "WATER_INDUSTRY", "Industrial water dischargers, utilities"
    elif any(w in title_lower for w in ['greenhouse gas', 'climate', 'carbon', 'co2']):
        return "FOSSIL_FUEL_INDUSTRY", "Fossil fuel industry"
    else:
        return "REGULATED_INDUSTRY", "Regulated industry"


def analyze_rule(doc):
    title = doc.get("title", "")
    url = doc.get("full_text_xml_url", "")
    docket_id = get_docket_id(doc)
    pub_date = doc.get("publication_date", "")

    if not url or not docket_id:
        return None

    org_id, industry_desc = classify_industry(title)
    final_text = fetch_rule_text(url)
    if not final_text:
        return None

    prompt = f"""You are an expert regulatory analyst investigating regulatory capture at the US EPA.

Analyze this EPA rule for signs of regulatory capture by the regulated industry.

RULE TITLE: {title}
DATE: {pub_date}
LIKELY INDUSTRY: {industry_desc}

RULE TEXT (excerpt):
{final_text}

Look for these EPA capture signals:
1. Rollbacks or repeals of previously stronger requirements
2. Reconsideration that weakens rules after industry petition
3. Extended compliance timelines
4. Self-monitoring instead of independent inspection
5. Broad exemptions for existing facilities
6. Vague standards that give industry discretion
7. Cost-benefit framing that prioritizes industry costs over health benefits
8. Postponement of effectiveness delaying rules from taking effect

Respond ONLY with valid JSON:
{{
    "capture_signals": [
        {{
            "signal_type": "rollback|reconsideration|long_timeline|self_reporting|exemption|vague_standard|cost_benefit|postponement",
            "significance": "high|medium|low",
            "interpretation": "brief explanation"
        }}
    ],
    "genuine_protections": [
        {{
            "description": "genuine public health protection",
            "significance": "high|medium|low"
        }}
    ],
    "industry_influence_score": <integer 0-100>,
    "confidence": "high|medium|low",
    "key_finding": "single most important finding in one sentence"
}}"""

    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.replace("```json", "").replace("```", "").strip()
        analysis = json.loads(raw)
    except Exception:
        return None

    return {
        "docket_id": docket_id,
        "title": title,
        "pub_date": pub_date,
        "score": analysis.get("industry_influence_score", 0),
        "confidence": analysis.get("confidence", "low"),
        "key_finding": analysis.get("key_finding", ""),
        "org_id": org_id,
        "industry_desc": industry_desc,
        "analysis": analysis
    }


def update_graph(result):
    with driver.session() as session:
        session.run("""
            MERGE (r:Rule {id: $docket_id})
            SET r.title = $title,
                r.agency = 'EPA',
                r.publication_date = $pub_date,
                r.industry_language_score = $score,
                r.capture_score = $score,
                r.key_finding = $key_finding,
                r.confidence = $confidence
        """,
            docket_id=result["docket_id"],
            title=result["title"][:200],
            pub_date=result["pub_date"],
            score=float(result["score"]),
            key_finding=result["key_finding"],
            confidence=result["confidence"]
        )
        session.run("""
            MERGE (o:Organization {id: $org_id})
            SET o.name = $industry_desc, o.sector = 'private'
            WITH o
            MATCH (r:Rule {id: $docket_id})
            MERGE (o)-[l:LOBBIED_ON]->(r)
            SET l.influence_score = $score,
                l.language_adopted = $high_capture
        """,
            org_id=result["org_id"],
            industry_desc=result["industry_desc"],
            docket_id=result["docket_id"],
            score=float(result["score"]),
            high_capture=result["score"] > 60
        )


def print_summary(progress):
    results = progress.get("results", [])
    if not results:
        return

    console.print(f"\n[bold]Results so far: {len(results)} rules analyzed[/bold]")
    high = [r for r in results if r["score"] >= 60]
    medium = [r for r in results if 30 <= r["score"] < 60]
    console.print(f"  🔴 High risk (60+): {len(high)}")
    console.print(f"  🟡 Medium risk (30-60): {len(medium)}")
    console.print(f"  🟢 Low risk (<30): {len(results) - len(high) - len(medium)}")

    if high:
        console.print("\n[bold red]High risk rules:[/bold red]")
        for r in sorted(high, key=lambda x: -x["score"]):
            console.print(f"  {r['score']:>3}/100 | {r['pub_date']} | {r['title'][:60]}")
            if r.get("key_finding"):
                console.print(f"         → {r['key_finding'][:90]}")


def main():
    console.print("[bold]EPA Batch Rule Analyzer[/bold]")
    console.print("Progress is saved after each rule — safe to stop and restart.\n")

    with open("data/raw/federal_register/epa_rules.json") as f:
        docs = json.load(f)

    targets = [d for d in docs if d.get("significant") and d.get("full_text_xml_url")]
    console.print(f"Found {len(targets)} significant rules with full text")

    progress = load_progress()
    already_done = set(progress.get("analyzed", []))
    remaining = [d for d in targets if get_docket_id(d) not in already_done]

    console.print(f"Already analyzed: {len(already_done)}")
    console.print(f"Remaining: {len(remaining)}\n")

    if not remaining:
        console.print("[green]All rules already analyzed![/green]")
        print_summary(progress)
        return

    for i, doc in enumerate(remaining):
        title = doc.get("title", "")[:60]
        console.print(f"[{i+1}/{len(remaining)}] {title}...")

        result = analyze_rule(doc)

        if result is None:
            console.print(f"  [yellow]Skipped[/yellow]")
            continue

        score = result["score"]
        color = "red" if score > 60 else "yellow" if score > 30 else "green"
        console.print(f"  [{color}]{score}/100[/{color}] — {result['key_finding'][:90]}")

        update_graph(result)

        progress["analyzed"].append(get_docket_id(doc))
        progress["results"].append({
            "docket_id": result["docket_id"],
            "title": result["title"],
            "pub_date": result["pub_date"],
            "score": result["score"],
            "confidence": result["confidence"],
            "key_finding": result["key_finding"]
        })
        save_progress(progress)
        time.sleep(0.5)

    console.print("\n[bold green]Done![/bold green]")
    print_summary(progress)
    driver.close()


if __name__ == "__main__":
    main()
