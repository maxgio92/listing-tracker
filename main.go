// Track real-estate listings in a Google Sheet.
//
// The sync subcommand searches a listing platform (--platform, default
// immobiliare) and upserts listings: URLs not
// in column A are appended, rows whose title, price, surface, or address
// changed are updated in place. "watch" remains as a deprecated alias.
//
// City, price, size, building type, spreadsheet, and tab are flags; there is
// no default city or spreadsheet.
//
// State lives in the Google Sheet itself: a listing is "new" if its URL is not
// already in column A. Designed for cron; prints one summary line per run.
//
// Requires: gcloud logged in with Drive scope on the account that owns the sheet
// (gcloud auth login --enable-gdrive-access). Use --account when that is not
// the active gcloud account.
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"sort"
	"strings"
	"time"
)

const (
	defaultTab = "Listings"

	userAgent = "Mozilla/5.0 (X11; Linux x86_64) listing-tracker/1.0"
)

// filters narrows a listing search; zero values disable the numeric bounds.
type filters struct {
	category                             string
	minPrice, maxPrice, minSize, maxSize int
}

// provider searches one listing platform and returns the matching rows.
type provider interface {
	search(city string, f filters) ([]row, error)
}

var providers = map[string]provider{
	"immobiliare": immobiliareProvider{},
}

// providerNames returns the registered platform names, sorted.
func providerNames() []string {
	names := make([]string, 0, len(providers))
	for name := range providers {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

var httpClient = &http.Client{Timeout: 30 * time.Second}

// parseSpreadsheetID accepts a bare spreadsheet ID or a full Google Sheets
// URL and returns the ID from the /d/<ID>/ segment.
func parseSpreadsheetID(s string) (string, error) {
	if i := strings.Index(s, "/d/"); i >= 0 {
		id := s[i+3:]
		if j := strings.IndexAny(id, "/?#"); j >= 0 {
			id = id[:j]
		}
		if id == "" {
			return "", fmt.Errorf("no spreadsheet ID in %q", s)
		}
		return id, nil
	}
	if s == "" || strings.ContainsAny(s, "/?#") {
		return "", fmt.Errorf("no spreadsheet ID in %q", s)
	}
	return s, nil
}

// a1Sheet quotes a sheet name for use in an A1 range, so tabs with spaces
// or quotes in the name still form valid ranges.
func a1Sheet(name string) string {
	return "'" + strings.ReplaceAll(name, "'", "''") + "'"
}

type httpError struct {
	code int
	url  string
}

func (e *httpError) Error() string {
	return fmt.Sprintf("HTTP %d: %s", e.code, e.url)
}

func httpJSON(rawURL, token string, data any, method string, out any) error {
	var body io.Reader
	if data != nil {
		buf, err := json.Marshal(data)
		if err != nil {
			return err
		}
		body = bytes.NewReader(buf)
	}
	req, err := http.NewRequest(method, rawURL, body)
	if err != nil {
		return err
	}
	req.Header.Set("User-Agent", userAgent)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	if data != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return &httpError{code: resp.StatusCode, url: rawURL}
	}
	if out == nil {
		return nil
	}
	dec := json.NewDecoder(resp.Body)
	dec.UseNumber()
	return dec.Decode(out)
}

// gcloudToken returns an access token for account, or for the active gcloud
// account when account is empty.
func gcloudToken(account string) (string, error) {
	args := []string{"auth", "print-access-token"}
	if account != "" {
		args = append(args, account)
	}
	var stdout, stderr bytes.Buffer
	cmd := exec.Command("gcloud", args...)
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("gcloud token failed: %s\nRun: gcloud auth login --enable-gdrive-access",
			strings.TrimSpace(stderr.String()))
	}
	return strings.TrimSpace(stdout.String()), nil
}

type options struct {
	platform    string
	city        string
	f           filters
	spreadsheet string // resolved spreadsheet ID
	tab         string
	account     string // gcloud account owning the sheet; empty = active account
	dryRun      bool
}

// row is (url, title, price, surface, location); price is a json.Number or "n/a".
type row struct {
	url     string
	title   string
	price   any
	surface string
	address string
}

// existingURLRows maps each URL in column A (trailing slash stripped) to its
// 1-based row number; data starts at row 2.
func existingURLRows(token, spreadsheetID, sheetName string) (map[string]int, error) {
	var data struct {
		Values [][]any `json:"values"`
	}
	u := fmt.Sprintf("https://sheets.googleapis.com/v4/spreadsheets/%s/values/%s!A2:A", spreadsheetID, a1Sheet(sheetName))
	if err := httpJSON(u, token, nil, http.MethodGet, &data); err != nil {
		return nil, err
	}
	byURL := map[string]int{}
	for i, r := range data.Values {
		if len(r) == 0 {
			continue
		}
		if s, ok := r[0].(string); ok {
			byURL[strings.TrimRight(s, "/")] = i + 2
		}
	}
	return byURL, nil
}

// existingRows reads columns A2:E and maps each URL (trailing slash stripped)
// to its 1-based row number and cell values. UNFORMATTED_VALUE is required:
// the sheet applies currency formatting to prices, and the default
// FORMATTED_VALUE would return "€ 150.000" instead of the number.
func existingRows(token, spreadsheetID, sheetName string) (map[string]update, error) {
	var data struct {
		Values [][]any `json:"values"`
	}
	u := fmt.Sprintf("https://sheets.googleapis.com/v4/spreadsheets/%s/values/%s!A2:E?valueRenderOption=UNFORMATTED_VALUE",
		spreadsheetID, a1Sheet(sheetName))
	if err := httpJSON(u, token, nil, http.MethodGet, &data); err != nil {
		return nil, err
	}
	return decodeSheetRows(data.Values), nil
}

// decodeSheetRows maps each A2:E row to its URL key (trailing slash
// stripped), 1-based row number, and canonical cell values. Rows with a
// missing or non-string URL cell are skipped; short rows read as empty cells.
func decodeSheetRows(values [][]any) map[string]update {
	cell := func(cells []any, i int) any {
		if i < len(cells) {
			return cells[i]
		}
		return nil
	}
	byURL := map[string]update{}
	for i, cells := range values {
		s, ok := cell(cells, 0).(string)
		if !ok || s == "" {
			continue
		}
		byURL[strings.TrimRight(s, "/")] = update{
			rowNum: i + 2,
			r: row{
				url:     s,
				title:   canonCell(cell(cells, 1)),
				price:   canonCell(cell(cells, 2)),
				surface: canonCell(cell(cells, 3)),
				address: canonCell(cell(cells, 4)),
			},
		}
	}
	return byURL
}

// canonCell renders a cell value in the canonical string form used for
// comparisons: numbers and strings compare equal when they print the same.
func canonCell(v any) string {
	if v == nil {
		return ""
	}
	return strings.TrimSpace(fmt.Sprint(v))
}

// rowChanged reports whether any of title, price, surface, or address differ
// between the sheet row and the fetched listing, and describes the changes.
func rowChanged(sheet, fetched row) (bool, string) {
	var diffs []string
	for _, f := range []struct {
		name     string
		old, new any
	}{
		{"title", sheet.title, fetched.title},
		{"price", sheet.price, fetched.price},
		{"surface", sheet.surface, fetched.surface},
		{"address", sheet.address, fetched.address},
	} {
		if o, n := canonCell(f.old), canonCell(f.new); o != n {
			diffs = append(diffs, fmt.Sprintf("%s %s -> %s", f.name, o, n))
		}
	}
	return len(diffs) > 0, strings.Join(diffs, ", ")
}

type update struct {
	rowNum int
	r      row
}

// updateRows overwrites A<n>:F<n> for each update in one batchUpdate call.
func updateRows(token, spreadsheetID, sheetName string, updates []update) error {
	data := make([]map[string]any, 0, len(updates))
	for _, u := range updates {
		data = append(data, map[string]any{
			"range":  fmt.Sprintf("%s!A%d:F%d", a1Sheet(sheetName), u.rowNum, u.rowNum),
			"values": [][]any{rowValues(u.r)},
		})
	}
	u := fmt.Sprintf("https://sheets.googleapis.com/v4/spreadsheets/%s/values:batchUpdate", spreadsheetID)
	body := map[string]any{"valueInputOption": "USER_ENTERED", "data": data}
	return httpJSON(u, token, body, http.MethodPost, &struct{}{})
}

// rowValues renders a row as sheet cells A:F; column F carries the
// price-per-m2 formula used by the existing rows.
func rowValues(r row) []any {
	return []any{
		r.url, r.title, r.price, r.surface, r.address,
		`=INDIRECT("C"&ROW())/INDIRECT("D"&ROW())`,
	}
}

func appendRows(token, spreadsheetID, sheetName string, rows []row) error {
	values := make([][]any, 0, len(rows))
	for _, r := range rows {
		values = append(values, rowValues(r))
	}
	u := fmt.Sprintf("https://sheets.googleapis.com/v4/spreadsheets/%s/values/%s!A1:append?valueInputOption=USER_ENTERED",
		spreadsheetID, a1Sheet(sheetName))
	body := map[string]any{"values": values}
	return httpJSON(u, token, body, http.MethodPost, &struct{}{})
}

func run(opts options) error {
	listings, err := providers[opts.platform].search(opts.city, opts.f)
	if err != nil {
		return err
	}
	token, err := gcloudToken(opts.account)
	if err != nil {
		return err
	}
	known, err := existingRows(token, opts.spreadsheet, opts.tab)
	if err != nil {
		return err
	}
	var newRows []row
	var updates []update
	for _, r := range listings {
		sr, ok := known[strings.TrimRight(r.url, "/")]
		if !ok {
			newRows = append(newRows, r)
			fmt.Printf("new: %s | %v EUR | %s m2 | %s\n", r.title, r.price, r.surface, r.url)
			continue
		}
		if changed, desc := rowChanged(sr.r, r); changed {
			updates = append(updates, update{rowNum: sr.rowNum, r: r})
			fmt.Printf("update: %s (row %d): %s\n", r.url, sr.rowNum, desc)
		}
	}
	if !opts.dryRun {
		if len(newRows) > 0 {
			if err := appendRows(token, opts.spreadsheet, opts.tab, newRows); err != nil {
				return err
			}
		}
		if len(updates) > 0 {
			if err := updateRows(token, opts.spreadsheet, opts.tab, updates); err != nil {
				return err
			}
		}
	}
	suffix := ""
	if opts.dryRun {
		suffix = " (dry run)"
	}
	fmt.Printf("checked %d known, appended %d new, updated %d changed listings%s\n",
		len(known), len(newRows), len(updates), suffix)
	return nil
}

func syncCmd(args []string) error {
	fs := flag.NewFlagSet("sync", flag.ExitOnError)
	var opts options
	var spreadsheet string
	fs.StringVar(&opts.platform, "platform", "immobiliare", "listing platform: "+strings.Join(providerNames(), ", "))
	fs.StringVar(&opts.f.category, "category", "commercial", "listing category: commercial or residential")
	fs.StringVar(&opts.city, "city", "", "city name (required)")
	fs.IntVar(&opts.f.maxPrice, "max-price", 200000, "max price in EUR, 0 disables")
	fs.IntVar(&opts.f.minPrice, "min-price", 0, "min price in EUR, 0 disables")
	fs.IntVar(&opts.f.minSize, "min-size", 60, "min surface in m2, 0 disables")
	fs.IntVar(&opts.f.maxSize, "max-size", 0, "max surface in m2, 0 disables")
	fs.StringVar(&spreadsheet, "spreadsheet", "", "spreadsheet ID or Google Sheets URL (required)")
	fs.StringVar(&opts.tab, "tab", defaultTab, "sheet tab name")
	fs.StringVar(&opts.account, "account", "", "gcloud account owning the sheet (default: active account)")
	fs.BoolVar(&opts.dryRun, "dry-run", false, "print actions without touching the sheet")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if spreadsheet == "" || opts.city == "" {
		return errors.New("sync: --spreadsheet and --city are required")
	}
	if _, ok := providers[opts.platform]; !ok {
		return fmt.Errorf("unknown --platform %q (available: %s)", opts.platform, strings.Join(providerNames(), ", "))
	}
	id, err := parseSpreadsheetID(spreadsheet)
	if err != nil {
		return err
	}
	opts.spreadsheet = id
	return run(opts)
}

func main() {
	args := os.Args[1:]
	if len(args) == 0 {
		fmt.Fprintln(os.Stderr, "usage: listing-tracker sync [flags]")
		os.Exit(2)
	}
	var err error
	switch args[0] {
	case "sync":
		err = syncCmd(args[1:])
	case "watch": // deprecated alias for sync
		fmt.Fprintln(os.Stderr, "note: \"watch\" is deprecated, use \"sync\"")
		err = syncCmd(args[1:])
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand %q\nusage: listing-tracker sync [flags]\n", args[0])
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
