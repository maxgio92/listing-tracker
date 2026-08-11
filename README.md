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
