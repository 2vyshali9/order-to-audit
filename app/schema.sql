-- Order-to-Audit schema (SQLite).
-- Conventions: money = integer paise; quantities = integer base units (g, ml, piece, packet).
-- Stock balances are NEVER stored: they are SUM() over the append-only stock_ledger.

CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  full_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('salesman','admin','production','accountant','owner')),
  password_hash TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);

CREATE TABLE customers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('retailer','distributor')),
  gstin TEXT,
  state_code TEXT NOT NULL,            -- GST state code, e.g. '33' = Tamil Nadu
  address TEXT,
  phone TEXT,
  salesman_id INTEGER REFERENCES users(id),
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE suppliers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  gstin TEXT,
  state_code TEXT,
  phone TEXT
);

-- One table for raw materials, packaging and finished products.
CREATE TABLE items (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('raw','packaging','finished')),
  base_unit TEXT NOT NULL CHECK (base_unit IN ('g','ml','piece','packet')),
  disp_unit TEXT NOT NULL,             -- kg | L | piece | packet
  factor INTEGER NOT NULL DEFAULT 1,   -- base units per display unit (kg->1000 g)
  hsn TEXT,
  gst_rate_bp INTEGER NOT NULL DEFAULT 0,   -- basis points: 500 = 5%
  price_paise INTEGER NOT NULL DEFAULT 0,   -- default selling price per packet (finished)
  carton_size INTEGER,                      -- packets per carton (finished)
  shelf_life_days INTEGER,
  reorder_level_base INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE customer_prices (
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  item_id INTEGER NOT NULL REFERENCES items(id),
  price_paise INTEGER NOT NULL,
  PRIMARY KEY (customer_id, item_id)
);

-- Bill of materials: one BOM per finished product; quantities are per ONE batch.
CREATE TABLE boms (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL UNIQUE REFERENCES items(id),
  yield_packets INTEGER NOT NULL CHECK (yield_packets > 0),
  note TEXT
);
CREATE TABLE bom_lines (
  id INTEGER PRIMARY KEY,
  bom_id INTEGER NOT NULL REFERENCES boms(id),
  item_id INTEGER NOT NULL REFERENCES items(id),
  qty_base INTEGER NOT NULL CHECK (qty_base > 0),
  per_packet INTEGER NOT NULL DEFAULT 0,    -- 1 = consumption follows output (pouches)
  UNIQUE (bom_id, item_id)
);

CREATE TABLE purchases (
  id INTEGER PRIMARY KEY,
  purchase_no TEXT NOT NULL UNIQUE,
  supplier_id INTEGER REFERENCES suppliers(id),
  supplier_inv_no TEXT,
  purchase_date TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL
);
CREATE TABLE purchase_lines (
  id INTEGER PRIMARY KEY,
  purchase_id INTEGER NOT NULL REFERENCES purchases(id),
  item_id INTEGER NOT NULL REFERENCES items(id),
  qty_base INTEGER NOT NULL CHECK (qty_base > 0),
  rate_paise INTEGER NOT NULL,              -- per DISPLAY unit (per kg / per L / per piece)
  amount_paise INTEGER NOT NULL,            -- taxable value
  gst_rate_bp INTEGER NOT NULL DEFAULT 0,
  gst_paise INTEGER NOT NULL DEFAULT 0,
  batch_no TEXT,
  expiry TEXT
);

CREATE TABLE runs (
  id INTEGER PRIMARY KEY,
  run_no TEXT NOT NULL UNIQUE,
  item_id INTEGER NOT NULL REFERENCES items(id),
  bom_id INTEGER NOT NULL REFERENCES boms(id),
  run_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('open','completed','cancelled')),
  batches TEXT NOT NULL,                    -- Decimal as text, e.g. '1' or '1.5'
  expected_output INTEGER NOT NULL,
  good_output INTEGER,
  damaged_units INTEGER NOT NULL DEFAULT 0,
  batch_no TEXT,
  expiry TEXT,
  notes TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  completed_at TEXT
);
CREATE TABLE run_lines (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  item_id INTEGER NOT NULL REFERENCES items(id),
  planned_base INTEGER NOT NULL,
  actual_base INTEGER NOT NULL DEFAULT 0,
  actual_value_paise INTEGER NOT NULL DEFAULT 0,
  per_packet INTEGER NOT NULL DEFAULT 0,
  UNIQUE (run_id, item_id)
);
CREATE TABLE run_overheads (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  kind TEXT NOT NULL CHECK (kind IN ('labour','power','gas','other')),
  amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
  note TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL
);

-- The single source of truth for stock. Append-only (see triggers below).
CREATE TABLE stock_ledger (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  mov_date TEXT NOT NULL,
  item_id INTEGER NOT NULL REFERENCES items(id),
  qty_base INTEGER NOT NULL,                -- signed: + in, - out
  value_paise INTEGER NOT NULL,             -- signed valuation of the movement
  mtype TEXT NOT NULL CHECK (mtype IN
    ('OPENING','PURCHASE','PROD_ISSUE','PROD_OUTPUT','SALE','SALE_RETURN','ADJUSTMENT','COST_ADJ','REVERSAL')),
  ref_type TEXT NOT NULL,                   -- purchase | run | invoice | adjustment | opening
  ref_id INTEGER,
  ref_no TEXT,
  batch_no TEXT,
  expiry TEXT,
  reverses_id INTEGER REFERENCES stock_ledger(id),
  user_id INTEGER REFERENCES users(id),
  note TEXT
);
CREATE INDEX ix_ledger_item ON stock_ledger(item_id, mov_date);
CREATE INDEX ix_ledger_ref ON stock_ledger(ref_type, ref_id);
CREATE TRIGGER ledger_no_update BEFORE UPDATE ON stock_ledger
  BEGIN SELECT RAISE(ABORT, 'stock_ledger is append-only'); END;
CREATE TRIGGER ledger_no_delete BEFORE DELETE ON stock_ledger
  BEGIN SELECT RAISE(ABORT, 'stock_ledger is append-only'); END;

CREATE TABLE orders (
  id INTEGER PRIMARY KEY,
  order_no TEXT NOT NULL UNIQUE,
  client_uuid TEXT UNIQUE,                  -- idempotency key for offline sync
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  salesman_id INTEGER NOT NULL REFERENCES users(id),
  status TEXT NOT NULL CHECK (status IN
    ('submitted','approved','rejected','invoiced','dispatched','cancelled')),
  notes TEXT,
  reject_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE order_lines (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  item_id INTEGER NOT NULL REFERENCES items(id),
  qty INTEGER NOT NULL CHECK (qty > 0),     -- packets
  price_paise INTEGER NOT NULL,             -- per packet, before tax
  UNIQUE (order_id, item_id)
);

CREATE TABLE invoices (
  id INTEGER PRIMARY KEY,
  invoice_no TEXT NOT NULL UNIQUE,
  fy TEXT NOT NULL,
  order_id INTEGER REFERENCES orders(id),
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  inv_date TEXT NOT NULL,
  place_of_supply TEXT NOT NULL,
  supply_type TEXT NOT NULL CHECK (supply_type IN ('intra','inter')),
  taxable_paise INTEGER NOT NULL,
  cgst_paise INTEGER NOT NULL,
  sgst_paise INTEGER NOT NULL,
  igst_paise INTEGER NOT NULL,
  total_paise INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('posted','cancelled')),
  dispatched_at TEXT,
  share_token TEXT NOT NULL UNIQUE,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL
);
CREATE TABLE invoice_lines (
  id INTEGER PRIMARY KEY,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id),
  item_id INTEGER NOT NULL REFERENCES items(id),
  description TEXT NOT NULL,
  hsn TEXT,
  qty INTEGER NOT NULL,
  rate_paise INTEGER NOT NULL,
  taxable_paise INTEGER NOT NULL,
  gst_rate_bp INTEGER NOT NULL,
  cgst_paise INTEGER NOT NULL,
  sgst_paise INTEGER NOT NULL,
  igst_paise INTEGER NOT NULL,
  total_paise INTEGER NOT NULL
);
-- Posted invoices are immutable: no delete, and amounts cannot be updated.
CREATE TRIGGER invoices_no_delete BEFORE DELETE ON invoices
  BEGIN SELECT RAISE(ABORT, 'posted invoices cannot be deleted; issue a credit note'); END;
CREATE TRIGGER invoices_amounts_locked
  BEFORE UPDATE OF invoice_no, customer_id, inv_date, place_of_supply, supply_type,
                   taxable_paise, cgst_paise, sgst_paise, igst_paise, total_paise ON invoices
  BEGIN SELECT RAISE(ABORT, 'posted invoice amounts are locked; issue a credit note'); END;
CREATE TRIGGER invoice_lines_no_delete BEFORE DELETE ON invoice_lines
  BEGIN SELECT RAISE(ABORT, 'invoice lines cannot be deleted'); END;
CREATE TRIGGER invoice_lines_no_update BEFORE UPDATE ON invoice_lines
  BEGIN SELECT RAISE(ABORT, 'invoice lines are locked'); END;

CREATE TABLE credit_notes (
  id INTEGER PRIMARY KEY,
  cn_no TEXT NOT NULL UNIQUE,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id),
  cn_date TEXT NOT NULL,
  taxable_paise INTEGER NOT NULL,
  cgst_paise INTEGER NOT NULL,
  sgst_paise INTEGER NOT NULL,
  igst_paise INTEGER NOT NULL,
  total_paise INTEGER NOT NULL,
  reason TEXT NOT NULL,
  goods_returned INTEGER NOT NULL DEFAULT 0,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL
);

CREATE TABLE payments (
  id INTEGER PRIMARY KEY,
  receipt_no TEXT NOT NULL UNIQUE,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id),
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  pay_date TEXT NOT NULL,
  amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
  mode TEXT NOT NULL,
  ref TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL
);

CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  user_id INTEGER,
  username TEXT,
  entity TEXT NOT NULL,
  entity_id INTEGER,
  action TEXT NOT NULL,
  details TEXT
);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
