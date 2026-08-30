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
bulk_refresh
price_history
```

SQLite foreign keys, WAL mode, busy timeouts, explicit write transactions, and
schema `user_version` are enabled by the repository adapter.

## Features

- Game switcher for MTG, Pokemon TCG, and Riftbound.
- Searchable, set-filtered catalog browser.
- Rich card inspector with provider artwork, double-faced card switching,
  rules, stats, legalities, printings, prices, trusted card links, local deck
  usage, and collection records.
- Visual Art & printings picker with transactional Use this printing actions
  for existing deck slots and collection records. Swaps preserve deck
  quantity/board/category and collection quantity/variant/acquisition metadata;
  an existing destination printing is merged safely.
- Scryfall-style advanced local search for rules/type text, colors, commander
  identity, mana and numeric stats, games, format legality, set/group, rarity,
  all current Scryfall card criteria, prices, artist/flavor/lore, language,
  sorting, unique cards/art, and preferred printing. Extra cards are imported
  by new Scryfall refreshes but remain hidden unless Include Extras or an
  extra-specific criterion is selected.
- Manual catalog records for games without a configured provider.
- Deck create, edit, archive, delete, tag, and commander workflows.
- Mainboard, sideboard, maybeboard, and commander card groups.
- Persistent Table and grouped overlapping Stacks deck views.
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
- Collection quantity, foil, condition, acquired price, currency, and notes.
- Owned-versus-required counts in each deck.
- Card and deck value-history SVG charts.
- Optional draw.io notes stored as validated XML.

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