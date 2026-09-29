# TerminalWeave

TerminalWeave is a dependency-light Python engine for modelling day-to-day container-terminal operations. It covers marine access, berth use, quay cranes, yard placement, truck-gate appointments, customs holds, reefer monitoring, hazardous-goods segregation, equipment maintenance, rail blocks, demurrage billing and transshipment connections.

The package is intentionally usable without a database or web framework. Domain services expose ordinary Python APIs, while the persistence layer supplies an append-only event store and snapshots for embedding in larger systems.

## Quick start

```bash
python -m pip install -e ".[test]"
python -m terminalweave.cli demo
python -m pytest -q
```

Run the small HTTP adapter with `terminalweave serve`. It exposes `GET /health`,
`POST /tide/clearance`, and `POST /billing/demurrage`. The other packages are
library components intended to be composed by an embedding application.

## Unified billing settlement

`billing.settlement` settles one business event in a single ordered pass via
`SettlementService.settle`, instead of settling rates, discounts, credit and
payments by hand in several places:

1. each consumed item is rated with the handling tariff *as of the business
   occurrence* (`RateSchedule` is append-only and effective-dated, so later
   rate changes never rewrite history) and frozen into immutable `RatedLine`s
   sealed with a SHA-256 detail checksum;
2. the single best eligible discount is applied;
3. the discounted balance is authorized against the customer's credit (only up
   to available credit);
4. the remainder is covered by payments, reusing the reconciliation matching
   convention (a payment referencing the business event is consumed first).

The pipeline stages against scratch values and commits the credit
authorization, payment applications and settlement record in one critical
section, so a failure leaves no half-written bill and the credit account is
untouched. Every request carries an `idempotency_key`: retries (including
concurrent retries and retries whose payload changed) return the identical
settlement, identified deterministically from the key. Supporting HTTP routes:
`POST /billing/rates`, `POST /billing/discounts`,
`POST /billing/credit-accounts`, `POST /billing/settle`, and
`GET /billing/settlements/{id}`.

## Design principles

- domain calculations are deterministic for identical inputs;
- time values are timezone-aware UTC datetimes;
- operational state transitions are explicit and validated;
- safety constraints fail closed when required information is absent;
- money uses `Decimal`;
- core domain modules depend only on the Python standard library.

## Main packages

`marine`, `cranes`, `yard`, `gate`, `customs`, `reefer`, `hazmat`, `equipment`, `rail`, `billing`, `transshipment`, `planning`, `persistence`, `reporting`, and `api`.
