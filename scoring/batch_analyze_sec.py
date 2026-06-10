"""
SEC Batch Rule Analyzer

Analyzes significant SEC rules for regulatory capture signals.

SEC capture patterns:
- Rollback of disclosure requirements (reduces transparency)
- Weakening of enforcement policies
- Expanding exemptions for large financial players
- Reducing oversight of emerging companies
- Rescinding investor protection rules
- "Simplification" that actually reduces accountability

Run: python3 scoring/batch_analyze_sec.py
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path
from neo4j import GraphDatabase
from rich.console import Console
import anthropic

console = Console()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "capturetracker")
client = anthropic.Anthropic()
driver = GraphDatabase.driver(NEO4J_URI, auth=("neo4j", NEO4J_PASSWORD))

PROGRESS_FILE = Path("data/sec_analysis_progress.json")


def load_progress():
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"analyzed": [], "results": []}


def save_progress(progress):
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


def get_docket_id(doc):
    ids = doc.get("docket_ids", [])
    raw = ids[0] if ids else doc.get("document_number", "")
    return raw.replace("Docket No. ", "").strip()


def fetch_rule_text(url, max_chars=3000):
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "--max-time", "20", url],
            capture_output=True, text=True, timeout=25
        )
        if not result.stdout or len(result.stdout) < 100:
            return ""
        text = re.sub(r'<[^>]+>', ' ', result.stdout)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:max_chars]
    except Exception:
        return ""


def analyze_rule(doc):
    title = doc.get("title", "")
    url = doc.get("full_text_xml_url", "")
    docket_id = get_docket_id(doc)
    pub_date = doc.get("publication_date", "")

    if not url or not docket_id:
        return None

    final_text = fetch_rule_text(url)
    if not final_text:
        return None

    prompt = f"""You are an expert regulatory analyst investigating regulatory capture at the US Securities and Exchange Commission (SEC).

Analyze this SEC rule for signs of regulatory capture by the financial industry.

RULE TITLE: {title}
DATE: {pub_date}
REGULATED INDUSTRY: Financial services, investment banks, hedge funds, private equity, public companies

RULE TEXT (excerpt):
{final_text}

Look specifically for these SEC capture signals:
- Rescission or rollback of disclosure requirements (reduces investor transparency)
- Weakening of enforcement policies or settlement procedures
- Expanding exemptions that benefit large financial institutions
- "Simplification" that reduces investor protections
- Reducing oversight of emerging companies or private markets
- Rolling back climate or ESG disclosure requirements
- Weakening rules on conflicts of interest
- Reducing penalties or enforcement discretion
- Self-regulatory organization (SRO) rules that favor exchanges over investors
- Narrowing definitions that exempt large players

Also: cost-benefit framing that underweights investor protection, industry language adopted verbatim, unusually long compliance timelines.

Respond ONLY with valid JSON:
{{
    "capture_signals": [
        {{
            "signal_type": "rollback|disclosure_weakening|enforcement_weakening|exemption|simplification|esg_rollback|conflict_of_interest|penalty_reduction|sro_capture",
            "significance": "high|medium|low",
            "interpretation": "specific explanation referencing the rule text"
        }}
    ],
    "genuine_protections": [
        {{
            "description": "genuine investor/market protection this rule imposes",
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
    except Exception as e:
        console.print(f"  [red]Error: {e}[/red]")
        return None

    return {
        "docket_id": docket_id,
        "title": title,
        "pub_date": pub_date,
        "agency": "SEC",
        "score": analysis.get("industry_influence_score", 0),
        "confidence": analysis.get("confidence", "low"),
        "key_finding": analysis.get("key_finding", ""),
        "num_signals": len(analysis.get("capture_signals", [])),
        "high_signals": len([s for s in analysis.get("capture_signals", []) if s.get("significance") == "high"]),
    }


def update_graph(result):
    if not result:
        return
    with driver.session() as session:
        session.run("""
            MERGE (r:Rule {id: $docket_id})
            SET r.title = $title, r.agency = 'SEC',
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
            MERGE (o:Organization {id: 'FINANCE_INDUSTRY'})
            SET o.name = 'Financial Services Industry', o.sector = 'private'
            WITH o
            MATCH (r:Rule {id: $docket_id})
            MERGE (o)-[l:LOBBIED_ON]->(r)
            SET l.influence_score = $score, l.language_adopted = $high
        """,
            docket_id=result["docket_id"],
            score=float(result["score"]),
            high=result["score"] > 60
        )


def print_summary(progress):
    results = progress.get("results", [])
    if not results:
        return
    high = [r for r in results if r["score"] >= 60]
    med = [r for r in results if 30 <= r["score"] < 60]
    console.print(f"\n[bold]SEC Results: {len(results)} rules analyzed[/bold]")
    console.print(f"  🔴 High risk: {len(high)}  🟡 Medium: {len(med)}  🟢 Low: {len(results)-len(high)-len(med)}")
    if high:
        console.print(f"\n[bold red]High risk SEC rules:[/bold red]")
        for r in sorted(high, key=lambda x: -x["score"]):
            console.print(f"  {r['score']:>3}/100 | {r['pub_date']} | {r['title'][:65]}")
            if r.get("key_finding"):
                console.print(f"         → {r['key_finding'][:95]}")


def main():
    console.print("[bold]SEC Batch Rule Analyzer[/bold]\n")
    progress = load_progress()
    already_done = set(progress.get("analyzed", []))

    with open("data/raw/federal_register/sec_rules.json") as f:
        docs = json.load(f)

    targets = [
        d for d in docs
        if d.get("full_text_xml_url") and get_docket_id(d) not in already_done
    ]

    console.print(f"SEC rules to analyze: {len(targets)}")
    console.print("Progress saved after each rule — safe to stop and restart\n")

    if not targets:
        console.print("[green]All rules already analyzed![/green]")
        print_summary(progress)
        return

    for i, doc in enumerate(targets):
        title = doc.get("title", "")[:65]
        console.print(f"[{i+1}/{len(targets)}] {title}...")

        result = analyze_rule(doc)

        if result is None:
            console.print(f"  [yellow]Skipped[/yellow]")
            progress["analyzed"].append(get_docket_id(doc))
            save_progress(progress)
            continue

        score = result["score"]
        color = "red" if score > 60 else "yellow" if score > 30 else "green"
        console.print(f"  [{color}]{score}/100[/{color}] — {result['key_finding'][:95]}")

        update_graph(result)
        progress["analyzed"].append(get_docket_id(doc))
        progress["results"].append(result)
        save_progress(progress)
        time.sleep(0.3)

    console.print("\n[bold green]Done![/bold green]")
    print_summary(progress)
    console.print("\nRun scorer: python3 scoring/scorer.py")
    driver.close()


if __name__ == "__main__":
    main()
