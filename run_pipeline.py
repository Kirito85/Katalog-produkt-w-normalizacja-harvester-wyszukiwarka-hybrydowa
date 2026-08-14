"""Uruchamia cały pipeline: normalizacja -> harvester. Wygodne dla oceniającego."""
from app import normalize, harvester

if __name__ == "__main__":
    normalize.main()
    print()
    harvester.main()
