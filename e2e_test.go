//go:build e2e

// End-to-end tests against the real immobiliare.it and Google Sheets APIs.
//
// Run with:
//
//	E2E_CITY=<city> E2E_SPREADSHEET_ID=<id> [E2E_ACCOUNT=<gcloud account>] \
//	  go test -tags e2e ./...
//
// Tests skip when their environment variable is unset. The Sheets test works
// on a temporary tab that is created and deleted by the test; existing tabs
// are never touched.
package main

import (
	"fmt"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"
)

func e2eCity(t *testing.T) string {
	city := os.Getenv("E2E_CITY")
	if city == "" {
		t.Skip("E2E_CITY not set")
	}
	return city
}

func e2eSpreadsheet(t *testing.T) string {
	id := os.Getenv("E2E_SPREADSHEET_ID")
	if id == "" {
		t.Skip("E2E_SPREADSHEET_ID not set")
	}
	return id
}

func TestE2EResolveCity(t *testing.T) {
	city := e2eCity(t)
	g, err := immobiliareResolveCity(city)
	if err != nil {
		t.Fatalf("immobiliareResolveCity(%q): %v", city, err)
	}
	if g.region == "" || g.province == "" || g.comune == "" || g.keyurl == "" {
		t.Errorf("immobiliareResolveCity(%q) returned empty fields: %+v", city, g)
	}
}

func TestE2EProviderSearch(t *testing.T) {
	city := e2eCity(t)
	f := filters{category: "commercial", maxPrice: 200000, minSize: 60}
	rows, err := providers["immobiliare"].search(city, f)
	if err != nil {
		t.Fatalf("search(%q): %v", city, err)
	}
	if len(rows) == 0 {
		t.Fatalf("expected listings for %q, got none", city)
	}
	for _, r := range rows {
		if !strings.HasPrefix(r.url, "https://www.immobiliare.it/annunci/") {
			t.Errorf("bad listing URL: %q", r.url)
		}
		if r.title == "" || r.surface == "" || r.address == "" {
			t.Errorf("empty field in row: %+v", r)
		}
	}
}

func TestE2ESheetsRoundTrip(t *testing.T) {
	spreadsheetID := e2eSpreadsheet(t)
	token, err := gcloudToken(os.Getenv("E2E_ACCOUNT"))
	if err != nil {
		t.Skipf("no gcloud token: %v", err)
	}

	scratch := fmt.Sprintf("e2e-test-%d", time.Now().UnixNano())
	sheetID, err := addSheet(token, spreadsheetID, scratch)
	if err != nil {
		t.Fatalf("addSheet: %v", err)
	}
	defer func() {
		if err := deleteSheet(token, spreadsheetID, sheetID); err != nil {
			t.Errorf("cleanup deleteSheet: %v", err)
		}
	}()

	testRow := row{
		url:     "https://www.immobiliare.it/annunci/999999999/",
		title:   "e2e test listing",
		price:   12345,
		surface: "67",
		address: "Via di Test",
	}

	// A fresh tab has no known URLs.
	known, err := existingURLRows(token, spreadsheetID, scratch)
	if err != nil {
		t.Fatalf("existingURLRows on empty tab: %v", err)
	}
	if len(known) != 0 {
		t.Fatalf("expected empty tab, got %d URLs", len(known))
	}

	// Append a header row (existingURLRows reads A2:A) and the row.
	header := row{url: "URL", title: "Title", price: "Price (EUR)", surface: "Surface (m2)", address: "Address"}
	if err := appendRows(token, spreadsheetID, scratch, []row{header, testRow}); err != nil {
		t.Fatalf("appendRows: %v", err)
	}

	// The appended URL must now be known, trailing slash stripped.
	known, err = existingURLRows(token, spreadsheetID, scratch)
	if err != nil {
		t.Fatalf("existingURLRows after append: %v", err)
	}
	if _, ok := known["https://www.immobiliare.it/annunci/999999999"]; !ok {
		t.Errorf("appended URL not found in existingURLRows, got %v", known)
	}
}

// addSheet creates a tab in the spreadsheet and returns its sheet ID.
func addSheet(token, spreadsheetID, title string) (int64, error) {
	var reply struct {
		Replies []struct {
			AddSheet struct {
				Properties struct {
					SheetID int64 `json:"sheetId"`
				} `json:"properties"`
			} `json:"addSheet"`
		} `json:"replies"`
	}
	body := map[string]any{
		"requests": []any{
			map[string]any{"addSheet": map[string]any{"properties": map[string]any{"title": title}}},
		},
	}
	u := fmt.Sprintf("https://sheets.googleapis.com/v4/spreadsheets/%s:batchUpdate", spreadsheetID)
	if err := httpJSON(u, token, body, http.MethodPost, &reply); err != nil {
		return 0, err
	}
	if len(reply.Replies) == 0 {
		return 0, fmt.Errorf("addSheet: empty reply")
	}
	return reply.Replies[0].AddSheet.Properties.SheetID, nil
}

// deleteSheet removes a tab from the spreadsheet by sheet ID.
func deleteSheet(token, spreadsheetID string, sheetID int64) error {
	body := map[string]any{
		"requests": []any{
			map[string]any{"deleteSheet": map[string]any{"sheetId": sheetID}},
		},
	}
	u := fmt.Sprintf("https://sheets.googleapis.com/v4/spreadsheets/%s:batchUpdate", spreadsheetID)
	return httpJSON(u, token, body, http.MethodPost, &struct{}{})
}
