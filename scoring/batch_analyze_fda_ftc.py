"""
FDA and FTC Batch Rule Analyzer
Uses curl for fetching (avoids httpx hanging issues on Mac).
Run: python3 scoring/batch_analyze_fda_ftc.py
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path
from neo4j import GraphDatabase
from rich.console import Console
import anthropic

console = Console()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "capturetracker")
client = anthropic.Anthropic()
driver = GraphDatabase.driver(NEO4J_URI, auth=("neo4j", NEO4J_PASSWORD))

PROGRESS_FILE = Path("data/fda_ftc_analysis_progress.json")

AGENCY_CONTEXTS = {
    "FDA": {
        "industry": "Pharmaceutical, food, tobacco, and medical device manufacturers",
        "signals": "weakening drug approval standards, self-certification, extended compliance timelines, narrow scope exempting large manufacturers, rollback of food labeling, user fee agreements giving pharma influence over FDA priorities, withdrawal of consumer protection rules"
    },
    "FTC": {
        "industry": "Technology, retail, financial services, and automotive industries",
        "signals": "withdrawal of consumer protection rules after industry petition, weakening merger review standards, guidance instead of binding rules, narrow market definitions allowing monopolistic behavior, reduced penalties making violations cost-effective, rollback of rules against deceptive practices"
    }
}


def load_progress():
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"analyzed": [], "results": []}


def save_progress(progress):
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


def get_docket_id(doc):
    ids = doc.get("docket_ids", [])
    raw = ids[0] if ids else doc.get("document_number", "")
    return raw.replace("Docket No. ", "").strip()


def fetch_rule_text(url, max_chars=3000):
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "--max-time", "20", url],
            capture_output=True, text=True, timeout=25
        )
        if not result.stdout:
            return ""
        text = re.sub(r'<[^>]+>', ' ', result.stdout)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:max_chars]
    except Exception:
        return ""


def analyze_rule(doc, agency):
    title = doc.get("title", "")
    url = doc.get("full_text_xml_url", "")
    docket_id = get_docket_id(doc)
    pub_date = doc.get("publication_date", "")
    context = AGENCY_CONTEXTS[agency]

    if not url or not docket_id:
        return None

    final_text = fetch_rule_text(url)
    if not final_text:
        console.print(f"  [yellow]Could not fetch rule text[/yellow]")
        return None

    prompt = f"""You are an expert regulatory analyst investigating regulatory capture at the US {agency}.

Analyze this {agency} rule for signs of regulatory capture by the regulated industry.

RULE TITLE: {title}
DATE: {pub_date}
REGULATED INDUSTRY: {context['industry']}

RULE TEXT (excerpt):
{final_text}

Look for these capture signals: {context['signals']}

Also: rollbacks, withdrawals, reconsiderations after industry petition, cost-benefit framing underweighting public health, language mirroring industry lobbying positions.

Respond ONLY with valid JSON:
{{
    "capture_signals": [
        {{
            "signal_type": "rollback|withdrawal|self_reporting|long_timeline|exemption|guidance_only|cost_benefit|industry_language",
            "significance": "high|medium|low",
            "interpretation": "specific explanation"
        }}
    ],
    "genuine_protections": [
        {{
            "description": "genuine consumer/public health protection",
            "significance": "high|medium|low"
        }}
    ],
    "industry_influence_score": <integer 0-100>,
    "confidence": "high|medium|low",
    "key_finding": "single most important finding in one sentence"
}}"""

    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.replace("```json", "").replace("```", "").strip()
        analysis = json.loads(raw)
    except Exception as e:
        console.print(f"  [red]API error: {e}[/red]")
        return None

    return {
        "docket_id": docket_id,
        "title": title,
        "pub_date": pub_date,
        "agency": agency,
        "score": analysis.get("industry_influence_score", 0),
        "confidence": analysis.get("confidence", "low"),
        "key_finding": analysis.get("key_finding", ""),
    }


def update_graph(result):
    if not result:
        return
    industry_id = "PHARMA_INDUSTRY" if result["agency"] == "FDA" else "TECH_RETAIL_INDUSTRY"
    industry_name = "Pharmaceutical & Food Industry" if result["agency"] == "FDA" else "Technology & Retail Industry"
    with driver.session() as session:
        session.run("""
            MERGE (r:Rule {id: $docket_id})
            SET r.title = $title, r.agency = $agency,
                r.publication_date = $pub_date,
                r.industry_language_score = $score,
                r.capture_score = $score,
                r.key_finding = $key_finding,
                r.confidence = $confidence
        """, docket_id=result["docket_id"], title=result["title"][:200],
            agency=result["agency"], pub_date=result["pub_date"],
            score=float(result["score"]), key_finding=result["key_finding"],
            confidence=result["confidence"])

        session.run("""
            MERGE (o:Organization {id: $org_id})
            SET o.name = $name, o.sector = 'private'
            WITH o
            MATCH (r:Rule {id: $docket_id})
            MERGE (o)-[l:LOBBIED_ON]->(r)
            SET l.influence_score = $score, l.language_adopted = $high
        """, org_id=industry_id, name=industry_name,
            docket_id=result["docket_id"], score=float(result["score"]),
            high=result["score"] > 60)


def print_summary(progress):
    results = progress.get("results", [])
    if not results:
        return
    by_agency = {}
    for r in results:
        by_agency.setdefault(r.get("agency", "?"), []).append(r)
    console.print(f"\n[bold]Results: {len(results)} rules analyzed[/bold]")
    for agency, ar in by_agency.items():
        high = [r for r in ar if r["score"] >= 60]
        med = [r for r in ar if 30 <= r["score"] < 60]
        console.print(f"  [cyan]{agency}[/cyan]: {len(ar)} rules — 🔴 {len(high)} high  🟡 {len(med)} medium  🟢 {len(ar)-len(high)-len(med)} low")
    high_all = sorted([r for r in results if r["score"] >= 60], key=lambda x: -x["score"])
    if high_all:
        console.print(f"\n[bold red]High risk rules:[/bold red]")
        for r in high_all:
            console.print(f"  {r['score']:>3}/100 | {r['agency']} | {r['pub_date']} | {r['title'][:60]}")
            if r.get("key_finding"):
                console.print(f"         → {r['key_finding'][:90]}")


def main():
    console.print("[bold]FDA + FTC Batch Rule Analyzer[/bold]\n")
    progress = load_progress()
    already_done = set(progress.get("analyzed", []))

    all_targets = []
    for agency, filename in [("FDA", "fda"), ("FTC", "ftc")]:
        with open(f"data/raw/federal_register/{filename}_rules.json") as f:
            docs = json.load(f)
        targets = [
            (doc, agency) for doc in docs
            if doc.get("significant") and doc.get("full_text_xml_url")
            and get_docket_id(doc) not in already_done
        ]
        console.print(f"{agency}: {len(targets)} rules to analyze")
        all_targets.extend(targets)

    console.print(f"\nTotal remaining: {len(all_targets)} rules")
    console.print("Progress saved after each rule — safe to stop and restart\n")

    if not all_targets:
        console.print("[green]All rules already analyzed![/green]")
        print_summary(progress)
        return

    for i, (doc, agency) in enumerate(all_targets):
        title = doc.get("title", "")[:60]
        console.print(f"[{i+1}/{len(all_targets)}] [{agency}] {title}...")

        result = analyze_rule(doc, agency)

        if result is None:
            progress["analyzed"].append(get_docket_id(doc))
            save_progress(progress)
            continue

        score = result["score"]
        color = "red" if score > 60 else "yellow" if score > 30 else "green"
        console.print(f"  [{color}]{score}/100[/{color}] — {result['key_finding'][:90]}")

        update_graph(result)
        progress["analyzed"].append(get_docket_id(doc))
        progress["results"].append(result)
        save_progress(progress)
        time.sleep(0.3)

    console.print("\n[bold green]Done![/bold green]")
    print_summary(progress)
    driver.close()


if __name__ == "__main__":
    main()
