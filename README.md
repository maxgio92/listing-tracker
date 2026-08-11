# listing-tracker

Tracks immobiliare.it real-estate listings in a Google Sheet. Go, stdlib only.

One subcommand:

- **sync**: queries a listing platform's search API for a city and upserts the
  results into a sheet tab, keyed by listing URL. Unknown URLs are appended;
  rows whose title, price, surface, or address changed are updated in place.
  `--platform` selects the platform (default `immobiliare`, currently the only
  one); an unknown value fails with the list of available platforms.

## Setup

1. `gcloud auth login --enable-gdrive-access` with the Google account that
   owns (or can edit) the target spreadsheet.
2. `go build -o listing-tracker .`

## Usage

```sh
# sync: commercial listings in a city, max 200k EUR, min 60 m2 (defaults)
./listing-tracker sync --city Rimini --spreadsheet <ID or full Sheets URL>

# without writing
./listing-tracker sync --city Rimini --spreadsheet <ID> --dry-run

# residential, custom filters, specific tab and account
./listing-tracker sync --city Bologna --category residential \
  --max-price 150000 --min-size 80 \
  --spreadsheet <ID> --tab Listings --account you@example.com
```

`--spreadsheet` accepts a bare ID or a full docs.google.com URL. `--account`
selects the gcloud account when the sheet owner is not the active login.
`--ignore-tab` (default "Ignored") names a tab whose column A lists URLs that
sync must never add or update; move a row's URL there to reject a listing
permanently. A missing tab means nothing is ignored.
Column F of the sheet gets a price-per-m2 formula on every append and update.

Cron example (daily at 9:00):

```
0 9 * * * $HOME/listing-tracker/listing-tracker sync --city <city> --spreadsheet <ID> >> watch.log 2>&1
```

## Tests

```sh
go test ./...                 # unit tests, offline
E2E_CITY=<city> E2E_SPREADSHEET_ID=<ID> go test -tags e2e ./...
```

The e2e Sheets test creates and deletes a temporary tab; existing tabs are
never touched. E2E tests skip when their environment variables are unset.

## Adding a provider

Platforms implement the `provider` interface in `main.go`:

```go
type provider interface {
	search(city string, f filters) ([]row, error)
}
```

Add a new file (like `immobiliare.go`) with a type whose `search` method
resolves the city on the platform, applies the `filters`, and returns `row`
values, then register it in the `providers` map in `main.go`. Nothing else
changes: `--platform <name>` picks it up.

## Caveats

The immobiliare.it search API is unofficial and sits behind bot protection.
It works unauthenticated today, but it can change or be blocked without
notice; the sync runs will start failing in the log if that happens.

## Renovation costs

[RENOVATION.md](RENOVATION.md) is a companion guide for estimating the full
cost of a listing (purchase, agency, works) when the goal is converting a
commercial property into an apartment in Italy.

## refresh.py: curated tabs

`sync` maintains flat raw tabs (one row per listing, columns A-F). `refresh.py`
decorates a curated tab in place, keeping its listing set and adding detail
columns (condition, rooms, bathrooms, floor, sea/centre distance, a best-effort
construction note, and zona), formatted and optionally sorted by price/m2:

```sh
python3 refresh.py --spreadsheet <ID|URL> --tab Commerciali \
    --city Fano --category commercial --account you@example.com --sort
```

Detail comes from the search API matched by listing id. Auto columns
(condition, distances, amenities: Esterni, Parcheggio, Arredato, Dotazioni)
are recomputed every run; manual columns (Proprietà, Lavori, Adatto affitto,
Stato, Note) are sticky, matched by URL, so human input survives a refresh.
Distances are straight-line to the city centre (override with --centre
"lat,lon").

Bare-ownership listings (nuda proprietà, detected from the listing text or a
manual Note) get Proprietà = "nuda" and a red row, since the property cannot
be used until the occupant leaves. Auction rows are amber; the red wins.

### Search-sourced refresh

`refresh.py --from-search` sources the listing set from a filtered search
instead of the tab's column A, then enriches and preserves manual columns by
URL. This drives the daily residential feed (a saved immobiliare search):

```sh
python3 refresh.py --from-search --spreadsheet <ID> --tab Appartamenti-ricerca \
    --city Fano --category residential --account you@example.com \
    --max-price 310000 --min-size 70 --min-rooms 3 \
    --mzona 10775,10776,... --quartiere 14240,... \
    --exclude-zona "Centro Storico" --sort
```

### Total cost of ownership

refresh.py adds Manutenzione, Costo lavori (EUR), Costo totale (EUR), and
Costo tot./m2, using the parameters read live from the Ristrutturazione tab
(per-m2 ordinary/extraordinary rates, roof, geometra + oneri, agency %). The
maintenance level is inferred from the listing condition and any renovation
year: "Da ristrutturare" -> straordinaria; new or recently renovated (>=2010)
or "Ottimo" -> nessuna; "Buono / Abitabile" -> ordinaria; unknown condition ->
ordinaria with a "?" marker. Costo totale = price + agency + works, so a
renovated home at a higher EUR/m2 can be cheaper all-in than a cheaper one
needing straordinaria work. Edit the parameters in the Ristrutturazione tab
and re-run to recompute. Use `--sort-by total-m2` to rank by all-in
cost per m2 instead of sticker price (`--sort-by price-m2`, or `--sort`).

### review.py: review buckets

`review.py` splits the enriched source tab into maintenance buckets, after
subtracting listings already chosen (--preferiti) or rejected (--ignore-tab)
and dropping nuda proprietà and auctions:

- `<prefix>-pronti` (Manutenzione = nessuna)
- `<prefix>-ordinaria` (light works)
- `<prefix>-straordinaria` (heavy works)

Costs differ by bucket, so each tab is sorted by all-in cost per m2 and the
good-deal mark (green Costo tot./m2) is the cheapest quartile within that
bucket. Outdoor space is tinted green. The daily cron runs it after the
residential refresh.

```sh
python3 review.py --spreadsheet <ID> --source Appartamenti-ricerca \
    --prefix Appartamenti --account you@example.com
```

### Frazionabile (subdivision potential)

refresh.py adds a Frazionabile column flagging structural potential to split
into 2-3 units from typology and size: "sì" for plurifamiliare/bifamiliare,
"forse" for large (>=180 or >=300 m2) houses or "su più livelli". External
buildings or a dependance that live only in the (usually absent) description
are not detected; note those manually.

review.py also marks value on the shortlist: the Costo tot./m2 cell is green
for good deals (all-in cost per m2 in the cheapest quartile), and a "Pronto
(<250k, no lavori)" column flags ready-to-use listings (no maintenance and
price under 250k).

review.py subtracts listings already chosen (--preferiti, default
Appartamenti-preferiti) or rejected (--ignore-tab, default Ignorati), so the
review tab is ricerca minus preferiti minus to-exclude, minus the nuda/auction
hard exclusions.

### Market & rental insights

refresh.py adds analytic columns per listing:
- Zona turistica: tier (mare/centro/interno) driving rental assumptions.
- Prezzo vs zona %: price/m2 versus the median of the same microzone
  (negative = cheaper than its zone).
- Rendita lorda % and Payback anni: gross yield and payback, from the
  Affitto-parametri tab (nightly rate and occupancy per tier). Rental income
  scales with size (~1 unit per 75 m2, capped at 4), so large or subdividable
  properties are not penalised as a single unit.
- Var. prezzo: price change since the previous refresh (motivated-seller
  signal).
- Primo avvist. / Novità: first-seen date and a flag for listings seen in the
  last 7 days.

Rental rates are assumptions; edit Affitto-parametri and re-run to recompute.

Review decisions go in the Stato column of the bucket tabs (dropdown: da
vedere, da contattare, visita fissata, visitato, interessante, scartare).
review.py preserves Stato, Note, and Adatto affitto by URL across the daily
regeneration, so decisions are not lost when the buckets rebuild.

Marking a listing "scartare" in the Stato dropdown moves its URL to the
Ignorati tab on the next review run (with reason "scartato in review"), so it
drops out of the buckets permanently, the same as a manual reject.

### Readability

The listing tabs share one readable layout (in refresh.py's HEADER and
format_listing_sheet, reused by review.py): a lean left block (clickable
Titolo, Zona, Prezzo, Superficie, Costo tot./m2, Prezzo/m2, Manutenzione,
Stato) with the raw URL hidden and the title as a HYPERLINK; a collapsible
detail group for the rest; green→red color scales on Costo tot./m2, Prezzo/m2,
Payback (low = good) and Rendita lorda % (high = good); the first columns and
header frozen; euro/percent/km number formats; and a Stato dropdown. Tabs are
colour-coded (green favourites, blue review buckets, grey feeds, red Ignorati,
amber reference) and a Legenda tab documents every colour and column.
