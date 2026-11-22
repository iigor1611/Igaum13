# SQL Server indexing and statistics

## Inspect what exists first

```sql
-- All indexes on a table: keys, includes, filter, size, usage
SELECT i.name, i.type_desc, i.is_unique, i.has_filter, i.filter_definition,
       keys = STUFF((SELECT ', ' + c.name + CASE WHEN ic.is_descending_key = 1 THEN ' DESC' ELSE '' END
                     FROM sys.index_columns ic JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                     WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.is_included_column = 0
                     ORDER BY ic.key_ordinal FOR XML PATH('')), 1, 2, ''),
       includes = STUFF((SELECT ', ' + c.name
                     FROM sys.index_columns ic JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                     WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.is_included_column = 1
                     FOR XML PATH('')), 1, 2, ''),
       size_mb = (SELECT SUM(ps.used_page_count) * 8 / 1024.0 FROM sys.dm_db_partition_stats ps
                  WHERE ps.object_id = i.object_id AND ps.index_id = i.index_id),
       us.user_seeks, us.user_scans, us.user_lookups, us.user_updates
FROM sys.indexes i
LEFT JOIN sys.dm_db_index_usage_stats us
       ON us.object_id = i.object_id AND us.index_id = i.index_id AND us.database_id = DB_ID()
WHERE i.object_id = OBJECT_ID('dbo.Orders')
ORDER BY i.index_id;
```
Usage stats reset when the instance restarts, and in older versions on index rebuild. Check `sqlserver_start_time` in `sys.dm_os_sys_info` before concluding an index is "unused".

## Clustered index choice

- The clustering key should be **narrow, unique, static, ever-increasing**: `int`/`bigint IDENTITY`, or a sequence. Every nonclustered index carries the clustering key, so a wide key bloats all of them.
- `uniqueidentifier` with `NEWID()` causes random inserts, page splits and fragmentation. Use `NEWSEQUENTIALID()`, keep the GUID as a nonclustered PK, or cluster on something else.
- **Last-page insert contention** (`PAGELATCH_EX` on one page under very high insert concurrency): use `OPTIMIZE_FOR_SEQUENTIAL_KEY = ON` (2019+), or hash partitioning.
- Clustering on a frequently range-queried column (e.g. `CreatedAt, Id`) can beat clustering on an identity for reporting tables.
- Heaps: OK for staging or bulk loads. Otherwise they suffer forwarded records (check `forwarded_fetch_count` in `sys.dm_db_index_operational_stats`) and don't reclaim space on delete.

## Nonclustered index design

**Key column order: equality → range → ORDER BY/GROUP BY. Then `INCLUDE` the remaining SELECT columns.**

```sql
-- WHERE CustomerId = @c AND Status = 'Open' AND OrderDate >= @d ORDER BY OrderDate
-- SELECT Id, OrderDate, Total
CREATE INDEX IX_Orders_Customer_Status_Date
ON dbo.Orders (CustomerId, Status, OrderDate)
INCLUDE (Total)                      -- Id (clustering key) is included automatically
WITH (ONLINE = ON, DATA_COMPRESSION = PAGE);
```
- Only the **first inequality** column is used to seek. Columns after it become residual predicates.
- Included columns live at the leaf level only. They're cheap for covering, but they can't be used for seeking or sorting. Max key size is 900 bytes clustered and 1700 bytes nonclustered (2016+). Max 32 key columns.
- Matching `ORDER BY` direction (or its exact reverse) removes the Sort. Mixed directions need a matching index (`a ASC, b DESC`).
- Among equality columns, put first the column other queries also filter on, so the index can be reused through its leftmost prefix.
- **Index intersection** is rare in practice. Prefer one composite index over relying on two single-column indexes.
- **Foreign keys** are not indexed automatically. Index FK columns used in joins, and those on the child side of parent deletes or updates (otherwise there's a scan per parent row and long blocking).

### Filtered indexes

```sql
CREATE INDEX IX_Orders_Open ON dbo.Orders (CustomerId, OrderDate)
INCLUDE (Total) WHERE Status = 'Open';             -- tiny if few rows are open
CREATE UNIQUE INDEX UX_Users_Email ON dbo.Users (Email) WHERE Email IS NOT NULL;  -- unique-with-many-NULLs
```
Gotchas:
- They're often **not used with parameterized queries** (`WHERE Status = @s`), which show the `UnmatchedIndexes` warning. Use literals, `OPTION (RECOMPILE)`, or dynamic SQL.
- They require `ANSI_NULLS`, `QUOTED_IDENTIFIER`, `ANSI_PADDING` and related options ON in every writing session, or DML fails.
- Include the filter column in the INCLUDE (or key) list. Otherwise a lookup may be needed for residual checks.
- Filtered **statistics** (`CREATE STATISTICS ... WHERE ...`) fix estimates on skewed subsets even without an index.

### Indexed views

These materialize joins and aggregates (they need `SCHEMABINDING`, `COUNT_BIG(*)`, and deterministic expressions). Enterprise Edition matches them automatically. In Standard Edition, reference the view `WITH (NOEXPAND)`, which is recommended in every edition for reliable stats use. They add heavy write overhead and blocking on hot rows, so use them for read-mostly aggregates.

## Columnstore

| Use | Choice |
|---|---|
| Data warehouse fact tables, large scans and aggregations | **Clustered columnstore index (CCI)** |
| OLTP table with some analytic queries | **Nonclustered columnstore (NCCI)**, optionally filtered (e.g. cold rows), with `COMPRESSION_DELAY` |
| Batch mode on old versions without columnstore data | Empty filtered NCCI trick: `CREATE NONCLUSTERED COLUMNSTORE INDEX x ON t(col) WHERE col = -1 AND col = -2;` (on 2019+ batch mode on rowstore makes this unnecessary) |

Health checks:
```sql
SELECT OBJECT_NAME(object_id) tbl, partition_number, row_group_id, state_desc, total_rows,
       deleted_rows, trim_reason_desc
FROM sys.dm_db_column_store_row_group_physical_stats
WHERE object_id = OBJECT_ID('dbo.FactSales') ORDER BY row_group_id;
```
- Aim for about 1,048,576 rows per rowgroup. Bulk-insert **102,400 rows or more** per batch so rows go straight to compressed rowgroups. Smaller batches land in delta stores.
- A high `deleted_rows` ratio, or many small rowgroups (`trim_reason` = `DICTIONARY_SIZE` / `MEMORY_LIMITATION`), call for `ALTER INDEX ... REORGANIZE WITH (COMPRESS_ALL_ROW_GROUPS = ON)`.
- **Segment elimination** needs the data ordered by the filter column (usually date). 2022+ supports ordered CCI: `CREATE CLUSTERED COLUMNSTORE INDEX cci ON t ORDER (DateKey)`. On older versions, build a rowstore clustered index on the date first, then create the CCI with `DROP_EXISTING`, `MAXDOP = 1`.
- String predicates pushdown improved in 2016+. Avoid wide `nvarchar(max)` columns in a CCI (supported only in 2017+, and with poor compression).
- Combine a CCI with partitioning by date for partition elimination and fast switching.

## Statistics

```sql
-- State of stats on a table
SELECT s.name, s.auto_created, s.user_created, s.has_filter, sp.last_updated, sp.rows,
       sp.rows_sampled, sp.modification_counter, sp.steps, sp.persisted_sample_percent
FROM sys.stats s CROSS APPLY sys.dm_db_stats_properties(s.object_id, s.stats_id) sp
WHERE s.object_id = OBJECT_ID('dbo.Orders');

DBCC SHOW_STATISTICS ('dbo.Orders', IX_Orders_Customer_Status_Date) WITH HISTOGRAM;
```
- **Auto update threshold**: in compat 130+ it's dynamic, about √(1000 × rows), which is much more frequent than 20% on big tables.
- `AUTO_UPDATE_STATISTICS_ASYNC = ON` avoids compile stalls on OLTP systems. The trade-off is that the first query after the threshold uses the old stats.
- The default sample can be tiny on large tables. For skewed columns use `UPDATE STATISTICS dbo.T stat WITH FULLSCAN, PERSIST_SAMPLE_PERCENT = ON` (2016 SP1 CU4+), so later automatic updates keep the sample rate.
- A histogram has at most **200 steps**. Badly skewed columns with many important values need **filtered statistics** for each range or hot value, or partitioning (incremental statistics: `INCREMENTAL = ON`).
- Multi-column statistics only hold a histogram for the *first* column, plus a density vector. They help correlated equality predicates.
- After a large load: `UPDATE STATISTICS dbo.T;` (or Ola Hallengren `IndexOptimize @UpdateStatistics = 'ALL', @OnlyModifiedStatistics = 'Y'`).

## Maintenance (be skeptical of fragmentation dogma)

- On SSD/SAN storage, **logical fragmentation rarely matters** for seeks. Page density, meaning empty space (`avg_page_space_used_in_percent`), matters more because it wastes memory and I/O.
- Updating statistics fixes most "the rebuild fixed it" cases. The rebuild only helped because it updated stats with a full scan, and because it caused a recompile.
- When you do maintain: use `REORGANIZE` (online, interruptible) or `REBUILD WITH (ONLINE = ON, RESUMABLE = ON, MAX_DURATION = 60)` (Enterprise or Azure SQL, 2017+ for resumable). Use `WAIT_AT_LOW_PRIORITY` to avoid blocking chains.
- `FILLFACTOR` below 100 only on indexes that suffer real page splits (random keys). It wastes space everywhere else.
- `DATA_COMPRESSION = PAGE` often cuts I/O 2–4× for read-heavy tables, at some CPU cost. Estimate first with `sp_estimate_data_compression_savings`.

## Safe index changes in production

- Use `ONLINE = ON` (Enterprise, Developer, Azure SQL). In Standard Edition, building an index blocks writes on the table, so schedule a window.
- Use `RESUMABLE = ON` (2017+ rebuild, 2019+ create) for big indexes. You can pause or resume, and it survives failover.
- Use `DROP_EXISTING = ON` to change an index definition in one step without a gap.
- Check tempdb and log space first (`SORT_IN_TEMPDB = ON` moves the sort to tempdb).
- Before dropping an "unused" index, check whether it's unique or enforces a constraint, is used by a monthly report, or is named in an index hint (dropping it breaks that query with error 308). Disabling first (`ALTER INDEX ... DISABLE`) keeps the definition so you can rebuild it, but for a clustered index this makes the table inaccessible.

## Missing-index DMVs: use with judgment

```sql
SELECT TOP (25)
       impact = migs.avg_user_impact * (migs.user_seeks + migs.user_scans),
       mid.statement, mid.equality_columns, mid.inequality_columns, mid.included_columns,
       migs.user_seeks, migs.last_user_seek
FROM sys.dm_db_missing_index_group_stats migs
JOIN sys.dm_db_missing_index_groups mig ON mig.index_group_handle = migs.group_handle
JOIN sys.dm_db_missing_index_details mid ON mid.index_handle = mig.index_handle
WHERE mid.database_id = DB_ID()
ORDER BY impact DESC;
-- 2019+: sys.dm_db_missing_index_group_stats_query links suggestions to the query text
```
Limitations: they ignore column order beyond equality/inequality, suggest near-duplicate indexes, never suggest filtered or columnstore indexes, and lump every SELECT column into INCLUDE. **Consolidate** suggestions with existing indexes. Widening an existing index is usually better than adding a new one. Aim for about 5–10 nonclustered indexes on OLTP tables, fewer on write-hot ones.
