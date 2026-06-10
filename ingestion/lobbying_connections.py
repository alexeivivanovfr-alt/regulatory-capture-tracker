"""
Lobbying Connection Builder

Fetches Senate LDA lobbying filings for industries connected to
our high-risk EPA rules and links them in the graph.

Targets:
- Oil & gas companies lobbying EPA on climate/methane rules
- Coal industry lobbying on mercury/coal ash rules  
- Chemical industry lobbying on TSCA/TCE rules
"""

import asyncio
import os
import json
from neo4j import GraphDatabase
from rich.console import Console
import httpx

console = Console()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "capturetracker")

LDA_API = "https://lda.senate.gov/api/v1"

# Industries and companies to search for
TARGETS = [
    # Oil & gas — connected to greenhouse gas and methane rules
    {"client": "ExxonMobil", "industry": "OIL_GAS_INDUSTRY", "agency_keyword": "EPA"},
    {"client": "Chevron", "industry": "OIL_GAS_INDUSTRY", "agency_keyword": "EPA"},
    {"client": "American Petroleum Institute", "industry": "OIL_GAS_INDUSTRY", "agency_keyword": "EPA"},
    {"client": "Koch Industries", "industry": "OIL_GAS_INDUSTRY", "agency_keyword": "EPA"},

    # Coal — connected to mercury/MATS and coal ash rules
    {"client": "Peabody Energy", "industry": "COAL_INDUSTRY", "agency_keyword": "EPA"},
    {"client": "Murray Energy", "industry": "COAL_INDUSTRY", "agency_keyword": "EPA"},
    {"client": "American Coal Council", "industry": "COAL_INDUSTRY", "agency_keyword": "EPA"},
    {"client": "Edison Electric Institute", "industry": "COAL_INDUSTRY", "agency_keyword": "EPA"},

    # Chemical — connected to TSCA/TCE/PFAS rules
    {"client": "Dow Chemical", "industry": "CHEMICAL_INDUSTRY", "agency_keyword": "EPA"},
    {"client": "American Chemistry Council", "industry": "CHEMICAL_INDUSTRY", "agency_keyword": "EPA"},
    {"client": "3M Company", "industry": "CHEMICAL_INDUSTRY", "agency_keyword": "EPA"},
    {"client": "DuPont", "industry": "CHEMICAL_INDUSTRY", "agency_keyword": "EPA"},
]

# Map our high-risk rule docket IDs to issue keywords
# So we can match lobbying filings to specific rules
RULE_KEYWORDS = {
    "EPA-HQ-OAR-2025-0124": ["greenhouse gas", "power plant", "fossil fuel"],
    "EPA-HQ-OAR-2025-0194": ["endangerment", "greenhouse gas", "climate"],
    "EPA-HQ-OAR-2019-0178": ["ethylene oxide", "hazardous air"],
    "EPA-HQ-OAR-2026-07396": ["mercury", "coal", "power plant", "MATS"],
    "EPA-HQ-OLEM-2025-0313": ["accidental release", "risk management", "chemical"],
    "EPA-HQ-OLEM-2020-0107": ["coal combustion", "coal ash", "CCR"],
    "EPA-HQ-OLEM-2026-07061": ["coal combustion", "coal ash", "CCR"],
    "EPA-HQ-OAR-2025-0162": ["methane", "oil and gas", "natural gas"],
    "EPA-HQ-OPPT-2021-0598": ["persistent", "bioaccumulative", "toxic", "TSCA"],
    "EPA-HQ-OPPT-2025-0260": ["risk evaluation", "TSCA", "chemical"],
    "EPA-HQ-OW-2025-0322": ["waters of the united states", "WOTUS", "clean water"],
    "FAA-2019-0001": ["aviation", "aircraft", "certification", "boeing"],
}


async def fetch_lobbying_filings(client_name: str) -> list:
    """Fetch LDA filings for a specific company."""
    async with httpx.AsyncClient(timeout=30) as http:
        try:
            resp = await http.get(
                f"{LDA_API}/filings/",
                params={
                    "client_name": client_name,
                    "filing_year": 2024
                },
                headers={"Accept": "application/json"}
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("results", [])
        except Exception as e:
            console.print(f"  [yellow]API error for {client_name}: {e}[/yellow]")
            return []


def extract_revolving_door(filing: dict) -> list:
    """Extract lobbyists with former government positions."""
    revolvers = []
    for activity in filing.get("lobbying_activities", []):
        for lobbyist in activity.get("lobbyists", []):
            position = lobbyist.get("covered_official_position", "")
            if position:
                revolvers.append({
                    "name": lobbyist.get("lobbyist_name", ""),
                    "former_position": position,
                    "client": filing.get("client", {}).get("client_name", ""),
                    "registrant": filing.get("registrant", {}).get("name", ""),
                    "year": filing.get("filing_year"),
                    "amount": filing.get("income", 0) or filing.get("expenses", 0) or 0
                })
    return revolvers


def match_filing_to_rules(filing: dict) -> list[str]:
    """
    Match a lobbying filing to specific rule docket IDs
    based on issue descriptions.
    """
    matched_rules = []

    # Get all issue text from this filing
    issue_text = " ".join([
        activity.get("general_issue_area_code", "") + " " +
        activity.get("description", "")
        for activity in filing.get("lobbying_activities", [])
    ]).lower()

    # Also check agencies contacted
    agencies = " ".join([
        activity.get("government_entities", [{}])[0].get("name", "")
        if activity.get("government_entities") else ""
        for activity in filing.get("lobbying_activities", [])
    ]).lower()

    combined_text = issue_text + " " + agencies

    for rule_id, keywords in RULE_KEYWORDS.items():
        if any(kw.lower() in combined_text for kw in keywords):
            matched_rules.append(rule_id)

    return matched_rules


def update_graph_with_lobbying(
    driver,
    filing: dict,
    industry_id: str,
    matched_rules: list[str],
    revolvers: list[dict]
):
    """Write lobbying connections to Neo4j."""
    client_name = filing.get("client", {}).get("client_name", "Unknown")
    registrant_name = filing.get("registrant", {}).get("name", "")
    amount = filing.get("income", 0) or filing.get("expenses", 0) or 0
    year = filing.get("filing_year", 0)
    filing_id = filing.get("filing_uuid", "")

    with driver.session() as session:
        # Create/update the company node
        session.run("""
            MERGE (o:Organization {id: $client_id})
            SET o.name = $client_name,
                o.sector = 'private',
                o.industry_group = $industry_id
        """,
            client_id=client_name.upper().replace(" ", "_"),
            client_name=client_name,
            industry_id=industry_id
        )

        # Link company to matched rules
        for rule_id in matched_rules:
            session.run("""
                MATCH (o:Organization {id: $client_id})
                MATCH (r:Rule {id: $rule_id})
                MERGE (o)-[l:LOBBIED_ON]->(r)
                SET l.amount = $amount,
                    l.year = $year,
                    l.filing_id = $filing_id,
                    l.registrant = $registrant,
                    l.language_adopted = false
            """,
                client_id=client_name.upper().replace(" ", "_"),
                rule_id=rule_id,
                amount=float(amount) if amount else 0.0,
                year=year,
                filing_id=filing_id,
                registrant=registrant_name
            )

        # Add revolving door persons
        for r in revolvers:
            person_id = r["name"].upper().replace(" ", "_").replace(",", "")
            session.run("""
                MERGE (p:Person {id: $person_id})
                SET p.name = $name,
                    p.former_government_role = $former_position,
                    p.now_lobbying_for = $client
            """,
                person_id=person_id,
                name=r["name"],
                former_position=r["former_position"],
                client=r["client"]
            )

            # Link person to the company they're lobbying for
            session.run("""
                MATCH (p:Person {id: $person_id})
                MATCH (o:Organization {id: $client_id})
                MERGE (p)-[e:EMPLOYED_AT]->(o)
                SET e.role = 'lobbyist',
                    e.sector_at_time = 'lobbying',
                    e.former_gov_role = $former_position
            """,
                person_id=person_id,
                client_id=client_name.upper().replace(" ", "_"),
                former_position=r["former_position"]
            )


async def main():
    console.print("[bold]Lobbying Connection Builder[/bold]")
    console.print("Fetching Senate LDA filings for key industries...\n")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    total_filings = 0
    total_matches = 0
    total_revolvers = 0
    all_revolvers = []

    for target in TARGETS:
        client_name = target["client"]
        console.print(f"Searching: [cyan]{client_name}[/cyan]...")

        filings = await fetch_lobbying_filings(client_name)
        console.print(f"  Found {len(filings)} filings")

        for filing in filings:
            # Find revolving door lobbyists
            revolvers = extract_revolving_door(filing)
            if revolvers:
                total_revolvers += len(revolvers)
                all_revolvers.extend(revolvers)

            # Match to specific rules
            matched = match_filing_to_rules(filing)

            if matched or revolvers:
                update_graph_with_lobbying(
                    driver,
                    filing,
                    target["industry"],
                    matched,
                    revolvers
                )
                if matched:
                    total_matches += len(matched)

        total_filings += len(filings)

    console.print(f"\n[bold]Results:[/bold]")
    console.print(f"  Total filings fetched: [cyan]{total_filings}[/cyan]")
    console.print(f"  Rule connections made: [cyan]{total_matches}[/cyan]")
    console.print(f"  Revolving door lobbyists found: [cyan]{total_revolvers}[/cyan]")

    if all_revolvers:
        console.print(f"\n[bold red]Revolving Door Lobbyists:[/bold red]")
        seen = set()
        for r in sorted(all_revolvers, key=lambda x: x.get("amount", 0), reverse=True):
            key = r["name"] + r["former_position"]
            if key in seen:
                continue
            seen.add(key)
            console.print(f"  [red]•[/red] {r['name']}")
            console.print(f"    Former: {r['former_position']}")
            console.print(f"    Now lobbying for: {r['client']}")
            if r.get("amount"):
                console.print(f"    Amount: ${r['amount']:,.0f}")

    console.print(f"\n[bold]Checking graph connections...[/bold]")
    with driver.session() as session:
        result = session.run("""
            MATCH (o:Organization)-[l:LOBBIED_ON]->(r:Rule)
            WHERE r.capture_score > 60
            RETURN o.name as company,
                   r.title as rule,
                   r.capture_score as score,
                   l.amount as amount,
                   l.year as year
            ORDER BY r.capture_score DESC
            LIMIT 20
        """).data()

        if result:
            console.print(f"\n[bold red]Industry-Rule Connections (High Risk):[/bold red]")
            for row in result:
                console.print(
                    f"  [red]{row['score']:.0f}/100[/red] | "
                    f"{str(row['company'])[:30]:<30} → "
                    f"{str(row['rule'])[:50]}"
                )
        else:
            console.print("  No high-risk connections found yet")

    console.print("\n[bold green]✓ Lobbying connections loaded![/bold green]")
    console.print("\nIn Neo4j browser try:")
    console.print("[dim]MATCH (o:Organization)-[l:LOBBIED_ON]->(r:Rule) WHERE r.capture_score > 60 RETURN o,l,r[/dim]")

    driver.close()


if __name__ == "__main__":
    asyncio.run(main())
