"""
In-memory stand-in for the supabase-py query builder: only the calls the agent
uses. Like PostgREST, a response never holds more than max_rows rows, whatever
range was requested.
"""

import copy
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace


class FakeSupabase:
    # Unique constraints of sql/005, enforced like Postgres (error code 23505).
    UNIQUE = {
        "merchant_performance_periods": ("client_key", "merchant_id", "period_start"),
        "merchant_product_performance": ("fetch_id", "offer_id", "marketing_method"),
    }

    def __init__(self, max_rows: int = 1000):
        self.tables: dict[str, list[dict]] = {}
        self.max_rows = max_rows
        self.writes: list[tuple[str, str]] = []
        self.rpc_results: dict[str, object] = {}
        self.fail_inserts: dict[str, int] = {}   # table -> fail on the n-th insert call (1-based)
        self._insert_calls: dict[str, int] = {}

    def rpc(self, name: str, params: dict):
        result = self.rpc_results.get(name)
        value = result(params) if callable(result) else result
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=value, count=None))

    def table(self, name: str) -> "_Query":
        return _Query(self, name)

    def rows(self, name: str) -> list[dict]:
        return self.tables.setdefault(name, [])

    def seed(self, name: str, rows: list[dict]) -> None:
        for r in rows:
            r = dict(r)
            r.setdefault("id", str(uuid.uuid4()))
            r.setdefault("created_at", datetime.now(UTC).isoformat())
            self.rows(name).append(r)


class _Query:
    def __init__(self, fake: FakeSupabase, name: str):
        self.fake = fake
        self.name = name
        self.filters = []
        self.orders = []
        self.offset = 0
        self.limit_n = None
        self.count_mode = None
        self.op = "select"
        self.payload = None
        self.on_conflict = None

    # read
    def select(self, columns="*", count=None):
        self.count_mode = count
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def neq(self, col, val):
        self.filters.append(lambda r: r.get(col) != val)
        return self

    def gte(self, col, val):
        self.filters.append(lambda r: r.get(col) is not None and r.get(col) >= val)
        return self

    def in_(self, col, values):
        values = list(values)
        self.filters.append(lambda r: r.get(col) in values)
        return self

    def gt(self, col, val):
        self.filters.append(lambda r: r.get(col) is not None and r.get(col) > val)
        return self

    def lt(self, col, val):
        self.filters.append(lambda r: r.get(col) is not None and r.get(col) < val)
        return self

    def order(self, col, desc=False):
        self.orders.append((col, desc))
        return self

    def range(self, start, end):
        self.offset = start
        self.limit_n = end - start + 1
        return self

    def limit(self, n):
        self.limit_n = n
        return self

    # write
    def insert(self, rows):
        self.op, self.payload = "insert", rows
        return self

    def delete(self):
        self.op = "delete"
        return self

    def update(self, values):
        self.op, self.payload = "update", values
        return self

    def upsert(self, row, on_conflict=None):
        self.op, self.payload, self.on_conflict = "upsert", row, on_conflict
        return self

    def execute(self):
        rows = self.fake.rows(self.name)
        if self.op == "insert":
            self.fake.writes.append((self.name, "insert"))
            n = self.fake._insert_calls[self.name] = self.fake._insert_calls.get(self.name, 0) + 1
            if self.fake.fail_inserts.get(self.name) == n:
                raise RuntimeError(f"simulated write failure on {self.name}")
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            keys = self.fake.UNIQUE.get(self.name)
            out = []
            for item in items:
                item = dict(item)
                item.setdefault("id", str(uuid.uuid4()))
                item.setdefault("created_at", datetime.now(UTC).isoformat())
                if any(r["id"] == item["id"] for r in rows) or (
                        keys and any(all(r.get(k) == item.get(k) for k in keys) for r in rows)):
                    raise RuntimeError(f"23505 duplicate key value violates unique constraint on {self.name}")
                rows.append(item)
                out.append(item)
            return SimpleNamespace(data=out, count=None)
        matched = [r for r in rows if all(f(r) for f in self.filters)]
        if self.op == "delete":
            self.fake.writes.append((self.name, "delete"))
            self.fake.tables[self.name] = [r for r in rows if r not in matched]
            return SimpleNamespace(data=matched, count=None)
        if self.op == "update":
            self.fake.writes.append((self.name, "update"))
            for r in matched:
                r.update(self.payload)
            return SimpleNamespace(data=matched, count=None)
        if self.op == "upsert":
            self.fake.writes.append((self.name, "upsert"))
            keys = (self.on_conflict or "id").split(",")
            for r in rows:
                if all(r.get(k) == self.payload.get(k) for k in keys):
                    r.update(self.payload)
                    return SimpleNamespace(data=[r], count=None)
            rows.append(dict(self.payload, id=self.payload.get("id", str(uuid.uuid4()))))
            return SimpleNamespace(data=[rows[-1]], count=None)

        for col, desc in reversed(self.orders):
            matched.sort(key=lambda r: (r.get(col) is None, r.get(col)), reverse=desc)
        total = len(matched)
        n = min(self.limit_n or self.fake.max_rows, self.fake.max_rows)
        page = matched[self.offset:self.offset + n]
        return SimpleNamespace(data=copy.deepcopy(page), count=total if self.count_mode else None)
