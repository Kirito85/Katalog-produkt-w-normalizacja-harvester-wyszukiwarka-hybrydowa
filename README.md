# Product Catalog — Normalization + Harvester + Hybrid Search

Solution to the M1 recruitment task (see `data/README_zadanie.md` — original task description). Below are the design decisions, assumptions, and what I would do differently with more time.

## How to Run

```bash
pip install -r requirements.txt

python run_pipeline.py          # normalization + dedup + harvester -> catalog.db

python -m app.search "RNA isolation from blood"
python -m app.search "PCR mix" --producent "NovaGen Labs" --kategoria "PCR Reagents"
python -m app.search "NO-10091"                 # exact SKU match

python -m app.server             # alternatively: HTTP on :8000/search?q=...
```

Output files: `catalog.db` (SQLite), `dedup_log.txt`, `harvester_log.txt` (decision rationale, regenerated on every run).

## Structure

```text
app/
  clean.py       # pure functions for normalizing fields (category/package/price)
  db.py          # SQLite schema
  normalize.py   # step 1: CSV loading, cleaning, deduplication, database storage
  harvester.py   # step 2: parsing manufacturer HTML, merging by SKU
  search.py      # steps 3+4: hybrid search + scoring, CLI
  server.py      # step 5 (HTTP variant)
data/            # source files (untouched by the pipeline)
```

## 1. Normalization

**Target schema** (the `products` table in SQLite — see `app/db.py`):

`sku`, `nazwa`, `producent`, `kategoria` (+ `kategoria_zrodlowa` for auditing), `opak_ilosc`/`opak_jednostka` (+ `opak_surowa`), `cena_pln`, `atrybuty_dodatkowe`, plus harvester fields (`opis`, `specyfikacja`, `zrodlo_opisu`) and `search_blob`, which is used by the search engine.

**Why SQLite instead of Postgres**: zero setup, no dependency on an external service in the sandbox environment I worked in (no network access to a Postgres instance). The schema is directly portable to Postgres (simple data types, with the exception of SQLite-specific FTS5; in Postgres this could be replaced with `tsvector` or, for semantic search, `pgvector`, discussed below).

**Categories**: I grouped rows by `nazwa` and collected all category variants that appeared for each product in the source data. It turned out that the products map to exactly **5 canonical categories** under different PL/EN spellings (e.g. `PCR reagents` / `PCR - odczynniki` / `Odczynniki PCR` / `Odczynniki do PCR` → `Odczynniki PCR`). The mapping is defined in `app/clean.py:CATEGORY_MAP`.

For 46 rows with an empty category, if another row with the **same product name** had a known category, I assigned that category and marked it with `kategoria_uzupelniona=1` — this is an inference, not a source fact, so I kept an audit flag rather than silently filling it in.

**Packaging**: quantities occur exclusively in the set `{1,5,10,25,50,100}`, while the "units" (`kg`/`l`/`ml`/`g`/`szt`/`op`/`-`/`rxn`/`reactions`/`x50`/`50-pack`/`50 test.` etc.) do not make physical sense for the products (e.g. PCR tubes with "50 kg") — this is noise from the test data generator, not something that can be meaningfully "fixed".

I therefore standardized only the **textual representation** of synonyms (`rxn`/`reactions`/`test.`/`pack` → one label, `x50` → quantity=50), while preserving the original representation in `opak_surowa` for auditing. I did not attempt to infer the "correct" physical unit because the data does not provide enough information to do so.

**Price**: parsing `1021 zl` / `4199 PLN` / `1990,00` / `2156` into `float` values in PLN (comma treated as the decimal separator). 5 rows have no price in the source → `cena_pln=NULL`, `cena_brak=1` (I do not guess prices).

### Duplicate / Near-Duplicate Catalog Numbers

Observed patterns (see `dedup_log.txt` after running the pipeline):

1. **9 pairs of rows identical across all fields** (same SKU, duplicated entry) → one row is kept.
2. **1 pair with the same SKU (`PO-10022`)**, differing only in the presence of `atrybuty_dodatkowe` (one row empty, the other populated) → merged by keeping the non-empty value.
3. **4 pairs with SKUs differing only by a suffix** — a trailing letter (`CH-10248` / `CH-10248A`, `PO-10093` / `PO-10093A`) or `-1` (`BI-10220` / `BI-10220-1`, `BI-10282` / `BI-10282-1`) — with identical name, manufacturer, packaging, and price. I treated these as typos/duplicate entries for the same product and kept the "cleaner" (shorter) SKU.

Result: **228 → 214 products** (14 rows removed).

I deliberately did **NOT** treat many pairs with small edit distances between catalog numbers (e.g. `PO-10022` / `PO-10029`) as duplicates — the catalog has densely packed numbers (~230 products in the 10000–10360 range per manufacturer), so random edit distances of 1–2 are common and do not imply the same product.

The duplicate criterion is therefore **SKU similarity AND identical remaining fields** (name + manufacturer + packaging + price), not SKU similarity alone.

## 2. Mini-Harvester

The manufacturer snapshot `producent_novagen_snapshot.html` is parsed with BeautifulSoup and merged into the database **exclusively by `data-sku`**, as required by the task (names differ between sources).

* **SKU typo**: the `NO-103l6` card (letter `l` instead of digit `1`) has no exact match in the catalog. I added a fallback: search among known SKUs from the same manufacturer (prefix `NO-`) for the unique candidate with an edit distance ≤1 — in this case, `NO-10316`. If there were multiple candidates, I would **not guess** and would leave the record unmatched, as this is safer than creating a false match.

* **Different name, same SKU**: the `NO-10064` card is named `Mix PCR 2x Universal`, while the CSV contains the same SKU under `Mix do PCR 2x`. I decided that the **CSV name remains authoritative** for product identification (it is tied to the price/package/category data), while the manufacturer snapshot is treated purely as enrichment (`opis`, `specyfikacja`) and does not overwrite the name. The discrepancy is logged for auditing.

* **Unmatched entries**: 2 cards in the snapshot (`NO-10500` — new product, `NO-99999` — test/discontinued item) have no corresponding CSV entry. I decided **not to discard them**, but to add them as full records in the database (source = harvester), with `NULL` in fields normally sourced from the CSV (price/category/package). They are searchable but explicitly marked as incomplete.

  An alternative would be to discard them; for a real client, I would rather keep the data visible and clearly marked as incomplete than silently lose it.

  34 NovaGen Labs products in the catalog have no corresponding entry in the snapshot — this is not an error; the snapshot is intentionally incomplete (as described in the task).

## 3–4. Hybrid Search + Scoring

`app/search.py`: `hybrid_search()` combines:

* **Exact SKU matching** → score `1.0`, with the explanation: "exact catalog number match";
* **Semantic search** over `search_blob` (name + manufacturer + category + attributes + harvested description) → score = `0.85 × cosine similarity`, with the explanation containing the cosine value and method;
* **Filters** by `producent`/`kategoria` (hard filters applied before scoring; they narrow the result set but do not affect the numerical score).

Results with a score ≈0 are filtered out as long as there is at least one meaningful result — otherwise, a query for a rare term would return a page full of noise.

### Semantic Search — What "Embedding" Means Here

In the environment I worked in, I did not have access to an external embedding API or the ability to download model weights (network access was limited to PyPI/npm/GitHub, without Hugging Face).

Rather than falsely claiming to use "real" embeddings, I used **TF-IDF (1–2 grams) + TruncatedSVD (LSA)** as a lightweight, fully local substitute, with manually computed cosine similarity. Functionally, this is similar to a "flat index" from FAISS/Qdrant, without using an actual vector database/library.

This works reasonably well for such a small, domain-specific corpus (214 products with laboratory-specific vocabulary), but it does not generalize well to queries phrased using completely different terminology from the catalog. A proper sentence-embedding model would perform better in that scenario.

The code is structured so that replacing this with real embeddings + pgvector/Qdrant/FAISS would require changes only to `build_semantic_index` / `embed_query`.

## 5. Interface

CLI:

```bash
python -m app.search "..."
```

Alternatively, a simple HTTP endpoint:

```bash
python -m app.server
```

implemented using the standard-library `http.server`. Since there is only one route, adding a web framework was not justified.

## Assumptions Where the Data Was Ambiguous

* I did not attempt to correct packaging units semantically (see above) — the data is constructed so that the unit does not carry meaningful information about the product; I only standardized its representation.
* A duplicate catalog number means **similar SKU + identical remaining fields**, never SKU similarity alone — otherwise, the dense catalog would produce many false duplicates.
* The **CSV name is the source of truth** for product identification; manufacturer data is treated as enrichment and does not overwrite it.
* Harvester entries without a CSV match are **retained and marked as incomplete**, rather than discarded.

## What I Would Do Differently With More Time

* Use real embeddings (e.g. a local model via `sentence-transformers` or an API) instead of TF-IDF/LSA — especially important for natural-language queries that do not share vocabulary with product names.
* Use a real Postgres + `pgvector` setup instead of SQLite + manual cosine similarity — easier to scale and allows both mechanisms to live in a single database.
* Add unit tests for `app/clean.py` (the normalization functions are fully deterministic and easy to test in isolation) as well as for the deduplication/matching logic in the harvester (edge cases: >1 fuzzy-match candidate, conflicting fields for the same SKU).
* Weight `atrybuty_dodatkowe` separately in semantic ranking (currently it is simply included in a single `search_blob` without field-level weighting).
* Support multiple manufacturer snapshots (currently the harvester handles only one file and assumes `producent='NovaGen Labs'` for unmatched entries).
