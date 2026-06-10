import json
from pathlib import Path
from neo4j import GraphDatabase

driver = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "capturetracker"))

all_revolvers = []
for f in Path("data/lobbying").glob("*.json"):
    data = json.load(open(f))
    for filing in data.get("results", []):
        client = filing.get("client", {}).get("name", "")
        client_id = client.upper().replace(" ", "_").replace(",", "").replace(".", "")[:50]
        for activity in filing.get("lobbying_activities", []):
            for lob in activity.get("lobbyists", []):
                lobbyist = lob.get("lobbyist", {})
                name = f"{lobbyist.get('first_name','')} {lobbyist.get('last_name','')}".strip()
                position = lob.get("covered_position", "")
                if name and position:
                    all_revolvers.append({
                        "name": name,
                        "position": position,
                        "client": client,
                        "client_id": client_id
                    })

seen = set()
loaded = 0
with driver.session() as session:
    for r in all_revolvers:
        key = r["name"] + r["position"]
        if key in seen:
            continue
        seen.add(key)

        person_id = r["name"].upper().replace(" ", "_").replace(",", "").replace(".", "")
        session.run("""
            MERGE (p:Person {id: $person_id})
            SET p.name = $name,
                p.former_government_role = $position,
                p.now_lobbying_for = $client,
                p.revolving_door = true
        """, person_id=person_id, name=r["name"],
            position=r["position"], client=r["client"])

        session.run("""
            MATCH (p:Person {id: $person_id})
            MERGE (o:Organization {id: $client_id})
            SET o.name = $client, o.sector = 'private'
            MERGE (p)-[e:EMPLOYED_AT]->(o)
            SET e.role = 'lobbyist',
                e.sector_at_time = 'lobbying',
                e.former_gov_role = $position
        """, person_id=person_id, client_id=r["client_id"],
            client=r["client"], position=r["position"])

        loaded += 1

print(f"Loaded {loaded} revolving door lobbyists into Neo4j")
driver.close()
