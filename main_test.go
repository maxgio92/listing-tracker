package main

import (
	"encoding/json"
	"testing"
)

func TestParseSpreadsheetID(t *testing.T) {
	tests := []struct {
		in      string
		want    string
		wantErr bool
	}{
		{in: "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-example", want: "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-example"},
		{in: "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-example/edit?gid=0#gid=0", want: "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-example"},
		{in: "https://docs.google.com/spreadsheets/d/abc123", want: "abc123"},
		{in: "https://docs.google.com/spreadsheets/d/abc123#gid=0", want: "abc123"},
		{in: "", wantErr: true},
		{in: "https://docs.google.com/spreadsheets/d/", wantErr: true},
		{in: "https://docs.google.com/spreadsheets", wantErr: true},
	}
	for _, tt := range tests {
		got, err := parseSpreadsheetID(tt.in)
		if tt.wantErr {
			if err == nil {
				t.Errorf("parseSpreadsheetID(%q) = %q, want error", tt.in, got)
			}
			continue
		}
		if err != nil {
			t.Errorf("parseSpreadsheetID(%q): %v", tt.in, err)
			continue
		}
		if got != tt.want {
			t.Errorf("parseSpreadsheetID(%q) = %q, want %q", tt.in, got, tt.want)
		}
	}
}

func TestNormalizeSurface(t *testing.T) {
	tests := []struct {
		in   string
		want string
	}{
		{"88 m²", "88"},
		{"1.234 m²", "1234"},
		{"1.234,5 m²", "1234.5"},
		{"96,5 m²", "96.5"},
		{"", "n/a"},
		{"m²", "n/a"},
		{"  215 m² ", "215"},
	}
	for _, tt := range tests {
		if got := normalizeSurface(tt.in); got != tt.want {
			t.Errorf("normalizeSurface(%q) = %q, want %q", tt.in, got, tt.want)
		}
	}
}

func TestRowChanged(t *testing.T) {
	base := row{
		url:     "https://www.immobiliare.it/annunci/123/",
		title:   "Shop in Sample Street",
		price:   json.Number("150000"),
		surface: "88",
		address: "Sample Street, Center",
	}
	tests := []struct {
		name        string
		sheet       row
		fetched     row
		wantChanged bool
		wantDesc    string
	}{
		{
			name:    "identical, sheet price numeric vs fetched json.Number",
			sheet:   row{title: base.title, price: json.Number("150000"), surface: "88", address: base.address},
			fetched: base,
		},
		{
			name:        "sheet n/a price vs fetched number",
			sheet:       row{title: base.title, price: "n/a", surface: "88", address: base.address},
			fetched:     base,
			wantChanged: true,
			wantDesc:    "price n/a -> 150000",
		},
		{
			name:        "changed price",
			sheet:       row{title: base.title, price: json.Number("140000"), surface: "88", address: base.address},
			fetched:     base,
			wantChanged: true,
			wantDesc:    "price 140000 -> 150000",
		},
		{
			name:        "changed title",
			sheet:       row{title: "Commercial unit", price: json.Number("150000"), surface: "88", address: base.address},
			fetched:     base,
			wantChanged: true,
			wantDesc:    "title Commercial unit -> Shop in Sample Street",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			changed, desc := rowChanged(tt.sheet, tt.fetched)
			if changed != tt.wantChanged {
				t.Errorf("changed = %v, want %v (desc %q)", changed, tt.wantChanged, desc)
			}
			if desc != tt.wantDesc {
				t.Errorf("desc = %q, want %q", desc, tt.wantDesc)
			}
		})
	}
}

func TestA1Sheet(t *testing.T) {
	tests := []struct{ in, want string }{
		{"Listings", "'Listings'"},
		{"My Tab", "'My Tab'"},
		{"O'Brien", "'O''Brien'"},
	}
	for _, tt := range tests {
		if got := a1Sheet(tt.in); got != tt.want {
			t.Errorf("a1Sheet(%q) = %q, want %q", tt.in, got, tt.want)
		}
	}
}

func TestDecodeSheetRows(t *testing.T) {
	got := decodeSheetRows([][]any{
		{"https://x/1/", "Shop", json.Number("150000"), json.Number("88"), "Sample Street"},
		{"https://x/2"},                      // short row: missing cells read as empty
		{},                                   // empty row: skipped
		{json.Number("42"), "not a URL"},     // non-string URL cell: skipped
		{"https://x/3/", nil, nil, nil, nil}, // nil cells read as empty
	})
	if len(got) != 3 {
		t.Fatalf("got %d rows, want 3: %v", len(got), got)
	}

	full := got["https://x/1"]
	if full.rowNum != 2 {
		t.Errorf("rowNum = %d, want 2", full.rowNum)
	}
	// Numeric cells must land in canonical string form so they compare
	// equal to fetched values like json.Number("150000") or "88".
	want := row{url: "https://x/1/", title: "Shop", price: "150000", surface: "88", address: "Sample Street"}
	if full.r != want {
		t.Errorf("row = %+v, want %+v", full.r, want)
	}
	if changed, desc := rowChanged(full.r, row{
		url: want.url, title: "Shop", price: json.Number("150000"), surface: "88", address: "Sample Street",
	}); changed {
		t.Errorf("decoded row compares as changed against equal fetched row: %s", desc)
	}

	short := got["https://x/2"]
	if short.rowNum != 3 || short.r.title != "" || short.r.price != "" {
		t.Errorf("short row = %+v, want rowNum 3 and empty cells", short)
	}
	if nils := got["https://x/3"]; nils.rowNum != 6 || nils.r.title != "" {
		t.Errorf("nil-cells row = %+v, want rowNum 6 and empty cells", nils)
	}
}

func TestBuildSearchParams(t *testing.T) {
	g := geo{region: "reg", province: "PR", comune: "1234", keyurl: "sampletown"}

	tests := []struct {
		name            string
		opts            options
		wantParamsCount string
		wantPath        string
		wantSet         map[string]string
		wantUnset       []string
	}{
		{
			name:            "defaults",
			opts:            options{buildingType: "commercial", maxPrice: 200000, minSize: 60},
			wantParamsCount: "2",
			wantPath:        "/vendita-negozi/sampletown/",
			wantSet: map[string]string{
				"prezzoMassimo":    "200000",
				"superficieMinima": "60",
				"idCategoria":      "26",
				"idContratto":      "1",
				"idComune":         "1234",
			},
			wantUnset: []string{"prezzoMinimo", "superficieMassima"},
		},
		{
			name: "all filters residential",
			opts: options{
				buildingType: "residential",
				maxPrice:     150000, minPrice: 50000, minSize: 80, maxSize: 120,
			},
			wantParamsCount: "4",
			wantPath:        "/vendita-case/sampletown/",
			wantSet: map[string]string{
				"prezzoMinimo":      "50000",
				"superficieMassima": "120",
				"idCategoria":       "1",
			},
		},
		{
			name:            "zero disables all filters",
			opts:            options{buildingType: "commercial"},
			wantParamsCount: "0",
			wantPath:        "/vendita-negozi/sampletown/",
			wantUnset:       []string{"prezzoMassimo", "prezzoMinimo", "superficieMinima", "superficieMassima"},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			params := buildSearchParams(tt.opts, g)
			if got := params.Get("paramsCount"); got != tt.wantParamsCount {
				t.Errorf("paramsCount = %q, want %q", got, tt.wantParamsCount)
			}
			if got := params.Get("path"); got != tt.wantPath {
				t.Errorf("path = %q, want %q", got, tt.wantPath)
			}
			for k, want := range tt.wantSet {
				if got := params.Get(k); got != want {
					t.Errorf("%s = %q, want %q", k, got, want)
				}
			}
			for _, k := range tt.wantUnset {
				if params.Has(k) {
					t.Errorf("%s should be unset, got %q", k, params.Get(k))
				}
			}
		})
	}
}
