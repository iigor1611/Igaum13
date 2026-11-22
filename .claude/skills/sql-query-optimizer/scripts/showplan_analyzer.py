#!/usr/bin/env python3
"""
SQL Server showplan analyzer.

Reads a SQL Server execution plan (.sqlplan / showplan XML: actual or estimated)
and prints a ranked list of performance problems, citing node IDs so you can
find them in SSMS.

Usage:
    python3 showplan_analyzer.py plan.sqlplan
    python3 showplan_analyzer.py plan.sqlplan --top 15
    cat plan.xml | python3 showplan_analyzer.py -

Python 3.8+, standard library only.
"""
import argparse
import sys
import xml.etree.ElementTree as ET

NS = "{http://schemas.microsoft.com/sqlserver/2004/07/showplan}"

# thresholds
MISESTIMATE_RATIO = 10.0
MISESTIMATE_MIN_ROWS = 1000      # ignore misestimates where both sides are tiny
RESIDUAL_READ_RATIO = 10.0
RESIDUAL_MIN_READ = 10000
LOOKUP_MIN_EXECS = 1000
INNER_MIN_EXECS = 10000
GRANT_WASTE_RATIO = 10.0
GRANT_WASTE_MIN_KB = 100 * 1024  # 100 MB


def tag(el):
    return el.tag.replace(NS, "")


def f(el, attr, default=None):
    v = el.get(attr)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def fmt_n(n):
    if n is None:
        return "?"
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n:.0f}" if n == int(n) else f"{n:.2f}"


def child_relops(relop):
    """Nearest RelOp descendants (direct plan children)."""
    out = []
    stack = list(relop)
    while stack:
        el = stack.pop(0)
        if tag(el) == "RelOp":
            out.append(el)
        else:
            stack[0:0] = list(el)
    return out


def own_iter(relop, name):
    """Iterate elements named `name` belonging to this operator only (skip nested RelOps)."""
    stack = list(relop)
    while stack:
        el = stack.pop(0)
        if tag(el) == "RelOp":
            continue
        if tag(el) == name:
            yield el
        stack[0:0] = list(el)


def object_name(relop):
    for el in own_iter(relop, "Object"):
        parts = [el.get(k, "").strip("[]") for k in ("Schema", "Table")]
        name = ".".join(p for p in parts if p)
        idx = el.get("Index")
        if idx:
            name += f" ({idx.strip('[]')})"
        return name
    return ""


class Op:
    def __init__(self, el, parent=None):
        self.el = el
        self.parent = parent
        self.node_id = el.get("NodeId")
        self.physical = el.get("PhysicalOp", "")
        self.logical = el.get("LogicalOp", "")
        self.est_rows = f(el, "EstimateRows", 0.0)                 # per execution
        self.est_rebinds = f(el, "EstimateRebinds", 0.0)
        self.est_rewinds = f(el, "EstimateRewinds", 0.0)
        self.est_rows_read = f(el, "EstimatedRowsRead")
        self.est_no_rowgoal = f(el, "EstimateRowsWithoutRowGoal")
        self.table_card = f(el, "TableCardinality")
        self.est_mode = el.get("EstimatedExecutionMode", "Row")
        self.parallel = el.get("Parallel") in ("1", "true")
        self.cost = f(el, "EstimatedTotalSubtreeCost", 0.0)
        self.obj = object_name(el)

        # Lookup flag lives on the IndexScan child element
        self.is_lookup = False
        self.has_residual = False
        self.residual_text = ""
        for ch in el:
            t = tag(ch)
            if t in ("IndexScan", "TableScan"):
                if ch.get("Lookup") in ("1", "true"):
                    self.is_lookup = True
                pred = ch.find(NS + "Predicate")
                if pred is not None:
                    self.has_residual = True
                    so = pred.find(NS + "ScalarOperator")
                    if so is not None:
                        self.residual_text = so.get("ScalarString", "")
        if self.physical == "RID Lookup":
            self.is_lookup = True

        # runtime
        rti = el.find(NS + "RunTimeInformation")
        self.actual = rti is not None
        self.act_rows = self.act_execs = self.act_rows_read = 0.0
        self.elapsed = self.cpu = self.reads = self.phys_reads = 0.0
        self.act_mode = self.est_mode
        self.thread_rows = []
        if rti is not None:
            elapsed_max = 0.0
            for t in rti.findall(NS + "RunTimeCountersPerThread"):
                rows = f(t, "ActualRows", 0.0)
                self.act_rows += rows
                self.act_execs += f(t, "ActualExecutions", 0.0)
                self.act_rows_read += f(t, "ActualRowsRead", 0.0)
                self.cpu += f(t, "ActualCPUms", 0.0)
                self.reads += f(t, "ActualLogicalReads", 0.0)
                self.phys_reads += f(t, "ActualPhysicalReads", 0.0)
                elapsed_max = max(elapsed_max, f(t, "ActualElapsedms", 0.0))
                self.act_mode = t.get("ActualExecutionMode", self.act_mode)
                if t.get("Thread") != "0" or len(rti) == 1:
                    self.thread_rows.append(rows)
            self.elapsed = elapsed_max

        self.warnings = el.find(NS + "Warnings")
        self.children = [Op(c, self) for c in child_relops(el)]

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    @property
    def exclusive_ms(self):
        """Approximate own time. Row-mode elapsed includes children; batch mode does not."""
        if not self.actual:
            return 0.0
        if self.act_mode == "Batch":
            return self.elapsed
        child = sum(c.elapsed for c in self.children if c.actual)
        return max(self.elapsed - child, 0.0)

    @property
    def est_total_rows(self):
        # estimated rows across all executions
        execs = 1.0 + self.est_rebinds + self.est_rewinds
        return self.est_rows * execs

    def label(self):
        s = f"[Node {self.node_id}] {self.physical}"
        if self.is_lookup and "Lookup" not in self.physical:
            s += " (Key Lookup)"
        if self.logical and self.logical not in self.physical:
            s += f" / {self.logical}"
        if self.obj:
            s += f" on {self.obj}"
        return s


class Finding:
    def __init__(self, severity, title, detail, fix):
        self.severity, self.title, self.detail, self.fix = severity, title, detail, fix


SEV_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}


def misestimate(op):
    """Return (ratio, direction) if this operator is badly misestimated, else None."""
    if not op.actual or op.act_execs <= 0:
        return None
    if op.parallel:
        # per-thread execution counts make per-exec math misleading: compare totals
        est, act = max(op.est_total_rows, 1.0), max(op.act_rows, 1.0)
    else:
        est, act = max(op.est_rows, 1.0), max(op.act_rows / op.act_execs, 1.0)
    ratio = max(est / act, act / est)
    if ratio >= MISESTIMATE_RATIO and max(op.act_rows, op.est_total_rows) >= MISESTIMATE_MIN_ROWS:
        return ratio, ("UNDER" if act > est else "OVER")
    return None


def count_propagated(op, direction):
    """How many ancestors inherit the same-direction misestimate."""
    n, p = 0, op.parent
    while p is not None:
        m = misestimate(p)
        if not m or m[1] != direction:
            break
        n, p = n + 1, p.parent
    return n


def analyze_operator(op, findings):
    actual = op.actual

    # 1. Cardinality misestimate: report only the lowest operator of a chain (the root cause)
    m = misestimate(op)
    if m and not any((misestimate(c) or (0, None))[1] == m[1] for c in op.children):
        ratio, direction = m
        act_per_exec = op.act_rows / op.act_execs
        if op.parallel:
            numbers = (f"Estimated {fmt_n(op.est_total_rows)} rows total, actual {fmt_n(op.act_rows)} "
                       f"(parallel operator, totals compared).")
        else:
            numbers = (f"Estimated {fmt_n(op.est_rows)} rows/exec, actual {fmt_n(act_per_exec)} rows/exec "
                       f"over {fmt_n(op.act_execs)} executions.")
        sev = "HIGH" if ratio >= 100 else "MEDIUM"
        extra = ""
        if op.est_no_rowgoal and op.est_no_rowgoal > op.est_rows * 2:
            extra = (f" Row goal active (estimate without row goal: {fmt_n(op.est_no_rowgoal)}); "
                     "try OPTION (USE HINT('DISABLE_OPTIMIZER_ROWGOAL')).")
        findings.append(Finding(
            sev, f"Cardinality {direction}estimate ×{ratio:.0f}: {op.label()}",
            numbers + extra
            + (f" Propagates to {count_propagated(op, direction)} parent operator(s)."
               if count_propagated(op, direction) else ""),
            "Find the lowest node with this gap; check stats freshness/sampling, non-sargable predicates, "
            "local variables, table variables / MSTVFs, correlated predicates, parameter sniffing."))

    # 2. Scan / seek reading far more rows than returned (residual predicate)
    if op.physical in ("Clustered Index Scan", "Index Scan", "Table Scan",
                       "Clustered Index Seek", "Index Seek", "Columnstore Index Scan"):
        read = op.act_rows_read if actual and op.act_rows_read else None
        returned = op.act_rows if actual else op.est_rows
        if read is None and not actual:
            read = op.est_rows_read
        if read and read >= RESIDUAL_MIN_READ and read >= RESIDUAL_READ_RATIO * max(returned, 1):
            findings.append(Finding(
                "HIGH" if read >= 1e6 else "MEDIUM",
                f"Reads {fmt_n(read)} rows to return {fmt_n(returned)}: {op.label()}",
                (f"Residual predicate: {op.residual_text[:300]}" if op.residual_text
                 else "Most rows read are discarded."),
                "Make the predicate sargable and/or create an index whose key leads with the filtered "
                "columns (equality → range)."))
        elif op.physical in ("Table Scan", "Clustered Index Scan") and op.has_residual and \
                (op.table_card or 0) >= 100000 and not actual:
            findings.append(Finding(
                "MEDIUM", f"Full scan with filter on {fmt_n(op.table_card)}-row table: {op.label()}",
                f"Predicate: {op.residual_text[:300]}",
                "Check sargability and indexing of the predicate columns."))

    # 3. Key / RID lookups executed many times
    if op.is_lookup:
        execs = op.act_execs if actual else op.est_total_rows
        if execs >= LOOKUP_MIN_EXECS:
            out_cols = []
            ol = op.el.find(NS + "OutputList")
            if ol is not None:
                out_cols = [c.get("Column") for c in ol.findall(NS + "ColumnReference") if c.get("Column")]
            findings.append(Finding(
                "HIGH" if execs >= 100000 else "MEDIUM",
                f"Lookup executed {fmt_n(execs)} times: {op.label()}",
                "Columns fetched: " + (", ".join(out_cols) if out_cols else "(see Output List)")
                + (f"; residual: {op.residual_text[:200]}" if op.residual_text else ""),
                "Add these columns to the seeking index's INCLUDE list (and predicate columns to the key)."))

    # 4. Eager index spool = missing index
    if op.physical == "Index Spool" and "Eager" in op.logical:
        findings.append(Finding(
            "HIGH", f"Eager Index Spool (optimizer building a temp index every run): {op.label()}",
            "The spool's seek predicate columns are the key of the index you are missing.",
            "Create a permanent index on the spool's seek columns, including its output columns."))

    # 5. Nested loops with huge inner executions
    if op.physical == "Nested Loops" and len(op.children) >= 2:
        inner = op.children[1]
        execs = inner.act_execs if inner.actual else (1 + inner.est_rebinds + inner.est_rewinds)
        if execs >= INNER_MIN_EXECS:
            findings.append(Finding(
                "HIGH" if inner.actual and inner.elapsed > 1000 else "MEDIUM",
                f"Nested Loops inner side executed {fmt_n(execs)} times: {op.label()}",
                f"Inner input: {inner.label()}; outer estimated {fmt_n(op.children[0].est_total_rows)} rows"
                + (f", actual {fmt_n(op.children[0].act_rows)}" if op.children[0].actual else "") + ".",
                "If outer rows were underestimated, fix the estimate (a hash join would win). "
                "Otherwise ensure the inner side is an index seek, not a scan."))

    # 6. Operator-level warnings
    w = op.warnings
    if w is not None:
        spills = w.findall(NS + "SpillToTempDb")
        details = [d for d in w if tag(d) in ("SortSpillDetails", "HashSpillDetails", "ExchangeSpillDetails")]
        if spills or details:
            level = spills[0].get("SpillLevel", "?") if spills else "?"
            parts = [f"Spilled threads: {spills[0].get('SpilledThreadCount', '?')}"] if spills else []
            for d in details:
                parts.append(f"{tag(d)}: pages written {d.get('WritesToTempDb', '?')}, read "
                             f"{d.get('ReadsFromTempDb', '?')}, granted KB {d.get('GrantedMemoryKb', '?')}, "
                             f"used KB {d.get('UsedMemoryKb', '?')}")
            findings.append(Finding(
                "HIGH", f"Spill to tempdb (level {level}): {op.label()}",
                "; ".join(parts) + ".",
                "Fix the underestimate feeding this operator; reduce row width (avoid SELECT *); "
                "memory grant feedback (compat 140/150+); MIN_GRANT_PERCENT as last resort."))
        if w.get("NoJoinPredicate") in ("1", "true"):
            findings.append(Finding("HIGH", f"No join predicate (Cartesian product): {op.label()}",
                                    "Rows are multiplied.", "Usually a missing ON condition: verify the join."))
        for c in w.findall(NS + "ColumnsWithNoStatistics"):
            cols = [r.get("Column") for r in c.findall(NS + "ColumnReference")]
            findings.append(Finding("MEDIUM", f"Columns with no statistics: {op.label()}",
                                    ", ".join(filter(None, cols)),
                                    "Enable AUTO_CREATE_STATISTICS or CREATE STATISTICS on these columns."))
        for pc in w.findall(NS + "PlanAffectingConvert"):
            add_convert_finding(pc, findings, op.label())

    # 7. Parallel skew
    if actual and len(op.thread_rows) >= 2 and op.act_rows >= 100000:
        srt = sorted(op.thread_rows)
        mx, med = srt[-1], srt[len(srt) // 2]
        if mx / max(med, 1.0) >= 3:
            findings.append(Finding(
                "LOW", f"Parallel thread skew (max/median ×{mx / max(med, 1.0):.1f}): {op.label()}",
                f"Per-thread rows: {', '.join(fmt_n(r) for r in op.thread_rows)}.",
                "Skewed key or few distinct values; consider batch mode, different join order, or data fix."))


def add_convert_finding(pc, findings, where):
    issue = pc.get("ConvertIssue", "")
    expr = pc.get("Expression", "")
    if issue == "Seek Plan":
        findings.append(Finding(
            "HIGH", f"Implicit conversion prevents index seek ({where})", expr[:300],
            "Match parameter/variable type to the column (e.g. varchar vs nvarchar from ORM); fix join column types."))
    elif "CONVERT_IMPLICIT" in expr:
        findings.append(Finding(
            "LOW", f"Implicit conversion may affect cardinality estimate ({where})", expr[:300],
            "Usually harmless if in SELECT list; investigate if estimates near it are wrong."))


def analyze_statement(stmt, top_n):
    findings = []
    text = (stmt.get("StatementText") or "").strip()
    qp = stmt.find(NS + "QueryPlan")
    print("=" * 100)
    print(f"Statement {stmt.get('StatementId', '?')} [{stmt.get('StatementType', '?')}]: "
          f"{' '.join(text.split())[:300]}")
    if qp is None:
        print("  (no query plan: trivial / DDL / not captured)")
        return

    ce = stmt.get("CardinalityEstimationModelVersion")
    meta = [f"est. cost {fmt_n(f(stmt, 'StatementSubTreeCost', 0))}",
            f"CE model {ce}" if ce else None,
            f"DOP {qp.get('DegreeOfParallelism')}" if qp.get("DegreeOfParallelism") else None,
            f"compat {stmt.get('StatementOptmLevel')}" if stmt.get("StatementOptmLevel") else None,
            f"QueryHash {stmt.get('QueryHash')}" if stmt.get("QueryHash") else None]
    print("  " + " | ".join(m for m in meta if m))

    qts = qp.find(NS + "QueryTimeStats")
    if qts is not None:
        print(f"  Elapsed {fmt_n(f(qts, 'ElapsedTime'))} ms, CPU {fmt_n(f(qts, 'CpuTime'))} ms")

    if stmt.get("StatementOptmEarlyAbortReason") == "TimeOut":
        findings.append(Finding("MEDIUM", "Optimizer timed out (plan may be far from optimal)",
                                "StatementOptmEarlyAbortReason = TimeOut.",
                                "Simplify: split into steps with #temp tables, reduce joins/views-on-views."))
    npr = qp.get("NonParallelPlanReason")
    if npr and npr not in ("MaxDOPSetToOne", "EstimatedDOPIsOne", "NoParallelPlansInDesktopOrExpressEdition"):
        findings.append(Finding("MEDIUM" if "Udf" in npr or "Function" in npr or "TableVariable" in npr else "LOW",
                                f"Serial plan forced: {npr}",
                                "Something in the query prevents parallelism.",
                                "Scalar UDFs → inline TVF (or compat 150 inlining); table variable inserts → #temp."))

    # memory grant
    mg = qp.find(NS + "MemoryGrantInfo")
    if mg is not None:
        granted, used = f(mg, "GrantedMemory", 0), f(mg, "MaxUsedMemory", 0)
        if granted:
            print(f"  Memory grant: granted {fmt_n(granted)} KB, max used {fmt_n(used)} KB"
                  + (f", feedback adjusted: {mg.get('IsMemoryGrantFeedbackAdjusted')}"
                     if mg.get("IsMemoryGrantFeedbackAdjusted") else ""))
            if granted >= GRANT_WASTE_MIN_KB and granted >= GRANT_WASTE_RATIO * max(used, 1):
                findings.append(Finding("MEDIUM", f"Excessive memory grant ({fmt_n(granted)} KB, used {fmt_n(used)} KB)",
                                        "Over-granted memory blocks other queries (RESOURCE_SEMAPHORE).",
                                        "Fix overestimates; avoid SELECT * / wide (max) columns in sorts/hashes; "
                                        "MAX_GRANT_PERCENT hint or memory grant feedback."))

    # statement-level warnings
    sw = qp.find(NS + "Warnings")
    if sw is not None:
        for pc in sw.findall(NS + "PlanAffectingConvert"):
            add_convert_finding(pc, findings, "statement")
        for mgw in sw.findall(NS + "MemoryGrantWarning"):
            findings.append(Finding("MEDIUM", f"Memory grant warning: {mgw.get('GrantWarningKind')}",
                                    f"Requested {mgw.get('RequestedMemory')} KB, granted {mgw.get('GrantedMemory')} KB, "
                                    f"max used {mgw.get('MaxUsedMemory')} KB.",
                                    "Fix the cardinality estimate driving the grant."))
        if sw.find(NS + "UnmatchedIndexes") is not None:
            findings.append(Finding("MEDIUM", "Filtered index could not be used (UnmatchedIndexes)",
                                    "Parameterized predicate cannot guarantee the filter matches.",
                                    "Use a literal, OPTION (RECOMPILE), or dynamic SQL for that predicate."))
        if sw.get("NoJoinPredicate") in ("1", "true"):
            findings.append(Finding("HIGH", "No join predicate in statement", "", "Verify join conditions."))

    # parameter sniffing
    pl = qp.find(NS + "ParameterList")
    if pl is not None:
        rows = []
        for cr in pl.findall(NS + "ColumnReference"):
            comp, run = cr.get("ParameterCompiledValue"), cr.get("ParameterRuntimeValue")
            dtype = cr.get("ParameterDataType", "")
            rows.append((cr.get("Column"), dtype, comp, run))
        if rows:
            print("  Parameters: " + "; ".join(
                f"{c} {d} compiled={cv} runtime={rv}" if rv is not None else f"{c} {d} compiled={cv}"
                for c, d, cv, rv in rows))
        diffs = [r for r in rows if r[2] is not None and r[3] is not None and r[2] != r[3]]
        if diffs:
            findings.append(Finding("INFO", "Compiled ≠ runtime parameter values (possible parameter sniffing)",
                                    "; ".join(f"{c}: compiled {cv}, runtime {rv}" for c, _, cv, rv in diffs),
                                    "If misestimates appear below, see the parameter sniffing decision tree "
                                    "(references/diagnostics.md)."))

    # missing indexes
    for grp in qp.iter(NS + "MissingIndexGroup"):
        impact = grp.get("Impact")
        for mi in grp.findall(NS + "MissingIndex"):
            tbl = ".".join(mi.get(k, "").strip("[]") for k in ("Schema", "Table"))
            cols = {"EQUALITY": [], "INEQUALITY": [], "INCLUDE": []}
            for cg in mi.findall(NS + "ColumnGroup"):
                cols.setdefault(cg.get("Usage"), []).extend(
                    c.get("Name", "").strip("[]") for c in cg.findall(NS + "Column"))
            keys = cols["EQUALITY"] + cols["INEQUALITY"]
            ddl = f"CREATE INDEX IX_{mi.get('Table', '').strip('[]')}_{'_'.join(keys)[:60]} ON {tbl} ({', '.join(keys)})"
            if cols["INCLUDE"]:
                ddl += f" INCLUDE ({', '.join(cols['INCLUDE'])})"
            findings.append(Finding("INFO", f"Missing index suggestion (impact {impact}%) on {tbl}",
                                    ddl + ";",
                                    "Do NOT create blindly: merge with existing indexes, check key order, write cost."))

    # stats usage
    osu = qp.find(NS + "OptimizerStatsUsage")
    if osu is not None:
        for si in osu.findall(NS + "StatisticsInfo"):
            pct, mods = f(si, "SamplingPercent"), f(si, "ModificationCount", 0)
            rows = f(si, "TableCardinality") or 0
            name = f"{si.get('Table', '').strip('[]')}.{si.get('Statistics', '').strip('[]')}"
            if rows and mods >= max(0.2 * rows, 1000):
                findings.append(Finding("MEDIUM", f"Stale statistics: {name}",
                                        f"{fmt_n(mods)} modifications on {fmt_n(rows)} rows; last update {si.get('LastUpdate')}.",
                                        f"UPDATE STATISTICS {si.get('Schema', '')}.{si.get('Table', '')} "
                                        f"{si.get('Statistics', '')} WITH FULLSCAN;"))
            elif pct is not None and pct < 5 and rows >= 1e6:
                findings.append(Finding("LOW", f"Low sample rate {pct:.2f}%: {name}",
                                        f"{fmt_n(rows)} rows.",
                                        "If estimates on this column are off, use FULLSCAN + PERSIST_SAMPLE_PERCENT."))

    # waits
    ws = qp.find(NS + "WaitStats")
    if ws is not None:
        waits = sorted(((w.get("WaitType"), f(w, "WaitTimeMs", 0)) for w in ws.findall(NS + "Wait")),
                       key=lambda x: -x[1])
        if waits:
            print("  Top waits: " + ", ".join(f"{t} {fmt_n(ms)} ms" for t, ms in waits[:5]))

    # operator tree
    root_el = qp.find(NS + "RelOp")
    if root_el is None:
        return
    root = Op(root_el)
    ops = list(root.walk())
    for op in ops:
        analyze_operator(op, findings)

    actual = any(o.actual for o in ops)
    print(f"  Plan type: {'ACTUAL' if actual else 'ESTIMATED (no runtime stats: misestimate/spill checks limited)'}"
          f", {len(ops)} operators")

    if actual:
        ranked = sorted(ops, key=lambda o: -o.exclusive_ms)[:top_n]
        total = max(sum(o.exclusive_ms for o in ops), 1e-9)
        print(f"\n  Hottest operators (approx. exclusive elapsed time):")
        print(f"    {'ms':>9} {'%':>5} {'act rows':>9} {'est/exec':>9} {'execs':>7} {'reads':>9} mode   operator")
        for o in ranked:
            if o.exclusive_ms <= 0 and o.reads <= 0:
                continue
            print(f"    {o.exclusive_ms:9.0f} {100 * o.exclusive_ms / total:5.1f} {fmt_n(o.act_rows):>9} "
                  f"{fmt_n(o.est_rows):>9} {fmt_n(o.act_execs):>7} {fmt_n(o.reads):>9} {o.act_mode:<6} {o.label()}")
        by_reads = sorted((o for o in ops if o.reads), key=lambda o: -o.reads)[:5]
        if by_reads:
            print("\n  Most logical reads: " + "; ".join(f"Node {o.node_id} {o.obj or o.physical}: {fmt_n(o.reads)}"
                                                       for o in by_reads))
    else:
        ranked = sorted(ops, key=lambda o: -o.cost)[:top_n]
        print("\n  Highest estimated subtree costs (estimates only):")
        for o in ranked:
            print(f"    cost {o.cost:10.2f}  est rows {fmt_n(o.est_total_rows):>8}  {o.label()}")

    findings.sort(key=lambda x: SEV_ORDER[x.severity])
    print(f"\n  Findings ({len(findings)}):")
    if not findings:
        print("    none flagged by heuristics: inspect the hottest operators manually.")
    for i, fd in enumerate(findings, 1):
        print(f"\n  {i}. [{fd.severity}] {fd.title}")
        if fd.detail:
            print(f"     {fd.detail}")
        print(f"     → {fd.fix}")
    print()


def main():
    ap = argparse.ArgumentParser(description="Analyze SQL Server showplan XML.")
    ap.add_argument("plan", help="path to .sqlplan/.xml file, or '-' for stdin")
    ap.add_argument("--top", type=int, default=10, help="number of hottest operators to list")
    args = ap.parse_args()

    data = sys.stdin.buffer.read() if args.plan == "-" else open(args.plan, "rb").read()
    # SSMS saves UTF-16 with BOM; ElementTree handles it if we let it see bytes
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        try:
            root = ET.fromstring(data.decode("utf-16"))
        except Exception as e:
            sys.exit(f"Could not parse showplan XML: {e}")

    stmts = [s for s in root.iter() if tag(s) in ("StmtSimple", "StmtCursor") and s.find(NS + "QueryPlan") is not None]
    if not stmts:
        stmts = [s for s in root.iter() if tag(s) == "StmtSimple"]
    if not stmts:
        sys.exit("No statements found: is this a SQL Server showplan XML?")
    for s in stmts:
        analyze_statement(s, args.top)


if __name__ == "__main__":
    main()
