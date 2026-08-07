package main

import (
	"testing"
)

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

func TestBuildSearchParams(t *testing.T) {
	g := geo{region: "reg", province: "PR", comune: "1234", keyurl: "sampletown"}

	tests := []struct {
		name            string
		f               filters
		wantParamsCount string
		wantPath        string
		wantSet         map[string]string
		wantUnset       []string
	}{
		{
			name:            "defaults",
			f:               filters{category: "commercial", maxPrice: 200000, minSize: 60},
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
			f: filters{
				category: "residential",
				maxPrice: 150000, minPrice: 50000, minSize: 80, maxSize: 120,
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
			f:               filters{category: "commercial"},
			wantParamsCount: "0",
			wantPath:        "/vendita-negozi/sampletown/",
			wantUnset:       []string{"prezzoMassimo", "prezzoMinimo", "superficieMinima", "superficieMassima"},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			params := buildSearchParams(tt.f, g)
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

func TestImmobiliareSearchRejectsBadCategory(t *testing.T) {
	_, err := immobiliareProvider{}.search("Sampletown", filters{category: "castles"})
	if err == nil {
		t.Fatal("search with invalid category: want error, got nil")
	}
	if got, want := err.Error(), `invalid --category "castles" (choose from commercial, residential)`; got != want {
		t.Errorf("error = %q, want %q", got, want)
	}
}
