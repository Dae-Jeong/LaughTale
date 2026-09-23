"""Bounded chunk evidence; no message bodies, cookies, or access credentials."""

import hashlib
import math
import sqlite3
from uuid import UUID, uuid5


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else None


def resource_failure(sample):
    required = {
        "host_free_percent",
        "internal_free_gib",
        "external_free_gib",
        "guest_free_gib",
        "healthy",
    }
    if not required <= sample.keys():
        return "missing_resource_evidence"
    if not sample["healthy"]:
        return "restarted_or_unhealthy_target"
    for key, minimum in (
        ("host_free_percent", 15),
        ("internal_free_gib", 15),
        ("external_free_gib", 200),
        ("guest_free_gib", 100),
    ):
        if (
            not isinstance(sample[key], (int, float))
            or not math.isfinite(sample[key])
            or sample[key] < minimum
        ):
            return key
    return None


class Ledger:
    def __init__(self, path, run_id, count):
        if path.exists() or not 1 <= count <= 100_000:
            raise ValueError("Require a fresh bounded chunk ledger")
        # Runtime calls are serialized through one dedicated executor thread.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE attempts (
              idx INTEGER PRIMARY KEY, cid TEXT UNIQUE NOT NULL, state TEXT NOT NULL,
              code INTEGER, mid TEXT, seq INTEGER, sent REAL, ack REAL, dispatch_delay REAL);
            CREATE TABLE observed (
              channel TEXT, cid TEXT, mid TEXT, seq INTEGER, hash TEXT, stamp REAL,
              n INTEGER NOT NULL DEFAULT 1, conflict INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(channel,cid));
        """)
        self.db.executemany(
            "INSERT INTO attempts(idx,cid,state) VALUES(?,?,'planned')",
            ((i, str(uuid5(UUID(run_id), str(i)))) for i in range(count)),
        )
        self.db.commit()

    def observe(self, channel, message, stamp):
        cid = message["client_message_id"]
        if not self.db.execute("SELECT 1 FROM attempts WHERE cid=?", (cid,)).fetchone():
            return False
        values = (
            channel,
            cid,
            message["message_id"],
            int(message["seq"]),
            digest(message["text"]),
            stamp,
        )
        self.db.execute(
            """INSERT INTO observed(channel,cid,mid,seq,hash,stamp) VALUES(?,?,?,?,?,?)
            ON CONFLICT(channel,cid) DO UPDATE SET n=n+1,
              conflict=conflict OR mid!=excluded.mid OR seq!=excluded.seq OR hash!=excluded.hash""",
            values,
        )
        return True

    def query(self, statement, parameters=()):
        return self.db.execute(statement, parameters).fetchall()

    def report(self, expected_hash):
        states = dict(
            self.db.execute("SELECT state,count(*) FROM attempts GROUP BY state")
        )
        checks = {}
        for channel in ("primary", "replica", "kafka", "ws"):
            missing = self.db.execute(
                """SELECT count(*) FROM attempts a LEFT JOIN observed o
                ON o.cid=a.cid AND o.channel=? WHERE a.state='ack' AND
                (o.cid IS NULL OR o.mid!=a.mid OR o.seq!=a.seq OR o.hash!=? OR o.conflict!=0)""",
                (channel, expected_hash),
            ).fetchone()[0]
            checks[channel] = missing == 0 and states.get("ack", 0) > 0
        checks["ws_no_duplicates"] = (
            self.db.execute(
                "SELECT count(*) FROM observed WHERE channel='ws' AND n!=1"
            ).fetchone()[0]
            == 0
        )
        checks["no_rejected_commits"] = (
            self.db.execute("""SELECT count(*) FROM attempts a
            JOIN observed o ON o.cid=a.cid AND o.channel='primary'
            WHERE a.state IN ('rejected','skipped','planned')""").fetchone()[0]
            == 0
        )
        latency = [
            row[0]
            for row in self.db.execute(
                "SELECT ack-sent FROM attempts WHERE state='ack'"
            )
        ]
        ws_latency = [
            row[0]
            for row in self.db.execute("""SELECT o.stamp-a.sent FROM attempts a
            JOIN observed o ON o.cid=a.cid AND o.channel='ws' WHERE a.state='ack'""")
        ]
        return {
            "states": states,
            "http_status_counts": {
                str(code) if code is not None else "no_response": count
                for code, count in self.db.execute(
                    "SELECT code,count(*) FROM attempts WHERE state IN ('ack','unknown','rejected') GROUP BY code"
                )
            },
            "checks": checks,
            "ack_p99_seconds": percentile(latency, 0.99),
            "ws_p99_seconds": percentile(ws_latency, 0.99),
            "dispatch_p99_seconds": percentile(
                [
                    r[0]
                    for r in self.db.execute(
                        "SELECT dispatch_delay FROM attempts WHERE dispatch_delay IS NOT NULL"
                    )
                ],
                0.99,
            ),
            "unknown_committed": self.db.execute("""SELECT count(*) FROM attempts a JOIN observed o
                    ON o.cid=a.cid AND o.channel='primary' WHERE a.state='unknown'""").fetchone()[
                0
            ],
            "kafka_raw_duplicates": self.db.execute(
                "SELECT coalesce(sum(n-1),0) FROM observed WHERE channel='kafka'"
            ).fetchone()[0],
        }

    def close(self):
        self.db.commit()
        self.db.close()
