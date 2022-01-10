"""PostgreSQL storage solution."""

from __future__ import annotations
from typing import (
    Any,
    AsyncGenerator,
    Iterable,
    List,
    Optional,
    Tuple,
)

import json
import aiopg  # type: ignore
import logging
import asyncio
import datetime as dt
from contextlib import asynccontextmanager

from money import predicate as P
from money.event import EventBase, EventMeta
from money.predicate import Predicate
from money.aggregate import AggregateBase, AggregateMeta
from money.storage.utils import (
    PredicateSQLSimplifier,
    SimplifiedPredicate,
    cast_simplified_predicate,
    visit_field_predicate,
    visit_predicate,
)

LOG = logging.getLogger("postgresql")


class _pgjson(json.JSONEncoder):
    """Provides JSON encoding for the database layer."""

    def default(self, o):

        if isinstance(o, dt.datetime):
            return o.astimezone(dt.timezone.utc).isoformat()
        return super().default(o)

    @classmethod
    def dumps(cls, val: Any) -> str:
        return json.dumps(val, cls=cls)


class _PostgreSQLSimplifier(PredicateSQLSimplifier):
    def __init__(self, type_field: str, data_field: str, timestamp_convert: str = None):
        self.type_field = type_field
        self.data_field = data_field
        self.timestamp_convert = (
            timestamp_convert
            if timestamp_convert is not None
            else r"""to_timestamp({}, 'YYYY-MM-DD"T"HH24:MI:SSTZH:TZM')"""
        )

    def _ph(self) -> str:
        return "%s"

    def on_is(self, p_is: P.Is) -> SimplifiedPredicate:
        clause = f"{self.type_field} IN ({', '.join(self._ph() for _ in p_is.types)})"
        params = [t.__name__ if isinstance(t, type) else str(t) for t in p_is.types]
        return None, clause, params

    def on_where(self, p_where: P.Where) -> SimplifiedPredicate:
        exprs: List[str] = []
        params: List[Any] = []
        for name, fpred in p_where.fields.items():
            pred, clause, fparams = visit_field_predicate(self, name, fpred)
            assert pred is None and clause is not None and fparams is not None
            exprs.append(clause)
            params.extend(fparams)
        return None, f"({' AND '.join(exprs)})", params

    def _smart_query(
        self, field: str, oper: str, val: Any
    ) -> Tuple[str, Iterable[Any]]:
        if isinstance(val, (str, int, float, bool)):
            return (
                f"{self.data_field}->{self._ph()} {oper} {self._ph()}::jsonb",
                (field, _pgjson.dumps(val)),
            )
        if isinstance(val, dt.datetime):
            field_as_timestamp_tz = self.timestamp_convert.format(
                f"{self.data_field}->>{self._ph()}"
            )
            return (
                f"{field_as_timestamp_tz} {oper} {self._ph()}::timestamp",
                (field, val.isoformat()),
            )
        raise RuntimeError(f"unsupported value type {type(val).__name__}")

    def on_eq(self, field: str, p_eq: P.Eq) -> SimplifiedPredicate:
        query, params = self._smart_query(field, "=", p_eq.expect)
        return (None, query, params)

    def on_not_eq(self, field: str, p_neq: P.NotEq) -> SimplifiedPredicate:
        query, params = self._smart_query(field, "<>", p_neq.expect)
        return (None, query, params)

    def on_less(self, field: str, p_less: P.Less) -> SimplifiedPredicate:
        query, params = self._smart_query(field, "<", p_less.limit)
        return (None, query, params)

    def on_more(self, field: str, p_more: P.More) -> SimplifiedPredicate:
        query, params = self._smart_query(field, ">", p_more.limit)
        return (None, query, params)

    def on_less_eq(self, field: str, p_less_eq: P.LessEq) -> SimplifiedPredicate:
        query, params = self._smart_query(field, "<=", p_less_eq.limit)
        return (None, query, params)

    def on_more_eq(self, field: str, p_more_eq: P.MoreEq) -> SimplifiedPredicate:
        query, params = self._smart_query(field, ">=", p_more_eq.limit)
        return (None, query, params)

    def on_between(self, field: str, p_between: P.Between) -> SimplifiedPredicate:
        low_query, low_params = self._smart_query(field, ">=", p_between.lower)
        hi_query, hi_params = self._smart_query(field, "<=", p_between.upper)
        return (
            None,
            f"({low_query} AND {hi_query})",
            tuple(list(low_params) + list(hi_params)),
        )

    def on_one_of(self, field: str, p_one_of: P.OneOf) -> SimplifiedPredicate:
        params: List[Any] = []
        exprs: List[str] = []
        for opt in p_one_of.options:
            oquery, oparams = self._smart_query(field, "=", opt)
            exprs.append(oquery)
            params.extend(oparams)
        return (None, f"({' OR '.join(exprs)})", tuple(params))


class PostgreSQLStorage:
    """Provides a storage interface for a postgresql database."""

    def __init__(
        self, *, host: str, port: str, user: str, password: str, database: str, **pgopts
    ):
        """Initialize new PostgreSQL storage using the given connection."""
        self.__setup = asyncio.Lock()
        self.__dsn = (
            f"postgres://{user}:{password}@{host}:{port}/{database}"
            f"?{'&'.join(f'{name}={value}' for name, value in pgopts.items())}"
        )
        self.__pool: Optional[aiopg.Pool] = None
        self.__timestamp_convert: Optional[str] = None

    async def __get_pool(self) -> aiopg.Pool:
        async with self.__setup:
            if self.__pool is not None:
                return self.__pool
            pool: aiopg.Pool = await aiopg.create_pool(self.__dsn)
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("BEGIN;")
                    try:
                        LOG.info("creating tables")
                        await cur.execute(
                            """
                            CREATE TABLE IF NOT EXISTS __events (
                                sequence_num BIGSERIAL NOT NULL PRIMARY KEY,
                                event_type   VARCHAR(128) NOT NULL,
                                event_data   JSONB NOT NULL DEFAULT '{}'
                            );
                            """
                        )
                        await cur.execute(
                            """
                            CREATE TABLE IF NOT EXISTS __snapshots (
                                sequence_num   BIGSERIAL NOT NULL PRIMARY KEY,
                                aggregate_id   VARCHAR(64) NOT NULL UNIQUE,
                                aggregate_type VARCHAR(128) NOT NULL,
                                aggregate_data JSONB NOT NULL DEFAULT '{}'
                            );
                            """
                        )
                        try:
                            await cur.execute(
                                "CREATE EXTENSION IF NOT EXISTS plpython3u;"
                            )
                        except Exception as err:  # pylint: disable=broad-except
                            LOG.error(
                                "plpython3u is unavailable: %s", err, exc_info=err
                            )
                        else:
                            await cur.execute(
                                """
                                CREATE OR REPLACE FUNCTION fromisoformat(raw text)
                                    RETURNS timestamp with time zone
                                AS $$
                                    from datetime import datetime
                                    return datetime.fromisoformat(raw)
                                $$ LANGUAGE plpython3u;
                                """
                            )
                            self.__timestamp_convert = r"fromisoformat({})"
                            LOG.info("creating custom functions")
                        await cur.execute("COMMIT;")
                    except Exception:
                        await cur.execute("ROLLBACK;")
                        raise
            self.__pool = pool
            return pool

    @asynccontextmanager
    async def get_cursor(self) -> AsyncGenerator[aiopg.Cursor, None]:
        """Get a transaction cursor for the underlying connection."""
        pool = await self.__get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("BEGIN;")
                try:
                    yield cur
                    await cur.execute("COMMIT;")
                except Exception:
                    await cur.execute("ROLLBACK;")
                    raise

    def __simplify(self, pred: Predicate, type_field: str, data_field: str):
        return cast_simplified_predicate(
            visit_predicate(
                _PostgreSQLSimplifier(
                    type_field, data_field, timestamp_convert=self.__timestamp_convert
                ),
                pred,
            )
        )

    async def load_events(self, query: Predicate = None) -> Iterable[EventBase]:
        """Load events that match the predicate."""
        async with self.get_cursor() as conn:
            events: List[EventBase] = []
            sql_str = "SELECT event_type, event_data from __events"
            params = None
            if query:
                query, where_clause, params = self.__simplify(
                    query, "event_type", "event_data"
                )
                if where_clause is not None:
                    sql_str += f" WHERE {where_clause}"
            sql_str += " ORDER BY sequence_num ASC"
            LOG.info("SQL QUERY: %s", sql_str)
            await conn.execute(sql_str, params)
            async for row in conn:
                evt = EventMeta.construct_named(row[0], row[1])
                if query is None or query(evt):
                    events.append(evt)
            return events

    async def save_events(self, events: Iterable[EventBase]):
        """Save new events."""
        async with self.get_cursor() as conn:
            sql_str = "INSERT INTO __events (event_type, event_data) values (%s, %s);"
            for evt in events:
                params = (
                    evt.__class__.__name__,
                    _pgjson.dumps(evt.to_dict()),
                )
                await conn.execute(sql_str, params)
            LOG.info("SQL EXEC: %s", sql_str)

    async def save_snapshots(self, snaps: Iterable[AggregateBase]):
        """Save new snapshots."""
        async with self.get_cursor() as conn:
            for snap in snaps:
                snap_typ = type(snap)
                snap_id = f"{snap_typ.__name__}:{getattr(snap, snap_typ.__agg_id__)}"
                snap_data = _pgjson.dumps(snap.to_dict())
                sql_str = """
                    INSERT INTO __snapshots (aggregate_id, aggregate_type, aggregate_data)
                        VALUES (%s, %s, %s)
                        ON CONFLICT(aggregate_id) DO UPDATE SET
                            aggregate_data=excluded.aggregate_data;
                    """
                params = (snap_id, snap_typ.__name__, snap_data)
                LOG.info("SQL EXEC: %s", sql_str)
                await conn.execute(sql_str, params)

    async def load_snapshots(self, query: Predicate = None) -> Iterable[AggregateBase]:
        """Load snapshots that match the predicate."""
        async with self.get_cursor() as conn:
            snaps: List[AggregateBase] = []
            sql_str = "SELECT aggregate_type, aggregate_data from __snapshots"
            params = None
            if query:
                query, where_clause, params = self.__simplify(
                    query, "aggregate_type", "aggregate_data"
                )
                if where_clause is not None:
                    sql_str += f" WHERE {where_clause}"
            sql_str += " ORDER BY sequence_num ASC"
            LOG.info("SQL QUERY: %s", sql_str)
            await conn.execute(sql_str, params)
            async for row in conn:
                snap = AggregateMeta.construct_named(row[0], row[1])
                if query is None or query(snap):
                    snaps.append(snap)
            return snaps

    async def close(self):
        """Close underlying connection(s)."""
        async with self.__setup:
            if self.__pool is not None:
                self.__pool.close()
                self.__pool = None
