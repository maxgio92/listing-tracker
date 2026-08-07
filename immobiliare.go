package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
)

const (
	immobiliareAPI          = "https://www.immobiliare.it/api-next/search-list/listings/"
	immobiliareAutocomplete = "https://www.immobiliare.it/api-next/geography/autocomplete/"
)

// idCategoria and URL path segment per building type.
var immobiliareCategories = map[string][2]string{
	"commercial":  {"26", "negozi"}, // API category 26, URL segment "negozi" (shops)
	"residential": {"1", "case"},    // API category 1, URL segment "case" (homes)
}

// immobiliareProvider is the immobiliare.it listing provider.
type immobiliareProvider struct{}

func (immobiliareProvider) search(city string, f filters) ([]row, error) {
	if _, ok := immobiliareCategories[f.category]; !ok {
		return nil, fmt.Errorf("invalid --category %q (choose from commercial, residential)", f.category)
	}
	g, err := immobiliareResolveCity(city)
	if err != nil {
		return nil, err
	}
	return immobiliareFetchListings(immobiliareSearchParams(f, g))
}

type immobiliareGeoParent struct {
	Type   int    `json:"type"`
	ID     string `json:"id"`
	Label  string `json:"label"`
	Keyurl string `json:"keyurl"`
}

type immobiliareGeoEntry struct {
	Parents []immobiliareGeoParent `json:"parents"`
}

// immobiliareGeo identifies a comune on immobiliare.it.
type immobiliareGeo struct {
	region   string
	province string
	comune   string
	keyurl   string
}

func immobiliareResolveCity(name string) (immobiliareGeo, error) {
	q := url.Values{
		"macrozones": {"1"},
		"min_level":  {"9"},
		"query":      {name},
		"__lang":     {"it"},
	}
	var entries []immobiliareGeoEntry
	if err := httpJSON(immobiliareAutocomplete+"?"+q.Encode(), "", nil, http.MethodGet, &entries); err != nil {
		return immobiliareGeo{}, err
	}
	for _, entry := range entries {
		parents := map[int]immobiliareGeoParent{}
		for _, p := range entry.Parents {
			parents[p.Type] = p
		}
		comune, ok2 := parents[2]
		_, ok1 := parents[1]
		_, ok0 := parents[0]
		if ok2 && ok1 && ok0 && strings.EqualFold(comune.Label, name) {
			return immobiliareGeo{
				region:   parents[0].ID,
				province: parents[1].ID,
				comune:   comune.ID,
				keyurl:   strings.ToLower(comune.Keyurl),
			}, nil
		}
	}
	return immobiliareGeo{}, fmt.Errorf("city not found on immobiliare.it: %s", name)
}

func immobiliareSearchParams(f filters, g immobiliareGeo) url.Values {
	cat := immobiliareCategories[f.category]
	idCategoria, pathSegment := cat[0], cat[1]
	fv := url.Values{}
	if f.maxPrice != 0 {
		fv.Set("prezzoMassimo", strconv.Itoa(f.maxPrice))
	}
	if f.minPrice != 0 {
		fv.Set("prezzoMinimo", strconv.Itoa(f.minPrice))
	}
	if f.minSize != 0 {
		fv.Set("superficieMinima", strconv.Itoa(f.minSize))
	}
	if f.maxSize != 0 {
		fv.Set("superficieMassima", strconv.Itoa(f.maxSize))
	}
	params := url.Values{
		"fkRegione":   {g.region},
		"idProvincia": {g.province},
		"idComune":    {g.comune},
		"idContratto": {"1"}, // vendita
		"idCategoria": {idCategoria},
		"__lang":      {"it"},
		"paramsCount": {strconv.Itoa(len(fv))},
		"path":        {fmt.Sprintf("/vendita-%s/%s/", pathSegment, g.keyurl)},
	}
	for k, v := range fv {
		params[k] = v
	}
	return params
}

// normalizeSurface turns immobiliare's "1.234,5 m²" style into "1234.5", or "n/a".
func normalizeSurface(s string) string {
	s = strings.TrimSpace(strings.ReplaceAll(s, "m²", ""))
	s = strings.ReplaceAll(s, ".", "")
	s = strings.ReplaceAll(s, ",", ".")
	if s == "" {
		return "n/a"
	}
	return s
}

type immobiliareSearchPage struct {
	Results []struct {
		RealEstate struct {
			ID    json.Number `json:"id"`
			Title string      `json:"title"`
			Price struct {
				Value json.Number `json:"value"`
			} `json:"price"`
			Properties []struct {
				Surface  string `json:"surface"`
				Location struct {
					Address   string `json:"address"`
					Macrozone string `json:"macrozone"`
					City      string `json:"city"`
				} `json:"location"`
			} `json:"properties"`
		} `json:"realEstate"`
	} `json:"results"`
	TotalAds int `json:"totalAds"`
}

func immobiliareFetchListings(searchParams url.Values) ([]row, error) {
	var rows []row
	seen := 0
	for page := 1; ; page++ {
		params := url.Values{}
		for k, v := range searchParams {
			params[k] = v
		}
		params.Set("pag", strconv.Itoa(page))
		var data immobiliareSearchPage
		err := httpJSON(immobiliareAPI+"?"+params.Encode(), "", nil, http.MethodGet, &data)
		if err != nil {
			var herr *httpError
			if errors.As(err, &herr) && herr.code == http.StatusNotFound {
				return rows, nil // past the last page
			}
			return nil, err
		}
		for _, item := range data.Results {
			re := item.RealEstate
			if len(re.Properties) == 0 {
				continue // a malformed listing must not kill the whole run
			}
			prop := re.Properties[0]
			loc := prop.Location
			var price any = "n/a"
			if re.Price.Value != "" {
				price = re.Price.Value
			}
			var parts []string
			for _, p := range []string{loc.Address, loc.Macrozone, loc.City} {
				if p != "" {
					parts = append(parts, p)
				}
			}
			address := strings.Join(parts, ", ")
			if address == "" {
				address = "n/a"
			}
			title := re.Title
			if title == "" {
				title = "n/a"
			}
			rows = append(rows, row{
				url:     fmt.Sprintf("https://www.immobiliare.it/annunci/%s/", re.ID.String()),
				title:   title,
				price:   price,
				surface: normalizeSurface(prop.Surface),
				address: address,
			})
		}
		seen += len(data.Results)
		if len(data.Results) == 0 || seen >= data.TotalAds {
			return rows, nil
		}
	}
}
