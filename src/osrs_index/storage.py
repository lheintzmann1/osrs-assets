"""SQLite persistence.

SQLite rather than Postgres/TimescaleDB because at Phase 0 volumes the
difference is unmeasurable and the setup cost is not: `git clone && python3
-m osrs_index collect` has to work, or nobody reproduces your index.

Sizing, for the record: screening to ~2500 liquid items and storing 5m bars
gives ~720k rows/day, roughly 36 MB/day, ~13 GB/year. SQLite handles that.
Beyond a year of 5m bars, or once several indices need concurrent writers,
move `price_bar` to a TimescaleDB hypertable -- the schema is deliberately
portable and the queries here are plain SQL.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

from .models import Bar, IndexLevel, Item

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS item (
    id            INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL,
    members       INTEGER NOT NULL,
    buy_limit     INTEGER,
    ge_value      INTEGER,
    highalch      INTEGER,
    lowalch       INTEGER,
    first_seen    INTEGER NOT NULL,
    last_seen     INTEGER NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS item_name_idx ON item(name);

-- Aggregate buckets. The only sanctioned input to a NAV.
CREATE TABLE IF NOT EXISTS price_bar (
    item_id   INTEGER NOT NULL,
    ts        INTEGER NOT NULL,
    step      TEXT    NOT NULL,
    avg_high  INTEGER,
    avg_low   INTEGER,
    vol_high  INTEGER NOT NULL DEFAULT 0,
    vol_low   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (item_id, ts, step)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS price_bar_step_ts_idx ON price_bar(step, ts);

-- Last instant-buy / instant-sell prints. Stored for microstructure research
-- and manipulation detection. NEVER read by the NAV path: see nav.py for the
-- staleness and crossing statistics that make it unusable for valuation.
CREATE TABLE IF NOT EXISTS price_tick (
    item_id    INTEGER NOT NULL,
    ts         INTEGER NOT NULL,
    high       INTEGER,
    high_time  INTEGER,
    low        INTEGER,
    low_time   INTEGER,
    PRIMARY KEY (item_id, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS index_def (
    index_id        TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT,
    method          TEXT NOT NULL,
    rebalance_rule  TEXT NOT NULL,
    base_level      REAL NOT NULL,
    inception_ts    INTEGER
);

CREATE TABLE IF NOT EXISTS index_member (
    index_id        TEXT    NOT NULL,
    item_id         INTEGER NOT NULL,
    effective_from  INTEGER NOT NULL,
    effective_to    INTEGER,
    target_weight   REAL    NOT NULL,
    units           REAL    NOT NULL,
    was_capped      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (index_id, item_id, effective_from)
);

CREATE TABLE IF NOT EXISTS index_value (
    index_id         TEXT    NOT NULL,
    ts               INTEGER NOT NULL,
    level            REAL    NOT NULL,
    divisor          REAL    NOT NULL,
    basket_value_gp  REAL    NOT NULL,
    n_members        INTEGER NOT NULL,
    n_stale_members  INTEGER NOT NULL,
    quality          TEXT    NOT NULL,
    PRIMARY KEY (index_id, ts)
) WITHOUT ROWID;

-- Every discretionary act on an index leaves a row here. Divisor changes
-- without an audit trail are how index providers lose credibility.
CREATE TABLE IF NOT EXISTS corporate_action (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    index_id        TEXT    NOT NULL,
    ts              INTEGER NOT NULL,
    kind            TEXT    NOT NULL,
    item_id         INTEGER,
    note            TEXT,
    divisor_before  REAL,
    divisor_after   REAL
);
CREATE INDEX IF NOT EXISTS corporate_action_index_ts_idx ON corporate_action(index_id, ts);

CREATE TABLE IF NOT EXISTS collection_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job         TEXT    NOT NULL,
    started_at  INTEGER NOT NULL,
    finished_at INTEGER,
    rows        INTEGER,
    ok          INTEGER,
    error       TEXT
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ items

    def upsert_items(self, items: Iterable[Item], now: int) -> int:
        rows = [
            (i.id, i.name, int(i.members), i.buy_limit, i.ge_value, i.highalch, i.lowalch, now, now)
            for i in items
        ]
        self.conn.executemany(
            """
            INSERT INTO item (id, name, members, buy_limit, ge_value, highalch, lowalch,
                              first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name      = excluded.name,
                members   = excluded.members,
                buy_limit = excluded.buy_limit,
                ge_value  = excluded.ge_value,
                highalch  = excluded.highalch,
                lowalch   = excluded.lowalch,
                last_seen = excluded.last_seen,
                is_active = 1
            """,
            rows,
        )
        self.conn.commit()
        return len(rows)

    def deactivate_missing_items(self, seen_ids: Sequence[int], now: int) -> int:
        """Flag items that vanished from /mapping.

        Jagex removing a tradeable item is a delisting event for any index
        holding it, and the only notification you get is its disappearance
        from this endpoint. Never hard-delete: the history has to stay
        readable after the fact.
        """
        placeholders = ",".join("?" for _ in seen_ids)
        cursor = self.conn.execute(
            f"UPDATE item SET is_active = 0 WHERE is_active = 1 AND id NOT IN ({placeholders})",
            list(seen_ids),
        )
        self.conn.commit()
        return cursor.rowcount

    def items(self, active_only: bool = True) -> list[Item]:
        sql = "SELECT * FROM item"
        if active_only:
            sql += " WHERE is_active = 1"
        return [
            Item(
                id=r["id"],
                name=r["name"],
                members=bool(r["members"]),
                buy_limit=r["buy_limit"],
                ge_value=r["ge_value"],
                highalch=r["highalch"],
                lowalch=r["lowalch"],
            )
            for r in self.conn.execute(sql)
        ]

    def item_by_name(self, name: str) -> Item | None:
        row = self.conn.execute(
            "SELECT * FROM item WHERE lower(name) = lower(?)", (name,)
        ).fetchone()
        if row is None:
            return None
        return Item(
            id=row["id"],
            name=row["name"],
            members=bool(row["members"]),
            buy_limit=row["buy_limit"],
            ge_value=row["ge_value"],
            highalch=row["highalch"],
            lowalch=row["lowalch"],
        )

    # ------------------------------------------------------------------- bars

    def insert_bars(self, bars: Iterable[Bar]) -> int:
        rows = [(b.item_id, b.ts, b.step, b.avg_high, b.avg_low, b.vol_high, b.vol_low) for b in bars]
        self.conn.executemany(
            """
            INSERT INTO price_bar (item_id, ts, step, avg_high, avg_low, vol_high, vol_low)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id, ts, step) DO UPDATE SET
                avg_high = excluded.avg_high,
                avg_low  = excluded.avg_low,
                vol_high = excluded.vol_high,
                vol_low  = excluded.vol_low
            """,
            rows,
        )
        self.conn.commit()
        return len(rows)

    def bars(
        self, item_id: int, step: str, limit: int = 24, as_of: int | None = None
    ) -> list[Bar]:
        """Most recent `limit` bars at or before `as_of`.

        `as_of` is not optional decoration. Without it a historical replay
        reads bars that had not happened yet, and the backtest silently
        becomes a description of the future -- the single most common way a
        published index history turns out to be worthless.
        """
        rows = self.conn.execute(
            """
            SELECT * FROM price_bar
            WHERE item_id = ? AND step = ? AND (? IS NULL OR ts <= ?)
            ORDER BY ts DESC
            LIMIT ?
            """,
            (item_id, step, as_of, as_of, limit),
        ).fetchall()
        return [
            Bar(
                item_id=r["item_id"],
                ts=r["ts"],
                step=r["step"],
                avg_high=r["avg_high"],
                avg_low=r["avg_low"],
                vol_high=r["vol_high"],
                vol_low=r["vol_low"],
            )
            for r in reversed(rows)
        ]

    def bar_dates(self, step: str, item_ids: list[int]) -> list[int]:
        """Distinct timestamps for which any of these items has a bar."""
        if not item_ids:
            return []
        placeholders = ",".join("?" for _ in item_ids)
        rows = self.conn.execute(
            f"SELECT DISTINCT ts FROM price_bar WHERE step = ? AND item_id IN ({placeholders}) "
            "ORDER BY ts",
            [step, *item_ids],
        )
        return [r["ts"] for r in rows]

    def history_days(self, item_id: int, step: str, as_of: int | None = None) -> float:
        """Span of stored history in days, from the data itself.

        Must not be inferred from the number of rows a caller happened to
        fetch: reading the last 30 bars and concluding "30 days of history"
        makes a 90-day minimum permanently unsatisfiable. That bug rejected
        every constituent in every basket on first run.
        """
        row = self.conn.execute(
            "SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM price_bar "
            "WHERE item_id = ? AND step = ? AND (? IS NULL OR ts <= ?)",
            (item_id, step, as_of, as_of),
        ).fetchone()
        if not row or row["lo"] is None:
            return 0.0
        return (row["hi"] - row["lo"]) / 86400.0

    def latest_bar_ts(self, step: str) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(ts) AS ts FROM price_bar WHERE step = ?", (step,)
        ).fetchone()
        return row["ts"] if row and row["ts"] is not None else None

    def bar_count(self, step: str | None = None) -> int:
        if step:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM price_bar WHERE step = ?", (step,)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM price_bar").fetchone()
        return row["n"]

    # ------------------------------------------------------------------ ticks

    def insert_ticks(self, ticks: Iterable[tuple[int, int, int | None, int | None, int | None, int | None]]) -> int:
        rows = list(ticks)
        self.conn.executemany(
            """
            INSERT INTO price_tick (item_id, ts, high, high_time, low, low_time)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id, ts) DO NOTHING
            """,
            rows,
        )
        self.conn.commit()
        return len(rows)

    # ----------------------------------------------------------------- index

    def upsert_index_def(
        self,
        index_id: str,
        name: str,
        description: str,
        method: str,
        rebalance_rule: str,
        base_level: float,
        inception_ts: int | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO index_def (index_id, name, description, method, rebalance_rule,
                                   base_level, inception_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(index_id) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                method = excluded.method,
                rebalance_rule = excluded.rebalance_rule,
                base_level = excluded.base_level
            """,
            (index_id, name, description, method, rebalance_rule, base_level, inception_ts),
        )
        self.conn.commit()

    def set_members(
        self,
        index_id: str,
        effective_from: int,
        members: Sequence[tuple[int, float, float, bool]],
    ) -> None:
        """Close the previous membership window and open a new one."""
        self.conn.execute(
            """
            UPDATE index_member SET effective_to = ?
            WHERE index_id = ? AND effective_to IS NULL
            """,
            (effective_from, index_id),
        )
        self.conn.executemany(
            """
            INSERT INTO index_member (index_id, item_id, effective_from, target_weight,
                                      units, was_capped)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(index_id, item_id, effective_from) DO UPDATE SET
                target_weight = excluded.target_weight,
                units = excluded.units,
                was_capped = excluded.was_capped
            """,
            [(index_id, i, effective_from, w, u, int(c)) for i, w, u, c in members],
        )
        self.conn.commit()

    def current_members(self, index_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT m.*, i.name FROM index_member m
                JOIN item i ON i.id = m.item_id
                WHERE m.index_id = ? AND m.effective_to IS NULL
                """,
                (index_id,),
            )
        )

    def insert_level(self, level: IndexLevel) -> None:
        self.conn.execute(
            """
            INSERT INTO index_value (index_id, ts, level, divisor, basket_value_gp,
                                     n_members, n_stale_members, quality)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(index_id, ts) DO UPDATE SET
                level = excluded.level,
                divisor = excluded.divisor,
                basket_value_gp = excluded.basket_value_gp,
                n_members = excluded.n_members,
                n_stale_members = excluded.n_stale_members,
                quality = excluded.quality
            """,
            (
                level.index_id,
                level.ts,
                level.level,
                level.divisor,
                level.basket_value_gp,
                level.n_members,
                level.n_stale_members,
                level.quality.value,
            ),
        )
        self.conn.commit()

    def levels(self, index_id: str, limit: int = 1000) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM index_value WHERE index_id = ? ORDER BY ts DESC LIMIT ?",
                (index_id, limit),
            )
        )

    def record_action(
        self,
        index_id: str,
        ts: int,
        kind: str,
        note: str,
        item_id: int | None = None,
        divisor_before: float | None = None,
        divisor_after: float | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO corporate_action (index_id, ts, kind, item_id, note,
                                          divisor_before, divisor_after)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (index_id, ts, kind, item_id, note, divisor_before, divisor_after),
        )
        self.conn.commit()

    # ------------------------------------------------------------------- runs

    def start_run(self, job: str, started_at: int) -> int:
        cursor = self.conn.execute(
            "INSERT INTO collection_run (job, started_at) VALUES (?, ?)", (job, started_at)
        )
        self.conn.commit()
        return int(cursor.lastrowid or 0)

    def finish_run(
        self, run_id: int, finished_at: int, rows: int, ok: bool, error: str | None = None
    ) -> None:
        self.conn.execute(
            "UPDATE collection_run SET finished_at = ?, rows = ?, ok = ?, error = ? WHERE id = ?",
            (finished_at, rows, int(ok), error, run_id),
        )
        self.conn.commit()

    def recent_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM collection_run ORDER BY id DESC LIMIT ?", (limit,)
            )
        )


def iter_bars_from_payload(payload: dict[str, dict], ts: int, step: str) -> Iterator[Bar]:
    """Convert an aggregate endpoint payload into Bar rows."""
    for raw_id, entry in payload.items():
        yield Bar(
            item_id=int(raw_id),
            ts=ts,
            step=step,
            avg_high=entry.get("avgHighPrice"),
            avg_low=entry.get("avgLowPrice"),
            vol_high=entry.get("highPriceVolume") or 0,
            vol_low=entry.get("lowPriceVolume") or 0,
        )
