"""
Capture Scoring Model

Scores each rule in the graph by regulatory capture risk.
Combines available signals into a single 0-100 score.

Signals used (more will be added as pipeline grows):
- Industry language adoption (from language provenance analysis)
- Lobbying activity on this rule
- Revolving door officials involved
- Whether rule was significantly weakened from proposal
- Economic significance (higher stakes = higher scrutiny)

Run this after loading data into Neo4j.
"""

import os
from neo4j import GraphDatabase
from rich.console import Console
from rich.table import Table

console = Console()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "capturetracker")


class CaptureScorer:
    def __init__(self):
        self.driver = GraphDatabase.driver(
            NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD)
        )
        console.print(f"[green]✓[/green] Connected to Neo4j")

    def close(self):
        self.driver.close()

    def query(self, cypher: str, **params):
        with self.driver.session() as session:
            return session.run(cypher, **params).data()

    def score_all_rules(self):
        """
        Score every rule in the graph.
        Each signal contributes a weighted portion of the final score.
        """
        console.print("\n[bold]Scoring all rules...[/bold]")

        rules = self.query("MATCH (r:Rule) RETURN r.id as id, r.significant as significant")
        console.print(f"Found {len(rules)} rules to score")

        scored = 0
        for rule in rules:
            rule_id = rule["id"]
            score, signals = self.compute_score(rule_id, rule.get("significant", False))

            # Write score back to graph
            self.query("""
    MATCH (r:Rule {id: $id})
    WHERE r.industry_language_score IS NULL
    SET r.capture_score = $score,
        r.score_signals = $signals
""", id=rule_id, score=score, signals=str(signals))

            scored += 1
            if scored % 500 == 0:
                console.print(f"  Scored {scored}/{len(rules)} rules...")

        console.print(f"[green]✓[/green] Scored {scored} rules")

    def compute_score(self, rule_id: str, significant: bool) -> tuple[float, dict]:
        """
        Compute capture score for a single rule.
        Returns (score, signals_dict).
        """
        signals = {}

        # Signal 1: Language provenance (weight: 35%)
        # Did industry's language end up in the final rule?
        lang_result = self.query("""
            MATCH (r:Rule {id: $id})
            RETURN coalesce(r.industry_language_score, 0) as lang_score
        """, id=rule_id)
        lang_score = lang_result[0]["lang_score"] if lang_result else 0
        signals["language_adoption"] = lang_score
        language_contribution = (lang_score / 100) * 35

        # Signal 2: Lobbying activity (weight: 25%)
        # Was this rule actively lobbied on?
        lobby_result = self.query("""
            MATCH (o:Organization)-[l:LOBBIED_ON]->(r:Rule {id: $id})
            RETURN count(l) as num_lobbyists,
                   coalesce(avg(l.influence_score), 0) as avg_influence
        """, id=rule_id)
        if lobby_result and lobby_result[0]["num_lobbyists"] > 0:
            num_lobbyists = lobby_result[0]["num_lobbyists"]
            avg_influence = lobby_result[0]["avg_influence"]
            lobby_score = min(100, (num_lobbyists * 20) + avg_influence)
        else:
            lobby_score = 0
        signals["lobbying_activity"] = lobby_score
        lobbying_contribution = (lobby_score / 100) * 25

        # Signal 3: Revolving door (weight: 25%)
        # Were officials with industry ties involved?
        revolve_result = self.query("""
            MATCH (p:Person)-[:HAD_OVERSIGHT_OF]->(r:Rule {id: $id})
            OPTIONAL MATCH (p)-[e:EMPLOYED_AT]->(o:Organization {sector: 'private'})
            RETURN count(distinct p) as total_officials,
                   count(distinct e) as industry_connected
        """, id=rule_id)
        if revolve_result and revolve_result[0]["total_officials"] > 0:
            total = revolve_result[0]["total_officials"]
            connected = revolve_result[0]["industry_connected"]
            revolve_score = (connected / total) * 100 if total > 0 else 0
        else:
            revolve_score = 0
        signals["revolving_door"] = revolve_score
        revolving_contribution = (revolve_score / 100) * 25

        # Signal 4: Economic significance (weight: 15%)
        # Higher stakes rules get more scrutiny weight
        significance_score = 100 if significant else 20
        signals["economic_significance"] = significance_score
        significance_contribution = (significance_score / 100) * 15

        # Final score
        total_score = (
            language_contribution +
            lobbying_contribution +
            revolving_contribution +
            significance_contribution
        )

        return round(total_score, 1), signals

    def print_top_rules(self, limit: int = 20):
        """Print the highest scoring rules."""
        console.print(f"\n[bold]Top {limit} Rules by Capture Risk Score:[/bold]")

        results = self.query("""
            MATCH (r:Rule)
            WHERE r.capture_score > 0
            OPTIONAL MATCH (o:Organization)-[:LOBBIED_ON]->(r)
            RETURN r.id as id,
                   r.title as title,
                   r.agency as agency,
                   r.capture_score as score,
                   r.publication_date as date,
                   r.significant as significant,
                   collect(distinct o.name) as lobbyists
            ORDER BY r.capture_score DESC
            LIMIT $limit
        """, limit=limit)

        table = Table(show_header=True, header_style="bold blue")
        table.add_column("Score", width=7)
        table.add_column("Date", width=12)
        table.add_column("Rule", width=55)
        table.add_column("Lobbied By", width=20)

        for r in results:
            score = r["score"]
            color = "red" if score > 60 else "yellow" if score > 30 else "green"
            lobbyists = ", ".join(r["lobbyists"]) if r["lobbyists"] else "—"
            table.add_row(
                f"[{color}]{score}[/{color}]",
                str(r["date"] or "")[:10],
                str(r["title"] or "")[:55],
                lobbyists[:20]
            )

        console.print(table)

    def print_distribution(self):
        """Show distribution of capture scores."""
        console.print("\n[bold]Score Distribution:[/bold]")

        buckets = [
            ("🔴 High risk   (60-100)", 60, 100),
            ("🟡 Medium risk (30-60) ", 30, 60),
            ("🟢 Low risk    (0-30)  ", 0, 30),
        ]

        for label, low, high in buckets:
            result = self.query("""
                MATCH (r:Rule)
                WHERE r.capture_score >= $low AND r.capture_score < $high
                RETURN count(r) as n
            """, low=low, high=high)
            count = result[0]["n"] if result else 0
            console.print(f"  {label}: [cyan]{count:,}[/cyan] rules")

    def flag_for_investigation(self):
        """Return rules that warrant human investigation."""
        console.print("\n[bold]Rules Flagged for Investigation:[/bold]")

        results = self.query("""
            MATCH (r:Rule)
            WHERE r.capture_score >= 60
            OPTIONAL MATCH (o:Organization)-[l:LOBBIED_ON]->(r)
            OPTIONAL MATCH (p:Person)-[:HAD_OVERSIGHT_OF]->(r)
            RETURN r.id as id,
                   r.title as title,
                   r.capture_score as score,
                   r.citation as citation,
                   collect(distinct o.name) as industries,
                   collect(distinct p.name) as officials
            ORDER BY r.capture_score DESC
        """)

        if not results:
            console.print("  No rules above threshold yet — run language provenance analysis on more rules to increase scores")
            return

        for r in results:
            console.print(f"\n  [red]⚠ Score {r['score']}/100[/red] — {r['citation']}")
            console.print(f"  Rule: {str(r['title'])[:70]}")
            console.print(f"  Industries: {', '.join(r['industries']) or 'Unknown'}")
            console.print(f"  Officials:  {', '.join(r['officials']) or 'Unknown'}")
            console.print(f"  Neo4j ID:   {r['id']}")


def main():
    scorer = CaptureScorer()

    try:
        scorer.score_all_rules()
        scorer.print_distribution()
        scorer.print_top_rules(limit=15)
        scorer.flag_for_investigation()

        console.print("\n[bold green]✓ Scoring complete![/bold green]")
        console.print("\nIn Neo4j browser, try:")
        console.print("[dim]MATCH (r:Rule) WHERE r.capture_score > 10 RETURN r ORDER BY r.capture_score DESC LIMIT 25[/dim]")

    finally:
        scorer.close()


if __name__ == "__main__":
    main()
