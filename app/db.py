"""
Schemat bazy danych (SQLite jako lekki substytut Postgresa — patrz README,
sekcja "Dlaczego SQLite zamiast Postgresa").
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "catalog.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku                 TEXT PRIMARY KEY,      -- kanoniczny nr_katalogowy
    nazwa               TEXT NOT NULL,
    producent           TEXT NOT NULL,
    kategoria           TEXT,                  -- znormalizowana (5 kategorii kanonicznych)
    kategoria_zrodlowa  TEXT,                  -- oryginalny zapis z CSV (audyt)
    kategoria_uzupelniona INTEGER DEFAULT 0,    -- 1 = dogadana/wnioskowana, nie z danych źródłowych
    opak_ilosc          REAL,                  -- np. 50
    opak_jednostka      TEXT,                  -- znormalizowana jednostka: szt/g/kg/l/ml/op/rxn/nieznana
    opak_surowa         TEXT,                  -- oryginalny zapis (audyt)
    cena_pln            REAL,
    cena_brak           INTEGER DEFAULT 0,      -- 1 = brakująca cena w źródle
    atrybuty_dodatkowe  TEXT,
    opis                TEXT,                   -- z harvestera (producent_novagen_snapshot.html)
    specyfikacja        TEXT,                   -- JSON list, z harvestera
    zrodlo_opisu        TEXT,                   -- 'harvester:NO-xxxx' albo NULL
    duplikat_uzasadnienie TEXT,                 -- jeśli scalono z innych wierszy CSV, notatka jak/dlaczego
    search_blob         TEXT                    -- tekst użyty do wyszukiwania semantycznego (denormalizowany)
);

CREATE VIRTUAL TABLE IF NOT EXISTS products_fts USING fts5(
    sku, nazwa, opis, atrybuty_dodatkowe, content='products', content_rowid='rowid'
);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(fresh: bool = True):
    if fresh and DB_PATH.exists():
        DB_PATH.unlink()
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
