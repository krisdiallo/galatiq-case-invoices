import sys


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if any(arg.split("=", 1)[0] in {"--invoice_path", "--invoice-path"} for arg in args):
        from invoices.cli import main as cli_main

        return cli_main(args)
    from invoices.tui import main as tui_main

    return tui_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
