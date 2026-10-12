package agent

import (
	"bytes"
	"encoding/json"
	"fmt"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/tidwall/gjson"
	"github.com/tidwall/jsonc"

	"github.com/yetone/magpie/internal/edit"
)

const (
	empryoProviders = "providers"
	empryoModel     = "defaultModel"
)

func empryo(home string) *Agent { return empryoAt(here(home)) }

func empryoIn(at place) *Agent { return empryoAt(at) }

type empryoSavedProvider struct {
	Index int
	Value json.RawMessage
}

type empryoSavedConfig struct {
	Model            json.RawMessage `json:"model,omitempty"`
	Providers        []empryoSavedProvider
	ProvidersPresent bool
	Created          bool
}

func empryoDir(at place) string {
	if runtime.GOOS == "windows" && at.spell == nil {
		local := at.getenv("LOCALAPPDATA")
		if !filepath.IsAbs(local) {
			local = filepath.Join(at.home, "AppData", "Local")
		}
		return filepath.Join(local, "Empryo")
	}
	return filepath.Join(at.home, ".empryo")
}

func setEmpryoModel(path, value, gatewayV1 string) error {
	raw, err := edit.Read(path)
	if err != nil {
		return err
	}
	if len(bytes.TrimSpace(raw)) == 0 {
		raw = []byte("{}")
	}
	parsed := jsonc.ToJSONInPlace(append([]byte(nil), raw...))
	root := gjson.ParseBytes(parsed)
	if !json.Valid(parsed) || !root.IsObject() {
		return fmt.Errorf("%s: invalid JSON config", path)
	}
	providers := root.Get(empryoProviders)
	if providers.Exists() && !providers.IsArray() {
		return fmt.Errorf("%s: providers must be an array", path)
	}
	stored, err := edit.Read(stashPath())
	if err != nil {
		return err
	}
	stashed := map[string]string{}
	if len(bytes.TrimSpace(stored)) > 0 {
		if err := json.Unmarshal(stored, &stashed); err != nil {
			return fmt.Errorf("Empryo originals cannot be read: %w", err)
		}
		if stashed == nil {
			return fmt.Errorf("Empryo originals must be an object")
		}
	}
	key := "empryo:" + path + ":state"
	oldModel := "empryo:" + path + ":" + empryoModel
	oldNew := "empryo:" + path + ":new"
	saved := empryoSavedConfig{}
	encoded, managed := stashed[key]
	if managed {
		if err := json.Unmarshal([]byte(encoded), &saved); err != nil {
			return fmt.Errorf("Empryo originals cannot be decoded: %w", err)
		}
	}
	list := make([]json.RawMessage, 0, len(providers.Array())+1)
	for _, p := range providers.Array() {
		list = append(list, json.RawMessage(p.Raw))
	}
	ref, prefixed := strings.CutPrefix(value, magpieID+"/")
	add := prefixed && isMagpie(ref)
	legacy := !managed && (stashed[oldModel] != "" || stashed[oldNew] != "")
	if legacy {
		saved.Created = stashed[oldNew] != ""
		if previous := stashed[oldModel]; previous != "" {
			saved.Model, err = json.Marshal(previous)
			if err != nil {
				return err
			}
		}
		for _, p := range providers.Array() {
			if p.Get("id").String() != magpieID {
				saved.ProvidersPresent = true
			}
		}
		managed = true
	}
	if add && !managed {
		saved = empryoSavedConfig{Model: json.RawMessage(root.Get(empryoModel).Raw), ProvidersPresent: providers.Exists(), Created: !isFile(path)}
		for i, p := range providers.Array() {
			if p.Get("id").String() == magpieID {
				saved.Providers = append(saved.Providers, empryoSavedProvider{Index: i, Value: json.RawMessage(p.Raw)})
			}
		}
		managed = true
	}
	set := []edit.KV{}
	del := []string{}
	if managed {
		kept := make([]json.RawMessage, 0, len(list)+1)
		for _, p := range list {
			if gjson.GetBytes(p, "id").String() != magpieID {
				kept = append(kept, p)
			}
		}
		if add {
			mine, err := json.Marshal(map[string]any{"id": magpieID, "name": "Magpie", "baseURL": gatewayV1, "modelsAPI": gatewayV1 + "/models"})
			if err != nil {
				return err
			}
			kept = append(kept, mine)
			state, err := json.Marshal(saved)
			if err != nil {
				return err
			}
			stashed[key] = string(state)
		} else {
			for _, p := range saved.Providers {
				if p.Index < 0 || !json.Valid(p.Value) {
					return fmt.Errorf("Empryo saved provider is invalid")
				}
				at := min(p.Index, len(kept))
				kept = append(kept, nil)
				copy(kept[at+1:], kept[at:])
				kept[at] = p.Value
			}
			delete(stashed, key)
		}
		if len(kept) > 0 || saved.ProvidersPresent {
			set = append(set, edit.KV{Path: empryoProviders, Value: kept})
		} else {
			del = append(del, empryoProviders)
		}
		delete(stashed, oldModel)
		delete(stashed, oldNew)
	}
	switch {
	case value != "":
		set = append(set, edit.KV{Path: empryoModel, Value: value})
	case managed && len(saved.Model) > 0:
		set = append(set, edit.KV{Path: empryoModel, Value: saved.Model})
	default:
		del = append(del, empryoModel)
	}
	updated, err := edit.PatchJSON(raw, set, del)
	if err != nil {
		return err
	}
	if managed {
		if err := stashWrite(stashed); err != nil {
			return err
		}
	}
	if !add && saved.Created && len(gjson.ParseBytes(jsonc.ToJSONInPlace(append([]byte(nil), updated...))).Map()) == 0 {
		return edit.Remove(path)
	}
	if !isFile(path) && len(gjson.ParseBytes(updated).Map()) == 0 {
		return nil
	}
	return edit.WriteAtomic(path, updated)
}

func empryoAt(at place) *Agent {
	dir := empryoDir(at)
	path := filepath.Join(dir, "config.json")
	model := func() string { v, _ := edit.GetJSON(path, empryoModel); return v }
	return &Agent{
		ID: "empryo", Name: "Empryo", Icon: "empryo-color", Aliases: []string{"soulforge"}, Spelled: prefixed,
		Bin: "empryo", Dir: dir, Path: path,
		Notice: func() string { return noticeEmpryo.String() },
		Check: func() string {
			ref, routed := strings.CutPrefix(model(), magpieID+"/")
			if !routed || !isMagpie(ref) {
				return ""
			}
			raw, err := edit.Read(path)
			if err != nil {
				return fmt.Sprintf("Empryo's config cannot be read: %v", err)
			}
			for _, p := range gjson.GetBytes(raw, empryoProviders).Array() {
				if p.Get("id").String() != magpieID {
					continue
				}
				return wiringOff("Empryo", path, func(k string) (string, bool) {
					r := p.Get(k)
					return r.String(), r.Exists()
				}, "baseURL", at.v1(), "modelsAPI", at.v1()+"/models")
			}
			return "Empryo's magpie provider (config.json) is gone, so it no longer reaches magpie"
		},
		Fields: []Field{{
			Key: "model", Label: "model", Get: model,
			Set: func(v string) error {
				stashMu.Lock()
				defer stashMu.Unlock()
				return edit.Atomically(func() error { return setEmpryoModel(path, v, at.v1()) }, path, stashPath())
			},
			Options: func(cur map[string]string) []Option {
				var own []Option
				if c := cur["model"]; c != "" && !usesMagpie(c) {
					_, id, _ := strings.Cut(c, "/")
					own = append(own, Option{Value: c, Icon: modelIcon("", id)})
				}
				return append(group("Empryo", own), viaMagpie("empryo", magpieID+"/")...)
			},
		}},
	}
}

var (
	noticeEmpryo = newNotice("Empryo v3 reloads these settings live. Project settings and existing tabs can override this global default. Older versions may need a restart.")
)
