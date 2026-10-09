# Design decisions

1. **Indexes:** Unique `employees.emp_code` and `(attendance_logs.emp_code, date)` enforce identity. The date/code and status/date/code indexes support descending attendance lists and monthly scans; department/joined-on indexes support headcount and filtered employee lists. An employee/punch-in index finds the latest eligible record for punch-out. I rejected a standalone `late_minutes` index: leaderboard grouping is month-driven, and that index would not avoid grouping.

2. **Simultaneous punch-ins:** Both may read the employee, compute identical attendance dates and attempt insertion. MongoDB's unique compound index accepts the first insert and rejects the second with `DuplicateKeyError`; the API maps that rejection to HTTP 409. No check-then-insert race remains.

3. **Ties:** `$setWindowFields` with `$rank` ranks by total late minutes before filtering by rank. If rank 10 has three employees, all three appear at `limit=10`; ordering within the tie is by employee code.

4. **No-log headcount:** Department aggregation starts from eligible employees, then left-joins their attendance. A missing log contributes zero to metrics without removing the employee from headcount.

5. **100x scale:** I would precompute incrementally refreshed per-employee daily/monthly summaries while retaining immutable raw events and correction history. This would reduce repeated `$lookup` and grouping work without weakening auditability.