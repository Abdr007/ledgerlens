"""Evaluation harness — field-level accuracy and anomaly precision/recall.

Spec §7/§8: *"`eval/` runs the labelled set and prints field-level accuracy —
your resume numbers come from here."*

What it does, in order:

1. Materialises `eval/testset/` as **real files** (PDFs and degraded scans) with a
   `labels.json` of ground truth computed from the generator's specs, never read
   back from the rendered document.
2. Pushes every file through the **real pipeline** in dependency order, so the
   duplicate detector sees the same history the product would.
3. Scores each field with a type-aware comparison — money to the cent, dates as
   calendar dates, vendor names normalised the way the duplicate detector
   normalises them — and scores anomalies as precision / recall / F1 against the
   labels.

Numbers printed here are the numbers that belong on a resume, and the report
records which model produced them so a live figure is never confused with an
offline-baseline one.

    python eval/run_eval.py                 # score the labelled set
    python eval/run_eval.py --regenerate    # rebuild the documents first
    python eval/run_eval.py --json out.json # machine-readable report
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

_API_ROOT = Path(__file__).resolve().parent.parent / "apps" / "api"
sys.path.insert(0, str(_API_ROOT))

from sqlalchemy import text  # noqa: E402

from app.core.bootstrap import init_schema  # noqa: E402
from app.core.db import dispose_engine, init_engine, transaction  # noqa: E402
from app.core.logging import configure_logging  # noqa: E402
from app.core.settings import get_settings  # noqa: E402
from app.core.tracing import get_tracer  # noqa: E402
from app.deps import get_claude_client  # noqa: E402
from app.devtools.corpus import build_eval_corpus  # noqa: E402
from app.devtools.documents import degrade_to_scan, render_invoice_pdf  # noqa: E402
from app.models.enums import AnomalyType  # noqa: E402
from app.models.schemas import parse_date, parse_money  # noqa: E402
from app.pipeline.anomaly import normalise_vendor  # noqa: E402
from app.pipeline.orchestrator import PipelineOrchestrator  # noqa: E402

TESTSET_DIR = Path(__file__).resolve().parent / "testset"
RESULTS_DIR = Path(__file__).resolve().parent / "results"
LABELS_PATH = TESTSET_DIR / "labels.json"

SCALAR_FIELDS = (
    "vendor",
    "invoice_number",
    "issue_date",
    "due_date",
    "subtotal",
    "tax",
    "total",
    "currency",
    "payment_terms",
)
MONEY_FIELDS = frozenset({"subtotal", "tax", "total"})
DATE_FIELDS = frozenset({"issue_date", "due_date"})
_CENT = Decimal("0.01")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _norm_text(value: Any) -> str:
    return " ".join(str(value).strip().lower().split()) if value is not None else ""


def field_matches(name: str, expected: Any, observed: Any) -> bool:
    """Type-aware comparison.

    Money is compared to the cent rather than as a string, so `1200` and
    `"1,200.00"` agree. Vendor names go through the same normalisation the
    duplicate detector uses, so `GULF METALS L.L.C.` and `Gulf Metals LLC` agree —
    marking those wrong would understate accuracy for a difference the product
    itself treats as identical.
    """
    if expected in (None, "") and observed in (None, ""):
        return True
    if expected in (None, "") or observed in (None, ""):
        return False

    if name in MONEY_FIELDS:
        left, right = parse_money(expected), parse_money(observed)
        if left is None or right is None:
            return False
        return abs(left - right) <= _CENT

    if name in DATE_FIELDS:
        left_date, right_date = parse_date(expected), parse_date(observed)
        return left_date is not None and left_date == right_date

    if name == "vendor":
        return normalise_vendor(str(expected)) == normalise_vendor(str(observed))

    if name == "invoice_number":
        return _norm_text(expected).replace(" ", "") == _norm_text(observed).replace(" ", "")

    return _norm_text(expected) == _norm_text(observed)


def score_line_items(expected: list[dict[str, Any]], observed: list[dict[str, Any]]) -> tuple[int, int]:
    """Positionally matched line items: (matched, expected_total).

    A line counts only when description, quantity and amount all agree — a
    partial row is not a usable row for an accounts-payable team.
    """
    matched = 0
    for index, want in enumerate(expected):
        if index >= len(observed):
            break
        got = observed[index]
        if (
            _norm_text(want.get("description")) == _norm_text(got.get("description"))
            and field_matches("qty_num", parse_money(want.get("qty")), parse_money(got.get("qty")))
            and field_matches("amount", want.get("amount"), got.get("amount"))
        ):
            matched += 1
    return matched, len(expected)


@dataclass
class FieldTally:
    correct: int = 0
    total: int = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


@dataclass
class DocumentResult:
    filename: str
    status: str
    latency_ms: int
    cost_usd: float
    lane: str | None
    correct_fields: int
    total_fields: int
    line_items_matched: int
    line_items_expected: int
    expected_anomalies: set[str]
    observed_anomalies: set[str]
    mismatches: list[str] = field(default_factory=list)
    unreadable: bool = False
    #: "generated" (this repository rendered it) or "real" (it did not). Scored
    #: separately, because one number over both would describe neither.
    population: str = "generated"

    @property
    def accuracy(self) -> float:
        return self.correct_fields / self.total_fields if self.total_fields else 0.0


# ---------------------------------------------------------------------------
# Test set materialisation
# ---------------------------------------------------------------------------


def materialise_testset(*, force: bool) -> list[dict[str, Any]]:
    """Write the labelled documents to disk and return their manifest."""
    TESTSET_DIR.mkdir(parents=True, exist_ok=True)
    corpus = build_eval_corpus()

    if LABELS_PATH.exists() and not force:
        manifest: list[dict[str, Any]] = json.loads(LABELS_PATH.read_text())
        if all((TESTSET_DIR / entry["filename"]).exists() for entry in manifest):
            return manifest

    manifest = []
    for item in corpus:
        pdf = render_invoice_pdf(item.spec)
        payload = degrade_to_scan(pdf) if item.as_scan else pdf
        (TESTSET_DIR / item.filename).write_bytes(payload)
        manifest.append(
            {
                "filename": item.filename,
                "content_type": "image/jpeg" if item.as_scan else "application/pdf",
                "is_scan": item.as_scan,
                "expected_anomalies": sorted(str(a) for a in item.expected_anomalies),
                "tags": list(item.spec.tags),
                "ground_truth": item.spec.ground_truth(),
            }
        )

    LABELS_PATH.write_text(json.dumps(manifest, indent=2))
    return manifest


PRIVATE_DIR = TESTSET_DIR / "private"
PRIVATE_LABELS = PRIVATE_DIR / "labels.json"


def load_private_manifest() -> list[dict[str, Any]]:
    """Real documents, scored from outside version control.

    The generated corpus establishes that the pipeline is correct on the format it
    was built for. It cannot say anything about invoices this repository did not
    author — one renderer, one layout, and the offline parser's patterns written
    against the captions that renderer prints.

    A real invoice cannot be committed to answer that. It carries a vendor, an
    address, a tax number, line items and the recipient's own details, and this
    repository is public; `labels.json` would publish the contents in plaintext
    even where the file itself is ignored. So real documents live here, outside
    git, and are scored as a **separate population**. Averaging them together with
    the generated set would produce a single number describing neither.

    Absent directory means absent population — `make eval` stays reproducible for
    anyone who clones this.
    """
    if not PRIVATE_LABELS.exists():
        return []
    entries: list[dict[str, Any]] = json.loads(PRIVATE_LABELS.read_text())
    missing = [e["filename"] for e in entries if not (PRIVATE_DIR / e["filename"]).exists()]
    if missing:
        raise SystemExit(
            f"{PRIVATE_LABELS} references files that are not present: {', '.join(missing)}"
        )
    return entries


def _population_scores(results: list[DocumentResult]) -> dict[str, tuple[int, int, int]]:
    """(correct_fields, total_fields, documents) per population."""
    scores: dict[str, tuple[int, int, int]] = {}
    for result in results:
        correct, total, docs = scores.get(result.population, (0, 0, 0))
        scores[result.population] = (
            correct + result.correct_fields,
            total + result.total_fields,
            docs + 1,
        )
    return scores


def _public_document_entries(results: list[DocumentResult]) -> list[dict[str, Any]]:
    """Per-document rows for the persisted report.

    The report is committed; the private documents deliberately are not. A real
    invoice's *filename* can name a supplier, and `mismatches` quote extracted
    values verbatim — so for the real population both are withheld and the row
    carries scores under a positional alias instead. Nothing is lost in practice:
    the console printed the full detail during the run, which is where a mismatch
    gets debugged, and that output is not written anywhere.
    """
    entries: list[dict[str, Any]] = []
    private_seen = 0
    for result in results:
        public = result.population == "generated"
        if not public:
            private_seen += 1
        entries.append(
            {
                "filename": result.filename if public else f"private-{private_seen:02d}",
                "population": result.population,
                "status": result.status,
                "lane": result.lane,
                "field_accuracy": round(result.accuracy, 4),
                "line_items": f"{result.line_items_matched}/{result.line_items_expected}",
                "expected_anomalies": sorted(result.expected_anomalies),
                "observed_anomalies": sorted(result.observed_anomalies),
                "unreadable_in_mode": result.unreadable,
                "mismatches": result.mismatches if public else [],
            }
        )
    return entries


def _blocked_expectations(
    results: list[DocumentResult],
    manifest: list[dict[str, Any]],
    min_history_for_zscore: int,
) -> dict[str, set[str]]:
    """Labelled anomalies whose supporting evidence this run could not read.

    Returns `{filename: {anomaly_type, ...}}`. These are excluded from recall so
    the metric reflects detector quality rather than which lanes were configured.
    """
    # Every scored document, both populations. Keyed on filename because that is
    # what a DocumentResult carries; a private entry with a labelled anomaly used
    # to miss this map entirely and raise KeyError below.
    truth_by_file = {entry["filename"]: entry for entry in manifest}
    readable_names = {r.filename for r in results if not r.unreadable}
    blocked: dict[str, set[str]] = {}

    for position, result in enumerate(results):
        if result.unreadable or not result.expected_anomalies:
            continue
        entry = truth_by_file[result.filename]
        vendor = entry["ground_truth"].get("vendor")
        priors = [r for r in results[:position]]

        for kind in result.expected_anomalies:
            if kind == str(AnomalyType.AMOUNT_ZSCORE):
                same_vendor_readable = sum(
                    1
                    for prior in priors
                    if prior.filename in readable_names
                    and truth_by_file[prior.filename]["ground_truth"].get("vendor") == vendor
                )
                if same_vendor_readable < min_history_for_zscore:
                    blocked.setdefault(result.filename, set()).add(kind)
            elif kind == str(AnomalyType.DUPLICATE):
                # The pair is emitted original-then-copy, so the original is the
                # most recent readable prior from the same vendor.
                original_readable = any(
                    prior.filename in readable_names
                    and truth_by_file[prior.filename]["ground_truth"].get("vendor") == vendor
                    for prior in priors
                )
                if not original_readable:
                    blocked.setdefault(result.filename, set()).add(kind)
    return blocked


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def _reset_ledger() -> None:
    async with transaction() as session:
        await session.execute(
            text(
                "TRUNCATE anomalies, extractions, llm_traces, audit_log, failed_jobs, documents "
                "RESTART IDENTITY CASCADE"
            )
        )


async def run_eval(*, regenerate: bool, keep_ledger: bool) -> dict[str, Any]:
    settings = get_settings()
    configure_logging("ERROR")

    manifest = materialise_testset(force=regenerate)
    private = load_private_manifest()
    documents = [(TESTSET_DIR, entry, "generated") for entry in manifest]
    documents += [(PRIVATE_DIR, entry, "real") for entry in private]
    engine = init_engine(settings)
    await init_schema(engine)
    if not keep_ledger:
        await _reset_ledger()

    client = get_claude_client()
    orchestrator = PipelineOrchestrator(client=client, settings=settings, tracer=get_tracer())

    banner = f"{len(manifest)} generated"
    if private:
        banner += f" + {len(private)} real"
    print(f"\nLedgerLens evaluation — {banner} labelled documents")
    print(f"mode={client.mode}  router={settings.model_router}  extractor={settings.model_extractor}")
    print("=" * 100)

    tallies: dict[str, FieldTally] = {name: FieldTally() for name in SCALAR_FIELDS}
    results: list[DocumentResult] = []
    started = time.perf_counter()

    for source_dir, entry, population in documents:
        payload = (source_dir / entry["filename"]).read_bytes()
        outcome = await orchestrator.ingest(
            data=payload,
            filename=entry["filename"],
            declared_content_type=entry["content_type"],
        )
        await orchestrator.process(
            document_id=outcome.document_id,
            data=payload,
            filename=entry["filename"],
            media_type=outcome.media_type,
        )

        async with transaction() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT d.status, d.latency_ms, d.cost_usd, d.lane, "
                        "  e.vendor, e.invoice_number, e.issue_date, e.due_date, e.subtotal, "
                        "  e.tax, e.total, e.currency, e.payment_terms, e.line_items "
                        "FROM documents d LEFT JOIN extractions e ON e.document_id = d.id "
                        "WHERE d.id = :id"
                    ),
                    {"id": outcome.document_id},
                )
            ).one()
            flags = [
                str(value)
                for (value,) in (
                    await session.execute(
                        text("SELECT anomaly_type FROM anomalies WHERE document_id = :id"),
                        {"id": outcome.document_id},
                    )
                ).all()
            ]

        truth = entry["ground_truth"]
        mismatches: list[str] = []
        field_scores: list[tuple[str, bool]] = []
        correct = 0
        for name in SCALAR_FIELDS:
            expected = truth.get(name)
            observed = getattr(row, name, None)
            matched = field_matches(name, expected, observed)
            if matched:
                correct += 1
            else:
                mismatches.append(f"{name}: expected {expected!r}, got {observed!r}")
            field_scores.append((name, matched))

        matched_lines, expected_lines = score_line_items(
            truth.get("line_items", []), list(row.line_items or [])
        )

        # A scan has no text layer. Without a vision model there is nothing to
        # read, and the pipeline correctly routes it to NEEDS_REVIEW rather than
        # inventing fields. Scoring that as an extraction error would blame the
        # extractor for a capability the run was not configured to have, so it is
        # counted separately and called out explicitly.
        unreadable = (
            client.mode == "offline" and bool(entry["is_scan"]) and row.vendor is None
        )

        # Tally only documents this run could actually read.
        if not unreadable:
            for name, matched in field_scores:
                tallies[name].total += 1
                if matched:
                    tallies[name].correct += 1

        results.append(
            DocumentResult(
                filename=entry["filename"],
                status=row.status,
                latency_ms=int(row.latency_ms or 0),
                cost_usd=float(row.cost_usd or 0),
                lane=row.lane,
                correct_fields=correct,
                total_fields=len(SCALAR_FIELDS),
                line_items_matched=matched_lines,
                line_items_expected=expected_lines,
                expected_anomalies=set(entry["expected_anomalies"]),
                observed_anomalies=set(flags),
                mismatches=mismatches,
                unreadable=unreadable,
                population=population,
            )
        )

    elapsed = time.perf_counter() - started

    # ---- Report ---------------------------------------------------------
    print(f"{'document':24} {'status':13} {'lane':7} {'fields':>8} {'lines':>8} {'ms':>6}  anomalies")
    print("-" * 100)
    for result in results:
        flags = ",".join(sorted(result.observed_anomalies)) or "-"
        print(
            f"{result.filename:24} {result.status:13} {result.lane or '-':7} "
            f"{result.correct_fields}/{result.total_fields:<6} "
            f"{result.line_items_matched}/{result.line_items_expected:<6} "
            f"{result.latency_ms:>6}  {flags}"
        )
        for problem in result.mismatches:
            print(f"{'':24} └─ {problem}")

    readable = [r for r in results if not r.unreadable]
    unreadable = [r for r in results if r.unreadable]

    total_fields = sum(r.total_fields for r in readable)
    correct_fields = sum(r.correct_fields for r in readable)
    total_lines = sum(r.line_items_expected for r in readable)
    matched_lines = sum(r.line_items_matched for r in readable)

    # Anomaly scoring uses readable documents only, and additionally discounts
    # expectations whose *evidence* was unreadable. A duplicate cannot be matched
    # against an original the run could not read, and a per-vendor z-score needs a
    # minimum number of readable priors before it is even computed. Counting those
    # as misses would measure the absent vision model, not the detector.
    blocked = _blocked_expectations(
        results, manifest + private, settings.min_history_for_zscore
    )

    true_positive = sum(len(r.expected_anomalies & r.observed_anomalies) for r in readable)
    false_positive = sum(len(r.observed_anomalies - r.expected_anomalies) for r in readable)
    false_negative = sum(
        len(r.expected_anomalies - r.observed_anomalies - blocked.get(r.filename, set()))
        for r in readable
    )
    precision = true_positive / (true_positive + false_positive) if (true_positive + false_positive) else 1.0
    recall = true_positive / (true_positive + false_negative) if (true_positive + false_negative) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    blocked_expectations = sum(len(r.expected_anomalies) for r in unreadable) + sum(
        len(kinds) for kinds in blocked.values()
    )
    latencies = sorted(r.latency_ms for r in results)
    p95 = latencies[min(int(len(latencies) * 0.95), len(latencies) - 1)] if latencies else 0
    total_cost = sum(r.cost_usd for r in results)

    print("=" * 100)
    if unreadable:
        names = ", ".join(r.filename for r in unreadable)
        print(
            f"\n{len(unreadable)} document(s) could not be read in this mode and are"
            f" excluded from scoring:\n  {names}\n"
            "  These are scans with no text layer. The pipeline routed them to"
            " NEEDS_REVIEW rather than\n  inventing fields, which is the correct"
            " behaviour. Configure ANTHROPIC_API_KEY to score them\n  through the"
            " Claude vision lane."
        )
    print("\nFIELD-LEVEL ACCURACY" + (f" (over {len(readable)} readable documents)" if unreadable else ""))
    for name in SCALAR_FIELDS:
        tally = tallies[name]
        bar = "#" * int(tally.accuracy * 24)
        print(f"  {name:16} {tally.accuracy:7.1%}  {tally.correct:>2}/{tally.total:<2} {bar}")
    overall = correct_fields / total_fields if total_fields else 0.0
    line_accuracy = matched_lines / total_lines if total_lines else 0.0
    print(f"  {'-' * 16}")
    print(f"  {'OVERALL':16} {overall:7.1%}  {correct_fields}/{total_fields}")
    print(f"  {'line items':16} {line_accuracy:7.1%}  {matched_lines}/{total_lines}")

    populations = _population_scores(readable)
    if len(populations) > 1:
        # Deliberately not averaged. A combined figure over documents this
        # repository rendered and documents it did not would describe neither, and
        # the real number is the one worth quoting.
        print("\n  by population")
        for name, (correct, total, docs) in sorted(populations.items()):
            share = correct / total if total else 0.0
            print(f"    {name:12} {share:7.1%}  {correct}/{total}  over {docs} document(s)")

    print("\nANOMALY DETECTION")
    print(f"  precision {precision:6.1%}   recall {recall:6.1%}   F1 {f1:6.1%}")
    print(
        f"  true positives {true_positive}, false positives {false_positive}, "
        f"false negatives {false_negative}"
    )
    if blocked_expectations:
        detail = "; ".join(
            f"{name}: {', '.join(sorted(kinds))} (evidence unreadable in this mode)"
            for name, kinds in sorted(blocked.items())
        )
        print(
            f"  {blocked_expectations} labelled anomaly/anomalies were not scorable in this mode"
        )
        if detail:
            print(f"    {detail}")

    print("\nCOST & LATENCY")
    print(f"  documents        {len(results)}")
    print(f"  mean latency     {sum(latencies) / len(latencies):.0f} ms" if latencies else "")
    print(f"  p95 latency      {p95} ms")
    print(f"  total cost       ${total_cost:.4f}")
    print(f"  cost/document    ${total_cost / len(results):.5f}" if results else "")
    print(f"  wall clock       {elapsed:.1f}s")

    if client.mode == "offline":
        print(
            "\nNOTE: these are OFFLINE BASELINE numbers from the deterministic\n"
            "rule-based extractor. Set ANTHROPIC_API_KEY and re-run to measure the\n"
            "Claude vision + tool-use pipeline. Both are recorded in the report."
        )

    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": client.mode,
        "router_model": settings.model_router,
        "extractor_model": settings.model_extractor,
        "documents": len(results),
        "documents_scored": len(readable),
        "documents_unreadable_in_mode": [r.filename for r in unreadable],
        "populations": {
            name: {
                "documents": docs,
                "correct_fields": correct,
                "total_fields": total,
                "field_accuracy": round(correct / total, 4) if total else 0.0,
            }
            for name, (correct, total, docs) in _population_scores(readable).items()
        },
        "field_accuracy": {name: round(tallies[name].accuracy, 4) for name in SCALAR_FIELDS},
        "overall_field_accuracy": round(overall, 4),
        "line_item_accuracy": round(line_accuracy, 4),
        "anomaly": {
            # Keyed on filename, so a private document would be named here too.
            "blocked_by_mode": {
                name: sorted(kinds)
                for name, kinds in blocked.items()
                if name in {r.filename for r in results if r.population == "generated"}
            },
            "blocked_by_mode_private": sum(
                len(kinds)
                for name, kinds in blocked.items()
                if name not in {r.filename for r in results if r.population == "generated"}
            ),
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "true_positives": true_positive,
            "false_positives": false_positive,
            "false_negatives": false_negative,
        },
        "latency_ms": {
            "mean": round(sum(latencies) / len(latencies), 1) if latencies else 0,
            "p95": p95,
        },
        "cost_usd": {
            "total": round(total_cost, 6),
            "per_document": round(total_cost / len(results), 6) if results else 0,
        },
        "per_document": _public_document_entries(results),
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamped = RESULTS_DIR / f"eval-{client.mode}-{date.today().isoformat()}.json"
    stamped.write_text(json.dumps(report, indent=2))
    print(f"\nReport written to {stamped.relative_to(Path.cwd()) if stamped.is_relative_to(Path.cwd()) else stamped}\n")

    await dispose_engine()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Score LedgerLens on the labelled test set.")
    parser.add_argument("--regenerate", action="store_true", help="Rebuild the documents first.")
    parser.add_argument(
        "--keep-ledger",
        action="store_true",
        help="Do not truncate the ledger first (scores against existing history).",
    )
    parser.add_argument("--json", type=Path, help="Also write the report to this path.")
    args = parser.parse_args()

    report = asyncio.run(run_eval(regenerate=args.regenerate, keep_ledger=args.keep_ledger))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2))

    # Exit non-zero if the labelled anomalies were not all found: a regression in
    # the detector should fail CI, not print a lower number nobody reads.
    return 0 if report["anomaly"]["recall"] >= 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
