"""
Krok 2: mini-harvester. Parsuje producent_novagen_snapshot.html i merguje
opisy/specyfikacje do bazy powstałej w normalize.py, dopasowując WYŁĄCZNIE po
numerze katalogowym (nie po nazwie — nazwy się różnią, patrz README).

Uruchomienie:  python -m app.harvester   (wymaga wcześniejszego `python -m app.normalize`)
"""
import json
from pathlib import Path

from bs4 import BeautifulSoup

from app.db import get_conn

SNAPSHOT_HTML = Path(__file__).resolve().parent.parent / "data" / "producent_novagen_snapshot.html"


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    m, n = len(a), len(b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        dp[i][0] = i
    for j in range(n + 1):
        dp[0][j] = j
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            dp[i][j] = min(
                dp[i - 1][j] + 1,
                dp[i][j - 1] + 1,
                dp[i - 1][j - 1] + (a[i - 1] != b[j - 1]),
            )
    return dp[m][n]


def parse_snapshot():
    soup = BeautifulSoup(SNAPSHOT_HTML.read_text(encoding="utf-8"), "html.parser")
    cards = []
    for card in soup.select(".product-card"):
        sku = card.get("data-sku", "").strip()
        name_el = card.select_one(".product-name")
        desc_el = card.select_one(".product-desc")
        specs = [li.get_text(strip=True) for li in card.select(".specs li")]
        cards.append({
            "sku_raw": sku,
            "nazwa": name_el.get_text(strip=True) if name_el else None,
            "opis": desc_el.get_text(strip=True) if desc_el else None,
            "specyfikacja": specs,
        })
    return cards


def match_sku(sku_raw: str, known_skus: set[str]) -> tuple[str | None, str]:
    """
    Zwraca (dopasowany_sku_lub_None, powod).
    Dopasowanie: najpierw dokładne; jeśli brak, szukamy jedynego kandydata
    w odległości edycyjnej <=1 wśród numerów katalogowych TEGO SAMEGO
    producenta (prefiks NO-) — to obsługuje literówkę w SKU (np. "NO-103l6"
    zamiast "NO-10316": 'l' zamiast '1').
    """
    if sku_raw in known_skus:
        return sku_raw, "exact"

    candidates = [s for s in known_skus if s.startswith("NO-") and _levenshtein(sku_raw, s) <= 1]
    if len(candidates) == 1:
        return candidates[0], "fuzzy(edit_distance<=1)"
    return None, "no_match"


def main():
    conn = get_conn()
    known = {r["sku"] for r in conn.execute("SELECT sku FROM products")}

    cards = parse_snapshot()
    print(f"Sparsowano {len(cards)} kart produktowych z {SNAPSHOT_HTML.name}")

    log = []
    matched, unmatched_cards = 0, []

    for card in cards:
        target_sku, how = match_sku(card["sku_raw"], known)
        if target_sku is None:
            unmatched_cards.append(card)
            continue

        row = conn.execute("SELECT nazwa FROM products WHERE sku=?", (target_sku,)).fetchone()
        note = None
        if how != "exact":
            note = f"SKU ze snapshotu '{card['sku_raw']}' potraktowany jako literówka -> dopasowano do '{target_sku}' ({how})."
            log.append(note)
        if row and card["nazwa"] and row["nazwa"] != card["nazwa"]:
            msg = (f"SKU {target_sku}: nazwa w snapshocie producenta ('{card['nazwa']}') różni się "
                   f"od nazwy w katalogu ('{row['nazwa']}') — dopasowano mimo to PO SKU, nazwa w "
                   f"katalogu pozostaje wiodąca (nazwa z CSV traktowana jako źródło prawdy dla "
                   f"identyfikacji, opis/specyfikacja z producenta to wzbogacenie).")
            log.append(msg)

        conn.execute(
            "UPDATE products SET opis=?, specyfikacja=?, zrodlo_opisu=?, "
            "search_blob = search_blob || ' | ' || ? WHERE sku=?",
            (
                card["opis"],
                json.dumps(card["specyfikacja"], ensure_ascii=False),
                f"harvester:{card['sku_raw']}" + ("" if how == "exact" else f" ({how})"),
                card["opis"] or "",
                target_sku,
            ),
        )
        matched += 1

    # Karty bez pary w katalogu (nowy produkt / wycofywany / cokolwiek spoza CSV):
    # decyzja projektowa (patrz README) -> dodajemy je do bazy jako pełnoprawne
    # produkty źródła 'harvester', z NULL tam gdzie CSV normalnie dostarczałby
    # dane (cena/kategoria/opakowanie), zamiast je pomijać - są przeszukiwalne,
    # ale wyraźnie oznaczone, skąd pochodzą i że brakuje im części pól.
    for card in unmatched_cards:
        sku = card["sku_raw"]
        blob = " | ".join(x for x in [card["nazwa"], card["opis"] or ""] if x)
        conn.execute(
            "INSERT INTO products (sku, nazwa, producent, opis, specyfikacja, zrodlo_opisu, "
            "duplikat_uzasadnienie, search_blob, cena_brak) VALUES (?,?,?,?,?,?,?,?,1)",
            (
                sku, card["nazwa"], "NovaGen Labs", card["opis"],
                json.dumps(card["specyfikacja"], ensure_ascii=False),
                f"harvester:{sku} (brak dopasowania w CSV)",
                "Pozycja obecna wyłącznie w snapshocie producenta, brak w katalog_probka.csv "
                "(nowy produkt lub wycofywany — nie da się rozstrzygnąć automatycznie). "
                "Cena/kategoria/opakowanie nieznane.",
                blob,
            ),
        )
        log.append(f"SKU {sku}: karta producenta bez pary w CSV -> dodano jako nową pozycję "
                    f"(source=harvester), pola z CSV pozostają puste.")

    # CSV NovaGen bez pary w snapshocie (informacyjnie, nie wymaga akcji)
    novagen_no_desc = conn.execute(
        "SELECT COUNT(*) c FROM products WHERE producent='NovaGen Labs' AND opis IS NULL"
    ).fetchone()["c"]
    log.append(f"{novagen_no_desc} pozycji NovaGen Labs w katalogu nie ma odpowiednika w snapshocie "
               f"(snapshot jest z założenia niepełny) -> pozostają bez opisu, to nie błąd.")

    # tabela products_fts to "external content" fts5 -> po INSERT/UPDATE na
    # products trzeba ją jawnie przebudować
    conn.execute("INSERT INTO products_fts(products_fts) VALUES('rebuild')")
    conn.commit()

    log_path = Path(__file__).resolve().parent.parent / "harvester_log.txt"
    log_path.write_text("\n".join(log), encoding="utf-8")
    print(f"Dopasowano {matched} kart do istniejących SKU, {len(unmatched_cards)} dodano jako nowe pozycje.")
    print(f"Log: {log_path.name} ({len(log)} wpisów)")
    conn.close()


if __name__ == "__main__":
    main()
