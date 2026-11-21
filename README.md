# Order-to-Audit: salesman's order to the auditor's monthly report

A working prototype for a small South Indian snack manufacturer (Tamil Nadu). Every transaction is entered once, at its source, and flows into stock, production, costing and the auditor's reports. It links the **sales loop** (order → GST invoice → dispatch → receivables) and the **production loop** (purchase → issue per BOM → run → finished goods → variance and cost per packet) at finished-goods stock.

## Run it

**Docker (one command)**

```bash
docker compose up --build        # http://localhost:8000, seeds the Section 5 data on first start
```

**Locally**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m app.seed               # creates instance/app.db with the Section 5 master data (--reset to start over)
flask --app wsgi run             # http://127.0.0.1:5000
python -m pytest                 # 45 tests
python -m app.benchmark          # runs the Section 5 scenario on a fresh DB and prints 23 checks
```

**Test logins** (password for all: `demo123`)

| User | Role | Sees |
| --- | --- | --- |
| `salesA` / `salesB` | Salesman | Mobile order entry, own orders and customer-wise sales (A: Retailer A, B: Distributor B) |
| `admin` | Factory admin | Orders, approval, invoices, dispatch, payments, masters, everything operational |
| `production` | Production supervisor | Purchases, BOM, runs, stock, production and costing reports |
| `auditor` | Accountant | Read-only reports and invoices |
| `owner` | Owner | Dashboard (today's orders, stock, variance alerts, cost per packet, dues) and reports |

## Stack and why

Python 3.12, Flask, SQLite (WAL), server-rendered Jinja pages with a small PWA layer for the salesman screen, ReportLab (PDF), openpyxl (Excel). One process, one file database and no build step, so it starts with a single command and a reviewer can read every line. SQLite gives real transactions, foreign keys, triggers and window functions, which is what the ledger design needs. The mobile screen is a PWA (service worker + local queue) rather than a native app so it works on any phone with no install and can queue orders offline.

## What is built, against the Section 6 tiers

**Must have: all done.** Login with five roles and per-route access control; product, customer, raw-material, packaging and BOM masters; mobile-friendly order entry and admin approval; GST invoice PDF; production run that updates raw and finished stock automatically; planned vs actual consumption and yield with variance; stock summary and stock ledger; sales register and stock report as CSV/Excel/PDF.

**Should have: all done.** Purchases and suppliers; payments, outstanding and ageing; cost per packet with overheads and margin; GST summary (plus GSTR-1 and HSN tables); variance alerts beyond a tolerance (setting, default 3%); owner dashboard.

**Stretch goals done:** offline order capture with idempotent sync (`client_uuid`); batch numbers and expiry dates recorded on purchases and production output; reorder levels with LOW flags on the stock report and dashboard; shortage calculation that says how many batches are needed to fulfil an order. Orders can be placed in packets or cartons (unit conversion applies).

**Also built:** invoice cancellation by credit note (never deleted), WhatsApp/e-mail share link for invoices, audit-trail report, run cancellation with ledger reversal, stock adjustments with a mandatory reason, CSRF protection on every form post.

## Benchmark (Section 5), run with `python -m app.benchmark`

| Check | Expected | Result |
| --- | --- | --- |
| Flour closing | 120 kg | 120 kg |
| Oil closing | 19.5 L | 19.5 L |
| Pouches closing | 306 | 306 |
| Finished goods after run | 238 | 238 |
| After Retailer A dispatch | 88 | 88 |
| Yield variance | −6%, flagged | −6.00%, flagged |
| Oil variance | +5%, flagged | +5.00%, flagged |
| Retailer A invoice | ₹7,500 + 187.50 + 187.50 = ₹7,875 | identical |
| Retailer A outstanding | ₹3,875 | ₹3,875.00 |
| Distributor B | Shortage 32, IGST if invoiced | Shortage 32; approval blocked; IGST ₹300.00 |
| Cost per packet | own figure | **₹31.14** |

Two differences from the checklist, both deliberate:

* **Cartons are also flagged (−10%).** 9 cartons used against 10 planned breaches the 3% tolerance, so the system flags it along with oil and yield. Pouches are −3.00% exactly (194 vs 200) and are **not** flagged because the rule is strictly greater than the tolerance.
* **Variance is computed against the batch plan, not scaled to actual output.** A yield shortfall therefore shows up as a yield variance and also makes packaging look under-used.

### Cost per packet: ₹31.14

| Component | Amount |
| --- | --- |
| Flour 40 kg at weighted average ₹43.125 | ₹1,725.00 |
| Oil 10.5 L at ₹140 | ₹1,470.00 |
| Spice mix 1.2 kg at ₹200 | ₹240.00 |
| Pouches 194 (188 good + 6 damaged) at ₹2.50 | ₹485.00 |
| Cartons 9 at ₹15 | ₹135.00 |
| **Materials** | **₹4,055.00** |
| Labour ₹1,200 + gas/power ₹600 | ₹1,800.00 |
| **Total** | **₹5,855.00 ÷ 188 good packets = ₹31.14** |

Against the ₹50 selling price the margin is ₹18.86 (37.7%). Valuation method and overhead rule are in the [design note](docs/design-note.md).

## Assumptions

1. **Valuation is moving weighted average**, which gives flour a ₹43.125/kg cost after the ₹45 purchase mixes with the ₹42 opening stock.
2. **Overheads are charged directly to the run** and divided over *good* packets, so yield loss and damaged pouches are absorbed by the good output.
3. **Opening finished goods (50 packets) are valued at ₹30/packet** because the brief gives no figure. This affects stock valuation only, not cost per packet.
4. GST on Murukku is 5% as the brief instructs; HSN codes and all GSTINs in the seed are dummy values. Seller state is Tamil Nadu (33), so any other state gets IGST.
5. Approval is blocked when stock does not cover the order, unless the admin ticks the shortage override; **dispatch is always blocked** while finished goods are insufficient.
6. Admin is a superset of the production role for operations; auditor and owner are strictly read-only.

## Known gaps

* No first-expiry-first-out dispatch (expiry is recorded and reported, not used to pick stock).
* No partial dispatch or partial returns; a posted invoice is reversed whole by credit note.
* No Tally export, e-invoice/e-way bill format, GPS visit log, or Tamil interface.
* Single-node SQLite: fine for one factory, not for multi-site scale. Change `SECRET_KEY` and the demo passwords before any real deployment.
* Overhead per month is entered per run; there is no automatic apportioning of a monthly pool across runs.

## Layout

```
app/schema.sql        tables, append-only triggers
app/services/         business rules: stock (ledger), production, sales, purchase, reports
app/views/            Flask blueprints (auth, sales, prod, reports) and role checks
app/exporters.py      CSV / XLSX / PDF and the GST invoice PDF
app/templates, static server-rendered pages, PWA order screen (order.js, sw.js)
app/seed.py           Section 5 master data     app/benchmark.py  the scenario runner
tests/                stock, tax and unit maths, benchmark, and the benchmark through the web app
```
