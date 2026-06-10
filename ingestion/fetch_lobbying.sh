#!/bin/bash
# Fetch lobbying filings for key industries using curl
# Run this first, then run process_lobbying.py

mkdir -p data/lobbying

echo "Fetching lobbying filings via curl..."

companies=(
    "ExxonMobil"
    "Chevron"
    "American+Petroleum+Institute"
    "Koch+Industries"
    "Peabody+Energy"
    "American+Coal+Council"
    "Edison+Electric+Institute"
    "Dow+Chemical"
    "American+Chemistry+Council"
    "3M+Company"
    "DuPont"
    "Boeing"
)

for company in "${companies[@]}"; do
    filename=$(echo "$company" | tr '+' '_' | tr '[:upper:]' '[:lower:]')
    echo "  Fetching $company..."
    curl -s "https://lda.senate.gov/api/v1/filings/?client_name=${company}&filing_year=2024" \
        -o "data/lobbying/${filename}_2024.json"
    sleep 1
done

echo "Done! Files saved to data/lobbying/"
ls -la data/lobbying/
