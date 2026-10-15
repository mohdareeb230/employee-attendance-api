# REVIEW.md

Review of the supplied starter `app/main.py` and the corrections made in this version. Please verify each item against the final code and add any issues you discover while testing.

| # | Where | What is wrong | How you'd notice it | How it was fixed |
|---|---|---|---|---|
| 1 | `health` | It returned 200 without contacting MongoDB. | Stop MongoDB and request `/health`. | Ping MongoDB and return 503 on failure. |
| 2 | `create_employee` | Duplicate checking before insertion is race-prone; duplicate codes were not database-enforced. | Send parallel creates with the same `emp_code`. | Unique index and `DuplicateKeyError` → 409. |
| 3 | `create_employee` | No input constraints or shift-time validation. | Submit malformed codes, emails, dates or equal shift times. | Pydantic validation and a model validator. |
| 4 | `list_employees` | `skip = page * page_size` skipped page one; total ignored filters. | Request page 1 and a department filter. | Use `(page - 1) * page_size` and count with the same filter. |
| 5 | `list_employees` | No page/page-size bounds or stable sort. | Use page 0, page_size 101 or compare repeated pages. | Validate pagination and sort by `emp_code`. |
| 6 | `compute_late_minutes` | Naive datetime handling and no explicit attendance-day anchor can miscalculate dates, especially overnight shifts. | Test a post-midnight punch-in for a night shift. | Use timezone-aware instants and the attendance date's shift anchor. |
| 7 | `compute_late_minutes` | It used float/int truncation implicitly and did not explicitly truncate timestamps to whole seconds. | Test 09:40:01 for a 09:30 shift. | Truncate instants and apply the strict 600-second grace rule. |
| 8 | `compute_work_hours` | Python `round()` uses ties-to-even rather than required half-up rounding. | Test durations whose hour value lands on a half-cent boundary. | Use `Decimal` with `ROUND_HALF_UP`. |
| 9 | `compute_overtime` | It always used the attendance date and did not correctly anchor an overnight shift end to the following day; it also lacked the 30-minute threshold. | Test 22:00–06:00 and 29 vs 30 minutes overtime. | Compute the shift-end instant with overnight handling and apply the threshold. |
| 10 | `punch_in` | Unknown employees caused an unhandled lookup failure; timestamps were converted to naive local datetimes, making timezone handling inconsistent and skipping strict timestamp validation. | Use an unknown code and a millisecond timestamp. | Return 404 and convert epoch milliseconds to UTC. |
| 11 | `punch_in` | Check-then-insert did not guarantee uniqueness under concurrency; status was not constrained. | Send duplicate simultaneous requests or an invalid status. | Unique compound index plus request validation. |
| 12 | `list_attendance` | It loaded all matching records into Python, then sorted and paginated in memory. | Seed many records and inspect memory/latency. | Apply database sort, skip and limit; count with the same filter. |
| 13 | `list_attendance` | It returned MongoDB `_id` as a string and omitted defaults for missing optional fields. | Inspect responses for `_id`, missing `history` or `half_day`. | Omit `_id` and normalize documented defaults. |
| 14 | All timestamp responses | BSON datetimes were not consistently converted to epoch milliseconds, including nested audit-history values. | Inspect `created_at`, punch times and history changes. | Centralize response serialization. |
| 15 | Missing endpoints | Punch-out, regularization, analytics and explain were unimplemented. | Call each documented route. | Implement the remaining routes and aggregation pipelines. |

**Not considered a defect:** storing `date` and `joined_on` as `YYYY-MM-DD` strings is intentional. They represent calendar dates, not instants, and lexicographic range matching is valid for the fixed ISO format.
