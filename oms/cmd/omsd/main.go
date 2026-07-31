// Command omsd runs the OMS HTTP service — Session 3's "skeleton" made
// actually runnable, not just a library. Not started by anything in
// production yet (no systemd unit, no watchdog.py/autodeploy.py
// awareness) — see the roadmap doc's Phase 2, Session 5 for when bot.py
// starts calling this.
package main

import (
	"log"
	"net/http"
	"os"

	"github.com/trytobecomenice/polymarket-copybot/oms/httpserver"
	"github.com/trytobecomenice/polymarket-copybot/oms/store"
)

func main() {
	dbPath := os.Getenv("OMS_DB_PATH")
	if dbPath == "" {
		// Same default data/app.db every other part of this monorepo
		// already reads/writes, run from the repo root.
		dbPath = "data/app.db"
	}
	addr := os.Getenv("OMS_ADDR")
	if addr == "" {
		addr = "127.0.0.1:8090"
	}

	s, err := store.Open(dbPath)
	if err != nil {
		log.Fatalf("omsd: opening store at %s: %v", dbPath, err)
	}
	defer s.Close()

	srv := httpserver.New(s)
	log.Printf("omsd: listening on %s (db: %s)", addr, dbPath)
	if err := http.ListenAndServe(addr, srv.Handler()); err != nil {
		log.Fatalf("omsd: %v", err)
	}
}
