"""
Daily Monitor

Checks the Federal Register for new significant rules from tracked agencies.
Run daily via cron or manually.

Usage:
    python3 monitoring/daily_monitor.py

To schedule daily at 8am, add to crontab:
    0 8 * * * cd /path/to/regulatory_capture_tracker && source venv/bin/activate && python3 monitoring/daily_monitor.py >> data/monitor.log 2>&1
"""

import json
import os
import re
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path
from neo4j import GraphDatabase
from rich.console import Console
import anthropic

console = Console()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "capturetracker")
driver = GraphDatabase.driver(NEO4J_URI, auth=("neo4j", NEO4J_PASSWORD))
client = anthropic.Anthropic()

ALERTS_FILE = Path("data/alerts.json")
LOG_FILE = Path("data/monitor.log")

TRACKED_AGENCIES = {
    "federal-aviation-administration": "FAA",
    "environmental-protection-agency": "EPA",
    "food-and-drug-administration": "FDA",
    "federal-trade-commission": "FTC",
    "securities-and-exchange-commission": "SEC",
}

INDUSTRY_MAP = {
    "FAA": {"id": "AEROSPACE_INDUSTRY", "name": "Aerospace & Aviation Industry"},
    "EPA": {"id": "FOSSIL_FUEL_INDUSTRY", "name": "Fossil Fuel & Chemical Industry"},
    "FDA": {"id": "PHARMA_INDUSTRY", "name": "Pharmaceutical & Food Industry"},
    "FTC": {"id": "TECH_RETAIL_INDUSTRY", "name": "Technology & Retail Industry"},
    "SEC": {"id": "FINANCE_INDUSTRY", "name": "Financial Services Industry"},
}


def fetch_new_rules(days_back: int = 1) -> list[dict]:
    """Fetch rules published in the last N days from all tracked agencies."""
    since = (date.today() - timedelta(days=days_back)).isoformat()
    all_new = []

    for slug, agency_code in TRACKED_AGENCIES.items():
        # Use separate -d params to avoid URL encoding issues
        result = subprocess.run([
            "curl", "-s", "-G",
            "https://www.federalregister.gov/api/v1/documents.json",
            "--data-urlencode", f"conditions[agencies][]={slug}",
            "--data-urlencode", f"conditions[publication_date][gte]={since}",

            "--data-urlencode", "fields[]=document_number",
            "--data-urlencode", "fields[]=title",
            "--data-urlencode", "fields[]=type",
            "--data-urlencode", "fields[]=publication_date",
            "--data-urlencode", "fields[]=significant",
            "--data-urlencode", "fields[]=docket_ids",
            "--data-urlencode", "fields[]=citation",
            "--data-urlencode", "fields[]=full_text_xml_url",
            "--data-urlencode", "fields[]=significant",
            "--data-urlencode", "fields[]=type",
            "--data-urlencode", "per_page=50",
        ], capture_output=True, text=True)
        try:
            data = json.loads(result.stdout)
            docs = data.get("results", [])
            for doc in docs:
                doc["_agency_code"] = agency_code
            all_new.extend(docs)
            console.print(f"  {agency_code}: {len(docs)} new rules")
        except Exception as e:
            console.print(f"  [red]{agency_code}: fetch error — {e}[/red]")
            if result.stdout:
                console.print(f"  [dim]Response: {result.stdout[:100]}[/dim]")

    return all_new


def is_already_analyzed(docket_id: str) -> bool:
    """Check if this rule is already in Neo4j with a non-zero score."""
    with driver.session() as session:
        result = session.run("""
            MATCH (r:Rule {id: $id})
            WHERE r.capture_score > 0
            RETURN r.id
        """, id=docket_id).single()
    return result is not None


def get_docket_id(doc: dict) -> str:
    ids = doc.get("docket_ids", [])
    raw = ids[0] if ids else doc.get("document_number", "")
    return raw.replace("Docket No. ", "").strip()


def fetch_text(url: str, max_chars: int = 3000) -> str:
    try:
        # Ensure URL ends with .xml not .xm (API sometimes truncates)
        if url.endswith('.xm'):
            url = url + 'l'
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


def quick_score(title: str, agency: str, text: str) -> dict:
    """Run quick capture analysis on a new rule."""
    industry = INDUSTRY_MAP.get(agency, {}).get("name", "regulated industry")

    prompt = f"""You are a regulatory capture analyst. Quickly assess this new {agency} rule for capture risk.

TITLE: {title}
AGENCY: {agency}
INDUSTRY: {industry}

RULE TEXT:
{text}

Look for: rollbacks, withdrawals, industry-favorable exemptions, extended timelines, self-certification replacing independent oversight, cost-benefit framing underweighting public health.

Respond ONLY with valid JSON:
{{
    "industry_influence_score": <integer 0-100>,
    "confidence": "high|medium|low",
    "key_finding": "one sentence summary",
    "warrants_investigation": <true|false>
}}"""

    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception:
        return {"industry_influence_score": 0, "confidence": "low",
                "key_finding": "Could not analyze", "warrants_investigation": False}


def load_to_graph(doc: dict, score: dict):
    """Load new rule and score into Neo4j."""
    docket_id = get_docket_id(doc)
    agency = doc.get("_agency_code", "")
    industry = INDUSTRY_MAP.get(agency, {})

    with driver.session() as session:
        session.run("""
            MERGE (r:Rule {id: $id})
            SET r.title = $title,
                r.agency = $agency,
                r.doc_type = $doc_type,
                r.publication_date = $pub_date,
                r.significant = $significant,
                r.citation = $citation,
                r.capture_score = $score,
                r.industry_language_score = $score,
                r.key_finding = $key_finding,
                r.confidence = $confidence,
                r.new_alert = true
        """,
            id=docket_id,
            title=doc.get("title", "")[:200],
            agency=agency,
            doc_type=doc.get("type", ""),
            pub_date=doc.get("publication_date", ""),
            significant=bool(doc.get("significant", False)),
            citation=doc.get("citation", ""),
            score=float(score.get("industry_influence_score", 0)),
            key_finding=score.get("key_finding", ""),
            confidence=score.get("confidence", "low")
        )

        if industry:
            session.run("""
                MERGE (o:Organization {id: $org_id})
                SET o.name = $name, o.sector = 'private'
                WITH o
                MATCH (r:Rule {id: $rule_id})
                MERGE (o)-[l:LOBBIED_ON]->(r)
                SET l.influence_score = $score
            """,
                org_id=industry["id"],
                name=industry["name"],
                rule_id=docket_id,
                score=float(score.get("industry_influence_score", 0))
            )


def load_alerts() -> list:
    if ALERTS_FILE.exists():
        with open(ALERTS_FILE) as f:
            return json.load(f)
    return []


def save_alert(alert: dict):
    alerts = load_alerts()
    alerts.append(alert)
    ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERTS_FILE, "w") as f:
        json.dump(alerts, f, indent=2)


def print_report(new_rules: list, alerts: list):
    """Print a daily monitoring report."""
    today = date.today().isoformat()
    console.print(f"\n{'='*60}")
    console.print(f"[bold]Daily Monitoring Report — {today}[/bold]")
    console.print(f"{'='*60}")
    console.print(f"New rules fetched: {len(new_rules)}")
    console.print(f"Alerts generated: {len(alerts)}")

    if not alerts:
        console.print("\n[green]No high-risk rules detected today.[/green]")
        return

    high = [a for a in alerts if a["score"] >= 60]
    medium = [a for a in alerts if 30 <= a["score"] < 60]

    if high:
        console.print(f"\n[bold red]🚨 HIGH RISK ALERTS ({len(high)}):[/bold red]")
        for a in sorted(high, key=lambda x: -x["score"]):
            console.print(f"\n  Score: [red]{a['score']}/100[/red] | {a['agency']} | {a['date']}")
            console.print(f"  Rule: {a['title'][:70]}")
            console.print(f"  Finding: {a['key_finding'][:100]}")
            console.print(f"  Citation: {a.get('citation', '—')}")

    if medium:
        console.print(f"\n[bold yellow]⚠ MEDIUM RISK ({len(medium)}):[/bold yellow]")
        for a in sorted(medium, key=lambda x: -x["score"]):
            console.print(f"  {a['score']}/100 | {a['agency']} | {a['title'][:60]}")


def main():
    days_back = int(os.getenv("MONITOR_DAYS_BACK", "1"))
    console.print(f"[bold]Daily Monitor[/bold] — checking last {days_back} day(s)\n")

    # Fetch new rules
    console.print("Fetching new rules from Federal Register...")
    new_rules = fetch_new_rules(days_back=days_back)
    console.print(f"Total new rules: {len(new_rules)}")

    if not new_rules:
        console.print("[green]No new rules today.[/green]")
        return

    # Filter to significant rules with full text, not already analyzed
    to_analyze = []
    skipped_no_text = 0
    skipped_not_significant = 0
    for doc in new_rules:
        docket_id = get_docket_id(doc)
        if not docket_id:
            continue
        if not doc.get("full_text_xml_url"):
            skipped_no_text += 1
            continue
        # Only analyze Rules and Proposed Rules (not Notices)
        if doc.get("type") not in ["Rule", "Proposed Rule"]:
            skipped_not_significant += 1
            continue
        if is_already_analyzed(docket_id):
            console.print(f"  [dim]Already analyzed: {doc.get('title', '')[:50]}[/dim]")
            continue
        to_analyze.append(doc)

    console.print(f"  Skipped {skipped_not_significant} non-significant rules")
    console.print(f"  Skipped {skipped_no_text} rules with no full text")

    console.print(f"\nNew rules to analyze: {len(to_analyze)}")

    # Analyze each new rule
    today_alerts = []
    for doc in to_analyze:
        title = doc.get("title", "")[:60]
        agency = doc.get("_agency_code", "")
        console.print(f"\n  Analyzing [{agency}]: {title}...")

        url = doc.get("full_text_xml_url", "")
        text = fetch_text(url) if url else ""

        if not text:
            console.print(f"    [yellow]No text — URL: {url[:60]}[/yellow]")
            continue

        score = quick_score(title, agency, text)
        capture_score = score.get("industry_influence_score", 0)

        color = "red" if capture_score >= 60 else "yellow" if capture_score >= 30 else "green"
        console.print(f"    [{color}]{capture_score}/100[/{color}] — {score.get('key_finding', '')[:80]}")

        load_to_graph(doc, score)

        if capture_score >= 30 or score.get("warrants_investigation"):
            alert = {
                "date": doc.get("publication_date", ""),
                "agency": agency,
                "title": doc.get("title", ""),
                "score": capture_score,
                "key_finding": score.get("key_finding", ""),
                "citation": doc.get("citation", ""),
                "docket_id": get_docket_id(doc),
                "confidence": score.get("confidence", "low"),
            }
            today_alerts.append(alert)
            save_alert(alert)

        time.sleep(0.5)

    print_report(new_rules, today_alerts)

    console.print(f"\n[bold green]✓ Monitoring complete[/bold green]")
    console.print(f"Alerts saved to: {ALERTS_FILE}")
    console.print("\nTo run daily automatically, add to crontab:")
    console.print("[dim]0 8 * * * cd ~/Documents/regulatory_capture_tracker && source venv/bin/activate && ANTHROPIC_API_KEY=your_key python3 monitoring/daily_monitor.py >> data/monitor.log 2>&1[/dim]")

    driver.close()


if __name__ == "__main__":
    main()
