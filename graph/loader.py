"""
Graph Loader — loads processed data into Neo4j.

Run this after ingestion to populate the graph database.
Then open http://localhost:7474 to explore visually.
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


class GraphLoader:
    def __init__(self):
        self.driver = GraphDatabase.driver(
            NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD)
        )
        console.print(f"[green]✓[/green] Connected to Neo4j at {NEO4J_URI}")

    def close(self):
        self.driver.close()

    def query(self, cypher: str, **params):
        with self.driver.session() as session:
            result = session.run(cypher, **params)
            return result.data()

    def setup_schema(self):
        console.print("\n[bold]Setting up schema...[/bold]")
        statements = [
            "CREATE CONSTRAINT rule_id IF NOT EXISTS FOR (r:Rule) REQUIRE r.id IS UNIQUE",
            "CREATE CONSTRAINT org_id IF NOT EXISTS FOR (o:Organization) REQUIRE o.id IS UNIQUE",
            "CREATE CONSTRAINT person_id IF NOT EXISTS FOR (p:Person) REQUIRE p.id IS UNIQUE",
            "CREATE INDEX rule_agency IF NOT EXISTS FOR (r:Rule) ON (r.agency)",
            "CREATE INDEX rule_score IF NOT EXISTS FOR (r:Rule) ON (r.capture_score)",
            "CREATE INDEX org_name IF NOT EXISTS FOR (o:Organization) ON (o.name)",
        ]
        for stmt in statements:
            try:
                self.query(stmt)
            except Exception as e:
                if "already exists" not in str(e).lower():
                    console.print(f"[yellow]Warning: {e}[/yellow]")
        console.print("[green]✓[/green] Schema ready")

    def load_faa_rules(self, data_dir: Path):
        rules_file = data_dir / "raw" / "federal_register" / "faa_rules.json"
        if not rules_file.exists():
            console.print(f"[yellow]No rules file found — run federal_register.py first[/yellow]")
            return 0

        console.print(f"\n[bold]Loading FAA rules...[/bold]")

        with open(rules_file) as f:
            documents = json.load(f)

        self.query("""
            MERGE (o:Organization {id: 'FAA'})
            SET o.name = 'Federal Aviation Administration',
                o.sector = 'government'
        """)

        loaded = 0
        for doc in documents:
            if doc.get("type") not in ["RULE", "PRULE"]:
                continue
            docket_ids = doc.get("docket_ids", [])
            docket_id = docket_ids[0] if docket_ids else doc.get("document_number", "")
            if not docket_id:
                continue

            self.query("""
                MERGE (r:Rule {id: $id})
                SET r.title = $title,
                    r.agency = 'FAA',
                    r.doc_type = $doc_type,
                    r.publication_date = $pub_date,
                    r.significant = $significant,
                    r.citation = $citation,
                    r.capture_score = 0.0
            """,
                id=docket_id,
                title=doc.get("title", "")[:200],
                doc_type=doc.get("type", ""),
                pub_date=doc.get("publication_date", ""),
                significant=bool(doc.get("significant", False)),
                citation=doc.get("citation", "")
            )

            self.query("""
                MATCH (r:Rule {id: $rule_id})
                MATCH (o:Organization {id: 'FAA'})
                MERGE (o)-[:ISSUED]->(r)
            """, rule_id=docket_id)

            loaded += 1

        console.print(f"[green]✓[/green] Loaded {loaded} FAA rules into graph")
        return loaded

    def load_boeing_org(self):
        console.print("\n[bold]Creating Boeing organization nodes...[/bold]")

        for org in [
            {"id": "BOEING", "name": "Boeing Company", "sector": "private", "industry": "aerospace"},
            {"id": "BOEING_PAC", "name": "Boeing PAC", "sector": "private", "industry": "aerospace"},
            {"id": "AIA", "name": "Aerospace Industries Association", "sector": "trade_group", "industry": "aerospace"},
        ]:
            self.query("""
                MERGE (o:Organization {id: $id})
                SET o.name = $name, o.sector = $sector, o.industry = $industry
            """, **org)

        self.query("""
            MATCH (boeing:Organization {id: 'BOEING'})
            MATCH (pac:Organization {id: 'BOEING_PAC'})
            MERGE (boeing)-[:CONTROLS]->(pac)
        """)

        console.print("[green]✓[/green] Boeing organization nodes created")

    def load_mcas_capture_case(self):
        console.print("\n[bold]Loading Boeing/MCAS capture case findings...[/bold]")

        self.query("""
            MERGE (r:Rule {id: 'FAA-2019-0001'})
            SET r.title = 'MCAS Flight Control System Certification Requirements',
                r.agency = 'FAA',
                r.doc_type = 'RULE',
                r.publication_date = '2019-03-01',
                r.significant = true,
                r.capture_score = 85.0,
                r.industry_language_score = 85.0,
                r.notes = 'Boeing 737 MAX MCAS. Language provenance score: 85/100.'
        """)

        self.query("""
            MATCH (boeing:Organization {id: 'BOEING'})
            MATCH (rule:Rule {id: 'FAA-2019-0001'})
            MERGE (boeing)-[l:LOBBIED_ON]->(rule)
            SET l.position = 'oppose',
                l.outcome = 'favorable',
                l.language_adopted = true,
                l.influence_score = 85.0,
                l.notes = 'Boeing requested maintaining ODA self-certification. Final rule adopted this verbatim.'
        """)

        self.query("""
            MERGE (p:Person {id: 'ALI_BAHRAMI'})
            SET p.name = 'Ali Bahrami',
                p.name_clean = 'ali bahrami',
                p.notes = 'FAA Associate Administrator for Aviation Safety'
        """)

        self.query("""
            MATCH (p:Person {id: 'ALI_BAHRAMI'})
            MATCH (faa:Organization {id: 'FAA'})
            MERGE (p)-[e:EMPLOYED_AT]->(faa)
            SET e.role = 'Associate Administrator for Aviation Safety',
                e.start_date = '2005-01-01',
                e.end_date = '2018-01-01',
                e.seniority_score = 0.9,
                e.sector_at_time = 'government'
        """)

        self.query("""
            MATCH (p:Person {id: 'ALI_BAHRAMI'})
            MATCH (r:Rule {id: 'FAA-2019-0001'})
            MERGE (p)-[o:HAD_OVERSIGHT_OF]->(r)
            SET o.role = 'Associate Administrator for Aviation Safety'
        """)

        console.print("[green]✓[/green] MCAS capture case loaded")

    def print_summary(self):
        console.print("\n[bold]Graph Summary:[/bold]")

        counts = {
            "Rules": "MATCH (r:Rule) RETURN count(r) as n",
            "Organizations": "MATCH (o:Organization) RETURN count(o) as n",
            "People": "MATCH (p:Person) RETURN count(p) as n",
            "Lobbying relationships": "MATCH ()-[l:LOBBIED_ON]->() RETURN count(l) as n",
            "Employment relationships": "MATCH ()-[e:EMPLOYED_AT]->() RETURN count(e) as n",
        }

        for label, query in counts.items():
            result = self.query(query)
            count = result[0]["n"] if result else 0
            console.print(f"  {label}: [cyan]{count:,}[/cyan]")

        console.print("\n[bold]Rules with high capture scores:[/bold]")
        results = self.query("""
            MATCH (r:Rule)
            WHERE r.capture_score > 50
            RETURN r.id as id, r.title as title, r.capture_score as score, r.agency as agency
            ORDER BY r.capture_score DESC
            LIMIT 10
        """)

        for r in results:
            console.print(f"  [red]Score {r['score']:.0f}[/red] | {r['agency']} | {str(r['title'])[:60]}")

    def run_capture_query(self):
        console.print("\n[bold]Running capture chain query...[/bold]")

        results = self.query("""
            MATCH (org:Organization)-[l:LOBBIED_ON]->(rule:Rule)
            WHERE l.language_adopted = true
            OPTIONAL MATCH (person:Person)-[:HAD_OVERSIGHT_OF]->(rule)
            RETURN
                org.name as industry,
                rule.title as rule,
                rule.capture_score as score,
                collect(distinct person.name) as officials,
                l.notes as evidence
            ORDER BY rule.capture_score DESC
        """)

        if not results:
            console.print("  No capture chains found yet")
            return

        for r in results:
            console.print(f"\n  [red]⚠ CAPTURE SIGNAL DETECTED[/red]")
            console.print(f"  Industry:  {r['industry']}")
            console.print(f"  Rule:      {str(r['rule'])[:70]}")
            console.print(f"  Score:     {r['score']}/100")
            console.print(f"  Officials: {', '.join(r['officials']) or 'Unknown'}")
            console.print(f"  Evidence:  {str(r['evidence'])[:120]}")


def main():
    data_dir = Path("./data")
    loader = GraphLoader()

    try:
        loader.setup_schema()
        loader.load_faa_rules(data_dir)
        loader.load_boeing_org()
        loader.load_mcas_capture_case()
        loader.print_summary()
        loader.run_capture_query()

        console.print("\n[bold green]✓ Graph loaded successfully![/bold green]")
        console.print("\nOpen [cyan]http://localhost:7474[/cyan] to explore visually.")
        console.print("Login: neo4j / capturetracker")
        console.print("\nTry this query in the Neo4j browser:")
        console.print("[dim]MATCH (o:Organization)-[l:LOBBIED_ON]->(r:Rule) RETURN o,l,r[/dim]")

    finally:
        loader.close()


if __name__ == "__main__":
    main()
