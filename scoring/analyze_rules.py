"""
Real Rule Analysis

Fetches actual rule text from the Federal Register and runs
language provenance analysis using the Claude API.

Focuses on significant FAA rules most likely to show capture.
"""

import json
import os
import re
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

# Target rules — most likely to show industry influence
TARGET_RULES = [
    {
        "title": "Safety Management Systems",
        "doc_number": "2024-08669",
        "docket_id": "FAA-2018-0696",
        "url": "https://www.federalregister.gov/documents/full_text/xml/2024/04/26/2024-08669.xml",
        "why": "Safety management systems directly relate to Boeing MCAS-era oversight failures",
        "industry": "Aerospace manufacturers (Boeing, Airbus, etc.)"
    },
    {
        "title": "Drug and Alcohol Testing of Certificated Repair Station Employees",
        "doc_number": "2024-30848",
        "docket_id": "FAA-2018-0696-alt",
        "url": "https://www.federalregister.gov/documents/full_text/xml/2024/12/27/2024-30848.xml",
        "why": "Repair stations are key Boeing supply chain — industry lobbied hard against testing requirements",
        "industry": "Aviation repair industry, Boeing supply chain"
    },
    {
        "title": "Pilot Professional Development",
        "doc_number": "2020-pilot-dev",
        "docket_id": "FAA-2010-0100",
        "url": "https://www.federalregister.gov/documents/full_text/xml/2020/08/12/2020-14297.xml",
        "why": "Pilot training requirements — airlines lobbied to minimize mandatory training hours",
        "industry": "Airlines (United, Delta, American)"
    }
]


def fetch_rule_text(url: str, max_chars: int = 6000) -> str:
    """Fetch and clean rule text from Federal Register XML."""
    console.print(f"  Fetching: {url[:70]}...")
    
    try:
        response = httpx.get(url, timeout=30, follow_redirects=True)
        response.raise_for_status()
        
        # Strip XML tags to get plain text
        text = response.text
        text = re.sub(r'<[^>]+>', ' ', text)  # Remove XML tags
        text = re.sub(r'\s+', ' ', text)       # Collapse whitespace
        text = text.strip()
        
        return text[:max_chars]
    
    except Exception as e:
        console.print(f"  [red]Failed to fetch: {e}[/red]")
        return ""


def analyze_rule(rule: dict) -> dict:
    """
    Analyze a single rule for regulatory capture signals.
    Since we don't have the proposed rule text or industry comments
    stored locally, we ask Claude to analyze the final rule text
    for general capture signals based on known industry positions.
    """
    console.print(f"\n[bold]Analyzing: {rule['title'][:60]}[/bold]")
    console.print(f"  Why interesting: {rule['why']}")
    
    final_text = fetch_rule_text(rule["url"])
    if not final_text:
        return {"rule": rule, "score": 0, "error": "Could not fetch rule text"}
    
    console.print(f"  Got {len(final_text)} chars of rule text")
    console.print(f"  Running AI analysis...")
    
    prompt = f"""You are an expert regulatory analyst investigating regulatory capture in US federal aviation rulemaking.

Analyze this final FAA rule for signs of regulatory capture by the aviation industry.

RULE TITLE: {rule['title']}
LIKELY INDUSTRY INTEREST: {rule['industry']}
CONTEXT: {rule['why']}

FINAL RULE TEXT (excerpt):
{final_text}

Look for these capture signals:
1. Vague or permissive language where specific requirements would be expected
2. Compliance timelines that are unusually long (giving industry time to adjust)
3. Self-reporting or self-certification provisions instead of independent oversight
4. Exemptions or carve-outs that benefit specific large operators
5. "Risk-based" language that gives regulators discretion to do less
6. Language that mirrors typical industry lobbying positions

Be specific — quote actual passages from the rule text when you find signals.
Be balanced — note where the rule does impose genuine requirements.

Respond ONLY with valid JSON:
{{
    "capture_signals": [
        {{
            "passage": "exact text from rule",
            "signal_type": "self_certification|vague_language|long_timeline|exemption|risk_based",
            "significance": "high|medium|low",
            "interpretation": "why this is a capture signal"
        }}
    ],
    "genuine_requirements": [
        {{
            "description": "what genuine requirement the rule does impose",
            "significance": "high|medium|low"
        }}
    ],
    "industry_influence_score": <integer 0-100>,
    "confidence": "high|medium|low",
    "summary": "2-3 sentence plain English summary",
    "key_finding": "single most important finding in one sentence"
}}"""

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    
    raw = response.content[0].text.replace("```json", "").replace("```", "").strip()
    
    try:
        analysis = json.loads(raw)
    except json.JSONDecodeError:
        console.print(f"  [red]Could not parse response[/red]")
        return {"rule": rule, "score": 0, "error": "Parse error"}
    
    score = analysis.get("industry_influence_score", 0)
    confidence = analysis.get("confidence", "low")
    
    color = "red" if score > 60 else "yellow" if score > 30 else "green"
    console.print(f"  Score: [{color}]{score}/100[/{color}] (confidence: {confidence})")
    console.print(f"  Key finding: {analysis.get('key_finding', '')}")
    
    return {"rule": rule, "score": score, "analysis": analysis}


def update_graph(result: dict):
    """Write analysis results back to Neo4j."""
    if "error" in result:
        return
    
    rule = result["rule"]
    score = result["score"]
    analysis = result.get("analysis", {})
    
    with driver.session() as session:
        # Try to find the rule by docket ID or create it
        session.run("""
            MERGE (r:Rule {id: $docket_id})
            SET r.title = $title,
                r.agency = 'FAA',
                r.industry_language_score = $score,
                r.capture_score = $score,
                r.key_finding = $key_finding,
                r.analysis_summary = $summary,
                r.confidence = $confidence
        """,
            docket_id=rule["docket_id"],
            title=rule["title"],
            score=float(score),
            key_finding=analysis.get("key_finding", ""),
            summary=analysis.get("summary", ""),
            confidence=analysis.get("confidence", "low")
        )
    
    console.print(f"  [green]✓[/green] Updated graph for {rule['docket_id']}")


def print_final_report(results: list):
    """Print a summary report of all analyzed rules."""
    console.print("\n" + "="*60)
    console.print("[bold]ANALYSIS REPORT — FAA Significant Rules[/bold]")
    console.print("="*60)
    
    scored = [r for r in results if "score" in r and "error" not in r]
    scored.sort(key=lambda x: -x["score"])
    
    for r in scored:
        score = r["score"]
        color = "red" if score > 60 else "yellow" if score > 30 else "green"
        analysis = r.get("analysis", {})
        
        console.print(f"\n[{color}]Score {score}/100[/{color}] — {r['rule']['title'][:55]}")
        console.print(f"  {analysis.get('summary', '')}")
        
        signals = analysis.get("capture_signals", [])
        high_signals = [s for s in signals if s.get("significance") == "high"]
        if high_signals:
            console.print(f"  [red]High significance signals:[/red]")
            for s in high_signals[:2]:
                console.print(f"    • {s.get('interpretation', '')[:80]}")


def main():
    console.print("[bold]FAA Significant Rules — Capture Analysis[/bold]\n")
    
    results = []
    for rule in TARGET_RULES:
        result = analyze_rule(rule)
        results.append(result)
        update_graph(result)
    
    print_final_report(results)
    
    console.print("\n[bold green]✓ Analysis complete![/bold green]")
    console.print("\nRun the scorer again to update all scores:")
    console.print("[dim]python3 scoring/scorer.py[/dim]")
    
    driver.close()


if __name__ == "__main__":
    main()
