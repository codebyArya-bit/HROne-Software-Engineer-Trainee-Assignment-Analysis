# Starter code review

Defects below refer to the **supplied starter**, before replacement. Inputs and symptoms make each finding reproducible.

| # | Where | Defect | Reproduction / symptom | Fix |
|---|---|---|---|---|
| 1 | `health` | Returns 200 with MongoDB offline. | Stop MongoDB, call `/health`. | Run `ping`, return 503 on failure. |
| 2 | `create_employee` | Check-then-insert is not concurrency safe. | Parallel creates of `EMP1234` can both succeed. | Unique index and catch `DuplicateKeyError` (409). |
| 3 | `create_employee` | No contract validation of code, dates, shifts, email, lengths or unequal shift times. | `shift_start="25:99"` or equal shift times accepted. | Constrained Pydantic model and strict date parser. |
| 4 | `create_employee` | `created_at` is naive and returned as datetime, not epoch milliseconds. | 201 response sends a timestamp string. | UTC BSON datetime, serialize integer milliseconds. |
| 5 | `list_employees` | `skip = page * page_size` skips the first page. | `page=1&page_size=2` begins at employee 3. | `(page-1)*page_size`. |
| 6 | `list_employees` | `total` ignores department filter. | Filter Engineering with employees from Sales. | `count_documents(filter)`. |
| 7 | `list_employees` | Missing deterministic `emp_code` sorting. | Shuffled inserts produce insertion-order results. | Indexed ascending sort. |
| 8 | `list_employees` | Allows invalid page/page_size and no max 100. | `page=0` or `page_size=500`. | Query constraints yield 422. |
| 9 | `punch_in` | Unknown employee dereference raises 500. | `emp_code="EMP9999"`. | Explicit 404. |
| 10 | `punch_in` | Seconds-looking, floats, strings and out-of-range timestamps were not rejected. | `punched_at=1783312500`, or `"1783312500000"`. | Strict integer millisecond bounds; truncate seconds. |
| 11 | `punch_in` | Uses naive local timezone and derives wrong IST dates. | Punch close to IST midnight. | Timezone-aware UTC/IST conversion. |
| 12 | `punch_in` | Ignores overnight-shift previous-date rule. | 22:00–06:00 shift, punch at 02:00 IST. | Resolve prior attendance day for pre-end punches. |
| 13 | `punch_in` | Unvalidated status permits `LEAVE` with a punch. | POST `status="LEAVE"`. | Presence-status enum. |
| 14 | `punch_in` | Check-then-insert races under concurrency. | Two simultaneous same-day POSTs. | Unique `(emp_code,date)` index with 409 mapping. |
| 15 | `punch_in` | Returns extra `id`, including internal MongoDB identity. | Inspect successful JSON. | Strict record projection without `_id` or `id`. |
| 16 | `compute_late_minutes` | Uses naive `replace` and truncates before testing exact grace threshold. | Shift 09:30, punch 09:40:01 reports 0 instead of 10. | Seconds comparison (>600), then floor minutes against IST shift start. |
| 17 | `compute_work_hours` | Python `round` is half-even, not half-up. | Exactly X.XX5 hours. | Decimal `ROUND_HALF_UP`. |
| 18 | `compute_overtime` | Doesn't handle overnight next-day shift end. | 22:00–06:00 shift, out at 06:40. | Next-day shift-end datetime. |
| 19 | `compute_overtime` | Counts overtime under 30 minutes. | Shift end 18:30, out 18:45 gives 15. | Only record if whole-minute overtime >=30. |
| 20 | `list_attendance` | Fetches full collection into Python, then sorts/slices. | 100k records incur full scan, RAM and slow pagination. | Indexed MongoDB sort, skip, limit. |
| 21 | `list_attendance` | Sorts by date only; no emp-code tiebreak. | Several employees on same date. | Date desc, employee code asc. |
| 22 | `list_attendance` | Returns internal `id` and datetime rather than epoch integers. | Inspect records and audit history. | Explicit field projection and recursive history timestamps. |
| 23 | `list_attendance` | No status or calendar-date validation, no inverted-range 422. | `date_from` after `date_to`, arbitrary `status`. | Validated query types and comparisons. |
| 24 | General startup | No idempotent application indexes. | Hidden database has only `_id`. | Add unique and performance indexes in lifespan. |

**Examined and retained:** The choice of FastAPI/Pydantic and PyMongo was compatible with the contract. Keeping date and shift as strings is intentional: they are IST calendar values, not instants. The starter's use of a `history` array initialized to `[]` is correct; only manual corrections append entries. `status=PRESENT` and default pagination size 20 match the contract.