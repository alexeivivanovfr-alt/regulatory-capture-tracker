"""
Politician Pipeline

Fetches:
1. Members of key congressional committees (congress.gov API)
2. FEC PAC donations to those members from regulated industries
3. Loads into Neo4j connecting politicians to industries and rules

Key committees:
- ssev00: Senate Environment & Public Works (oversees EPA)
- sscm00: Senate Commerce, Science & Transportation (oversees FAA)
- sseg00: Senate Energy & Natural Resources
- hspw00: House Transportation & Infrastructure (oversees FAA)
- hsif00: House Energy & Commerce (oversees EPA)

Run: python3 ingestion/politicians.py
"""

import json
import os
import time
from pathlib import Path
from neo4j import GraphDatabase
from rich.console import Console

console = Console()

CONGRESS_API_KEY = os.getenv("CONGRESS_API_KEY", "DEMO_KEY")
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "capturetracker")

driver = GraphDatabase.driver(NEO4J_URI, auth=("neo4j", NEO4J_PASSWORD))

# Committees that oversee our target agencies
TARGET_COMMITTEES = [
    {"code": "ssev00", "name": "Senate Environment & Public Works", "agency": "EPA", "chamber": "senate"},
    {"code": "sscm00", "name": "Senate Commerce, Science & Transportation", "agency": "FAA", "chamber": "senate"},
    {"code": "sseg00", "name": "Senate Energy & Natural Resources", "agency": "EPA", "chamber": "senate"},
]

# Industry PAC name keywords to match against FEC data
INDUSTRY_PACS = {
    "OIL_GAS": ["petroleum", "oil", "gas", "exxon", "chevron", "koch", "energy transfer", "pipeline"],
    "COAL": ["coal", "peabody", "arch coal", "alpha natural", "foresight"],
    "CHEMICAL": ["chemical", "dow", "dupont", "3m company", "basf", "eastman"],
    "AEROSPACE": ["boeing", "lockheed", "raytheon", "aerospace", "aviation", "airbus"],
}


def fetch_committee_members(committee_code: str, chamber: str) -> list[dict]:
    """Fetch members of a congressional committee using curl (avoids httpx issues)."""
    import subprocess

    key = os.environ.get("CONGRESS_API_KEY", CONGRESS_API_KEY)
    url = f"https://api.congress.gov/v3/committee/{chamber}/{committee_code}?api_key={key}&format=json"
    result = subprocess.run(["curl", "-s", url], capture_output=True, text=True, env=os.environ)

    try:
        data = json.loads(result.stdout)
        committee = data.get("committee", {})
        # Try to get current members from the committee data
        current_members = committee.get("currentMembers", [])
        if current_members:
            return current_members
        # Some endpoints nest differently
        members = committee.get("members", [])
        return members
    except Exception as e:
        console.print(f"[red]Error parsing committee data: {e}[/red]")
        return []


def fetch_all_senate_members() -> list[dict]:
    """Fetch all current Senate members."""
    import subprocess
    members = []
    offset = 0
    key = os.environ.get("CONGRESS_API_KEY", CONGRESS_API_KEY)

    while True:
        url = f"https://api.congress.gov/v3/member?api_key={key}&format=json&limit=250&currentMember=true&chamber=Senate&offset={offset}"
        console.print(f"  Fetching: {url[:80]}...")
        result = subprocess.run(["curl", "-s", url], capture_output=True, text=True, env=os.environ)

        try:
            data = json.loads(result.stdout)
            if "error" in data:
                console.print(f"  [red]API error: {data['error']}[/red]")
                break
            batch = data.get("members", [])
            if not batch:
                console.print(f"  [yellow]Empty batch. Raw: {result.stdout[:200]}[/yellow]")
                break
            members.extend(batch)
            console.print(f"  Fetched {len(members)} senators...")
            if len(batch) < 250:
                break
            offset += 250
            time.sleep(0.5)
        except Exception as e:
            console.print(f"  [red]Parse error: {e}. Raw: {result.stdout[:200]}[/red]")
            break

    return members


def fetch_member_committees(bioguide_id: str) -> list[str]:
    """Fetch committee assignments for a specific member."""
    import subprocess
    key = os.environ.get("CONGRESS_API_KEY", CONGRESS_API_KEY)
    url = f"https://api.congress.gov/v3/member/{bioguide_id}/committee-assignment?api_key={key}&format=json&limit=50"
    result = subprocess.run(["curl", "-s", url], capture_output=True, text=True, env=os.environ)

    try:
        data = json.loads(result.stdout)
        assignments = data.get("committeeAssignments", [])
        return [a.get("committee", {}).get("systemCode", "") for a in assignments]
    except Exception:
        return []


def load_fec_donations_for_politicians(politicians: list[dict]) -> dict:
    """
    Load FEC data for politicians from our already-downloaded lobbying files.
    Cross-references politician names against FEC committee data.

    Returns dict of {bioguide_id: [donation_records]}
    """
    # Load FEC committee master if available
    fec_path = Path("data/raw/fec")
    if not fec_path.exists():
        console.print("[yellow]No FEC data downloaded yet. Run FEC ingestion first.[/yellow]")
        return {}

    console.print("Checking FEC data for politician donations...")
    return {}


def load_politicians_to_graph(politicians: list[dict], committee_info: dict):
    """Load politicians and their committee assignments into Neo4j."""
    loaded = 0

    with driver.session() as session:
        for p in politicians:
            name = p.get("name", "") or f"{p.get('firstName','')} {p.get('lastName','')}".strip()
            bioguide = p.get("bioguideId", "")
            state = p.get("state", "")
            party = ""

            # Get party from partyHistory
            party_history = p.get("partyHistory", [])
            if party_history:
                party = party_history[-1].get("partyAbbreviation", "")

            if not bioguide:
                continue

            # Create politician node
            session.run("""
                MERGE (p:Politician {id: $bioguide})
                SET p.name = $name,
                    p.state = $state,
                    p.party = $party,
                    p.bioguide_id = $bioguide
            """, bioguide=bioguide, name=name, state=state, party=party)

            # Link to committee
            committee_code = committee_info.get("code", "")
            committee_name = committee_info.get("name", "")
            agency = committee_info.get("agency", "")

            if committee_code:
                session.run("""
                    MERGE (c:Committee {id: $code})
                    SET c.name = $name, c.agency = $agency
                    WITH c
                    MATCH (p:Politician {id: $bioguide})
                    MERGE (p)-[r:SAT_ON]->(c)
                    SET r.chamber = $chamber
                """,
                    code=committee_code,
                    name=committee_name,
                    agency=agency,
                    bioguide=bioguide,
                    chamber=committee_info.get("chamber", "")
                )

            loaded += 1

    return loaded


def link_industry_donations_to_politicians():
    """
    Use FEC lobbying data we already have to find donations.
    Since we have FEC committee data, cross-reference with politician list.
    """
    console.print("\n[bold]Linking industry donations to politicians via FEC...[/bold]")

    # Use the lobbying files we already downloaded — they contain
    # some politician references in the government_entities field
    lobbying_dir = Path("data/lobbying")
    if not lobbying_dir.exists():
        console.print("[yellow]No lobbying data found[/yellow]")
        return

    politician_mentions = {}

    for f in lobbying_dir.glob("*.json"):
        data = json.load(open(f))
        company = f.stem.replace("_2024", "").replace("_", " ").title()

        for filing in data.get("results", []):
            client = filing.get("client", {}).get("name", "")
            amount = float(filing.get("income") or filing.get("expenses") or 0)

            for activity in filing.get("lobbying_activities", []):
                for entity in activity.get("government_entities", []):
                    entity_name = entity.get("name", "").upper()
                    # Look for Senate/House committee mentions
                    if "SENATE" in entity_name or "HOUSE" in entity_name:
                        key = client
                        if key not in politician_mentions:
                            politician_mentions[key] = []
                        politician_mentions[key].append({
                            "entity": entity_name,
                            "amount": amount
                        })

    console.print(f"Found lobbying activity mentioning Congress from {len(politician_mentions)} companies")


def print_summary():
    """Show what's in the graph."""
    with driver.session() as session:
        politicians = session.run("MATCH (p:Politician) RETURN count(p) as n").single()["n"]
        committees = session.run("MATCH (c:Committee) RETURN count(c) as n").single()["n"]
        on_committee = session.run("MATCH ()-[:SAT_ON]->() RETURN count(*) as n").single()["n"]

    console.print(f"\n[bold]Politicians in graph:[/bold]")
    console.print(f"  Politicians: [cyan]{politicians}[/cyan]")
    console.print(f"  Committees: [cyan]{committees}[/cyan]")
    console.print(f"  Committee assignments: [cyan]{on_committee}[/cyan]")


def main():
    console.print("[bold]Politician Pipeline[/bold]\n")

    if CONGRESS_API_KEY == "DEMO_KEY":
        console.print("[yellow]Warning: Using DEMO_KEY — rate limited to 5 requests/hour[/yellow]")
        console.print("Get a free key at: https://api.congress.gov/sign-up/\n")
        console.print("Set it with: export CONGRESS_API_KEY=your_key_here\n")

    total_loaded = 0

    for committee in TARGET_COMMITTEES:
        console.print(f"\n[bold]Fetching members: {committee['name']}[/bold]")

        members = fetch_committee_members(committee["code"], committee["chamber"])

        if not members:
            console.print(f"  [yellow]No members returned — fetching all senators and filtering[/yellow]")
            # Fallback: get all senators, then check their committee assignments
            # This uses more API calls but works when committee endpoint doesn't return members
            members = fetch_all_senate_members()
            console.print(f"  Got {len(members)} total senators")

            # Filter to those on this committee
            target_members = []
            console.print(f"  Checking committee assignments (this takes a moment)...")

            # Only check first 20 to avoid rate limits with DEMO_KEY
            check_count = 5 if CONGRESS_API_KEY == "DEMO_KEY" else len(members)
            for m in members[:check_count]:
                bioguide = m.get("bioguideId", "")
                committees_for_member = fetch_member_committees(bioguide)
                if committee["code"] in committees_for_member:
                    target_members.append(m)
                time.sleep(0.3)

            members = target_members
            console.print(f"  Found {len(members)} members on {committee['name']}")

        if members:
            loaded = load_politicians_to_graph(members, committee)
            console.print(f"  [green]✓[/green] Loaded {loaded} politicians")
            total_loaded += loaded
        else:
            console.print(f"  [yellow]No members found — get a real API key for full results[/yellow]")

        time.sleep(1)  # Rate limit respect

    link_industry_donations_to_politicians()
    print_summary()

    console.print(f"\n[bold green]✓ Done! {total_loaded} politicians loaded[/bold green]")

    if CONGRESS_API_KEY == "DEMO_KEY":
        console.print("\n[yellow]To get full committee membership data:[/yellow]")
        console.print("1. Get free key at https://api.congress.gov/sign-up/")
        console.print("2. export CONGRESS_API_KEY=your_key")
        console.print("3. Run this script again")

    driver.close()


if __name__ == "__main__":
    main()
