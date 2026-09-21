# Trading Cards

## Storage decision

Trading Cards uses one app-owned SQLite file, configured with
`LUIGI_WEB_CARDS_DB` and defaulting to `data/cards.db`.

This file is intentionally separate from both LuigiBot PostgreSQL and Finance:

- LuigiBot owns its task schema and schema version. Card tables are not part of
  that contract.
- The card catalog can contain hundreds of thousands of refreshable rows and
  has different backup, indexing, and write patterns from tasks.
- Finance has a separate unlock and privacy boundary that card data must not
  enter.
- Keeping the catalog, decks, and collection together allows atomic imports
  and direct owned-versus-required and deck-value joins.

The database contains:

```text
games
cards
decks
deck_cards
tags
deck_tags
collection
collection_lots
collection_imports
deck_versions
bulk_refresh
price_history
```

SQLite foreign keys, WAL mode, busy timeouts, explicit write transactions, and
schema `user_version` are enabled by the repository adapter. Cards owns schema
version 4; this does not change LuigiBot's shared schema or Finance.

The v4 migration preserves each existing holding as one `legacy_summary` lot,
including its aggregate acquisition metadata and known priced quantity. It does
not invent individual past purchases. Copies without known acquisition costs
remain unknown. `collection_lots` records subsequent purchases and retained
removal history; `collection_imports` holds bounded CSV preview/apply state.
`deck_versions` is created when version storage is initialized and keeps bounded
deck snapshots in this same database.

## Features

- Game switcher for MTG, Pokemon TCG, and Riftbound.
- Searchable, set-filtered catalog browser.
- Rich card inspector with provider artwork, double-faced card switching,
  rules, stats, legalities, printings, prices, trusted card links, local deck
  usage, and collection records.
- Visual Art & printings picker with transactional Use this printing actions
  for existing deck slots and collection records. Swaps preserve deck
  quantity/board/category and collection quantity/variant/purchase lots;
  original purchased printings and price provenance survive an alternate-art
  swap. A compatible existing destination holding is merged transactionally.
- Scryfall-style advanced local search for rules/type text, colors, commander
  identity, mana and numeric stats, games, format legality, set/group, rarity,
  all current Scryfall card criteria, prices, artist/flavor/lore, language,
  sorting, unique cards/art, and preferred printing. Extra cards are imported
  by new Scryfall refreshes but remain hidden unless Include Extras or an
  extra-specific criterion is selected.
- Manual catalog records for games without a configured provider.
- Deck create, edit, archive, delete, tag, and commander workflows.
- Mainboard, sideboard, maybeboard, and commander card groups.
- Full-category Table and overlapping Stacks deck views, with 40 card rows per
  stack lane, additional lanes for larger categories, and bounded scrolling.
- Compact deck library, secondary-action menu, and editable mobile Table rows.
  View/group preferences are local to the deck and device; view switching still
  works when browser preference storage is unavailable.
- Deck totals and statistics refresh after successful card mutations. Card
  search supports Enter without leaving its dialog; loading and error states
  keep an explicit close action.
- Quantity-weighted mana, color, type, category, and board statistics with
  explicit unknown metadata and secondary, non-blocking format advisories.
- Named deck versions, duplicate/clone, version comparison, and preview-first
  restore with stale-state checks and an automatic pre-restore snapshot.
- Archidekt, Moxfield, MTGO, MTGA, and plain-text list parsing.
- Dry-run import preview and one-transaction import application.
- Board and custom category preservation from text imports. `Mainboard`,
  `Sideboard`, `Maybeboard`, and `Commander` may be plain, bracketed, counted,
  or comment-prefixed headers. `[Ramp]`, `Category: Ramp`, `Ramp:`, `Ramp (12)`,
  and `// Ramp` preserve arbitrary section labels. Trailing card markers such
  as `1 Example Card [Ramp]` and `1 Example Card #Ramp` override the active
  category for that card. Labels retain their first-seen order.
- Table and Stacks views split Commander, Mainboard, Sideboard, and Maybeboard
  into distinct bands with custom categories nested beneath each board.
- Plain-text deck export.
- Filtered, paginated collection with purchase lots, lot-specific corrections,
  retained removal history, and preview-first CSV import/export.
- Separate actual paid, historical estimated, legacy aggregate, and unknown
  acquisition costs; exact remaining-lot totals and comparable market gain/loss.
  Cached market values show as-of dates and missing-price coverage.
- Owned-versus-needed build checklists, explicit competing-deck allocation,
  exact or MTG-equivalent printing matching, and missing-card shopping CSV.
- Card and deck value-history SVG charts.
- Optional draw.io notes stored as validated XML.

## Deck views and build planning

Table keeps every card row in its board/category group. Stacks keeps the same
grouping: a category of 40 rows remains one lane; row 41 starts another lane.
The limit counts rows, not copy quantities, and never hides cards beyond 40.
Each lane scrolls vertically within `min(700px, 72svh)`; the board scrolls
horizontally across lanes. Keyboard navigation and focus/scroll restoration
support repeated edits. Browser preferences are not part of a deck snapshot.

Open **Build checklist** from the deck header. Commander and Mainboard are
selected by default; Sideboard and Maybeboard can be included explicitly.
Repeated cards across selected boards/categories share one owned pool. Exact
printing matching is the default. MTG's any-printing option uses oracle identity,
with a labeled name fallback only among records that lack oracle identity.
Pokemon and Riftbound fall back to exact printing, with a notice.

Competing allocation is opt-in: select up to 20 other same-game decks in priority
order. They consume available copies on the selected boards before the current
deck. No other deck is inferred automatically. The checklist separates required,
owned, reserved, available, allocated, and missing quantities. This is a read-only
plan, not a persistent reservation or assignment of physical copies, and does
not change collection quantities. Holdings across foil/condition rows contribute
to ownership; the plan does not choose a physical variant.

Shopping CSV contains missing items only. Its estimates use cached regular USD
prices: the cheapest priced requested printing, or a cached equivalent when no
requested printing is priced. The chosen printing, price basis, and cache date
are exposed. Unknown prices stay blank; known subtotals do not imply complete
coverage. These estimates are not purchase costs or live provider quotes.

## Statistics and advisories

**Stats** separates main-plus-commander, sideboard, maybeboard, and all-board
counts and values. Categories retain their board identity. Distributions are
quantity-weighted main-plus-commander copies: MTG includes nonland mana values,
colors, and types; cards with a land face are excluded from the mana curve, and
`X` and unknown mana values remain separate. Multicolor and multi-type copies
count in each applicable group. Pokemon uses cached supertypes/types; Riftbound
uses cached type labels. Missing metadata is shown as unknown.

Format checks are secondary, advisory, and never block edits. Bounded MTG checks
cover Commander, Standard, Modern, Pioneer, and Legacy only. Commander checks
main-plus-commander size, basic commander eligibility, color identity, known
copy limits, and cached legality. The other supported formats check main/side
sizes, known copy limits, and cached legality. Maybeboard is not checked.
Unsupported formats/games, incomplete metadata, commander-pair rules, and
unrecognized exceptions remain unknown, not passes. Cache import dates are not
the provider's current rules date; no tournament-legal verdict is issued.

## Versions, clone, and restore

Open **Versions** from the deck header or **Duplicate deck** from Actions.
Snapshots include name, format, description, archive state, commander/partner/
cover references, all printing/quantity/board/category slots, tags, and validated
draw.io XML. Duplicate creates an independent deck with this content, not another
set of owned cards or a copy of the source's version history.

Compare a saved version with the current deck or another saved version of that
deck. Differences include additions, removals, quantity changes, board/category
moves, metadata, and tags. Diagram changes are summarized without rendering raw
XML. This is same-deck version comparison, not arbitrary cross-deck comparison.

Restore requires a preview and explicit confirmation. Hashes guard both the
current deck and saved version; a change after preview requires a new preview.
Missing/wrong-game card references and conflicting shared tags also block the
restore. A **Before restore** snapshot is saved atomically before replacement,
and the committed deck is read back for verification.

Each deck can retain at most 100 versions, each at most 2,000,000 bytes (2 MB).
Restore needs room for its automatic snapshot and refuses at the limit without
discarding older versions. Versions are local to the deck and deleted with it;
they do not back up the catalog, collection, or price history. Plain-text deck
export is also not a full backup. Protect the whole Cards SQLite database with
a SQLite-consistent backup when full recovery is required.

## Collection and purchases

**Collection** groups holdings by printing, foil, and condition. **Record
purchase** adds a separate lot with quantity, purchase date, unit price,
USD/EUR currency, and notes. **Purchase history** supports lot-specific cost/date
corrections; a holding with multiple lots requires an explicit lot selection.
The holding limit is 9,999 copies. Priced lots in one holding must share a
currency; different holdings can use different currencies without conversion.

Price provenance stays explicit:

| Source | Meaning |
|---|---|
| `entered` | User-entered actual unit cost, not a provider estimate |
| `market_estimate` | Explicitly confirmed local market snapshot for the exact purchase date, with source/date retained |
| `unknown` | No recorded cost; not a zero-cost purchase |
| `legacy_summary` | Migrated aggregate baseline, not reconstructed individual purchases |

Historical estimates use the original printing, requested currency, and matching
regular/foil column on the exact date. They use local `price_history`, or the
catalog cache only when its price date is that same day. No earlier or later
snapshot, today's price for an older purchase, regular-price substitute for
foil, or currency conversion is used. EUR foil estimates are unavailable.
Absent exact-day coverage leaves cost unknown without blocking the purchase.
A found estimate requires confirmation and remains separate from actual paid.

Totals sum integer unit cost times remaining priced quantity for each lot; they
do not multiply all copies by a rounded average. A rounded weighted unit average
remains only for aggregate compatibility/display. Partial legacy `acquired_qty`
is preserved: the unpriced remainder stays unknown, and a legacy correction does
not turn the baseline into actual purchase history. Actual gain/loss compares
only remaining copies with entered costs and matching-currency market coverage,
excluding estimated, legacy, unknown, and incompatible-currency costs. It is a
cached valuation comparison, not a realized sale or a Finance record.

Printing swaps preserve `original_card_id`, purchase date/cost, and estimate
provenance while changing the current holding printing. Removal consumes lots
in recorded-lot order and retains their original quantities and purchase fields.
Partial removal from a legacy aggregate proportionally rounds its known priced
quantity because original copy-level records do not exist. Fully removed
holdings remain accessible through **History**; removal is not history erasure.

### Filtering and CSV

Filter by name, set, rarity, foil, condition, use in active decks, deck coverage,
currency, and current market unit-price range. Pages default to 50 holdings and
allow up to 200; filtered totals and whole-game counts are separate. Coverage
compares exact-printing ownership with demand across all boards of non-archived
decks. It is not the build checklist's explicit competing-deck allocation and
does not list completely unowned catalog cards.

CSV export streams all matching active lots across pages, up to 100,000 lots;
larger exports require narrower filters instead of silent truncation. It keeps
original/current printing identity, remaining quantity, priced quantity,
currency, source/date provenance, and notes, with spreadsheet formula escaping.
It excludes removed lots and is an exchange format, not a database/history
backup. Export size may exceed the import limit; split large files before import.

Import accepts UTF-8 CSV up to 500,000 bytes and 2,000 rows. Resolve each printing
by `external_id` or exact `set_code` plus `collector_number`, not an ambiguous
name. Choose duplicate handling explicitly: add purchases, skip existing
holdings, or reject duplicates. Preview validates every row and trial-applies in
a rolled-back savepoint; only bounded normalized preview state is retained.
Apply is transactional and idempotent for that preview token, which expires
after one hour if unapplied. Re-importing as a new preview is a new operation.
Imported market estimates require exact-day local or retained provenance and
explicit confirmation; a CSV cannot simply relabel an arbitrary price as an
estimate. Legacy partial priced quantities remain legacy after round-trip.

## Pages and routes

The Cards manifest mounts
[../module-repos/cards/src/luigi_web/modules/cards/composition.py](../module-repos/cards/src/luigi_web/modules/cards/composition.py),
which includes the library, analysis, collection, and version routers once.
Existing Catalog, Decks, Collection, and Card Data URLs are unchanged. The new
deck-header links are **Build checklist** and **Versions**; **Stats** remains a
deck tab and **Duplicate deck** is in Actions.

All paths below start with `/cards/{game_code}` and require authentication:

| Method | Path | Purpose |
|---|---|---|
| GET | `/decks/{deck_id}/build`, `/decks/{deck_id}/build.csv` | Build plan and missing-card CSV |
| GET | `/decks/{deck_id}/analysis.json` | Version-1 analysis result |
| GET / POST | `/decks/{deck_id}/versions` | Compare/list or save a named version |
| GET | `/decks/{deck_id}/versions/{version_id}/preview` | Restore preview |
| POST | `/decks/{deck_id}/versions/{version_id}/restore` | Confirm guarded restore |
| POST | `/decks/{deck_id}/duplicate` | Clone current deck content |
| POST | `/collection/query`, `/collection/search`, `/collection/estimate` | Filter page, catalog search, exact-date estimate |
| GET | `/collection/{collection_id}/lots`, `/collection/removed/history` | Purchase and removed-holding history |
| POST | `/collection/import/preview`, `/collection/import/apply`, `/collection/export` | Bounded CSV exchange |

Build HTML/CSV accepts `match_mode=exact` or `any`, repeated `board` values, and
repeated `reserve_deck_id` values in allocation order. `boards_set=1` allows an
explicit empty board selection. Collection filter/search/import contents stay
in POST bodies in the UI. Existing collection add, acquisition-correction,
printing-swap, and delete routes remain in place.

## Catalog providers

MTG uses the Scryfall bulk-data API. A refresh:

1. validates the manifest and download hosts;
2. enforces a maximum advertised and downloaded size;
3. streams JSON with `ijson` instead of loading it into memory;
4. commits cards in bounded batches;
5. stores prices as exact integer minor units;
6. records a refresh audit row and price snapshot; and
7. removes the bulk and partial-download files.

Automatic refresh is off by default. Set
`LUIGI_WEB_CARDS_REFRESH_HOURS` to a positive interval or use the authenticated
Card Data page. The service runs one Uvicorn worker, so the in-process refresh
coordinator cannot start duplicate local jobs.

Pokemon uses the paginated pokemontcg.io API for cards, expansions, artwork,
TCGPlayer USD prices, and Cardmarket EUR prices. An API key is optional and is
sent only to that provider; manual entries remain available as a fallback.
Riftbound currently uses manual catalog entry. Its deck, import/export, and
collection features are otherwise the same as MTG.

"Current" or today's market value means the latest locally cached provider
price, not a live quote. The Collection page shows per-holding price dates,
the filtered as-of range, and unpriced copy counts; actual, estimated, and legacy
costs remain separate. A missing price is unavailable, not zero. No targeted
per-card, per-set, or stale-price refresh was added: use the existing manual
Card Data refresh or optional scheduled provider refresh. Merely opening a
deck, collection, build plan, or estimate does not fetch provider prices.

## Money and images

Scryfall decimal prices are converted with `Decimal` and half-up rounding to
integer cents. Acquisition rows also store an ISO currency. Persisted card
prices never use binary floating point.

Only HTTPS images from the allow-listed Scryfall and Pokemon TCG image hosts
are retained from provider data. Manual entries do not accept arbitrary image
URLs.

## Security and privacy

- Every route requires the main Luigi Web session or bearer token.
- Cookie-authenticated mutations require the existing double-submit CSRF
  header.
- Deck and collection pages use `Cache-Control: no-store`.
- Build plans, version responses, purchase history, and CSV responses are also
  private/no-store. Cookie-authenticated POST workflows use same-origin CSRF;
  bearer clients retain the existing authentication contract.
- URL object IDs are scoped to their parent game and deck.
- Import text is bounded by bytes, lines, field lengths, and quantities.
- Draw.io XML is limited to 1 MB, parsed locally, and rejects declarations,
  active elements, and active URL schemes.
- Draw.io is disabled unless `LUIGI_WEB_CARDS_DRAWIO_URL` names a trusted HTTPS
  endpoint or local development endpoint.
- Card records are not exposed through Luigi Web's LLM tool registry.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LUIGI_WEB_CARDS_DB` | `data/cards.db` | Isolated domain database |
| `LUIGI_WEB_CARDS_BULK_DIR` | sibling `cards-bulk` directory | Temporary downloads |
| `LUIGI_WEB_CARDS_SCRYFALL_BULK` | `default_cards` | Scryfall dataset |
| `LUIGI_WEB_CARDS_REFRESH_HOURS` | `0` | Automatic refresh interval; zero disables |
| `LUIGI_WEB_CARDS_POKEMON_API_KEY` | blank | Optional pokemontcg.io rate-limit key |
| `LUIGI_WEB_CARDS_DRAWIO_URL` | blank | Optional trusted diagram editor |

The database and bulk directory are gitignored. Production paths must be
writable by the Luigi Web service account and kept outside public backups.
The collection, analysis, and version workflows add no environment variables.

## Local performance measurement

From the repository root with the project's Python dependencies installed:

```powershell
python scripts/benchmark_cards.py
```

[../scripts/benchmark_cards.py](../scripts/benchmark_cards.py) creates a
deterministic temporary database, clears inherited application settings, blocks
outbound network/DNS, avoids the application host, and removes the temporary
data on completion. Defaults are 100,000 catalog printings, 5,000 holdings,
120 deck rows, and 40 rows per category. Each operation has one warmup followed
by five timed iterations; SQL tracing/query plans are collected separately.
Timing includes repository initialization. It does not contact real providers.

On one local Python 3.14.0 / SQLite 3.50.4 run with that unchanged fixture, the
unique-card (deduplicated) catalog median changed from **567.866 ms to
250.737 ms** after narrowing window ranking to IDs and replacing the repeated
wide-window count with a distinct-identity count. This is a measured local
synthetic result, not a production benchmark, end-to-end page latency, or a
guaranteed percentage improvement. Running the command measures the current
checkout; it does not reproduce the old implementation automatically.

## Disposable GUI preview

From the repository root, the optional development preview runs with synthetic
cards and decks in a temporary database:

```powershell
python scripts/preview_cards.py
```

It prints an available loopback URL, uses a fresh local session with CSRF
protection, and removes its database on normal shutdown. It does not load
deployment environment files or start LuigiBot, Finance, chat, or background
provider integrations. Catalog refresh and diagram embeds are disabled.
Never expose this development preview through a proxy or public network.

[../scripts/preview_cards.py](../scripts/preview_cards.py) mounts the same Cards
composition, including build/analysis, collection/purchases/CSV, and versions
pages. It seeds the existing twelve-card, hundred-card, and forty-card-category
decks with locally generated artwork; purchase history is not copied from real
holdings. External network/DNS is blocked and no actual provider is used.

For manual validation, check deck create/search/add/edit/import/export/delete,
build allocation, Stats, version compare/restore/duplicate, and synthetic
collection purchase/CSV workflows at 1440x900 and 390x844. Include blocked browser
storage, delayed/failed responses, the hundred-card fixture, a full 40-row
category, and an added 41st row.

Deck-action icons are locally vendored from Lucide v0.468.0; their license is
included in
[../luigi_web/core/static/icons/lucide/LICENSE](../luigi_web/core/static/icons/lucide/LICENSE).
No runtime CDN is used.