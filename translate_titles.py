import csv
import gzip
import json
import os
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
env_path = BASE_DIR / ".env"
if env_path.exists():
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
OUTPUT_FILE = BASE_DIR / "watcha_ratings_english.csv"

VALID_FILM_TYPES = {
    "Q11424", "Q24862", "Q202866", "Q5398426",
    "Q506240", "Q15416", "Q21191270", "Q24856",
}

# Standard Unicode Hangul Jamo tables for algorithmic phonetic romanization
CHOSEONG = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "", "j", "jj", "ch", "k", "t", "p", "h"]
JUNGSEONG = ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae", "oe", "yo", "u", "wo", "we", "wi", "yu", "eu", "ui", "i"]
JONGSEONG = ["", "k", "k", "k", "n", "n", "n", "t", "l", "l", "l", "l", "l", "l", "l", "m", "p", "p", "s", "ss", "ng", "j", "ch", "k", "t", "p", "h"]


def find_latest_watcha_csv() -> Path:
    """Finds the most recent watcha_ratings*.csv in the project folder or Downloads."""
    candidates = list(BASE_DIR.glob("watcha_ratings*.csv")) + list((Path.home() / "Downloads").glob("watcha_ratings*.csv"))
    candidates = [p for p in candidates if "_english" not in p.name]
    if not candidates:
        raise FileNotFoundError(
            f"Could not find 'watcha_ratings.csv' in {BASE_DIR} or {Path.home() / 'Downloads'}."
        )
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    print(f"Using input file: {latest}")
    return latest


def clean_watcha_title(title: str) -> str:
    return re.sub(
        r"\s*[\(\[]?(리마스터링|4K\s*리마스터링|디지털\s*리마스터링|감독판|확장판|무삭제판|극장판|재개봉|디렉터스\s*컷|파이널\s*컷)[\)\]]?$",
        "",
        title.strip(),
        flags=re.IGNORECASE,
    ).strip()


def romanize_hangul(text: str) -> str:
    """Algorithmically converts Hangul syllables into Latin phonetics."""
    out = []
    for ch in text:
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:
            s_idx = code - 0xAC00
            cho = s_idx // 588
            jung = (s_idx % 588) // 28
            jong = s_idx % 28
            out.append(CHOSEONG[cho] + JUNGSEONG[jung] + JONGSEONG[jong])
        else:
            out.append(ch)
    return "".join(out)


def expand_word_forms(word: str) -> list[str]:
    """Algorithmic English morphological stems (e.g., Copy -> Copied, Copying)."""
    w = word.lower()
    forms = [word]
    if w.endswith("y") and len(w) > 3 and w[-2] not in "aeiou":
        stem = word[:-1]
        forms.extend([stem + "ied", stem + "ies", word + "ing"])
    elif w.endswith("e") and len(w) > 3:
        forms.extend([word + "d", word[:-1] + "ing"])
    elif len(w) > 3:
        forms.extend([word + "ed", word + "ing", word + "s"])
    return forms


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "WatchaResolver/2.0 (https://github.com)",
            "Accept-Encoding": "gzip",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))


def tmdb_get(path: str, params: dict) -> dict:
    params["api_key"] = TMDB_API_KEY
    return fetch_json(f"https://api.themoviedb.org/3{path}?{urllib.parse.urlencode(params)}")


def lookup_tmdb_primary(cleaned: str, year: str) -> str | None:
    """Pass 1: Fast Korean title + release year lookup on TMDB."""
    search_params = {"query": cleaned, "language": "ko-KR", "include_adult": "true"}
    if year:
        search_params["year"] = year

    results = tmdb_get("/search/movie", search_params).get("results", [])

    if not results and year:
        del search_params["year"]
        results = tmdb_get("/search/movie", search_params).get("results", [])

    if not results and ":" in cleaned:
        search_params["query"] = cleaned.split(":")[0].strip()
        results = tmdb_get("/search/movie", search_params).get("results", [])

    if not results:
        return None

    best = results[0]
    if year and year.isdigit():
        target_y = int(year)
        for r in results:
            rel = r.get("release_date", "")
            if rel[:4].isdigit() and abs(int(rel[:4]) - target_y) <= 1:
                best = r
                break

    details = tmdb_get(f"/movie/{best['id']}", {"language": "en-US"})
    return details.get("title") or details.get("original_title")


def lookup_tmdb_broad(cleaned: str, year: str) -> str | None:
    """Pass 2: Global/alternative title search with region=KR and ±2 year window."""
    for query in dict.fromkeys([cleaned, cleaned.replace(" ", "")]):
        results = tmdb_get("/search/movie", {"query": query, "region": "KR", "include_adult": "true"}).get("results", [])
        if not results:
            results = tmdb_get("/search/movie", {"query": query, "include_adult": "true"}).get("results", [])

        if results:
            best = None
            if year and year.isdigit():
                target_y = int(year)
                for r in results:
                    rel = r.get("release_date", "")
                    if rel[:4].isdigit() and abs(int(rel[:4]) - target_y) <= 2:
                        best = r
                        break
            if not best:
                best = results[0]

            details = tmdb_get(f"/movie/{best['id']}", {"language": "en-US"})
            en = details.get("title") or details.get("original_title")
            if en:
                return en
    return None


def lookup_verified_wiki(cleaned: str) -> str | None:
    """Pass 3: Korean Wikipedia search strictly verified via Wikidata P31 film types."""
    q = urllib.parse.quote(f'"{cleaned}" 영화')
    url = (
        f"https://ko.wikipedia.org/w/api.php?action=query&generator=search"
        f"&gsrsearch={q}&gsrlimit=5&prop=pageprops&ppprop=wikibase_item&format=json"
    )
    pages = fetch_json(url).get("query", {}).get("pages", {})
    qids = [
        p.get("pageprops", {}).get("wikibase_item")
        for p in sorted(pages.values(), key=lambda x: x.get("index", 99))
        if p.get("pageprops", {}).get("wikibase_item")
    ]
    if not qids:
        return None

    wd_url = (
        f"https://www.wikidata.org/w/api.php?action=wbgetentities"
        f"&ids={'|'.join(qids)}&props=claims|labels|sitelinks&languages=en&format=json"
    )
    entities = fetch_json(wd_url).get("entities", {})

    for qid in qids:
        ent = entities.get(qid, {})
        claims = ent.get("claims", {})
        inst = {c.get("mainsnak", {}).get("datavalue", {}).get("value", {}).get("id") for c in claims.get("P31", [])}
        if not (inst & VALID_FILM_TYPES):
            continue

        en_title = ent.get("sitelinks", {}).get("enwiki", {}).get("title") or ent.get("labels", {}).get("en", {}).get("value")
        if en_title and "(disambiguation)" not in en_title.lower():
            return re.sub(r"\s*\([^)]*film[^)]*\)$", "", en_title, flags=re.IGNORECASE).strip()

    return None


def lookup_translate_and_verify(cleaned: str, year: str) -> str | None:
    """Pass 4: Translation + Hangul phonetic romanization + morphological stemming verified against TMDB."""
    q = urllib.parse.quote(cleaned)
    url = f"https://api.mymemory.translated.net/get?q={q}&langpair=ko|en"
    data = fetch_json(url)
    translated = data.get("responseData", {}).get("translatedText", "").strip().rstrip(".")

    romanized = romanize_hangul(cleaned)
    loanword_approx = (
        romanized.replace("eo", "er")
        .replace("ae", "a")
        .replace("eu", "")
        .replace("oo", "u")
    )

    if year and year.isdigit():
        # Sub-pass A: Full phrase & phonetic loanword candidates first
        for exact_cand in dict.fromkeys(c for c in (translated, romanized, loanword_approx) if c):
            results = tmdb_get(
                "/search/movie",
                {"query": exact_cand, "primary_release_year": year, "language": "en-US"},
            ).get("results", [])
            if results:
                best = max(results, key=lambda r: (r.get("vote_count", 0), r.get("popularity", 0)))
                return best.get("title") or best.get("original_title")

        # Sub-pass B: Pooled single-word + stemmed candidates
        word_queries = []
        if translated:
            words = [w for w in re.findall(r"[A-Za-z0-9]+", translated) if len(w) >= 4]
            for w in words:
                word_queries.extend(expand_word_forms(w))

        pooled: dict[int, dict] = {}
        for cand in dict.fromkeys(word_queries):
            results = tmdb_get(
                "/search/movie",
                {"query": cand, "primary_release_year": year, "language": "en-US"},
            ).get("results", [])
            for r in results:
                pooled[r["id"]] = r

        if pooled:
            best = max(pooled.values(), key=lambda r: (r.get("vote_count", 0), r.get("popularity", 0)))
            return best.get("title") or best.get("original_title")

    return translated or None


def process_one(idx: int, row: list[str]) -> tuple[int, list[str], str]:
    if len(row) < 3:
        return idx, row, "SKIP"

    raw_title, year, rating = row[0], row[1], row[2]
    if not re.search(r"[^\u0000-\u024F]", raw_title):
        return idx, [raw_title, year, rating], "ASCII"

    cleaned = clean_watcha_title(raw_title)

    try:
        # Cascade through all resolution tiers in order of confidence
        tiers = [
            ("TMDB", lambda: lookup_tmdb_primary(cleaned, year)),
            ("TMDB-BROAD", lambda: lookup_tmdb_broad(cleaned, year)),
            ("WIKI", lambda: lookup_verified_wiki(cleaned)),
            ("TRANSLATE+TMDB", lambda: lookup_translate_and_verify(cleaned, year)),
        ]
        for label, resolver in tiers:
            en = resolver()
            if en and en != raw_title:
                return idx, [en, year, rating], label

        return idx, [raw_title, year, rating], "MISS"
    except Exception as e:
        return idx, [raw_title, year, rating], f"ERR:{e}"


def main():
    if not TMDB_API_KEY:
        raise RuntimeError("Missing TMDB_API_KEY. Create a .env file with TMDB_API_KEY=your_key")

    start = time.time()
    input_file = find_latest_watcha_csv()

    with open(input_file, mode="r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))

    header, data = rows[0], rows[1:]
    results: list[list[str] | None] = [None] * len(data)
    done = 0

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(process_one, i, row) for i, row in enumerate(data)]
        for fut in as_completed(futures):
            idx, out_row, status = fut.result()
            results[idx] = out_row
            done += 1
            print(f"[{done}/{len(data)}] [{status}] {data[idx][0]} ({data[idx][1]}) -> {out_row[0]}", flush=True)

    with open(OUTPUT_FILE, mode="w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(results)

    print(f"\nDone in {time.time() - start:.1f}s! Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()