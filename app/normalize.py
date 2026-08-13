"""
Krok 1: normalizacja katalog_probka.csv do spójnego schematu + rozstrzygnięcie
duplikatów/near-duplikatów numerów katalogowych.

Uruchomienie:  python -m app.normalize
"""
import csv
import json
import re
from pathlib import Path

from app.clean import (
    normalize_category, normalize_package, normalize_price,
    normalize_name, normalize_producer,
)
from app.db import init_db

DATA_CSV = Path(__file__).resolve().parent.parent / "data" / "katalog_probka.csv"

# Wzorzec numeru katalogowego z ewentualnym "podejrzanym" sufiksem literowym
# lub "-1" na końcu — kandydat na literówkę/duplikat tego samego SKU.
_SUFFIX_RE = re.compile(r"^(?P<base>[A-Z]{2}-\d{5})(?:[A-Z]|-1)$")


def load_rows():
    with open(DATA_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def dedupe(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """
    Zwraca (lista_zdeduplikowanych_wierszy, log_decyzji).

    Strategia (opisana też w README):
    1. Wiersze identyczne na wszystkich polach -> zostaje jeden.
    2. Ten sam nr_katalogowy, różnice tylko w polach opcjonalnych (puste vs.
       wypełnione) -> scalenie: bierzemy wartości niepuste.
    3. nr_katalogowy różniący się jedynie sufiksem (litera na końcu albo
       "-1") ORAZ identyczna nazwa/producent/opakowanie/cena -> traktujemy
       jako literówkę/powtórny wpis tego samego produktu, zostaje wersja
       z "czystszym" (krótszym, bez sufiksu) numerem.
    Każda pozostała para o tej samej nazwie i producencie, ale różnym SKU
    i różnych pozostałych polach, NIE jest duplikatem — to odrębne pozycje
    katalogowe (np. inny wariant opakowania/ceny tego samego produktu u tego
    samego producenta), zostają obie.
    """
    log = []

    # --- krok 1 + 2: grupowanie po nr_katalogowy -----------------------------
    by_sku: dict[str, list[dict]] = {}
    for r in rows:
        by_sku.setdefault(r["nr_katalogowy"], []).append(r)

    merged: dict[str, dict] = {}
    for sku, group in by_sku.items():
        if len(group) == 1:
            merged[sku] = group[0]
            continue
        base = group[0]
        for other in group[1:]:
            diffs = {k for k in base if base[k] != other[k]}
            if not diffs:
                log.append(f"SKU {sku}: 2 identyczne wiersze -> scalono w 1 (exact duplicate).")
                continue
            # scal: preferuj wartość niepustą dla pól opcjonalnych
            for k in diffs:
                if not base.get(k) and other.get(k):
                    base[k] = other[k]
                    log.append(f"SKU {sku}: pole '{k}' uzupełnione z drugiego wiersza-duplikatu "
                                f"(scalono niepuste wartości).")
                elif base.get(k) and other.get(k) and base[k] != other[k]:
                    log.append(f"SKU {sku}: KONFLIKT w polu '{k}' ('{base[k]}' vs '{other[k]}') "
                                f"-> zachowano pierwszą napotkaną wartość ('{base[k]}').")
        merged[sku] = base

    # --- krok 3: sufiksowane near-duplikaty -----------------------------------
    skus = set(merged.keys())
    to_drop = set()
    for sku in list(skus):
        m = _SUFFIX_RE.match(sku)
        if not m:
            continue
        base_sku = m.group("base")
        if base_sku not in merged or base_sku == sku:
            continue
        a, b = merged[sku], merged[base_sku]
        same_core = (
            a["nazwa"] == b["nazwa"] and a["producent"] == b["producent"]
            and a["opakowanie"] == b["opakowanie"] and a["cena"] == b["cena"]
        )
        if same_core:
            to_drop.add(sku)
            log.append(f"SKU {sku}: wygląda na literówkę/powtórny wpis SKU {base_sku} "
                        f"(identyczna nazwa/producent/opakowanie/cena, różnica tylko w sufiksie "
                        f"numeru katalogowego) -> odrzucono, zachowano {base_sku}.")

    final_rows = [v for k, v in merged.items() if k not in to_drop]
    return final_rows, log


def transform(row: dict) -> dict:
    ilosc, jednostka, opak_raw = normalize_package(row.get("opakowanie"))
    cena = normalize_price(row.get("cena"))
    kat_raw = row.get("kategoria") or None
    kat_norm = normalize_category(kat_raw)
    return {
        "sku": row["nr_katalogowy"].strip(),
        "nazwa": normalize_name(row["nazwa"]),
        "producent": normalize_producer(row["producent"]),
        "kategoria": kat_norm,
        "kategoria_zrodlowa": kat_raw,
        "kategoria_uzupelniona": 0,
        "opak_ilosc": ilosc,
        "opak_jednostka": jednostka,
        "opak_surowa": opak_raw,
        "cena_pln": cena,
        "cena_brak": 1 if cena is None else 0,
        "atrybuty_dodatkowe": (row.get("atrybuty_dodatkowe") or None),
    }


def fill_missing_categories(products: list[dict], log: list[str]) -> None:
    """
    Wzbogacenie: dla wierszy bez kategorii, jeśli inny produkt o tej samej
    nazwie ma znaną (znormalizowaną) kategorię, przypisujemy ją i oznaczamy
    jako 'uzupełnioną' (kategoria_uzupelniona=1) — to inferencja, nie fakt
    ze źródła, więc audytowalność ma znaczenie (patrz README: założenia).
    """
    by_name: dict[str, str] = {}
    for p in products:
        if p["kategoria"]:
            by_name.setdefault(p["nazwa"], p["kategoria"])
    filled = 0
    for p in products:
        if not p["kategoria"] and p["nazwa"] in by_name:
            p["kategoria"] = by_name[p["nazwa"]]
            p["kategoria_uzupelniona"] = 1
            filled += 1
    if filled:
        log.append(f"Uzupełniono kategorię (na podstawie innych wierszy o tej samej nazwie) "
                    f"dla {filled} pozycji bez kategorii w źródle.")


def build_search_blob(p: dict) -> str:
    parts = [p["nazwa"], p["producent"], p.get("kategoria") or "", p.get("atrybuty_dodatkowe") or ""]
    return " | ".join(x for x in parts if x)


def main():
    rows = load_rows()
    print(f"Wczytano {len(rows)} wierszy z {DATA_CSV.name}")

    deduped, log = dedupe(rows)
    print(f"Po deduplikacji: {len(deduped)} pozycji (usunięto {len(rows) - len(deduped)})")

    products = [transform(r) for r in deduped]
    fill_missing_categories(products, log)

    for p in products:
        p["opis"] = None
        p["specyfikacja"] = None
        p["zrodlo_opisu"] = None
        p["duplikat_uzasadnienie"] = None
        p["search_blob"] = build_search_blob(p)

    conn = init_db(fresh=True)
    cols = list(products[0].keys())
    placeholders = ",".join("?" for _ in cols)
    conn.executemany(
        f"INSERT INTO products ({','.join(cols)}) VALUES ({placeholders})",
        [tuple(p[c] for c in cols) for p in products],
    )
    conn.execute(
        "INSERT INTO products_fts(rowid, sku, nazwa, opis, atrybuty_dodatkowe) "
        "SELECT rowid, sku, nazwa, opis, atrybuty_dodatkowe FROM products"
    )
    conn.commit()

    log_path = Path(__file__).resolve().parent.parent / "dedup_log.txt"
    log_path.write_text("\n".join(log), encoding="utf-8")
    print(f"Log decyzji deduplikacyjnych zapisany w {log_path.name} ({len(log)} wpisów)")
    print("Baza gotowa: catalog.db")

    conn.close()


if __name__ == "__main__":
    main()
