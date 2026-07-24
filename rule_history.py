import math
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from community_rules import RULE_DEFINITIONS, normalize_rules_config


DEFAULT_HISTORY_PATH = "/shared/action-rules-history.sqlite3"
DEFAULT_RETENTION_DAYS = 90
DEFAULT_MAX_RESPONSE_POINTS = 480
HISTORY_WINDOWS = {
    "24h": 24,
    "7d": 7 * 24,
    "30d": 30 * 24,
    "90d": 90 * 24,
}

_SCHEMA_LOCK = threading.Lock()
_INITIALIZED_PATHS = set()
_MAINTENANCE_LOCK = threading.Lock()
_LAST_MAINTENANCE: Dict[str, float] = {}


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        number = default
    return max(minimum, min(maximum, number))


def _parse_timestamp_ms(value: Any) -> Optional[int]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.astimezone(timezone.utc).timestamp() * 1000)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _iso_from_ms(value: Any) -> Optional[str]:
    try:
        milliseconds = int(value)
        return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def rule_history_path(path: Optional[str] = None) -> str:
    return str(path or os.getenv("COMMUNITY_RULES_HISTORY_PATH") or DEFAULT_HISTORY_PATH)


def history_retention_days() -> int:
    return _bounded_int(
        os.getenv("COMMUNITY_RULES_HISTORY_RETENTION_DAYS"),
        DEFAULT_RETENTION_DAYS,
        90,
        365,
    )


def history_max_response_points() -> int:
    return _bounded_int(
        os.getenv("COMMUNITY_RULES_HISTORY_MAX_POINTS"),
        DEFAULT_MAX_RESPONSE_POINTS,
        120,
        1000,
    )


@contextmanager
def _connect(path: str):
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        yield connection
    finally:
        connection.close()


def _ensure_schema(path: str) -> None:
    normalized_path = os.path.abspath(path)
    if normalized_path in _INITIALIZED_PATHS and os.path.exists(normalized_path):
        return
    with _SCHEMA_LOCK:
        if normalized_path in _INITIALIZED_PATHS and os.path.exists(normalized_path):
            return
        with _connect(normalized_path) as connection:
            try:
                connection.execute("PRAGMA journal_mode = WAL")
            except sqlite3.DatabaseError:
                # SQLite can still provide safe transactions if WAL is unavailable.
                pass
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rule_history_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mint TEXT NOT NULL,
                    rule_type TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('point', 'gap', 'target')),
                    observed_at_ms INTEGER NOT NULL,
                    source_at_ms INTEGER,
                    value REAL,
                    target REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT '',
                    operator TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    scenario_key TEXT NOT NULL DEFAULT '',
                    scenario_label TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_rule_history_lookup
                ON rule_history_events (mint, rule_type, observed_at_ms, id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_rule_history_source
                ON rule_history_events (mint, rule_type, source_at_ms, scenario_key)
                WHERE kind = 'point'
                """
            )
            connection.execute("PRAGMA user_version = 1")
        _INITIALIZED_PATHS.add(normalized_path)


def _scenario(config: Dict[str, Any], runtime: Dict[str, Any], rule_type: str) -> Dict[str, Any]:
    if rule_type != "max_price_impact":
        return {"key": "", "label": ""}

    impact = runtime.get("price_impact") if isinstance(runtime.get("price_impact"), dict) else {}
    mode = str(impact.get("scenario") or config.get("sell_amount_mode") or "tracked_usdc")
    if mode == "token_amount":
        amount = _finite_number(config.get("sell_token_amount"))
        label = f"Custom {amount:g} tokens" if amount is not None else "Custom token amount"
        return {
            "key": f"token_amount:{amount:.12g}" if amount is not None else "token_amount:unknown",
            "label": label,
        }

    tracked_usdc = _finite_number(impact.get("tracked_usdc"))
    if tracked_usdc is None:
        tracked_usdc = _finite_number(runtime.get("tracked_usdc"))
    if tracked_usdc is None:
        label = "Tracked USDC amount"
        key = "tracked_usdc:unknown"
    else:
        label = f"${tracked_usdc:g} tracked amount"
        key = f"tracked_usdc:{tracked_usdc:.12g}"
    return {"key": key, "label": label}


def _last_event(connection: sqlite3.Connection, mint: str, rule_type: str) -> Optional[sqlite3.Row]:
    return connection.execute(
        """
        SELECT *
        FROM rule_history_events
        WHERE mint = ? AND rule_type = ?
        ORDER BY observed_at_ms DESC, id DESC
        LIMIT 1
        """,
        (mint, rule_type),
    ).fetchone()


def _has_history(connection: sqlite3.Connection, mint: str, rule_type: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM rule_history_events WHERE mint = ? AND rule_type = ? LIMIT 1",
        (mint, rule_type),
    ).fetchone() is not None


def _insert_event(
    connection: sqlite3.Connection,
    *,
    mint: str,
    rule_type: str,
    kind: str,
    observed_at_ms: int,
    source_at_ms: Optional[int],
    value: Optional[float],
    target: float,
    status: str,
    operator: str,
    unit: str,
    scenario_key: str,
    scenario_label: str,
    reason: str = "",
) -> None:
    connection.execute(
        """
        INSERT INTO rule_history_events (
            mint, rule_type, kind, observed_at_ms, source_at_ms, value, target,
            status, operator, unit, scenario_key, scenario_label, reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mint,
            rule_type,
            kind,
            int(observed_at_ms),
            int(source_at_ms) if source_at_ms is not None else None,
            value,
            target,
            status,
            operator,
            unit,
            scenario_key,
            scenario_label,
            str(reason or "")[:180],
        ),
    )


def _record_gap(
    connection: sqlite3.Connection,
    *,
    mint: str,
    rule_type: str,
    observed_at_ms: int,
    target: float,
    operator: str,
    unit: str,
    scenario_key: str,
    scenario_label: str,
    reason: str,
) -> bool:
    last = _last_event(connection, mint, rule_type)
    if last is not None and last["kind"] == "gap":
        return False
    _insert_event(
        connection,
        mint=mint,
        rule_type=rule_type,
        kind="gap",
        observed_at_ms=observed_at_ms,
        source_at_ms=None,
        value=None,
        target=target,
        status="unknown",
        operator=operator,
        unit=unit,
        scenario_key=scenario_key,
        scenario_label=scenario_label,
        reason=reason,
    )
    return True


def record_rule_history(
    mint: str,
    config: Any,
    runtime: Any,
    *,
    path: Optional[str] = None,
    max_gap_seconds: int = 360,
) -> int:
    """Store one evaluated snapshot without affecting readiness or alert state."""
    clean_mint = str(mint or "").strip()
    normalized = normalize_rules_config(config)
    state = runtime if isinstance(runtime, dict) else {}
    observed_at_ms = _parse_timestamp_ms(state.get("evaluated_at"))
    if not clean_mint or observed_at_ms is None or not normalized["enabled"]:
        return 0

    results = {
        str(item.get("type") or ""): item
        for item in state.get("items") or []
        if isinstance(item, dict)
    }
    database_path = rule_history_path(path)
    _ensure_schema(database_path)
    inserted = 0
    maximum_gap_ms = max(60, int(max_gap_seconds)) * 1000

    with _connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for configured_item in normalized["items"]:
            rule_type = configured_item["type"]
            definition = RULE_DEFINITIONS[rule_type]
            target = _finite_number(configured_item.get("target"))
            if target is None:
                continue
            scenario = _scenario(normalized, state, rule_type)
            last_before = _last_event(connection, clean_mint, rule_type)

            if not configured_item["enabled"]:
                if _has_history(connection, clean_mint, rule_type):
                    inserted += int(_record_gap(
                        connection,
                        mint=clean_mint,
                        rule_type=rule_type,
                        observed_at_ms=observed_at_ms,
                        target=target,
                        operator=definition["operator"],
                        unit=definition["unit"],
                        scenario_key=scenario["key"],
                        scenario_label=scenario["label"],
                        reason="Rule collection was disabled",
                    ))
                continue

            result = results.get(rule_type, {})
            current = _finite_number(result.get("current"))
            status = str(result.get("status") or "")
            source_at_ms = _parse_timestamp_ms(
                (state.get("price_impact") or {}).get("checked_at")
                if rule_type == "max_price_impact" and isinstance(state.get("price_impact"), dict)
                else state.get("source_updated_at")
            )

            target_changed = bool(
                last_before is not None
                and (
                    _finite_number(last_before["target"]) != target
                    or last_before["operator"] != definition["operator"]
                    or last_before["unit"] != definition["unit"]
                    or last_before["scenario_key"] != scenario["key"]
                )
            )
            if target_changed:
                _insert_event(
                    connection,
                    mint=clean_mint,
                    rule_type=rule_type,
                    kind="target",
                    observed_at_ms=observed_at_ms,
                    source_at_ms=None,
                    value=None,
                    target=target,
                    status="",
                    operator=definition["operator"],
                    unit=definition["unit"],
                    scenario_key=scenario["key"],
                    scenario_label=scenario["label"],
                    reason="Rule target or price-impact scenario changed",
                )
                inserted += 1

            if current is None or status not in {"pass", "fail"} or source_at_ms is None:
                inserted += int(_record_gap(
                    connection,
                    mint=clean_mint,
                    rule_type=rule_type,
                    observed_at_ms=observed_at_ms,
                    target=target,
                    operator=definition["operator"],
                    unit=definition["unit"],
                    scenario_key=scenario["key"],
                    scenario_label=scenario["label"],
                    reason=str(result.get("reason") or state.get("fetch_error") or "Rule data was unavailable"),
                ))
                continue

            if (
                last_before is not None
                and last_before["kind"] != "gap"
                and observed_at_ms - int(last_before["observed_at_ms"]) > maximum_gap_ms
            ):
                _insert_event(
                    connection,
                    mint=clean_mint,
                    rule_type=rule_type,
                    kind="gap",
                    observed_at_ms=max(int(last_before["observed_at_ms"]) + 1, observed_at_ms - 1),
                    source_at_ms=None,
                    value=None,
                    target=target,
                    status="unknown",
                    operator=definition["operator"],
                    unit=definition["unit"],
                    scenario_key=scenario["key"],
                    scenario_label=scenario["label"],
                    reason="Rule collection paused",
                )
                inserted += 1

            latest = _last_event(connection, clean_mint, rule_type)
            if (
                latest is not None
                and latest["kind"] == "point"
                and latest["source_at_ms"] == source_at_ms
                and latest["scenario_key"] == scenario["key"]
                and _finite_number(latest["target"]) == target
            ):
                continue

            _insert_event(
                connection,
                mint=clean_mint,
                rule_type=rule_type,
                kind="point",
                observed_at_ms=observed_at_ms,
                source_at_ms=source_at_ms,
                value=current,
                target=target,
                status=status,
                operator=definition["operator"],
                unit=definition["unit"],
                scenario_key=scenario["key"],
                scenario_label=scenario["label"],
            )
            inserted += 1

        connection.commit()

    _maybe_maintain(database_path)
    return inserted


def _maybe_maintain(path: str) -> None:
    now = time.monotonic()
    with _MAINTENANCE_LOCK:
        if now - _LAST_MAINTENANCE.get(path, 0.0) < 3600:
            return
        _LAST_MAINTENANCE[path] = now
    maintain_rule_history(path=path)


def _compact_points(connection: sqlite3.Connection, newest_ms: int, oldest_ms: int, bucket_ms: int) -> None:
    connection.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                MIN(id) OVER bucket AS first_id,
                MAX(id) OVER bucket AS last_id,
                FIRST_VALUE(id) OVER (
                    PARTITION BY mint, rule_type, scenario_key, observed_at_ms / ?
                    ORDER BY value ASC, id ASC
                ) AS min_value_id,
                FIRST_VALUE(id) OVER (
                    PARTITION BY mint, rule_type, scenario_key, observed_at_ms / ?
                    ORDER BY value DESC, id ASC
                ) AS max_value_id
            FROM rule_history_events
            WHERE kind = 'point' AND observed_at_ms < ? AND observed_at_ms >= ?
            WINDOW bucket AS (
                PARTITION BY mint, rule_type, scenario_key, observed_at_ms / ?
            )
        ),
        doomed AS (
            SELECT id
            FROM ranked
            WHERE id NOT IN (first_id, last_id, min_value_id, max_value_id)
        )
        DELETE FROM rule_history_events
        WHERE id IN (SELECT id FROM doomed)
        """,
        (bucket_ms, bucket_ms, newest_ms, oldest_ms, bucket_ms),
    )


def maintain_rule_history(*, path: Optional[str] = None, now: Optional[datetime] = None) -> None:
    database_path = rule_history_path(path)
    _ensure_schema(database_path)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    retention_cutoff = int((current - timedelta(days=history_retention_days())).timestamp() * 1000)
    seven_day_cutoff = int((current - timedelta(days=7)).timestamp() * 1000)
    thirty_day_cutoff = int((current - timedelta(days=30)).timestamp() * 1000)

    with _connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM rule_history_events WHERE observed_at_ms < ?",
            (retention_cutoff,),
        )
        _compact_points(connection, seven_day_cutoff, thirty_day_cutoff, 15 * 60 * 1000)
        _compact_points(connection, thirty_day_cutoff, retention_cutoff, 60 * 60 * 1000)
        connection.commit()
        connection.execute("PRAGMA optimize")


def _spread_indices(length: int, count: int) -> List[int]:
    if count <= 0 or length <= 0:
        return []
    if count >= length:
        return list(range(length))
    if count == 1:
        return [length - 1]
    return sorted({round(index * (length - 1) / (count - 1)) for index in range(count)})


def _sample_points_with_extremes(rows: List[sqlite3.Row], budget: int) -> List[sqlite3.Row]:
    if len(rows) <= budget:
        return rows
    if budget <= 2:
        return [rows[index] for index in _spread_indices(len(rows), budget)]

    bucket_count = max(1, budget // 4)
    first_time = int(rows[0]["observed_at_ms"])
    last_time = int(rows[-1]["observed_at_ms"])
    span = max(1, last_time - first_time + 1)
    buckets: List[List[sqlite3.Row]] = [[] for _ in range(bucket_count)]
    for row in rows:
        index = min(bucket_count - 1, int((int(row["observed_at_ms"]) - first_time) * bucket_count / span))
        buckets[index].append(row)

    chosen: Dict[int, sqlite3.Row] = {}
    for bucket in buckets:
        if not bucket:
            continue
        candidates = (
            bucket[0],
            bucket[-1],
            min(bucket, key=lambda row: (_finite_number(row["value"]) or 0.0, int(row["id"]))),
            max(bucket, key=lambda row: (_finite_number(row["value"]) or 0.0, -int(row["id"]))),
        )
        for row in candidates:
            chosen[int(row["id"])] = row

    if len(chosen) < budget:
        remaining = [row for row in rows if int(row["id"]) not in chosen]
        for index in _spread_indices(len(remaining), budget - len(chosen)):
            chosen[int(remaining[index]["id"])] = remaining[index]
    sampled = sorted(chosen.values(), key=lambda row: (int(row["observed_at_ms"]), int(row["id"])))
    return sampled[:budget]


def _downsample_rows(rows: Iterable[sqlite3.Row], limit: int) -> List[sqlite3.Row]:
    ordered = list(rows)
    if len(ordered) <= limit:
        return ordered

    mandatory = [row for row in ordered if row["kind"] != "point"]
    point_rows = [row for row in ordered if row["kind"] == "point"]
    if len(mandatory) >= limit - 2:
        kept_mandatory = [mandatory[index] for index in _spread_indices(len(mandatory), max(1, limit - 2))]
        candidates = [ordered[0], *kept_mandatory, ordered[-1]]
    else:
        point_budget = max(2, limit - len(mandatory))
        kept_points = _sample_points_with_extremes(point_rows, point_budget)
        candidates = [*mandatory, *kept_points]

    unique = {int(row["id"]): row for row in candidates}
    return sorted(unique.values(), key=lambda row: (int(row["observed_at_ms"]), int(row["id"])))[:limit]


def get_rule_history(
    mint: str,
    rule_type: str,
    window: str,
    *,
    path: Optional[str] = None,
    max_points: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    clean_mint = str(mint or "").strip()
    clean_rule_type = str(rule_type or "").strip()
    clean_window = str(window or "7d").strip().lower()
    if not clean_mint:
        raise ValueError("Token mint is required")
    if clean_rule_type not in RULE_DEFINITIONS:
        raise ValueError("Unsupported action rule")
    if clean_window not in HISTORY_WINDOWS:
        raise ValueError("History window must be 24h, 7d, 30d, or 90d")

    database_path = rule_history_path(path)
    _ensure_schema(database_path)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff_ms = int((current - timedelta(hours=HISTORY_WINDOWS[clean_window])).timestamp() * 1000)
    response_limit = _bounded_int(
        max_points,
        history_max_response_points(),
        120,
        1000,
    )

    with _connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM rule_history_events
            WHERE mint = ? AND rule_type = ? AND observed_at_ms >= ?
            ORDER BY observed_at_ms ASC, id ASC
            """,
            (clean_mint, clean_rule_type, cutoff_ms),
        ).fetchall()
        latest = connection.execute(
            """
            SELECT *
            FROM rule_history_events
            WHERE mint = ? AND rule_type = ? AND kind = 'point'
            ORDER BY observed_at_ms DESC, id DESC
            LIMIT 1
            """,
            (clean_mint, clean_rule_type),
        ).fetchone()

    sampled_rows = _downsample_rows(rows, response_limit)

    def serialize(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "kind": row["kind"],
            "timestamp": _iso_from_ms(row["observed_at_ms"]),
            "source_timestamp": _iso_from_ms(row["source_at_ms"]),
            "value": row["value"],
            "target": row["target"],
            "status": row["status"] or None,
            "operator": row["operator"],
            "unit": row["unit"],
            "scenario_key": row["scenario_key"],
            "scenario_label": row["scenario_label"],
            "reason": row["reason"] or None,
        }

    return {
        "mint": clean_mint,
        "rule_type": clean_rule_type,
        "window": clean_window,
        "retention_days": history_retention_days(),
        "points": [serialize(row) for row in sampled_rows],
        "latest_valid": serialize(latest) if latest is not None else None,
        "total_events": len(rows),
        "sampled": len(sampled_rows) < len(rows),
        "max_points": response_limit,
    }


def delete_rule_history(mint: str, *, path: Optional[str] = None) -> int:
    clean_mint = str(mint or "").strip()
    if not clean_mint:
        return 0
    database_path = rule_history_path(path)
    if not os.path.exists(database_path):
        return 0
    _ensure_schema(database_path)
    with _connect(database_path) as connection:
        cursor = connection.execute(
            "DELETE FROM rule_history_events WHERE mint = ?",
            (clean_mint,),
        )
        connection.commit()
        return max(0, int(cursor.rowcount or 0))


def mark_rule_history_gaps(
    mint: str,
    config: Any,
    *,
    rule_types: Optional[Iterable[str]] = None,
    reason: str = "Rule collection was disabled",
    observed_at: Optional[str] = None,
    path: Optional[str] = None,
) -> int:
    """Close existing series immediately when settings stop collection."""
    clean_mint = str(mint or "").strip()
    database_path = rule_history_path(path)
    if not clean_mint or not os.path.exists(database_path):
        return 0

    normalized = normalize_rules_config(config)
    configured = {item["type"]: item for item in normalized["items"]}
    selected = set(rule_types) if rule_types is not None else set(configured)
    selected.intersection_update(RULE_DEFINITIONS)
    timestamp_ms = _parse_timestamp_ms(observed_at or datetime.now(timezone.utc).isoformat())
    if not selected or timestamp_ms is None:
        return 0

    _ensure_schema(database_path)
    inserted = 0
    with _connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for rule_type in sorted(selected):
            last = _last_event(connection, clean_mint, rule_type)
            if last is None or last["kind"] == "gap":
                continue
            definition = RULE_DEFINITIONS[rule_type]
            configured_target = _finite_number((configured.get(rule_type) or {}).get("target"))
            target = configured_target
            if target is None:
                target = _finite_number(last["target"])
            if target is None:
                continue
            _insert_event(
                connection,
                mint=clean_mint,
                rule_type=rule_type,
                kind="gap",
                observed_at_ms=timestamp_ms,
                source_at_ms=None,
                value=None,
                target=target,
                status="unknown",
                operator=definition["operator"],
                unit=definition["unit"],
                scenario_key=str(last["scenario_key"] or ""),
                scenario_label=str(last["scenario_label"] or ""),
                reason=reason,
            )
            inserted += 1
        connection.commit()
    return inserted


