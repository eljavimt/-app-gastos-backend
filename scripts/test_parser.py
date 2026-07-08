import sys
import os
from unittest.mock import MagicMock

# Añadir el directorio raíz del backend al PATH
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import engine, SessionLocal, Base
from app.models.user import User
from app.services.invoice_parser import InvoiceParserService, InvoiceSchema, InvoiceItemSchema

# Mock de la respuesta de Gemini en caso de que no haya API Key configurada
MOCK_GEMINI_RESPONSE = """{
  "emitter_name": "Iberdrola Clientes S.A.U.",
  "emitter_tax_id": "A95723729",
  "issue_date": "2026-07-05",
  "base_amount": 75.20,
  "tax_rate": 21.00,
  "tax_amount": 15.79,
  "total_amount": 90.99,
  "currency": "EUR",
  "items": [
    {
      "description": "Término de potencia (Periodo del 01/06 al 01/07)",
      "quantity": 1.0,
      "unit_price": 35.20,
      "total_amount": 35.20
    },
    {
      "description": "Consumo de energía activa (240 kWh)",
      "quantity": 1.0,
      "unit_price": 40.00,
      "total_amount": 40.00
    }
  ]
}"""

def run_parser_test():
    print("=== SIMULADOR DE EXTRACCIÓN DE FACTURA CON IA ===")
    
    # 1. Asegurar base de datos limpia para pruebas
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    
    try:
        # Asegurar un usuario de prueba
        test_user = db.query(User).filter(User.email == "parser_test@appgastos.com").first()
        if not test_user:
            test_user = User(
                email="parser_test@appgastos.com",
                password_hash="pbkdf2:sha256:mock_hash_value",
                base_currency="EUR"
            )
            db.add(test_user)
            db.commit()
            db.refresh(test_user)

        parser = InvoiceParserService(db)

        # 2. Verificar API KEY de Gemini
        from app.config import settings
        if not settings.GEMINI_API_KEY:
            print("\n[!] AVISO: GEMINI_API_KEY no detectada en el archivo .env.")
            print("    Ejecutando simulación mediante MOCK de la respuesta de la IA...\n")
            
            # Mockear la llamada del LLM
            parser.parse_with_llm = MagicMock(
                return_value=InvoiceSchema.model_validate_json(MOCK_GEMINI_RESPONSE)
            )
            
            # Texto simulado que vendría de un PDF
            mock_invoice_text = "IBERDROLA\nFactura Simplificada\nEmisor: Iberdrola Clientes S.A.U. CIF: A95723729\nFecha de emisión: 05/07/2026\nBase Imponible: 75,20 EUR\nIVA (21%): 15,79 EUR\nTotal Factura: 90,99 EUR\nConceptos:\n- Término de potencia: 35.20 EUR\n- Consumo energía: 40.00 EUR"
            
            # Simular extracción
            parsed_data = parser.parse_with_llm(mock_invoice_text)
        else:
            print("\n[+] GEMINI_API_KEY detectada. Preparado para llamada real.")
            # Para una llamada real, necesitamos un archivo PDF de ejemplo.
            # Comprobar si se ha pasado un archivo por argumento
            if len(sys.argv) < 2:
                print("[!] ERROR: Por favor, proporciona la ruta de un archivo PDF real para probar.")
                print("    Ejemplo: python scripts/test_parser.py ruta/a/factura.pdf")
                print("    O bien, quita la API Key para correr el test en modo simulación (mock).")
                return
            
            pdf_path = sys.argv[1]
            print(f"Leyendo PDF desde: {pdf_path}...")
            text = parser.extract_text_from_pdf(pdf_path)
            print("Extrayendo datos estructurados usando Gemini LLM...")
            parsed_data = parser.parse_with_llm(text)

        # 3. Guardar en base de datos
        print("Guardando datos estructurados en la base de datos...")
        invoice = parser.save_to_database(user_id=test_user.id, data=parsed_data)
        
        # 4. Mostrar resultados guardados en BD
        print("\n=== FACTURA EXTRAÍDA Y REGISTRADA EN DB ===")
        print(f"ID Factura:      {invoice.id}")
        print(f"Emisor:          {invoice.emitter_name} (CIF/NIF: {invoice.emitter_tax_id})")
        print(f"Fecha Emisión:   {invoice.issue_date}")
        print(f"Base Imponible:  {invoice.base_amount} {invoice.currency}")
        print(f"Tasa IVA:        {invoice.tax_rate}%")
        print(f"Importe IVA:     {invoice.tax_amount} {invoice.currency}")
        print(f"Total Factura:   {invoice.total_amount} {invoice.currency}")
        
        print("\n--- Desglose de Líneas (`invoice_items`) ---")
        for idx, item in enumerate(invoice.items, 1):
            print(f"Artículo #{idx}:")
            print(f"  - Descripción: {item.description}")
            print(f"  - Cantidad:    {item.quantity}")
            print(f"  - P. Unitario: {item.unit_price} {invoice.currency}")
            print(f"  - Importe Lin: {item.total_amount} {invoice.currency}")
        
        print("\n[+] Test completado con éxito.")
        
    except Exception as e:
        db.rollback()
        print(f"\n[!] ERROR EN EL TEST: {str(e)}")
        raise e
    finally:
        db.close()

if __name__ == "__main__":
    run_parser_test()
