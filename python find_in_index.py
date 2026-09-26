# find_in_index.py
# Запуск:  python find_in_index.py "2.105"  "60950"
import json
import sys
from download_gosts import build_index, normalize_designation  # если файл называется иначе — поправьте

import requests

if len(sys.argv) < 2:
    print("Использование: python find_in_index.py <фрагмент1> <фрагмент2> ...")
    sys.exit(1)

session = requests.Session()
index = build_index(session)  # подхватит index_cache.json, если он есть

print(f"\nВсего записей в индексе: {len(index)}\n")

for needle in sys.argv[1:]:
    needle_norm = normalize_designation(needle)
    print(f"=== Поиск: '{needle}' (ищем '{needle_norm}') ===")
    hits = [k for k in index if needle_norm in k]
    if not hits:
        print("  Ничего не найдено.\n")
        continue
    for k in hits[:50]:
        print(f"  {k}  ->  {index[k]}")
    if len(hits) > 50:
        print(f"  ... и ещё {len(hits) - 50}")
    print()