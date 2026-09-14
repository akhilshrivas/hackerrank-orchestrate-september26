# Token Usage Report

## Summary
Fully deterministic financial decision engine. No LLM calls.

| Metric | Value |
|--------|-------|
| Model Provider | None (deterministic) |
| Model Name | N/A |
| Model Calls | 0 |
| Input Tokens | 0 |
| Output Tokens | 0 |
| Total Tokens | 0 |
| Avg Tokens/Request | 0 |
| Estimated Total Cost | $0.00 |
| Estimated Per-Request Cost | $0.00 |

## Notes
- Image amounts: pre-extracted and cached in IMAGE_AMOUNTS dict
- Message parsing: rule-based regex with tightened currency-prefix requirements
- Recurrence detection: monthly aggregation of category totals (v4)
- Salary projection: recurring from scheduled event or historical payroll
- No API keys required
- Requests processed: 250
