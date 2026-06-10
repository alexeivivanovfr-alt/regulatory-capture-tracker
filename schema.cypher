// ============================================================
// Regulatory Capture Tracker — Neo4j Schema
// ============================================================
// Run this after starting Neo4j to set up constraints and indexes

// --- Constraints (ensure uniqueness) ---

CREATE CONSTRAINT person_id IF NOT EXISTS
FOR (p:Person) REQUIRE p.id IS UNIQUE;

CREATE CONSTRAINT org_id IF NOT EXISTS
FOR (o:Organization) REQUIRE o.id IS UNIQUE;

CREATE CONSTRAINT rule_id IF NOT EXISTS
FOR (r:Rule) REQUIRE r.id IS UNIQUE;

CREATE CONSTRAINT filing_id IF NOT EXISTS
FOR (f:LobbyingFiling) REQUIRE f.id IS UNIQUE;

CREATE CONSTRAINT donation_id IF NOT EXISTS
FOR (d:Donation) REQUIRE d.id IS UNIQUE;

// --- Indexes (for query performance) ---

CREATE INDEX person_name IF NOT EXISTS FOR (p:Person) ON (p.name_clean);
CREATE INDEX org_name IF NOT EXISTS FOR (o:Organization) ON (o.name);
CREATE INDEX org_sector IF NOT EXISTS FOR (o:Organization) ON (o.sector);
CREATE INDEX rule_agency IF NOT EXISTS FOR (r:Rule) ON (r.agency);
CREATE INDEX rule_date IF NOT EXISTS FOR (r:Rule) ON (r.final_date);
CREATE INDEX rule_score IF NOT EXISTS FOR (r:Rule) ON (r.capture_score);

// ============================================================
// NODE SCHEMAS (documentation — actual creation via Python)
// ============================================================

/*
(:Person {
    id: STRING,              // Canonical ID (we assign)
    name: STRING,            // Display name
    name_clean: STRING,      // Normalized: "john smith"
    aliases: [STRING],       // Other name variants found
    source_ids: [STRING],    // IDs in source systems
    last_known_sector: STRING // "private" | "government" | "lobbying"
})

(:Organization {
    id: STRING,
    name: STRING,
    name_clean: STRING,
    sector: STRING,          // "private" | "government" | "ngo" | "lobbying_firm"
    industry: STRING,        // "aerospace" | "pharma" | "finance" etc.
    parent_id: STRING,       // Parent org if subsidiary
    is_pac: BOOLEAN
})

(:Rule {
    id: STRING,              // Docket ID
    title: STRING,
    agency: STRING,
    proposed_date: DATE,
    final_date: DATE,
    proposed_doc_number: STRING,
    final_doc_number: STRING,
    significant: BOOLEAN,    // Economically significant
    capture_score: FLOAT,    // 0-100, computed
    industry_language_score: FLOAT,  // From language provenance analysis
    status: STRING           // "proposed" | "final" | "withdrawn"
})

(:LobbyingFiling {
    id: STRING,
    registrant: STRING,      // Lobbying firm
    client: STRING,          // Company that hired them
    year: INTEGER,
    period: STRING,          // "Q1" | "Q2" | "Q3" | "Q4" | "annual"
    amount: FLOAT,
    issue_codes: [STRING]
})

(:Donation {
    id: STRING,
    amount: FLOAT,
    date: DATE,
    transaction_type: STRING,
    cycle: STRING            // Election cycle "2022" | "2024" etc.
})
*/

// ============================================================
// RELATIONSHIP SCHEMAS
// ============================================================

/*
(Person)-[:EMPLOYED_AT {
    start_date: DATE,
    end_date: DATE,
    role: STRING,
    seniority_score: FLOAT,  // 0-1
    source: STRING
}]->(Organization)

(Organization)-[:LOBBIED_ON {
    amount: FLOAT,
    year: INTEGER,
    issue_codes: [STRING],
    agencies_contacted: [STRING]
}]->(Rule)

(Organization)-[:FILED_DONATION {
    via_filing: STRING       // Filing ID
}]->(Donation)

(Donation)-[:DONATED_TO]->(Person)  // Person = politician

(Person)-[:HAD_OVERSIGHT_OF {
    role: STRING,
    committee: STRING
}]->(Rule)

(Organization)-[:COMMENTED_ON {
    comment_id: STRING,
    position: STRING,        // "support" | "oppose" | "modify"
    language_adopted_score: FLOAT  // How much of their language made it in
}]->(Rule)

(Organization)-[:SUBSIDIARY_OF]->(Organization)
(Person)-[:AFFILIATED_WITH]->(Organization)
*/

// ============================================================
// KEY QUERIES
// ============================================================

// Find the full capture chain for a rule
// MATCH path = (corp:Organization {sector: 'private'})
//     -[:LOBBIED_ON]->(rule:Rule)
//     <-[:HAD_OVERSIGHT_OF]-(official:Person)
//     -[:EMPLOYED_AT]->(corp)
// WHERE rule.capture_score > 60
// RETURN path, rule.title, corp.name, official.name
// ORDER BY rule.capture_score DESC

// Find revolving door: person went from industry to regulator to industry
// MATCH (p:Person)
//     -[:EMPLOYED_AT {sector_at_time: 'private'}]->(corp:Organization)
//     , (p)-[:EMPLOYED_AT {sector_at_time: 'government'}]->(agency:Organization)
// WHERE corp.industry = agency.regulates
// RETURN p.name, corp.name, agency.name
// ORDER BY p.name

// Top industries by lobbying spend on a specific agency's rules
// MATCH (o:Organization)-[l:LOBBIED_ON]->(r:Rule {agency: 'FAA'})
// RETURN o.industry, SUM(l.amount) as total_spend, COUNT(r) as rules_lobbied
// ORDER BY total_spend DESC
// LIMIT 20
