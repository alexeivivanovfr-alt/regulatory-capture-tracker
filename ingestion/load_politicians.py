"""
Load Politicians

Loads current members of key congressional committees into Neo4j.
Committee membership sourced from senate.gov and house.gov (119th Congress, 2025).

Then fetches FEC PAC donation data for each member from regulated industries.
"""

import json
import os
import subprocess
from pathlib import Path
from neo4j import GraphDatabase
from rich.console import Console

console = Console()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "capturetracker")
driver = GraphDatabase.driver(NEO4J_URI, auth=("neo4j", NEO4J_PASSWORD))

# Current committee members — 119th Congress (2025)
# Source: senate.gov/committees, house.gov/committees
COMMITTEES = {
    "ssev00": {
        "name": "Senate Environment & Public Works",
        "agency": "EPA",
        "members": [
            {"name": "Shelley Moore Capito", "bioguide": "C001047", "party": "R", "state": "WV", "role": "Chair"},
            {"name": "John Barrasso", "bioguide": "B001261", "party": "R", "state": "WY"},
            {"name": "James Inhofe", "bioguide": "I000024", "party": "R", "state": "OK"},
            {"name": "Roger Wicker", "bioguide": "W000437", "party": "R", "state": "MS"},
            {"name": "Dan Sullivan", "bioguide": "S001198", "party": "R", "state": "AK"},
            {"name": "Kevin Cramer", "bioguide": "C001096", "party": "R", "state": "ND"},
            {"name": "Pete Ricketts", "bioguide": "R000618", "party": "R", "state": "NE"},
            {"name": "Bernie Moreno", "bioguide": "M001224", "party": "R", "state": "OH"},
            {"name": "Adam Bergman", "bioguide": "B001317", "party": "R", "state": "ID"},
            {"name": "Tom Carper", "bioguide": "C000174", "party": "D", "state": "DE"},
            {"name": "Bernie Sanders", "bioguide": "S000033", "party": "I", "state": "VT"},
            {"name": "Jeff Merkley", "bioguide": "M001176", "party": "D", "state": "OR"},
            {"name": "Sheldon Whitehouse", "bioguide": "W000802", "party": "D", "state": "RI"},
            {"name": "Edward Markey", "bioguide": "M000133", "party": "D", "state": "MA"},
            {"name": "Chris Coons", "bioguide": "C001088", "party": "D", "state": "DE"},
            {"name": "Alex Padilla", "bioguide": "P000145", "party": "D", "state": "CA"},
        ]
    },
    "sscm00": {
        "name": "Senate Commerce, Science & Transportation",
        "agency": "FAA",
        "members": [
            {"name": "Ted Cruz", "bioguide": "C001098", "party": "R", "state": "TX", "role": "Chair"},
            {"name": "Roger Wicker", "bioguide": "W000437", "party": "R", "state": "MS"},
            {"name": "John Thune", "bioguide": "T000250", "party": "R", "state": "SD"},
            {"name": "Jerry Moran", "bioguide": "M000934", "party": "R", "state": "KS"},
            {"name": "Dan Sullivan", "bioguide": "S001198", "party": "R", "state": "AK"},
            {"name": "Marsha Blackburn", "bioguide": "B001243", "party": "R", "state": "TN"},
            {"name": "Todd Young", "bioguide": "Y000064", "party": "R", "state": "IN"},
            {"name": "Mike Lee", "bioguide": "L000577", "party": "R", "state": "UT"},
            {"name": "Rick Scott", "bioguide": "S001217", "party": "R", "state": "FL"},
            {"name": "Maria Cantwell", "bioguide": "C000127", "party": "D", "state": "WA", "role": "Ranking Member"},
            {"name": "Amy Klobuchar", "bioguide": "K000367", "party": "D", "state": "MN"},
            {"name": "Brian Schatz", "bioguide": "S001194", "party": "D", "state": "HI"},
            {"name": "Ed Markey", "bioguide": "M000133", "party": "D", "state": "MA"},
            {"name": "Gary Peters", "bioguide": "P000595", "party": "D", "state": "MI"},
            {"name": "Jacky Rosen", "bioguide": "R000608", "party": "D", "state": "NV"},
        ]
    },
    "sseg00": {
        "name": "Senate Energy & Natural Resources",
        "agency": "EPA",
        "members": [
            {"name": "Mike Lee", "bioguide": "L000577", "party": "R", "state": "UT", "role": "Chair"},
            {"name": "John Barrasso", "bioguide": "B001261", "party": "R", "state": "WY"},
            {"name": "James Risch", "bioguide": "R000584", "party": "R", "state": "ID"},
            {"name": "Steve Daines", "bioguide": "D000618", "party": "R", "state": "MT"},
            {"name": "Bill Cassidy", "bioguide": "C001075", "party": "R", "state": "LA"},
            {"name": "John Hoeven", "bioguide": "H001061", "party": "R", "state": "ND"},
            {"name": "Josh Hawley", "bioguide": "H001089", "party": "R", "state": "MO"},
            {"name": "Ted Budd", "bioguide": "B001305", "party": "R", "state": "NC"},
            {"name": "Joe Manchin", "bioguide": "M001183", "party": "D", "state": "WV", "role": "Ranking Member"},
            {"name": "Maria Cantwell", "bioguide": "C000127", "party": "D", "state": "WA"},
            {"name": "Martin Heinrich", "bioguide": "H001046", "party": "D", "state": "NM"},
            {"name": "Mazie Hirono", "bioguide": "H001042", "party": "D", "state": "HI"},
            {"name": "Angus King", "bioguide": "K000383", "party": "I", "state": "ME"},
            {"name": "John Hickenlooper", "bioguide": "H001077", "party": "D", "state": "CO"},
        ]
    }
}

# Industry PAC keywords to search for in FEC data
INDUSTRY_PACS = {
    "OIL_GAS_INDUSTRY": ["petroleum", "oil", "exxon", "chevron", "koch", "energy transfer", "conocophillips"],
    "COAL_INDUSTRY": ["coal", "peabody", "arch coal", "alpha natural", "consol energy"],
    "CHEMICAL_INDUSTRY": ["chemical", "dow ", "dupont", "3m ", "basf", "american chemistry"],
    "AEROSPACE_INDUSTRY": ["boeing", "lockheed", "raytheon", "aerospace industries", "general dynamics"],
}


def load_committees_and_politicians():
    """Load all committee members into Neo4j."""
    console.print("\n[bold]Loading committee members into Neo4j...[/bold]")
    total = 0

    with driver.session() as session:
        for committee_code, committee_data in COMMITTEES.items():
            # Create committee node
            session.run("""
                MERGE (c:Committee {id: $code})
                SET c.name = $name, c.agency = $agency
            """, code=committee_code, name=committee_data["name"], agency=committee_data["agency"])

            for m in committee_data["members"]:
                # Create politician node
                session.run("""
                    MERGE (p:Politician {id: $bioguide})
                    SET p.name = $name, p.party = $party,
                        p.state = $state, p.bioguide_id = $bioguide
                """, bioguide=m["bioguide"], name=m["name"],
                    party=m["party"], state=m["state"])

                # Link to committee
                session.run("""
                    MATCH (p:Politician {id: $bioguide})
                    MATCH (c:Committee {id: $code})
                    MERGE (p)-[r:SAT_ON]->(c)
                    SET r.role = $role
                """, bioguide=m["bioguide"], code=committee_code,
                    role=m.get("role", "Member"))

                total += 1

    console.print(f"[green]✓[/green] Loaded {total} committee assignments")
    return total


def fetch_fec_donations():
    """
    Fetch FEC PAC contributions to our committee members.
    Uses the FEC API to find industry PAC donations.
    """
    console.print("\n[bold]Fetching FEC PAC donations...[/bold]")

    # Get all unique bioguide IDs
    all_members = {}
    for committee_data in COMMITTEES.values():
        for m in committee_data["members"]:
            all_members[m["bioguide"]] = m["name"]

    console.print(f"Looking up FEC donations for {len(all_members)} politicians...")

    donation_dir = Path("data/fec_donations")
    donation_dir.mkdir(parents=True, exist_ok=True)

    total_donations = 0

    for bioguide, name in list(all_members.items())[:10]:  # Start with 10
        # Search FEC for this candidate
        search_name = name.split(",")[0].split(" ")[-1].upper()  # Last name
        cache_file = donation_dir / f"{bioguide}.json"

        if not cache_file.exists():
            url = f"https://api.openFEC.gov/v1/candidates/search/?api_key=DEMO_KEY&q={search_name}&office=S&per_page=5&sort=-receipts"
            result = subprocess.run(["curl", "-s", url], capture_output=True, text=True)
            try:
                data = json.loads(result.stdout)
                cache_file.write_text(json.dumps(data))
            except Exception:
                continue

        # Load and process
        try:
            data = json.loads(cache_file.read_text())
            results = data.get("results", [])
            for candidate in results:
                if name.split()[-1].upper() in candidate.get("name", "").upper():
                    candidate_id = candidate.get("candidate_id", "")
                    if candidate_id:
                        donations = fetch_pac_donations_for_candidate(candidate_id, name)
                        total_donations += donations
        except Exception:
            continue

    console.print(f"[green]✓[/green] Processed {total_donations} PAC donations")
    return total_donations


def fetch_pac_donations_for_candidate(candidate_id: str, politician_name: str) -> int:
    """Fetch industry PAC donations to a specific candidate."""
    donations_loaded = 0

    for industry_id, keywords in INDUSTRY_PACS.items():
        for keyword in keywords[:2]:  # Limit to avoid rate limits
            url = f"https://api.openFEC.gov/v1/schedules/schedule_b/?api_key=DEMO_KEY&recipient_id={candidate_id}&contributor_name={keyword}&per_page=20&two_year_transaction_period=2024"
            result = subprocess.run(["curl", "-s", url], capture_output=True, text=True)

            try:
                data = json.loads(result.stdout)
                donations = data.get("results", [])

                for donation in donations:
                    amount = donation.get("contribution_receipt_amount", 0)
                    contributor = donation.get("contributor_name", "")
                    date = donation.get("contribution_receipt_date", "")

                    if amount and amount > 0:
                        with driver.session() as session:
                            # Create industry org if not exists
                            session.run("""
                                MERGE (o:Organization {id: $industry_id})
                                SET o.sector = 'private'
                            """, industry_id=industry_id)

                            # Find politician and create donation relationship
                            session.run("""
                                MATCH (p:Politician) WHERE p.name CONTAINS $lastname
                                MATCH (o:Organization {id: $industry_id})
                                MERGE (o)-[d:DONATED_TO]->(p)
                                SET d.amount = coalesce(d.amount, 0) + $amount,
                                    d.contributor = $contributor,
                                    d.date = $date,
                                    d.cycle = '2024'
                            """,
                                lastname=politician_name.split()[-1],
                                industry_id=industry_id,
                                amount=float(amount),
                                contributor=contributor,
                                date=date
                            )
                            donations_loaded += 1
            except Exception:
                continue

    return donations_loaded


def link_politicians_to_rules():
    """
    Link politicians to high-risk rules via their committee oversight.
    Committee → Agency → Rules the committee oversees.
    """
    console.print("\n[bold]Linking politicians to rules via committee oversight...[/bold]")

    with driver.session() as session:
        # Senate EPW oversees EPA rules
        result = session.run("""
            MATCH (p:Politician)-[:SAT_ON]->(c:Committee {id: 'ssev00'})
            MATCH (r:Rule {agency: 'EPA'})
            WHERE r.capture_score >= 60
            MERGE (p)-[h:HAD_OVERSIGHT_OF]->(r)
            SET h.via_committee = c.name,
                h.type = 'committee_oversight'
            RETURN count(h) as n
        """).single()
        console.print(f"  EPA oversight links: [cyan]{result['n']}[/cyan]")

        # Senate Commerce + Energy oversees FAA rules
        result = session.run("""
            MATCH (p:Politician)-[:SAT_ON]->(c:Committee)
            WHERE c.id IN ['sscm00', 'sseg00']
            MATCH (r:Rule {agency: 'FAA'})
            WHERE r.capture_score >= 60
            MERGE (p)-[h:HAD_OVERSIGHT_OF]->(r)
            SET h.via_committee = c.name,
                h.type = 'committee_oversight'
            RETURN count(h) as n
        """).single()
        console.print(f"  FAA oversight links: [cyan]{result['n']}[/cyan]")


def print_summary():
    with driver.session() as session:
        politicians = session.run("MATCH (p:Politician) RETURN count(p) as n").single()["n"]
        committees = session.run("MATCH (c:Committee) RETURN count(c) as n").single()["n"]
        assignments = session.run("MATCH ()-[:SAT_ON]->() RETURN count(*) as n").single()["n"]
        donations = session.run("MATCH ()-[:DONATED_TO]->() RETURN count(*) as n").single()["n"]
        oversight = session.run(
            "MATCH (p:Politician)-[:HAD_OVERSIGHT_OF]->(r:Rule) RETURN count(*) as n"
        ).single()["n"]

    console.print(f"\n[bold]Graph Summary:[/bold]")
    console.print(f"  Politicians loaded: [cyan]{politicians}[/cyan]")
    console.print(f"  Committees: [cyan]{committees}[/cyan]")
    console.print(f"  Committee assignments: [cyan]{assignments}[/cyan]")
    console.print(f"  Industry donations: [cyan]{donations}[/cyan]")
    console.print(f"  Oversight connections: [cyan]{oversight}[/cyan]")

    # Show the capture chain
    console.print("\n[bold red]Sample capture chains:[/bold red]")
    results = session.run("""
        MATCH (o:Organization)-[:LOBBIED_ON]->(r:Rule)<-[:HAD_OVERSIGHT_OF]-(p:Politician)
        WHERE r.capture_score >= 60
        OPTIONAL MATCH (o2:Organization)-[:DONATED_TO]->(p)
        RETURN p.name as politician, p.party as party, p.state as state,
               r.title as rule, r.capture_score as score,
               o.name as lobbying_org,
               o2.name as donor_org
        ORDER BY r.capture_score DESC
        LIMIT 10
    """).data()

    for row in results:
        console.print(
            f"  [red]{row['score']:.0f}/100[/red] | "
            f"{row['politician']} ({row['party']}-{row['state']}) | "
            f"{str(row['rule'])[:40]}"
        )
        if row.get('donor_org'):
            console.print(f"    → Received donations from: {row['donor_org']}")


def main():
    console.print("[bold]Politician Pipeline[/bold]\n")

    load_committees_and_politicians()
    link_politicians_to_rules()
    fetch_fec_donations()
    print_summary()

    console.print("\n[bold green]✓ Done![/bold green]")
    console.print("\nIn Neo4j browser:")
    console.print("[dim]MATCH (o:Organization)-[:DONATED_TO]->(p:Politician)-[:HAD_OVERSIGHT_OF]->(r:Rule) WHERE r.capture_score > 60 RETURN o,p,r[/dim]")

    driver.close()


if __name__ == "__main__":
    main()
