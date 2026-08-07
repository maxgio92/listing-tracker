package main

// The import subcommand upserts listing rows from a TSV/CSV file into the
// tracking sheet: rows whose URL (trailing slash stripped) is already in
// column A are updated in place with one values:batchUpdate call, the rest
// are appended.

import (
	"encoding/csv"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type importOptions struct {
	spreadsheet string // resolved spreadsheet ID
	tab         string
	file        string
	account     string // gcloud account owning the sheet; empty = active account
	dryRun      bool
}

func importCmd(args []string) error {
	fs := flag.NewFlagSet("import", flag.ExitOnError)
	var opts importOptions
	var spreadsheet string
	fs.StringVar(&spreadsheet, "spreadsheet", "", "spreadsheet ID or Google Sheets URL (required)")
	fs.StringVar(&opts.tab, "tab", defaultTab, "sheet tab name")
	fs.StringVar(&opts.file, "file", "", "TSV or CSV file with rows to upsert (or pass it as the argument)")
	fs.StringVar(&opts.account, "account", "", "gcloud account owning the sheet (default: active account)")
	fs.BoolVar(&opts.dryRun, "dry-run", false, "print actions without touching the sheet")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if opts.file == "" && fs.NArg() > 0 {
		opts.file = fs.Arg(0)
	}
	if spreadsheet == "" || opts.file == "" {
		return errors.New("import: --spreadsheet is required and a file must be given (positional or --file)")
	}
	id, err := parseSpreadsheetID(spreadsheet)
	if err != nil {
		return err
	}
	opts.spreadsheet = id
	return runImport(opts)
}

// cellNumber validates a Price or Surface cell: a number or "n/a".
func cellNumber(s string) (string, error) {
	s = strings.TrimSpace(s)
	if s == "n/a" {
		return s, nil
	}
	if _, err := strconv.ParseFloat(s, 64); err != nil {
		return "", fmt.Errorf("want a number or \"n/a\", got %q", s)
	}
	return s, nil
}

// parseImportFile reads rows from r in the format implied by name's extension:
// tab-separated for .tsv, comma-separated for .csv. Columns are URL, Title,
// Price, Surface, Address; extra columns are ignored. A first row whose first
// cell is "URL" is treated as a header and skipped.
func parseImportFile(name string, r io.Reader) ([]row, error) {
	cr := csv.NewReader(r)
	switch ext := strings.ToLower(filepath.Ext(name)); ext {
	case ".tsv":
		cr.Comma = '\t'
	case ".csv":
	default:
		return nil, fmt.Errorf("unsupported file extension %q (want .tsv or .csv)", ext)
	}
	cr.FieldsPerRecord = -1
	records, err := cr.ReadAll()
	if err != nil {
		return nil, err
	}
	// Record numbers, not physical lines: a quoted CSV field may span lines.
	record := 0
	if len(records) > 0 && len(records[0]) > 0 && records[0][0] == "URL" {
		records = records[1:]
		record = 1
	}
	var rows []row
	for _, rec := range records {
		record++
		if len(rec) < 5 {
			return nil, fmt.Errorf("record %d: want 5 columns (URL, Title, Price, Surface, Address), got %d", record, len(rec))
		}
		u := strings.TrimSpace(rec[0])
		if u == "" {
			return nil, fmt.Errorf("record %d: empty URL", record)
		}
		priceCell, err := cellNumber(rec[2])
		if err != nil {
			return nil, fmt.Errorf("record %d: price: %v", record, err)
		}
		var price any = priceCell
		if priceCell != "n/a" {
			price = json.Number(priceCell)
		}
		surface, err := cellNumber(rec[3])
		if err != nil {
			return nil, fmt.Errorf("record %d: surface: %v", record, err)
		}
		rows = append(rows, row{
			url:     u,
			title:   rec[1],
			price:   price,
			surface: surface,
			address: rec[4],
		})
	}
	return rows, nil
}

// dedupeRows drops rows whose key (URL, trailing slash stripped) repeats;
// the last occurrence wins, keeping the position of the first.
func dedupeRows(rows []row) (out []row, dups int) {
	index := map[string]int{}
	for _, r := range rows {
		key := strings.TrimRight(r.url, "/")
		if i, ok := index[key]; ok {
			out[i] = r
			dups++
			continue
		}
		index[key] = len(out)
		out = append(out, r)
	}
	return out, dups
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

func runImport(opts importOptions) error {
	f, err := os.Open(opts.file)
	if err != nil {
		return err
	}
	defer func() { _ = f.Close() }()
	rows, err := parseImportFile(opts.file, f)
	if err != nil {
		return fmt.Errorf("%s: %w", opts.file, err)
	}
	rows, dups := dedupeRows(rows)
	token, err := gcloudToken(opts.account)
	if err != nil {
		return err
	}
	known, err := existingURLRows(token, opts.spreadsheet, opts.tab)
	if err != nil {
		return err
	}
	var updates []update
	var appends []row
	for _, r := range rows {
		if n, ok := known[strings.TrimRight(r.url, "/")]; ok {
			updates = append(updates, update{rowNum: n, r: r})
			fmt.Printf("update: %s (row %d)\n", r.url, n)
		} else {
			appends = append(appends, r)
			fmt.Printf("new: %s\n", r.url)
		}
	}
	if !opts.dryRun {
		if len(updates) > 0 {
			if err := updateRows(token, opts.spreadsheet, opts.tab, updates); err != nil {
				return err
			}
		}
		if len(appends) > 0 {
			if err := appendRows(token, opts.spreadsheet, opts.tab, appends); err != nil {
				return err
			}
		}
	}
	dupNote := ""
	if dups > 0 {
		dupNote = fmt.Sprintf(", %d duplicate keys ignored", dups)
	}
	suffix := ""
	if opts.dryRun {
		suffix = " (dry run)"
	}
	fmt.Printf("imported %d rows: %d updated, %d appended%s%s\n", len(rows), len(updates), len(appends), dupNote, suffix)
	return nil
}
