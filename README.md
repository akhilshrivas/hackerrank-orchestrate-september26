# Buy or Wait? — HackerRank Orchestrate Sept 2026

## Overview
This is a deterministic financial decision engine built for the HackerRank Orchestrate September 2026 challenge. 
It analyzes a user's financial profile, past transactions, pending events, and unstructured messages to deterministically calculate the `amount_safe_to_pay` and recommend a payment plan (Full Payment, Wait, Installments, Partial Payment, or Not Recommended).

## Key Features
- **Deterministic Forecast Engine**: Projects recurring expenses and income forward 90 days to simulate daily cash flow, ensuring the user's balance never drops below their minimum threshold.
- **Message Overrides**: Parses unstructured messages (e.g., salary increases, final payroll, rent changes) to adjust the cash flow projection.
- **Spending Changes**: Automatically evaluates `stoppable` and `reducible` expenses (up to 3 combinations) if a request is otherwise unaffordable.
- **Multi-Currency Support**: Uses a graph-based search or direct table lookup to evaluate exchange rates, converting everything to the user's home currency.
- **Missing Amounts**: Falls back to OCR-extracted values from provided receipt images when event amounts are missing.

## Requirements
- Python 3.9+
- Standard libraries only (no external dependencies required, except standard testing tools if added).

## Execution
To process the datasets and generate `output.csv`:
```bash
python code/main.py
```

## Structure
- `code/main.py`: The complete engine.
- `code/evaluation/usage_report.md`: Resource and token usage report.
- `dataset/`: Contains the CSV input data.
- `output.csv`: Generated output matching the required problem schema.
