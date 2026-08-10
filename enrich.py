#!/usr/bin/env python3
"""Decorate a curated listing tab in place with detail columns.

Reads the URLs already in the target tab (column A) and rewrites the tab in the
14-column curated layout, filling condition, rooms, bathrooms, floor, sea and
centre distance, a best-effort construction/renovation note, address and zona.
The set of listings and their order are preserved; only columns are (re)built.

Details come from immobiliare.it's search API for the given city and category,
matched by listing id. Listings absent from the current search (delisted) keep
whatever title/price/surface/address the tab already held and get "n/d" for the
rest. Nothing is written to any immobiliare account.

Usage:
    python3 enrich.py --spreadsheet <ID|URL> --tab Commerciali \
        --city Fano --category commercial --account you@example.com [--sort]

Curated layout (columns A..N):
    URL, Titolo, Prezzo (EUR), Superficie (m2), Condizione, Locali, Bagni,
    Piano, Dist. mare (m), Dist. centro (m), Anno / ristrutt.,
    Indirizzo / Zona, Zona, Prezzo/m2
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
UA = "Mozilla/5.0 (X11; Linux x86_64) listing-tracker-enrich/1.0"
CATEGORIES = {"commercial": ("26", "negozi"), "residential": ("1", "case")}
# Fano city centre (Piazza XX Settembre); override with --centre "lat,lon".
DEFAULT_CENTRE = (43.8436, 13.0170)
HEADER = [
    "URL", "Titolo", "Prezzo (EUR)", "Superficie (m2)", "Condizione", "Locali",
    "Bagni", "Piano", "Dist. mare (m)", "Dist. centro (m)", "Anno / ristrutt.",
    "Indirizzo / Zona", "Zona", "Prezzo/m2",
]
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


def fetch_details(city, category, centre):
    idc, seg = CATEGORIES[category]
    region, prov, comune, keyurl = resolve_city(city)
    base = {"fkRegione": region, "idProvincia": prov, "idComune": comune,
            "idContratto": "1", "idCategoria": idc, "__lang": "it",
            "paramsCount": "0", "path": f"/vendita-{seg}/{keyurl}/"}
    det = {}
    page, seen, total = 1, 0, None
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
            det[str(re_["id"])] = {
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
            }
        seen += len(d.get("results", []))
        total = d.get("totalAds")
        if not d.get("results") or seen >= (total or 0):
            break
        page += 1
    return det


def listing_id(url):
    return str(url).rstrip("/").rsplit("/", 1)[-1]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spreadsheet", required=True)
    ap.add_argument("--tab", required=True)
    ap.add_argument("--city", required=True)
    ap.add_argument("--category", choices=sorted(CATEGORIES), required=True)
    ap.add_argument("--account", default="")
    ap.add_argument("--centre", default="", help='"lat,lon" city centre override')
    ap.add_argument("--sort", action="store_true", help="sort by price per m2")
    args = ap.parse_args()

    centre = DEFAULT_CENTRE
    if args.centre:
        centre = tuple(float(x) for x in args.centre.split(","))
    sid = parse_spreadsheet_id(args.spreadsheet)
    tok = token(args.account)
    sh = sheet_id(tok, sid, args.tab)

    rows = sheets(tok, sid, f"/values/{args.tab}!A1:N1000"
                  "?valueRenderOption=UNFORMATTED_VALUE").get("values", [])
    # existing base values keyed by id, for listings the search no longer returns
    fallback = {}
    urls = []
    for i, r in enumerate(rows):
        if i == 0 and r and str(r[0]).strip().upper() == "URL":
            continue
        if not r or "immobiliare.it/annunci" not in str(r[0]):
            continue
        r = list(r) + [""] * (14 - len(r))
        urls.append(r[0])
        fallback[listing_id(r[0])] = {
            "title": r[1], "price": r[2], "surface": r[3],
            "address": r[11] if r[11] else "n/a",
        }

    det = fetch_details(args.city, args.category, centre)
    out, enriched, auction = [HEADER], 0, []
    for u in urls:
        lid = listing_id(u)
        d = det.get(lid)
        if d:
            enriched += 1
        else:
            b = fallback.get(lid, {})
            d = {"title": b.get("title", "n/a"), "price": b.get("price", "n/a"),
                 "surface": b.get("surface", "n/a"), "cond": "n/d", "rooms": "",
                 "bath": "", "floor": "", "sea": "", "centre": "", "year": "n/d",
                 "address": b.get("address", "n/a"), "zona": "n/d"}
        if "asta" in str(d["title"]).lower():
            auction.append(len(out) + 1)
        out.append([u, d["title"], d["price"], d["surface"], d["cond"], d["rooms"],
                    d["bath"], d["floor"], d["sea"], d["centre"], d["year"],
                    d["address"], d["zona"], PM2_FORMULA])

    if args.sort:
        body = out[1:]
        def pm2(row):
            try:
                return float(row[2]) / float(row[3])
            except (ValueError, ZeroDivisionError, TypeError):
                return float("inf")
        body.sort(key=pm2)
        out = [HEADER] + body

    n = len(out)
    sheets(tok, sid, f"/values/{args.tab}!A1:Z1000:clear", {}, "POST")
    sheets(tok, sid, f"/values/{args.tab}!A1?valueInputOption=USER_ENTERED",
           {"values": out}, "PUT")

    def c(r, g, b):
        return {"red": r / 255, "green": g / 255, "blue": b / 255}

    meta = sheets(tok, sid, "?fields=sheets(properties(sheetId),bandedRanges(bandedRangeId))")
    bands = [b["bandedRangeId"] for s in meta["sheets"]
             if s["properties"]["sheetId"] == sh for b in s.get("bandedRanges", [])]
    reqs = [{"deleteBanding": {"bandedRangeId": b}} for b in bands]
    reqs += [
        {"updateSheetProperties": {"properties": {"sheetId": sh,
            "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount"}},
        {"repeatCell": {"range": {"sheetId": sh, "startRowIndex": 0, "endRowIndex": n,
            "startColumnIndex": 0, "endColumnIndex": 14},
            "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment,wrapStrategy)"}},
        {"repeatCell": {"range": {"sheetId": sh, "startRowIndex": 0, "endRowIndex": 1,
            "startColumnIndex": 0, "endColumnIndex": 14},
            "cell": {"userEnteredFormat": {"backgroundColor": c(31, 41, 55),
                "textFormat": {"bold": True, "foregroundColor": c(255, 255, 255)}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        {"addBanding": {"bandedRange": {"range": {"sheetId": sh, "startRowIndex": 1,
            "endRowIndex": n, "startColumnIndex": 0, "endColumnIndex": 14},
            "rowProperties": {"firstBandColor": c(255, 255, 255),
                "secondBandColor": c(238, 242, 247)}}}},
        {"repeatCell": {"range": {"sheetId": sh, "startRowIndex": 1,
            "startColumnIndex": 2, "endColumnIndex": 3},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY",
                "pattern": "€ #,##0"}}}, "fields": "userEnteredFormat.numberFormat"}},
        {"repeatCell": {"range": {"sheetId": sh, "startRowIndex": 1,
            "startColumnIndex": 13, "endColumnIndex": 14},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "CURRENCY",
                "pattern": "€ #,##0"}}}, "fields": "userEnteredFormat.numberFormat"}},
        {"setBasicFilter": {"filter": {"range": {"sheetId": sh, "startRowIndex": 0,
            "startColumnIndex": 0, "endColumnIndex": 14}}}},
    ]
    for r in auction:
        reqs.append({"repeatCell": {"range": {"sheetId": sh, "startRowIndex": r - 1,
            "endRowIndex": r, "startColumnIndex": 0, "endColumnIndex": 14},
            "cell": {"userEnteredFormat": {"backgroundColor": c(255, 217, 102)}},
            "fields": "userEnteredFormat.backgroundColor"}})
    sheets(tok, sid, ":batchUpdate", {"requests": reqs}, "POST")
    print(f"{args.tab}: {len(urls)} listings, {enriched} enriched, "
          f"{len(urls) - enriched} kept from tab, {len(auction)} auction highlighted")


if __name__ == "__main__":
    main()
