"""Submit an actual TLS-protected MIME message on loopback. No external mailbox is involved."""

import os
import shutil
import socketserver
import ssl
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

from study_report import deliver


class Mailbox(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 localhost SMTP\r\n")
        while raw := self.rfile.readline():
            command = raw.split(maxsplit=1)[0].upper()
            if command == b"EHLO":
                self.wfile.write(b"250-localhost\r\n250 AUTH PLAIN\r\n")
            elif command == b"AUTH":
                self.wfile.write(b"235 authenticated\r\n")
            elif command == b"DATA":
                self.wfile.write(b"354 finish with a dot\r\n")
                message = bytearray()
                while (line := self.rfile.readline()) not in (b".\r\n", b""):
                    message.extend(line[1:] if line.startswith(b"..") else line)
                self.server.messages.append(bytes(message))
                self.wfile.write(b"250 queued\r\n")
            elif command == b"QUIT":
                self.wfile.write(b"221 bye\r\n")
                break
            else:
                self.wfile.write(b"250 OK\r\n")


class TestMailTransport(unittest.TestCase):
    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL required for a temporary local certificate")
    def test_real_self_addressed_smtp_with_exact_csv_and_no_duplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cert, key = root / "cert.pem", root / "key.pem"
            config = root / "openssl.cnf"
            config.write_text(
                "[req]\ndistinguished_name=dn\nprompt=no\nx509_extensions=ext\n"
                "[dn]\nCN=localhost\n[ext]\nsubjectAltName=IP:127.0.0.1\n"
                "basicConstraints=critical,CA:TRUE\n",
                encoding="ascii",
            )
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "1",
                    "-config",
                    str(config),
                    "-keyout",
                    str(key),
                    "-out",
                    str(cert),
                ],
                capture_output=True,
                check=True,
            )
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            with socketserver.TCPServer(("127.0.0.1", 0), Mailbox) as endpoint:
                endpoint.socket = ctx.wrap_socket(endpoint.socket, server_side=True)
                endpoint.messages = []
                thread = threading.Thread(target=endpoint.serve_forever, daemon=True)
                thread.start()
                attachment = "Предмет;Срок\r\nДемо;2026-11-10".encode("utf-8-sig")
                try:
                    with patch.dict(os.environ, {"SSL_CERT_FILE": str(cert)}):
                        for _ in range(2):
                            deliver(
                                "<table><tr><td>Demo</td></tr></table>",
                                root,
                                datetime.now(timezone.utc),
                                slot="demo",
                                mailbox="student@example.invalid",
                                host="127.0.0.1",
                                port=endpoint.server_address[1],
                                password="unused-test",
                                attachment=attachment,
                            )
                finally:
                    endpoint.shutdown()
                    thread.join(timeout=5)
            self.assertEqual(len(endpoint.messages), 1)
            mail = BytesParser(policy=policy.default).parsebytes(endpoint.messages[0])
            self.assertEqual(mail["To"].addresses[0].addr_spec, "student@example.invalid")
            self.assertIn("<table>", mail.get_body(preferencelist=("html",)).get_content())
            self.assertEqual(next(mail.iter_attachments()).get_payload(decode=True), attachment)
