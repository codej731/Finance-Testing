"""DuckDB storage and per-run Parquet snapshots (schema §7).

The DuckDB file holds the latest state of every table. Each run also writes
its inputs and outputs to snapshots/{run_id}/ so the run can be reproduced
exactly after the database has moved on (D-007).
"""

from __future__ import annotations

import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from data.schema import RUN_SCOPED_TABLES, SOURCES, TABLES, quoted
from data.transform import FxProvider, fundamentals_panel

# Tables keyed by security that are snapshotted for the run's universe.
SECURITY_SCOPED_TABLES = ("securities", "fundamentals", "prices_daily",
                          "insider_transactions", "corporate_actions", "watchlist")


def new_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def snapshot_dirname(run_id: str) -> str:
    return run_id.replace(":", "")  # colons are not valid in Windows paths


def code_version(repo_dir: Path | None = None) -> str:
    repo_dir = repo_dir or Path(__file__).resolve().parent.parent
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True,
                             text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                               cwd=repo_dir, capture_output=True, text=True).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


class Store:
    def __init__(self, db_path: str | Path, snapshot_dir: str | Path):
        self.db_path = Path(db_path)
        self.snapshot_dir = Path(snapshot_dir)
        if str(db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(db_path))
        for table in TABLES.values():
            self.con.execute(table.ddl())

    def close(self) -> None:
        self.con.close()

    # -- writes -------------------------------------------------------------

    def write(self, table: str, df: pd.DataFrame) -> int:
        """Insert rows; rows with an existing primary key are replaced."""
        if df is None or df.empty:
            return 0
        spec = TABLES[table]
        cols = spec.column_names
        frame = df.reindex(columns=cols)
        self.con.register("_incoming", frame)
        try:
            verb = "INSERT OR REPLACE" if spec.primary_key else "INSERT"
            col_list = quoted(cols)
            if spec.primary_key:
                # The incoming batch itself must be unique on the key.
                pk = quoted(spec.primary_key)
                select = (f"SELECT {col_list} FROM _incoming "
                          f"QUALIFY row_number() OVER (PARTITION BY {pk}) = 1")
            else:
                select = f"SELECT {col_list} FROM _incoming"
            self.con.execute(f"{verb} INTO {table} ({col_list}) {select}")
        finally:
            self.con.unregister("_incoming")
        return len(frame)

    def write_securities(self, df: pd.DataFrame) -> int:
        """Upsert the security master, keeping first_seen and any known CIK."""
        if df is None or df.empty:
            return 0
        cols = TABLES["securities"].column_names
        self.con.register("_incoming", df.reindex(columns=cols))
        try:
            updates = ", ".join(
                f"{c} = coalesce(excluded.{c}, securities.{c})" for c in cols
                if c not in ("security_id", "first_seen", "last_seen"))
            self.con.execute(f"""
                INSERT INTO securities ({', '.join(cols)})
                SELECT {', '.join(cols)} FROM _incoming
                QUALIFY row_number() OVER (PARTITION BY security_id) = 1
                ON CONFLICT (security_id) DO UPDATE SET {updates},
                    first_seen = least(securities.first_seen, excluded.first_seen),
                    last_seen = greatest(securities.last_seen, excluded.last_seen)
            """)
        finally:
            self.con.unregister("_incoming")
        return len(df)

    def mark_inactive(self, seen_ids: list[str]) -> None:
        """Securities absent from the latest listing stay in the master (survivorship)."""
        self.con.register("_seen", pd.DataFrame({"security_id": seen_ids}))
        try:
            self.con.execute("""UPDATE securities SET is_active = FALSE
                                WHERE security_id NOT IN (SELECT security_id FROM _seen)""")
        finally:
            self.con.unregister("_seen")

    # -- runs ---------------------------------------------------------------

    def start_run(self, config_hash: str, notes: str | None = None,
                  run_id: str | None = None) -> str:
        run_id = base = run_id or new_run_id()
        n = 1
        while self.con.execute("SELECT 1 FROM run_log WHERE run_id = ?", [run_id]).fetchone():
            n += 1
            run_id = f"{base}-{n}"
        self.write("run_log", pd.DataFrame([{
            "run_id": run_id, "config_hash": config_hash, "code_version": code_version(),
            "universe_size": None, "notes": notes,
            "started_at": datetime.now(timezone.utc).replace(tzinfo=None),
            "finished_at": None,
        }]))
        return run_id

    def finish_run(self, run_id: str, universe_size: int, notes: str | None = None) -> None:
        self.con.execute("""UPDATE run_log SET universe_size = ?, finished_at = ?,
                            notes = coalesce(?, notes) WHERE run_id = ?""",
                         [universe_size, datetime.now(timezone.utc).replace(tzinfo=None),
                          notes, run_id])

    # -- reads --------------------------------------------------------------

    def read(self, table: str, where: str | None = None, params: list | None = None
             ) -> pd.DataFrame:
        sql = f"SELECT * FROM {table}" + (f" WHERE {where}" if where else "")
        return self.con.execute(sql, params or []).df()

    def fundamentals(self, security_ids: list[str] | None = None) -> pd.DataFrame:
        if security_ids is None:
            df = self.read("fundamentals")
        else:
            df = self.read("fundamentals", "list_contains(?, security_id)", [security_ids])
        for col in ("period_end", "available_date"):
            df[col] = pd.to_datetime(df[col]).dt.date
        return df

    def panel(self, period_type: str = "FY", *, as_of: date | None = None,
              security_ids: list[str] | None = None,
              priority: tuple[str, ...] = SOURCES,
              currency: dict[str, str] | str | None = None,
              fx: FxProvider | None = None) -> pd.DataFrame:
        """Wide, point-in-time, source-resolved fundamentals (see data.transform)."""
        return fundamentals_panel(self.fundamentals(security_ids), period_type, as_of=as_of,
                                  priority=priority, currency=currency, fx=fx)

    # -- snapshots ----------------------------------------------------------

    def snapshot(self, run_id: str, security_ids: list[str],
                 tables: tuple[str, ...] | None = None) -> Path:
        """Write the run's inputs and outputs to snapshots/{run_id}/*.parquet.

        `tables` limits the write to those tables, so a later stage can add its
        outputs without re-copying inputs that may have changed since the run.
        """
        out = self.snapshot_dir / snapshot_dirname(run_id)
        out.mkdir(parents=True, exist_ok=True)
        self.con.register("_universe", pd.DataFrame({"security_id": security_ids}))
        try:
            queries = {"run_log": ("SELECT * FROM run_log WHERE run_id = $run_id")}
            for t in RUN_SCOPED_TABLES:
                queries[t] = f"SELECT * FROM {t} WHERE run_id = $run_id"
            for t in SECURITY_SCOPED_TABLES:
                queries[t] = (f"SELECT * FROM {t} WHERE security_id IN "
                              f"(SELECT security_id FROM _universe)")
            if tables is not None:
                queries = {t: q for t, q in queries.items() if t in tables}
            for table, sql in queries.items():
                path = (out / f"{table}.parquet").as_posix()
                params = {"run_id": run_id} if "$run_id" in sql else {}
                self.con.execute(f"COPY ({sql}) TO '{path}' (FORMAT PARQUET)", params)
        finally:
            self.con.unregister("_universe")
        return out


def load_snapshot(snapshot_dir: str | Path, run_id: str) -> dict[str, pd.DataFrame]:
    """Read back every table of a past run."""
    path = Path(snapshot_dir) / snapshot_dirname(run_id)
    return {p.stem: pd.read_parquet(p) for p in sorted(path.glob("*.parquet"))}
