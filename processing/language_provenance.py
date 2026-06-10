"""
Language Provenance Analysis

The most damning evidence of regulatory capture: when final rule text
is copied from industry comment letters rather than the original proposed rule.

Uses Claude API to:
1. Compare proposed rule vs final rule (what changed?)
2. Compare final rule vs industry comments (was industry language adopted?)
3. Score the degree of industry influence on the final text
"""

import json
from dataclasses import dataclass
from typing import Optional

import anthropic
from rich.console import Console

console = Console()

client = anthropic.Anthropic()


@dataclass
class ProvenanceResult:
    rule_id: str
    industry_influence_score: float  # 0-100
    adopted_passages: list[dict]     # Text adopted from industry
    weakened_provisions: list[dict]  # Provisions that were watered down
    summary: str
    confidence: str  # "high", "medium", "low"

    def __str__(self):
        return (
            f"Rule: {self.rule_id}\n"
            f"Industry Influence Score: {self.industry_influence_score}/100\n"
            f"Adopted passages: {len(self.adopted_passages)}\n"
            f"Weakened provisions: {len(self.weakened_provisions)}\n"
            f"Summary: {self.summary}"
        )


def analyze_language_provenance(
    rule_id: str,
    proposed_rule_text: str,
    final_rule_text: str,
    industry_comment_text: str,
    max_chars: int = 4000
) -> ProvenanceResult:
    """
    Use Claude to detect when final rule language came from industry comments
    rather than the government's own proposed rule.
    
    This is the smoking gun of regulatory capture.
    
    Args:
        rule_id: Identifier for this rule (docket number)
        proposed_rule_text: The original government proposal
        final_rule_text: What was actually enacted
        industry_comment_text: Industry's formal comment/lobbying position
        max_chars: Max characters to send per document (to manage token costs)
    """

    # Truncate texts to stay within context limits
    # In production, use chunking + summarization for very long documents
    proposed = proposed_rule_text[:max_chars]
    final = final_rule_text[:max_chars]
    industry = industry_comment_text[:max_chars]

    prompt = f"""You are an expert regulatory analyst investigating regulatory capture in US federal rulemaking.

Regulatory capture occurs when a regulated industry influences a regulatory agency to act in the industry's interests rather than the public interest. One key indicator is when final rule text adopts language from industry comments rather than the government's original proposal.

Analyze these three documents:

---
DOCUMENT 1: PROPOSED RULE (Government's original proposal)
{proposed}
---

DOCUMENT 2: INDUSTRY COMMENT (Industry's lobbying position on the proposed rule)
{industry}
---

DOCUMENT 3: FINAL RULE (What was actually enacted after the comment period)
{final}
---

Analyze for signs of regulatory capture:

1. ADOPTED LANGUAGE: Find passages in the FINAL RULE that closely match the INDUSTRY COMMENT but differ from the PROPOSED RULE. These suggest industry language was directly incorporated.

2. WEAKENED PROVISIONS: Find provisions in the PROPOSED RULE that were made less stringent in the FINAL RULE, specifically where the industry commented opposing those provisions.

3. STRENGTHENED PROVISIONS: Note any cases where the final rule was STRONGER than proposed (this is the opposite of capture and is important for balance).

Be precise and epistemically honest. Note the strength of evidence for each finding.

Respond ONLY with valid JSON in this exact format:
{{
    "adopted_from_industry": [
        {{
            "final_rule_passage": "exact text from final rule",
            "industry_comment_passage": "similar text from industry comment",
            "similarity": "verbatim|near-verbatim|conceptual",
            "significance": "high|medium|low",
            "interpretation": "brief explanation of what this means"
        }}
    ],
    "weakened_provisions": [
        {{
            "proposed_text": "what the government originally proposed",
            "final_text": "what ended up in the final rule",
            "industry_opposed": true,
            "weakening_description": "how and how much it was weakened",
            "significance": "high|medium|low"
        }}
    ],
    "strengthened_provisions": [
        {{
            "description": "provision that became stronger",
            "significance": "high|medium|low"
        }}
    ],
    "industry_influence_score": <integer 0-100>,
    "confidence": "high|medium|low",
    "confidence_reason": "why you assigned this confidence level",
    "summary": "2-3 sentence plain English summary of findings",
    "caveats": "important limitations or alternative explanations"
}}"""

    console.print(f"Analyzing language provenance for [cyan]{rule_id}[/cyan]...")

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text

    # Clean up any markdown fences
    raw = raw.replace("```json", "").replace("```", "").strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        console.print(f"[red]JSON parse error: {e}[/red]")
        console.print(f"Raw response: {raw[:500]}")
        return ProvenanceResult(
            rule_id=rule_id,
            industry_influence_score=0,
            adopted_passages=[],
            weakened_provisions=[],
            summary="Analysis failed — could not parse response",
            confidence="low"
        )

    return ProvenanceResult(
        rule_id=rule_id,
        industry_influence_score=float(data.get("industry_influence_score", 0)),
        adopted_passages=data.get("adopted_from_industry", []),
        weakened_provisions=data.get("weakened_provisions", []),
        summary=data.get("summary", ""),
        confidence=data.get("confidence", "low")
    )


def batch_analyze_rulemaking(
    rulemakings: list[dict],
    max_rules: int = 10
) -> list[ProvenanceResult]:
    """
    Analyze multiple rulemaking records for language provenance.
    
    Each dict should have: rule_id, proposed_text, final_text, industry_comments
    """
    results = []

    for rulemaking in rulemakings[:max_rules]:
        # Combine multiple industry comments if present
        industry_text = "\n\n---\n\n".join(
            c.get("text", "") for c in rulemaking.get("industry_comments", [])
        )

        if not industry_text:
            console.print(f"[yellow]No industry comments for {rulemaking['rule_id']} — skipping[/yellow]")
            continue

        result = analyze_language_provenance(
            rule_id=rulemaking["rule_id"],
            proposed_rule_text=rulemaking.get("proposed_text", ""),
            final_rule_text=rulemaking.get("final_text", ""),
            industry_comment_text=industry_text
        )

        results.append(result)

        # Print findings as we go
        if result.industry_influence_score > 60:
            console.print(f"[red]⚠ HIGH CAPTURE SIGNAL[/red]: {result.rule_id} — Score: {result.industry_influence_score}/100")
            console.print(f"  {result.summary}")
        elif result.industry_influence_score > 30:
            console.print(f"[yellow]~ Moderate signal[/yellow]: {result.rule_id} — Score: {result.industry_influence_score}/100")
        else:
            console.print(f"[green]✓ Low signal[/green]: {result.rule_id} — Score: {result.industry_influence_score}/100")

    return sorted(results, key=lambda r: -r.industry_influence_score)


# --- Boeing/FAA example ---

BOEING_MCAS_CASE = {
    "rule_id": "FAA-2019-0001",
    "proposed_text": """
    The FAA proposes to require independent review of all flight control 
    software modifications that affect aircraft handling characteristics.
    Manufacturers shall not self-certify safety-critical systems.
    All MCAS-type systems shall undergo FAA-administered testing with 
    independent test pilots not employed by the manufacturer.
    Minimum simulator training requirements: 4 hours for significant 
    flight characteristic changes.
    """,
    "industry_comments": [
        {
            "commenter": "Boeing Commercial Airplanes",
            "text": """
            Boeing respectfully requests that the FAA maintain the existing 
            Organization Designation Authorization (ODA) framework that has 
            served aviation safety well for decades. Requiring FAA-direct 
            oversight of all software modifications would create unworkable 
            delays and is unnecessary given the existing safety management 
            systems Boeing has in place. We recommend maintaining manufacturer 
            self-certification with post-hoc FAA review for non-safety-critical 
            modifications. Simulator training requirements should be determined 
            by the manufacturer based on handling characteristic changes.
            """
        }
    ],
    "final_text": """
    The final rule maintains the ODA framework for manufacturer self-certification
    with enhanced FAA oversight processes. Manufacturers shall submit documentation
    for FAA review of flight control software modifications. The FAA will conduct
    risk-based oversight of safety-critical systems. Training requirements for 
    flight characteristic changes shall be determined through manufacturer analysis
    with FAA approval.
    """
}


if __name__ == "__main__":
    console.print("\n[bold]Boeing/FAA MCAS Language Provenance Analysis[/bold]\n")
    console.print("[dim]Note: Using simplified example text for demonstration[/dim]\n")

    result = analyze_language_provenance(
        rule_id=BOEING_MCAS_CASE["rule_id"],
        proposed_rule_text=BOEING_MCAS_CASE["proposed_text"],
        final_rule_text=BOEING_MCAS_CASE["final_text"],
        industry_comment_text=BOEING_MCAS_CASE["industry_comments"][0]["text"]
    )

    console.print("\n[bold]Results:[/bold]")
    console.print(result)

    if result.adopted_passages:
        console.print("\n[bold red]Adopted Industry Language:[/bold red]")
        for p in result.adopted_passages:
            console.print(f"  Similarity: {p['similarity']} | Significance: {p['significance']}")
            console.print(f"  Final rule: \"{p['final_rule_passage'][:100]}...\"")
            console.print(f"  Industry:   \"{p['industry_comment_passage'][:100]}...\"")

    if result.weakened_provisions:
        console.print("\n[bold yellow]Weakened Provisions:[/bold yellow]")
        for p in result.weakened_provisions:
            console.print(f"  {p['weakening_description']}")
