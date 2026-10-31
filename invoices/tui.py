"""Invoice desk: auto-process data/inbox/ with live models; --demo uses offline examples."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from dotenv import load_dotenv
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
)

from .cli import ROOT, components, model_arguments
from .pipeline import Pipeline
from .review import decide
from .store import Store
from .verification import STRATEGIES

STATUS = {
    "queued": ("INBOX", "#a7b7d0"),
    "processing": ("PROCESSING", "#6dd5ed"),
    "awaiting_approval": ("VP APPROVAL", "#eac477"),
    "hold": ("ON HOLD", "#eac477"),
    "reviewing": ("REVIEW INTERRUPTED", "#eac477"),
    "rejected": ("REJECTED", "#ef9295"),
    "paid": ("PAID · MOCK", "#83d6b0"),
    "duplicate": ("ALREADY PAID", "#83d6b0"),
}
DEMO_FILES = [
    "data/demo/approval.pdf",
    "data/invoices/invoice_1013.pdf",
    "data/invoices/invoice_1006.csv",
    "data/invoices/invoice_1004.json",
]
INPUT_FORMATS = {".pdf", ".txt", ".json", ".csv", ".xml"}


def discover_inputs(args):
    roots = args.files or ([ROOT / name for name in DEMO_FILES] if args.demo else [args.inbox])
    files = []
    for root in roots:
        path = Path(root).expanduser().resolve()
        if path.is_dir():
            files.extend(
                sorted(
                    p.resolve()
                    for p in path.iterdir()
                    if p.is_file()
                    and not p.name.startswith(".")
                    and p.suffix.lower() in INPUT_FORMATS
                )
            )
        elif path.is_file() and path.suffix.lower() in INPUT_FORMATS:
            files.append(path)
        else:
            raise ValueError(f"Not a supported invoice file or folder: {path}")
    return list(dict.fromkeys(files))


def file_hash(path):
    try:
        with Path(path).open("rb") as file:
            return hashlib.file_digest(file, "sha256").hexdigest()
    except OSError:
        return None


class HoldReason(ModalScreen[str | None]):
    BINDINGS: ClassVar = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    HoldReason { align: center middle; background: $background 75%; }
    HoldReason > Vertical { width: 66; height: auto; padding: 1 2; border: round #eac477; background: #162235; }
    HoldReason Input { margin: 1 0; }
    HoldReason Horizontal { height: 3; align-horizontal: right; }
    HoldReason Button { margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Hold this invoice", classes="dialog-title")
            yield Label("Explain what needs resolving before this invoice can proceed.")
            yield Input(
                placeholder="e.g. Waiting for purchase order confirmation", id="hold-reason"
            )
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("Place on hold", id="confirm-hold", variant="warning")

    def on_mount(self):
        self.query_one(Input).focus()

    def action_cancel(self):
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def cancel_button(self):
        self.dismiss(None)

    @on(Button.Pressed, "#confirm-hold")
    @on(Input.Submitted)
    def submit(self):
        reason = self.query_one(Input).value.strip()
        if reason:
            self.dismiss(reason)
        else:
            self.notify("Enter a reason for the hold.", severity="warning")


class InvoiceDesk(App):
    TITLE = "ACME / Invoice desk"
    CSS = """
    Screen { background: #0b1320; color: #dce6f2; }
    Header { background: #162235; }
    #context { height: 2; padding: 0 2; color: #aebed0; background: #101c2b; }
    #workspace { height: 1fr; }
    #queues { width: 38; min-width: 28; padding: 0 1; }
    .queue { border: round #30465e; margin-bottom: 1; height: 1fr; min-height: 6; }
    #vp { border: round #9a7b40; }
    #holds { border: round #9a7b40; }
    #detail { width: 1fr; padding: 0 1 0 0; }
    #decision { height: auto; min-height: 4; max-height: 9; padding: 1 2; border: round #30465e; }
    #flow { height: 2; padding: 0 1; }
    #tabs { height: 1fr; }
    TabPane { padding: 0; }
    #document-pair { height: 1fr; min-height: 9; }
    .document { width: 1fr; border: round #30465e; border-title-color: #9aacc1; padding: 0 1; }
    #source-scroll { margin-right: 1; }
    #source-text { height: auto; }
    #extracted { height: auto; }
    #checks { height: 11; border: round #30465e; border-title-color: #9aacc1; margin-top: 1; }
    #activity-scroll { border: round #30465e; padding: 1 2; }
    #actions { height: 3; margin-top: 1; }
    #actions Button { margin-right: 1; min-width: 12; }
    Screen.compact #context { height: 1; }
    Screen.compact #queues { width: 29; }
    Screen.compact .queue { min-height: 4; margin-bottom: 0; }
    Screen.compact #decision { min-height: 3; max-height: 5; padding: 0 1; }
    Screen.compact #flow { display: none; }
    Screen.compact #document-pair { min-height: 4; }
    Screen.compact #checks { height: 6; }
    Screen.compact #actions { display: none; }
    Footer { background: #162235; }
    DataTable { background: #101c2b; }
    DataTable > .datatable--header { background: #1c2e43; color: #aebed0; }
    Button { background: #203348; }
    """
    BINDINGS: ClassVar = [
        Binding("a", "approve", "Approve"),
        Binding("d", "decline", "Decline"),
        Binding("h", "hold", "Hold"),
        Binding("r", "reprocess", "Reprocess"),
        Binding("p", "process_all", "Process inbox"),
        Binding("f", "follow", "Follow", show=False),
        Binding("1", 'queue("vp")', "VP", show=False),
        Binding("2", 'queue("holds")', "Holds", show=False),
        Binding("3", 'queue("other")', "All other", show=False),
        Binding("e", "export_result", "Export"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, args, paths, *, stage_delay=0):
        super().__init__()
        self.args = args
        self.paths = [Path(p).resolve() for p in paths]
        self.store = Store(args.state_db)
        self.processing_config = {
            "processing_version": "4-provider-capacity",
            "max_model_calls": args.max_model_calls,
            "request_settings": {
                name: os.getenv(name)
                for name in (
                    "MODEL_CHUNK_SOURCE_BYTES",
                    "MODEL_CHUNK_PAGES",
                    "MODEL_REQUEST_BYTES",
                    "MODEL_CONTEXT_TOKENS",
                    "MODEL_REQUEST_IMAGES",
                    "MODEL_OUTPUT_TOKENS",
                )
            },
            "mode": args.mode,
            "strategy": args.strategy,
            "extractor_model": args.extractor_model if args.mode == "live" else None,
            "verifier_model": args.verifier_model if args.mode == "live" else None,
            "extractor_url": args.extractor_url if args.mode == "live" else None,
            "verifier_url": args.verifier_url if args.mode == "live" else None,
            "threshold": args.approval_threshold,
            "default_currency": args.default_currency,
        }
        saved = self.store.latest_runs()
        self.input_hashes = {}
        self.results = {str(p): self.saved_or_queued(p, saved.get(str(p))) for p in self.paths}
        self.selected = str(self.paths[0]) if self.paths else None
        self.follow_processing = not args.demo
        self.busy = False
        self.stage_delay = stage_delay
        self.worker_handle = None

    def saved_or_queued(self, path, saved):
        digest = file_hash(path)
        self.input_hashes[str(path)] = digest
        old_hash = (saved or {}).get("input_hash") or ((saved or {}).get("draft") or {}).get(
            "source_hash"
        )
        if (
            saved
            and digest
            and old_hash == digest
            and saved.get("processing_config") == self.processing_config
        ):
            return saved
        return {
            "status": "queued",
            "reason": "Source changed; queued for reprocessing."
            if saved
            else "Queued for processing.",
            "events": [],
        }

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="context")
        with Horizontal(id="workspace"):
            with Vertical(id="queues"):
                yield DataTable(id="vp", classes="queue", cursor_type="row", show_header=False)
                yield DataTable(id="holds", classes="queue", cursor_type="row", show_header=False)
                yield DataTable(id="other", classes="queue", cursor_type="row", show_header=False)
            with Vertical(id="detail"):
                yield Static(id="decision")
                yield Static(id="flow")
                with TabbedContent(id="tabs"):
                    with TabPane("Invoice & checks", id="invoice-tab"):
                        with Horizontal(id="document-pair"):
                            with VerticalScroll(id="source-scroll", classes="document"):
                                yield Static(id="source-text")
                            with VerticalScroll(id="extracted-scroll", classes="document"):
                                yield Static(id="extracted")
                        yield DataTable(id="checks", cursor_type="row", zebra_stripes=True)
                    with (
                        TabPane("Activity & evidence", id="activity-tab"),
                        VerticalScroll(id="activity-scroll"),
                    ):
                        yield Static(id="activity")
                with Horizontal(id="actions"):
                    yield Button("Approve [a]", id="approve", variant="success")
                    yield Button("Decline [d]", id="decline", variant="error")
                    yield Button("Hold [h]", id="hold", variant="warning")
        yield Footer()

    def on_mount(self):
        self.screen.set_class(self.size.height < 38 or self.size.width < 110, "compact")
        for name in ("vp", "holds", "other"):
            self.query_one("#" + name, DataTable).add_column("Invoice", width=32)
        checks = self.query_one("#checks", DataTable)
        checks.add_columns("Result", "Check", "Expected", "Observed", "Reason")
        checks.border_title = "Deterministic checks"
        self.query_one("#source-scroll").border_title = "Source document · page / region"
        self.query_one("#extracted-scroll").border_title = "Extracted invoice"
        label = "OFFLINE FIXTURES" if self.args.mode == "offline" else "LIVE MODELS"
        self.query_one("#context", Static).update(
            Text(
                f"{'DEMO  •  ' if self.args.demo else ''}{label}  •  {self.args.strategy}  •  VP above ${float(self.args.approval_threshold):,.2f}  •  MOCK PAYMENTS\n"
                "Select a row + Enter  |  1 VP queue · 2 Holds · 3 Other  |  scroll panels for details"
            )
        )
        self.refresh_queues()
        self.show_detail()
        self.action_queue("vp", manual=False)
        if not self.args.no_autorun:
            self.call_after_refresh(self.action_process_all)

    def on_resize(self, event):
        self.screen.set_class(event.size.height < 38 or event.size.width < 110, "compact")

    def refresh_queues(self):
        groups = {"vp": [], "holds": [], "other": []}
        for path, result in self.results.items():
            status = result.get("status", "queued")
            group = (
                "vp"
                if status == "awaiting_approval"
                else "holds"
                if status in {"hold", "reviewing"}
                else "other"
            )
            groups[group].append((path, result))
        names = {"vp": "1 · VP approval", "holds": "2 · On hold", "other": "3 · Inbox / completed"}
        for group, rows in groups.items():
            table = self.query_one("#" + group, DataTable)
            table.clear()
            table.border_title = f"{names[group]}  ({len(rows)})"
            for path, result in rows:
                label, color = STATUS.get(result.get("status"), STATUS["queued"])
                invoice = (result.get("draft") or {}).get("invoice", {})
                name = invoice.get("number") or Path(path).name
                row = Text(
                    ("› " if path == self.selected else "  ") + f"{name}  ·  {label}", style=color
                )
                hint = result.get("reason") or ""
                if result.get("status") == "awaiting_approval":
                    hint = f"{invoice.get('currency') or ''} {invoice.get('total')} · awaiting decision"
                row.append("\n  " + hint[:30], style="#9aacc1")
                table.add_row(row, key=path, height=2)
                if path == self.selected:
                    table.move_cursor(row=len(table.rows) - 1)

    @on(DataTable.RowSelected)
    def select_invoice(self, event):
        if event.data_table.id not in {"vp", "holds", "other"}:
            return
        self.selected = event.row_key.value
        self.follow_processing = False
        self.refresh_queues()
        self.show_detail()

    def action_queue(self, name, manual=True):
        if manual:
            self.follow_processing = False
        table = self.query_one("#" + name, DataTable)
        table.focus()
        if table.ordered_rows:
            self.selected = table.ordered_rows[0].key.value
            table.move_cursor(row=0)
            self.refresh_queues()
            self.show_detail()

    def show_detail(self):
        if not self.selected:
            self.query_one("#decision", Static).update(
                Text(
                    f"INBOX EMPTY\nDrop PDF, TXT, JSON, CSV, or XML invoices into {self.args.inbox}\nPress p to scan and process new files."
                )
            )
            for name in ("approve", "decline", "hold"):
                self.query_one("#" + name, Button).disabled = True
            return
        result = self.results[self.selected]
        status = result.get("status", "queued")
        label, color = STATUS.get(status, STATUS["queued"])
        draft = result.get("draft") or {}
        invoice = draft.get("invoice") or {}
        text = Text()
        text.append(label + "  ", style=f"bold {color}")
        text.append(invoice.get("number") or Path(self.selected).name, style="bold")
        reason = result.get("reason") or "Processing source…"
        if status == "awaiting_approval":
            reason = f"Invoice total {invoice.get('currency') or ''} {invoice.get('total')} exceeds the ${float(self.args.approval_threshold):,.2f} approval threshold."
        elif reason == "Currency is absent; configure a documented default or clarify":
            reason = (
                "The invoice does not specify a currency. Confirm the currency before reprocessing."
            )
        text.append("\n" + reason)
        if status == "awaiting_approval":
            text.append("\n[a] Approve and pay   [d] Decline   [h] Hold with a reason", style=color)
        if status in {"hold", "reviewing"}:
            text.append(
                "\nResolve the issue, then [r] reprocess. Holds cannot be approved directly.",
                style=color,
            )
        self.query_one("#decision", Static).update(text)
        events = result.get("events", [])
        stages = {e["stage"] for e in events}
        flow = Text()
        for i, (stage, title) in enumerate(
            [
                ("source", "Read"),
                ("extraction", "Extract"),
                ("verification", "Verify"),
                ("validation", "Check"),
                ("approval", "Approve"),
                ("payment", "Pay"),
            ]
        ):
            if i:
                flow.append("  →  ", style="#536981")
            symbol, shade = ("● ", "#83d6b0") if stage in stages else ("○ ", "#536981")
            data = next((e["data"] for e in reversed(events) if e["stage"] == stage), {})
            if stage == "approval" and data.get("decision") == "pending":
                symbol, shade = "◷ ", "#eac477"
            if (
                stage == "validation"
                and any(c["outcome"] in {"reject", "hold"} for c in data.get("checks", []))
            ) or (stage == "approval" and data.get("decision") == "decline"):
                symbol, shade = "× ", "#ef9295"
            if stage == "verification" and data.get("verdict") in {"incorrect", "uncertain"}:
                symbol, shade = "! ", "#eac477"
            if status == "processing" and result.get("active_stage") == stage:
                symbol, shade = "◷ ", "#6dd5ed"
            flow.append(symbol + title, style=shade)
        self.query_one("#flow", Static).update(flow)
        source = next((e["data"] for e in events if e["stage"] == "source"), {})
        source_text = Text()
        self.query_one(
            "#source-scroll"
        ).border_title = f"{Path(self.selected).suffix[1:].upper()} · {Path(self.selected).name}"
        for region in source.get("regions", []):
            source_text.append(region["id"] + "  ", style="#6d93b8")
            source_text.append(region["text"] + "\n")
        if not source_text:
            source_text.append("Source text appears as processing begins.", style="dim")
        self.query_one("#source-text", Static).update(source_text)
        table = Table(box=None, expand=True, padding=(0, 1), show_header=False)
        table.add_column(style="#9eb3c9", ratio=1)
        table.add_column(ratio=2)
        if invoice:
            for key, title in [
                ("vendor", "Vendor"),
                ("date", "Issued"),
                ("due_date", "Due"),
                ("currency", "Currency"),
                ("total", "Total"),
            ]:
                value = invoice.get(key)
                table.add_row(
                    title,
                    Text(
                        str(value) if value is not None else "Missing",
                        style="white" if value is not None else "#eac477",
                    ),
                )
            table.add_row("Version", str(draft.get("version", 1)))
            table.add_row(
                "Verification", (result.get("verification") or {}).get("verdict", "Pending")
            )
            table.add_section()
            for i, item in enumerate(invoice.get("items", []), 1):
                table.add_row(
                    Text(f"{i}. {item.get('name') or 'Unnamed'}", style="bold"),
                    Text(
                        f"Qty {item.get('quantity')} × {item.get('unit_price')}\nAmount: {item.get('amount') or 'not stated'}"
                    ),
                )
        else:
            table.add_row("Waiting", "Extraction will appear here.")
        self.query_one("#extracted", Static).update(table)
        checks = self.query_one("#checks", DataTable)
        checks.clear()
        ordered = sorted(
            result.get("checks", []),
            key=lambda c: {"reject": 0, "hold": 1, "pass": 2, "unavailable": 3}[c["outcome"]],
        )
        failures = sum(c["outcome"] in {"reject", "hold"} for c in ordered)
        checks.border_title = f"Deterministic checks · {failures} failed / held"
        for c in ordered:
            color = {
                "reject": "#ef9295",
                "hold": "#eac477",
                "pass": "#83d6b0",
                "unavailable": "#8798ac",
            }[c["outcome"]]
            checks.add_row(
                Text(c["outcome"].upper(), style=color),
                c["rule"],
                c.get("expected") or "—",
                c.get("observed") or "—",
                c["reason"],
            )
        activity = Text()
        for e in events:
            activity.append(f"{e['at'][11:19]}  {e['stage'].upper()}\n", style="bold #6dd5ed")
            if e["stage"] == "source":
                activity.append(f"{len(e['data'].get('regions', []))} source regions\n\n")
            elif e["stage"] == "extraction":
                activity.append(f"Invoice draft version {e['data'].get('version')}\n\n")
            else:
                activity.append(json.dumps(e["data"], indent=2, ensure_ascii=False) + "\n\n")
        for field, citation in draft.get("citations", {}).items():
            activity.append(field + " → " + citation["region_id"] + "\n", style="bold #eac477")
            activity.append(citation["quote"] + "\n")
        self.query_one("#activity", Static).update(activity or "No events yet.")
        enabled = status == "awaiting_approval" and not self.busy
        for button in ("approve", "decline", "hold"):
            self.query_one("#" + button, Button).disabled = not enabled

    def _stage(self, path, event):
        result = self.results[path]
        result.setdefault("events", []).append(event)
        stage, data = event["stage"], event["data"]
        if stage == "chunk_progress":
            result["active_stage"] = data["stage"]
            pages = ", ".join(str(p) for p in data["pages"])
            result["reason"] = (
                f"{data['stage'].capitalize()}: pages {pages} — "
                f"{data['status']}; {data['completed']} chunks completed"
            )
            if path == self.selected:
                self.show_detail()
            return
        if stage == "stage_started":
            result["active_stage"] = data["stage"]
            result["reason"] = f"{data['stage'].capitalize()} in progress…"
            if path == self.selected:
                self.show_detail()
            return
        if stage == "extraction":
            result["draft"] = data
        elif stage == "verification":
            result["verification"] = data
        elif stage == "validation":
            result["checks"] = data["checks"]
        result["reason"] = f"{stage.replace('_', ' ').capitalize()}…"
        if path == self.selected:
            self.show_detail()

    def _completed(self, path, result):
        result["input_hash"] = self.input_hashes.get(path)
        result["processing_config"] = self.processing_config
        if result.get("run_id"):
            self.store.finish(result["run_id"], result)
        self.results[path] = result
        self.refresh_queues()
        self.show_detail()

    def _idle(self):
        self.busy = False
        if self.follow_processing:
            self.action_queue("vp", manual=False)
        self.show_detail()

    def _started(self, path):
        self.results[path] = {"status": "processing", "reason": "Reading source…", "events": []}
        if self.follow_processing:
            self.selected = path
        self.refresh_queues()
        self.show_detail()

    def action_follow(self):
        self.follow_processing = True
        active = next((p for p, r in self.results.items() if r.get("status") == "processing"), None)
        if active:
            self.selected = active
            self.show_detail()
        self.notify("Following each invoice as it runs.")

    def scan_inputs(self):
        discovered = discover_inputs(self.args)
        saved = self.store.latest_runs()
        for path in discovered:
            key = str(path)
            digest = file_hash(path)
            if key not in self.results or digest != self.input_hashes.get(key):
                self.results[key] = self.saved_or_queued(path, saved.get(key))
                if path not in self.paths:
                    self.paths.append(path)
        if self.selected is None and discovered:
            self.selected = str(discovered[0])
        self.refresh_queues()
        self.show_detail()

    def action_process_all(self):
        if self.busy:
            return
        try:
            self.scan_inputs()
        except (OSError, ValueError) as exc:
            self.notify(str(exc), severity="error")
            return
        paths = [p for p, r in self.results.items() if r.get("status") == "queued"]
        if not paths:
            self.notify("Inbox processed. Select an invoice and press r to reprocess.")
            return
        self.start_processing(paths)

    def action_reprocess(self):
        if not self.busy and self.selected:
            self.start_processing([self.selected])

    def start_processing(self, paths):
        self.busy = True
        for path in paths:
            self.input_hashes[path] = file_hash(path)
            self.results[path] = {
                "status": "queued",
                "reason": "Waiting to process…",
                "events": [],
            }
        self.refresh_queues()
        self.show_detail()
        self.worker_handle = self.process_paths(paths)

    @work(thread=True)
    def process_paths(self, paths):
        try:
            for path in paths:
                self.call_from_thread(self._started, path)
                extractor, verifier = components(self.args, self.args.strategy)

                def event(e, path=path):
                    self.call_from_thread(self._stage, path, e)
                    if self.stage_delay:
                        time.sleep(self.stage_delay)

                result = Pipeline(
                    extractor,
                    verifier,
                    self.store,
                    self.args.inventory_db,
                    threshold=self.args.approval_threshold,
                    default_currency=self.args.default_currency,
                    render=self.args.mode == "live",
                    on_event=event,
                ).run(path)
                self.call_from_thread(self._completed, path, result)
        except Exception as exc:  # noqa: BLE001 -- keep the terminal usable after worker failure
            self.call_from_thread(self.notify, str(exc), severity="error")
            for path in paths:
                if self.results[path].get("status") in {"processing", "queued"}:
                    self.call_from_thread(
                        self._completed, path, {"status": "hold", "reason": str(exc), "events": []}
                    )
        finally:
            self.call_from_thread(self._idle)

    def _pending(self):
        if self.busy or not self.selected:
            return False
        if self.results[self.selected].get("status") != "awaiting_approval":
            self.notify("Select an invoice in the VP approval queue first.", severity="warning")
            return False
        return True

    def action_approve(self):
        if self._pending():
            self.start_decision("approve")

    def action_decline(self):
        if self._pending():
            self.start_decision("decline")

    def action_hold(self):
        if not self._pending():
            return
        path = self.selected

        def held(reason):
            if reason and path == self.selected and self._pending():
                self.start_decision("hold", reason)

        self.push_screen(HoldReason(), held)

    def start_decision(self, decision, reason=""):
        self.busy = True
        self.show_detail()
        self.worker_handle = self.review_selected(self.selected, decision, reason)

    @work(thread=True)
    def review_selected(self, path, decision, reason):
        try:
            result = decide(
                self.store, self.results[path]["run_id"], decision, self.args.inventory_db, reason
            )
            self.call_from_thread(self._completed, path, result)
        except Exception as exc:  # noqa: BLE001 -- preserve persisted status on rejected actions
            self.call_from_thread(self.notify, str(exc), severity="error")
        finally:
            self.call_from_thread(self._idle)

    @on(Button.Pressed)
    def action_button(self, event):
        actions = {
            "approve": self.action_approve,
            "decline": self.action_decline,
            "hold": self.action_hold,
        }
        if event.button.id in actions:
            actions[event.button.id]()

    def action_export_result(self):
        if not self.selected:
            return
        result = self.results[self.selected]
        if not result.get("run_id"):
            self.notify("Process this invoice before exporting.")
            return
        target = ROOT / "runs" / f"{result['run_id']}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, indent=2) + "\n")
        self.notify(f"Saved {target}")

    def action_quit(self):
        if self.busy:
            self.notify("Processing is still running. Wait for it to finish before quitting.")
            return
        self.exit()


def parse_args(argv=None):
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*")
    parser.add_argument(
        "--inbox",
        type=Path,
        default=ROOT / "data/inbox",
        help="Folder to scan when no files are specified",
    )
    parser.add_argument(
        "--no-autorun",
        action="store_true",
        help="Open the desk without processing; press p to begin",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Auto-process four offline examples using the normal policy",
    )
    parser.add_argument("--strategy", choices=STRATEGIES, default="field_by_field")
    parser.add_argument("--inventory-db", default=str(ROOT / "inventory.db"))
    parser.add_argument("--state-db")
    parser.add_argument("--approval-threshold", default="10000")
    parser.add_argument("--default-currency", choices=["USD"])
    model_arguments(parser)
    parser.set_defaults(mode=None)
    args = parser.parse_args(argv)
    args.mode = args.mode or ("offline" if args.demo else "live")
    if args.demo and args.mode != "offline":
        parser.error("--demo uses offline fixtures")
    args.state_db = args.state_db or str(
        ROOT
        / "runs"
        / (
            "tui-demo-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".db"
            if args.demo
            else "tui.db"
        )
    )
    try:
        if not args.files and not args.demo:
            args.inbox = args.inbox.expanduser().resolve()
            args.inbox.mkdir(parents=True, exist_ok=True)
        paths = discover_inputs(args)
        components(args, args.strategy)
        if args.mode == "live":
            for name in {args.extractor_key_env, args.verifier_key_env}:
                if not os.getenv(name):
                    raise ValueError(
                        f"Missing {name}. Set credentials and model IDs in .env (see .env.example) or your shell."
                    )
        from decimal import Decimal

        threshold = Decimal(args.approval_threshold)
        if not threshold.is_finite() or threshold < 0:
            raise ValueError("Invalid threshold")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return args, paths


def main(argv=None):
    args, paths = parse_args(argv)
    InvoiceDesk(args, paths, stage_delay=0.12 if args.demo else 0).run()


if __name__ == "__main__":
    main()
