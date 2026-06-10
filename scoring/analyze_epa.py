"""
EPA Capture Analysis

Analyzes high-priority EPA rules for regulatory capture signals.
Focuses on rules where industry capture is most likely:
- Coal/oil emissions standards
- Chemical safety rules
- Coal combustion residuals
"""

import json
import os
import re
import httpx
from neo4j import GraphDatabase
from rich.console import Console
import anthropic

console = Console()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "capturetracker")

client = anthropic.Anthropic()
driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

TARGET_RULES = [
    {
        "title": "Coal Combustion Residuals Disposal",
        "doc_number": "2026-07061",
        "docket_id": "EPA-HQ-OLEM-2026-07061",
        "url": "https://www.federalregister.gov/documents/full_text/xml/2026/04/13/2026-07061.xml",
        "why": "Coal ash disposal rules — coal industry has lobbied extensively to weaken disposal requirements that protect groundwater",
        "industry": "Coal industry, electric utilities (Duke Energy, Southern Company, etc.)"
    },
    {
        "title": "National Emission Standards for Hazardous Air Pollutants: Coal and Oil-Fired Power Plants",
        "doc_number": "2026-07396",
        "docket_id": "EPA-HQ-OAR-2026-07396",
        "url": "https://www.federalregister.gov/documents/full_text/xml/2026/04/16/2026-07396.xml",
        "why": "Hazardous air pollutant standards for power plants — energy industry historically lobbies for longer compliance timelines and weaker standards",
        "industry": "Coal and oil power plant operators, energy utilities"
    },
    {
        "title": "Reconsideration of Standards of Performance for New Stationary Sources",
        "doc_number": "2026-06808",
        "docket_id": "EPA-HQ-OAR-2026-06808",
        "url": "https://www.federalregister.gov/documents/full_text/xml/2026/04/09/2026-06808.xml",
        "why": "'Reconsideration' rules are a classic capture mechanism — industry requests reconsideration of rules it opposed, often resulting in weakened standards",
        "industry": "Oil & gas, manufacturing, industrial emitters"
    },
    {
        "title": "Accidental Release Prevention: Risk Management Program",
        "doc_number": "2026-06444",
        "docket_id": "EPA-HQ-OEM-2026-06444",
        "url": "https://www.federalregister.gov/documents/full_text/xml/2026/04/02/2026-06444.xml",
        "why": "Chemical accident prevention rules — chemical industry lobbies against third-party audits and public disclosure requirements",
        "industry": "Chemical manufacturers, oil refiners (ExxonMobil, Dow, BASF)"
    }
]


def fetch_rule_text(url: str, max_chars: int = 3000) -> str:
    """Fetch and clean rule text from Federal Register XML."""
    console.print(f"  Fetching rule text...")
    try:
        response = httpx.get(url, timeout=30, follow_redirects=True)
        response.raise_for_status()
        text = re.sub(r'<[^>]+>', ' ', response.text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:max_chars]
    except Exception as e:
        console.print(f"  [red]Failed: {e}[/red]")
        return ""


def analyze_rule(rule: dict) -> dict:
    """Run AI capture analysis on a single EPA rule."""
    console.print(f"\n[bold]Analyzing: {rule['title'][:65]}[/bold]")
    console.print(f"  Industry: {rule['industry'][:70]}")
    console.print(f"  Why: {rule['why'][:80]}")

    final_text = fetch_rule_text(rule["url"])
    if not final_text:
        return {"rule": rule, "score": 0, "error": "Could not fetch rule text"}

    console.print(f"  Got {len(final_text)} chars — running AI analysis...")

    prompt = f"""You are an expert regulatory analyst investigating regulatory capture at the US Environmental Protection Agency (EPA).

Analyze this EPA rule for signs of regulatory capture by the regulated industry.

RULE TITLE: {rule['title']}
LIKELY INDUSTRY INTEREST: {rule['industry']}
CONTEXT: {rule['why']}

RULE TEXT (excerpt):
{final_text}

Look for these specific capture signals in EPA rules:
1. Compliance deadlines that are unusually long or phased (industry wins time)
2. Self-monitoring and self-reporting instead of independent inspection
3. "Flexibility" provisions that let companies choose cheaper compliance paths
4. Exemptions for existing facilities vs new facilities (protecting incumbents)
5. Vague standards ("best available technology" without defining it)
6. Rollbacks or "reconsideration" of previously stronger requirements
7. Cost-benefit framing that discounts health benefits vs compliance costs
8. Language that mirrors industry lobbying positions

Be specific — quote actual passages when you find signals.
Be balanced — note genuine public health protections the rule does impose.

Respond ONLY with valid JSON:
{{
    "capture_signals": [
        {{
            "passage": "quoted text from rule",
            "signal_type": "long_timeline|self_reporting|flexibility|exemption|vague_standard|rollback|cost_benefit|industry_language",
            "significance": "high|medium|low",
            "interpretation": "why this is a capture signal and who benefits"
        }}
    ],
    "genuine_protections": [
        {{
            "description": "genuine public health protection this rule imposes",
            "significance": "high|medium|low"
        }}
    ],
    "industry_influence_score": <integer 0-100>,
    "confidence": "high|medium|low",
    "confidence_reason": "why you assigned this confidence level",
    "summary": "2-3 sentence plain English summary of findings",
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
    console.print(f"  Key finding: {analysis.get('key_finding', '')[:100]}")

    return {"rule": rule, "score": score, "analysis": analysis}


def update_graph(result: dict):
    """Write findings back to Neo4j."""
    if "error" in result:
        return

    rule = result["rule"]
    score = result["score"]
    analysis = result.get("analysis", {})

    with driver.session() as session:
        session.run("""
            MERGE (r:Rule {id: $docket_id})
            SET r.title = $title,
                r.agency = 'EPA',
                r.industry_language_score = $score,
                r.capture_score = $score,
                r.key_finding = $key_finding,
                r.analysis_summary = $summary,
                r.confidence = $confidence,
                r.doc_number = $doc_number
        """,
            docket_id=rule["docket_id"],
            title=rule["title"],
            score=float(score),
            key_finding=analysis.get("key_finding", ""),
            summary=analysis.get("summary", ""),
            confidence=analysis.get("confidence", "low"),
            doc_number=rule["doc_number"]
        )

        # Create industry org nodes and link them
        for industry_fragment in ["Coal", "Oil", "Chemical", "Energy"]:
            if industry_fragment.lower() in rule["industry"].lower():
                session.run("""
                    MERGE (o:Organization {id: $org_id})
                    SET o.name = $org_name, o.sector = 'private', o.industry = $industry
                    WITH o
                    MATCH (r:Rule {id: $rule_id})
                    MERGE (o)-[l:LOBBIED_ON]->(r)
                    SET l.influence_score = $score,
                        l.language_adopted = $high_capture
                """,
                    org_id=f"{industry_fragment.upper()}_INDUSTRY",
                    org_name=f"{industry_fragment} Industry",
                    industry=industry_fragment.lower(),
                    rule_id=rule["docket_id"],
                    score=float(score),
                    high_capture=score > 60
                )
                break

    console.print(f"  [green]✓[/green] Updated graph")


def print_report(results: list):
    """Print final analysis report."""
    console.print("\n" + "="*65)
    console.print("[bold]EPA REGULATORY CAPTURE ANALYSIS REPORT[/bold]")
    console.print("="*65)

    scored = sorted(
        [r for r in results if "score" in r and "error" not in r],
        key=lambda x: -x["score"]
    )

    for r in scored:
        score = r["score"]
        color = "red" if score > 60 else "yellow" if score > 30 else "green"
        analysis = r.get("analysis", {})

        console.print(f"\n[{color}]● Score {score}/100[/{color}] — {r['rule']['title'][:60]}")
        console.print(f"  {analysis.get('summary', '')}")

        high_signals = [s for s in analysis.get("capture_signals", []) if s.get("significance") == "high"]
        if high_signals:
            console.print(f"  [red]High-significance signals:[/red]")
            for s in high_signals[:2]:
                console.print(f"    • {s.get('interpretation', '')[:90]}")

        protections = analysis.get("genuine_protections", [])
        if protections:
            console.print(f"  [green]Genuine protections:[/green]")
            for p in protections[:1]:
                console.print(f"    ✓ {p.get('description', '')[:90]}")

    console.print("\n[dim]Run python3 scoring/scorer.py to update all scores[/dim]")


def main():
    console.print("[bold]EPA Significant Rules — Regulatory Capture Analysis[/bold]\n")

    results = []
    for rule in TARGET_RULES:
        result = analyze_rule(rule)
        results.append(result)
        update_graph(result)

    print_report(results)

    console.print("\n[bold green]✓ EPA analysis complete![/bold green]")
    driver.close()


if __name__ == "__main__":
    main()
