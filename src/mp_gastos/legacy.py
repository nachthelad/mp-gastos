"""Run the existing AnalisisGastos entry point with shared MP endpoint methods."""

import sys
from pathlib import Path

from .client import Endpoints


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print("mp-gastos-analisis PATH/TO/scripts/mercadopago.py [legacy CLI arguments]")
        return 0 if args else 1
    script = Path(args.pop(0)).resolve()
    src = script.parent.parent / "src"
    if script.name != "mercadopago.py" or not (src / "analisis_gastos" / "cli.py").is_file():
        print("Expected the AnalisisGastos scripts/mercadopago.py path.", file=sys.stderr)
        return 1
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(src))
    from analisis_gastos import mercadopago_client as legacy

    class IntegratedClient(Endpoints, legacy.MercadoPagoClient):
        error_type = legacy.MercadoPagoError

    # Bind before the original CLI and sync modules import their client. All
    # old commands, exception types, backup paths and persistence stay intact.
    legacy.MercadoPagoClient = IntegratedClient
    from analisis_gastos.cli import main as legacy_main

    return legacy_main(args)


if __name__ == "__main__":
    sys.exit(main())
