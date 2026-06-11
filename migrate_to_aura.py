"""
Migrate local Neo4j data to AuraDB.

Copies all nodes and relationships from local Neo4j to cloud AuraDB.
Run once to set up production database.
"""

from neo4j import GraphDatabase
from rich.console import Console

console = Console()

LOCAL = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "capturetracker"))
AURA = GraphDatabase.driver(
    "neo4j+s://b4654f24.databases.neo4j.io",
    auth=("b4654f24", "xVnbKoE44an9495TyoG9LEO5eULne-CbEQwVUmMgddI")
)


def migrate_nodes(label: str, batch_size: int = 500):
    """Migrate all nodes of a given label."""
    console.print(f"Migrating {label} nodes...")

    with LOCAL.session() as local_session:
        total = local_session.run(f"MATCH (n:{label}) RETURN count(n) as c").single()["c"]
        console.print(f"  Found {total:,} {label} nodes locally")

        if total == 0:
            return

        offset = 0
        migrated = 0
        while offset < total:
            batch = local_session.run(
                f"MATCH (n:{label}) RETURN properties(n) as props SKIP $skip LIMIT $limit",
                skip=offset, limit=batch_size
            ).data()

            if not batch:
                break

            with AURA.session() as aura_session:
                aura_session.run(f"""
                    UNWIND $batch AS props
                    MERGE (n:{label} {{id: props.id}})
                    SET n = props
                """, batch=[r["props"] for r in batch])

            migrated += len(batch)
            offset += batch_size
            console.print(f"  {migrated:,}/{total:,} {label} nodes migrated...")

    console.print(f"[green]✓[/green] {label}: {migrated:,} nodes migrated")


def migrate_relationships(rel_type: str, source_label: str, target_label: str, batch_size: int = 500):
    """Migrate all relationships of a given type."""
    console.print(f"Migrating {rel_type} relationships...")

    with LOCAL.session() as local_session:
        total = local_session.run(
            f"MATCH (a:{source_label})-[r:{rel_type}]->(b:{target_label}) RETURN count(r) as c"
        ).single()["c"]
        console.print(f"  Found {total:,} {rel_type} relationships")

        if total == 0:
            return

        offset = 0
        migrated = 0
        while offset < total:
            batch = local_session.run(f"""
                MATCH (a:{source_label})-[r:{rel_type}]->(b:{target_label})
                RETURN a.id as src, b.id as tgt, properties(r) as props
                SKIP $skip LIMIT $limit
            """, skip=offset, limit=batch_size).data()

            if not batch:
                break

            with AURA.session() as aura_session:
                aura_session.run(f"""
                    UNWIND $batch AS row
                    MATCH (a:{source_label} {{id: row.src}})
                    MATCH (b:{target_label} {{id: row.tgt}})
                    MERGE (a)-[r:{rel_type}]->(b)
                    SET r = row.props
                """, batch=batch)

            migrated += len(batch)
            offset += batch_size

    console.print(f"[green]✓[/green] {rel_type}: {migrated:,} relationships migrated")


def setup_constraints():
    """Create indexes and constraints on AuraDB."""
    console.print("Setting up AuraDB schema...")
    statements = [
        "CREATE CONSTRAINT rule_id IF NOT EXISTS FOR (r:Rule) REQUIRE r.id IS UNIQUE",
        "CREATE CONSTRAINT org_id IF NOT EXISTS FOR (o:Organization) REQUIRE o.id IS UNIQUE",
        "CREATE CONSTRAINT person_id IF NOT EXISTS FOR (p:Person) REQUIRE p.id IS UNIQUE",
        "CREATE CONSTRAINT politician_id IF NOT EXISTS FOR (p:Politician) REQUIRE p.id IS UNIQUE",
        "CREATE INDEX rule_agency IF NOT EXISTS FOR (r:Rule) ON (r.agency)",
        "CREATE INDEX rule_score IF NOT EXISTS FOR (r:Rule) ON (r.capture_score)",
    ]
    with AURA.session() as session:
        for stmt in statements:
            try:
                session.run(stmt)
            except Exception as e:
                if "already exists" not in str(e).lower():
                    console.print(f"[yellow]Warning: {e}[/yellow]")
    console.print("[green]✓[/green] Schema ready")


def verify():
    """Verify migration was successful."""
    console.print("\n[bold]Verifying AuraDB data:[/bold]")
    with AURA.session() as session:
        for label in ["Rule", "Organization", "Person", "Politician", "Committee"]:
            count = session.run(f"MATCH (n:{label}) RETURN count(n) as c").single()["c"]
            console.print(f"  {label}: [cyan]{count:,}[/cyan]")
        for rel in ["LOBBIED_ON", "EMPLOYED_AT", "SAT_ON", "DONATED_TO", "HAD_OVERSIGHT_OF"]:
            count = session.run(f"MATCH ()-[r:{rel}]->() RETURN count(r) as c").single()["c"]
            console.print(f"  {rel}: [cyan]{count:,}[/cyan]")


def main():
    console.print("[bold]Migrating local Neo4j → AuraDB[/bold]\n")

    setup_constraints()

    # Migrate nodes
    for label in ["Rule", "Organization", "Person", "Politician", "Committee"]:
        migrate_nodes(label)

    # Migrate relationships
    migrate_relationships("LOBBIED_ON", "Organization", "Rule")
    migrate_relationships("EMPLOYED_AT", "Person", "Organization")
    migrate_relationships("HAD_OVERSIGHT_OF", "Person", "Rule")
    migrate_relationships("SAT_ON", "Politician", "Committee")
    migrate_relationships("DONATED_TO", "Organization", "Politician")
    migrate_relationships("PART_OF", "Organization", "Organization")
    migrate_relationships("ISSUED", "Organization", "Rule")
    migrate_relationships("HAD_OVERSIGHT_OF", "Politician", "Rule")
    migrate_relationships("CONTROLS", "Organization", "Organization")

    verify()

    console.print("\n[bold green]✓ Migration complete![/bold green]")
    LOCAL.close()
    AURA.close()


if __name__ == "__main__":
    main()
