#!/usr/bin/env python3
"""Build a review shortlist tab from an enriched listing tab.

Reads an already-enriched source tab (as produced by refresh.py), drops the
hard exclusions (bare ownership and auctions), keeps everything else including
heavy-work listings, and writes them to a destination tab sorted by all-in cost
per m2 (Costo tot./m2). Heavy work is acceptable when the cost per m2 is low, so
straordinaria listings are kept and only flagged, not removed.

Cues on the destination tab:
  - sorted cheapest all-in first (Costo tot./m2)
  - Manutenzione cell tinted orange where the listing needs straordinaria work
  - Esterni cell tinted green where the listing has outdoor space (a plus)

Usage:
    python3 review.py --spreadsheet <ID|URL> --source Appartamenti-ricerca \
        --dest Appartamenti-da-valutare --account you@example.com
"""

import argparse
import json
import subprocess
import urllib.request

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


def parse_spreadsheet_id(s):
    if "/d/" in s:
        return s.split("/d/", 1)[1].split("/")[0].split("?")[0].split("#")[0]
    return s


def ensure_tab(tok, sid, title):
    for s in sheets(tok, sid, "?fields=sheets.properties")["sheets"]:
        if s["properties"]["title"] == title:
            return s["properties"]["sheetId"]
    rep = sheets(tok, sid, ":batchUpdate",
                 {"requests": [{"addSheet": {"properties": {"title": title}}}]}, "POST")
    return rep["replies"][0]["addSheet"]["properties"]["sheetId"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spreadsheet", required=True)
    ap.add_argument("--source", default="Appartamenti-ricerca")
    ap.add_argument("--dest", default="Appartamenti-da-valutare")
    ap.add_argument("--account", default="")
    args = ap.parse_args()

    sid = parse_spreadsheet_id(args.spreadsheet)
    tok = token(args.account)

    v = sheets(tok, sid, f"/values/{args.source}!A1:AA2000"
               "?valueRenderOption=UNFORMATTED_VALUE").get("values", [])
    if not v:
        raise SystemExit(f"source tab {args.source} is empty")
    H = v[0]
    idx = {h: i for i, h in enumerate(H)}
    PROP, MAN, EST, TIT = idx["Proprietà"], idx["Manutenzione"], idx["Esterni"], idx["Titolo"]
    TM, PM2 = idx["Costo tot./m2"], len(H) - 1

    def val(r, i):
        return r[i] if i < len(r) else ""

    kept, dropped = [], {"nuda": 0, "auction": 0}
    for r in v[1:]:
        r = list(r) + [""] * (len(H) - len(r))
        if str(val(r, PROP)).strip().lower() == "nuda":
            dropped["nuda"] += 1
            continue
        if "asta" in str(val(r, TIT)).lower():
            dropped["auction"] += 1
            continue
        r[PM2] = PM2_FORMULA
        kept.append(r)

    def totm2(r):
        try:
            return float(r[TM])
        except (ValueError, TypeError):
            return float("inf")  # unknown cost sorts last
    kept.sort(key=totm2)

    sh = ensure_tab(tok, sid, args.dest)
    sheets(tok, sid, f"/values/{args.dest}!A1:AA4000:clear", {}, "POST")
    sheets(tok, sid, f"/values/{args.dest}!A1?valueInputOption=USER_ENTERED",
           {"values": [H] + kept}, "PUT")

    n, NC = len(kept) + 1, len(H)

    def c(r, g, b):
        return {"red": r / 255, "green": g / 255, "blue": b / 255}
    euro = [idx["Prezzo (EUR)"], idx["Costo lavori (EUR)"], idx["Costo totale (EUR)"],
            idx["Costo tot./m2"], PM2]
    meta = sheets(tok, sid, "?fields=sheets(properties(sheetId),bandedRanges(bandedRangeId))")
    bands = [b["bandedRangeId"] for s in meta["sheets"]
             if s["properties"]["sheetId"] == sh for b in s.get("bandedRanges", [])]
    reqs = [{"deleteBanding": {"bandedRangeId": b}} for b in bands]
    reqs += [
        {"updateSheetProperties": {"properties": {"sheetId": sh,
            "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
        {"repeatCell": {"range": {"sheetId": sh, "startRowIndex": 0, "endRowIndex": n,
            "startColumnIndex": 0, "endColumnIndex": NC}, "cell": {"userEnteredFormat":
            {"horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat(horizontalAlignment,verticalAlignment,wrapStrategy)"}},
        {"repeatCell": {"range": {"sheetId": sh, "startRowIndex": 0, "endRowIndex": 1,
            "startColumnIndex": 0, "endColumnIndex": NC}, "cell": {"userEnteredFormat":
            {"backgroundColor": c(31, 41, 55), "textFormat": {"bold": True,
             "foregroundColor": c(255, 255, 255)}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        {"addBanding": {"bandedRange": {"range": {"sheetId": sh, "startRowIndex": 1,
            "endRowIndex": n, "startColumnIndex": 0, "endColumnIndex": NC},
            "rowProperties": {"firstBandColor": c(255, 255, 255),
             "secondBandColor": c(238, 242, 247)}}}},
        {"setBasicFilter": {"filter": {"range": {"sheetId": sh, "startRowIndex": 0,
            "startColumnIndex": 0, "endColumnIndex": NC}}}},
    ]
    for ci in euro:
        reqs.append({"repeatCell": {"range": {"sheetId": sh, "startRowIndex": 1,
            "startColumnIndex": ci, "endColumnIndex": ci + 1}, "cell": {"userEnteredFormat":
            {"numberFormat": {"type": "CURRENCY", "pattern": "€ #,##0"}}},
            "fields": "userEnteredFormat.numberFormat"}})
    for i, r in enumerate(kept, 2):
        if str(val(r, EST)).strip().lower() not in ("", "no"):
            reqs.append({"repeatCell": {"range": {"sheetId": sh, "startRowIndex": i - 1,
                "endRowIndex": i, "startColumnIndex": EST, "endColumnIndex": EST + 1},
                "cell": {"userEnteredFormat": {"backgroundColor": c(212, 237, 218)}},
                "fields": "userEnteredFormat.backgroundColor"}})
        if str(val(r, MAN)).strip().lower().startswith("straordinaria"):
            reqs.append({"repeatCell": {"range": {"sheetId": sh, "startRowIndex": i - 1,
                "endRowIndex": i, "startColumnIndex": MAN, "endColumnIndex": MAN + 1},
                "cell": {"userEnteredFormat": {"backgroundColor": c(255, 224, 178)}},
                "fields": "userEnteredFormat.backgroundColor"}})
    sheets(tok, sid, ":batchUpdate", {"requests": reqs}, "POST")
    print(f"{args.dest}: {len(kept)} listings from {args.source}, dropped {dropped}")


if __name__ == "__main__":
    main()
