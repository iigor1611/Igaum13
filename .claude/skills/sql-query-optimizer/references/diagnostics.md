# SQL Server diagnostics: Query Store, DMVs, waits, sniffing, IQP

## Finding the expensive queries

### Query Store (preferred: survives restarts, keeps history)

```sql
-- Enable / tune (on by default for new DBs in 2022+ and Azure SQL)
ALTER DATABASE CURRENT SET QUERY_STORE = ON
  (OPERATION_MODE = READ_WRITE, QUERY_CAPTURE_MODE = AUTO, MAX_STORAGE_SIZE_MB = 2048,
   INTERVAL_LENGTH_MINUTES = 15, SIZE_BASED_CLEANUP_MODE = AUTO);

-- Top queries by total CPU, last 24h
SELECT TOP (25) q.query_id, qt.query_sql_text,
       SUM(rs.count_executions)                                    AS execs,
       SUM(rs.avg_cpu_time * rs.count_executions) / 1000.0         AS total_cpu_ms,
       SUM(rs.avg_logical_io_reads * rs.count_executions)          AS total_reads,
       MAX(rs.max_duration) / 1000.0                               AS max_duration_ms,
       COUNT(DISTINCT p.plan_id)                                   AS plan_count
FROM sys.query_store_runtime_stats rs
JOIN sys.query_store_runtime_stats_interval i ON i.runtime_stats_interval_id = rs.runtime_stats_interval_id
JOIN sys.query_store_plan p ON p.plan_id = rs.plan_id
JOIN sys.query_store_query q ON q.query_id = p.query_id
JOIN sys.query_store_query_text qt ON qt.query_text_id = q.query_text_id
WHERE i.start_time >= DATEADD(HOUR, -24, SYSUTCDATETIME())
GROUP BY q.query_id, qt.query_sql_text
ORDER BY total_cpu_ms DESC;
```
`plan_count > 1` together with a large spread in duration points to plan instability (sniffing, or stats changes). Compare the plans:
```sql
SELECT p.plan_id, p.is_forced_plan, p.last_execution_time,
       AVG(rs.avg_duration)/1000.0 avg_ms, AVG(rs.avg_logical_io_reads) avg_reads, SUM(rs.count_executions) execs,
       TRY_CAST(p.query_plan AS xml) plan_xml
FROM sys.query_store_plan p JOIN sys.query_store_runtime_stats rs ON rs.plan_id = p.plan_id
WHERE p.query_id = @query_id GROUP BY p.plan_id, p.is_forced_plan, p.last_execution_time, p.query_plan;
```
Waits per query (2017+): `sys.query_store_wait_stats` (grouped into categories: CPU, Lock, Buffer IO, Memory, Parallelism, and so on).

**Remedies without changing code:**
```sql
EXEC sys.sp_query_store_force_plan @query_id = 42, @plan_id = 7;         -- pin good plan
EXEC sys.sp_query_store_unforce_plan @query_id = 42, @plan_id = 7;
-- Query Store hints (2022+, Azure SQL): inject hints without touching code
EXEC sys.sp_query_store_set_hints @query_id = 42,
     @query_hints = N'OPTION (RECOMPILE)';   -- or USE HINT(...), MAXDOP 1, MAX_GRANT_PERCENT=10, etc.
EXEC sys.sp_query_store_clear_hints @query_id = 42;
```
Monitor forced plans: `sys.query_store_plan.force_failure_count` and `last_force_failure_reason_desc`. A forced plan fails when an index it relies on is dropped. **Automatic tuning** (`ALTER DATABASE CURRENT SET AUTOMATIC_TUNING (FORCE_LAST_GOOD_PLAN = ON)`) forces the last good plan when a regression is detected (Enterprise/Azure).

### Plan cache DMVs (since the last restart or eviction)

```sql
SELECT TOP (25)
       qs.execution_count,
       qs.total_worker_time/1000 AS total_cpu_ms,
       qs.total_logical_reads, qs.total_elapsed_time/1000 AS total_ms,
       qs.total_grant_kb, qs.total_spills,                    -- 2016 SP2+/2017+
       SUBSTRING(st.text, qs.statement_start_offset/2 + 1,
         (CASE qs.statement_end_offset WHEN -1 THEN DATALENGTH(st.text) ELSE qs.statement_end_offset END
          - qs.statement_start_offset)/2 + 1) AS stmt,
       qp.query_plan
FROM sys.dm_exec_query_stats qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
OUTER APPLY sys.dm_exec_query_plan(qs.plan_handle) qp
ORDER BY qs.total_worker_time DESC;   -- or total_logical_reads, total_spills
```
Also: `sys.dm_exec_procedure_stats`, `sys.dm_exec_function_stats`, `sys.dm_exec_trigger_stats`.
Many single-use ad hoc plans (`objtype = 'Adhoc'` with `usecounts = 1` in `sys.dm_exec_cached_plans`) call for `optimize for ad hoc workloads` and parameterization in the app. Forced parameterization is a last resort.

### What's running right now

```sql
SELECT r.session_id, r.status, r.command, r.wait_type, r.wait_time, r.blocking_session_id,
       r.cpu_time, r.logical_reads, r.granted_query_memory*8 AS grant_kb, r.dop,
       DB_NAME(r.database_id) db, st.text, qp.query_plan
FROM sys.dm_exec_requests r
CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) st
OUTER APPLY sys.dm_exec_query_plan(r.plan_handle) qp
WHERE r.session_id <> @@SPID AND r.session_id > 50;
-- live actual plan of a running query (2016 SP1+/2019 default):
SELECT * FROM sys.dm_exec_query_statistics_xml(@session_id);
```
Community tools worth recommending: `sp_WhoIsActive` (Adam Machanic), and the First Responder Kit (`sp_Blitz`, `sp_BlitzCache`, `sp_BlitzIndex`, `sp_BlitzWho`, `sp_BlitzLock`).

## Wait statistics: where the server spends time

```sql
SELECT TOP (20) wait_type, waiting_tasks_count, wait_time_ms/1000.0 wait_s,
       signal_wait_time_ms/1000.0 signal_s,
       100.0 * wait_time_ms / SUM(wait_time_ms) OVER () pct
FROM sys.dm_os_wait_stats
WHERE wait_type NOT IN (  -- benign/idle waits
 'SLEEP_TASK','BROKER_TASK_STOP','BROKER_TO_FLUSH','SQLTRACE_BUFFER_FLUSH','CLR_AUTO_EVENT','CLR_MANUAL_EVENT',
 'LAZYWRITER_SLEEP','CHECKPOINT_QUEUE','WAITFOR','XE_TIMER_EVENT','XE_DISPATCHER_WAIT','FT_IFTS_SCHEDULER_IDLE_WAIT',
 'LOGMGR_QUEUE','REQUEST_FOR_DEADLOCK_SEARCH','DIRTY_PAGE_POLL','HADR_FILESTREAM_IOMGR_IOCOMPLETION',
 'SP_SERVER_DIAGNOSTICS_SLEEP','QDS_PERSIST_TASK_MAIN_LOOP_SLEEP','QDS_ASYNC_QUEUE','SOS_WORK_DISPATCHER',
 'BROKER_EVENTHANDLER','ONDEMAND_TASK_QUEUE','SLEEP_SYSTEMTASK','SQLTRACE_INCREMENTAL_FLUSH_SLEEP',
 'QDS_CLEANUP_STALE_QUERIES_TASK_MAIN_LOOP_SLEEP','PREEMPTIVE_XE_DISPATCHER','PWAIT_EXTENSIBILITY_CLEANUP_TASK')
  AND wait_type NOT LIKE 'SLEEP%' AND waiting_tasks_count > 0
ORDER BY wait_time_ms DESC;
```
These are cumulative since restart. Take two snapshots and diff them for a current picture. Per session: `sys.dm_exec_session_wait_stats`. Azure SQL DB: `sys.dm_db_wait_stats`.

| Wait | Meaning | Look at |
|---|---|---|
| `PAGEIOLATCH_SH/EX` | Reading data pages from disk | Scans, missing indexes, memory pressure (low Page Life Expectancy), slow storage |
| `CXPACKET` / `CXCONSUMER` | Parallel query coordination | Usually a symptom: big scans, skew. Set cost threshold for parallelism (≈50) and MAXDOP (≤8, ≤ cores per NUMA node). Don't just set MAXDOP 1. |
| `SOS_SCHEDULER_YIELD` | CPU pressure, spinning on CPU | High-CPU queries (scans in memory, UDFs, bad joins) |
| `LCK_M_*` | Blocking on locks | Blocking chain. See below. |
| `RESOURCE_SEMAPHORE` | Waiting for a memory grant | Oversized grants (`SELECT *`, bad estimates, sorts). Memory grant feedback, `MAX_GRANT_PERCENT`. |
| `RESOURCE_SEMAPHORE_QUERY_COMPILE` | Compile memory throttling | Huge ad hoc queries, compile storms |
| `WRITELOG` | Log flush latency | Many tiny commits (batch them), slow log disk, `DELAYED_DURABILITY` if acceptable |
| `PAGELATCH_EX/SH` (tempdb 2:1:x) | tempdb allocation contention | More tempdb data files (1 per core, up to 8), memory-optimized tempdb metadata (2019+) |
| `PAGELATCH_EX` (user DB, last page) | Sequential insert hotspot | `OPTIMIZE_FOR_SEQUENTIAL_KEY`, key design |
| `ASYNC_NETWORK_IO` | Client not consuming results fast enough | App fetching row by row or returning too many rows. Not a SQL problem. |
| `THREADPOOL` | Out of worker threads | Massive blocking or parallelism. Urgent. |
| `HADR_SYNC_COMMIT` | AG synchronous replica latency | Network or secondary disk |

## Parameter sniffing decision tree

Symptom: the same query is sometimes fast and sometimes slow; or "fast in SSMS, slow in app" (also check different SET options there); or it got fixed by `sp_recompile` or by a stats update (which forced a recompile).

1. **Confirm.** Compare `ParameterCompiledValue` with `ParameterRuntimeValue` in the actual plan. Query Store shows several plans with very different averages for one query_id.
2. **Check the data skew.** Histogram (`DBCC SHOW_STATISTICS ... WITH HISTOGRAM`): one value with 2M rows and others with 10.
3. **On 2022+ / compat 160: PSP optimization** may already create a dispatcher with plan variants for each cardinality bucket (equality predicates only, up to 3 variants). Look for `PLAN PER VALUE` in the query text or `Dispatcher` in the plan XML. Check whether it's actually kicking in.
4. **Pick a remedy** (from least to most invasive):

| Remedy | When | Cost |
|---|---|---|
| `OPTION (RECOMPILE)` on the statement | Runs infrequently (< a few per second), or catch-all patterns | Compile CPU on every execution. Plan not cached in DMVs (Query Store still captures it). |
| `OPTIMIZE FOR (@p = typical_value)` | One value's plan is good enough for all | Breaks if the data distribution changes |
| `OPTIMIZE FOR UNKNOWN` / `DISABLE_PARAMETER_SNIFFING` | An average plan is acceptable for all | Mediocre for both extremes |
| Branching: `IF @p = big THEN EXEC proc_big ELSE EXEC proc_small` | Few known "whale" values | More code |
| Dynamic SQL with a comment tag per bucket to force separate plans | Several skew buckets | Complexity |
| Query Store force plan, or Query Store hint | Can't change code, one good plan exists | Must be monitored |
| Fix the underlying cause: covering index | The bad plan is a lookup or scan. With a covering index both plans become cheap and the problem disappears. | Index write overhead. **Often the best fix.** |

Avoid `WITH RECOMPILE` on the whole procedure (it recompiles every statement), and avoid "fixing" it by clearing the whole plan cache.

## Intelligent Query Processing (IQP) by compatibility level

| Feature | Compat | Edition notes | What it fixes |
|---|---|---|---|
| Batch mode adaptive joins | 140 | Enterprise | Hash vs. NL chosen at runtime |
| Batch mode memory grant feedback | 140 | | Spills and excessive grants on repeated runs |
| Interleaved execution for MSTVFs | 140 | | MSTVF fixed estimate (read-only queries) |
| Row mode memory grant feedback | 150 | Enterprise | Same, for row mode |
| Batch mode on rowstore | 150 | Enterprise | Batch speed for analytics without columnstore |
| Scalar UDF inlining | 150 | All | Per-row UDF execution, serial plans |
| Table variable deferred compilation | 150 | All | 1-row estimate |
| `APPROX_COUNT_DISTINCT` | any (2019+) | All | Fast distinct counts |
| Memory grant feedback: percentile + persistence | 160 | | Oscillating grants. Feedback survives eviction (stored in Query Store). |
| **Parameter Sensitive Plan (PSP) optimization** | 160 | All | Sniffing on skewed equality predicates |
| **DOP feedback** | 160 (on by default in 170) | Enterprise | Excess parallelism |
| **CE feedback** | 160 | All | Repeated misestimates (correlation, join containment, row goals) |
| Optimized plan forcing | 160 | | Compile time for forced plans |
| Approximate percentiles | 160 | | `APPROX_PERCENTILE_CONT/DISC` |
| Optional Parameter Plan Optimization (OPPO) | 170 (2025) | | `@p IS NULL OR col = @p` patterns |
| Optimized locking (TID locking, lock after qualification) | 2025 / Azure SQL (needs ADR) | | Lock memory, escalation, blocking |

Notes:
- Several of these (memory grant persistence, CE feedback, DOP feedback) **require Query Store to be enabled** in READ_WRITE mode.
- Turn an individual feature off with database scoped configuration or a hint, e.g. `ALTER DATABASE SCOPED CONFIGURATION SET PARAMETER_SENSITIVE_PLAN_OPTIMIZATION = OFF;` or `OPTION (USE HINT('DISABLE_PARAMETER_SENSITIVE_PLAN_OPTIMIZATION'))`.
- List valid `USE HINT` names: `SELECT * FROM sys.dm_exec_valid_use_hints;`
- **Safe compat-level upgrade**: (1) Query Store ON at the *old* level and collect a baseline; (2) raise the compat level; (3) watch *Regressed Queries*; (4) force the old plans, or apply `QUERYTRACEON`-free hints such as `USE HINT('QUERY_OPTIMIZER_COMPATIBILITY_LEVEL_150')` or `FORCE_LEGACY_CARDINALITY_ESTIMATION` per query.
- The `QUERY_OPTIMIZER_HOTFIXES` database scoped config (or TF 4199) turns on optimizer fixes released after RTM.

## Blocking and deadlocks

```sql
-- Head of blocking chains
SELECT r.session_id, r.blocking_session_id, r.wait_type, r.wait_resource, r.wait_time,
       t.text, s.open_transaction_count, s.program_name, s.host_name
FROM sys.dm_exec_requests r
JOIN sys.dm_exec_sessions s ON s.session_id = r.session_id
CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) t
WHERE r.blocking_session_id <> 0
   OR r.session_id IN (SELECT blocking_session_id FROM sys.dm_exec_requests);
```
- A head blocker with `status = sleeping` and `open_transaction_count > 0` is an app that opened a transaction and never committed (missing `COMMIT` or rollback in an error path). Use `SET XACT_ABORT ON` in procedures.
- Deadlock graphs are captured by default in the `system_health` XE session:
  ```sql
  SELECT CAST(event_data AS xml) FROM sys.fn_xe_file_target_read_file('system_health*.xel', NULL, NULL, NULL)
  WHERE object_name = 'xml_deadlock_report';
  ```
- Common deadlock fixes:
  - Access tables in a consistent order.
  - A covering index removes the key-lookup vs. update deadlock. This is the classic one: a reader seeks a nonclustered index then looks up the clustered index, while a writer updates the clustered index then the nonclustered one.
  - Shorter transactions.
  - Use RCSI.
  - Use `UPDLOCK` on read-then-write patterns.
- **RCSI** (`ALTER DATABASE ... SET READ_COMMITTED_SNAPSHOT ON`) means readers don't block writers, and the reverse. It's the right replacement for `NOLOCK`. It costs tempdb version store space (or the PVS with ADR), and 14 bytes per row. Code that relies on blocking reads (queue tables, check-then-insert) needs `READCOMMITTEDLOCK` or `UPDLOCK` hints.
- Lock escalation: a statement taking more than about 5,000 locks on one object escalates to a table lock. Batch the work, or use `ALTER TABLE ... SET (LOCK_ESCALATION = AUTO)` for partitioned tables.

## Server-level sanity checks (only when the evidence points here)

- `max server memory` set, leaving room for the OS. `cost threshold for parallelism` around 50. `MAXDOP` per Microsoft guidance: ≤ 8, ≤ cores per NUMA node.
- tempdb: several equally sized data files, on fast storage.
- `optimize for ad hoc workloads` = 1 for ad hoc-heavy systems.
- Instant file initialization enabled. Log file autogrowth in MB, not %. Few VLFs (`sys.dm_db_log_info`).
- Power plan set to High performance, on Windows hosts and VMs.
- Azure SQL: check `sys.dm_db_resource_stats` for DTU/vCore limits (CPU, data IO, log write %). You may be hitting the service tier ceiling rather than a query problem.
