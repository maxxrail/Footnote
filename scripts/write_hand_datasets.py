# ruff: noqa: E501 (hand-written questions and key points)
"""Write the hand-written eval sets: qualitative (30) and refusal (15).

    uv run python scripts/write_hand_datasets.py

Each qualitative item was written from the filing passage that answers it (found with the
Part 2 search and read before writing): the expected (ticker, section) is where that
passage lives, and the key points restate what it says. Refusal items ask for data the
corpus does not have.
"""

from __future__ import annotations

from analyst.evals.datasets import DATASETS, ExpectedSection, Item, save

Q = [
    (
        "NVDA",
        "Item 1A",
        "According to NVIDIA's 10-K for fiscal 2026, how did U.S. export license requirements affect its H20 products?",
        [
            "The U.S. government required a license to export H20 chips to China (and D:5 countries) starting April 2025",
            "NVIDIA took a $4.5 billion charge in Q1 fiscal 2026 for H20 excess inventory and purchase obligations",
            "Licenses granted in August 2025 allowed some H20 shipments to certain China customers, generating about $60 million of revenue",
            "U.S. officials expect to receive 15% or more of revenue from licensed sales",
        ],
    ),
    (
        "NVDA",
        "Item 7",
        "What does NVIDIA's MD&A say caused its gross margin to decline in fiscal 2026?",
        [
            "The business model transitioned from Hopper HGX systems to Blackwell full-scale datacenter solutions",
            "A $4.5 billion charge for H20 excess inventory and purchase obligations also reduced gross margin",
        ],
    ),
    (
        "MSFT",
        "Item 7",
        "In its fiscal 2026 10-K MD&A, what product-level drivers does Microsoft give for its revenue and gross margin?",
        [
            "Microsoft 365 Consumer cloud revenue grew 28%, driven by revenue per user and subscriber growth",
            "LinkedIn revenue increased 11% and Dynamics 365 revenue grew 18%",
            "Cost of revenue rose on investments in AI infrastructure for Microsoft 365 Copilot",
            "Gross margin percentage increased slightly on efficiency gains in Microsoft 365 Commercial cloud",
        ],
    ),
    (
        "MSFT",
        "Part II, Item 1A",
        "What AI-related cybersecurity risks does Microsoft describe in its latest 10-Q risk factors?",
        [
            "Threat actors use AI to increase the speed and scale of attacks, automate reconnaissance and exploit vulnerabilities faster",
            "Attacks may use, manipulate or misuse AI models or agents",
            "Microsoft's capabilities may not detect some new attack methods, which could persist undetected",
        ],
    ),
    (
        "AAPL",
        "Item 1A",
        "What does Apple's 10-K say about its reliance on outsourcing partners for manufacturing?",
        [
            "Substantially all manufacturing is done by outsourcing partners, primarily in China mainland, India, Japan, South Korea, Taiwan and Vietnam",
            "A significant concentration of manufacturing is with a small number of partners, often in single locations",
            "This reduces Apple's direct control over production and can affect quality or quantity",
        ],
    ),
    (
        "AAPL",
        "Item 7",
        "How did Apple's net sales by product category change in fiscal 2024 compared with fiscal 2023?",
        [
            "iPhone net sales were roughly flat at about $201.2 billion",
            "Services grew 13% to about $96.2 billion",
            "Mac grew 2%, while iPad fell 6% and Wearables, Home and Accessories fell 7%",
            "Total net sales grew 2% to about $391.0 billion",
        ],
    ),
    (
        "JPM",
        "Item 7",
        "How does JPMorgan Chase govern and measure its structural interest rate risk?",
        [
            "Structural interest rate risk arises from traditional banking activities: loans, deposits, debt issuance and the investment securities portfolio",
            "Treasury and CIO calculate the structural interest rate risk profile and review it with senior management",
            "The CTC Risk Committee sets the interest rate risk policy and limits, approved by the Board Risk Committee",
            "An independent risk function reporting to the CTC CRO oversees it; earnings-at-risk is a key measure",
        ],
    ),
    (
        "INTC",
        "Item 7",
        "What does Intel's 2025 10-K say about its R&D and MG&A expenses and why they changed?",
        [
            "Total R&D and MG&A expenses were $18.4 billion in 2025, down 17% from 2024",
            "R&D fell mainly from lower payroll after headcount reductions under the 2025 and 2024 Restructuring Plans",
            "Intel expects these expenses to decrease further in 2026, helped by the Altera divestiture",
        ],
    ),
    (
        "AMD",
        "Item 7",
        "What does AMD's fiscal 2025 MD&A say about its Data Center and AI GPU business?",
        [
            "Accelerating growth in the Data Center segment was a priority in 2025",
            "Demand for Instinct MI350X Series GPUs was strong from hyperscalers, OEMs and ODMs",
            "AMD committed to an annual cadence of AI GPU product launches",
        ],
    ),
    (
        "AVGO",
        "Item 7",
        "Why did Broadcom's segment revenues change in fiscal 2024?",
        [
            "Infrastructure software revenue rose 181% to about $21.5 billion, primarily from VMware",
            "Semiconductor solutions revenue rose 7% on AI networking demand",
            "Lower broadband and server storage demand partly offset semiconductor growth",
        ],
    ),
    (
        "QCOM",
        "Item 1A",
        "What risks does Qualcomm's 10-K describe about depending on a small number of significant customers?",
        [
            "Losing a significant customer or reduced purchases could harm results",
            "Customers may switch to their own integrated circuits or competitors' products",
            "Engineering work on customer specifications may not lead to purchase commitments and can create non-recoverable costs",
        ],
    ),
    (
        "TXN",
        "Item 7",
        "What does Texas Instruments describe as the best measure of its progress, and what is its strategy built on?",
        [
            "The best metric is growth of free cash flow per share over the long term",
            "The strategy has three elements, starting with a business model focused on analog and embedded processing",
            "Four sustainable competitive advantages, including a strong manufacturing and technology foundation that lowers costs and gives supply chain control",
        ],
    ),
    (
        "MU",
        "Item 7",
        "How does Micron's fiscal 2025 MD&A describe industry conditions?",
        [
            "AI-driven demand is accelerating and outpacing industry supply",
            "DRAM pricing, volumes and margins improved substantially compared with 2024",
            "Micron's fiscal year is the 52- or 53-week period ending the Thursday closest to August 31",
        ],
    ),
    (
        "ADI",
        "Item 7",
        "Why did Analog Devices' revenue decline in fiscal 2024?",
        [
            "Revenue fell 23% on weaker macroeconomic trends",
            "The decline was most pronounced in Industrial as customers reduced inventory",
            "Communications fell on infrastructure deployment timing; Automotive and Consumer declined less",
        ],
    ),
    (
        "ORCL",
        "Item 7",
        "How did Oracle's revenue by offering change in fiscal 2024?",
        [
            "Cloud services revenue grew 25% to about $19.8 billion",
            "License support revenue grew about 1%",
            "Cloud license and on-premise license revenue fell 12%",
            "Total revenues grew 8% to about $44.5 billion",
        ],
    ),
    (
        "CRM",
        "Item 1",
        "How does Salesforce's 10-K describe its Agentforce 360 Platform?",
        [
            "It lets companies build workflows, applications and AI agents on a single platform",
            "It offers no-code and low-code development tools",
            "A built-in Trust Layer sets guardrails on what AI agents do with customer data",
            "It runs on Hyperforce, which helps customers manage data governance and compliance locally",
        ],
    ),
    (
        "ADBE",
        "Item 1A",
        "How does Adobe's 10-K describe the competitive risks it faces?",
        [
            "Its markets are rapidly evolving and intensely competitive, and competition is expected to intensify",
            "Competitors range from large diversified companies with big resources to small specialized firms",
            "Competitors may develop or acquire similar products that win faster acceptance",
        ],
    ),
    (
        "NOW",
        "Item 7",
        "Why did ServiceNow's cost of subscription revenues rise in 2024, and what happened to its gross margin?",
        [
            "Cost of subscription revenues rose $336 million, from headcount and costs to support subscription growth including regulated markets",
            "Data center capacity expansion costs rose about $85 million",
            "Subscription gross margin held at 82%; total gross profit grew 23%",
        ],
    ),
    (
        "INTU",
        "Item 7",
        "What does Intuit's fiscal 2026 10-K say about its cash and investments?",
        [
            "Cash, cash equivalents and investments were $7.2 billion at July 31, 2026",
            "That was up $2.6 billion from July 31, 2025, driven by cash from operations, partly offset by financing and investing",
            "Intuit is assessing the effects of the OBBBA tax legislation on fiscal 2027 and beyond",
        ],
    ),
    (
        "WDAY",
        "Item 1A",
        "What risks does Workday's 10-K describe from using open source AI models?",
        [
            "Open source AI models may be trained on data of uncertain provenance, including copyrighted or confidential information",
            "This could lead to copyright infringement or other claims",
            "Open source AI components can be subject to data poisoning and other security vulnerabilities",
        ],
    ),
    (
        "ADSK",
        "Item 7",
        "What does Autodesk's fiscal 2025 10-K say about its remaining performance obligations and billing changes?",
        [
            "RPO was about $6.9 billion at January 31, 2025, up from about $6.1 billion",
            "Current RPO was about $4.5 billion",
            "Customers are moving from multi-year contracts billed upfront to annual billing",
            "EBA sales are seasonally higher in the fourth quarter",
        ],
    ),
    (
        "BAC",
        "Item 7",
        "How did Bank of America's Global Markets segment perform in 2025?",
        [
            "Net income rose $489 million to $6.1 billion",
            "Revenue rose $2.3 billion to $24.1 billion on sales and trading, leveraged finance sales and investment banking fees",
            "Sales and trading revenue rose $2.1 billion in both Equities and FICC",
            "Noninterest expense rose $1.5 billion to $15.4 billion",
        ],
    ),
    (
        "WFC",
        "Item 1A",
        "Where does Wells Fargo's 10-K Item 1A say its risk factors can be found?",
        [
            "Item 1A incorporates the risk factors by reference",
            "They are in Item 1 and in the Annual Report to Shareholders under 'Financial Review - Risk Factors'",
        ],
    ),
    (
        "C",
        "Item 1A",
        "What regulatory actions related to data quality does Citigroup's 10-K describe?",
        [
            "On July 10, 2024 the Federal Reserve entered a Civil Money Penalty Consent Order with Citigroup, and the OCC one with Citibank",
            "The OCC amended its 2020 Consent Order with Citibank",
            "Regulators found ongoing deficiencies in data quality management",
            "Citibank must formalize a process to make sure enough resources go to compliance",
        ],
    ),
    (
        "GS",
        "Item 7",
        "How did Goldman Sachs' FICC net revenues change in 2023 and why?",
        [
            "FICC net revenues were $12.06 billion, 18% lower than 2022",
            "FICC intermediation fell, especially in currencies and commodities, with lower client activity",
            "Higher mortgages and credit products revenues partly offset the decline",
        ],
    ),
    (
        "MS",
        "Item 7",
        "What were Morgan Stanley Wealth Management's net new assets in 2023, compared with prior years?",
        [
            "Net new assets were $282.3 billion in 2023",
            "That was down from $311.3 billion in 2022 and $437.7 billion in 2021",
        ],
    ),
    (
        "USB",
        "Part I, Item 2",
        "How does U.S. Bancorp's 10-Q describe its net interest income sensitivity analysis?",
        [
            "It evaluates all assets, liabilities and off-balance sheet instruments under rate scenarios that vary in direction, size and speed",
            "It includes assumptions on loan and deposit volumes and pricing",
            "Deposit beta is how much interest-bearing deposit rates change with short-term market rates",
        ],
    ),
    (
        "PNC",
        "Item 7",
        "What risks does PNC's 10-K describe about growing through acquisitions?",
        [
            "Acquisitions bring risks from the acquired business and entry into new businesses or markets",
            "Integration of acquired businesses and regulatory issues are risks",
            "Competition and technological change affect customer acquisition, deposits and revenue",
        ],
    ),
    (
        "NVDA",
        "Part I, Item 2",
        "What drove NVIDIA's revenue in the second quarter of fiscal 2027, according to its 10-Q?",
        [
            "Revenue was $96.2 billion, up 106% year over year",
            "Data Center revenue was $89.0 billion, up 117%, driven by the Blackwell Ultra ramp",
            "Hyperscale revenue more than doubled and ACIE revenue rose 138%",
        ],
    ),
    (
        "MSFT",
        "Part I, Item 2",
        "How did Microsoft change its segment reporting in fiscal 2027?",
        [
            "In September 2026 Microsoft announced new reportable segments",
            "The segments are Agents and Infra, and Devices and Consumer",
            "Agents and Infra combines applications and agents, the multi-model system and Azure infrastructure",
        ],
    ),
]

R = [
    (
        "refuse-tesla",
        ["TSLA"],
        "What was Tesla's revenue in fiscal 2025?",
        "company not in the universe",
    ),
    (
        "refuse-amazon-aws",
        ["AMZN"],
        "What was Amazon Web Services' operating income in 2025?",
        "company not in the universe",
    ),
    (
        "refuse-meta-dau",
        ["META"],
        "How many daily active users did Meta report in Q3 2026?",
        "company not in the universe",
    ),
    (
        "refuse-netflix",
        ["NFLX"],
        "How many paid memberships did Netflix have at the end of 2025?",
        "company not in the universe",
    ),
    (
        "refuse-snowflake",
        ["SNOW"],
        "What was Snowflake's product revenue growth last year?",
        "company not in the universe",
    ),
    (
        "refuse-samsung",
        ["SSNLF"],
        "What was Samsung Electronics' operating profit in 2025?",
        "company not in the universe (foreign filer)",
    ),
    (
        "refuse-gs-gross-profit",
        ["GS"],
        "What was Goldman Sachs' gross profit in its latest fiscal year?",
        "banks report no gross profit",
    ),
    (
        "refuse-jpm-gross-margin",
        ["JPM"],
        "What was JPMorgan Chase's gross margin in 2025?",
        "banks report no gross profit",
    ),
    (
        "refuse-wfc-op-margin",
        ["WFC"],
        "What was Wells Fargo's operating margin in 2025?",
        "banks report no operating income",
    ),
    (
        "refuse-nvda-fy2030",
        ["NVDA"],
        "What was NVIDIA's revenue in fiscal 2030?",
        "period not filed yet",
    ),
    (
        "refuse-adbe-forecast",
        ["ADBE"],
        "What revenue does Adobe forecast for fiscal 2028?",
        "no forecasts in the corpus",
    ),
    (
        "refuse-msft-ceo-pay",
        ["MSFT"],
        "What was Microsoft's CEO's total compensation in fiscal 2026?",
        "proxy statements are not ingested",
    ),
    (
        "refuse-intc-2020-price",
        ["INTC"],
        "What was Intel's closing stock price on March 16, 2020?",
        "only recent prices are in the price file",
    ),
    (
        "refuse-qcom-fy2030-div",
        ["QCOM"],
        "What dividend per share did Qualcomm pay in fiscal 2030?",
        "period not filed yet",
    ),
    (
        "refuse-aapl-fy2005",
        ["AAPL"],
        "What was Apple's net income in fiscal 2005?",
        "before XBRL; not in the corpus",
    ),
]


def main() -> None:
    qual = []
    for i, (t, sec, question, points) in enumerate(Q, 1):
        qual.append(
            Item(
                id=f"qual-{i:02d}-{t}",
                category="qualitative",
                question=question,
                tickers=[t],
                sections=[ExpectedSection(ticker=t, section=sec)],
                key_points=points,
            )
        )
    refuse = [Item(id=i, category="refusal", question=q, tickers=t, reason=r) for i, t, q, r in R]
    save(DATASETS / "qualitative.jsonl", qual)
    save(DATASETS / "refusal.jsonl", refuse)
    print(f"{len(qual)} qualitative, {len(refuse)} refusal items written")


if __name__ == "__main__":
    main()
