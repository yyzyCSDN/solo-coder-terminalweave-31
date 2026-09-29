from __future__ import annotations
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from terminalweave.api.service import TerminalService
from terminalweave.core.serialization import to_primitive

class Handler(BaseHTTPRequestHandler):
    service = TerminalService()

    def _reply(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, sort_keys=True).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path == '/health':
                self._reply(200, self.service.health())
            elif self.path.startswith('/billing/settlements/'):
                settlement_id = self.path.rsplit('/', 1)[-1]
                self._reply(200, to_primitive(self.service.settlement.get_settlement(settlement_id)))
            else:
                self._reply(404, {'error': 'not_found'})
        except Exception as exc:
            self._reply(422, {'error': exc.__class__.__name__, 'message': str(exc)})

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            payload = json.loads(self.rfile.read(length) or b'{}')
        except Exception:
            self._reply(400, {'error': 'invalid_json'})
            return
        try:
            if self.path == '/tide/clearance':
                result = self.service.tide_clearance(payload)
            elif self.path == '/billing/demurrage':
                result = self.service.demurrage_quote(payload)
            elif self.path == '/billing/rates':
                result = self.service.publish_rate(payload)
            elif self.path == '/billing/discounts':
                result = self.service.add_discount_rule(payload)
            elif self.path == '/billing/credit-accounts':
                result = self.service.open_credit_account(payload)
            elif self.path == '/billing/settle':
                result = self.service.settle_business(payload)
            else:
                self._reply(404, {'error': 'not_found'})
                return
            self._reply(200, result)
        except Exception as exc:
            self._reply(422, {'error': exc.__class__.__name__, 'message': str(exc)})

    def log_message(self, format, *args):
        return

def serve(host: str='127.0.0.1', port: int=8080) -> None:
    ThreadingHTTPServer((host, port), Handler).serve_forever()
