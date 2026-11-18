"""Exercise the installed adapter in a fresh interpreter for each legacy invocation."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class LegacyTests(unittest.TestCase):
    def test_legacy_contract_and_shared_endpoint_resolution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / "src" / "analisis_gastos"
            package.mkdir(parents=True)
            (package / "__init__.py").touch()
            (package / "mercadopago_client.py").write_text(
                "class MercadoPagoError(Exception): pass\n"
                "class MercadoPagoClient:\n"
                "    def _request_json(self, method, path, payload=None):\n"
                "        assert path == '/users/me'\n"
                "        return {'id': 430}\n"
                "    def backup(self): return True\n", encoding="utf-8")
            (package / "cli.py").write_text(
                "from .mercadopago_client import MercadoPagoClient\n"
                "def main(args):\n"
                "    client = MercadoPagoClient()\n"
                "    assert client.get_user_id() == '430' and client.backup()\n"
                "    assert args == ['--json', 'sync']\n"
                "    return 2\n", encoding="utf-8")
            result = subprocess.run([sys.executable, "-B", "-m", "mp_gastos.legacy",
                                     str(root / "scripts" / "mercadopago.py"), "--json", "sync"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertFalse(list(root.rglob("__pycache__")))

    @unittest.skipUnless(os.getenv("MP_GASTOS_LEGACY_SOURCE"), "external legacy checkout optional")
    def test_existing_card_command_in_isolated_ledger(self):
        source = Path(os.environ["MP_GASTOS_LEGACY_SOURCE"])
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = source / "scripts" / "mercadopago.py"
            env = dict(os.environ, MP_ACCESS_TOKEN="fixture-only", EXPENSES_USER_ID="test",
                       PYTHONDONTWRITEBYTECODE="1")
            # Import only application code from the external checkout. Redirect
            # every Settings path before calling the genuine sync-cards command.
            harness = """
import json, sys
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
from io import BytesIO
from mp_gastos.legacy import main
root, script, fixtures = map(Path, sys.argv[1:])
sys.path.insert(0, str(script.parents[1] / 'src'))
from analisis_gastos import config
config.project_root = lambda: root
rows = json.loads(fixtures.read_text(encoding='utf-8'))
def respond(request, **kwargs):
    url = urlsplit(request.full_url)
    if url.path == '/users/me':
        data = {'id': 430}
    else:
        assert url.path == '/v1/payments/search'
        kind = parse_qs(url.query)['payment_type_id'][0]
        results = [r for r in rows if r['payment_type_id'] == kind]
        data = {'results': results, 'paging': {'offset': 0, 'total': len(results)}}
    return BytesIO(json.dumps(data).encode())
with patch('urllib.request.urlopen', respond):
    sys.exit(main([str(script), '--json', 'sync-cards', '--period', '2026-04']))
"""
            result = subprocess.run([sys.executable, "-B", "-c", harness, str(root), str(script),
                                     str(Path(__file__).parent / "fixtures" / "payments.json")],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["card_payments"], 2)
            self.assertTrue(list(root.rglob("*.sqlite3")))
