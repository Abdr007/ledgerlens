"""The canonical document corpora.

Two sets, both generated deterministically from a fixed seed so that a run today
and a run next month score the same documents:

* **Seed corpus** — 30 historical invoices across 6 vendors (spec §8), so the
  vendor-spend chart and the per-vendor z-scores are meaningful the first time the
  dashboard is opened, plus a planted near-duplicate pair so the anomaly demo
  always fires.
* **Evaluation corpus** — 10 labelled invoices (spec §8) including two
  near-duplicates and one inflated amount, with the expected anomaly for each
  document recorded up front so `eval/run_eval.py` can compute precision/recall
  rather than eyeballing the output.

Vendors have distinct, stable profiles — currency, payment terms, typical spend
band — because a per-vendor z-score is meaningless if every vendor looks alike.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from app.devtools.documents import InvoiceSpec, LineSpec
from app.models.enums import AnomalyType
from app.pipeline.anomaly import InvoiceRecord, ScreeningConfig, screen_invoice

SEED: Final = 20260729
SEED_CORPUS_SIZE: Final = 30
EVAL_CORPUS_SIZE: Final = 10


@dataclass(frozen=True, slots=True)
class VendorProfile:
    name: str
    address: str
    trn: str
    currency: str
    payment_terms: str
    vat_rate: Decimal
    catalogue: tuple[tuple[str, Decimal], ...]
    qty_range: tuple[int, int]
    lines_range: tuple[int, int]


VENDORS: Final[tuple[VendorProfile, ...]] = (
    VendorProfile(
        name="Gulf Metals L.L.C.",
        address="Warehouse 14, Industrial Area 12\nAl Quoz, Dubai, UAE",
        trn="100234567800003",
        currency="AED",
        payment_terms="Net 30",
        vat_rate=Decimal("0.05"),
        catalogue=(
            ("Steel plate 10mm x 2400", Decimal("420.00")),
            ("Galvanised bolts M12 (box of 100)", Decimal("95.50")),
            ("Welding consumables set", Decimal("812.00")),
            ("Aluminium channel 6m", Decimal("268.75")),
            ("Stainless sheet 1.5mm", Decimal("534.20")),
        ),
        qty_range=(3, 14),
        lines_range=(3, 5),
    ),
    VendorProfile(
        name="Aurora Systems FZ-LLC",
        address="Office 1108, Dubai Internet City\nDubai, UAE",
        trn="100987654300003",
        currency="AED",
        payment_terms="Net 45",
        vat_rate=Decimal("0.05"),
        catalogue=(
            ("Managed endpoint support (per seat)", Decimal("148.00")),
            ("Cloud backup retention 1TB", Decimal("612.40")),
            ("Security monitoring tier 2", Decimal("1_284.00")),
            ("Network hardware maintenance", Decimal("376.90")),
        ),
        qty_range=(2, 9),
        lines_range=(2, 4),
    ),
    VendorProfile(
        name="Nakheel Facilities Management",
        address="Building 3, Business Bay\nDubai, UAE",
        trn="100445566700003",
        currency="AED",
        payment_terms="Net 15",
        vat_rate=Decimal("0.05"),
        catalogue=(
            ("Daily office cleaning (per week)", Decimal("362.50")),
            ("Deep carpet treatment", Decimal("845.00")),
            ("HVAC filter replacement", Decimal("218.30")),
            ("Pest control visit", Decimal("176.45")),
        ),
        qty_range=(1, 6),
        lines_range=(2, 4),
    ),
    VendorProfile(
        name="Cedar Print House",
        address="Shop 7, Al Karama\nDubai, UAE",
        trn="100112233400003",
        currency="AED",
        payment_terms="Due on receipt",
        vat_rate=Decimal("0.05"),
        catalogue=(
            ("Business cards (500)", Decimal("142.75")),
            ("A2 poster print", Decimal("63.40")),
            ("Bound report, colour", Decimal("88.90")),
            ("Roll-up banner", Decimal("311.20")),
        ),
        qty_range=(1, 8),
        lines_range=(2, 3),
    ),
    VendorProfile(
        name="Meridian Logistics DMCC",
        address="JAFZA One, Tower A\nJebel Ali, Dubai, UAE",
        trn="100778899100003",
        currency="USD",
        payment_terms="Net 30",
        vat_rate=Decimal("0.00"),
        catalogue=(
            ("Sea freight FCL 40ft", Decimal("2_845.00")),
            ("Customs clearance handling", Decimal("418.60")),
            ("Inland haulage per container", Decimal("736.25")),
            ("Warehousing per pallet/month", Decimal("54.80")),
        ),
        qty_range=(1, 5),
        lines_range=(2, 4),
    ),
    VendorProfile(
        name="Palm Office Supplies Trading",
        address="Warehouse 22, Al Qusais\nDubai, UAE",
        trn="100556677800003",
        currency="AED",
        payment_terms="Net 30",
        vat_rate=Decimal("0.05"),
        catalogue=(
            ("A4 paper (box of 5 reams)", Decimal("78.30")),
            ("Toner cartridge, mono", Decimal("246.90")),
            ("Ergonomic chair", Decimal("689.50")),
            ("Desk organiser set", Decimal("41.25")),
        ),
        qty_range=(2, 12),
        lines_range=(2, 4),
    ),
)

_BILL_TO: Final = "Northline Trading FZE\nJebel Ali Free Zone\nDubai, UAE"
_ROUND_GUARD: Final = Decimal("500")
# Smallest recurring-basket quantity, so a +-1 unit step stays a small move.
_MIN_BASKET_QTY: Final = 12
# How far a month may drift from the vendor baseline. A ~12% uniform band is
# ~7% standard deviation, which keeps an ordinary month under the 2-sigma
# z-score threshold while still looking like real month-to-month movement.
_SPEND_BAND: Final = Decimal("0.12")


@dataclass(frozen=True, slots=True)
class CorpusItem:
    """A document plus the anomalies it is expected to raise."""

    spec: InvoiceSpec
    as_scan: bool
    expected_anomalies: frozenset[AnomalyType]

    @property
    def stem(self) -> str:
        safe = self.spec.invoice_number.replace("/", "-")
        return f"{safe}{'-scan' if self.as_scan else ''}"

    @property
    def extension(self) -> str:
        return ".jpg" if self.as_scan else ".pdf"

    @property
    def filename(self) -> str:
        return f"{self.stem}{self.extension}"


def _build_lines(profile: VendorProfile, rng: random.Random) -> tuple[LineSpec, ...]:
    """A one-off invoice with quantities drawn freely across the vendor's range."""
    count = rng.randint(*profile.lines_range)
    chosen = rng.sample(profile.catalogue, min(count, len(profile.catalogue)))
    return tuple(
        LineSpec(
            description=description,
            qty=Decimal(rng.randint(*profile.qty_range)),
            unit_price=price,
        )
        for description, price in chosen
    )


def _vendor_basket(profile: VendorProfile, rng: random.Random) -> tuple[LineSpec, ...]:
    """The recurring basket a vendor bills month after month.

    Real suppliers invoice a fairly stable book of business, and the per-vendor
    z-score detector depends on that: if every historical invoice were drawn
    independently across the full quantity range, ordinary months would sit two
    standard deviations from the mean and the review queue would fill with noise
    that buries the findings that matter.
    """
    count = rng.randint(*profile.lines_range)
    chosen = rng.sample(profile.catalogue, min(count, len(profile.catalogue)))
    low, high = profile.qty_range
    # Sit in the upper half of the vendor's range. A recurring basket needs enough
    # units that a one-unit month-to-month step is a small relative move; anchored
    # at a quantity of 3, +-1 is a 33% swing and the spend band stops being a band.
    base = max(low + (high - low) * 2 // 3, _MIN_BASKET_QTY)
    return tuple(
        LineSpec(description=description, qty=Decimal(base), unit_price=price)
        for description, price in chosen
    )


def _vary_basket(
    basket: tuple[LineSpec, ...], rng: random.Random, *, spread: float = 0.12
) -> tuple[LineSpec, ...]:
    """Month-to-month wobble around the recurring basket, bounded at ±`spread`."""
    varied: list[LineSpec] = []
    for line in basket:
        delta = Decimal(str(1.0 + rng.uniform(-spread, spread)))
        # With a basket anchored at _MIN_BASKET_QTY a +-12% swing spans three
        # integers, so quantising produces genuine variety. Forcing a step
        # instead would delete the midpoint and collapse the space to 2^n
        # combinations, which collide within a five-month history.
        qty = (line.qty * delta).quantize(Decimal("1"))
        varied.append(LineSpec(line.description, max(Decimal("1"), qty), line.unit_price))
    return tuple(varied)


def _make_spec(
    profile: VendorProfile,
    *,
    number: str,
    issued: date,
    rng: random.Random,
    lines: tuple[LineSpec, ...] | None = None,
    payment_terms: str | None = None,
    forced_total: Decimal | None = None,
    note: str | None = None,
    tags: tuple[str, ...] = (),
) -> InvoiceSpec:
    terms = payment_terms or profile.payment_terms
    due_days = {"Net 15": 15, "Net 30": 30, "Net 45": 45}.get(terms, 0)
    body = lines if lines is not None else _build_lines(profile, rng)
    return InvoiceSpec(
        vendor=profile.name,
        vendor_address=profile.address,
        trn=profile.trn,
        invoice_number=number,
        issue_date=issued,
        due_date=issued + timedelta(days=due_days),
        bill_to=_BILL_TO,
        lines=body,
        currency=profile.currency,
        vat_rate=profile.vat_rate,
        payment_terms=terms,
        forced_total=forced_total,
        note=note,
        tags=tags,
    )


def _near_duplicate_lines(
    lines: tuple[LineSpec, ...], vat_rate: Decimal, target_gap: Decimal = Decimal("0.004")
) -> tuple[LineSpec, ...]:
    """Rebuild an item list so the invoice total lands `target_gap` above the original.

    A fixed multiplier on the last line is unreliable: how far it moves the total
    depends on that line's share of the invoice, which is exactly how the eval pair
    once drifted past the detector's 1% window. Solving for the unit price from the
    desired *total* delta keeps the pair inside the window whatever the basket
    looks like.
    """
    subtotal = sum((line.amount for line in lines), start=Decimal("0"))
    total = subtotal * (Decimal("1") + vat_rate)
    tail = lines[-1]
    if tail.qty <= 0 or total <= 0:
        return lines

    delta_unit = (total * target_gap) / (tail.qty * (Decimal("1") + vat_rate))
    bumped_price = (tail.unit_price + delta_unit).quantize(Decimal("0.01"))
    return (*lines[:-1], LineSpec(tail.description, tail.qty, bumped_price))


def _assert_within_duplicate_window(
    original: InvoiceSpec, copy: InvoiceSpec, tolerance: Decimal = Decimal("0.01")
) -> None:
    """Fail loudly at build time if a planted pair could not actually be detected."""
    gap = abs(copy.total - original.total) / max(copy.total, original.total)
    assert gap <= tolerance, (
        f"planted duplicate {copy.invoice_number} is {gap:.2%} from "
        f"{original.invoice_number}, outside the {tolerance:.0%} detector window"
    )
    days = abs((copy.issue_date - original.issue_date).days)
    assert days <= 7, f"planted duplicate is {days} days apart, outside the 7-day window"


def _distinct_variation(
    profile: VendorProfile,
    basket: tuple[LineSpec, ...],
    rng: random.Random,
    seen_totals: set[Decimal],
    attempts: int = 40,
) -> tuple[LineSpec, ...]:
    """Draw a month's basket whose total is new for this vendor and not round.

    Repeating a total across a vendor's history is the tell that data was
    generated rather than observed, and a coincidentally round total would fire
    the round-number detector on a document the corpus labelled clean. Redrawing
    until both hold keeps the seeded ledger credible without widening the spend
    band enough to disturb the z-score baseline.
    """

    def probe_total(lines: tuple[LineSpec, ...]) -> Decimal:
        return InvoiceSpec(
            vendor=profile.name,
            vendor_address=profile.address,
            trn=profile.trn,
            invoice_number="PROBE",
            issue_date=date(2026, 1, 1),
            due_date=date(2026, 1, 31),
            bill_to=_BILL_TO,
            lines=lines,
            currency=profile.currency,
            vat_rate=profile.vat_rate,
            payment_terms=profile.payment_terms,
        ).total

    baseline = probe_total(basket)
    best: tuple[Decimal, tuple[LineSpec, ...]] | None = None

    for _ in range(attempts):
        lines = _vary_basket(basket, rng)
        total = probe_total(lines)
        if total in seen_totals:
            continue
        drift = abs(total - baseline) / baseline if baseline else Decimal("0")
        if best is None or drift < best[0]:
            best = (drift, lines)
        if drift <= _SPEND_BAND and total % _ROUND_GUARD != 0:
            seen_totals.add(total)
            return lines

    # Nothing landed inside the band; take the closest distinct draw so the
    # history still varies rather than repeating a total.
    chosen = best[1] if best is not None else _vary_basket(basket, rng)
    seen_totals.add(probe_total(chosen))
    return chosen


def _is_suspiciously_round(spec: InvoiceSpec) -> bool:
    """Guard: an unintentionally round total would fire the round-number detector."""
    return spec.total >= _ROUND_GUARD and spec.total % _ROUND_GUARD == 0


def _resample_until_not_round(
    profile: VendorProfile, rng: random.Random, attempts: int = 24
) -> tuple[LineSpec, ...]:
    """Redraw line items until the total is not an exact multiple of 500.

    A total that happens to land on a round figure would fire the round-number
    detector, which would then count as a false positive against a document the
    corpus labelled clean.
    """
    for _ in range(attempts):
        lines = _build_lines(profile, rng)
        probe = InvoiceSpec(
            vendor=profile.name,
            vendor_address=profile.address,
            trn=profile.trn,
            invoice_number="PROBE",
            issue_date=date(2026, 1, 1),
            due_date=date(2026, 1, 31),
            bill_to=_BILL_TO,
            lines=lines,
            currency=profile.currency,
            vat_rate=profile.vat_rate,
            payment_terms=profile.payment_terms,
        )
        if not _is_suspiciously_round(probe):
            return lines
    return _build_lines(profile, rng)


def _draw_seed_corpus(reference: date | None, seed: int) -> list[CorpusItem]:
    """Exactly 30 historical invoices across 6 vendors (spec §8).

    Two of the thirty are a **planted near-duplicate pair**: the last Gulf Metals
    invoice is rewritten as a re-issue of the one before it, so the anomaly demo
    fires on a freshly seeded database without inflating the corpus past the
    thirty documents the spec calls for.

    History is dated backwards from `reference` so the dashboard always shows a
    recent, plausible six months of activity.
    """
    today = reference or date(2026, 7, 20)
    rng = random.Random(seed)
    items: list[CorpusItem] = []

    per_vendor = SEED_CORPUS_SIZE // len(VENDORS)
    for vendor_index, profile in enumerate(VENDORS):
        basket = _vendor_basket(profile, rng)
        seen_totals: set[Decimal] = set()
        for occurrence in range(per_vendor):
            # Oldest first, roughly monthly, with a few days of natural jitter.
            # The ~22-day minimum gap keeps ordinary months well outside the
            # 7-day duplicate window.
            months_ago = per_vendor - occurrence
            issued = today - timedelta(days=months_ago * 30 + rng.randint(-4, 4))
            number = f"{profile.name[:2].upper()}-2026-{vendor_index + 1}{occurrence + 1:02d}"
            lines = _distinct_variation(profile, basket, rng, seen_totals)
            spec = _make_spec(
                profile, number=number, issued=issued, rng=rng, lines=lines, tags=("seed",)
            )
            items.append(CorpusItem(spec=spec, as_scan=False, expected_anomalies=frozenset()))

    # --- Plant the near-duplicate *inside* the thirty, by rewriting the most
    # recent Gulf Metals invoice as a re-issue of the one before it.
    metals_positions = [
        index for index, item in enumerate(items) if item.spec.vendor == VENDORS[0].name
    ]
    source_index, planted_index = metals_positions[-2], metals_positions[-1]
    source = items[source_index].spec

    planted_spec = _make_spec(
        VENDORS[0],
        number=f"{source.invoice_number}-R",
        # Inside the 7-day duplicate window, so the detector must catch it.
        issued=source.issue_date + timedelta(days=3),
        rng=rng,
        lines=_near_duplicate_lines(source.lines, VENDORS[0].vat_rate),
        note="Re-issued copy of a previously submitted invoice.",
        tags=("seed", "planted-duplicate"),
    )
    _assert_within_duplicate_window(source, planted_spec)
    items[planted_index] = CorpusItem(
        spec=planted_spec,
        as_scan=False,
        expected_anomalies=frozenset({AnomalyType.DUPLICATE}),
    )
    assert len(items) == SEED_CORPUS_SIZE
    return items


def _draw_eval_corpus(reference: date | None, seed: int) -> list[CorpusItem]:
    """10 labelled invoices: 2 near-duplicates and 1 inflated amount (spec §8).

    Order matters — a duplicate can only be detected against a document the
    pipeline has already seen, so the pair is emitted original-then-copy.
    """
    today = reference or date(2026, 7, 20)
    rng = random.Random(seed + 1)
    metals, aurora, nakheel, cedar = VENDORS[0], VENDORS[1], VENDORS[2], VENDORS[3]
    items: list[CorpusItem] = []

    # 1-4: Gulf Metals history on a stable recurring basket, so the inflated
    # invoice at the end has a tight baseline to stand out against.
    metals_basket = _vendor_basket(metals, rng)
    metals_totals: set[Decimal] = set()
    for index in range(4):
        issued = today - timedelta(days=(6 - index) * 21)
        items.append(
            CorpusItem(
                spec=_make_spec(
                    metals,
                    number=f"EV-GM-{index + 1:03d}",
                    issued=issued,
                    rng=rng,
                    lines=_distinct_variation(metals, metals_basket, rng, metals_totals),
                    tags=("eval", "normal"),
                ),
                # Mixed media: the vision lane must be exercised too.
                as_scan=index in {1, 3},
                expected_anomalies=frozenset(),
            )
        )

    # 5-6: two other vendors, clean.
    for index, profile in enumerate((nakheel, cedar)):
        items.append(
            CorpusItem(
                spec=_make_spec(
                    profile,
                    number=f"EV-{profile.name[:2].upper()}-{index + 1:03d}",
                    issued=today - timedelta(days=30 + index * 9),
                    rng=rng,
                    lines=_resample_until_not_round(profile, rng),
                    tags=("eval", "normal"),
                ),
                as_scan=index == 1,
                expected_anomalies=frozenset(),
            )
        )

    # 7: Aurora original — the first half of the near-duplicate pair.
    aurora_issue = today - timedelta(days=18)
    aurora_lines = _resample_until_not_round(aurora, rng)
    items.append(
        CorpusItem(
            spec=_make_spec(
                aurora,
                number="EV-AU-101",
                issued=aurora_issue,
                rng=rng,
                lines=aurora_lines,
                tags=("eval", "duplicate-original"),
            ),
            as_scan=False,
            expected_anomalies=frozenset(),
        )
    )

    # 8: Aurora near-duplicate — 4 days later, ~0.4% apart, new reference number.
    aurora_copy = _make_spec(
        aurora,
        number="EV-AU-102",
        issued=aurora_issue + timedelta(days=4),
        rng=rng,
        lines=_near_duplicate_lines(aurora_lines, aurora.vat_rate),
        note="Duplicate submission of the same engagement.",
        tags=("eval", "duplicate-copy"),
    )
    _assert_within_duplicate_window(items[-1].spec, aurora_copy)
    items.append(
        CorpusItem(
            spec=aurora_copy,
            as_scan=False,
            expected_anomalies=frozenset({AnomalyType.DUPLICATE}),
        )
    )

    # 9: broken arithmetic — must route to NEEDS_REVIEW, not be silently repaired.
    broken_lines = _resample_until_not_round(cedar, rng)
    broken = _make_spec(
        cedar,
        number="EV-CP-901",
        issued=today - timedelta(days=11),
        rng=rng,
        lines=broken_lines,
        tags=("eval", "arithmetic-defect"),
    )
    # The printed total disagrees with subtotal + tax by a wide margin, so the
    # deterministic validator must reject it rather than the model "fixing" it.
    broken = replace(broken, forced_total=broken.subtotal + broken.tax + Decimal("450.00"))
    items.append(CorpusItem(spec=broken, as_scan=False, expected_anomalies=frozenset()))

    # 10: inflated amount — same vendor as 1-4, roughly 8x their usual spend.
    baseline = sum(
        (item.spec.total for item in items if item.spec.vendor == metals.name),
        start=Decimal("0"),
    ) / Decimal(4)
    inflated_lines = (
        LineSpec("Steel plate 10mm x 2400", Decimal("96"), Decimal("420.00")),
        LineSpec("Stainless sheet 1.5mm", Decimal("41"), Decimal("534.20")),
    )
    items.append(
        CorpusItem(
            spec=_make_spec(
                metals,
                number="EV-GM-999",
                issued=today - timedelta(days=3),
                rng=rng,
                lines=inflated_lines,
                note=f"Baseline for this vendor is approximately {baseline:,.0f}.",
                tags=("eval", "inflated-amount"),
            ),
            as_scan=False,
            expected_anomalies=frozenset({AnomalyType.AMOUNT_ZSCORE}),
        )
    )

    return items


# ---------------------------------------------------------------------------
# Self-verification
# ---------------------------------------------------------------------------


def _unexpected_findings(items: list[CorpusItem]) -> list[str]:
    """Run the *real* detector over a corpus and report label violations.

    Statistical guarantees are fragile: with only four priors a vendor whose
    fifth invoice lands at the edge of an otherwise reasonable spend band can
    still exceed two standard deviations. Rather than hand-tuning constants until
    a particular seed happens to behave, the corpus is checked against the engine
    that will actually score it, and redrawn if it does not match its own labels.

    This also means the corpora self-heal if a detector threshold is ever tuned.
    """
    config = ScreeningConfig()
    records = [
        InvoiceRecord(
            document_id=uuid.uuid5(uuid.NAMESPACE_OID, item.spec.invoice_number),
            vendor=item.spec.vendor,
            invoice_number=item.spec.invoice_number,
            issue_date=item.spec.issue_date,
            total=item.spec.total,
            currency=item.spec.currency,
            payment_terms=item.spec.payment_terms,
            filename=item.filename,
        )
        for item in items
    ]

    violations: list[str] = []
    for index, (item, record) in enumerate(zip(items, records, strict=True)):
        # Documents are screened against only what the pipeline had already seen.
        observed = {
            finding.anomaly_type for finding in screen_invoice(record, records[:index], config)
        }
        expected = set(item.expected_anomalies)
        if observed != expected:
            violations.append(
                f"{item.spec.invoice_number}: expected {sorted(expected)}, got {sorted(observed)}"
            )
    return violations


def _build_verified(
    draw: Callable[[date | None, int], list[CorpusItem]],
    reference: date | None,
    attempts: int = 40,
) -> list[CorpusItem]:
    """Redraw with successive seeds until the corpus matches its own labels."""
    last: list[CorpusItem] = []
    for offset in range(attempts):
        last = draw(reference, SEED + offset * 101)
        if not _unexpected_findings(last):
            return last
    problems = "; ".join(_unexpected_findings(last))
    msg = f"Could not generate a corpus matching its labels after {attempts} draws: {problems}"
    raise RuntimeError(msg)


def build_seed_corpus(reference: date | None = None) -> list[CorpusItem]:
    """30 historical invoices across 6 vendors, verified against the real detector."""
    return _build_verified(_draw_seed_corpus, reference)


def build_eval_corpus(reference: date | None = None) -> list[CorpusItem]:
    """10 labelled invoices, verified against the real detector."""
    return _build_verified(_draw_eval_corpus, reference)
