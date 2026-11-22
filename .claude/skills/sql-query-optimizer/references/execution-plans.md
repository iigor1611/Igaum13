# Reading SQL Server execution plans

## Capture options

| Method | Actual? | Notes |
|---|---|---|
| SSMS Ctrl+M (Include Actual Plan) | ✅ | Easiest. Save as `.sqlplan` (XML). |
| `SET STATISTICS XML ON` | ✅ | Plan XML as an extra result set. |
| `SET STATISTICS PROFILE ON` | ✅ | Text grid with Rows/Executes per operator. |
| `SET SHOWPLAN_XML ON` | ❌ estimated | Does **not** execute. Safe for writes. |
| `sys.dm_exec_query_plan(plan_handle)` | ❌ estimated | Cached plan. Compiled param values only. |
| `sys.dm_exec_query_plan_stats(plan_handle)` | ✅ last actual | 2019+, `ALTER DATABASE SCOPED CONFIGURATION SET LAST_QUERY_PLAN_STATS = ON`. Light overhead. |
| Query Store (`sys.query_store_plan.query_plan`) | ❌ estimated | Plus runtime stats over time. |
| `sys.dm_exec_query_profiles` | ✅ live | In-flight per-operator progress (lightweight profiling is on by default in 2019+). Good for queries that never finish. |
| Extended Events `query_post_execution_showplan` | ✅ | Heavy. Filter tightly. `query_plan_profile` + `QUERY_PLAN_PROFILE` hint is lighter (2019+). |

## How to read one

1. Read **right to left, top to bottom** for data flow. Execution actually starts at the root (left) and pulls rows.
2. Ignore "Estimated cost %" in actual plans. Costs are *always estimates*, even in actual plans. Use **actual rows, executions, actual time** (`ActualElapsedms`/`ActualCPUms` in `RunTimeInformation`, 2016 SP1+) and **actual logical reads** per operator.
3. For each operator compare **Estimated Number of Rows per Execution × Estimated Executions** with **Actual Number of Rows (for all executions)**. Find the *lowest* (rightmost) operator with a big gap.
4. Check the root (SELECT/INSERT) node properties:
   - `CardinalityEstimationModelVersion` (70 = legacy CE, 120+ = new)
   - `MemoryGrantInfo` (`GrantedMemory`, `MaxUsedMemory`, `IsMemoryGrantFeedbackAdjusted`)
   - `QueryTimeStats`
   - `WaitStats` (2016 SP1+, top waits for this query)
   - `ParameterList` (compiled vs. runtime values)
   - `NonParallelPlanReason`
   - `StatementOptmEarlyAbortReason` (`TimeOut` = optimizer gave up, so the plan may be poor; simplify the query)
   - `OptimizerStatsUsage` (stats objects used, their sample % and last update)
   - `Warnings`
   - `QueryHash` / `QueryPlanHash`

## Data access operators

| Operator | Meaning / what to check |
|---|---|
| **Table Scan** | Heap, full read. Usually wants a clustered index. |
| **Clustered Index Scan** | Full table read. Bad when a `Predicate` filters most rows out. OK for small tables or when most rows are needed. |
| **Index Scan** (nonclustered) | Full read of a narrower structure. Often a non-sargable predicate or a non-leading column. |
| **Index Seek / Clustered Index Seek** | Good. But check the split between **Seek Predicates** (used to navigate) and **Predicate** (residual, applied after reading). A seek on col1 with a residual on col2 can still read millions of rows. Compare *Number of Rows Read* to *Actual Rows*. |
| **Key Lookup / RID Lookup** | Fetching columns the index lacks, once per row. Fine for a few rows, bad for thousands. Fix: `INCLUDE` the columns from the lookup's *Output List* and *Predicate*. |
| **Columnstore Index Scan** | Check `Actual Execution Mode = Batch`, *Segment reads* vs. *segments skipped* (rowgroup elimination), and predicate pushdown (predicate on the scan, not in a Filter). |
| **Constant Scan** | Literal rows, or a contradiction detected (fast). |

## Join operators

| Operator | Best when | Red flags |
|---|---|---|
| **Nested Loops** | Small outer input with indexed inner seeks | Outer actual rows ≫ estimate gives a huge inner *Number of Executions*. `Outer References` without an index means repeated scans. |
| **Hash Match (join)** | Large unsorted inputs, equality join | Spill warning (`Hash Warning`, spill level). Build input (top) should be the smaller one. Blocking (no rows until build completes). |
| **Merge Join** | Both inputs sorted on the join key (index order) | An inserted `Sort` to feed it. `Many-to-Many = True` uses a worktable in tempdb, which is slow. |
| **Adaptive Join** (2017+ batch, 2019+ rowstore batch) | Chooses hash vs. NL at runtime by row threshold | Check `Actual Join Type` and `Adaptive Threshold Rows`. |

## Other important operators

- **Sort**: blocking and memory hungry. `Sort Warnings` / spill means tempdb. Often avoidable with an index in the needed order. `TOP N Sort` is cheaper.
- **Hash Match (Aggregate)** / **Stream Aggregate**: stream needs sorted input. Hash can spill.
- **Filter**: a predicate that couldn't be pushed into the scan or seek (e.g. a non-sargable expression, a predicate on a computed result, `HAVING`). A Filter after a large row stream is a big red flag.
- **Compute Scalar**: usually cheap. *Deferred* evaluation hides cost (e.g. scalar UDF calls appear here, but their cost is invisible in the plan, so check `QueryTimeStats` and `sys.dm_exec_function_stats`).
- **Table Spool / Index Spool (Eager)**: the optimizer builds a temporary index each execution. An **Eager Index Spool** almost always means "create this index". Its seek keys tell you which columns. **Lazy Table Spool** on the inner side of NL caches rebinds/rewinds; check rewinds vs. rebinds.
- **Parallelism** (Distribute / Repartition / Gather Streams): look for skew (per-thread actual rows very uneven) and `Exchange Spill`. CXPACKET/CXCONSUMER waits with skew means uneven work.
- **Segment / Sequence Project / Window Spool**: window functions. An on-disk Window Spool is slow; `ROWS` framing (instead of the default `RANGE`) lets it use an in-memory spool. Batch mode Window Aggregate (2016+/2019 rowstore) is much faster.
- **Assert**: constraint check (FK, CHECK, scalar subquery returning more than 1 row).
- **Top**: watch for a **row goal**. With `TOP`, `EXISTS`, `IN`, or `FAST n`, the optimizer assumes it will stop early and estimates rows low. If the rows it wants are rare, it reads far more. Test with `OPTION (USE HINT('DISABLE_OPTIMIZER_ROWGOAL'))`. Check the `EstimateRowsWithoutRowGoal` attribute (2017+).

## Warnings and what they mean

| Warning | Meaning | Fix direction |
|---|---|---|
| `Type conversion in expression (CONVERT_IMPLICIT...) may affect "SeekPlan"` | Column-side conversion prevents seeks | Match parameter/column types. Fix the ORM mapping. |
| `... may affect "CardinalityEstimate"` | Usually harmless (often from the SELECT list) | Ignore unless the estimates are bad. |
| `Operator used tempdb to spill data during execution with spill level N` | Memory grant too small | Fix the row estimate. Memory grant feedback (2019+ row mode). `MIN_GRANT_PERCENT` hint as a last resort. |
| `The query memory grant detected "ExcessiveGrant"` | Granted ≫ used | Fix the overestimate (wide `varchar(max)` columns, `SELECT *`, bad stats). `MAX_GRANT_PERCENT`. |
| `No Join Predicate` | Cartesian product | Usually a bug. |
| `Columns With No Statistics` | Auto-create stats is off, or read-only | Create statistics. |
| `MissingIndexes` (green text) | Optimizer's per-query wish | Evaluate, merge with existing indexes. Don't blindly create. |
| `UnmatchedIndexes` | A filtered index couldn't be used because of parameterization | Use a literal, `RECOMPILE`, or dynamic SQL. |
| `PlanAffectingConvert` | See above | |

## Estimation sources (why estimates go wrong)

- **Stale stats**: check `OptimizerStatsUsage` → `LastUpdate`, `ModificationCount`, `SamplingPercent`. Low sampling on skewed data gives a bad histogram. Use `WITH FULLSCAN` or `PERSIST_SAMPLE_PERCENT`.
- **Ascending key**: new rows beyond the histogram's last step get estimated low (legacy CE estimates 1). CE 120+ assumes average density.
- **Local variables / `OPTIMIZE FOR UNKNOWN`**: the estimate uses density (average), not the histogram.
- **Table variables**: estimated at 1 row before 2019 (or compat < 150). Deferred compilation in 150+ uses the actual count at first compile, but there are still no column statistics.
- **Multi-statement TVFs**: fixed at 1 row (CE 70) or 100 rows (CE 120+). Interleaved execution (140+) fixes this only for read-only queries.
- **Correlated predicates**: CE 120+ uses *exponential backoff*. Legacy CE assumes independence. Multi-column stats or filtered stats help.
- **Expressions/functions on columns**: guessed selectivity. Add a computed column (with an index or stats) to give it stats.
- **CE model change** after a compat-level upgrade: compare with `OPTION (USE HINT('FORCE_LEGACY_CARDINALITY_ESTIMATION'))`.
