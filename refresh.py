#!/usr/bin/env python3
"""Decorate a curated listing tab in place with detail and amenity columns.

Reads the URLs already in the target tab (column A) and rewrites it in the
curated layout below. Auto columns are recomputed from immobiliare.it's search
API on every run; manual columns are sticky, matched by URL, so human input
(and Paola's notes) survives a refresh. Nothing is written to any immobiliare
account.

Layout (A..W):
  auto:   URL, Titolo, Prezzo (EUR), Superficie (m2), Condizione, Locali,
          Bagni, Piano, Dist. mare (m), Dist. centro (m), Anno / ristrutt.,
          Indirizzo / Zona, Zona, Esterni, Parcheggio, Arredato, Dotazioni
  manual: Proprietà, Lavori, Adatto affitto, Stato, Note
  tail:   Prezzo/m2

`Lavori` is prefilled with "ristrutturazione" when the listing condition says
so and the cell is empty; any manual edit then sticks.

Usage:
    python3 refresh.py --spreadsheet <ID|URL> --tab Appartamenti \
        --city Fano --category residential --account you@example.com [--sort]
"""

import argparse
import json
import math
import re
import subprocess
import urllib.parse
import urllib.request

API = "https://www.immobiliare.it/api-next/search-list/listings/"
AUTOCOMPLETE = "https://www.immobiliare.it/api-next/geography/autocomplete/"
UA = "Mozilla/5.0 (X11; Linux x86_64) listing-tracker-refresh/1.0"
CATEGORIES = {"commercial": ("26", "negozi"), "residential": ("1", "case")}
DEFAULT_CENTRE = (43.8436, 13.0170)  # Piazza XX Settembre, Fano

AUTO_HEADER = [
    "URL", "Titolo", "Prezzo (EUR)", "Superficie (m2)", "Condizione", "Locali",
    "Bagni", "Piano", "Dist. mare (m)", "Dist. centro (m)", "Anno / ristrutt.",
    "Indirizzo / Zona", "Zona", "Esterni", "Parcheggio", "Arredato", "Dotazioni",
    "Frazionabile", "Manutenzione", "Costo lavori (EUR)", "Costo totale (EUR)",
    "Costo tot./m2",
]
MANUAL_HEADER = ["Proprietà", "Lavori", "Adatto affitto", "Stato", "Note"]
HEADER = AUTO_HEADER + MANUAL_HEADER + ["Prezzo/m2"]
NCOL = len(HEADER)
PM2_FORMULA = '=INDIRECT("C"&ROW())/INDIRECT("D"&ROW())'


def token(account):
    args = ["gcloud", "auth", "print-access-token"] + ([account] if account else [])
    out = subprocess.run(args, capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit("gcloud token failed; run: gcloud auth login "
                         "--enable-gdrive-access\n" + out.stderr.strip())
    return out.stdout.strip()


def sheets(tok, sid, path, body=None, method="GET"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"https://sheets.googleapis.com/v4/spreadsheets/{sid}{path}",
        data=data, method=method,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req))


def api_json(url):
    return json.load(urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30))


def parse_spreadsheet_id(s):
    if "/d/" in s:
        return s.split("/d/", 1)[1].split("/")[0].split("?")[0].split("#")[0]
    return s


def sheet_id(tok, sid, title):
    for s in sheets(tok, sid, "?fields=sheets.properties")["sheets"]:
        if s["properties"]["title"] == title:
            return s["properties"]["sheetId"]
    raise SystemExit(f"tab not found: {title}")


def resolve_city(name):
    q = urllib.parse.urlencode({"macrozones": "1", "min_level": "9",
                                "query": name, "__lang": "it"})
    for entry in api_json(f"{AUTOCOMPLETE}?{q}"):
        p = {x["type"]: x for x in entry.get("parents", [])}
        if 2 in p and 1 in p and 0 in p and p[2]["label"].lower() == name.lower():
            return p[0]["id"], p[1]["id"], p[2]["id"], p[2]["keyurl"].lower()
    raise SystemExit(f"city not found: {name}")


def haversine(a, b):
    r = 6371000
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = math.radians(b[0] - a[0]), math.radians(b[1] - a[1])
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(2 * r * math.asin(math.sqrt(x)))


def year_note(desc):
    d = (desc or "").lower()
    yrs = [int(y) for y in re.findall(r"\b(18\d\d|19\d\d|20[0-2]\d)\b", d)]
    parts = []
    if "ristruttur" in d:
        ry = [y for y in yrs if y >= 1980]
        parts.append("ristrutturato" + (f" {max(ry)}" if ry else ""))
    if re.search(r"costru\w+ nel|anno di costruzione|edificato nel", d) and yrs:
        parts.append(f"costr. {min(yrs)}")
    if not parts and yrs:
        parts.append(f"anno? {min(yrs)}")
    return "; ".join(parts) or "n/d"


def amenities(pr):
    """Return (esterni, parcheggio, arredato, dotazioni) from feature fields."""
    s = set()
    for f in pr.get("ga4features") or []:
        s.add(str(f).lower())
    for f in pr.get("featureList") or []:
        if isinstance(f, dict) and f.get("type"):
            s.add(str(f["type"]).lower())
    def any_of(*keys):
        return bool(s & set(keys))
    esterni = [n for n, k in (("terrazzo", ("terrazzo", "terrace")),
                              ("balcone", ("balcone", "balcony")),
                              ("giardino", ("giardino", "garden"))) if any_of(*k)]
    park = []
    if any_of("box", "garage"):
        park.append("box")
    if any_of("posto auto", "parking"):
        park.append("posto auto")
    if "parzialmente arredato" in s:
        arred = "parziale"
    elif any_of("arredato", "furniture"):
        arred = "arredato"
    else:
        arred = ""
    if "cucina" in s:
        arred = (arred + "+cucina") if arred else "cucina"
    dot = [n for n in ("ascensore", "cantina", "mansarda", "caminetto", "taverna",
                       "fibra ottica", "porta blindata", "idromassaggio") if n in s]
    if "elevator" in s and "ascensore" not in dot:
        dot.insert(0, "ascensore")
    if "basement" in s and "cantina" not in dot:
        dot.append("cantina")
    return (", ".join(esterni) or "no", " + ".join(park) or "no",
            arred or "no", ", ".join(dot))


def fetch_details(city, category, centre):
    idc, seg = CATEGORIES[category]
    region, prov, comune, keyurl = resolve_city(city)
    base = {"fkRegione": region, "idProvincia": prov, "idComune": comune,
            "idContratto": "1", "idCategoria": idc, "__lang": "it",
            "paramsCount": "0", "path": f"/vendita-{seg}/{keyurl}/"}
    det = {}
    page, seen = 1, 0
    while True:
        p = dict(base, pag=str(page))
        try:
            d = api_json(f"{API}?{urllib.parse.urlencode(p)}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                break
            raise
        for it in d.get("results", []):
            re_ = it["realEstate"]
            props = re_.get("properties") or []
            if not props:
                continue
            pr = props[0]
            loc = pr.get("location") or {}
            lat, lon = loc.get("latitude"), loc.get("longitude")
            price = (re_.get("price") or {}).get("value")
            surf = (pr.get("surface") or "").replace("m²", "").strip()
            surf = surf.replace(".", "").replace(",", ".")
            addr = ", ".join(x for x in (loc.get("address"), loc.get("macrozone"),
                                         loc.get("city")) if x)
            floor = pr.get("floor")
            est, park, arred, dot = amenities(pr)
            txt = " ".join(str(x or "") for x in (re_.get("title"), pr.get("caption"),
                                                  pr.get("description"))).lower()
            typ = (re_.get("typology") or {}).get("name") or (pr.get("typology") or {}).get("name", "")
            det[str(re_["id"])] = {
                "fraz": subdividable(re_.get("title") or "", typ, pr.get("surface") or ""),
                "proprieta": "nuda" if "nuda pro" in txt else "",
                "title": re_.get("title") or "n/a",
                "price": price if price is not None else "n/a",
                "surface": surf or "n/a",
                "cond": pr.get("ga4Condition") or "n/d",
                "rooms": pr.get("rooms") or "",
                "bath": pr.get("bathrooms") or "",
                "floor": floor.get("value", "") if isinstance(floor, dict) else "",
                "sea": pr.get("seaDistanceValue") if pr.get("seaDistanceValue") is not None else "",
                "centre": haversine(centre, (lat, lon)) if lat and lon else "",
                "year": year_note(pr.get("description")),
                "address": addr or "n/a",
                "zona": loc.get("microzone") or loc.get("macrozone") or "n/d",
                "esterni": est, "park": park, "arred": arred, "dot": dot,
            }
        seen += len(d.get("results", []))
        if not d.get("results") or seen >= (d.get("totalAds") or 0):
            break
        page += 1
    return det


def research_urls(city, category, mx, mn_size, mn_rooms, mzona, quartiere, exclude_zona):
    """Return listing URLs from a filtered saved search, in result order,
    dropping any whose microzone is in exclude_zona (case-insensitive)."""
    idc, seg = CATEGORIES[category]
    region, prov, comune, keyurl = resolve_city(city)
    flt = []
    if mx:
        flt.append(("prezzoMassimo", str(mx)))
    if mn_size:
        flt.append(("superficieMinima", str(mn_size)))
    if mn_rooms:
        flt.append(("localiMinimo", str(mn_rooms)))
    flt += [(f"idMZona[{i}]", z) for i, z in enumerate(mzona)]
    flt += [(f"idQuartiere[{i}]", z) for i, z in enumerate(quartiere)]
    base = [("fkRegione", region), ("idProvincia", prov), ("idComune", comune),
            ("idContratto", "1"), ("idCategoria", idc), ("__lang", "it"),
            ("paramsCount", str(1 + len(flt))), ("path", f"/vendita-{seg}/{keyurl}/")] + flt
    excl = {z.strip().lower() for z in exclude_zona if z.strip()}
    urls, seen, page = [], 0, 1
    while True:
        d = api_json(f"{API}?{urllib.parse.urlencode(base + [('pag', str(page))])}")
        res = d.get("results", [])
        for it in res:
            re_ = it["realEstate"]
            props = re_.get("properties") or []
            mz = ((props[0].get("location") or {}).get("microzone") or "") if props else ""
            if mz.strip().lower() in excl:
                continue
            urls.append(f"https://www.immobiliare.it/annunci/{re_['id']}/")
        seen += len(res)
        if not res or seen >= (d.get("totalAds") or 0):
            return urls
        page += 1


def subdividable(title, typology, surface):
    """Best-effort flag for splitting into 2-3 units, from typology and size.
    Cannot see external buildings/dependance that live only in the (usually
    absent) description; those need a manual note."""
    t = f"{title} {typology}".lower()
    try:
        s = float(str(surface).replace("m²", "").strip().replace(".", "").replace(",", "."))
    except (ValueError, TypeError):
        s = 0
    if "plurifamiliare" in t:
        return "sì: plurifamiliare"
    if "bifamiliare" in t:
        return "sì: bifamiliare"
    house = any(k in t for k in ("villa", "terratetto", "casale", "palazzo",
                                 "rustico", "colonica", "indipendente", "cascina"))
    levels = "più livelli" in t or "piu livelli" in t
    if s >= 300 and (house or levels):
        return "forse: ≥300 m²"
    if s >= 180 and (house or levels):
        return "forse: ≥180 m²"
    if levels:
        return "forse: più livelli"
    return ""


def reno_params(tok, sid):
    """Read the Ristrutturazione parameter table; fall back to defaults if the
    tab or a row is missing. Returns per-m2 and fixed renovation costs."""
    p = {"ord_min": 500, "ord_max": 1000, "straord_m2": 2000,
         "roof": 20000, "geom_oneri": 25000, "agency": 0.04}
    try:
        rows = sheets(tok, sid, "/values/Ristrutturazione!A1:C20"
                      "?valueRenderOption=UNFORMATTED_VALUE").get("values", [])
    except urllib.error.HTTPError:
        return p
    for r in rows:
        if not r:
            continue
        label = str(r[0]).lower().strip()
        # the parameter table is above the CALCOLO section; stop before it so
        # the calculator's own rows are not mistaken for parameters
        if label.startswith("calcolo"):
            break
        def num(i):
            try:
                return float(r[i])
            except (IndexError, ValueError, TypeError):
                return None
        if label.startswith("manutenzione ordinaria"):
            p["ord_min"], p["ord_max"] = num(1) or p["ord_min"], num(2) or p["ord_max"]
        elif label.startswith("manutenzione straordinaria"):
            p["straord_m2"] = num(1) or p["straord_m2"]
        elif label.startswith("rifacimento tetto"):
            p["roof"] = num(1) or p["roof"]
        elif label.startswith("geometra + oneri"):
            p["geom_oneri"] = num(1) or p["geom_oneri"]
        elif label.startswith("commissione agenzia"):
            p["agency"] = num(1) or p["agency"]
    return p


def maintenance_level(cond, year):
    """Infer the maintenance scenario from condition and any renovation year.
    Returns one of: nessuna, ordinaria, straordinaria; a trailing '?' marks a
    guess made when the condition is unknown."""
    c = (cond or "").lower()
    y = (year or "").lower()
    # "da ristrutturare" must be tested before the renovated/good branches,
    # since it also contains the substring "ristruttur"
    if "da ristruttur" in c:
        return "straordinaria"
    # a recent build or renovation (>= 2010) means little or no work
    recent = bool(re.search(r"(ristrutturato|costr\.?)\s*(20[1-2]\d)", y))
    if recent or "nuov" in c or "in costruzione" in c or "ristruttur" in c or "ottimo" in c:
        return "nessuna"
    if "buono" in c or "abitabile" in c or "discreto" in c:
        return "ordinaria"
    return "ordinaria?"  # unknown condition: assume light work, flagged as a guess


def cost_estimate(cond, year, price, surface, p):
    """Return (level, works_cost, total_cost, total_per_m2). Blank tuple when
    price or surface is not numeric."""
    try:
        P, S = float(price), float(surface)
    except (ValueError, TypeError):
        return ("", "", "", "")
    level = maintenance_level(cond, year)
    base = level.rstrip("?")
    if base == "straordinaria":
        works = p["straord_m2"] * S + p["roof"] + p["geom_oneri"]
    elif base == "ordinaria":
        works = (p["ord_min"] + p["ord_max"]) / 2 * S  # midpoint of the range
    else:
        works = 0
    total = P + p["agency"] * P + works
    return (level, round(works), round(total), round(total / S) if S else "")


def listing_id(url):
    return str(url).rstrip("/").rsplit("/", 1)[-1]


def read_existing(tok, sid, tab):
    """Return (ordered urls, {url: {manual header: value}})."""
    rows = sheets(tok, sid, f"/values/{tab}!A1:AD2000"
                  "?valueRenderOption=UNFORMATTED_VALUE").get("values", [])
    if not rows:
        return [], {}
    header = [str(h).strip() for h in rows[0]]
    idx = {h: i for i, h in enumerate(header)}
    manual_idx = {h: idx[h] for h in MANUAL_HEADER if h in idx}
    urls, manual = [], {}
    for r in rows[1:]:
        if not r or "immobiliare.it/annunci" not in str(r[0]):
            continue
        u = r[0]
        urls.append(u)
        manual[u] = {h: (str(r[i]) if i < len(r) and r[i] not in (None, "") else "")
                     for h, i in manual_idx.items()}
    return urls, manual


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spreadsheet", required=True)
    ap.add_argument("--tab", required=True)
    ap.add_argument("--city", required=True)
    ap.add_argument("--category", choices=sorted(CATEGORIES), required=True)
    ap.add_argument("--account", default="")
    ap.add_argument("--centre", default="")
    ap.add_argument("--sort", action="store_true", help="sort by price per m2")
    ap.add_argument("--sort-by", choices=["price-m2", "total-m2"], default="",
                    help="sort key; total-m2 ranks by all-in cost per m2")
    ap.add_argument("--from-search", action="store_true",
                    help="source listings from a filtered search, not the tab")
    ap.add_argument("--max-price", type=int, default=0)
    ap.add_argument("--min-size", type=int, default=0)
    ap.add_argument("--min-rooms", type=int, default=0)
    ap.add_argument("--mzona", default="", help="comma-separated idMZona values")
    ap.add_argument("--quartiere", default="", help="comma-separated idQuartiere values")
    ap.add_argument("--exclude-zona", default="",
                    help="comma-separated microzone names to drop")
    args = ap.parse_args()

    centre = DEFAULT_CENTRE
    if args.centre:
        centre = tuple(float(x) for x in args.centre.split(","))
    sid = parse_spreadsheet_id(args.spreadsheet)
    tok = token(args.account)
    sh = sheet_id(tok, sid, args.tab)

    tab_urls, manual = read_existing(tok, sid, args.tab)
    if args.from_search:
        urls = research_urls(
            args.city, args.category, args.max_price, args.min_size, args.min_rooms,
            [z for z in args.mzona.split(",") if z],
            [z for z in args.quartiere.split(",") if z],
            args.exclude_zona.split(","))
    else:
        urls = tab_urls
    det = fetch_details(args.city, args.category, centre)
    params = reno_params(tok, sid)

    prop_i = HEADER.index("Proprietà")
    out, enriched = [HEADER], 0
    for u in urls:
        d = det.get(listing_id(u))
        man = manual.get(u, {})
        if d:
            enriched += 1
            level, works, total, totm2 = cost_estimate(
                d["cond"], d["year"], d["price"], d["surface"], params)
            base = [u, d["title"], d["price"], d["surface"], d["cond"], d["rooms"],
                    d["bath"], d["floor"], d["sea"], d["centre"], d["year"],
                    d["address"], d["zona"], d["esterni"], d["park"], d["arred"], d["dot"],
                    d["fraz"], level, works, total, totm2]
            cond = d["cond"]
        else:
            base = [u, man.get("_title", "n/a")] + ["n/d"] * 20
            cond = ""
        lavori = man.get("Lavori", "")
        if not lavori and "ristruttur" in cond.lower():
            lavori = "ristrutturazione"
        # bare ownership: auto from the listing text or from a manual note
        proprieta = man.get("Proprietà", "")
        if not proprieta and ((d and d.get("proprieta") == "nuda")
                              or "nuda pro" in man.get("Note", "").lower()):
            proprieta = "nuda"
        manvals = [proprieta, lavori, man.get("Adatto affitto", ""),
                   man.get("Stato", ""), man.get("Note", "")]
        out.append(base + manvals + [PM2_FORMULA])

    sort_by = args.sort_by or ("price-m2" if args.sort else "")
    if sort_by:
        totm2_i = HEADER.index("Costo tot./m2")
        def key(row):
            try:
                if sort_by == "total-m2":
                    return float(row[totm2_i])
                return float(row[2]) / float(row[3])
            except (ValueError, ZeroDivisionError, TypeError):
                return float("inf")  # unknowns sort last
        out = [HEADER] + sorted(out[1:], key=key)

    # highlight rows by final position: bare ownership (red) wins over auction (amber)
    auction = [i + 1 for i, r in enumerate(out) if i and "asta" in str(r[1]).lower()]
    nuda = [i + 1 for i, r in enumerate(out) if i and str(r[prop_i]).strip().lower() == "nuda"]
    n = len(out)
    sheets(tok, sid, f"/values/{args.tab}!A1:AD2000:clear", {}, "POST")
    sheets(tok, sid, f"/values/{args.tab}!A1?valueInputOption=USER_ENTERED",
           {"values": out}, "PUT")

    def c(r, g, b):
        return {"red": r / 255, "green": g / 255, "blue": b / 255}
    pm2_col = NCOL - 1
    meta = sheets(tok, sid, "?fields=sheets(properties(sheetId),bandedRanges(bandedRangeId))")
    bands = [b["bandedRangeId"] for s in meta["sheets"]
             if s["properties"]["sheetId"] == sh for b in s.get("bandedRanges", [])]
    reqs = [{"deleteBanding": {"bandedRangeId": b}} for b in bands]
    reqs += [
        {"updateSheetProperties": {"properties": {"sheetId": sh,
            "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
        {"repeatCell": {"range": {"sheetId": sh, "startRowIndex": 0, "endRowIndex": n,
            "startColumnIndex": 0, "endColumnIndex": NCOL},
            "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment,wrapStrategy)"}},
        {"repeatCell": {"range": {"sheetId": sh, "startRowIndex": 0, "endRowIndex": 1,
            "startColumnIndex": 0, "endColumnIndex": NCOL},
            "cell": {"userEnteredFormat": {"backgroundColor": c(31, 41, 55),
                "textFormat": {"bold": True, "foregroundColor": c(255, 255, 255)}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        {"addBanding": {"bandedRange": {"range": {"sheetId": sh, "startRowIndex": 1,
            "endRowIndex": n, "startColumnIndex": 0, "endColumnIndex": NCOL},
            "rowProperties": {"firstBandColor": c(255, 255, 255),
                "secondBandColor": c(238, 242, 247)}}}},
        {"setBasicFilter": {"filter": {"range": {"sheetId": sh, "startRowIndex": 0,
            "startColumnIndex": 0, "endColumnIndex": NCOL}}}},
    ]
    # euro format on price, the two cost columns, cost/m2, and price/m2
    euro_cols = [2, HEADER.index("Costo lavori (EUR)"), HEADER.index("Costo totale (EUR)"),
                 HEADER.index("Costo tot./m2"), pm2_col]
    for cidx in euro_cols:
        reqs.append({"repeatCell": {"range": {"sheetId": sh, "startRowIndex": 1,
            "startColumnIndex": cidx, "endColumnIndex": cidx + 1},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY",
                "pattern": "€ #,##0"}}}, "fields": "userEnteredFormat.numberFormat"}})
    for r in auction:
        reqs.append({"repeatCell": {"range": {"sheetId": sh, "startRowIndex": r - 1,
            "endRowIndex": r, "startColumnIndex": 0, "endColumnIndex": NCOL},
            "cell": {"userEnteredFormat": {"backgroundColor": c(255, 217, 102)}},
            "fields": "userEnteredFormat.backgroundColor"}})
    for r in nuda:  # after auction so bare-ownership red wins
        reqs.append({"repeatCell": {"range": {"sheetId": sh, "startRowIndex": r - 1,
            "endRowIndex": r, "startColumnIndex": 0, "endColumnIndex": NCOL},
            "cell": {"userEnteredFormat": {"backgroundColor": c(244, 199, 195)}},
            "fields": "userEnteredFormat.backgroundColor"}})
    sheets(tok, sid, ":batchUpdate", {"requests": reqs}, "POST")
    print(f"{args.tab}: {len(urls)} listings, {enriched} enriched, "
          f"{len(urls) - enriched} kept from tab, {len(auction)} auction, "
          f"{len(nuda)} nuda proprietà highlighted")


if __name__ == "__main__":
    main()
