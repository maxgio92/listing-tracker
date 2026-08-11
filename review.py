#!/usr/bin/env python3
"""Split an enriched listing tab into maintenance buckets for review.

From an enriched source tab (produced by refresh.py), subtract listings already
chosen (--preferiti) or rejected (--ignore-tab), drop the hard exclusions (bare
ownership, auctions), then split the rest by maintenance level into three tabs:

  <prefix>-pronti          ready to use (Manutenzione = nessuna)
  <prefix>-ordinaria       light works
  <prefix>-straordinaria   heavy works

Costs differ by bucket (ready = price + agency; works add on top), so each tab
is sorted by all-in cost per m2 and the good-deal mark (green Costo tot./m2) is
the cheapest quartile within that bucket. Layout and styling are shared with
refresh.py. Review decisions in the Stato column (and Note, Adatto affitto) are
preserved by URL across regeneration; marking "scartare" moves the listing to
the ignore tab.

Usage:
    python3 review.py --spreadsheet <ID|URL> --source Appartamenti-ricerca \
        --prefix Appartamenti --account you@example.com
"""

import argparse
import urllib.request

import refresh  # shared layout, formatting, and helpers

BUCKETS = ("pronti", "ordinaria", "straordinaria")
STICKY = ("Stato", "Note", "Adatto affitto")
GOOD_DEAL = refresh._rgb(76, 175, 80)

sheets = refresh.sheets


def bucket_of(man):
    m = str(man).strip().lower()
    if m == "nessuna":
        return "pronti"
    if m.startswith("straordinaria"):
        return "straordinaria"
    return "ordinaria"  # ordinaria, ordinaria?, unknown -> light-works bucket


def tab_ids(tok, sid):
    return {s["properties"]["title"]: s["properties"]["sheetId"]
            for s in sheets(tok, sid, "?fields=sheets.properties")["sheets"]}


def sticky_map(tok, sid, tab):
    try:
        rows = sheets(tok, sid, f"/values/{tab}!A1:AZ4000"
                      "?valueRenderOption=UNFORMATTED_VALUE").get("values", [])
    except urllib.error.HTTPError:
        return {}
    if not rows:
        return {}
    hidx = {h: i for i, h in enumerate(rows[0])}
    cols = {f: hidx[f] for f in STICKY if f in hidx}
    out = {}
    for r in rows[1:]:
        if not r or "immobiliare.it/annunci" not in str(r[0]):
            continue
        vals = {f: (str(r[i]) if i < len(r) and r[i] not in (None, "") else "")
                for f, i in cols.items()}
        if any(vals.values()):
            out[str(r[0]).rstrip("/")] = vals
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spreadsheet", required=True)
    ap.add_argument("--source", default="Appartamenti-ricerca")
    ap.add_argument("--prefix", default="Appartamenti")
    ap.add_argument("--preferiti", default="Appartamenti-preferiti")
    ap.add_argument("--ignore-tab", default="Ignorati")
    ap.add_argument("--retire", default="Appartamenti-da-valutare")
    ap.add_argument("--account", default="")
    args = ap.parse_args()

    sid = refresh.parse_spreadsheet_id(args.spreadsheet)
    tok = refresh.token(args.account)
    sep = refresh.arg_sep(tok, sid)

    def urls_in(tab):
        try:
            rows = sheets(tok, sid, f"/values/{tab}!A2:A4000").get("values", [])
        except urllib.error.HTTPError:
            return set()
        return {r[0].rstrip("/") for r in rows if r and "immobiliare.it/annunci" in str(r[0])}

    pref_urls = urls_in(args.preferiti)
    ignore_urls = urls_in(args.ignore_tab)

    preserved = {}
    for b in BUCKETS:
        preserved.update(sticky_map(tok, sid, f"{args.prefix}-{b}"))

    scartare = {u for u, v in preserved.items()
                if str(v.get("Stato", "")).strip().lower() == "scartare"}
    new_rejects = sorted(scartare - ignore_urls)
    if new_rejects:
        sheets(tok, sid, f"/values/{args.ignore_tab}!A1:append?valueInputOption=USER_ENTERED",
               {"values": [[u + "/", "scartato in review"] for u in new_rejects]}, "POST")
    subtract = pref_urls | ignore_urls | scartare

    v = sheets(tok, sid, f"/values/{args.source}!A1:AZ2000"
               "?valueRenderOption=UNFORMATTED_VALUE").get("values", [])
    if not v:
        raise SystemExit(f"source tab {args.source} is empty")
    H = v[0]
    idx = {h: i for i, h in enumerate(H)}
    URL, TIT, PROP, MAN, TM = (idx["URL"], idx["Titolo"], idx["Proprietà"],
                               idx["Manutenzione"], idx["Costo tot./m2"])
    sticky_idx = {f: idx[f] for f in STICKY if f in idx}

    def val(r, i):
        return r[i] if i < len(r) else ""

    parts = {b: [] for b in BUCKETS}
    dropped = {"nuda": 0, "auction": 0, "preferiti/ignorati": 0}
    for r in v[1:]:
        r = list(r) + [""] * (len(H) - len(r))
        u = str(r[URL]).rstrip("/")
        if u in subtract:
            dropped["preferiti/ignorati"] += 1
            continue
        if str(val(r, PROP)).strip().lower() == "nuda":
            dropped["nuda"] += 1
            continue
        # title cell is a hyperlink formula; its plain value carries "asta"
        if "asta" in str(val(r, TIT)).lower():
            dropped["auction"] += 1
            continue
        # rebuild the clickable title and re-apply preserved review decisions
        r[TIT] = refresh.hyperlink(r[URL], r[TIT], sep)
        pr = preserved.get(u, {})
        for f, i in sticky_idx.items():
            if pr.get(f):
                r[i] = pr[f]
        parts[bucket_of(val(r, MAN))].append(r)

    def totm2(r):
        try:
            return float(r[TM])
        except (ValueError, TypeError):
            return float("inf")

    ids = tab_ids(tok, sid)
    summary = []
    for b in BUCKETS:
        rows = sorted(parts[b], key=totm2)
        tab = f"{args.prefix}-{b}"
        sh = ids[tab] if tab in ids else sheets(tok, sid, ":batchUpdate",
            {"requests": [{"addSheet": {"properties": {"title": tab}}}]},
            "POST")["replies"][0]["addSheet"]["properties"]["sheetId"]
        sheets(tok, sid, f"/values/{tab}!A1:AZ4000:clear", {}, "POST")
        sheets(tok, sid, f"/values/{tab}!A1?valueInputOption=USER_ENTERED",
               {"values": [H] + rows}, "PUT")
        tmv = sorted(v for v in (totm2(r) for r in rows) if v != float("inf"))
        cutoff = tmv[int(0.25 * len(tmv))] if tmv else 0
        extra = [(k + 2, TM, GOOD_DEAL) for k, r in enumerate(rows) if totm2(r) <= cutoff]
        refresh.format_listing_sheet(tok, sid, sh, H, len(rows) + 1, extra_rows=extra)
        summary.append(f"{b}={len(rows)} ({len(extra)} deals)")

    ids = tab_ids(tok, sid)
    if args.retire and args.retire in ids:
        sheets(tok, sid, ":batchUpdate",
               {"requests": [{"deleteSheet": {"sheetId": ids[args.retire]}}]}, "POST")
    moved = f", moved {len(new_rejects)} scartare -> {args.ignore_tab}" if new_rejects else ""
    print(f"{args.source} -> " + ", ".join(summary) + f"; dropped {dropped}{moved}")


if __name__ == "__main__":
    main()
