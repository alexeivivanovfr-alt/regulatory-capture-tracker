"""
Process Lobbying Data

Reads curl-fetched lobbying JSON files and loads connections into Neo4j.
Run fetch_lobbying.sh first to download the data.
"""

import json
import os
from pathlib import Path
from neo4j import GraphDatabase
from rich.console import Console

console = Console()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "capturetracker")

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

# Map company names to industry groups
INDUSTRY_MAP = {
    "exxonmobil": "OIL_GAS_INDUSTRY",
    "chevron": "OIL_GAS_INDUSTRY",
    "american_petroleum_institute": "OIL_GAS_INDUSTRY",
    "koch_industries": "OIL_GAS_INDUSTRY",
    "peabody_energy": "COAL_INDUSTRY",
    "american_coal_council": "COAL_INDUSTRY",
    "edison_electric_institute": "COAL_INDUSTRY",
    "dow_chemical": "CHEMICAL_INDUSTRY",
    "american_chemistry_council": "CHEMICAL_INDUSTRY",
    "3m_company": "CHEMICAL_INDUSTRY",
    "dupont": "CHEMICAL_INDUSTRY",
    "boeing": "AEROSPACE_INDUSTRY",
}

# Keywords to match filings to specific rules
RULE_KEYWORDS = {
    "EPA-HQ-OAR-2025-0124": ["greenhouse gas", "power plant", "fossil fuel", "GHG"],
    "EPA-HQ-OAR-2025-0194": ["endangerment", "greenhouse gas", "climate"],
    "EPA-HQ-OAR-2019-0178": ["ethylene oxide", "hazardous air", "EtO"],
    "EPA-HQ-OAR-2026-07396": ["mercury", "MATS", "coal", "power plant"],
    "EPA-HQ-OLEM-2025-0313": ["accidental release", "risk management", "RMP"],
    "EPA-HQ-OLEM-2020-0107": ["coal combustion", "coal ash", "CCR"],
    "EPA-HQ-OLEM-2026-07061": ["coal combustion", "coal ash", "CCR"],
    "EPA-HQ-OAR-2025-0162": ["methane", "oil and gas", "natural gas", "VOC"],
    "EPA-HQ-OPPT-2021-0598": ["persistent", "bioaccumulative", "TSCA", "PBT"],
    "EPA-HQ-OPPT-2025-0260": ["risk evaluation", "TSCA", "chemical risk"],
    "EPA-HQ-OW-2025-0322": ["waters of the united states", "WOTUS", "clean water"],
    "FAA-2019-0001": ["aviation", "aircraft", "certification", "FAA"],
}


def match_to_rules(filing: dict) -> list[str]:
    """Match a filing to rule docket IDs based on issue text."""
    issue_text = ""
    for activity in filing.get("lobbying_activities", []):
        issue_text += " " + activity.get("general_issue_area_code", "")
        issue_text += " " + activity.get("description", "")
        for entity in activity.get("government_entities", []):
            issue_text += " " + entity.get("name", "")

    issue_text = issue_text.lower()
    matched = []
    for rule_id, keywords in RULE_KEYWORDS.items():
        if any(kw.lower() in issue_text for kw in keywords):
            matched.append(rule_id)
    return matched


def extract_revolvers(filing: dict) -> list:
    """Extract lobbyists with former government positions."""
    revolvers = []
    for activity in filing.get("lobbying_activities", []):
        for lobbyist in activity.get("lobbyists", []):
            position = lobbyist.get("covered_official_position", "")
            if position and position.strip():
                revolvers.append({
                    "name": lobbyist.get("lobbyist_name", ""),
                    "former_position": position.strip(),
                    "client": filing.get("client", {}).get("client_name", ""),
                    "year": filing.get("filing_year"),
                    "amount": float(filing.get("income") or filing.get("expenses") or 0)
                })
    return revolvers


def load_to_graph(company_key: str, filings: list):
    """Load a company's filings into Neo4j."""
    industry_id = INDUSTRY_MAP.get(company_key, "REGULATED_INDUSTRY")
    total_connections = 0
    total_revolvers = 0

    with driver.session() as session:
        for filing in filings:
            client_name = filing.get("client", {}).get("client_name", "Unknown")
            registrant = filing.get("registrant", {}).get("name", "")
            amount = float(filing.get("income") or filing.get("expenses") or 0)
            year = filing.get("filing_year", 0)
            filing_id = filing.get("filing_uuid", "")

            # Create company node
            client_id = client_name.upper().replace(" ", "_").replace(",", "").replace(".", "")
            session.run("""
                MERGE (o:Organization {id: $id})
                SET o.name = $name,
                    o.sector = 'private',
                    o.industry_group = $industry
            """, id=client_id, name=client_name, industry=industry_id)

            # Match to rules and create relationships
            matched_rules = match_to_rules(filing)
            for rule_id in matched_rules:
                result = session.run("""
                    MATCH (r:Rule {id: $rule_id})
                    RETURN r.id as id
                """, rule_id=rule_id).data()

                if result:
                    session.run("""
                        MATCH (o:Organization {id: $client_id})
                        MATCH (r:Rule {id: $rule_id})
                        MERGE (o)-[l:LOBBIED_ON]->(r)
                        SET l.amount = $amount,
                            l.year = $year,
                            l.filing_id = $filing_id,
                            l.registrant = $registrant
                    """,
                        client_id=client_id,
                        rule_id=rule_id,
                        amount=amount,
                        year=year,
                        filing_id=filing_id,
                        registrant=registrant
                    )
                    total_connections += 1

            # Add revolving door lobbyists
            revolvers = extract_revolvers(filing)
            for r in revolvers:
                person_id = r["name"].upper().replace(" ", "_").replace(",", "").replace(".", "")
                if not person_id:
                    continue
                session.run("""
                    MERGE (p:Person {id: $person_id})
                    SET p.name = $name,
                        p.former_government_role = $position,
                        p.now_lobbying_for = $client
                """,
                    person_id=person_id,
                    name=r["name"],
                    position=r["former_position"],
                    client=r["client"]
                )
                session.run("""
                    MATCH (p:Person {id: $person_id})
                    MATCH (o:Organization {id: $client_id})
                    MERGE (p)-[e:EMPLOYED_AT]->(o)
                    SET e.role = 'lobbyist',
                        e.sector_at_time = 'lobbying',
                        e.former_gov_role = $position
                """,
                    person_id=person_id,
                    client_id=client_id,
                    position=r["former_position"]
                )
                total_revolvers += 1

    return total_connections, total_revolvers


def print_graph_summary():
    """Show what's now in the graph."""
    console.print("\n[bold]Graph Summary After Lobbying Load:[/bold]")

    with driver.session() as session:
        results = session.run("""
            MATCH (o:Organization)-[l:LOBBIED_ON]->(r:Rule)
            WHERE r.capture_score > 60
            RETURN o.name as company,
                   r.title as rule,
                   r.capture_score as score
            ORDER BY r.capture_score DESC
            LIMIT 20
        """).data()

        if results:
            console.print("\n[bold red]Industry → High Risk Rule Connections:[/bold red]")
            for row in results:
                console.print(
                    f"  [red]{row['score']:.0f}/100[/red] | "
                    f"{str(row['company'])[:28]:<28} → "
                    f"{str(row['rule'])[:50]}"
                )
        else:
            console.print("  No high-risk connections found")

        revolvers = session.run("""
            MATCH (p:Person)-[:EMPLOYED_AT]->(o:Organization)
            WHERE p.former_government_role IS NOT NULL
            RETURN p.name as name,
                   p.former_government_role as former_role,
                   o.name as now_at
            LIMIT 20
        """).data()

        if revolvers:
            console.print(f"\n[bold red]Revolving Door Lobbyists:[/bold red]")
            for r in revolvers:
                console.print(f"  [red]•[/red] {r['name']}")
                console.print(f"    Former: {r['former_role'][:70]}")
                console.print(f"    Now at: {r['now_at']}")


def main():
    console.print("[bold]Processing Lobbying Data[/bold]\n")

    lobbying_dir = Path("data/lobbying")
    if not lobbying_dir.exists():
        console.print("[red]No lobbying data found![/red]")
        console.print("Run this first: bash ingestion/fetch_lobbying.sh")
        return

    json_files = list(lobbying_dir.glob("*.json"))
    console.print(f"Found {len(json_files)} lobbying data files\n")

    total_filings = 0
    total_connections = 0
    total_revolvers = 0

    for json_file in sorted(json_files):
        company_key = json_file.stem.replace("_2024", "")
        console.print(f"Processing [cyan]{company_key}[/cyan]...")

        with open(json_file) as f:
            data = json.load(f)

        filings = data.get("results", [])
        if not filings:
            console.print(f"  No filings found")
            continue

        console.print(f"  {len(filings)} filings")
        connections, revolvers = load_to_graph(company_key, filings)
        console.print(f"  → {connections} rule connections, {revolvers} revolving door lobbyists")

        total_filings += len(filings)
        total_connections += connections
        total_revolvers += revolvers

    console.print(f"\n[bold]Totals:[/bold]")
    console.print(f"  Filings processed: [cyan]{total_filings}[/cyan]")
    console.print(f"  Rule connections:  [cyan]{total_connections}[/cyan]")
    console.print(f"  Revolving door:    [cyan]{total_revolvers}[/cyan]")

    print_graph_summary()

    console.print("\n[bold green]✓ Done![/bold green]")
    console.print("\nIn Neo4j browser:")
    console.print("[dim]MATCH (o:Organization)-[l:LOBBIED_ON]->(r:Rule) WHERE r.capture_score > 60 RETURN o,l,r[/dim]")

    driver.close()


if __name__ == "__main__":
    main()
