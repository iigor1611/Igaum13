# Igaum13
First try

## Claude Code skill: SQL Server Query Optimizer

`.claude/skills/sql-query-optimizer/` is a Claude Code skill for tuning Microsoft SQL Server / Azure SQL queries:
execution plans, T-SQL rewrites, indexing and columnstore, statistics, Query Store, wait stats and parameter sniffing.

- **Use in this repo:** open Claude Code here and the skill loads automatically.
- **Use everywhere:** copy the folder to `~/.claude/skills/sql-query-optimizer/`.
- **Plan analyzer:** `python3 .claude/skills/sql-query-optimizer/scripts/showplan_analyzer.py plan.sqlplan`
