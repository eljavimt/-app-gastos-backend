import logging
import os
from typing import List, Optional
import pdfplumber
from pypdf import PdfReader
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
import google.generativeai as genai

from app.config import settings
from app.models.invoice import Invoice, InvoiceItem
from app.services.reconciliation import ReconciliationService

logger = logging.getLogger(__name__)

# ============================================================================
# ESQUEMAS DE SALIDA ESTRUCTURADA (Pydantic para Gemini API)
# ============================================================================

class InvoiceItemSchema(BaseModel):
    description: str = Field(description="Descripción clara del artículo, producto o servicio")
    quantity: float = Field(description="Cantidad de unidades", default=1.0)
    unit_price: float = Field(description="Precio por unidad")
    total_amount: float = Field(description="Importe total de esta línea (cantidad * precio unitario)")


class InvoiceSchema(BaseModel):
    emitter_name: str = Field(description="Nombre comercial, marca o razón social del emisor de la factura")
    emitter_tax_id: Optional[str] = Field(description="CIF, NIF, NIE, VAT ID o identificación fiscal del emisor")
    issue_date: str = Field(description="Fecha de emisión de la factura en formato ISO YYYY-MM-DD")
    base_amount: float = Field(description="Suma total de bases imponibles antes de impuestos")
    tax_rate: float = Field(description="Porcentaje del IVA aplicado principal (e.g. 21.00, 10.00, 4.00)")
    tax_amount: float = Field(description="Importe total cobrado en concepto de impuestos (IVA)")
    total_amount: float = Field(description="Importe final total de la factura a pagar")
    currency: str = Field(description="Divisa expresada en código ISO de 3 letras (ej: EUR, USD, GBP)", default="EUR")
    items: List[InvoiceItemSchema] = Field(description="Desglose completo de todos los artículos o líneas facturadas")


# ============================================================================
# SERVICIO PARSER DE FACTURAS
# ============================================================================

class InvoiceParserService:
    def __init__(self, db: Session):
        self.db = db

    def extract_text_from_pdf(self, file_path: str) -> str:
        """
        Extrae el texto bruto de un archivo PDF utilizando pdfplumber
        con un fallback a pypdf si ocurre algún error.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"El archivo no existe en la ruta especificada: {file_path}")

        extracted_text = ""
        
        # 1. Intentar con pdfplumber (excelente para mantener orden visual y tablas)
        try:
            with pdfplumber.open(file_path) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        extracted_text += text + "\n"
        except Exception as e:
            logger.warning(f"Error al extraer texto con pdfplumber: {str(e)}. Intentando fallback con pypdf.")
            
        # 2. Fallback a pypdf
        if not extracted_text.strip():
            try:
                reader = PdfReader(file_path)
                for page in reader.pages:
                    text = page.extract_text()
                    if text:
                        extracted_text += text + "\n"
            except Exception as e:
                logger.error(f"Error crítico al extraer texto del PDF: {str(e)}")
                raise ValueError("No se pudo extraer texto del archivo PDF proporcionado.")

        return extracted_text.strip()

    def parse_with_llm(self, text: str) -> InvoiceSchema:
        """
        Envía el texto de la factura a la API de Gemini utilizando Structured Outputs
        para forzar una respuesta JSON estructurada y validada según nuestro esquema Pydantic.
        """
        if not settings.GEMINI_API_KEY:
            raise ValueError("No se ha configurado la variable GEMINI_API_KEY en el entorno.")

        # Configurar la API de Gemini
        genai.configure(api_key=settings.GEMINI_API_KEY)
        
        # Usamos el modelo optimizado para texto y razonamiento rápido
        model = genai.GenerativeModel("gemini-1.5-flash")
        
        prompt = (
            "Eres un experto en contabilidad y procesamiento de documentos financieros.\n"
            "Analiza el siguiente texto extraído de una factura de compra y extrae todos los campos requeridos.\n"
            "Es crucial que identifiques correctamente al emisor, los importes del desglose fiscal y cada artículo "
            "comprado de forma individual.\n\n"
            f"Texto de la Factura:\n{text}"
        )

        try:
            # Llamada al modelo con esquema de respuesta estricto
            response = model.generate_content(
                prompt,
                generation_config=genai.GenerationConfig(
                    response_mime_type="application/json",
                    response_schema=InvoiceSchema,
                    temperature=0.1  # Baja temperatura para máxima precisión
                )
            )
            
            # El SDK de Pydantic y Gemini nos devuelve un JSON garantizado.
            # Lo validamos cargándolo en el Pydantic Schema.
            parsed_data = InvoiceSchema.model_validate_json(response.text)
            return parsed_data
            
        except Exception as e:
            logger.error(f"Error en la llamada a la API de Gemini: {str(e)}")
            raise RuntimeError(f"Error de procesamiento de IA: {str(e)}")

    def save_to_database(self, user_id: str, data: InvoiceSchema, file_path: Optional[str] = None) -> Invoice:
        """
        Guarda los datos extraídos de la factura en las tablas correspondientes
        (invoices e invoice_items).
        """
        # Crear la cabecera de la factura
        db_invoice = Invoice(
            user_id=user_id,
            emitter_name=data.emitter_name,
            emitter_tax_id=data.emitter_tax_id,
            issue_date=data.issue_date,
            base_amount=data.base_amount,
            tax_rate=data.tax_rate,
            tax_amount=data.tax_amount,
            total_amount=data.total_amount,
            currency=data.currency,
            file_storage_path=file_path,
            parsed_confidence=1.00  # En producción, esto podría calcularse
        )
        
        self.db.add(db_invoice)
        self.db.commit()
        self.db.refresh(db_invoice)

        # Crear cada ítem de la factura
        for item in data.items:
            db_item = InvoiceItem(
                invoice_id=db_invoice.id,
                description=item.description,
                quantity=item.quantity,
                unit_price=item.unit_price,
                total_amount=item.total_amount
            )
            self.db.add(db_item)
            
        self.db.commit()
        self.db.refresh(db_invoice)
        return db_invoice

    def process_and_reconcile(self, user_id: str, file_path: str) -> Invoice:
        """
        Orquesta el flujo completo de una nueva factura:
        1. Extrae el texto del PDF.
        2. Analiza el contenido mediante IA (Gemini).
        3. Guarda la factura y su desglose en la base de datos.
        4. Lanza el motor de conciliación para intentar emparejar esta factura
           con cargos bancarios existentes.
        """
        logger.info(f"Procesando factura para usuario {user_id} desde {file_path}")
        
        # 1. Extraer texto
        text = self.extract_text_from_pdf(file_path)
        if not text:
            raise ValueError("No se pudo extraer ningún texto del PDF. ¿El archivo está corrupto o protegido?")

        # 2. Analizar mediante LLM
        parsed_data = self.parse_with_llm(text)

        # 3. Guardar en DB
        invoice = self.save_to_database(user_id=user_id, data=parsed_data, file_path=file_path)
        logger.info(f"Factura guardada con ID {invoice.id} de {invoice.emitter_name}")

        # 4. Lanzar conciliación asíncrona/directa
        reconciler = ReconciliationService(self.db)
        reconciler.reconcile_user_transactions(user_id=user_id)

        return invoice
