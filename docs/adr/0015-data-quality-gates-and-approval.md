# ADR 0015 — Data-quality gates and quality-configuration approval

- **Status:** proposed (implementation and this record awaiting review)
- **Date:** 2026-10-10

Each section is marked **implemented** (true of the code at this commit, enforced by
runtime checks and tests), **procedural** (relies on people and review, not code) or
**proposed / open** (not decided until this ADR is accepted). Residual limits are stated
where they apply.

## Context

M2.1 freezes market-data ranges as dataset snapshots (format `sentinel-dataset/v1`) that
research may consume only as a `VerifiedDataset`. The snapshot design is cited in code and
tests as ADR 0014, but **no ADR 0014 file exists in the repository at this commit**; its
existence and status must be checked before this ADR is accepted, and nothing here should be
read as recording what ADR 0014 decides.

A snapshot is only as trustworthy as its rows, so every row in a range must be judged by
objective quality gates before freezing, and that judgement must be reproducible on every
later load. Two risks drive the design: research contamination (thresholds loosened until
data passes, or changed after strategy or backtest results are seen), and procedural error
(a configuration treated as approved without a human having reviewed it against real data).

Step 3 implemented the gates (commits 07fdb48, b342863, 22c063b, 3738822, 90493eb, f9c9d29,
ce5fc82). A review on 2026-10-10 found approval enforced only by a `status` field that
callers could alter in memory, demonstrated three ways:

- **V1:** `dataclasses.replace(qc_v1, status="APPROVED")` kept the provisional hash and
  `attest()` accepted it.
- **V2:** a directly built `QualityConfig` with an arbitrary `sha256` was accepted.
- **V3:** `dataclasses.replace(report, config=<approved config>)` kept the report's issuer
  key and was accepted, so the attestation named a configuration the rows were never
  judged under.

`freeze` and `load` also trusted whatever configuration objects the caller supplied. No
production code froze or loaded a snapshot, so none of this was reachable in production.

A first fix (commit 31d0a86) closed those three. An independent review of it then found
three gaps, closed by the hardening described in §3 and §4:

- **F1:** a subclass of `QualityConfigRegistry` that overrode `__init__` and `approved()`
  passed the store's `isinstance` check and got an unapproved configuration through
  `freeze`'s trust checks.
- **F2:** the architecture scan missed aliases of `load_quality_registry`, `getattr` and
  attribute access to the registry key, `functools.partial`, and subclasses.
- **F3:** a report relabelled with another configuration, with its canonical JSON and hash
  recomputed, was internally consistent and `attest()` accepted it, although the same rows
  FAILED under the configuration it named.

## Decision

### 1. Quality gates detect; they never clean (implemented)

Engine `sentinel-quality/v1` (`sentinel.domain.quality.QualityChecker`) judges one
`[from, to)` range of one series in a single ordered pass, in constant memory. It returns
PASS or FAIL with complete counts, metrics, bounded examples and the largest gaps. It never
drops, alters, re-times, interpolates or smooths a row; a real extreme move stays in the data
and is at most flagged.

- **Integrity gates (FAIL on any occurrence):** rows not strictly increasing; outside the
  range; off the timeframe grid; not closed at evaluation time; another symbol or timeframe;
  a non-positive price; inconsistent OHLC; ask below bid at open or close; ask high or low
  below bid high or low; negative volume; spread at open or close above the symbol cap.
- **Series gates (FAIL):** no expected bars; completeness below the minimum; a gap longer
  than the maximum; more bars than allowed where the calendar says the market is closed.
- **Flags (never FAIL):** a price jump between bars, a single-bar range, zero volume.

### 2. Fail-closed requirements (implemented)

The verdict is FAIL when any gate fails. A configuration without thresholds for the series'
timeframe or symbol is an error, not a pass. An empty range cannot pass, and rows the digest
cannot accept are refused outright. `sentinel data-gate` exits 0 only on PASS (1 FAIL,
2 invalid configuration or environment, 3 evaluation error). No entry point accepts a skip,
force or override argument (architecture test, INV-DATA-QUALITY-GATED).

### 3. Configuration and report identity (implemented)

A configuration's identity is the SHA-256 of its canonical content: version, status,
engine, calendar, `report.max_listed` and every threshold, with decimals normalised.
Comments, key order, line endings and decimal scale do not affect it. The loader refuses
floats, duplicate keys, unknown or missing keys, non-finite numbers, and any engine or
calendar other than v1. A threshold change is a new version in a new file, and the registry
refuses two files with the same version or the same content. The algorithm is unchanged by
this work; `qc_v1`'s hash and both golden report hashes are pinned by tests.

- **Every construction checks the identity.** `QualityConfig.__post_init__` recomputes the
  hash from the content and refuses a mismatch, whether the object comes from
  `from_mapping`, direct construction or `dataclasses.replace`. That closes V1 and V2. It
  also checks every field (version pattern, status, engine, calendar, `max_listed`,
  fixed-grid timeframes, symbol format, threshold types) and keeps private read-only copies
  of the threshold mappings.
- **Consumers re-verify; a type is not trust.** `QualityConfig` cannot be subclassed
  (`__init_subclass__` raises). `QualityConfig.verify()` re-runs every check, including each
  threshold's own checks, without changing the object. `attest` and `issue_verified_dataset`
  call it, so an object allocated with `object.__new__` (which never ran `__post_init__`),
  with a forged hash or invalid thresholds, is refused.
- **Thresholds validate themselves.** `TimeframeThresholds` and `SymbolThresholds` apply the
  same range checks in `__post_init__` as the YAML parser does, and accept only finite
  `Decimal` values and exact integers.
- **Reports are internally consistent, not authoritative.** `QualityReport.__post_init__`
  rebuilds the canonical JSON from the report's own fields (spec, digest, configuration,
  `as_of`, result), requires it to equal `canonical_json`, and requires `sha256` to be its
  hash. Replacing any one field is refused. Replacing several consistently (F3) still yields
  a well-formed report object; this check proves integrity, not that the rows were judged.
  `QualityReport` cannot be subclassed.
- **Attestation is inseparable from judging (F3).** `attest(spec, config, as_of, candles)`
  takes rows, never a report. It re-verifies the configuration, refuses anything but
  APPROVED, judges and digests the rows itself in one pass, and issues an attestation only
  for a PASS. No report object, however built or relabelled, can become an attestation, and
  this does not depend on which module calls `attest`. `QualityAttestation` is not a
  dataclass (so `dataclasses.replace` does not apply), cannot be subclassed, is read-only
  (copying fails), and its key-checked constructor takes only the report `attest` just
  produced. `freeze` uses the attestation it obtained from that call, never one supplied by
  a caller. Verified load re-judges the stored rows itself.

  Alternatives considered: tracking issued report objects (rejected: global mutable state in
  the pure domain, a weak-reference slot, and object identity becoming part of
  correctness), and a keyed seal over reports (rejected: a secret held in the domain, the
  same private-key tier as the issuer keys, more machinery).

A matching hash proves integrity, not trust: a configuration built in memory with a
correctly recomputed hash is honest about its content but is still never trusted (§4).

### 4. Approval authority and evidence

**Mechanism (implemented).** A configuration is approved only if both of these hold:

1. its content declares `status: APPROVED`, which is part of its hashed identity; and
2. `config/quality/approvals/` holds exactly one approval record naming that exact
   `config_sha256`, with exactly the keys `config_version`, `config_sha256`, `approved_by`,
   `approved_on` (a date) and `evidence`. Records are parsed with the configuration loader's
   strictness (no floats, no duplicate or unknown keys, every key present and non-empty).

`load_quality_registry` refuses to load **at all** if an APPROVED configuration has no
record or more than one; if a record names a hash that matches no configuration (unknown, or
stale after an edit); if a record names a provisional configuration; if a record's version
differs from its configuration's; or if any record is malformed. With no `approvals/`
directory, nothing is approved. `registry.approved(sha256)` returns the registry's own
object for an approved hash and refuses anything else.

**Registry integrity (implemented, F1).** Runtime checks are authoritative:

- The constructor requires the loader's private key and sets a seal to that key.
  `verify()` and `approved()` refuse unless the object is exactly `QualityConfigRegistry`
  and carries that seal, so an instance allocated with `__new__`, or sealed with anything
  else, authorises nothing.
- The class cannot be subclassed (`__init_subclass__` raises, for `class` statements and
  three-argument `type()` alike), so `approved()` cannot be overridden.
- It is not a dataclass (`dataclasses.replace` does not apply) and is read-only after
  construction (copying and pickling fail).
- `PostgresDatasetStore` requires the exact type and calls `verify()` when it is
  constructed, so a forged registry is refused before `freeze` or `load` can reach the
  database.

**Trusted origin (implemented; partly enforced by review).** The approvals directory is
always the `approvals/` subdirectory of the configuration directory; it cannot be chosen
separately. `load_quality_registry` still accepts a directory argument so tests can load
temporary directories; nothing at run time stops a caller passing one. That production code
calls it only without arguments, which means the shipped `config/quality/`, is enforced by
the architecture scan (F2), not by the runtime.

The scan (`tests/architecture/test_quality_gated.py`) flags, outside the module that owns
them:

- any use of `load_quality_registry` other than a direct call without arguments (aliases,
  `functools.partial`, `getattr` by name, import under another name);
- any mention of the private issuance mechanisms (`_REGISTRY_KEY`, the registry seal,
  `_load_approvals`, `_match_approvals`, `_REPORT_KEY`, `_ATTESTATION_KEY`), including
  attribute access and string names;
- subclasses of the guarded classes, `__new__` allocation of them, three-argument `type()`,
  and building a `QualityConfigRegistry`.

Each evasion has a regression fixture. A static scan cannot prove the absence of a
deliberate bypass (string building, `exec`); it is a second line of defence.

**Store enforcement (implemented).** `PostgresDatasetStore` takes `registry=`; there is no
longer a caller-supplied mapping of configurations.

- `freeze` refuses without a registry. It also refuses any configuration that
  `registry.approved()` does not return, or that is not the very object the registry
  returned. All of this happens before the database is touched, so nothing is written and
  no range is sealed. It then calls `attest` with the registry's object, which judges the
  rows under the transaction's lock.
- `load` refuses without a registry and verifies only under `registry.approved()` for the
  snapshot's recorded configuration hash. `issue_verified_dataset` also requires an APPROVED
  configuration.
- `judge`, used by `sentinel data-gate`, needs no registry and accepts provisional
  configurations: a report is a diagnostic, not authority.

**Decision (proposed).** That this mechanism is how approval is established before the
first real freeze. The approval authority is the human repository owner, acting through a
reviewed pull request into protected `main`. This is procedural trust (git history and
review), not cryptographic trust. With zero required approvals on `main` and a single
contributor, it is in practice self-review with an audit trail, not separation of duties.

**Rejected for now:**

- *Status field only:* approval would be an unattributed edit.
- *Approval stored in the database under the `HumanRiskAdmin` capability:* needs a schema
  migration and step-up re-authentication, neither of which exists yet. The implementation
  plan lists "OIDC + WebAuthn step-up" under M14 (`docs/09-implementation-plan.md`), and
  `docs/07-deployment-and-api.md` describes step-up for dangerous endpoints. It is a
  candidate successor.
- *Signed approvals:* key management is out of scope.

**Approval evidence is procedural and hash-bound (procedural).** A record binds a decision
to one exact configuration hash. `approved_by`, `approved_on` and `evidence` are free text
that the loader checks only for form: it verifies no identity, no signature and no
existence of the evidence. Who approved, and on what evidence, is established by git
history and pull-request review. This is not cryptographic identity verification.

**Residual limits (recorded).** Python cannot stop deliberate forgery by code running in the
same process. Code that names a private issuer key (for example, sealing a hand-built
registry with `_REGISTRY_KEY`), monkeypatches a class or module, or uses `exec` can still
forge objects or redirect the registry directory. The runtime checks stop accidental and
convenient bypasses, including every route found in review (V1–V3, F1, F3). The
architecture scan and review are relied on for deliberate ones; the scan is not proof.

### 5. Why qc_v1 is provisional and uncalibrated (implemented)

`config/quality/qc_v1.yaml` is `PROVISIONAL_UNCALIBRATED`, SHA-256
`96163e820dd54ed14b144de11d54d06d383ac745c7b264919af19ee3353bf27b`. It has **no approval
record**, and the shipped registry refuses to approve it. Its thresholds are conservative
proposals written before any real dataset was examined. They are **not calibrated on
licensed real data**, and passing synthetic tests is no evidence of calibration.

Before approval:

1. Run `sentinel data-gate` on real history from an approved source.
2. Classify every FAIL and flag as bad data, a legitimate market event, a calendar
   assumption, or a threshold-design mistake.
3. Propose any change with its justification.
4. Obtain human approval as in §4.

Thresholds are never tuned to make data pass and never changed after seeing strategy or
backtest results. Approving `qc_v1` changes its hash, so reports judged under the
provisional hash cannot be used to freeze.

### 6. Freeze, load and immutability (implemented)

`freeze` runs in one transaction that holds the series' exclusive advisory lock. Within it,
`freeze`:

- checks the registered source;
- requires the range to end at or before the newest stored bar;
- judges and digests the same rows in one pass, at the transaction's own time;
- attests only a PASS under a trusted, approved configuration;
- stores the dataset hash, the report hash and the configuration hash.

The database then refuses inserts inside the range, and refuses any update, delete or
truncate of snapshots and sealed rows.

`load` reads the record and rows in one read-only REPEATABLE READ transaction. It resolves
the snapshot's own configuration by hash through the trusted registry (never the current
configuration) and re-judges at the snapshot's `created_at`. It issues a `VerifiedDataset`
only if the row count, rows hash, dataset hash and report hash all match exactly. There is
no override. Engine v1's output is pinned by golden reports: any change to gates, calendar,
formatting or layout is a new engine version, never an edit to v1.

### 7. Calendar and threshold limitations (recorded; open)

- **Calendar `fx-ny1700-xmas-newyear/v1`:**
  - Closed from Friday 17:00 to Sunday 17:00 New York time, and for the whole New York
    calendar day on 25 December and 1 January.
  - Knows no other holidays and models no early closes or thin holiday eves.
  - New York time comes from the IANA time-zone database: system data on Linux, the
    `tzdata` package on Windows.
- **M1 completeness and gaps:** providers may emit no M1 bar for a minute without ticks, so
  the completeness and gap thresholds may FAIL on genuinely quiet periods, such as the
  17:00 New York rollover. This is unverified until real data is examined.
- **Decimal precision:** jump and range percentages use the process's default decimal
  precision.
- **Snapshot size and lock duration:** snapshots are held in memory on load (limit
  1,000,000 rows), and freezing a large range holds the series lock for its whole duration.

None of these were changed by this work.

## Consequences

- **No snapshot can be frozen today, by design.** That requires a configuration that is
  approved with real-data evidence and has a matching record.
- **Configurations and records are append-only in practice.** Removing or editing a
  configuration file, or its approval record, makes every snapshot judged under it
  unloadable (fail closed).
- **Tests build their own registries.** Tests that freeze or load must load a registry from
  a temporary directory holding their configurations and approval records
  (`tests/support/quality.py`). Engine and attestation tests judge without a registry.
- **Existing hashes are unchanged.** Configuration and report hashes are untouched, and
  there are no stored snapshots to migrate.
- **Report checks add a small cost.** Each report rebuilds its canonical JSON once more,
  which is bounded by `max_listed` and independent of row count. `attest` judges the rows
  once, as before, so freeze still makes a single pass.
- **Freeze reports a quality FAIL before an oversized range.** The row-count limit is checked
  on the attestation, after judging; either way nothing is written.

## Open questions

1. Accept §4 as the approval mechanism, or require the database plus `HumanRiskAdmin` before
   the first freeze?
2. The approval record's fields, including how `approved_by` identifies a person in a public
   repository.
3. The calendar assumptions and every threshold value in §5 and §7, to be settled with
   licensed real data.
4. Fix the decimal precision locally in engine v1 now (its output is unchanged at the
   default precision), or leave it for a future v2?
5. The 1,000,000-row snapshot limit, and how long freeze holds the series lock.
6. ADR 0014 (dataset snapshots), which code and this ADR cite: write it, or confirm its
   status, before this ADR is accepted.
7. The comment in `qc_v1.yaml` says approval "sets `status: APPROVED`". Under §4, approval
   also needs a record. The comment was left untouched in this change (comments do not
   affect the hash); update it with the approval change.
