import asyncio
from pathlib import Path

from textual.widgets import Button, DataTable, Input

from invoices.tui import HoldReason, InvoiceDesk, parse_args
from scripts.setup_db import setup_database


def make_app(tmp_path):
    inventory = tmp_path / "inventory.db"
    setup_database(inventory)
    args, paths = parse_args(
        ["--demo", "--state-db", str(tmp_path / "state.db"), "--inventory-db", str(inventory)]
    )
    return InvoiceDesk(args, paths)


async def ready(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def test_demo_shows_pdf_results_failed_checks_and_hold(tmp_path):
    async def scenario():
        app = make_app(tmp_path)
        async with app.run_test(size=(140, 48)) as pilot:
            await ready(app, pilot)
            assert app.results[app.selected]["status"] == "awaiting_approval"
            assert app.args.approval_threshold == "10000"
            assert app.results[app.selected]["draft"]["invoice"]["total"] == "11000"
            assert app.focused is app.query_one("#vp", DataTable)
            assert Path(app.selected).suffix == ".pdf"
            assert app.query_one("#vp", DataTable).row_count == 1
            assert app.query_one("#holds", DataTable).row_count == 1
            assert app.query_one("#checks", DataTable).row_count > 0
            assert not app.query_one("#approve", Button).disabled
            await pilot.press("3")
            await pilot.pause()
            assert app.results[app.selected]["status"] == "rejected"
            assert any(c["outcome"] == "reject" for c in app.results[app.selected]["checks"])
            assert app.query_one("#approve", Button).disabled
            await pilot.press("2")
            await pilot.pause()
            assert app.results[app.selected]["status"] == "hold"
            assert "Currency" in app.results[app.selected]["reason"]

    asyncio.run(scenario())


def test_a_approves_and_moves_invoice_out_of_vp_queue(tmp_path):
    async def scenario():
        app = make_app(tmp_path)
        async with app.run_test(size=(140, 48)) as pilot:
            await ready(app, pilot)
            await pilot.press("a")
            await ready(app, pilot)
            assert app.results[app.selected]["status"] == "paid"
            assert app.query_one("#vp", DataTable).row_count == 0
            assert app.query_one("#approve", Button).disabled
            with app.store.connect() as conn:
                assert conn.execute("SELECT count(*) FROM receipts").fetchone()[0] == 2

    asyncio.run(scenario())


def test_h_places_invoice_on_hold_with_reason_and_reload(tmp_path):
    async def scenario():
        app = make_app(tmp_path)
        async with app.run_test(size=(140, 48)) as pilot:
            await ready(app, pilot)
            await pilot.press("h")
            await pilot.pause()
            assert isinstance(app.screen, HoldReason)
            app.screen.query_one(Input).value = "Waiting for PO confirmation"
            await pilot.press("enter")
            await ready(app, pilot)
            assert app.results[app.selected]["status"] == "hold"
            assert app.results[app.selected]["reason"] == "Waiting for PO confirmation"
            assert app.query_one("#holds", DataTable).row_count == 2
            await pilot.press("a")
            await pilot.pause()
            assert app.results[app.selected]["status"] == "hold"
        reloaded = InvoiceDesk(app.args, app.paths)
        assert reloaded.results[str(app.paths[0])]["reason"] == "Waiting for PO confirmation"

    asyncio.run(scenario())


def test_compact_terminal_keeps_keyboard_actions(tmp_path):
    async def scenario():
        app = make_app(tmp_path)
        async with app.run_test(size=(90, 30)) as pilot:
            await ready(app, pilot)
            assert app.screen.has_class("compact")
            await pilot.press("d")
            await ready(app, pilot)
            assert app.results[app.selected]["status"] == "rejected"

    asyncio.run(scenario())


def inbox_app(tmp_path, files=(), extra=()):
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    for filename in files:
        (inbox / filename).write_bytes((root / "data/invoices" / filename).read_bytes())
    inventory = tmp_path / "inventory.db"
    setup_database(inventory)
    args, paths = parse_args(
        [
            "--mode",
            "offline",
            "--inbox",
            str(inbox),
            "--state-db",
            str(tmp_path / "state.db"),
            "--inventory-db",
            str(inventory),
            *extra,
        ]
    )
    return InvoiceDesk(args, paths)


def test_normal_inbox_autoruns_and_follows_each_invoice(tmp_path):
    async def scenario():
        app = inbox_app(tmp_path, ["invoice_1011.pdf", "invoice_1004.json"])
        started = []
        original = app._started

        def record(path):
            original(path)
            started.append((path, app.selected))

        app._started = record
        async with app.run_test(size=(140, 48)) as pilot:
            await ready(app, pilot)
            assert len(started) == 2
            assert all(path == selected for path, selected in started)
            assert all(r["status"] == "paid" for r in app.results.values())
            assert app.focused is app.query_one("#vp", DataTable)
        reloaded = InvoiceDesk(app.args, app.paths)
        async with reloaded.run_test(size=(140, 48)) as pilot:
            await ready(reloaded, pilot)
            assert reloaded.worker_handle is None  # No repeated processing or model calls.
            assert all(r["status"] == "paid" for r in reloaded.results.values())

    asyncio.run(scenario())


def test_empty_inbox_and_rescan_new_file(tmp_path):
    async def scenario():
        app = inbox_app(tmp_path)
        async with app.run_test(size=(140, 48)) as pilot:
            await ready(app, pilot)
            assert app.selected is None
            assert app.query_one("#approve", Button).disabled
            root = Path(__file__).resolve().parents[1]
            added = app.args.inbox / "my-dropped-invoice.PDF"
            added.write_bytes((root / "data/invoices/invoice_1011.pdf").read_bytes())
            await pilot.press("p")
            await ready(app, pilot)
            assert app.results[str(added)]["status"] == "paid"
            assert len(app.results) == 1

    asyncio.run(scenario())


def test_changed_source_requeued_and_old_paid_result_not_reused(tmp_path):
    async def scenario():
        app = inbox_app(tmp_path, ["invoice_1011.pdf"])
        async with app.run_test(size=(140, 48)) as pilot:
            await ready(app, pilot)
            source = app.paths[0]
            source.write_bytes(source.read_bytes() + b"\n% changed source\n")
            await pilot.press("p")
            await ready(app, pilot)
            assert app.results[str(source)]["status"] == "hold"
            assert "No offline fixture" in app.results[str(source)]["reason"]

    asyncio.run(scenario())


def test_no_autorun_requires_p(tmp_path):
    async def scenario():
        app = inbox_app(tmp_path, ["invoice_1011.pdf"], ["--no-autorun"])
        async with app.run_test(size=(140, 48)) as pilot:
            await ready(app, pilot)
            assert app.results[app.selected]["status"] == "queued"
            await pilot.press("p")
            await ready(app, pilot)
            assert app.results[app.selected]["status"] == "paid"

    asyncio.run(scenario())


def test_demo_and_normal_modes_share_approval_policy(tmp_path):
    demo, _ = parse_args(["--demo"])
    normal, _ = parse_args(["--mode", "offline", "--inbox", str(tmp_path)])
    custom, _ = parse_args(["--demo", "--approval-threshold", "12000"])
    assert demo.approval_threshold == normal.approval_threshold == "10000"
    assert custom.approval_threshold == "12000"
