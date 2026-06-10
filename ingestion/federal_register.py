"""
Federal Register API ingestion.

The Federal Register is the official daily journal of the US government.
It contains all proposed rules, final rules, and notices from federal agencies.

API docs: https://www.federalregister.gov/developers/api/v1
No API key needed — fully public.

Key document types:
- Proposed Rule (PRULE): Agency's initial proposal, with public comment period
- Rule (RULE): Final version after comment period
- Notice (NOTICE): Announcements, hearings, etc.

The gap between proposed rule and final rule is where capture shows up.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Literal
import json

import httpx
from rich.console import Console

console = Console()

FR_API_BASE = "https://www.federalregister.gov/api/v1"

# Agency slugs for major regulatory agencies
# Full list: https://www.federalregister.gov/agencies
AGENCIES = {
    "faa": "federal-aviation-administration",
    "fda": "food-and-drug-administration",
    "epa": "environmental-protection-agency",
    "ftc": "federal-trade-commission",
    "sec": "securities-and-exchange-commission",
    "cftc": "commodity-futures-trading-commission",
    "osha": "occupational-safety-health-administration",
    "usda": "agricultural-marketing-service",
    "cms": "centers-for-medicare-medicaid-services",
    "ferc": "federal-energy-regulatory-commission",
}

DocumentType = Literal["RULE", "PRULE", "NOTICE", "PNOTICE"]


@dataclass
class FRDocument:
    document_number: str
    doc_type: str  # RULE, PRULE, NOTICE
    title: str
    abstract: str
    agencies: list[str]
    publication_date: Optional[date]
    effective_date: Optional[date]
    comment_deadline: Optional[date]
    significant: bool  # Economically significant (>$100M impact)
    docket_ids: list[str]
    full_text_url: Optional[str]
    raw_text_url: Optional[str]
    citation: str  # e.g., "88 FR 12345"
    cfr_references: list[dict]

    @property
    def is_significant(self) -> bool:
        return self.significant

    @property
    def fr_url(self) -> str:
        return f"https://www.federalregister.gov/documents/{self.document_number.replace('-', '/')}"


@dataclass
class RulemakingRecord:
    """
    A complete rulemaking record: proposed rule + final rule pair.
    This is the unit of analysis for detecting capture.
    """
    docket_id: str
    topic: str
    agency: str
    proposed_rule: Optional[FRDocument] = None
    final_rule: Optional[FRDocument] = None
    related_notices: list[FRDocument] = field(default_factory=list)

    @property
    def has_both_stages(self) -> bool:
        return self.proposed_rule is not None and self.final_rule is not None

    @property
    def comment_period_days(self) -> Optional[int]:
        if not self.proposed_rule:
            return None
        if not self.proposed_rule.comment_deadline or not self.proposed_rule.publication_date:
            return None
        return (self.proposed_rule.comment_deadline - self.proposed_rule.publication_date).days

    @property
    def rulemaking_duration_days(self) -> Optional[int]:
        if not self.has_both_stages:
            return None
        if not self.proposed_rule.publication_date or not self.final_rule.publication_date:
            return None
        return (self.final_rule.publication_date - self.proposed_rule.publication_date).days


class FederalRegisterIngester:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.raw_dir = data_dir / "raw" / "federal_register"
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    async def fetch_documents(
        self,
        agency_slug: str,
        doc_types: list[DocumentType] = ["RULE", "PRULE"],
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        significant_only: bool = False,
        max_pages: int = 10
    ) -> list[FRDocument]:
        """
        Fetch documents from the Federal Register API.
        
        Args:
            agency_slug: Agency identifier (use AGENCIES dict)
            doc_types: Types of documents to fetch
            date_from: Start date YYYY-MM-DD
            date_to: End date YYYY-MM-DD  
            significant_only: Only economically significant rules (>$100M)
            max_pages: Max pages to fetch (each page = 1000 documents)
        """
        all_docs = []

        params = {
            "conditions[agencies][]": agency_slug,
            "fields[]": [
                "document_number", "type", "title", "abstract",
                "agencies", "publication_date", "effective_on",
                "comments_close_on", "significant", "docket_ids",
                "full_text_xml_url", "raw_text_url", "citation",
                "cfr_references"
            ],
            "per_page": 1000,
            "order": "newest"
        }

        for dt in doc_types:
            params.setdefault("conditions[type][]", []).append(dt)

        if date_from:
            params["conditions[publication_date][gte]"] = date_from
        if date_to:
            params["conditions[publication_date][lte]"] = date_to
        if significant_only:
            params["conditions[significant]"] = "1"

        async with httpx.AsyncClient(timeout=30) as client:
            page = 1
            while page <= max_pages:
                params["page"] = page
                response = await client.get(
                    f"{FR_API_BASE}/documents.json",
                    params=params
                )
                response.raise_for_status()
                data = response.json()

                results = data.get("results", [])
                if not results:
                    break

                for item in results:
                    doc = self._parse_document(item)
                    all_docs.append(doc)

                total_pages = data.get("total_pages", 1)
                console.print(
                    f"Fetched page {page}/{total_pages} "
                    f"({len(all_docs)} documents so far)"
                )

                if page >= total_pages:
                    break
                page += 1

        console.print(f"[green]✓[/green] Fetched {len(all_docs)} documents for {agency_slug}")
        return all_docs

    def _parse_document(self, item: dict) -> FRDocument:
        def parse_date(s: Optional[str]) -> Optional[date]:
            if not s:
                return None
            try:
                return datetime.strptime(s, "%Y-%m-%d").date()
            except ValueError:
                return None

        return FRDocument(
            document_number=item.get("document_number", ""),
            doc_type=item.get("type", ""),
            title=item.get("title", ""),
            abstract=item.get("abstract", "") or "",
            agencies=[a.get("name", "") for a in item.get("agencies", [])],
            publication_date=parse_date(item.get("publication_date")),
            effective_date=parse_date(item.get("effective_on")),
            comment_deadline=parse_date(item.get("comments_close_on")),
            significant=bool(item.get("significant")),
            docket_ids=item.get("docket_ids", []),
            full_text_url=item.get("full_text_xml_url"),
            raw_text_url=item.get("raw_text_url"),
            citation=item.get("citation", ""),
            cfr_references=item.get("cfr_references", [])
        )

    async def fetch_full_text(self, doc: FRDocument) -> Optional[str]:
        """
        Download the full text of a rule.
        Used for language provenance analysis.
        """
        if not doc.raw_text_url:
            return None

        cache_path = self.raw_dir / f"{doc.document_number}.txt"
        if cache_path.exists():
            return cache_path.read_text()

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(doc.raw_text_url)
            if response.status_code == 200:
                text = response.text
                cache_path.write_text(text)
                return text

        return None

    async def fetch_public_comments(self, docket_id: str) -> list[dict]:
        """
        Fetch public comments on a proposed rule via regulations.gov API.
        Industry comments are the smoking gun for language adoption analysis.
        
        Note: regulations.gov requires a free API key.
        Sign up at: https://open.gsa.gov/api/regulationsgov/
        """
        import os
        api_key = os.getenv("REGULATIONS_GOV_API_KEY", "DEMO_KEY")

        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://api.regulations.gov/v4/comments",
                params={
                    "filter[searchTerm]": docket_id,
                    "page[size]": 250,
                    "sort": "postedDate",
                    "api_key": api_key
                }
            )

            if response.status_code == 429:
                console.print("[yellow]Rate limited by regulations.gov — using DEMO_KEY[/yellow]")
                return []

            response.raise_for_status()
            data = response.json()

        return data.get("data", [])

    def build_rulemaking_records(
        self, documents: list[FRDocument]
    ) -> list[RulemakingRecord]:
        """
        Group documents by docket ID to build complete rulemaking records.
        Each record = proposed rule + final rule + notices for one topic.
        """
        by_docket: dict[str, list[FRDocument]] = {}

        for doc in documents:
            for docket_id in doc.docket_ids:
                by_docket.setdefault(docket_id, []).append(doc)

        records = []
        for docket_id, docs in by_docket.items():
            proposed = next((d for d in docs if d.doc_type == "PRULE"), None)
            final = next((d for d in docs if d.doc_type == "RULE"), None)
            notices = [d for d in docs if d.doc_type in ["NOTICE", "PNOTICE"]]

            if not proposed and not final:
                continue

            topic = (proposed or final).title
            agency = (proposed or final).agencies[0] if (proposed or final).agencies else ""

            records.append(RulemakingRecord(
                docket_id=docket_id,
                topic=topic,
                agency=agency,
                proposed_rule=proposed,
                final_rule=final,
                related_notices=notices
            ))

        # Sort by completeness (both stages first) then by date
        records.sort(key=lambda r: (not r.has_both_stages, r.docket_id))
        return records

    def save_documents(self, documents: list[FRDocument], filename: str):
        """Cache documents to JSON for reuse."""
        output = self.raw_dir / filename
        data = [
            {
                "document_number": d.document_number,
                "type": d.doc_type,
                "title": d.title,
                "abstract": d.abstract,
                "publication_date": d.publication_date.isoformat() if d.publication_date else None,
                "effective_date": d.effective_date.isoformat() if d.effective_date else None,
                "significant": d.significant,
                "docket_ids": d.docket_ids,
                "citation": d.citation,
                "full_text_url": d.full_text_url,
            }
            for d in documents
        ]
        output.write_text(json.dumps(data, indent=2))
        console.print(f"[green]✓[/green] Saved {len(documents)} documents to {output}")


# --- Boeing/FAA validation ---

async def fetch_faa_aviation_rules(data_dir: Path):
    """
    Fetch FAA aviation safety rules — the domain where Boeing capture occurred.
    Focus on significant rules (>$100M economic impact).
    """
    ingester = FederalRegisterIngester(data_dir)

    console.print("\n[bold]Fetching FAA aviation safety rules (2015-2024)...[/bold]")
    docs = await ingester.fetch_documents(
        agency_slug=AGENCIES["faa"],
        doc_types=["RULE", "PRULE"],
        date_from="2015-01-01",
        significant_only=False,
        max_pages=5
    )

    records = ingester.build_rulemaking_records(docs)
    complete = [r for r in records if r.has_both_stages]

    console.print(f"\nRulemaking records: {len(records)} total, {len(complete)} with both stages")
    console.print("\nComplete rulemaking records (proposed + final):")
    for r in complete[:10]:
        duration = f"{r.rulemaking_duration_days}d" if r.rulemaking_duration_days else "?"
        console.print(f"  [{duration:>5}] {r.docket_id}: {r.topic[:70]}")

    ingester.save_documents(docs, "faa_rules.json")
    return records


if __name__ == "__main__":
    import sys
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./data")
    asyncio.run(fetch_faa_aviation_rules(data_dir))
