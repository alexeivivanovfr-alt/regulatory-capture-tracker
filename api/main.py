"""
Regulatory Capture Tracker — API Backend
Run with: uvicorn api.main:app --reload --port 8000
"""

import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from neo4j import GraphDatabase

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "capturetracker")

app = FastAPI(title="Regulatory Capture Tracker")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

def query(cypher, **params):
    with driver.session() as session:
        return session.run(cypher, **params).data()

@app.get("/api/stats")
def get_stats():
    rules = query("MATCH (r:Rule) RETURN count(r) as n")[0]["n"]
    high_risk = query("MATCH (r:Rule) WHERE r.capture_score >= 60 RETURN count(r) as n")[0]["n"]
    orgs = query("MATCH (o:Organization {sector: 'private'}) RETURN count(o) as n")[0]["n"]
    revolvers = query("MATCH (p:Person {revolving_door: true}) RETURN count(p) as n")[0]["n"]
    connections = query("MATCH ()-[l:LOBBIED_ON]->() RETURN count(l) as n")[0]["n"]
    return {"total_rules": rules, "high_risk_rules": high_risk,
            "industries_tracked": orgs, "revolving_door_lobbyists": revolvers,
            "lobbying_connections": connections}

@app.get("/api/rules")
def get_rules(company_id: str = None, agency: str = None,
              politician_id: str = None,
              min_score: float = 0, limit: int = 200):
    if politician_id:
        return query("""
            MATCH (p:Politician {id: $pol_id})-[:HAD_OVERSIGHT_OF]->(r:Rule)
            WHERE r.capture_score >= $min_score
            OPTIONAL MATCH (o:Organization)-[:LOBBIED_ON]->(r)
            OPTIONAL MATCH (person:Person)-[:HAD_OVERSIGHT_OF]->(r)
            WHERE person.revolving_door = true
            RETURN r.id as id, r.title as title, r.agency as agency,
                   r.capture_score as score, r.publication_date as date,
                   r.key_finding as key_finding, r.confidence as confidence,
                   r.citation as citation,
                   collect(distinct o.name) as industries,
                   count(distinct person) > 0 as has_revolvers
            ORDER BY r.capture_score DESC LIMIT $limit
        """, pol_id=politician_id, min_score=min_score, limit=limit)

    if company_id:
        return query("""
            MATCH (o:Organization {id: $company_id})-[:PART_OF]->(generic:Organization)-[:LOBBIED_ON]->(r:Rule)
            WHERE r.capture_score >= $min_score
            OPTIONAL MATCH (o2:Organization)-[:LOBBIED_ON]->(r)
            OPTIONAL MATCH (p:Person)-[:HAD_OVERSIGHT_OF]->(r)
            WHERE p.revolving_door = true
            RETURN r.id as id, r.title as title, r.agency as agency,
                   r.capture_score as score, r.publication_date as date,
                   r.key_finding as key_finding, r.confidence as confidence,
                   r.citation as citation,
                   collect(distinct o2.name) as industries,
                   count(distinct p) > 0 as has_revolvers
            ORDER BY r.capture_score DESC LIMIT $limit
        """, company_id=company_id, min_score=min_score, limit=limit)

    where = "WHERE r.capture_score >= $min_score"
    if agency:
        where += " AND r.agency = $agency"

    return query(f"""
        MATCH (r:Rule) {where}
        OPTIONAL MATCH (o:Organization)-[:LOBBIED_ON]->(r)
        OPTIONAL MATCH (p:Person)-[:HAD_OVERSIGHT_OF]->(r)
        WHERE p.revolving_door = true
        RETURN r.id as id, r.title as title, r.agency as agency,
               r.capture_score as score, r.publication_date as date,
               r.key_finding as key_finding, r.confidence as confidence,
               r.citation as citation,
               collect(distinct o.name) as industries,
               count(distinct p) > 0 as has_revolvers
        ORDER BY r.capture_score DESC LIMIT $limit
    """, min_score=min_score, agency=agency, limit=limit)

@app.get("/api/rules/{rule_id}")
def get_rule_detail(rule_id: str):
    rule = query("""
        MATCH (r:Rule {id: $id})
        RETURN r.id as id, r.title as title, r.agency as agency,
               r.capture_score as score, r.publication_date as date,
               r.key_finding as key_finding, r.analysis_summary as summary,
               r.confidence as confidence, r.citation as citation,
               r.industry_language_score as language_score
    """, id=rule_id)
    if not rule:
        return {"error": "Rule not found"}
    lobbying = query("""
        MATCH (o:Organization)-[l:LOBBIED_ON]->(r:Rule {id: $id})
        RETURN o.name as company, l.amount as amount, l.year as year
        ORDER BY l.amount DESC
    """, id=rule_id)
    revolvers = query("""
        MATCH (p:Person)-[:HAD_OVERSIGHT_OF]->(r:Rule {id: $id})
        OPTIONAL MATCH (p)-[:EMPLOYED_AT]->(o:Organization)
        RETURN p.name as name, p.former_government_role as former_role, o.name as now_at
        LIMIT 20
    """, id=rule_id)
    politicians = query("""
        MATCH (p:Politician)-[:HAD_OVERSIGHT_OF]->(r:Rule {id: $id})
        MATCH (p)-[:SAT_ON]->(c:Committee)
        OPTIONAL MATCH (o:Organization)-[d:DONATED_TO]->(p)
        RETURN p.name as name, p.party as party, p.state as state,
               c.name as committee,
               collect(distinct {industry: o.id, amount: d.total_amount}) as donations
        ORDER BY p.name
        LIMIT 20
    """, id=rule_id)
    return {"rule": rule[0] if rule else {}, "lobbying": lobbying,
            "revolving_door": revolvers, "politicians": politicians}

@app.get("/api/revolvers")
def get_revolvers(limit: int = 100):
    return query("""
        MATCH (p:Person {revolving_door: true})-[:EMPLOYED_AT]->(o:Organization)
        RETURN p.name as name, p.former_government_role as former_role, o.name as now_at
        ORDER BY p.name LIMIT $limit
    """, limit=limit)

@app.get("/api/agencies")
def get_agencies():
    return query("""
        MATCH (r:Rule) WHERE r.agency IS NOT NULL
        RETURN r.agency as agency, count(r) as total_rules,
               count(CASE WHEN r.capture_score >= 60 THEN 1 END) as high_risk
        ORDER BY high_risk DESC
    """)

@app.get("/api/companies")
def get_companies():
    return query("""
        MATCH (specific:Organization)-[:PART_OF]->(generic:Organization)-[:LOBBIED_ON]->(r:Rule)
        WHERE specific.name IS NOT NULL
        RETURN specific.id as id, specific.name as name,
               count(distinct r) as rules_lobbied,
               count(distinct CASE WHEN r.capture_score >= 60 THEN r END) as high_risk_rules
        ORDER BY high_risk_rules DESC, rules_lobbied DESC
        LIMIT 50
    """)

@app.get("/api/politicians")
def get_politicians():
    return query("""
        MATCH (p:Politician)-[:SAT_ON]->(c:Committee)
        OPTIONAL MATCH (o:Organization)-[d:DONATED_TO]->(p)
        RETURN p.id as id, p.name as name, p.party as party, p.state as state,
               collect(distinct c.name) as committees,
               sum(d.total_amount) as total_donations
        ORDER BY total_donations DESC
        LIMIT 100
    """)

@app.get("/api/scoreboard")
def get_scoreboard():
    """Politicians ranked by total industry PAC donations with breakdown by industry."""
    results = query("""
        MATCH (p:Politician)-[:SAT_ON]->(c:Committee)
        OPTIONAL MATCH (o:Organization)-[d:DONATED_TO]->(p)
        RETURN p.id as id, p.name as name, p.party as party, p.state as state,
               collect(distinct c.name) as committees,
               sum(d.total_amount) as total_donations,
               collect({industry: o.id, amount: d.total_amount}) as donation_details
        ORDER BY total_donations DESC
    """)

    # Build breakdown by industry for each politician
    scoreboard = []
    for r in results:
        breakdown = {}
        for d in (r.get("donation_details") or []):
            if d.get("industry") and d.get("amount"):
                industry = d["industry"]
                breakdown[industry] = breakdown.get(industry, 0) + (d["amount"] or 0)

        scoreboard.append({
            "id": r["id"],
            "name": r["name"],
            "party": r["party"],
            "state": r["state"],
            "committees": r["committees"],
            "total_donations": r["total_donations"] or 0,
            "breakdown": breakdown
        })

    return sorted(scoreboard, key=lambda x: -(x["total_donations"] or 0))


@app.get("/")
def serve_frontend():
    return FileResponse("api/index.html")


@app.get("/health")
def health():
    return {"status": "ok"}
