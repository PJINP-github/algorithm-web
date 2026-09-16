from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import logging
import sqlite3
from pathlib import Path
from typing import Any


VALID_AUDIT_STATUSES = (1, 2, 3)
INDEX_TABLE = "station_index"
LEGACY_TABLE = "annotations"
STATION_COLUMNS = {
    "id",
    "point_code",
    "recognition",
    "audit_status",
    "updated_at",
}
RESERVED_TABLE_NAMES = {
    INDEX_TABLE.casefold(),
    LEGACY_TABLE.casefold(),
    "sqlite_sequence",
}


@dataclass(frozen=True)
class AnnotationRecord:
    station_sn: str
    point_code: str
    recognition: str
    audit_status: int
    updated_at: str


class AnnotationDatabase:
    """Persistent audit history shared by Y/N processing and review mode."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._transaction() as connection:
            self._ensure_index_table(connection)
            if self._table_exists(connection, LEGACY_TABLE):
                self._migrate_legacy_table(connection)
            self._repair_index(connection)

    def _ensure_index_table(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_quote_identifier(INDEX_TABLE)} (
                station_sn TEXT PRIMARY KEY
            )
            """
        )

    def _migrate_legacy_table(self, connection: sqlite3.Connection) -> None:
        columns = _table_columns(connection, LEGACY_TABLE)
        missing_columns = (STATION_COLUMNS | {"station_sn"}) - columns
        if missing_columns:
            raise ValueError(
                f"旧 annotations 表缺少字段: {sorted(missing_columns)}"
            )

        rows = connection.execute(
            f"""
            SELECT id, station_sn, point_code, recognition, audit_status, updated_at
            FROM {_quote_identifier(LEGACY_TABLE)}
            ORDER BY id
            """
        ).fetchall()
        migrated = 0
        skipped = 0
        for row in rows:
            station_sn = _text(row[1])
            if not _valid_station_sn(station_sn):
                skipped += 1
                continue
            self._ensure_station_table(connection, station_sn)
            connection.execute(
                f"""
                INSERT INTO {_quote_identifier(station_sn)} (
                    id, point_code, recognition, audit_status, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (point_code) DO UPDATE SET
                    recognition = excluded.recognition,
                    audit_status = excluded.audit_status,
                    updated_at = excluded.updated_at
                """,
                (row[0], row[2], row[3], row[4], row[5]),
            )
            connection.execute(
                f"""
                INSERT OR IGNORE INTO {_quote_identifier(INDEX_TABLE)} (station_sn)
                VALUES (?)
                """,
                (station_sn,),
            )
            migrated += 1

        connection.execute(f"DROP TABLE {_quote_identifier(LEGACY_TABLE)}")
        logging.info(
            "SQLite 标注数据库已从旧大表拆分: migrated=%s skipped=%s",
            migrated,
            skipped,
        )

    def _repair_index(self, connection: sqlite3.Connection) -> None:
        station_tables = {
            name
            for name in _table_names(connection)
            if name.casefold() not in RESERVED_TABLE_NAMES
            and _is_station_table(connection, name)
        }
        for station_sn in station_tables:
            if _valid_station_sn(station_sn):
                connection.execute(
                    f"""
                    INSERT OR IGNORE INTO {_quote_identifier(INDEX_TABLE)} (station_sn)
                    VALUES (?)
                    """,
                    (station_sn,),
                )
            else:
                connection.execute(
                    f"DROP TABLE {_quote_identifier(station_sn)}"
                )

        indexed_stations = {
            str(row[0])
            for row in connection.execute(
                f"SELECT station_sn FROM {_quote_identifier(INDEX_TABLE)}"
            ).fetchall()
        }
        for station_sn in indexed_stations - station_tables:
            connection.execute(
                f"""
                DELETE FROM {_quote_identifier(INDEX_TABLE)}
                WHERE station_sn = ?
                """,
                (station_sn,),
            )
        connection.execute(
            f"""
            DELETE FROM {_quote_identifier(INDEX_TABLE)}
            WHERE lower(station_sn) = 'testcode'
            """
        )

    def _ensure_station_table(
        self,
        connection: sqlite3.Connection,
        station_sn: str,
    ) -> None:
        if not _valid_station_sn(station_sn):
            raise ValueError(f"不允许使用的 stationSN: {station_sn!r}")
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_quote_identifier(station_sn)} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                point_code TEXT NOT NULL UNIQUE,
                recognition TEXT NOT NULL,
                audit_status INTEGER NOT NULL CHECK (audit_status IN (1, 2, 3)),
                updated_at TEXT NOT NULL
            )
            """
        )
        if _table_columns(connection, station_sn) != STATION_COLUMNS:
            raise ValueError(f"stationSN 子表字段不符合要求: {station_sn}")

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
        row = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table_name,),
        ).fetchone()
        return row is not None

    def get(self, station_sn: str, point_code: str) -> AnnotationRecord | None:
        station_sn = _text(station_sn)
        point_code = _text(point_code)
        if not _valid_station_sn(station_sn) or not point_code:
            return None

        with self._transaction() as connection:
            if not self._station_is_indexed(connection, station_sn):
                return None
            row = connection.execute(
                f"""
                SELECT point_code, recognition, audit_status, updated_at
                FROM {_quote_identifier(station_sn)}
                WHERE point_code = ?
                """,
                (point_code,),
            ).fetchone()
        if row is None:
            return None
        return AnnotationRecord(
            station_sn=station_sn,
            point_code=str(row[0]),
            recognition=str(row[1]),
            audit_status=int(row[2]),
            updated_at=str(row[3]),
        )

    def get_station(self, station_sn: str) -> dict[str, AnnotationRecord]:
        station_sn = _text(station_sn)
        if not _valid_station_sn(station_sn):
            return {}

        with self._transaction() as connection:
            if not self._station_is_indexed(connection, station_sn):
                return {}
            rows = connection.execute(
                f"""
                SELECT point_code, recognition, audit_status, updated_at
                FROM {_quote_identifier(station_sn)}
                ORDER BY point_code
                """
            ).fetchall()
        return {
            str(row[0]): AnnotationRecord(
                station_sn=station_sn,
                point_code=str(row[0]),
                recognition=str(row[1]),
                audit_status=int(row[2]),
                updated_at=str(row[3]),
            )
            for row in rows
        }

    def get_all(self) -> list[AnnotationRecord]:
        with self._transaction() as connection:
            station_names = [
                str(row[0])
                for row in connection.execute(
                    f"""
                    SELECT station_sn
                    FROM {_quote_identifier(INDEX_TABLE)}
                    ORDER BY station_sn
                    """
                ).fetchall()
            ]
            records: list[AnnotationRecord] = []
            for station_sn in station_names:
                rows = connection.execute(
                    f"""
                    SELECT point_code, recognition, audit_status, updated_at
                    FROM {_quote_identifier(station_sn)}
                    ORDER BY point_code
                    """
                ).fetchall()
                records.extend(
                    AnnotationRecord(
                        station_sn=station_sn,
                        point_code=str(row[0]),
                        recognition=str(row[1]),
                        audit_status=int(row[2]),
                        updated_at=str(row[3]),
                    )
                    for row in rows
                )
        return records

    def station_names(self) -> list[str]:
        with self._transaction() as connection:
            return [
                str(row[0])
                for row in connection.execute(
                    f"""
                    SELECT station_sn
                    FROM {_quote_identifier(INDEX_TABLE)}
                    ORDER BY station_sn
                    """
                ).fetchall()
            ]

    def upsert(
        self,
        station_sn: str,
        point_code: str,
        recognition: str,
        audit_status: int,
    ) -> None:
        self.upsert_many(
            [
                {
                    "stationSN": station_sn,
                    "pointCode": point_code,
                    "recognition": recognition,
                    "auditStatus": audit_status,
                }
            ]
        )

    def upsert_many(
        self,
        items: list[dict[str, Any]],
        *,
        updated_at: str | None = None,
        only_if_newer: bool = False,
    ) -> int:
        rows: list[tuple[str, str, str, int, str]] = []
        updated_at = updated_at or datetime.now().isoformat(timespec="seconds")
        for item in items:
            station_sn = _text(item.get("stationSN"))
            point_code = _text(item.get("pointCode"))
            recognition = _text(item.get("recognition"))
            audit_status = _audit_status(item.get("auditStatus"))
            if (
                _valid_station_sn(station_sn)
                and point_code
                and audit_status in VALID_AUDIT_STATUSES
            ):
                rows.append((station_sn, point_code, recognition, audit_status, updated_at))
        if not rows:
            return 0

        with self._transaction() as connection:
            grouped: dict[str, list[tuple[str, str, int, str]]] = {}
            for station_sn, point_code, recognition, audit_status, row_updated_at in rows:
                grouped.setdefault(station_sn, []).append(
                    (point_code, recognition, audit_status, row_updated_at)
                )
            for station_sn, station_rows in grouped.items():
                self._ensure_station_table(connection, station_sn)
                connection.execute(
                    f"""
                    INSERT OR IGNORE INTO {_quote_identifier(INDEX_TABLE)} (station_sn)
                    VALUES (?)
                    """,
                    (station_sn,),
                )
                if only_if_newer:
                    connection.executemany(
                        f"""
                        INSERT INTO {_quote_identifier(station_sn)} (
                            point_code, recognition, audit_status, updated_at
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT (point_code) DO UPDATE SET
                            recognition = excluded.recognition,
                            audit_status = excluded.audit_status,
                            updated_at = excluded.updated_at
                        WHERE excluded.updated_at >= {_quote_identifier(station_sn)}.updated_at
                        """,
                        station_rows,
                    )
                else:
                    connection.executemany(
                        f"""
                        INSERT INTO {_quote_identifier(station_sn)} (
                            point_code, recognition, audit_status, updated_at
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT (point_code) DO UPDATE SET
                            recognition = excluded.recognition,
                            audit_status = excluded.audit_status,
                            updated_at = excluded.updated_at
                        """,
                        station_rows,
                    )
        return len(rows)

    def _station_is_indexed(
        self,
        connection: sqlite3.Connection,
        station_sn: str,
    ) -> bool:
        row = connection.execute(
            f"""
            SELECT 1
            FROM {_quote_identifier(INDEX_TABLE)}
            WHERE station_sn = ?
            """,
            (station_sn,),
        ).fetchone()
        return row is not None

    def apply_to_accuracy(self, accuracy: dict[str, Any]) -> int:
        """Apply exact pointCode + recognition matches and move them to the end."""
        station_sn = find_metadata_value(accuracy, "stationSN")
        if not station_sn:
            return 0

        records = self.get_station(station_sn)
        applied = 0
        for statistic in accuracy.get("data", {}).get("statistic", []):
            if not isinstance(statistic, dict):
                continue
            images = statistic.get("imagesData")
            if not isinstance(images, list):
                continue

            confirmed: list[Any] = []
            unconfirmed: list[Any] = []
            for item in images:
                if not isinstance(item, dict):
                    unconfirmed.append(item)
                    continue

                record = None
                if _audit_status(item.get("auditStatus")) == 0:
                    point_code = _text(item.get("pointCode"))
                    record = records.get(point_code)
                    if record and record.recognition != _text(item.get("recognition")):
                        record = None

                if record is None:
                    unconfirmed.append(item)
                    continue

                item["auditStatus"] = record.audit_status
                confirmed.append(item)
                applied += 1

            if confirmed:
                statistic["imagesData"] = unconfirmed + confirmed
                _recompute_statistic(statistic)

        return applied

    def upsert_marked_accuracy(
        self,
        accuracy: dict[str, Any],
        *,
        updated_at: str | None = None,
        only_if_newer: bool = False,
    ) -> int:
        """Store only confirmed audit results from an accuracy document."""
        station_sn = find_metadata_value(accuracy, "stationSN")
        if not station_sn:
            return 0

        items: list[dict[str, Any]] = []
        for statistic in accuracy.get("data", {}).get("statistic", []):
            if not isinstance(statistic, dict):
                continue
            images = statistic.get("imagesData")
            if not isinstance(images, list):
                continue
            for item in images:
                if not isinstance(item, dict):
                    continue
                audit_status = _audit_status(item.get("auditStatus"))
                point_code = _text(item.get("pointCode"))
                if audit_status not in VALID_AUDIT_STATUSES or not point_code:
                    continue
                items.append(
                    {
                        "stationSN": station_sn,
                        "pointCode": point_code,
                        "recognition": _text(item.get("recognition")),
                        "auditStatus": audit_status,
                    }
                )
        return self.upsert_many(
            items,
            updated_at=updated_at,
            only_if_newer=only_if_newer,
        )


def find_metadata_value(value: Any, target_key: str) -> str:
    target_key = target_key.casefold()

    def walk(node: Any) -> str:
        if isinstance(node, dict):
            for key, child in node.items():
                if str(key).casefold() == target_key and child is not None:
                    text = _text(child)
                    if text:
                        return text
                found = walk(child)
                if found:
                    return found
        elif isinstance(node, list):
            for child in node:
                found = walk(child)
                if found:
                    return found
        return ""

    return walk(value)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _valid_station_sn(station_sn: str) -> bool:
    return bool(station_sn) and station_sn.casefold() != "testcode"


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(
            f"PRAGMA table_info({_quote_identifier(table_name)})"
        ).fetchall()
    }


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        ).fetchall()
    }


def _is_station_table(connection: sqlite3.Connection, table_name: str) -> bool:
    return _table_columns(connection, table_name) == STATION_COLUMNS


def _audit_status(value: Any) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 0
    return value if value in (0, *VALID_AUDIT_STATUSES) else 0


def _recompute_statistic(statistic: dict[str, Any]) -> None:
    images = statistic.get("imagesData") or []
    total = len(images)
    correct = sum(
        1 for item in images if isinstance(item, dict) and _audit_status(item.get("auditStatus")) == 1
    )
    inaccuracy = sum(
        1 for item in images if isinstance(item, dict) and _audit_status(item.get("auditStatus")) == 2
    )
    problem = sum(
        1 for item in images if isinstance(item, dict) and _audit_status(item.get("auditStatus")) == 3
    )
    detected = correct + inaccuracy

    statistic["pointTotal"] = total
    if "correct" in statistic:
        statistic["correct"] = correct
    if "inaccuracy" in statistic:
        statistic["inaccuracy"] = inaccuracy
    if "blur" in statistic:
        statistic["blur"] = problem
    if "accuracyRate" in statistic:
        statistic["accuracyRate"] = int(correct * 100 / detected) if detected else 0
    if "detectionRate" in statistic:
        statistic["detectionRate"] = int(detected * 100 / total) if total else 0
