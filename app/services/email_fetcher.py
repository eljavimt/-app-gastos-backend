import logging
import imaplib
import email
from email.header import decode_header
import os
import tempfile
from typing import List, Dict, Any
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.config import settings
from app.services.invoice_parser import InvoiceParserService
from app.services.reconciliation import ReconciliationService

logger = logging.getLogger(__name__)

class EmailFetcherService:
    def __init__(self, db: Session):
        self.db = db
        self.parser_service = InvoiceParserService(db)

    def _connect(self) -> imaplib.IMAP4_SSL:
        """Establece conexión SSL segura con el servidor IMAP."""
        try:
            mail = imaplib.IMAP4_SSL(settings.IMAP_SERVER, settings.IMAP_PORT)
            mail.login(settings.IMAP_USER, settings.IMAP_PASSWORD)
            return mail
        except Exception as e:
            logger.error(f"Error conectando al servidor IMAP ({settings.IMAP_SERVER}): {str(e)}")
            raise e

    def _decode_mime_header(self, header_value: str) -> str:
        """Decodifica correctamente los encabezados del correo (ej: asunto, remitente)."""
        if not header_value:
            return ""
        decoded_header_parts = decode_header(header_value)
        header_text = ""
        for part, encoding in decoded_header_parts:
            if isinstance(part, bytes):
                header_text += part.decode(encoding or 'utf-8', errors='ignore')
            else:
                header_text += part
        return header_text

    def _clean_html_body(self, html_content: str) -> str:
        """Limpia el HTML del cuerpo del email para dejar texto plano limpio legible por el LLM."""
        soup = BeautifulSoup(html_content, "html.parser")
        
        # Eliminar scripts y estilos de diseño
        for script in soup(["script", "style"]):
            script.decompose()
            
        # Obtener texto plano y formatear espacios
        text = soup.get_text()
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        return "\n".join(chunk for chunk in chunks if chunk)

    def process_email_message(self, user_id: str, msg: email.message.Message) -> List[Dict[str, Any]]:
        """
        Procesa un objeto email.message.Message extrayendo adjuntos PDF
        o analizando el cuerpo del email con IA para registrar facturas.
        """
        processed_invoices = []
        subject = self._decode_mime_header(msg["Subject"])
        sender = self._decode_mime_header(msg["From"])
        
        pdf_processed = False
        email_body_html = ""
        email_body_text = ""

        # Carpeta temporal para guardar adjuntos
        with tempfile.TemporaryDirectory() as temp_dir:
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))

                # 1. Buscar adjuntos PDF
                if "attachment" in content_disposition and content_type == "application/pdf":
                    filename = self._decode_mime_header(part.get_filename())
                    if filename:
                        temp_file_path = os.path.join(temp_dir, filename)
                        with open(temp_file_path, "wb") as f:
                            f.write(part.get_payload(decode=True))

                        try:
                            # Procesar factura en PDF e intentar reconciliar
                            invoice = self.parser_service.process_and_reconcile(
                                user_id=user_id,
                                file_path=temp_file_path
                            )
                            processed_invoices.append({
                                "subject": subject,
                                "sender": sender,
                                "emitter": invoice.emitter_name,
                                "total_amount": float(invoice.total_amount),
                                "source": "PDF_ATTACHMENT",
                                "invoice_id": str(invoice.id)
                            })
                            pdf_processed = True
                        except Exception as parser_err:
                            logger.error(f"Error procesando adjunto {filename}: {str(parser_err)}")

                # Guardar el cuerpo por si no hay PDFs adjuntos (ej: Uber, Amazon email)
                elif content_type == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        email_body_html += payload.decode('utf-8', errors='ignore')
                elif content_type == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        email_body_text += payload.decode('utf-8', errors='ignore')

            # 2. Si no había PDFs adjuntos, procesar el cuerpo del email (HTML/Text)
            if not pdf_processed:
                body_to_parse = ""
                if email_body_html:
                    body_to_parse = self._clean_html_body(email_body_html)
                elif email_body_text:
                    body_to_parse = email_body_text

                # Solo enviamos a la IA si el texto del cuerpo tiene suficiente longitud
                if len(body_to_parse.strip()) > 100:
                    try:
                        logger.info("Analizando cuerpo de email mediante IA...")
                        parsed_data = self.parser_service.parse_with_llm(body_to_parse)
                        
                        # Guardar la factura
                        invoice = self.parser_service.save_to_database(
                            user_id=user_id,
                            data=parsed_data,
                            file_path=None
                        )
                        # Lanzar la conciliación
                        reconciler = ReconciliationService(self.db)
                        reconciler.reconcile_user_transactions(user_id=user_id)

                        processed_invoices.append({
                            "subject": subject,
                            "sender": sender,
                            "emitter": invoice.emitter_name,
                            "total_amount": float(invoice.total_amount),
                            "source": "EMAIL_BODY",
                            "invoice_id": str(invoice.id)
                        })
                    except Exception as parse_body_err:
                        logger.warning(f"No se pudo extraer factura del cuerpo del email: {str(parse_body_err)}")

        return processed_invoices

    def parse_eml_file(self, user_id: str, file_path: str) -> List[Dict[str, Any]]:
        """Parsea un archivo .eml subido manualmente y procesa sus facturas."""
        try:
            with open(file_path, "rb") as f:
                raw_email = f.read()
            msg = email.message_from_bytes(raw_email)
            return self.process_email_message(user_id, msg)
        except Exception as e:
            logger.error(f"Error parseando archivo .eml {file_path}: {str(e)}")
            raise e

    def fetch_inbound_invoices(self, user_id: str) -> List[Dict[str, Any]]:
        """
        Conecta al buzón, busca correos no leídos que coincidan con términos de facturación,
        procesa sus adjuntos PDF o extrae la información del cuerpo si es un correo HTML.
        """
        processed_invoices = []
        mail = None
        
        try:
            mail = self._connect()
            mail.select("INBOX")

            # Buscar correos no leídos (UNSEEN)
            status, response_data = mail.search(None, "UNSEEN")
            if status != "OK":
                logger.warning("No se pudo realizar la búsqueda en el buzón IMAP.")
                return []

            email_ids = response_data[0].split()
            logger.info(f"Se encontraron {len(email_ids)} correos no leídos en el buzón.")

            # Palabras clave para pre-filtrado
            keywords = ["factura", "recibo", "compra", "pedido", "invoice", "ticket", "payment"]

            for e_id in email_ids:
                # Obtener la estructura del correo
                status, msg_data = mail.fetch(e_id, "(RFC822)")
                if status != "OK":
                    continue

                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                subject = self._decode_mime_header(msg["Subject"])
                sender = self._decode_mime_header(msg["From"])
                
                # Verificar si el asunto o el remitente coinciden con alguna palabra clave
                subject_lower = subject.lower()
                sender_lower = sender.lower()
                
                matches_keywords = any(kw in subject_lower or kw in sender_lower for kw in keywords)
                if not matches_keywords:
                    # Si no coincide, omitir para no procesar emails personales/irrelevantes
                    continue

                logger.info(f"Procesando correo coincidente: '{subject}' de {sender}")

                # Procesar el mensaje con el nuevo helper
                results = self.process_email_message(user_id, msg)
                processed_invoices.extend(results)

                # Marcar correo como leído tras procesarlo
                mail.store(e_id, "+FLAGS", "\\Seen")

        except Exception as e:
            logger.error(f"Error general en el EmailFetcherService: {str(e)}")
            raise e
        finally:
            if mail:
                try:
                    mail.close()
                    mail.logout()
                except Exception:
                    pass

        return processed_invoices
