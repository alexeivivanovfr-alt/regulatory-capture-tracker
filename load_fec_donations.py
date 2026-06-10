"""
Load FEC PAC donations to committee members into Neo4j.
Uses bulk FEC data files already downloaded.
"""

import zipfile
import json
from pathlib import Path
from neo4j import GraphDatabase
from rich.console import Console

console = Console()
driver = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "capturetracker"))

# Committee master columns
CM_COLS = ["CMTE_ID","CMTE_NM","TRES_NM","CMTE_ST1","CMTE_ST2","CMTE_CITY",
           "CMTE_ST","CMTE_ZIP","CMTE_DSGN","CMTE_TP","CMTE_PTY_AFFILIATION",
           "CMTE_FILING_FREQ","ORG_TP","CONNECTED_ORG_NM","CAND_ID"]

# PAC-to-candidate columns
PAS2_COLS = ["CMTE_ID","AMNDT_IND","RPT_TP","TRANSACTION_PGI","IMAGE_NUM",
             "TRANSACTION_TP","ENTITY_TP","NAME","CITY","STATE","ZIP_CODE",
             "EMPLOYER","OCCUPATION","TRANSACTION_DT","TRANSACTION_AMT",
             "OTHER_ID","CAND_ID","TRAN_ID","FILE_NUM","MEMO_CD","MEMO_TEXT","SUB_ID"]

# Our politicians by last name for matching
POLITICIANS = {
    "CAPITO": {"name": "Shelley Moore Capito", "id": "C001047"},
    "BARRASSO": {"name": "John Barrasso", "id": "B001261"},
    "WICKER": {"name": "Roger Wicker", "id": "W000437"},
    "SULLIVAN": {"name": "Dan Sullivan", "id": "S001198"},
    "CRAMER": {"name": "Kevin Cramer", "id": "C001096"},
    "CRUZ": {"name": "Ted Cruz", "id": "C001098"},
    "THUNE": {"name": "John Thune", "id": "T000250"},
    "MORAN": {"name": "Jerry Moran", "id": "M000934"},
    "BLACKBURN": {"name": "Marsha Blackburn", "id": "B001243"},
    "LEE": {"name": "Mike Lee", "id": "L000577"},
    "SCOTT": {"name": "Rick Scott", "id": "S001217"},
    "CANTWELL": {"name": "Maria Cantwell", "id": "C000127"},
    "KLOBUCHAR": {"name": "Amy Klobuchar", "id": "K000367"},
    "MARKEY": {"name": "Ed Markey", "id": "M000133"},
    "PETERS": {"name": "Gary Peters", "id": "P000595"},
    "WHITEHOUSE": {"name": "Sheldon Whitehouse", "id": "W000802"},
    "MERKLEY": {"name": "Jeff Merkley", "id": "M001176"},
    "MANCHIN": {"name": "Joe Manchin", "id": "M001183"},
    "HEINRICH": {"name": "Martin Heinrich", "id": "H001046"},
    "HICKENLOOPER": {"name": "John Hickenlooper", "id": "H001077"},
}

# Industry PAC keywords
INDUSTRY_PACS = {
    "OIL_GAS_INDUSTRY": ["PETROLEUM","EXXON","CHEVRON","CONOCOPHILLIPS","SHELL","BP ",
                          "AMERICAN PETROLEUM","KOCH","ENERGY TRANSFER","PIONEER NAT"],
    "COAL_INDUSTRY": ["COAL","PEABODY","ARCH COAL","ALPHA NATURAL","CONSOL","FORESIGHT"],
    "CHEMICAL_INDUSTRY": ["CHEMICAL","DOW ","DUPONT","3M CO","BASF","EASTMAN CHEM",
                           "AMERICAN CHEMISTRY","WESTLAKE"],
    "AEROSPACE_INDUSTRY": ["BOEING","LOCKHEED","RAYTHEON","GENERAL DYNAMICS",
                            "NORTHROP","AEROSPACE IND"],
}


def load_committee_master():
    """Load FEC committee master — maps committee IDs to org names."""
    console.print("Loading FEC committee master...")
    committees = {}

    with zipfile.ZipFile("data/raw/fec/cm24.zip") as zf:
        fname = [f for f in zf.namelist() if f.endswith(".txt")][0]
        with zf.open(fname) as f:
            for line in f:
                try:
                    parts = line.decode("latin-1").strip().split("|")
                    if len(parts) >= len(CM_COLS):
                        row = dict(zip(CM_COLS, parts))
                        committees[row["CMTE_ID"]] = {
                            "name": row["CMTE_NM"],
                            "connected_org": row["CONNECTED_ORG_NM"],
                            "type": row["CMTE_TP"],
                        }
                except Exception:
                    continue

    console.print(f"  Loaded {len(committees):,} committees")
    return committees


def classify_industry(committee_name: str, connected_org: str) -> str | None:
    """Return industry ID if this PAC is from a regulated industry."""
    text = (committee_name + " " + connected_org).upper()
    for industry_id, keywords in INDUSTRY_PACS.items():
        if any(kw in text for kw in keywords):
            return industry_id
    return None


def find_politician_match(cand_id: str, committees: dict) -> str | None:
    """
    Try to match a candidate ID to one of our politicians.
    FEC candidate IDs start with state abbreviation + office code.
    """
    # We'll match by looking at which candidate IDs our politicians have
    # This is a simplified match — in production use FEC candidate master
    return None


def load_pac_donations(committees: dict):
    """Load PAC-to-candidate donations, filtering for our politicians and industries."""
    console.print("\nLoading PAC-to-candidate donations...")
    console.print("(Scanning 24MB file — takes ~30 seconds)")

    # First build a set of industry PAC committee IDs
    industry_pac_ids = {}
    for cmte_id, cmte_data in committees.items():
        industry = classify_industry(cmte_data["name"], cmte_data["connected_org"])
        if industry:
            industry_pac_ids[cmte_id] = {
                "industry": industry,
                "name": cmte_data["name"],
                "connected_org": cmte_data["connected_org"]
            }

    console.print(f"  Found {len(industry_pac_ids):,} industry PACs")

    # Now scan donations and match to our politicians
    donations = []
    total_scanned = 0

    with zipfile.ZipFile("data/raw/fec/pas224.zip") as zf:
        fname = [f for f in zf.namelist() if f.endswith(".txt")][0]
        with zf.open(fname) as f:
            for line in f:
                total_scanned += 1
                try:
                    parts = line.decode("latin-1").strip().split("|")
                    if len(parts) < 17:
                        continue

                    row = dict(zip(PAS2_COLS, parts))
                    cmte_id = row.get("CMTE_ID", "")
                    recipient_name = row.get("NAME", "").upper()
                    amount_str = row.get("TRANSACTION_AMT", "0")
                    amount = float(amount_str) if amount_str else 0

                    if amount <= 0:
                        continue

                    # Check if this is from an industry PAC
                    if cmte_id not in industry_pac_ids:
                        continue

                    # Check if recipient is one of our politicians
                    matched_politician = None
                    for lastname, pol_data in POLITICIANS.items():
                        if lastname in recipient_name:
                            matched_politician = pol_data
                            break

                    if matched_politician:
                        pac_info = industry_pac_ids[cmte_id]
                        donations.append({
                            "politician_id": matched_politician["id"],
                            "politician_name": matched_politician["name"],
                            "pac_name": pac_info["name"],
                            "connected_org": pac_info["connected_org"],
                            "industry": pac_info["industry"],
                            "amount": amount,
                            "date": row.get("TRANSACTION_DT", ""),
                        })

                except Exception:
                    continue

    console.print(f"  Scanned {total_scanned:,} donation records")
    console.print(f"  Found [green]{len(donations)}[/green] industry PAC donations to committee members")
    return donations


def load_donations_to_graph(donations: list):
    """Write donations to Neo4j."""
    if not donations:
        console.print("[yellow]No donations to load[/yellow]")
        return

    with driver.session() as session:
        for d in donations:
            session.run("""
                MATCH (p:Politician {id: $pol_id})
                MERGE (o:Organization {id: $industry})
                SET o.sector = 'private'
                MERGE (o)-[don:DONATED_TO]->(p)
                ON CREATE SET don.total_amount = $amount,
                              don.num_donations = 1,
                              don.pac_name = $pac_name
                ON MATCH SET don.total_amount = don.total_amount + $amount,
                             don.num_donations = don.num_donations + 1
            """,
                pol_id=d["politician_id"],
                industry=d["industry"],
                amount=d["amount"],
                pac_name=d["pac_name"]
            )

    console.print(f"[green]✓[/green] Loaded {len(donations)} donations into graph")


def print_top_donations():
    """Show the most significant donation connections."""
    console.print("\n[bold red]Industry PAC → Committee Member Donations:[/bold red]")
    with driver.session() as session:
        results = session.run("""
            MATCH (o:Organization)-[d:DONATED_TO]->(p:Politician)-[:SAT_ON]->(c:Committee)
            RETURN o.id as industry, p.name as politician,
                   p.party as party, p.state as state,
                   d.total_amount as amount, d.pac_name as pac,
                   c.name as committee
            ORDER BY d.total_amount DESC
            LIMIT 20
        """).data()

        for r in results:
            console.print(
                f"  ${r['amount']:>10,.0f} | {r['politician']} ({r['party']}-{r['state']}) "
                f"| {r['industry']} | {r['committee'][:30]}"
            )


def main():
    committees = load_committee_master()
    donations = load_pac_donations(committees)
    load_donations_to_graph(donations)
    print_top_donations()

    console.print("\n[bold green]✓ FEC donations loaded![/bold green]")
    console.print("\nNeo4j query to see full capture chain:")
    console.print("[dim]MATCH (o:Organization)-[:DONATED_TO]->(p:Politician)-[:HAD_OVERSIGHT_OF]->(r:Rule)<-[:LOBBIED_ON]-(o2:Organization) WHERE r.capture_score > 60 RETURN o,p,r,o2[/dim]")
    driver.close()


if __name__ == "__main__":
    main()
