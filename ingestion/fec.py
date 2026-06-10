"""
FEC (Federal Election Commission) data ingestion.

Uses FEC bulk data downloads — no API key needed, all public record.
Docs: https://www.fec.gov/data/browse-data/?tab=bulk-data

Key datasets:
- cm: Committee master (who the committees are)
- cn: Candidate master (who the candidates are)  
- ccl: Candidate-committee linkages
- pas2: PAC-to-candidate contributions (most useful for us)
- indiv: Individual contributions (large, ~3GB)
- oppexp: Operating expenditures
"""

import asyncio
import zipfile
from pathlib import Path
from typing import Literal

import duckdb
import httpx
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

console = Console()

# FEC bulk data base URL
FEC_BASE = "https://www.fec.gov/files/bulk-downloads"

# Column schemas for FEC files (they ship without headers)
# Source: https://www.fec.gov/campaign-finance-data/contributions-committees-candidates-file-description/
SCHEMAS = {
    "pas2": {  # PAC to candidate contributions
        "columns": [
            "CMTE_ID", "AMNDT_IND", "RPT_TP", "TRANSACTION_PGI",
            "IMAGE_NUM", "TRANSACTION_TP", "ENTITY_TP", "NAME",
            "CITY", "STATE", "ZIP_CODE", "EMPLOYER", "OCCUPATION",
            "TRANSACTION_DT", "TRANSACTION_AMT", "OTHER_ID",
            "CAND_ID", "TRAN_ID", "FILE_NUM", "MEMO_CD",
            "MEMO_TEXT", "SUB_ID"
        ],
        "dtypes": {"TRANSACTION_AMT": "DOUBLE", "FILE_NUM": "BIGINT", "SUB_ID": "BIGINT"}
    },
    "cm": {  # Committee master
        "columns": [
            "CMTE_ID", "CMTE_NM", "TRES_NM", "CMTE_ST1", "CMTE_ST2",
            "CMTE_CITY", "CMTE_ST", "CMTE_ZIP", "CMTE_DSGN",
            "CMTE_TP", "CMTE_PTY_AFFILIATION", "CMTE_FILING_FREQ",
            "ORG_TP", "CONNECTED_ORG_NM", "CAND_ID"
        ],
        "dtypes": {}
    },
    "cn": {  # Candidate master
        "columns": [
            "CAND_ID", "CAND_NAME", "CAND_PTY_AFFILIATION",
            "CAND_ELECTION_YR", "CAND_OFFICE_ST", "CAND_OFFICE",
            "CAND_OFFICE_DISTRICT", "CAND_ICI", "CAND_STATUS",
            "CAND_PCC", "CAND_ST1", "CAND_ST2", "CAND_CITY",
            "CAND_ST", "CAND_ZIP"
        ],
        "dtypes": {}
    },
    "indiv": {  # Individual contributions (large!)
        "columns": [
            "CMTE_ID", "AMNDT_IND", "RPT_TP", "TRANSACTION_PGI",
            "IMAGE_NUM", "TRANSACTION_TP", "ENTITY_TP", "NAME",
            "CITY", "STATE", "ZIP_CODE", "EMPLOYER", "OCCUPATION",
            "TRANSACTION_DT", "TRANSACTION_AMT", "OTHER_ID",
            "TRAN_ID", "FILE_NUM", "MEMO_CD", "MEMO_TEXT", "SUB_ID"
        ],
        "dtypes": {"TRANSACTION_AMT": "DOUBLE", "FILE_NUM": "BIGINT", "SUB_ID": "BIGINT"}
    }
}

Dataset = Literal["cm", "cn", "ccl", "pas2", "indiv", "oppexp"]


class FECIngester:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.raw_dir = data_dir / "raw" / "fec"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.db = duckdb.connect(str(data_dir / "fec.duckdb"))

    def url_for(self, dataset: Dataset, cycle: str) -> str:
        """FEC uses 2-digit year suffixes, e.g. 2024 → 24"""
        suffix = cycle[-2:]
        return f"{FEC_BASE}/{cycle}/{dataset}{suffix}.zip"

    async def download(self, dataset: Dataset, cycle: str, force: bool = False) -> Path:
        """Download a FEC bulk data file if not already cached."""
        url = self.url_for(dataset, cycle)
        dest = self.raw_dir / f"{dataset}{cycle[-2:]}.zip"

        if dest.exists() and not force:
            console.print(f"[dim]Using cached {dest.name}[/dim]")
            return dest

        console.print(f"Downloading [cyan]{dataset}[/cyan] data for cycle [cyan]{cycle}[/cyan]...")
        console.print(f"URL: {url}")

        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length", 0))

                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    TextColumn("{task.completed:.1f}/{task.total:.1f} MB"),
                ) as progress:
                    task = progress.add_task(
                        f"Downloading {dest.name}",
                        total=total / 1_000_000
                    )
                    with open(dest, "wb") as f:
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            f.write(chunk)
                            progress.advance(task, len(chunk) / 1_000_000)

        console.print(f"[green]✓[/green] Downloaded {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
        return dest

    def load_to_duckdb(self, dataset: Dataset, cycle: str) -> str:
        """
        Load a FEC zip file directly into DuckDB.
        DuckDB can read CSVs inside zips — no need to extract.
        Returns the table name created.
        """
        zip_path = self.raw_dir / f"{dataset}{cycle[-2:]}.zip"

        if not zip_path.exists():
            raise FileNotFoundError(f"Download {dataset} first: {zip_path}")

        schema = SCHEMAS.get(dataset)
        if not schema:
            raise ValueError(f"No schema defined for dataset: {dataset}")

        table_name = f"fec_{dataset}_{cycle}"

        console.print(f"Loading [cyan]{zip_path.name}[/cyan] into DuckDB table [cyan]{table_name}[/cyan]...")

        # Build column type overrides string for DuckDB
        col_list = ", ".join(
            f"'{col}': '{schema['dtypes'].get(col, 'VARCHAR')}'"
            for col in schema["columns"]
        )

        # DuckDB reads zipped CSVs natively
        # FEC uses pipe delimiter, no header row, latin-1 encoding
        with zipfile.ZipFile(zip_path) as zf:
            # Get the actual filename inside the zip
            txt_files = [f for f in zf.namelist() if f.endswith(".txt")]
            if not txt_files:
                raise ValueError(f"No .txt files found in {zip_path}")
            inner_file = txt_files[0]

        self.db.execute(f"DROP TABLE IF EXISTS {table_name}")
        self.db.execute(f"""
            CREATE TABLE {table_name} AS
            SELECT *
            FROM read_csv(
                'zip://{zip_path}/{inner_file}',
                delim='|',
                header=false,
                columns={{{col_list}}},
                ignore_errors=true
            )
        """)

        count = self.db.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        console.print(f"[green]✓[/green] Loaded {count:,} records into {table_name}")

        return table_name

    def query_industry_donations(
        self,
        cycle: str,
        industry_keywords: list[str],
        min_amount: float = 1000.0
    ) -> list[dict]:
        """
        Find donations from a specific industry to candidates/committees.
        
        Args:
            cycle: Election cycle, e.g. "2024"
            industry_keywords: List of employer name keywords, e.g. ["BOEING", "AIRBUS"]
            min_amount: Minimum donation amount to include
            
        Returns:
            List of donation records with donor, recipient, amount, date
        """
        # Build WHERE clause for employer filtering
        employer_filter = " OR ".join(
            f"EMPLOYER ILIKE '%{kw}%'" for kw in industry_keywords
        )

        # Join donations with committee master to get recipient names
        query = f"""
            SELECT 
                p.NAME as donor_name,
                p.EMPLOYER as employer,
                p.OCCUPATION as occupation,
                p.TRANSACTION_AMT as amount,
                p.TRANSACTION_DT as date,
                p.TRANSACTION_TP as transaction_type,
                c.CMTE_NM as committee_name,
                c.CMTE_TP as committee_type,
                c.CMTE_PTY_AFFILIATION as party,
                c.CONNECTED_ORG_NM as connected_org
            FROM fec_indiv_{cycle} p
            LEFT JOIN fec_cm_{cycle} c ON p.CMTE_ID = c.CMTE_ID
            WHERE ({employer_filter})
              AND p.TRANSACTION_AMT >= {min_amount}
              AND p.TRANSACTION_TP NOT IN ('24T', '24F')  -- exclude refunds
            ORDER BY p.TRANSACTION_AMT DESC
        """

        results = self.db.execute(query).fetchdf()
        return results.to_dict(orient="records")

    def query_pac_contributions(
        self,
        cycle: str,
        committee_name_keywords: list[str],
    ) -> list[dict]:
        """
        Find PAC-to-candidate contributions from industry PACs.
        PAC names often contain industry names: "BOEING PAC", "PHARMA PAC" etc.
        """
        keyword_filter = " OR ".join(
            f"cm.CMTE_NM ILIKE '%{kw}%'" for kw in committee_name_keywords
        )

        query = f"""
            SELECT
                cm.CMTE_NM as pac_name,
                cm.CONNECTED_ORG_NM as sponsoring_org,
                p.TRANSACTION_AMT as amount,
                p.TRANSACTION_DT as date,
                p.CAND_ID as candidate_id
            FROM fec_pas2_{cycle} p
            JOIN fec_cm_{cycle} cm ON p.CMTE_ID = cm.CMTE_ID
            WHERE ({keyword_filter})
              AND p.TRANSACTION_AMT > 0
            ORDER BY p.TRANSACTION_AMT DESC
        """

        results = self.db.execute(query).fetchdf()
        return results.to_dict(orient="records")

    def get_top_donors_to_candidate(
        self,
        cycle: str,
        candidate_id: str
    ) -> list[dict]:
        """Get top organizational donors to a specific candidate."""
        query = f"""
            SELECT
                EMPLOYER,
                COUNT(*) as num_donations,
                SUM(TRANSACTION_AMT) as total_amount,
                AVG(TRANSACTION_AMT) as avg_amount
            FROM fec_indiv_{cycle}
            WHERE CAND_ID = '{candidate_id}'  -- via committee linkage
               OR CMTE_ID IN (
                   SELECT CMTE_ID FROM fec_cm_{cycle} WHERE CAND_ID = '{candidate_id}'
               )
            GROUP BY EMPLOYER
            HAVING EMPLOYER IS NOT NULL AND EMPLOYER != ''
            ORDER BY total_amount DESC
            LIMIT 50
        """
        results = self.db.execute(query).fetchdf()
        return results.to_dict(orient="records")


# --- Boeing/FAA validation case ---

BOEING_KEYWORDS = [
    "BOEING", "SPIRIT AEROSYSTEMS", "GE AVIATION",
    "SAFRAN", "HEICO", "TRANSDIGM",
    "AEROSPACE INDUSTRIES ASSOCIATION"
]

FAA_RELEVANT_CANDIDATES = [
    # Chairs/members of House Transportation Committee
    # and Senate Commerce Committee — who oversee FAA
    # These would be looked up dynamically in production
]


async def run_boeing_validation(data_dir: Path, cycle: str = "2024"):
    """
    Validate the pipeline against the known Boeing/FAA capture case.
    Should detect significant aerospace industry donations to 
    Transportation Committee members who weakened FAA oversight.
    """
    ingester = FECIngester(data_dir)

    # Download committee master and individual contributions
    console.print("\n[bold]Step 1: Downloading FEC data[/bold]")
    await ingester.download("cm", cycle)
    await ingester.download("indiv", cycle)

    console.print("\n[bold]Step 2: Loading into DuckDB[/bold]")
    ingester.load_to_duckdb("cm", cycle)
    ingester.load_to_duckdb("indiv", cycle)

    console.print("\n[bold]Step 3: Querying Boeing-related donations[/bold]")
    donations = ingester.query_industry_donations(
        cycle=cycle,
        industry_keywords=BOEING_KEYWORDS,
        min_amount=500.0
    )

    console.print(f"\nFound [green]{len(donations):,}[/green] Boeing-related donations in {cycle}")
    if donations:
        console.print("\nTop 10 by amount:")
        for d in donations[:10]:
            console.print(
                f"  ${d['amount']:>10,.0f} | {d['employer']:<40} | {d['committee_name']}"
            )

    return donations


if __name__ == "__main__":
    import sys
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./data")
    cycle = sys.argv[2] if len(sys.argv) > 2 else "2024"
    asyncio.run(run_boeing_validation(data_dir, cycle))
