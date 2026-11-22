# T-SQL rewrite catalog

Each entry gives the problem, the rewrite, and **equivalence caveats**. Check the caveats every time.

## 1. Non-sargable predicates

```sql
-- Bad: function on column → scan
WHERE YEAR(OrderDate) = 2024
WHERE DATEDIFF(DAY, OrderDate, GETDATE()) <= 7
WHERE LEFT(LastName, 3) = 'Smi'
WHERE ISNULL(Status, 'X') = 'A'
WHERE Amount * 1.1 > 100
-- Good: bare column, constant side does the math
WHERE OrderDate >= '20240101' AND OrderDate < '20250101'
WHERE OrderDate >= DATEADD(DAY, -7, CAST(GETDATE() AS date))
WHERE LastName LIKE 'Smi%'
WHERE Status = 'A'                        -- equivalent: NULL never equals 'A'
WHERE Amount > 100 / 1.1
```
- `CAST(datetimecol AS date) = @d` **is** sargable (a dynamic seek), but its cardinality estimate is often poor. The explicit range is better.
- Use the `'YYYYMMDD'` literal format. `'2024-01-01'` is language/DATEFORMAT dependent for `datetime`.
- When the expression is unavoidable, add a **computed column** plus an index. SQL Server matches the expression automatically when it's written identically:
  ```sql
  ALTER TABLE dbo.Orders ADD OrderYear AS YEAR(OrderDate);  -- deterministic, can be indexed without PERSISTED
  CREATE INDEX IX_Orders_OrderYear ON dbo.Orders(OrderYear);
  ```
  This requires the correct SET options (`QUOTED_IDENTIFIER`, `ANSI_NULLS`, `ARITHABORT` effectively ON) for every session that writes to the table.
- `LIKE '%term%'`: use full-text search (`CONTAINS`), or an n-gram table. For suffix search, use a persisted `REVERSE(col)` computed column with `LIKE REVERSE('term') + '%'`.

## 2. Implicit conversions (data type precedence)

```sql
-- Column AccountNo is varchar(20); ORM sends nvarchar(4000)
WHERE AccountNo = N'A123'     -- CONVERT_IMPLICIT on column → scan (under SQL collations)
WHERE AccountNo = 'A123'      -- seek
```
- The lower-precedence side gets converted. `nvarchar` > `varchar`, `int` > `varchar`, `datetime` > `date` > strings. If the **column** is the lower one, you lose the seek.
- With **Windows collations**, varchar ← nvarchar can still do a range seek (`GetRangeThroughConvert`). It's better than a scan but still worse than a clean seek.
- Fix in the app: ADO.NET `SqlParameter` with `SqlDbType.VarChar` and `Size`; JDBC `sendStringParametersAsUnicode=false`; EF Core `.IsUnicode(false)` / `HasColumnType("varchar(20)")`; Dapper `DbString { IsAnsi = true }`.
- Joins on mismatched types (`int` vs `varchar`, different collations): fix the schema. In the meantime, convert the *smaller* side explicitly.
- Find them across the plan cache:
  ```sql
  SELECT TOP (50) qs.total_worker_time, st.text, qp.query_plan
  FROM sys.dm_exec_query_stats qs
  CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
  CROSS APPLY sys.dm_exec_query_plan(qs.plan_handle) qp
  WHERE CAST(qp.query_plan AS nvarchar(max)) LIKE N'%CONVERT_IMPLICIT%PlanAffectingConvert%'
  ORDER BY qs.total_worker_time DESC;
  ```

## 3. Catch-all / optional parameters

```sql
-- Bad: one plan for all combinations; usually scans
SELECT ... FROM dbo.Orders
WHERE (@CustomerId IS NULL OR CustomerId = @CustomerId)
  AND (@Status     IS NULL OR Status     = @Status);
```
Fix A (simple, costs a compile on every execution):
```sql
... OPTION (RECOMPILE);   -- parameter embedding optimization: NULL branches are removed
```
Fix B (best for frequent calls): parameterized dynamic SQL, one cached plan per combination:
```sql
DECLARE @sql nvarchar(max) = N'SELECT ... FROM dbo.Orders WHERE 1 = 1';
IF @CustomerId IS NOT NULL SET @sql += N' AND CustomerId = @CustomerId';
IF @Status     IS NOT NULL SET @sql += N' AND Status = @Status';
EXEC sys.sp_executesql @sql, N'@CustomerId int, @Status varchar(10)', @CustomerId, @Status;
```
Never concatenate the parameter values themselves (SQL injection). Use `QUOTENAME()` for dynamic object names.
SQL Server 2025 (compat 170) adds *Optional Parameter Plan Optimization (OPPO)*, which can create plan variants for the `IS NULL OR` pattern. Verify in the plan before relying on it.

## 4. Local variables

```sql
DECLARE @d date = DATEADD(DAY, -1, GETDATE());
SELECT ... WHERE OrderDate >= @d;       -- estimate from density, not histogram
```
Fixes: pass the value as a procedure parameter; `OPTION (RECOMPILE)` (it sniffs variable values); or move the query into a child proc or `sp_executesql`.

## 5. Scalar UDFs

```sql
-- Bad (pre-2019 or non-inlineable): per-row execution, forces serial plan, cost hidden in plan
SELECT o.Id, dbo.fn_CustomerTier(o.CustomerId) FROM dbo.Orders o;
```
- Check: `SELECT OBJECT_NAME(object_id), is_inlineable FROM sys.sql_modules WHERE is_inlineable IS NOT NULL;` (2019+). Things that prevent inlining include: `GETDATE()`-style time-dependent functions (before CU fixes), table variables, multiple RETURN statements in some versions, and use inside computed columns or check constraints.
- Rewrite as an **inline TVF** and `CROSS APPLY`:
  ```sql
  CREATE OR ALTER FUNCTION dbo.itvf_CustomerTier(@CustomerId int)
  RETURNS TABLE AS RETURN
    SELECT CASE WHEN SUM(Total) > 10000 THEN 'Gold' ELSE 'Std' END AS Tier
    FROM dbo.Orders WHERE CustomerId = @CustomerId;
  GO
  SELECT o.Id, t.Tier FROM dbo.Orders o CROSS APPLY dbo.itvf_CustomerTier(o.CustomerId) t;
  ```
- A scalar UDF in a **computed column or CHECK constraint** makes *every* query on that table serial, even queries that don't touch it.
- Measure the real cost: `sys.dm_exec_function_stats`.

## 6. Multi-statement TVFs and table variables → inline TVF / #temp

- A multi-statement TVF (`RETURNS @t TABLE (...) ... BEGIN ... END`) gets a fixed estimate and no statistics. Convert it to an inline TVF (a single `RETURN SELECT`) when you can.
- `@table` variables: no column statistics, and no parallelism for inserts into them. Use `#temp` for anything beyond a few hundred rows, or when it's joined or filtered. You can index `#temp` (create the clustered index inline, or after loading).
- `#temp` downsides: recompiles when stats change, and tempdb metadata contention under very high concurrency. SQL 2019 has *memory-optimized tempdb metadata*.

## 7. Cursors and WHILE loops → set-based

```sql
-- Bad
DECLARE c CURSOR FOR SELECT Id FROM dbo.Orders WHERE Status = 'P';
... UPDATE dbo.Orders SET Status = 'S' WHERE Id = @Id; ...
-- Good
UPDATE dbo.Orders SET Status = 'S' WHERE Status = 'P';
```
If a cursor really is required, use `LOCAL FAST_FORWARD` (or `STATIC READ_ONLY`), never the default dynamic cursor. Running totals: use `SUM() OVER (ORDER BY ... ROWS UNBOUNDED PRECEDING)` instead of loops or self-joins.

## 8. NOT IN → NOT EXISTS

```sql
-- Wrong if Payments.OrderId can be NULL (returns nothing), and plans worse
SELECT * FROM dbo.Orders WHERE Id NOT IN (SELECT OrderId FROM dbo.Payments);
-- Right
SELECT * FROM dbo.Orders o WHERE NOT EXISTS (SELECT 1 FROM dbo.Payments p WHERE p.OrderId = o.Id);
```
`EXCEPT` also works when only key columns are compared (it de-duplicates).

## 9. EXISTS instead of JOIN + DISTINCT

```sql
-- Bad: fan-out then sort/hash to dedupe
SELECT DISTINCT c.* FROM dbo.Customers c JOIN dbo.Orders o ON o.CustomerId = c.Id;
-- Good: semi-join
SELECT c.* FROM dbo.Customers c WHERE EXISTS (SELECT 1 FROM dbo.Orders o WHERE o.CustomerId = c.Id);
```
Existence checks: `IF EXISTS (SELECT 1 ...)`, not `IF (SELECT COUNT(*) ...) > 0`.

## 10. OR across columns → UNION ALL

```sql
SELECT Id FROM dbo.Orders WHERE CustomerId = @c OR SalesRepId = @r;
-- →
SELECT Id FROM dbo.Orders WHERE CustomerId = @c
UNION ALL
SELECT Id FROM dbo.Orders WHERE SalesRepId = @r AND (CustomerId <> @c OR CustomerId IS NULL);
```
Each branch can seek its own index. Check the plan first: the optimizer sometimes does an index union (two seeks plus a concatenation) on its own.

## 11. Correlated subqueries → APPLY / pre-aggregate

```sql
-- Per-row subquery
SELECT c.Id, (SELECT MAX(OrderDate) FROM dbo.Orders o WHERE o.CustomerId = c.Id) AS LastOrder
FROM dbo.Customers c;
-- Pre-aggregate (many customers)
SELECT c.Id, o.LastOrder
FROM dbo.Customers c
LEFT JOIN (SELECT CustomerId, MAX(OrderDate) LastOrder FROM dbo.Orders GROUP BY CustomerId) o
  ON o.CustomerId = c.Id;
```
Top-N per group with an index on `(CustomerId, OrderDate DESC) INCLUDE (...)`:
```sql
SELECT c.Id, x.*
FROM dbo.Customers c
CROSS APPLY (SELECT TOP (3) o.Id, o.OrderDate, o.Total
             FROM dbo.Orders o WHERE o.CustomerId = c.Id ORDER BY o.OrderDate DESC) x;
```
APPLY + TOP wins when there are few groups or a filtered outer set. `ROW_NUMBER()` with `WHERE rn <= 3` wins when you scan everything anyway, especially in batch mode.

## 12. Aggregate before joining (fan-out bugs)

Joining two child tables to the same parent and then `SUM`ming double-counts **and** wastes work. Aggregate each child in a derived table or CTE first, then join on the key. (Same pattern as the pre-aggregate in #11.)

## 13. Pagination

```sql
-- Bad at depth: reads and discards @Skip rows
SELECT ... ORDER BY CreatedAt DESC, Id DESC OFFSET @Skip ROWS FETCH NEXT 50 ROWS ONLY;
-- Keyset (index on CreatedAt DESC, Id DESC)
SELECT TOP (50) ...
FROM dbo.Posts
WHERE CreatedAt < @LastCreatedAt OR (CreatedAt = @LastCreatedAt AND Id < @LastId)
ORDER BY CreatedAt DESC, Id DESC;
```
If you must use OFFSET, page through the narrow key first and then join back:
`WITH k AS (SELECT Id FROM ... ORDER BY ... OFFSET .. FETCH ..) SELECT t.* FROM k JOIN dbo.T t ON t.Id = k.Id`.

## 14. Large modifications → batches

```sql
WHILE 1 = 1
BEGIN
    DELETE TOP (5000) FROM dbo.EventLog WHERE CreatedAt < @Cutoff;   -- needs index on CreatedAt
    IF @@ROWCOUNT < 5000 BREAK;
END;
```
- Keep each batch under about 5,000 locks to avoid **lock escalation** to a table lock.
- In SIMPLE recovery, a CHECKPOINT between batches keeps the log small. In FULL recovery, take log backups.
- With partitioning, `TRUNCATE TABLE ... WITH (PARTITIONS (n))` (2016+), or `ALTER TABLE ... SWITCH`, is instant.
- `UPDATE` only the rows that change: `WHERE Col <> @new OR Col IS NULL`.

## 15. Window functions

- Specify `ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` explicitly. The default `RANGE` frame forces an on-disk Window Spool in row mode.
- A POC index (Partition, Order, Covering): `(PartitionCols, OrderCols) INCLUDE (others)` avoids the Sort.
- Batch mode makes window aggregates far faster: compat 150+ batch mode on rowstore, or the "empty filtered columnstore" trick on older versions.

## 16. Misc

- `SELECT *`: inflates memory grants (especially `varchar(max)` and LOBs) and prevents covering indexes.
- `UNION` → `UNION ALL` when duplicates are impossible or acceptable.
- `TOP` without `ORDER BY` is non-deterministic, so don't "optimize" by removing the ORDER BY.
- `MERGE`: has had concurrency and bug history. For upserts, prefer `UPDATE` + `INSERT ... WHERE NOT EXISTS` with `WITH (UPDLOCK, SERIALIZABLE)` on the existence check.
- Triggers: make them set-based (use the `inserted`/`deleted` tables), and keep them small. Check `sys.dm_exec_trigger_stats`.
- `SET NOCOUNT ON` in procedures cuts chatty DONE_IN_PROC messages.
- `sp_` prefix procedures are looked up in `master` first. Use another prefix. Always schema-qualify (`dbo.`).
- Different `SET` options (e.g. SSMS `ARITHABORT ON` vs. an app's `OFF`) produce **separate plans**. That's why a query can be "fast in SSMS, slow in the app". To reproduce, match the options, or pull the app's plan from the cache.
- Use `STRING_AGG` (2017+) instead of `FOR XML PATH`. Use `STRING_SPLIT` (2016+, `enable_ordinal` in 2022) or table-valued parameters instead of hand-rolled split functions.
- `APPROX_COUNT_DISTINCT` (2019+), `APPROX_PERCENTILE_CONT` (2022+) for big analytics when approximate results are acceptable.
- `DATETRUNC`, `GREATEST`/`LEAST`, `GENERATE_SERIES`, `IS [NOT] DISTINCT FROM` (2022+). `IS DISTINCT FROM` replaces NULL-safe `OR` comparisons cleanly.
