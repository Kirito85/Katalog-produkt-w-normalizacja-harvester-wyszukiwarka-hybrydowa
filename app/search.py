"""
Krok 3+4: wyszukiwarka hybrydowa (dokładne SKU + semantyka + filtry) ze
scoringiem i uzasadnieniem. Krok 5 (interfejs) to CLI poniżej.

Uruchomienie:
    python -m app.search "izolacja RNA z krwi"
    python -m app.search "PCR mix" --producent NovaGen --kategoria "Odczynniki PCR"
    python -m app.search "NO-10091"          # trafienie dokładne po SKU

Wybór "silnika embeddingów" (patrz README, sekcja "Wyszukiwanie semantyczne"):
w tym środowisku nie mam dostępu do zewnętrznych API embeddingów ani do
pobrania wag modelu (sieć ograniczona do PyPI/npm/GitHub). Zamiast tego
używam TF-IDF + redukcji wymiarowości (TruncatedSVD, czyli klasyczne LSA)
jako lekkiego, w pełni lokalnego substytutu embeddingów, z ręcznym
wyszukiwaniem po podobieństwie kosinusowym (odpowiednik "flat index" w
FAISS/Qdrant, tylko bez samej biblioteki). Interfejs (`embed_texts`,
`semantic_search`) jest wydzielony tak, żeby podmiana na prawdziwe
embeddingi (OpenAI/Cohere/lokalny model) + pgvector/Qdrant/FAISS wymagała
zmiany tylko w jednym miejscu.
"""
import argparse
import sys

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.decomposition import TruncatedSVD
from sklearn.metrics.pairwise import cosine_similarity

from app.db import get_conn

N_COMPONENTS = 64


def load_corpus(conn):
    rows = conn.execute("SELECT rowid, sku, search_blob FROM products").fetchall()
    ids = [r["sku"] for r in rows]
    texts = [r["search_blob"] or "" for r in rows]
    return ids, texts


def build_semantic_index(texts: list[str]):
    """Zwraca (vectorizer, svd, macierz_wektorow) — 'embeddingi' lokalne."""
    vectorizer = TfidfVectorizer(
        analyzer="word", ngram_range=(1, 2), min_df=1,
        lowercase=True, token_pattern=r"(?u)\b\w[\w\-]*\b",
    )
    tfidf = vectorizer.fit_transform(texts)
    n_comp = min(N_COMPONENTS, tfidf.shape[1] - 1, tfidf.shape[0] - 1)
    n_comp = max(n_comp, 2)
    svd = TruncatedSVD(n_components=n_comp, random_state=42)
    vectors = svd.fit_transform(tfidf)
    return vectorizer, svd, vectors


def embed_query(vectorizer, svd, query: str) -> np.ndarray:
    return svd.transform(vectorizer.transform([query]))


def hybrid_search(conn, query: str, producent: str | None = None,
                   kategoria: str | None = None, top_k: int = 10):
    ids, texts = load_corpus(conn)
    if not ids:
        return []

    vectorizer, svd, vectors = build_semantic_index(texts)
    q_vec = embed_query(vectorizer, svd, query)
    sims = cosine_similarity(q_vec, vectors)[0]

    exact_skus = {sku for sku in ids if sku.lower() == query.strip().lower()}

    results = []
    for sku, sim in zip(ids, sims):
        row = conn.execute("SELECT * FROM products WHERE sku=?", (sku,)).fetchone()
        if producent and (row["producent"] or "").lower() != producent.lower():
            continue
        if kategoria and (row["kategoria"] or "").lower() != kategoria.lower():
            continue

        is_exact = sku in exact_skus
        semantic_score = float(max(sim, 0.0))
        score = 1.0 if is_exact else round(0.85 * semantic_score, 4)

        if is_exact:
            reason = f"Dokładne dopasowanie numeru katalogowego '{sku}'."
        elif semantic_score > 0.05:
            reason = (f"Podobieństwo semantyczne zapytania do nazwy/kategorii/opisu "
                      f"produktu (cosine={semantic_score:.3f}, TF-IDF+LSA).")
        else:
            reason = "Słabe dopasowanie treściowe — niski wynik podobieństwa semantycznego."

        results.append({
            "sku": sku, "nazwa": row["nazwa"], "producent": row["producent"],
            "kategoria": row["kategoria"], "cena_pln": row["cena_pln"],
            "score": score, "reason": reason, "opis": row["opis"],
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    # odetnij czysty szum (score≈0) o ile mamy cokolwiek sensowniejszego do pokazania
    meaningful = [r for r in results if r["score"] > 1e-6]
    filtered = meaningful if meaningful else results
    return filtered[:top_k]


def print_results(results):
    if not results:
        print("Brak wyników.")
        return
    for i, r in enumerate(results, 1):
        cena = f"{r['cena_pln']:.2f} PLN" if r["cena_pln"] is not None else "brak ceny"
        print(f"{i}. [{r['score']:.3f}] {r['nazwa']}  (SKU {r['sku']}, {r['producent']}, "
              f"{r['kategoria'] or 'brak kategorii'}, {cena})")
        print(f"   uzasadnienie: {r['reason']}")
        if r["opis"]:
            snippet = r["opis"][:140] + ("…" if len(r["opis"]) > 140 else "")
            print(f"   opis: {snippet}")


def main():
    ap = argparse.ArgumentParser(description="Wyszukiwarka hybrydowa katalogu produktów")
    ap.add_argument("query", help="zapytanie tekstowe albo numer katalogowy")
    ap.add_argument("--producent", default=None)
    ap.add_argument("--kategoria", default=None)
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    conn = get_conn()
    results = hybrid_search(conn, args.query, args.producent, args.kategoria, args.top)
    print_results(results)
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
