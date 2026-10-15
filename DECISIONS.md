# DECISIONS.md

1. **Indexes.** `employees.emp_code` is unique because it is the business identifier. `attendance_logs(emp_code, date)` is unique to enforce one attendance record per employee/day even when requests race. Date/employee and date/status indexes support attendance listing and month-based analytics. The department/join-date index supports department headcount queries. I would avoid adding indexes without a query or constraint that justifies their write and storage cost.

2. **Punch-in race.** Both requests may reach the insert path, but the unique compound index is enforced by MongoDB. One insert succeeds; the other raises `DuplicateKeyError`, which the API converts to HTTP 409. A Python check alone would not be safe because both requests could pass it.

3. **Ties.** The leaderboard uses `$rank` on total late minutes before applying the rank limit. Employees tied at the cutoff share a rank and are all returned; the next rank is skipped. Employee code is only a secondary display sort, not part of the ranking expression.

4. **Headcount.** The department summary starts from employees who joined by the month's final day, then looks up each employee's attendance logs. Employees with no logs remain in the pipeline and count toward headcount.

5. **100x data.** I would measure explain plans and execution statistics, then consider pre-aggregated daily summaries, retention/archive policies, and pagination/caching where appropriate. I would not add indexes blindly; each should be justified by observed query patterns.
