"""
Senate Lobbying Disclosure Act (LDA) data ingestion.

Senate LD-2 filings are the primary lobbying disclosure mechanism.
Bulk XML downloads: https://lda.senate.gov/system/public/

Key fields we care about:
- Registrant: the lobbying firm
- Client: the company that hired them
- Income amount: how much was paid
- Government entities contacted: which agencies were lobbied
- Issue area codes: what topics (e.g., TRN = Transportation, HCR = Healthcare)
- Lobbyists: individuals who lobbied + their former government positions
  (the coveredOfficialPosition field = revolving door evidence)
"""

import asyncio
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import xml.etree.ElementTree as ET

import httpx
from rich.console import Console
from rich.progress import track

console = Console()

# LDA bulk data URL pattern
LDA_BASE = "https://lda.senate.gov/api/v1"
LDA_BULK_BASE = "https://lda.senate.gov/system/public"

# Issue area codes we care about most
# Full list: https://lda.senate.gov/api/v1/constants/filing/lobbyingactivityissues/
HIGH_VALUE_ISSUES = {
    "TRN": "Transportation",
    "AVI": "Aviation/Aircraft",
    "ENV": "Environmental/Superfund",
    "CAW": "Clean Air & Water",
    "HCR": "Health Issues",
    "MED": "Medical/Disease",
    "PHR": "Pharmacy",
    "FIN": "Financial Institutions",
    "BNK": "Banking",
    "SEC": "Securities & Investments",
    "FOO": "Food Industry",
    "AGR": "Agriculture",
    "ENG": "Energy",
    "OIL": "Oil & Gas",
    "NUC": "Nuclear",
}


@dataclass
class Lobbyist:
    name: str
    covered_official_position: Optional[str]  # Former gov job — revolving door!
    new_lobbyist: bool


@dataclass
class LobbyingActivity:
    general_issue_area: str
    specific_issues: str
    agencies_contacted: list[str]
    lobbyists: list[Lobbyist]


@dataclass
class LDFiling:
    filing_id: str
    filing_type: str
    filing_year: int
    filing_period: str
    registrant_name: str
    registrant_country: str
    client_name: str
    client_country: str
    income_amount: Optional[float]
    expenses_amount: Optional[float]
    activities: list[LobbyingActivity]

    @property
    def total_amount(self) -> float:
        return self.income_amount or self.expenses_amount or 0.0

    @property
    def revolving_door_lobbyists(self) -> list[Lobbyist]:
        """Lobbyists who came from government positions."""
        return [
            l for act in self.activities
            for l in act.lobbyists
            if l.covered_official_position
        ]

    @property
    def agencies_contacted(self) -> set[str]:
        """All agencies contacted across all activities."""
        return {
            agency
            for act in self.activities
            for agency in act.agencies_contacted
        }


class LDAIngester:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.raw_dir = data_dir / "raw" / "lda"
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def filing_url(self, year: int, quarter: str) -> str:
        """
        quarter: "1", "2", "3", "4", or "mid_year", "year_end"
        """
        return f"{LDA_BULK_BASE}/{year}/sopr_xml/{year}_{quarter}.zip"

    async def download_quarter(
        self, year: int, quarter: str, force: bool = False
    ) -> Path:
        url = self.filing_url(year, quarter)
        dest = self.raw_dir / f"lda_{year}_Q{quarter}.zip"

        if dest.exists() and not force:
            console.print(f"[dim]Using cached {dest.name}[/dim]")
            return dest

        console.print(f"Downloading LDA filings [cyan]{year} Q{quarter}[/cyan]...")

        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.get(url)
            response.raise_for_status()

            with open(dest, "wb") as f:
                f.write(response.content)

        console.print(f"[green]✓[/green] Downloaded {dest.name}")
        return dest

    def parse_filing(self, xml_content: str) -> Optional[LDFiling]:
        """Parse a single LD-2 XML filing."""
        try:
            root = ET.fromstring(xml_content)
            ns = {"ld": ""}  # LDA XML has no namespace

            def find(path: str) -> Optional[str]:
                el = root.find(path)
                return el.text.strip() if el is not None and el.text else None

            # Parse income amount
            income_str = find(".//incomeLessThan5000")
            income_amt = None
            if income_str and income_str.lower() != "true":
                try:
                    income_amt = float(find(".//incomeAmount") or 0)
                except (ValueError, TypeError):
                    income_amt = None

            # Parse activities
            activities = []
            for activity in root.findall(".//lobbyingActivity"):
                agencies = [
                    a.text.strip()
                    for a in activity.findall(".//governmentEntity")
                    if a.text
                ]

                lobbyists = []
                for lob in activity.findall(".//lobbyist"):
                    name = lob.findtext("lobbyistName", "").strip()
                    covered = lob.findtext("coveredOfficialPosition", "").strip()
                    if name:
                        lobbyists.append(Lobbyist(
                            name=name,
                            covered_official_position=covered if covered else None,
                            new_lobbyist=lob.findtext("newLobbyist", "N").upper() == "Y"
                        ))

                issue_code = activity.findtext("generalIssueArea", "").strip()
                specific = activity.findtext("specificIssues", "").strip()

                activities.append(LobbyingActivity(
                    general_issue_area=issue_code,
                    specific_issues=specific,
                    agencies_contacted=agencies,
                    lobbyists=lobbyists
                ))

            return LDFiling(
                filing_id=find(".//registrationNumber") or "",
                filing_type=find(".//reportType") or "",
                filing_year=int(find(".//reportYear") or 0),
                filing_period=find(".//reportType") or "",
                registrant_name=find(".//registrantName") or "",
                registrant_country=find(".//registrantCountry") or "USA",
                client_name=find(".//clientName") or "",
                client_country=find(".//clientCountry") or "USA",
                income_amount=income_amt,
                expenses_amount=None,
                activities=activities
            )

        except ET.ParseError as e:
            console.print(f"[yellow]Warning: XML parse error: {e}[/yellow]")
            return None

    def load_filings_from_zip(
        self, zip_path: Path, client_keyword_filter: Optional[str] = None
    ) -> list[LDFiling]:
        """
        Load all filings from a quarterly zip file.
        Optionally filter by client name keyword.
        """
        filings = []

        with zipfile.ZipFile(zip_path) as zf:
            xml_files = [f for f in zf.namelist() if f.endswith(".xml")]
            console.print(f"Processing [cyan]{len(xml_files):,}[/cyan] filings from {zip_path.name}...")

            for filename in track(xml_files, description="Parsing filings..."):
                with zf.open(filename) as f:
                    content = f.read().decode("utf-8", errors="replace")

                    # Quick filter before expensive parse
                    if client_keyword_filter:
                        if client_keyword_filter.upper() not in content.upper():
                            continue

                    filing = self.parse_filing(content)
                    if filing:
                        filings.append(filing)

        console.print(f"[green]✓[/green] Parsed {len(filings):,} matching filings")
        return filings

    def find_revolving_door_lobbyists(
        self,
        filings: list[LDFiling],
        agency_filter: Optional[str] = None
    ) -> list[dict]:
        """
        Extract all revolving door lobbyists from a set of filings.
        These are people who came from government positions.

        This is the most damning data — someone who worked at FAA
        and now lobbies the FAA for Boeing.
        """
        results = []

        for filing in filings:
            revolvers = filing.revolving_door_lobbyists

            for lobbyist in revolvers:
                # Filter by agency if specified
                if agency_filter:
                    relevant_agencies = [
                        a for a in filing.agencies_contacted
                        if agency_filter.upper() in a.upper()
                    ]
                    if not relevant_agencies:
                        continue
                else:
                    relevant_agencies = list(filing.agencies_contacted)

                results.append({
                    "lobbyist_name": lobbyist.name,
                    "former_government_role": lobbyist.covered_official_position,
                    "client": filing.client_name,
                    "registrant": filing.registrant_name,
                    "year": filing.filing_year,
                    "amount": filing.total_amount,
                    "agencies_lobbied": relevant_agencies,
                    "issues": [
                        a.general_issue_area
                        for a in filing.activities
                        if lobbyist in a.lobbyists
                    ]
                })

        return sorted(results, key=lambda x: x["amount"], reverse=True)

    async def search_filings_api(
        self,
        client_name: str,
        year: Optional[int] = None,
        issue_code: Optional[str] = None
    ) -> list[dict]:
        """
        Search LDA API directly (no bulk download needed for targeted queries).
        Good for validating specific companies.
        """
        params = {"client_name": client_name}
        if year:
            params["filing_year"] = year
       
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{LDA_BASE}/filings/",
                params=params,
                headers={"Accept": "application/json"}
            )
            response.raise_for_status()
            data = response.json()

        return data.get("results", [])


# --- Boeing/FAA validation ---

async def find_boeing_faa_lobbyists(data_dir: Path):
    """
    Find lobbyists who worked at FAA and now lobby for Boeing/aerospace.
    This is a documented part of the Boeing capture story.
    """
    ingester = LDAIngester(data_dir)

    # Search via API (faster for targeted queries)
    console.print("\n[bold]Searching LDA API for Boeing lobbying filings...[/bold]")

    filings_raw = await ingester.search_filings_api(
        client_name="Boeing",
        issue_code="AVI"
    )

    console.print(f"Found [green]{len(filings_raw)}[/green] Boeing/Aviation filings via API")

    # Show revolving door instances
    console.print("\n[bold]Lobbyists with former FAA positions:[/bold]")
    for filing in filings_raw[:20]:
        for activity in filing.get("lobbying_activities", []):
            for lobbyist in activity.get("lobbyists", []):
                position = lobbyist.get("covered_official_position", "")
                if position and "FAA" in position.upper():
                    console.print(
                        f"  [red]•[/red] {lobbyist['lobbyist_name']} | "
                        f"Former: {position} | "
                        f"Now lobbying for: {filing.get('client', {}).get('client_name', '')}"
                    )

    return filings_raw


if __name__ == "__main__":
    import sys
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./data")
    asyncio.run(find_boeing_faa_lobbyists(data_dir))
