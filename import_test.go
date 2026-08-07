package main

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestParseImportFile(t *testing.T) {
	wantRow := row{
		url:     "https://www.immobiliare.it/annunci/123456789/",
		title:   "Downtown shop",
		price:   json.Number("150000"),
		surface: "120",
		address: "16 Sample Street, Sampletown",
	}

	tests := []struct {
		name string
		file string
		in   string
		want []row
	}{
		{
			name: "tsv no header",
			file: "listings.tsv",
			in:   "https://www.immobiliare.it/annunci/123456789/\tDowntown shop\t150000\t120\t16 Sample Street, Sampletown\n",
			want: []row{wantRow},
		},
		{
			name: "tsv with header",
			file: "listings.tsv",
			in: "URL\tTitle\tPrice\tSurface\tAddress\n" +
				"https://www.immobiliare.it/annunci/123456789/\tDowntown shop\t150000\t120\t16 Sample Street, Sampletown\n",
			want: []row{wantRow},
		},
		{
			name: "csv with header and n/a",
			file: "listings.csv",
			in: "URL,Title,Price,Surface,Address\n" +
				`https://www.immobiliare.it/annunci/123456789/,Downtown shop,n/a,n/a,"16 Sample Street, Sampletown"` + "\n",
			want: []row{{
				url:     wantRow.url,
				title:   wantRow.title,
				price:   "n/a",
				surface: "n/a",
				address: wantRow.address,
			}},
		},
		{
			name: "extra columns ignored",
			file: "listings.tsv",
			in:   "https://www.immobiliare.it/annunci/123456789/\tDowntown shop\t150000\t120\t16 Sample Street, Sampletown\textra\n",
			want: []row{wantRow},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := parseImportFile(tt.file, strings.NewReader(tt.in))
			if err != nil {
				t.Fatalf("parseImportFile: %v", err)
			}
			if len(got) != len(tt.want) {
				t.Fatalf("got %d rows, want %d", len(got), len(tt.want))
			}
			for i := range got {
				if got[i] != tt.want[i] {
					t.Errorf("row %d = %+v, want %+v", i, got[i], tt.want[i])
				}
			}
		})
	}
}

func TestParseImportFileErrors(t *testing.T) {
	tests := []struct {
		name     string
		file     string
		in       string
		wantLine string
	}{
		{
			name:     "too few columns",
			file:     "listings.tsv",
			in:       "https://example.com/1/\tTitle\t100\n",
			wantLine: "record 1",
		},
		{
			name: "bad price after header",
			file: "listings.tsv",
			in: "URL\tTitle\tPrice\tSurface\tAddress\n" +
				"https://example.com/1/\tTitle\tcheap\t100\tAddress\n",
			wantLine: "record 2",
		},
		{
			name:     "bad surface",
			file:     "listings.csv",
			in:       "https://example.com/1/,Title,100,huge,Address\n",
			wantLine: "record 1",
		},
		{
			name:     "unsupported extension",
			file:     "listings.txt",
			in:       "whatever",
			wantLine: ".txt",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := parseImportFile(tt.file, strings.NewReader(tt.in))
			if err == nil {
				t.Fatal("parseImportFile: want error, got nil")
			}
			if !strings.Contains(err.Error(), tt.wantLine) {
				t.Errorf("error %q does not mention %q", err, tt.wantLine)
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

func TestDedupeRows(t *testing.T) {
	rows := []row{
		{url: "https://example.com/1/", title: "first"},
		{url: "https://example.com/2/", title: "second"},
		{url: "https://example.com/1", title: "third"}, // same key as first
	}
	got, dups := dedupeRows(rows)
	if dups != 1 {
		t.Errorf("dups = %d, want 1", dups)
	}
	if len(got) != 2 {
		t.Fatalf("got %d rows, want 2", len(got))
	}
	if got[0].title != "third" {
		t.Errorf("got[0].title = %q, want %q (last occurrence wins)", got[0].title, "third")
	}
	if got[1].title != "second" {
		t.Errorf("got[1].title = %q, want %q", got[1].title, "second")
	}
}
