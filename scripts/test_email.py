import sys
import os
from unittest.mock import MagicMock, patch
from datetime import date

# Añadir el directorio raíz del backend al PATH
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import engine, SessionLocal, Base
from app.models.user import User
from app.models.bank import BankAccount, BankTransaction
from app.services.email_fetcher import EmailFetcherService
from app.services.invoice_parser import InvoiceSchema, InvoiceItemSchema

# Mock de cuerpo de correo HTML (por ejemplo, una factura de Uber Eats)
MOCK_EMAIL_BODY_HTML = """
<html>
  <body>
    <div style="font-family: Arial;">
      <h1>Gracias por elegir Uber Eats, Juan</h1>
      <p>Detalle de tu pedido del 06 de Julio, 2026</p>
      <hr/>
      <h3>Restaurante: Pizzeria Bella Italia</h3>
      <p>NIF del Emisor: ESB99887766</p>
      <table>
        <tr>
          <td>1x Pizza Margherita Grande</td>
          <td>18.50 EUR</td>
        </tr>
        <tr>
          <td>1x Refresco de Cola</td>
          <td>3.00 EUR</td>
        </tr>
      </table>
      <hr/>
      <p>Base Imponible: 19.55 EUR</p>
      <p>IVA (10%): 1.95 EUR</p>
      <h2>Total Cobrado: 21.50 EUR</h2>
    </div>
  </body>
</html>
"""

# Mock de respuesta estructurada que devolvería Gemini al procesar el email de Uber Eats
MOCK_GEMINI_EMAIL_RESPONSE = """{
  "emitter_name": "Pizzeria Bella Italia",
  "emitter_tax_id": "ESB99887766",
  "issue_date": "2026-07-06",
  "base_amount": 19.55,
  "tax_rate": 10.00,
  "tax_amount": 1.95,
  "total_amount": 21.50,
  "currency": "EUR",
  "items": [
    {
      "description": "Pizza Margherita Grande",
      "quantity": 1.0,
      "unit_price": 18.50,
      "total_amount": 18.50
    },
    {
      "description": "Refresco de Cola",
      "quantity": 1.0,
      "unit_price": 3.00,
      "total_amount": 3.00
    }
  ]
}"""

def run_email_test():
    print("=== SIMULADOR DE INGESTA DE FACTURAS POR EMAIL (IMAP) ===")
    
    # 1. Asegurar base de datos para pruebas
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    
    try:
        # Asegurar un usuario de prueba
        test_user = db.query(User).filter(User.email == "email_test@appgastos.com").first()
        if not test_user:
            test_user = User(
                email="email_test@appgastos.com",
                password_hash="pbkdf2:sha256:mock_hash_value",
                base_currency="EUR"
            )
            db.add(test_user)
            db.commit()
            db.refresh(test_user)

        # Asegurar una cuenta bancaria y transacción coincidente para probar la conciliación automática
        # Cargo de la pizzería por valor exacto de €21.50 el 06 de julio de 2026
        account = db.query(BankAccount).filter(BankAccount.user_id == test_user.id).first()
        if not account:
            account = BankAccount(
                user_id=test_user.id,
                provider_name="Revolut",
                account_number_masked="**** 1122",
                account_type="checking",
                balance=1000.00
            )
            db.add(account)
            db.commit()
            db.refresh(account)

        # Transacción bancaria correspondiente al cargo del correo
        bank_tx = BankTransaction(
            bank_account_id=account.id,
            transaction_date=date(2026, 7, 6),
            value_date=date(2026, 7, 6),
            raw_description="PIZZERIA BELLA ITALIA MADRID",
            cleaned_merchant="Pizzeria Bella Italia",
            amount=-21.50,
            currency="EUR",
            is_reconciled=False,
            import_source="API_PSD2"
        )
        db.add(bank_tx)
        db.commit()
        db.refresh(bank_tx)

        # Instanciar el cargador de correos
        fetcher = EmailFetcherService(db)

        # 2. Verificar si hay credenciales configuradas. Si no, mockeamos el servidor IMAP y el LLM.
        from app.config import settings
        if settings.IMAP_USER == "dummy@gmail.com" or not settings.GEMINI_API_KEY:
            print("\n[!] AVISO: Usando MOCK para simular la conexión IMAP y Gemini LLM.")
            print("    (Para prueba real, configura IMAP_USER, IMAP_PASSWORD y GEMINI_API_KEY en .env)\n")

            # Mockear la llamada de Gemini para el cuerpo del email
            fetcher.parser_service.parse_with_llm = MagicMock(
                return_value=InvoiceSchema.model_validate_json(MOCK_GEMINI_EMAIL_RESPONSE)
            )

            # Mockear la conexión IMAP
            mock_mail_client = MagicMock()
            # Retorna estado OK y el id "42" para el email no leído encontrado
            mock_mail_client.search.return_value = ("OK", [b"42"])
            
            # Crear un mensaje MIME simulado con cuerpo HTML
            msg = email.message_from_string(
                f"From: Uber Eats <orders@uber.com>\n"
                f"Subject: Tu recibo de Pizzeria Bella Italia\n"
                f"Content-Type: text/html; charset=utf-8\n\n"
                f"{MOCK_EMAIL_BODY_HTML}"
            )
            
            mock_mail_client.fetch.return_value = ("OK", [(b"42 (RFC822 {1234})", msg.as_bytes())])
            fetcher._connect = MagicMock(return_value=mock_mail_client)

            # Ejecutar sincronización
            print("Escaneando buzón de entrada...")
            processed_emails = fetcher.fetch_inbound_invoices(user_id=str(test_user.id))
        else:
            print("\n[+] Ejecutando sincronización de correo real contra el servidor IMAP...")
            processed_emails = fetcher.fetch_inbound_invoices(user_id=str(test_user.id))

        # 3. Mostrar resultados
        print(f"\nSincronización completada. Correos procesados: {len(processed_emails)}")
        for item in processed_emails:
            print(f"\n--- Correo Procesado ---")
            print(f"Asunto:        {item['subject']}")
            print(f"Remitente:     {item['sender']}")
            print(f"Emisor Extraí: {item['emitter']}")
            print(f"Total Factura: {item['total_amount']} EUR")
            print(f"Origen Datos:  {item['source']}")
            print(f"ID Registro:   {item['invoice_id']}")

        # 4. Verificar si la transacción bancaria de €21.50 fue conciliada automáticamente
        db.refresh(bank_tx)
        print(f"\n--- Verificación de Conciliación Bancaria ---")
        print(f"Transacción Banco:  '{bank_tx.raw_description}' por -{abs(bank_tx.amount)} EUR")
        print(f"¿Está reconciliado?: {bank_tx.is_reconciled}")
        
        if bank_tx.is_reconciled:
            # Consultar los asientos de diario generados por el split
            from app.models.accounting import AccountingEntry
            entries = db.query(AccountingEntry).filter(AccountingEntry.bank_transaction_id == bank_tx.id).all()
            print(f"\nAsientos contables creados ({len(entries)}):")
            for entry in entries:
                print(f"  * Asiento: {entry.description} | Importe: {entry.amount} EUR | Categoría ID: {entry.category_id}")
            print("\n[+] ¡Conciliación y desglose de artículos de email completada con éxito!")

    except Exception as e:
        db.rollback()
        print(f"\n[!] ERROR EN EL TEST: {str(e)}")
        raise e
    finally:
        db.close()

if __name__ == "__main__":
    run_email_test()
