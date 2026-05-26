"""One-shot generator for china_regions_pinyin.json.

Run once when the upstream administrative-division dataset changes:

    .venv/bin/python gen_regions.py

Source: modood/Administrative-divisions-of-China (WTFPL).
Output: china_regions_pinyin.json — hierarchical {province -> city -> [districts]},
each entry storing the Chinese name plus pinyin variants (full + suffix-stripped).
"""
import itertools
import json
import re
import urllib.request
from pypinyin import pinyin, Style

PCA_URL = "https://raw.githubusercontent.com/modood/Administrative-divisions-of-China/master/dist/pca-code.json"
OUT = "china_regions_pinyin.json"

# Suffixes commonly trimmed when people say a region name informally.
SUFFIX_RE = re.compile(
    r"(省|市|自治区|特别行政区|壮族自治区|回族自治区|维吾尔自治区|"
    r"区|县|自治县|自治州|盟|旗|林区)$"
)


def _pinyin_variants(text: str) -> list[str]:
    """All heteronym combinations of `text` joined as one lowercase pinyin string.

    Cap at a few hundred combos to guard against pathological multi-reading
    chains; in practice region names with multiple polyphones are rare.
    """
    per_char = pinyin(text, style=Style.NORMAL, heteronym=True)
    combos = itertools.islice(itertools.product(*per_char), 256)
    return list({"".join(c).lower() for c in combos})


def variants(name: str) -> list[str]:
    """Return Chinese + pinyin variants (full and suffix-stripped) for a region name.

    Pinyin variants enumerate all heteronym readings so multi-reading chars
    like 朝 (chao/zhao) and 重 (zhong/chong) are both recognized.
    """
    seen, out = [], []

    def add(s: str) -> None:
        s = s.strip()
        if s and s not in seen:
            seen.append(s)
            out.append(s)

    add(name)
    short = SUFFIX_RE.sub("", name)
    if short and short != name:
        add(short)

    for v in _pinyin_variants(name):
        add(v)
    if short:
        for v in _pinyin_variants(short):
            add(v)
    return out


def main() -> None:
    print(f"Downloading {PCA_URL} ...")
    with urllib.request.urlopen(PCA_URL, timeout=30) as r:
        raw = json.loads(r.read().decode("utf-8"))

    out = {"regions": []}
    for prov in raw:
        prov_entry = {"p": variants(prov["name"]), "cities": []}
        for city in prov.get("children", []):
            city_entry = {"c": variants(city["name"]), "districts": []}
            for dist in city.get("children", []):
                city_entry["districts"].append({"d": variants(dist["name"])})
            prov_entry["cities"].append(city_entry)
        out["regions"].append(prov_entry)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    n_prov = len(out["regions"])
    n_city = sum(len(p["cities"]) for p in out["regions"])
    n_dist = sum(len(c["districts"]) for p in out["regions"] for c in p["cities"])
    print(f"Wrote {OUT}: {n_prov} provinces, {n_city} cities, {n_dist} districts")


if __name__ == "__main__":
    main()
