import logging
import os
from typing import List, Optional
from datetime import date
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
import google.generativeai as genai

from app.config import settings
from app.models.bank import BankTransaction, BankAccount
from app.services.invoice_parser import InvoiceParserService
from app.services.reconciliation import ReconciliationService
from app.models.accounting import AccountingEntry
from app.models.category import Category

logger = logging.getLogger(__name__)

# ============================================================================
# ESQUEMAS DE SALIDA ESTRUCTURADA (Pydantic para extractos bancarios)
# ============================================================================

class BankTransactionLineSchema(BaseModel):
    transaction_date: str = Field(description="Fecha de la operación en formato ISO YYYY-MM-DD")
    raw_description: str = Field(description="Concepto, comercio o descripción literal de la operación")
    amount: float = Field(description="Importe de la operación. Negativo para cargos/gastos, positivo para abonos/ingresos")
    balance_snapshot: Optional[float] = Field(description="Saldo resultante en la cuenta después de la operación (si está disponible)")


class BankStatementSchema(BaseModel):
    currency: str = Field(description="Divisa de la cuenta expresada en código ISO de 3 letras (ej: EUR, USD)", default="EUR")
    transactions: List[BankTransactionLineSchema] = Field(description="Listado completo de transacciones extraídas de forma cronológica")


# ============================================================================
# SERVICIO DE EXTRACCIÓN DE EXTRACTOS
# ============================================================================

class BankStatementParserService:
    def __init__(self, db: Session):
        self.db = db
        self.pdf_parser = InvoiceParserService(db) # Reutilizamos extractor de PDF

    def parse_statement_file(self, file_path: str) -> BankStatementSchema:
        """
        Extrae el texto de un extracto bancario (PDF/CSV/Texto) y utiliza Gemini
        para parsear la tabla de movimientos en el esquema estructurado en trozos (chunks/páginas)
        para evitar que la IA ignore movimientos en archivos largos.
        """
        import time
        import pdfplumber
        
        # 1. Extraer texto según extensión en páginas o bloques
        ext = os.path.splitext(file_path)[1].lower()
        pages_text = []
        
        if ext == '.pdf':
            try:
                with pdfplumber.open(file_path) as pdf:
                    for page in pdf.pages:
                        t = page.extract_text()
                        if t and t.strip():
                            pages_text.append(t)
            except Exception as e:
                logger.warning(f"Error extrayendo por páginas con pdfplumber: {str(e)}. Fallback a extracción simple.")
                pages_text = [self.pdf_parser.extract_text_from_pdf(file_path)]
        else:
            # Leer como archivo de texto plano para CSV o TXT
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    text = f.read()
                
                if not text.strip():
                    raise ValueError("El archivo está vacío o no contiene texto legible.")
                
                # Dividir archivos CSV/TXT grandes en bloques de 100 líneas
                lines = text.splitlines()
                chunk_size = 100
                for i in range(0, len(lines), chunk_size):
                    chunk = "\n".join(lines[i:i+chunk_size])
                    if chunk.strip():
                        pages_text.append(chunk)
            except Exception as e:
                logger.error(f"Error al leer archivo de texto: {str(e)}")
                raise ValueError("No se pudo leer el contenido del extracto en formato de texto.")

        if not pages_text:
            raise ValueError("El archivo está vacío o no contiene texto legible.")

        # 2. Llamada a Gemini con esquema estructurado por cada bloque
        if not settings.GEMINI_API_KEY:
            raise ValueError("No se ha configurado la variable GEMINI_API_KEY en el entorno.")

        genai.configure(api_key=settings.GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")

        all_transactions = []
        currency = "EUR"

        for idx, page_content in enumerate(pages_text):
            # Control de Rate Limits (15 RPM en el plan gratis de Gemini)
            if idx > 0:
                time.sleep(2.0)

            prompt = (
                "Eres un experto en procesar extractos bancarios. Tu objetivo es convertir la tabla de movimientos "
                "del banco en un listado estructurado de transacciones.\n"
                "Presta atención especial a:\n"
                "- Identificar correctamente la fecha de cada operación.\n"
                "- Extraer la descripción del comercio o concepto.\n"
                "- Diferenciar importes negativos (cargos, cobros, transferencias enviadas) de positivos (ingresos, nóminas, abonos).\n\n"
                f"Texto del Extracto Bancario (Parte {idx+1} de {len(pages_text)}):\n{page_content}"
            )

            try:
                response = model.generate_content(
                    prompt,
                    generation_config=genai.GenerationConfig(
                        response_mime_type="application/json",
                        response_schema=BankStatementSchema,
                        temperature=0.1
                    )
                )
                parsed_data = BankStatementSchema.model_validate_json(response.text)
                if parsed_data.transactions:
                    all_transactions.extend(parsed_data.transactions)
                if parsed_data.currency:
                    currency = parsed_data.currency
            except Exception as e:
                logger.error(f"Error parseando bloque {idx+1} con Gemini: {str(e)}")
                if len(pages_text) == 1:
                    raise RuntimeError(f"Error de procesamiento de extracto con IA: {str(e)}")

        return BankStatementSchema(currency=currency, transactions=all_transactions)

    def save_and_process(self, user_id: str, bank_account_id: str, data: BankStatementSchema) -> int:
        """
        Guarda los movimientos extraídos en la tabla `bank_transactions` e inicia
        el flujo de conciliación del usuario.
        """
        # Comprobar que la cuenta existe
        account = self.db.query(BankAccount).filter(
            BankAccount.id == bank_account_id,
            BankAccount.user_id == user_id
        ).first()
        if not account:
            raise ValueError("La cuenta bancaria seleccionada no existe o no pertenece al usuario.")

        inserted_count = 0
        new_transactions = []

        # 1. Guardar cada transacción en DB
        for tx in data.transactions:
            # Comprobar si ya existe una transacción idéntica para evitar duplicados al re-subir extractos
            # (Filtramos por fecha, descripción e importe en la misma cuenta)
            exists = self.db.query(BankTransaction).filter(
                BankTransaction.bank_account_id == bank_account_id,
                BankTransaction.transaction_date == tx.transaction_date,
                BankTransaction.amount == tx.amount,
                BankTransaction.raw_description == tx.raw_description
            ).first()

            if not exists:
                db_tx = BankTransaction(
                    bank_account_id=bank_account_id,
                    transaction_date=tx.transaction_date,
                    value_date=tx.transaction_date,
                    raw_description=tx.raw_description,
                    cleaned_merchant=self._clean_merchant_name(tx.raw_description),
                    amount=tx.amount,
                    currency=data.currency,
                    balance_snapshot=tx.balance_snapshot,
                    is_reconciled=False,
                    import_source='CSV_MANUAL'
                )
                self.db.add(db_tx)
                new_transactions.append(db_tx)
                inserted_count += 1

        self.db.commit()

        # 2. Ejecutar la conciliación automática para este usuario
        reconciler = ReconciliationService(self.db)
        reconciler.reconcile_user_transactions(user_id=user_id)

        # 3. Crear asientos directos 1:1 para las transacciones no conciliadas (para asegurar consistencia en el ledger)
        self._create_ledger_entries_for_unreconciled(user_id, bank_account_id)

        return inserted_count

    def _clean_merchant_name(self, desc: str) -> str:
        """Limpia cadenas como 'COMPRA EN MERCADONA SPAIN' a 'Mercadona'."""
        desc_upper = desc.upper()
        if "MERCADONA" in desc_upper:
            return "Mercadona"
        elif "AMZN" in desc_upper or "AMAZON" in desc_upper:
            return "Amazon"
        elif "UBER" in desc_upper:
            return "Uber"
        elif "NETFLIX" in desc_upper:
            return "Netflix"
        elif "SPOTIFY" in desc_upper:
            return "Spotify"
        elif "IBERDROLA" in desc_upper:
            return "Iberdrola"
        return desc.strip()

    def _create_ledger_entries_for_unreconciled(self, user_id: str, bank_account_id: str):
        """
        Crea asientos contables 1:1 para los movimientos bancarios que NO se pudieron
        reconciliar con facturas o Amazon, asignándoles una categoría por defecto para el Dashboard.
        """
        # Buscar transacciones de esta cuenta que no estén reconciliadas
        unreconciled = self.db.query(BankTransaction).filter(
            BankTransaction.bank_account_id == bank_account_id,
            BankTransaction.is_reconciled == False
        ).all()

        # Categoría por defecto para gastos generales y para ingresos
        default_expense_cat = self.db.query(Category).filter(Category.code == 'OCI_TECNOLOGIA').first()
        default_income_cat = self.db.query(Category).filter(Category.code == 'ING_OTROS').first()

        for tx in unreconciled:
            # Comprobar si ya existe un asiento asociado a esta transacción bancaria
            entry_exists = self.db.query(AccountingEntry).filter(
                AccountingEntry.bank_transaction_id == tx.id
            ).first()

            if not entry_exists:
                # Decidir categoría analítica basada en heurísticas simples
                is_income = tx.amount > 0
                cat_id = self._resolve_category_by_description(tx.raw_description, is_income)
                
                if not cat_id:
                    cat_id = default_income_cat.id if is_income else default_expense_cat.id

                entry = AccountingEntry(
                    user_id=user_id,
                    category_id=cat_id,
                    bank_transaction_id=tx.id,
                    entry_date=tx.transaction_date,
                    amount=tx.amount,
                    description=tx.cleaned_merchant or tx.raw_description,
                    reconciliation_type='DIRECT_BANK',
                    confidence_score=0.70
                )
                self.db.add(entry)
                
        self.db.commit()

    def _resolve_category_by_description(self, desc: str, is_income: bool) -> Optional[str]:
        """Resolución simple de categorías por palabras clave para movimientos directos."""
        desc_lower = desc.lower()
        code = None
        
        if is_income:
            if "nomina" in desc_lower or "salario" in desc_lower or "payroll" in desc_lower:
                code = 'ING_NOMINA'
            elif "freelance" in desc_lower or "factura" in desc_lower:
                code = 'ING_FREELANCE'
        else:
            if "mercadona" in desc_lower or "carrefour" in desc_lower or "lidl" in desc_lower or "super" in desc_lower:
                code = 'ALI_SUPER'
            elif "restaurante" in desc_lower or "burger" in desc_lower or "mcdonald" in desc_lower or "cafe" in desc_lower:
                code = 'ALI_REST'
            elif "gasolin" in desc_lower or "repsol" in desc_lower or "bp" in desc_lower:
                code = 'TRA_GASOLINA'
            elif "uber" in desc_lower or "cabify" in desc_lower or "metro" in desc_lower:
                code = 'TRA_PUBLICO'
            elif "netflix" in desc_lower or "spotify" in desc_lower:
                code = 'SUB_STREAMING'

        if code:
            category = self.db.query(Category).filter(Category.code == code).first()
            return category.id if category else None
        return None
