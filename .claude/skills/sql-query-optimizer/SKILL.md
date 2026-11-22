---
name: sql-query-optimizer
description: Expert Microsoft SQL Server / Azure SQL performance tuning. Diagnoses slow T-SQL using actual execution plans, Query Store, DMVs and wait stats, then fixes it with sargable rewrites, index and columnstore design, statistics, parameter-sniffing remedies and Intelligent Query Processing features. Use when a SQL Server query is slow, times out, blocks or deadlocks, has high CPU/reads, regressed after an upgrade or plan change, or when the user shares a .sqlplan / showplan XML, STATISTICS IO output, or asks "why is this query slow", "what index should I add", "parameter sniffing", "Query Store". Covers SQL Server 2016–2025, Azure SQL Database and Managed Instance.
---

# SQL Server Query Optimizer

Tune T-SQL using evidence (actual plans, logical reads, waits), not folklore. Every recommendation must point to something specific: a plan operator, a warning, a DMV row, or a STATISTICS IO line. If you have no evidence, label the advice as a hypothesis and say what would confirm it.

## Core principles

1. **Use the actual plan, not the estimated plan.** Estimated plans hide misestimates, spills, and runtime parameter values.
2. **Logical reads are the most stable metric** (`SET STATISTICS IO ON`). Elapsed time varies with cache, blocking, and load. CPU time is second best.
3. **Find the one bottleneck.** Find the operator(s) with the highest actual cost, and the first point where estimated rows are 10× or more off from actual rows. Everything above that point was planned on bad numbers.
4. **Do less work.** Order of payoff: read fewer rows (sargable predicates, seeks), then avoid lookups (covering indexes), then join fewer rows, then pick better algorithms or batch mode, then change config. Hints are last.
5. **Results must stay identical.** Watch NULL semantics, duplicates, outer joins, and `TOP` without a deterministic `ORDER BY`.
6. **Know the version and compatibility level.** Many fixes depend on `compatibility_level`, not just the engine version: 130 = 2016, 140 = 2017, 150 = 2019, 160 = 2022, 170 = 2025. Always check it.

## Workflow

### Step 1: Gather context (ask only for what's missing)

```sql
SELECT @@VERSION;
SELECT name, compatibility_level, is_query_store_on FROM sys.databases WHERE name = DB_NAME();
SELECT * FROM sys.database_scoped_configurations;  -- MAXDOP, LEGACY_CE, PARAMETER_SNIFFING, IQP toggles
```

- **The query**, plus how it runs: stored procedure, `sp_executesql`, ORM (parameter types!), or ad hoc. Also how often it runs and its typical and worst-case parameter values.
- **The actual plan.** In SSMS use *Include Actual Execution Plan* (Ctrl+M), save as `.sqlplan`, and share the XML. Or capture it in T-SQL:
  ```sql
  SET STATISTICS IO, TIME ON;
  SET STATISTICS XML ON;   -- returns actual plan XML as an extra result set
  EXEC dbo.MyProc @p = ...;
  ```
  For production queries already in cache, pull them from Query Store or `sys.dm_exec_query_stats`. Those are *estimated* plans, but `sys.dm_exec_query_plan_stats` gives the last actual plan (2019+, needs `LAST_QUERY_PLAN_STATS = ON`). See `references/diagnostics.md`.
- **Schema**: table DDL, all indexes (including included columns and filters), row counts, and the statistics date. Run `sp_helpindex` or the query in `references/indexing.md`.
- **The symptom**: slow always, slow sometimes (suggests parameter sniffing or blocking), slow since a date (a plan regression, so check Query Store), high CPU, or blocking/deadlocks.

### Step 2: Read the plan

For showplan XML, run the bundled analyzer first:

```bash
python3 ~/.claude/skills/sql-query-optimizer/scripts/showplan_analyzer.py plan.sqlplan
```

It flags misestimates, spills, key lookups, scans with residual predicates, implicit conversions, missing-index hints, compiled vs. runtime parameter values, memory grant waste, serial-plan reasons, and top waits. Then read the plan yourself with `references/execution-plans.md`.

Red flags:

| Signal | Usually means |
|---|---|
| Actual rows ≫ or ≪ estimated rows | Stale or insufficient stats, a non-sargable predicate, a table variable, a multi-statement TVF, a local variable, or ascending-key growth |
| `Index Scan` / `Clustered Index Scan` with a `Predicate` (residual) | A non-sargable predicate, or no index leading on the filter column |
| `Key Lookup` / `RID Lookup` run thousands of times | Non-covering index, so add `INCLUDE` columns |
| ⚠ `CONVERT_IMPLICIT` in seek or predicate | Data type mismatch (often an `nvarchar` parameter against a `varchar` column) |
| ⚠ Spill to tempdb (Sort, Hash, Exchange) | Memory grant too small because of underestimated rows |
| Memory grant far larger than used | Overestimate, which causes `RESOURCE_SEMAPHORE` waits for other queries |
| `Nested Loops` with a huge inner execution count | Outer rows underestimated |
| Compiled param value ≠ runtime value | Parameter sniffing |
| `NonParallelPlanReason` | A scalar UDF, table variable modification, `MAXDOP 1`, and so on |
| `Table Spool` / `Index Spool` (eager) | The optimizer building a temp index on the fly, so a real index is missing |
| Thick arrow that shrinks after a `Filter` | The filter should be pushed down or made sargable |

### Step 3: Diagnose with the T-SQL anti-pattern checklist

Full catalog with before/after code: `references/tsql-rewrites.md`.

1. **Non-sargable predicates**: `WHERE YEAR(OrderDate) = 2024`, `CONVERT(date, col) = ...` (this one is sargable, but its estimate is poor), `ISNULL(col,'') = ''`, `LEFT(col,3) = 'abc'`, `col + 1 = @x`, `LIKE '%x'`.
2. **Implicit conversions**: an `nvarchar` parameter against a `varchar` column (common with ORMs, ADO.NET and JDBC `sendStringParametersAsUnicode`), mismatched join column types, mismatched collations.
3. **Parameter sniffing**: one cached plan used for skewed values. See the decision tree in `references/diagnostics.md`.
4. **Catch-all queries**: `WHERE (@p IS NULL OR col = @p)`. Fix with dynamic SQL via `sp_executesql`, or `OPTION (RECOMPILE)`.
5. **Local variables** in predicates get a density-based estimate. Use parameters, `RECOMPILE`, or `OPTIMIZE FOR`.
6. **Scalar UDFs**: before 2019 they run once per row and force serial plans. 2019+ inlines them only when the UDF is inlineable. Check `sys.sql_modules.is_inlineable`.
7. **Multi-statement TVFs and table variables**: fixed estimates (1 or 100 rows; 2017+ has interleaved execution, 2019+ has deferred compilation). Prefer inline TVFs and `#temp` tables.
8. **Row-by-row work**: cursors, `WHILE` loops, triggers that assume a single row. Rewrite set-based.
9. **`NOT IN` on a nullable column**: wrong results when NULLs exist. Use `NOT EXISTS`.
10. **`OR` across different columns**: rewrite as `UNION ALL`, or check whether index union kicks in.
11. **Deep `OFFSET/FETCH` pagination**: switch to keyset pagination.
12. **`SELECT *`**: blocks covering indexes and widens memory grants.
13. **`DISTINCT` covering up join fan-out**: use `EXISTS`, or aggregate before joining.
14. **Huge single transactions** (mass `DELETE`/`UPDATE`): cause log growth, lock escalation, and blocking. Batch them.
15. **`NOLOCK` used as a "performance fix"**: gives wrong results (missed or duplicate rows). The real fix for reader/writer blocking is RCSI (`READ_COMMITTED_SNAPSHOT ON`).

### Step 4: Pick a fix, cheapest and safest first

1. **Statistics**: `UPDATE STATISTICS dbo.T (stat) WITH FULLSCAN`, filtered statistics for skew, and checking the ascending-key problem (default on in CE 120+).
2. **Rewrite** with no schema change. Make predicates sargable, match data types, push filters down, go set-based.
3. **Indexes**: key columns ordered equality → range → sort, `INCLUDE` for covering, filtered indexes, columnstore for analytics. See `references/indexing.md`. Never blindly create missing-index DMV suggestions; merge them with existing indexes.
4. **Let IQP work**: raise the compatibility level (150/160/170) to get batch mode on rowstore, adaptive joins, memory grant feedback, PSP optimization, DOP/CE feedback. Test first: CE changes can regress some queries. Query Store makes that safe.
5. **Query Store remedies** (no code change): force a known-good plan, or apply **Query Store hints** (2022+ and Azure SQL), e.g. `sys.sp_query_store_set_hints @query_id, N'OPTION (RECOMPILE)'`.
6. **Query hints in code** (`RECOMPILE`, `OPTIMIZE FOR`, `USE HINT('...')`, `MAXDOP`). Document each one as technical debt.
7. **Server/database config**: MAXDOP, cost threshold for parallelism (the default 5 is too low; 30–50 is typical), tempdb file count, RCSI. Only with evidence from waits.

### Step 5: Verify

- Compare before and after: logical reads per table, CPU time, elapsed time, memory grant, spills, and row estimates. Run each query 2–3 times. Use `DBCC DROPCLEANBUFFERS` only on dev, never in production.
- Prove the results are equivalent:
  ```sql
  SELECT * FROM (/* old */) a EXCEPT SELECT * FROM (/* new */) b;   -- expect 0 rows
  SELECT * FROM (/* new */) b EXCEPT SELECT * FROM (/* old */) a;   -- expect 0 rows
  -- EXCEPT is set-based (ignores duplicate counts): also compare COUNT(*) and CHECKSUM_AGG(BINARY_CHECKSUM(*))
  ```
- Test with skewed and worst-case parameter values, not just the typical ones.
- After deploying, watch Query Store: *Regressed Queries* and *Top Resource Consuming*.

## Output format

```
## Diagnosis
<1–3 sentences naming the bottleneck, citing plan operator / warning / reads>

## Fix
<T-SQL rewrite and/or DDL, ready to run; ONLINE options where applicable>

## Why it's faster
<mechanism, e.g. "Clustered Index Scan 4.1M rows / 52k reads → Index Seek 1.8k rows / 9 reads">

## Verify
<exact commands: STATISTICS IO/TIME, EXCEPT checks, Query Store query>

## Trade-offs / risks
<write overhead, index size, blocking during build, edition requirements, plan-forcing staleness>
```

When there are several issues, rank them by impact and say which fix to do first.

## Safety

- Never run DDL, `DBCC FREEPROCCACHE` (it clears **all** plans and causes a CPU storm), `DBCC DROPCLEANBUFFERS`, `ALTER DATABASE`, or index rebuilds on production without explicit approval. Evict a single plan instead: `DBCC FREEPROCCACHE (plan_handle)`, or `sp_recompile`.
- Capturing an actual plan **executes** the query. For `INSERT`/`UPDATE`/`DELETE`, wrap it as `BEGIN TRAN; ...; ROLLBACK;`, and remember it still takes locks and writes to the log.
- Edition matters: `ONLINE = ON` index operations, and some features, require Enterprise, Developer, or Azure SQL. Say so when recommending them.
- Watch out for `sp_` prefix procedures, triggers, and Resource Governor limits, which change the behavior you observe.

## Reference files (load the one you need)

- `references/execution-plans.md`: every important operator, plan properties and warnings, how to read actual vs. estimated
- `references/tsql-rewrites.md`: T-SQL anti-pattern catalog with before/after code and equivalence notes
- `references/indexing.md`: rowstore and columnstore index design, filtered indexes, statistics, maintenance, safe online builds
- `references/diagnostics.md`: Query Store, DMV queries, wait stats, parameter-sniffing decision tree, IQP feature matrix by compat level, blocking/deadlocks
- `scripts/showplan_analyzer.py`: showplan XML (`.sqlplan`) analyzer, Python standard library only
