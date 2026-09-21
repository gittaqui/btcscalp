PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
 client_id TEXT PRIMARY KEY, exchange_id TEXT UNIQUE, status TEXT NOT NULL,
 created_ns INTEGER NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
 fill_id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES orders(client_id),
 ts_ns INTEGER NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fills_time ON fills(ts_ns);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, ts_ns INTEGER NOT NULL, category TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_category_time ON events(category,ts_ns);
CREATE TABLE IF NOT EXISTS commands (
 id INTEGER PRIMARY KEY, created_ns INTEGER NOT NULL, action TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', result TEXT
);
PRAGMA user_version=1;
