#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Скрипт для скачивания ГОСТ и ГОСТ Р с сайта meganorm.ru по списку из TXT-файла.

Разделы сайта (ГОСТ и ГОСТ Р) определяются автоматически: скрипт сканирует
все секции каталога /list/N-*.htm и строит единый индекс, в котором ключом
является полное обозначение стандарта ("ГОСТ 1.0-92", "ГОСТ Р 50571.1-2009").

Пользователю достаточно в TXT-файле написать полное обозначение, например:

    ГОСТ 1.0-92
    ГОСТ Р 50571.1-2009
    ГОСТ Р ИСО 9001-2015

Запуск:
    pip install requests beautifulsoup4
    python download_gosts.py gosts.txt
"""

import json
import os
import re
import sys
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# НАСТРОЙКИ
# ---------------------------------------------------------------------------

BASE_URL = "https://meganorm.ru"
DOWNLOAD_DIR = "downloads"
INDEX_CACHE = "index_cache.json"   # кэш индекса, чтобы не сканировать сайт повторно

SECTIONS_TO_SCAN = range(1, 60)    # попробуем секции 1..20
MAX_PAGES_PER_SECTION = 80         # максимум страниц в одной секции
EMPTY_PAGE_LIMIT = 3               # 3 пустых страницы подряд = конец секции

REQUEST_DELAY = 0.5                # пауза между запросами (сек)
REQUEST_TIMEOUT = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# Регулярное выражение для извлечения обозначения из текста ссылки.
# Захватывает: "ГОСТ 1.0-92", "ГОСТ Р 50571.1-2009",
#              "ГОСТ Р ИСО 9001-2015", "ГОСТ Р МЭК 60950-1-2014",
#              "ГОСТ 8.417-2002" и т.п.
DESIGNATION_RE = re.compile(
    r"""
    ^\s*
    (
        ГОСТ(?:\s+Р)?
        (?:\s+(?:ИСО|МЭК|ЕН))?       # необязательная серия
        \s+
        [\d\.\-]+                    # номер, точки и дефисы
        (?:-\d{2,4})?                # необязательный год
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# НОРМАЛИЗАЦИЯ
# ---------------------------------------------------------------------------

def normalize_designation(name: str) -> str:
    """Приводит обозначение к единому виду для сопоставления."""
    if not name:
        return ""
    name = name.strip().upper()
    name = re.sub(r"[–—−‐‑‒]", "-", name)          # все тире -> дефис
    name = re.sub(r"ГОСТ\s*Р\b", "ГОСТ Р", name)   # ГОСТР, ГОСТ  Р -> ГОСТ Р
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def extract_designation(text: str) -> str:
    """Извлекает «чистое» обозначение из текста ссылки на каталоге."""
    if not text:
        return ""
    m = DESIGNATION_RE.match(text)
    if m:
        return m.group(1)
    return text.strip()


def designation_variants(name: str) -> list[str]:
    """
    Возвращает список вариантов обозначения для поиска в индексе.
    Учитывает:
      - ГОСТ <-> ГОСТ Р
      - МЭК <-> IEC
      - ИСО <-> ISO
      - ЕН  <-> EN
    """
    norm = normalize_designation(name)
    if not norm:
        return []

    variants = set()
    variants.add(norm)

    # 1. ГОСТ <-> ГОСТ Р
    if norm.startswith("ГОСТ Р "):
        variants.add(norm.replace("ГОСТ Р ", "ГОСТ ", 1))
    elif norm.startswith("ГОСТ "):
        variants.add(norm.replace("ГОСТ ", "ГОСТ Р ", 1))

    # 2. Аббревиатуры МЭК/IEC, ИСО/ISO, ЕН/EN
    replacements = [
        ("МЭК", "IEC"), ("IEC", "МЭК"),
        ("ИСО", "ISO"), ("ISO", "ИСО"),
        ("ЕН", "EN"),  ("EN", "ЕН"),
    ]

    # Применяем замены ко всем уже накопленным вариантам
    current = list(variants)
    for variant in current:
        for rus, lat in replacements:
            if rus in variant:
                new_variant = variant.replace(rus, lat)
                variants.add(new_variant)
                # и ещё раз с добавлением/удалением «Р»
                if new_variant.startswith("ГОСТ Р "):
                    variants.add(new_variant.replace("ГОСТ Р ", "ГОСТ ", 1))
                elif new_variant.startswith("ГОСТ "):
                    variants.add(new_variant.replace("ГОСТ ", "ГОСТ Р ", 1))

    # 3. Если пользователь написал без префикса — добавим оба
    if not norm.startswith("ГОСТ"):
        variants.add("ГОСТ " + norm)
        variants.add("ГОСТ Р " + norm)

    return list(variants)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def fetch_page(url: str, session: requests.Session) -> str | None:
    try:
        resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return resp.text
    except requests.RequestException:
        return None


# ---------------------------------------------------------------------------
# ИНДЕКС
# ---------------------------------------------------------------------------

def scan_section(section: int, index: dict, session: requests.Session) -> int:
    """Сканирует одну секцию каталога. Возвращает число новых записей."""
    first_url = f"{BASE_URL}/list/{section}-0.htm"
    first_html = fetch_page(first_url, session)
    if not first_html:
        return 0

    print(f"  Секция {section} обнаружена, сканирую...")
    total_new = 0
    empty_streak = 0
    pages_scanned = 0

    for page in range(MAX_PAGES_PER_SECTION):
        if page == 0:
            html = first_html
            url = first_url
        else:
            url = f"{BASE_URL}/list/{section}-{page}.htm"
            html = fetch_page(url, session)

        pages_scanned += 1

        if not html:
            empty_streak += 1
            if empty_streak >= EMPTY_PAGE_LIMIT:
                break
            time.sleep(REQUEST_DELAY)
            continue

        soup = BeautifulSoup(html, "html.parser")
        new_on_page = 0

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if "/Index/" not in href:
                continue
            link_text = a_tag.get_text(strip=True)
            designation = extract_designation(link_text)
            if not designation or len(designation) < 5:
                continue
            key = normalize_designation(designation)
            if key and key not in index:
                index[key] = urljoin(url, href)
                new_on_page += 1

        total_new += new_on_page
        if new_on_page == 0:
            empty_streak += 1
            if empty_streak >= EMPTY_PAGE_LIMIT:
                break
        else:
            empty_streak = 0

        time.sleep(REQUEST_DELAY)

    print(f"    Просканировано страниц: {pages_scanned}, "
          f"найдено новых записей: {total_new}")
    return total_new


def build_index(session: requests.Session, use_cache: bool = True) -> dict:
    """Строит единый индекс по всем секциям сайта."""
    if use_cache and os.path.isfile(INDEX_CACHE):
        try:
            with open(INDEX_CACHE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data:
                print(f"Индекс загружен из кэша: {len(data)} записей.")
                return data
        except Exception:
            pass

    index: dict = {}
    print("Сбор индекса со всех секций сайта (это может занять несколько минут)...")

    for section in SECTIONS_TO_SCAN:
        try:
            scan_section(section, index, session)
        except Exception as e:
            print(f"  [!] Ошибка в секции {section}: {e}")

    print(f"\nИндекс собран: {len(index)} записей.\n")

    try:
        with open(INDEX_CACHE, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)
        print(f"Индекс сохранён в {INDEX_CACHE} (при следующем запуске будет "
              f"загружен из кэша).\n")
    except Exception:
        pass

    return index


# ---------------------------------------------------------------------------
# СКАЧИВАНИЕ
# ---------------------------------------------------------------------------

def find_pdf_url(page_url: str, session: requests.Session) -> str | None:
    html = fetch_page(page_url, session)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if href.lower().endswith(".pdf"):
            return urljoin(page_url, href)
    return None


def download_pdf(pdf_url: str, save_path: str, session: requests.Session) -> bool:
    try:
        resp = session.get(pdf_url, headers=HEADERS,
                           timeout=REQUEST_TIMEOUT, stream=True)
        resp.raise_for_status()
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return True
    except requests.RequestException as e:
        print(f"  [!] Ошибка скачивания {pdf_url}: {e}")
        return False


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    return name.strip() + ".pdf"


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def load_gost_list(filepath: str) -> list[str]:
    with open(filepath, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def main():
    if len(sys.argv) < 2:
        print("Использование: python download_gosts.py <файл_со_списком.txt>")
        sys.exit(1)

    list_file = sys.argv[1]
    if not os.path.isfile(list_file):
        print(f"Файл не найден: {list_file}")
        sys.exit(1)

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    gost_list = load_gost_list(list_file)
    print(f"Загружено {len(gost_list)} обозначений из {list_file}.\n")

    session = requests.Session()
    index = build_index(session)

    success = 0
    failed = 0
    not_found = []

    for i, name in enumerate(gost_list, 1):
        print(f"[{i}/{len(gost_list)}] {name}")

        matched_key = None
        for variant in designation_variants(name):
            if variant in index:
                matched_key = variant
                break

        if not matched_key:
            print(f"  [!] Не найдено в индексе.")
            # Подсказка: показать близкие обозначения
            prefix = normalize_designation(name)[:12]
            similar = [k for k in index.keys() if k.startswith(prefix)][:5]
            if similar:
                print(f"      Похожие: {', '.join(similar)}")
            failed += 1
            not_found.append(name)
            continue

        page_url = index[matched_key]
        pdf_url = find_pdf_url(page_url, session)
        if not pdf_url:
            print(f"  [!] PDF-ссылка не найдена на {page_url}")
            failed += 1
            continue

        save_path = os.path.join(DOWNLOAD_DIR, safe_filename(matched_key))
        if os.path.exists(save_path):
            print(f"  Уже скачано, пропуск.")
            success += 1
            continue

        print(f"  Найдено: {matched_key}")
        if download_pdf(pdf_url, save_path, session):
            print(f"  Сохранено: {save_path}")
            success += 1
        else:
            failed += 1

        time.sleep(REQUEST_DELAY)

    print("\n" + "=" * 60)
    print(f"Успешно скачано: {success}")
    print(f"Ошибок: {failed}")
    if not_found:
        print(f"Не найдено ({len(not_found)}):")
        for name in not_found:
            print(f"  - {name}")
    print(f"Папка: {os.path.abspath(DOWNLOAD_DIR)}")


if __name__ == "__main__":
    main()