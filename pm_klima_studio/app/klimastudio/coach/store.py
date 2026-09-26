"""SQLite-Ablage für Einstellungen, Rückmeldungen, KI-Läufe und Ereignisse.

Alle Zugriffe laufen über ``asyncio.to_thread`` und sind zusätzlich durch
einen Lock serialisiert (eine Verbindung, ``check_same_thread=False``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_KI_LAEUFE = 50
MAX_EREIGNISSE = 2000

SCHEMA = """
CREATE TABLE IF NOT EXISTS einstellungen (
    schluessel TEXT PRIMARY KEY,
    wert TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rueckmeldungen (
    tipp_id TEXT NOT NULL,
    aktion TEXT NOT NULL,
    bis REAL,
    zeit REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS rueckmeldungen_tipp ON rueckmeldungen (tipp_id);
CREATE TABLE IF NOT EXISTS ki_laeufe (
    id TEXT PRIMARY KEY,
    erstellt TEXT NOT NULL,
    entitaet TEXT NOT NULL,
    dauer_s REAL,
    lage_json TEXT,
    antwort_json TEXT,
    roh TEXT,
    fehler TEXT
);
CREATE TABLE IF NOT EXISTS ereignisse (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zeit TEXT NOT NULL,
    typ TEXT NOT NULL,
    raum TEXT,
    text TEXT NOT NULL,
    daten TEXT
);
CREATE INDEX IF NOT EXISTS ereignisse_typ ON ereignisse (typ);
"""


def jetzt_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _json_laden(text: str | None) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


class CoachStore:
    """Kleine SQLite-Datenbank (``coach.db``) der App."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------- Verbindung

    def _verbindung(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                _LOGGER.warning("coach.db hat neuere Schema-Version %s (erwartet %s)", version, SCHEMA_VERSION)
            conn.executescript(SCHEMA)
            if version < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
            self._conn = conn
        return self._conn

    def _ausfuehren(self, fn, *args: Any) -> Any:
        with self._lock:
            conn = self._verbindung()
            try:
                result = fn(conn, *args)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return result

    async def _run(self, fn, *args: Any) -> Any:
        return await asyncio.to_thread(self._ausfuehren, fn, *args)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def schema_version(self) -> int:
        return self._ausfuehren(lambda c: c.execute("PRAGMA user_version").fetchone()[0])

    # ------------------------------------------------------------- Einstellungen

    async def einstellung(self, schluessel: str) -> Any:
        def fn(c: sqlite3.Connection) -> Any:
            row = c.execute("SELECT wert FROM einstellungen WHERE schluessel = ?", (schluessel,)).fetchone()
            return _json_laden(row["wert"]) if row else None

        return await self._run(fn)

    async def einstellung_setzen(self, schluessel: str, wert: Any) -> None:
        text = json.dumps(wert, ensure_ascii=False)

        def fn(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT INTO einstellungen (schluessel, wert) VALUES (?, ?) "
                "ON CONFLICT(schluessel) DO UPDATE SET wert = excluded.wert",
                (schluessel, text),
            )

        await self._run(fn)

    # ------------------------------------------------------------- Rückmeldungen

    async def rueckmeldung(self, tipp_id: str, aktion: str, bis: float | None) -> None:
        now = time.time()

        def fn(c: sqlite3.Connection) -> None:
            c.execute("INSERT INTO rueckmeldungen (tipp_id, aktion, bis, zeit) VALUES (?, ?, ?, ?)", (tipp_id, aktion, bis, now))
            # Abgelaufene Einträge älter als 180 Tage entfernen
            c.execute("DELETE FROM rueckmeldungen WHERE zeit < ? AND (bis IS NULL OR bis < ?)", (now - 180 * 86400, now))

        await self._run(fn)

    async def ausgeblendet(self, now: float | None = None) -> set[str]:
        ts = time.time() if now is None else now

        def fn(c: sqlite3.Connection) -> set[str]:
            rows = c.execute("SELECT DISTINCT tipp_id FROM rueckmeldungen WHERE bis IS NOT NULL AND bis > ?", (ts,))
            return {r["tipp_id"] for r in rows}

        return await self._run(fn)

    # ------------------------------------------------------------- KI-Läufe

    async def ki_lauf_speichern(self, lauf: dict[str, Any], lage: Any) -> None:
        antwort = {"zusammenfassung": lauf.get("zusammenfassung"), "tipps": lauf.get("tipps") or []}
        row = (
            lauf["id"],
            lauf["erstellt"],
            lauf["entitaet"],
            lauf.get("dauer_s"),
            json.dumps(lage, ensure_ascii=False),
            json.dumps(antwort, ensure_ascii=False),
            lauf.get("roh"),
            lauf.get("fehler"),
        )

        def fn(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT OR REPLACE INTO ki_laeufe (id, erstellt, entitaet, dauer_s, lage_json, antwort_json, roh, fehler) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            c.execute(
                "DELETE FROM ki_laeufe WHERE id NOT IN (SELECT id FROM ki_laeufe ORDER BY erstellt DESC, rowid DESC LIMIT ?)",
                (MAX_KI_LAEUFE,),
            )

        await self._run(fn)

    @staticmethod
    def _lauf_aus_zeile(r: sqlite3.Row) -> dict[str, Any]:
        antwort = _json_laden(r["antwort_json"]) or {}
        return {
            "id": r["id"],
            "erstellt": r["erstellt"],
            "entitaet": r["entitaet"],
            "dauer_s": r["dauer_s"],
            "zusammenfassung": antwort.get("zusammenfassung"),
            "tipps": antwort.get("tipps") or [],
            "roh": r["roh"],
            "fehler": r["fehler"],
        }

    async def ki_laeufe(self, limit: int = 20) -> list[dict[str, Any]]:
        def fn(c: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = c.execute("SELECT * FROM ki_laeufe ORDER BY erstellt DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
            return [self._lauf_aus_zeile(r) for r in rows]

        return await self._run(fn)

    async def ki_lauf(self, lauf_id: str) -> dict[str, Any] | None:
        def fn(c: sqlite3.Connection) -> dict[str, Any] | None:
            row = c.execute("SELECT * FROM ki_laeufe WHERE id = ?", (lauf_id,)).fetchone()
            return self._lauf_aus_zeile(row) if row else None

        return await self._run(fn)

    # ------------------------------------------------------------- Ereignisse

    async def ereignis(self, typ: str, raum: str | None, text: str, daten: Any = None) -> None:
        row = (jetzt_iso(), typ, raum, text, json.dumps(daten, ensure_ascii=False) if daten is not None else None)

        def fn(c: sqlite3.Connection) -> None:
            c.execute("INSERT INTO ereignisse (zeit, typ, raum, text, daten) VALUES (?, ?, ?, ?, ?)", row)
            c.execute(
                "DELETE FROM ereignisse WHERE id NOT IN (SELECT id FROM ereignisse ORDER BY id DESC LIMIT ?)",
                (MAX_EREIGNISSE,),
            )

        await self._run(fn)

    async def ereignisse(self, typ: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        def fn(c: sqlite3.Connection) -> list[dict[str, Any]]:
            if typ:
                rows = c.execute("SELECT * FROM ereignisse WHERE typ = ? ORDER BY id DESC LIMIT ?", (typ, limit))
            else:
                rows = c.execute("SELECT * FROM ereignisse ORDER BY id DESC LIMIT ?", (limit,))
            return [
                {
                    "id": r["id"],
                    "zeit": r["zeit"],
                    "typ": r["typ"],
                    "raum": r["raum"],
                    "text": r["text"],
                    "daten": _json_laden(r["daten"]),
                }
                for r in rows
            ]

        return await self._run(fn)
