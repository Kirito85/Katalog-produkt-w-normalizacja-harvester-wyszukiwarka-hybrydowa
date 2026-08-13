"""
Funkcje normalizujące pola z surowego CSV. Wydzielone osobno, żeby
normalize.py i harvester.py mogły z nich korzystać bez duplikacji,
i żeby dało się je łatwo przetestować w izolacji.
"""
import re

# --- Kategorie -------------------------------------------------------------
# Mapowanie zaobserwowane empirycznie: pogrupowałem produkty po nazwie i
# zestawiłem wszystkie warianty kategorii, jakie się dla niej pojawiały
# (patrz README, sekcja "Kategorie"). Wynik: 5 kategorii kanonicznych.
CATEGORY_MAP = {
    # Izolacja kwasów nukleinowych
    "izolacja dna": "Izolacja kwasów nukleinowych",
    "izolacja kwasow nukleinowych": "Izolacja kwasów nukleinowych",
    "nucleic acid isolation": "Izolacja kwasów nukleinowych",
    "izolacja dna/rna": "Izolacja kwasów nukleinowych",
    # Odczynniki PCR
    "pcr reagents": "Odczynniki PCR",
    "pcr - odczynniki": "Odczynniki PCR",
    "odczynniki pcr": "Odczynniki PCR",
    "odczynniki do pcr": "Odczynniki PCR",
    # Odczynniki chemiczne (ogólne)
    "chemia laboratoryjna": "Odczynniki chemiczne",
    "odczynniki chemiczne": "Odczynniki chemiczne",
    "chemicals": "Odczynniki chemiczne",
    "odczynniki": "Odczynniki chemiczne",
    # Plastik / sprzęt jednorazowy
    "laboratory plasticware": "Plastik laboratoryjny",
    "plastik laboratoryjny": "Plastik laboratoryjny",
    "plastiki lab.": "Plastik laboratoryjny",
    "sprzet jednorazowy": "Plastik laboratoryjny",
    # Aparatura pomiarowa
    "aparatura pomiarowa": "Aparatura pomiarowa",
    "measuring equipment": "Aparatura pomiarowa",
    "pomiary": "Aparatura pomiarowa",
    "sprzet pomiarowy": "Aparatura pomiarowa",
}


def normalize_category(raw: str | None) -> str | None:
    if raw is None or (isinstance(raw, float)):
        return None
    key = raw.strip().lower()
    if not key:
        return None
    return CATEGORY_MAP.get(key, raw.strip())  # nieznane -> zostaw oryginał, nie zgadujemy


# --- Opakowanie --------------------------------------------------------------
# Obserwacja: ilości występują tylko w zbiorze {1,5,10,25,50,100}, a "jednostki"
# to w praktyce szum generatora (nie mają fizycznego sensu - "50 kg" probówek).
# Nie próbujemy więc korygować jednostki merytorycznie, tylko ujednolicić zapis
# tych samych synonimów tekstowych do jednej etykiety.
UNIT_SYNONYMS = {
    "kg": "kg", "g": "g", "l": "l", "ml": "ml", "op": "op", "szt": "szt",
    "szt.": "szt", "reactions": "rxn", "rxn": "rxn", "test.": "szt",
    "pack": "szt", "-": "nieznana",
}

# Specjalne, nietypowe zapisy całego pola (nie "liczba jednostka", tylko np. "x50")
_PACK_RE = re.compile(r"^x(\d+)$", re.IGNORECASE)
_DASH_PACK_RE = re.compile(r"^(\d+)-pack$", re.IGNORECASE)
_STD_RE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*([A-Za-z.\-]+)$")
_BARE_NUM_RE = re.compile(r"^(\d+(?:[.,]\d+)?)$")


def normalize_package(raw: str | None) -> tuple[float | None, str | None, str | None]:
    """Zwraca (ilosc, jednostka_znormalizowana, surowy_zapis)."""
    if raw is None or (isinstance(raw, float)):
        return None, None, raw
    s = raw.strip()
    if not s:
        return None, None, raw

    m = _PACK_RE.match(s)
    if m:
        return float(m.group(1)), "szt", raw

    m = _DASH_PACK_RE.match(s)
    if m:
        return float(m.group(1)), "szt", raw

    m = _BARE_NUM_RE.match(s)
    if m:
        return float(m.group(1)), "nieznana", raw

    m = _STD_RE.match(s)
    if m:
        num = float(m.group(1).replace(",", "."))
        unit_raw = m.group(2).strip().lower()
        unit = UNIT_SYNONYMS.get(unit_raw, unit_raw)
        return num, unit, raw

    return None, None, raw


# --- Cena --------------------------------------------------------------------
_PRICE_RE = re.compile(r"([\d.,]+)")


def normalize_price(raw: str | None) -> float | None:
    """Sprowadza zapisy '1021 zl' / '4199 PLN' / '1990,00' / '2156' do float PLN."""
    if raw is None or (isinstance(raw, float)):
        return None
    s = str(raw).strip()
    if not s:
        return None
    m = _PRICE_RE.search(s)
    if not m:
        return None
    num = m.group(1)
    # format polski: przecinek jako separator dziesiętny, ewentualna kropka jako tysięczny
    if "," in num and "." in num:
        num = num.replace(".", "").replace(",", ".")
    elif "," in num:
        num = num.replace(",", ".")
    try:
        return round(float(num), 2)
    except ValueError:
        return None


def normalize_name(raw: str) -> str:
    return " ".join(str(raw).strip().split())


def normalize_producer(raw: str) -> str:
    return " ".join(str(raw).strip().split())
