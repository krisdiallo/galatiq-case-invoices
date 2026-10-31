import pytest

from invoices.tui import discover_inputs, parse_args


def test_directory_discovery_filters_and_deduplicates(tmp_path):
    (tmp_path / "first.PDF").write_bytes(b"pdf")
    (tmp_path / "second.txt").write_text("invoice")
    (tmp_path / "readme.md").write_text("notes")
    (tmp_path / ".hidden.pdf").write_bytes(b"hidden")
    args, paths = parse_args(["--mode", "offline", str(tmp_path), str(tmp_path / "first.PDF")])
    assert [p.name for p in paths] == ["first.PDF", "second.txt"]
    assert discover_inputs(args) == paths


def test_live_is_default_and_models_are_read_from_environment(tmp_path, monkeypatch):
    monkeypatch.setattr("invoices.tui.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXTRACTOR_MODEL", "test-extractor")
    monkeypatch.setenv("VERIFIER_MODEL", "test-vision-verifier")
    monkeypatch.setenv("XAI_API_KEY", "test-only")
    args, paths = parse_args(["--inbox", str(tmp_path)])
    assert args.mode == "live"
    assert args.extractor_model == "test-extractor"
    assert args.verifier_model == "test-vision-verifier"
    assert not paths


def test_live_fails_early_without_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("invoices.tui.load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--inbox",
                str(tmp_path),
                "--extractor-model",
                "one",
                "--verifier-model",
                "two",
                "--extractor-key-env",
                "XAI_API_KEY",
                "--verifier-key-env",
                "XAI_API_KEY",
            ]
        )
    assert "Missing XAI_API_KEY" in capsys.readouterr().err


def test_entrypoint_routes_tui_and_preserves_json_cli(monkeypatch):
    from main import main

    calls = []
    monkeypatch.setattr("invoices.tui.main", lambda args: calls.append(("tui", args)))
    monkeypatch.setattr("invoices.cli.main", lambda args: calls.append(("cli", args)))
    main([])
    main(["--invoice_path=data/invoices/invoice_1001.txt"])
    assert calls[0] == ("tui", [])
    assert calls[1][0] == "cli"
